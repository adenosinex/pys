"""OpenRouter 管理 — model price / provider / popularity tracker.

Ported from the user's myweb openrouter blueprint: the tracking logic
(watchlist, price history, provider annotation, popularity sampling)
is preserved verbatim; requests became urllib, the data directory
moved to .runtime/openrouter (seeded from his existing data), and
access is gated by the hub's unified login instead of an admin token.
"""
from __future__ import annotations

import os
import re
import json
import math
import time
import copy
import tempfile
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.error import URLError, HTTPError

import auth
import panel_auth
from page import PAGE_HTML

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / ".runtime" / "openrouter"
SEED_DIR = BASE_DIR / "seed"

MODELS_FILE = DATA_DIR / "models.json"
CONFIG_FILE = DATA_DIR / "config.json"
CACHE_FILE = DATA_DIR / "cache.json"
HISTORY_FILE = DATA_DIR / "history.json"

API_ROOT = "https://openrouter.ai/api/v1"


def _seed_data():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("models.json", "config.json", "cache.json", "history.json"):
        target = DATA_DIR / name
        source = SEED_DIR / name
        if not target.exists() and source.exists():
            target.write_bytes(source.read_bytes())


_seed_data()

DEFAULT_MODELS = [
    {"id": "deepseek/deepseek-chat-v3.1", "name": "DeepSeek Chat V3.1", "enabled": True},
    {"id": "deepseek/deepseek-r1", "name": "DeepSeek R1", "enabled": True},
    {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash", "enabled": True},
    {"id": "xiaomi/mimo-v2.5", "name": "MiMo V2.5", "enabled": True},
]

DEFAULT_CONFIG = {
    "usd_cny_rate": 7.2,
    "proxy": "",
    "refresh_interval_seconds": 86400,
    "manual_refresh_cooldown_seconds": 60,
    "api_timeout_seconds": 20,
    "provider_workers": 4,
    "history_limit": 180,
    "low_price_ratio": 0.5,
    "high_price_ratio": 2.0,
    "latency_warning_ms": 5000,
    "uptime_warning_pct": 95,
}

EMPTY_CACHE = {
    "updated_at": None,
    "last_attempt_at": None,
    "last_error": None,
    "models": {},
    "popularity": {
        "available": False,
        "as_of": None,
        "start_date": None,
        "end_date": None,
        "error": None,
        "items": {},
    },
}

file_lock = threading.RLock()
refresh_lock = threading.Lock()
state_lock = threading.RLock()

refresh_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
}

last_manual = 0.0
last_auto = 0.0




def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, default):
    with file_lock:
        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (OSError, ValueError):
            return copy.deepcopy(default)


def write_json(path, data):
    with file_lock:
        path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())

            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def load_config():
    data = read_json(CONFIG_FILE, {})
    if not isinstance(data, dict):
        data = {}
    return {**DEFAULT_CONFIG, **data}


def load_models():
    data = read_json(MODELS_FILE, None)

    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        data = {"models": copy.deepcopy(DEFAULT_MODELS)}
        write_json(MODELS_FILE, data)

    result = []
    seen = set()

    for item in data["models"]:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        if not isinstance(mid, str) or not mid or mid in seen:
            continue
        seen.add(mid)
        result.append({
            "id": mid,
            "name": str(item.get("name") or mid),
            "enabled": item.get("enabled") is not False,
        })

    return result


def load_cache():
    data = read_json(CACHE_FILE, EMPTY_CACHE)
    if not isinstance(data, dict):
        data = {}
    cache = {**copy.deepcopy(EMPTY_CACHE), **data}

    if not isinstance(cache.get("models"), dict):
        cache["models"] = {}

    if not isinstance(cache.get("popularity"), dict):
        cache["popularity"] = copy.deepcopy(EMPTY_CACHE["popularity"])

    return cache


def load_history():
    value = read_json(HISTORY_FILE, {})
    return value if isinstance(value, dict) else {}


def finite_number(value):
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
        return float(d) if d.is_finite() else None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def price_1m(value):
    num = finite_number(value)
    return round(num * 1_000_000, 9) if num is not None else None


def int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def safe_list(value):
    return value if isinstance(value, list) else []


class RouterError(Exception):
    pass


def api_get(url, config, auth_required=False, params=None):
    headers = {"Accept": "application/json"}

    key = os.getenv("OPENROUTER_API_KEY", "").strip()

    if auth_required and not key:
        raise RouterError("未配置 OPENROUTER_API_KEY")

    if key:
        headers["Authorization"] = f"Bearer {key}"

    proxy = (
        config.get("proxy")
        or os.getenv("OPENROUTER_PROXY", "")
    ).strip()

    if params:
        url = url + ("&" if "?" in url else "?") + urlencode(params)

    request = Request(url, headers=headers)
    opener = build_opener()
    if proxy:
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))

    try:
        with opener.open(request, timeout=int(config["api_timeout_seconds"])) as response:
            raw = response.read().decode("utf-8")

    except (URLError, HTTPError, OSError) as exc:
        raise RouterError(f"OpenRouter 请求失败：{exc}") from exc

    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RouterError("OpenRouter 返回无效 JSON") from exc


def parse_model(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None

    pricing = raw.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}

    architecture = raw.get("architecture")
    if not isinstance(architecture, dict):
        architecture = {}

    mid = raw["id"]

    return {
        "id": mid,
        "name": raw.get("name") or mid,
        "found": True,
        "description": raw.get("description") or "",
        "input_price": price_1m(pricing.get("prompt")),
        "output_price": price_1m(pricing.get("completion")),
        "cache_read_price": price_1m(pricing.get("input_cache_read")),
        "cache_write_price": price_1m(pricing.get("input_cache_write")),
        "context_length": int_or_none(raw.get("context_length")),
        "architecture": architecture.get("modality"),
        "supported_parameters": safe_list(raw.get("supported_parameters")),
        "canonical_slug": raw.get("canonical_slug"),
        "providers": [],
    }


def parse_provider(raw):
    if not isinstance(raw, dict):
        return None

    pricing = raw.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}

    return {
        "name": str(raw.get("provider_name") or raw.get("name") or "未知供应商"),
        "tag": raw.get("tag"),
        "status": raw.get("status"),
        "quantization": raw.get("quantization"),
        "input_price": price_1m(pricing.get("prompt")),
        "output_price": price_1m(pricing.get("completion")),
        "cache_read_price": price_1m(pricing.get("input_cache_read")),
        "cache_write_price": price_1m(pricing.get("input_cache_write")),
        "context_length": int_or_none(raw.get("context_length")),
        "latency_ms": finite_number(raw.get("latency_last_30m")),
        "throughput_tps": finite_number(raw.get("throughput_last_30m")),
        "uptime_1d": finite_number(raw.get("uptime_last_1d")),
    }


def fetch_providers(model_id, config):
    slug = "/".join(quote(p, safe="") for p in model_id.split("/"))

    payload = api_get(
        f"{API_ROOT}/models/{slug}/endpoints",
        config,
    )

    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    endpoints = data.get("endpoints", []) if isinstance(data, dict) else []

    return [
        p for p in (parse_provider(raw) for raw in safe_list(endpoints))
        if p is not None
    ]


# ============================================================
# 全平台模型受欢迎程度
#
# 官方 rankings-daily：
# - 每日 Top 50 公开模型 + other 汇总行
# - total_tokens = prompt + completion
# - 不提供全平台请求次数
# - 不上榜不代表调用量为零
# ============================================================

def fetch_popularity(config):
    today_utc = datetime.now(timezone.utc).date()
    end_date = today_utc - timedelta(days=1)
    start_date = end_date - timedelta(days=6)

    payload = api_get(
        f"{API_ROOT}/datasets/rankings-daily",
        config,
        auth_required=True,
        params={
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
    )

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RouterError("模型热度接口返回格式异常")

    totals = {}
    days = {}

    for row in payload["data"]:
        if not isinstance(row, dict):
            continue

        slug = row.get("model_permaslug")
        if not isinstance(slug, str) or slug == "other":
            continue

        raw_tokens = row.get("total_tokens")

        try:
            tokens = int(raw_tokens)
        except (ValueError, TypeError):
            continue

        if tokens < 0:
            continue

        totals[slug] = totals.get(slug, 0) + tokens
        days.setdefault(slug, set()).add(str(row.get("date", "")))

    # 注意：这里是 7 天每日 Top 50 的合并样本排名，
    # 并不表示所有模型完整的 7 日真实排名。
    sorted_models = sorted(
        totals.items(),
        key=lambda pair: (-pair[1], pair[0]),
    )

    items = {}

    for rank, (slug, tokens) in enumerate(sorted_models, 1):
        items[slug] = {
            "tokens_7d": tokens,
            "rank": rank,
            "listed_days": len(days.get(slug, set())),
            "coverage": "daily_top50_sample",
        }

    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    return {
        "available": True,
        "as_of": meta.get("as_of"),
        "start_date": meta.get("start_date", start_date.isoformat()),
        "end_date": meta.get("end_date", end_date.isoformat()),
        "error": None,
        "items": items,
        "scope": "7日每日Top50公开流量样本",
    }


def popularity_for_model(model, popularity):
    if not popularity.get("available"):
        return {
            "status": "unavailable",
            "tokens_7d": None,
            "rank": None,
            "listed_days": None,
        }

    items = popularity.get("items", {})
    if not isinstance(items, dict):
        items = {}

    # 官方排行榜使用 model_permaslug。
    # 先精确匹配模型 ID，再匹配官方 canonical_slug。
    # 不做模糊匹配，避免不同版本数据合并。
    keys = [model.get("id"), model.get("canonical_slug")]

    for key in keys:
        if isinstance(key, str) and key in items:
            return {"status": "listed", **items[key]}

    return {
        "status": "unranked",
        "tokens_7d": None,
        "rank": None,
        "listed_days": None,
    }


# ============================================================
# 价格异常识别
# ============================================================

def median(values):
    values = sorted(values)
    if not values:
        return None

    n = len(values)
    mid = n // 2

    return (
        values[mid]
        if n % 2
        else (values[mid - 1] + values[mid]) / 2
    )


def annotate_providers(providers, config):
    fields = ("input_price", "output_price", "cache_read_price")

    baselines = {}

    for key in fields:
        values = [
            float(p[key])
            for p in providers
            if finite_number(p.get(key)) is not None and float(p[key]) > 0
        ]
        baselines[key] = median(values)

    for provider in providers:
        flags = {}
        special = []

        for key in fields:
            val = finite_number(provider.get(key))
            baseline = baselines[key]

            if val is None:
                flags[key] = "unknown"
            elif val == 0:
                flags[key] = "free"
                if "免费价格" not in special:
                    special.append("免费价格")
            elif baseline and val < baseline * config["low_price_ratio"]:
                flags[key] = "low"
                if "超低价" not in special:
                    special.append("超低价")
            elif baseline and val > baseline * config["high_price_ratio"]:
                flags[key] = "high"
                if "高价异常" not in special:
                    special.append("高价异常")
            else:
                flags[key] = "normal"

        latency = finite_number(provider.get("latency_ms"))
        uptime = finite_number(provider.get("uptime_1d"))

        if latency is not None and latency > config["latency_warning_ms"]:
            special.append("高延迟")

        if uptime is not None and uptime < config["uptime_warning_pct"]:
            special.append("低可用率")

        if provider.get("quantization"):
            special.append("量化")

        provider["price_flags"] = flags
        provider["special_tags"] = special

    return providers


# ============================================================
# 刷新任务、缓存与历史
# ============================================================

def is_stale(cache, config, models=None):
    updated = cache.get("updated_at")

    if not updated:
        return True

    try:
        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        if (datetime.now(timezone.utc) - dt).total_seconds() >= \
                config["refresh_interval_seconds"]:
            return True
    except (ValueError, TypeError):
        return True

    if models is None:
        models = load_models()

    cached_models = cache.get("models", {})

    return any(
        m["enabled"] and m["id"] not in cached_models
        for m in models
    )


def store_history(cache, config):
    with file_lock:
        history = load_history()

        for mid, model in cache["models"].items():
            if not model.get("found"):
                continue

            points = history.get(mid, [])

            if not isinstance(points, list):
                points = []

            point = {
                "time": cache["updated_at"],
                "input_price": model.get("input_price"),
                "output_price": model.get("output_price"),
                "cache_read_price": model.get("cache_read_price"),
            }

            if points and points[-1].get("time") == point["time"]:
                points[-1] = point
            else:
                points.append(point)

            history[mid] = points[-config["history_limit"]:]

        write_json(HISTORY_FILE, history)


def refresh_once():
    config = load_config()
    models = load_models()
    cache = load_cache()

    enabled = [m for m in models if m["enabled"]]

    if not enabled:
        cache["last_error"] = "没有已启用的模型"
        cache["last_attempt_at"] = now_iso()
        write_json(CACHE_FILE, cache)
        return {"ok": False, "error": cache["last_error"]}

    try:
        payload = api_get(f"{API_ROOT}/models", config)

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RouterError("模型列表格式异常")

        index = {}
        for raw in payload["data"]:
            item = parse_model(raw)
            if item:
                index[item["id"]] = item

        timestamp = now_iso()
        rebuilt = copy.deepcopy(cache["models"])
        found_ids = []

        for configured in enabled:
            mid = configured["id"]

            if mid not in index:
                rebuilt[mid] = {
                    "id": mid,
                    "name": configured["name"],
                    "found": False,
                    "fetched_at": timestamp,
                    "providers": [],
                    "input_price": None,
                    "output_price": None,
                    "cache_read_price": None,
                }
                continue

            item = index[mid]
            item["providers"] = rebuilt.get(mid, {}).get("providers", [])
            item["fetched_at"] = timestamp
            rebuilt[mid] = item
            found_ids.append(mid)

        def fetch_one(mid):
            try:
                return mid, fetch_providers(mid, config), None
            except Exception as exc:
                return mid, None, str(exc)

        if found_ids:
            with ThreadPoolExecutor(
                max_workers=min(config["provider_workers"], len(found_ids))
            ) as pool:

                jobs = [pool.submit(fetch_one, mid) for mid in found_ids]

                for job in as_completed(jobs):
                    mid, providers, error = job.result()

                    if error:
                        rebuilt[mid]["provider_error"] = error
                    else:
                        rebuilt[mid]["providers"] = annotate_providers(
                            providers, config
                        )
                        rebuilt[mid]["provider_error"] = None

        # 热度接口与价格接口分别处理，避免热度请求失败导致价格不可用
        try:
            popularity = fetch_popularity(config)
        except Exception as exc:
            popularity = copy.deepcopy(cache.get("popularity", {}))
            if not isinstance(popularity, dict):
                popularity = {}
            popularity["error"] = str(exc)
            if not popularity.get("available"):
                popularity["available"] = False

        cache.update({
            "models": rebuilt,
            "popularity": popularity,
            "updated_at": timestamp,
            "last_attempt_at": timestamp,
            "last_error": None,
        })

        write_json(CACHE_FILE, cache)
        store_history(cache, config)

        return {
            "ok": True,
            "found": len(found_ids),
            "not_found": len(enabled) - len(found_ids),
            "updated_at": timestamp,
            "popularity_available": popularity.get("available", False),
        }

    except Exception as exc:
        cache["last_attempt_at"] = now_iso()
        cache["last_error"] = str(exc)
        write_json(CACHE_FILE, cache)
        return {"ok": False, "error": str(exc)}


def running():
    with state_lock:
        return refresh_state["running"]


def worker():
    try:
        result = refresh_once()
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    finally:
        with state_lock:
            refresh_state["running"] = False
            refresh_state["finished_at"] = now_iso()
            refresh_state["result"] = result
        refresh_lock.release()


def start_refresh(manual=False):
    global last_manual, last_auto

    config = load_config()

    with state_lock:
        now = time.monotonic()

        if refresh_state["running"]:
            return False, "刷新正在进行", 409

        if manual and now - last_manual < config["manual_refresh_cooldown_seconds"]:
            return False, "操作过于频繁", 429

        if not refresh_lock.acquire(blocking=False):
            return False, "刷新任务繁忙", 409

        if manual:
            last_manual = now
        else:
            last_auto = now

        refresh_state["running"] = True
        refresh_state["started_at"] = now_iso()

    try:
        threading.Thread(
            target=worker,
            daemon=True,
            name="openrouter-refresh",
        ).start()
    except Exception:
        with state_lock:
            refresh_state["running"] = False
        refresh_lock.release()
        raise

    return True, "后台刷新已启动", 202


def auto_refresh():
    cache = load_cache()
    config = load_config()

    if not is_stale(cache, config):
        return

    with state_lock:
        if refresh_state["running"]:
            return
        if time.monotonic() - last_auto < 60:
            return

    start_refresh()


def response_models():
    config = load_config()
    cache = load_cache()
    configured = load_models()
    popularity = cache["popularity"]

    models = []

    for m in configured:
        raw = cache["models"].get(m["id"], {})

        item = {
            **raw,
            "id": m["id"],
            "name": m["name"],
            "enabled": m["enabled"],
            "found": bool(raw.get("found")),
        }

        item["popularity"] = popularity_for_model(item, popularity)
        models.append(item)

    return {
        "models": models,
        "status": (
            "refreshing" if running()
            else "error" if cache.get("last_error") and not cache.get("updated_at")
            else "cached" if cache.get("last_error") or is_stale(cache, config, configured)
            else "updated" if cache.get("updated_at")
            else "empty"
        ),
        "updated_at": cache.get("updated_at"),
        "last_error": cache.get("last_error"),
        "usd_cny_rate": config["usd_cny_rate"],
        "popularity": {
            key: popularity.get(key)
            for key in ("available", "as_of", "start_date", "end_date", "error", "scope")
        },
        "refreshing": running(),
    }


# ============================================================
# App Hub WSGI shell: unified login + routing.
# All /api/* endpoints require the hub login (panel SSO session or
# this project's own account); the page itself is served openly and
# gates client-side, like the other hub projects.
# ============================================================
import json as _json
from urllib.parse import parse_qs as _parse_qs

COOKIE_NAME = "orouter_session"

_account, _account_from_env = auth.load_account()
_account_lock = threading.Lock()


def _http_json(start_response, status, payload, extra_headers=None):
    body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"),
               ("Content-Length", str(len(body)))]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def _status_line(code):
    return {200: "200 OK", 201: "201 Created", 202: "202 Accepted",
            400: "400 Bad Request", 401: "401 Unauthorized",
            404: "404 Not Found", 409: "409 Conflict",
            429: "429 Too Many Requests",
            503: "503 Service Unavailable"}.get(code, "200 OK")


def _read_json(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > 262144:
        return None
    try:
        return _json.loads(environ["wsgi.input"].read(length).decode("utf-8") or "null")
    except Exception:
        return None


def _cookie_path(environ):
    prefix = environ.get("HTTP_X_FORWARDED_PREFIX") or ""
    return (prefix + "/") if prefix else "/"


def _session_cookie(environ, token, max_age):
    return ("Set-Cookie", "%s=%s; Path=%s; HttpOnly; SameSite=Strict; Max-Age=%d"
            % (COOKIE_NAME, token, _cookie_path(environ), max_age))


def _current_account():
    with _account_lock:
        return _account


def _auth_state(environ):
    account = _current_account()
    cookie = environ.get("HTTP_COOKIE", "")
    if account:
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value and account.check_token(value):
                return True, "account", account.username
    panel_user = panel_auth.panel_username(cookie)
    if panel_user:
        return True, "panel", panel_user
    return False, None, None


def _authed(environ):
    return _auth_state(environ)[0]


def handle_setup(environ, start_response):
    global _account
    with _account_lock:
        if _account is not None:
            return _http_json(start_response, "409 Conflict",
                              {"ok": False, "error": "setup_done", "message": "账户已设置，设置入口已关闭。"})
        data = _read_json(environ) or {}
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not (1 <= len(username) <= 128):
            return _http_json(start_response, "400 Bad Request",
                              {"ok": False, "error": "bad_username", "message": "账户名需为 1–128 个字符。"})
        if len(password) < 10:
            return _http_json(start_response, "400 Bad Request",
                              {"ok": False, "error": "short_password", "message": "密码至少 10 位。"})
        _account = auth.store_account(username, password)
        account = _account
    return _http_json(start_response, "200 OK", {"ok": True, "username": account.username},
                      [_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_login(environ, start_response):
    account = _current_account()
    if account is None:
        return _http_json(start_response, "503 Service Unavailable",
                          {"ok": False, "error": "not_configured", "message": "尚未设置账户。"})
    data = _read_json(environ) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if username != account.username or not account.check_password(password):
        return _http_json(start_response, "401 Unauthorized",
                          {"ok": False, "error": "bad_credentials", "message": "账户名或密码不正确。"})
    return _http_json(start_response, "200 OK", {"ok": True, "username": account.username},
                      [_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_session(environ, start_response):
    account = _current_account()
    authed, via, username = _auth_state(environ)
    return _http_json(start_response, "200 OK", {
        "ok": True,
        "setup_required": account is None,
        "authenticated": authed,
        "via": via,
        "username": username if authed else None,
    })


MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._~:/+@-]{1,200}$")


def handle_config(environ, start_response, method):
    if method == "GET":
        return _http_json(start_response, "200 OK", {"settings": load_config()})

    data = _read_json(environ)
    if not isinstance(data, dict):
        return _http_json(start_response, "400 Bad Request", {"error": "无效 JSON"})

    config = load_config()

    limits = {
        "usd_cny_rate": (0.000001, 1000),
        "refresh_interval_seconds": (60, 2592000),
        "manual_refresh_cooldown_seconds": (0, 86400),
        "api_timeout_seconds": (3, 120),
        "provider_workers": (1, 12),
        "history_limit": (2, 2000),
        "low_price_ratio": (0.01, 0.99),
        "high_price_ratio": (1.01, 100),
        "latency_warning_ms": (100, 600000),
        "uptime_warning_pct": (0, 100),
    }

    integer_keys = {
        "refresh_interval_seconds",
        "manual_refresh_cooldown_seconds",
        "api_timeout_seconds",
        "provider_workers",
        "history_limit",
    }

    for key, value in data.items():
        if key == "proxy":
            if not isinstance(value, str) or len(value) > 1000:
                return _http_json(start_response, "400 Bad Request", {"error": "代理地址无效"})
            if value and not value.startswith(("http://", "https://", "socks5://")):
                return _http_json(start_response, "400 Bad Request", {"error": "代理协议无效"})
            config[key] = value.strip()
            continue

        if key not in limits:
            return _http_json(start_response, "400 Bad Request", {"error": f"不支持的配置：{key}"})

        try:
            val = float(value)
        except (TypeError, ValueError):
            return _http_json(start_response, "400 Bad Request", {"error": f"{key} 必须为数字"})

        low, high = limits[key]

        if not math.isfinite(val) or not low <= val <= high:
            return _http_json(start_response, "400 Bad Request", {"error": f"{key} 超出允许范围"})

        if key in integer_keys:
            if not val.is_integer():
                return _http_json(start_response, "400 Bad Request", {"error": f"{key} 必须为整数"})
            val = int(val)

        config[key] = val

    write_json(CONFIG_FILE, config)
    return _http_json(start_response, "200 OK", {"ok": True, "settings": config})


def handle_add_model(environ, start_response):
    data = _read_json(environ)
    if not isinstance(data, dict):
        return _http_json(start_response, "400 Bad Request", {"error": "无效 JSON"})

    mid = data.get("id")
    name = data.get("name") or mid
    enabled = data.get("enabled", True)

    if not isinstance(mid, str) or not MODEL_ID_RE.fullmatch(mid) or "/" not in mid:
        return _http_json(start_response, "400 Bad Request", {"error": "Model ID 格式错误"})

    if not isinstance(name, str) or len(name) > 100:
        return _http_json(start_response, "400 Bad Request", {"error": "模型名称无效"})

    if not isinstance(enabled, bool):
        return _http_json(start_response, "400 Bad Request", {"error": "enabled 必须为布尔值"})

    with file_lock:
        models = load_models()

        if any(m["id"] == mid for m in models):
            return _http_json(start_response, "409 Conflict", {"error": "模型已存在"})

        models.append({
            "id": mid,
            "name": name.strip() or mid,
            "enabled": enabled,
        })
        write_json(MODELS_FILE, {"models": models})

    return _http_json(start_response, "201 Created", {"ok": True, "models": models})


def handle_edit_model(environ, start_response, method, model_id):
    with file_lock:
        models = load_models()
        target = next((m for m in models if m["id"] == model_id), None)

        if target is None:
            return _http_json(start_response, "404 Not Found", {"error": "模型不存在"})

        if method == "DELETE":
            models = [m for m in models if m["id"] != model_id]
            write_json(MODELS_FILE, {"models": models})
            return _http_json(start_response, "200 OK", {"ok": True, "models": models})

        data = _read_json(environ)

        if not isinstance(data, dict):
            return _http_json(start_response, "400 Bad Request", {"error": "无效 JSON"})

        if "name" in data:
            name = data["name"]
            if not isinstance(name, str) or not name.strip() or len(name) > 100:
                return _http_json(start_response, "400 Bad Request", {"error": "模型名称无效"})
            target["name"] = name.strip()

        if "enabled" in data:
            if not isinstance(data["enabled"], bool):
                return _http_json(start_response, "400 Bad Request", {"error": "enabled 必须为布尔值"})
            target["enabled"] = data["enabled"]

        write_json(MODELS_FILE, {"models": models})

    return _http_json(start_response, "200 OK", {"ok": True, "models": models})


def handle_page(environ, start_response):
    body = PAGE_HTML.encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                              ("Content-Length", str(len(body)))])
    return [body]


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"

    if path == "/" and method == "GET":
        return handle_page(environ, start_response)
    if path == "/health" and method == "GET":
        return _http_json(start_response, "200 OK", {"ok": True})
    if path == "/api/session" and method == "GET":
        return handle_session(environ, start_response)
    if path == "/api/setup" and method == "POST":
        return handle_setup(environ, start_response)
    if path == "/api/login" and method == "POST":
        return handle_login(environ, start_response)
    if path == "/api/logout" and method == "POST":
        return _http_json(start_response, "200 OK", {"ok": True},
                          [_session_cookie(environ, "", 0)])

    if not _authed(environ):
        return _http_json(start_response, "401 Unauthorized",
                          {"ok": False, "error": "unauthorized", "message": "请先登录。"})

    if path == "/api/models" and method == "GET":
        auto_refresh()
        return _http_json(start_response, "200 OK", response_models())
    if path == "/api/models" and method == "POST":
        return handle_add_model(environ, start_response)
    if path.startswith("/api/models/"):
        model_id = unquote(path[len("/api/models/"):])
        if method == "GET":
            model = next(
                (m for m in response_models()["models"] if m["id"] == model_id),
                None,
            )
            if model is None:
                return _http_json(start_response, "404 Not Found", {"error": "模型不存在"})
            return _http_json(start_response, "200 OK", {"model": model})
        if method in ("PUT", "DELETE"):
            return handle_edit_model(environ, start_response, method, model_id)
    if path.startswith("/api/history/") and method == "GET":
        model_id = unquote(path[len("/api/history/"):])
        return _http_json(start_response, "200 OK", {
            "id": model_id,
            "history": load_history().get(model_id, []),
        })
    if path == "/api/status" and method == "GET":
        cache = load_cache()
        return _http_json(start_response, "200 OK", {
            "updated_at": cache.get("updated_at"),
            "last_error": cache.get("last_error"),
            "refresh": copy.deepcopy(refresh_state),
        })
    if path == "/api/refresh" and method == "POST":
        ok, message, code = start_refresh(manual=True)
        return _http_json(start_response, _status_line(code),
                          {"ok": ok, "message": message, "error": None if ok else message})
    if path == "/api/config" and method in ("GET", "POST"):
        return handle_config(environ, start_response, method)

    return _http_json(start_response, "404 Not Found",
                      {"ok": False, "error": "not_found", "message": "接口不存在。"})


app = application
