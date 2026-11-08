# -*- coding: utf-8 -*-
"""API 中转检测 (apirelay): AI API 集中转发 + 上游中转站检测。

- 上游管理: 网页添加 (名称 / base_url / API key 导入), key 只写不
  回显, 存在平台数据目录 credentials.json (0600)。
- 自动检测: 添加后自动跑一轮, 之后可手动重测单个或全部。每个 key
  最多测两个模型: 从上游 /v1/models 里挑最新 Opus 与最新 Sol, 各发
  一个固定简单问题, 记录 token 用量核对 (上游自报 vs 本地估算)、
  延迟、对端 IP 与归属 (ip-api + Scamalytics, 一周缓存)、机器特点
  推测。结果逐行落 tests.csv, 网页可下载。
- 透明转发: /r/<上游名>/v1/<path> 原样透传 (含流式), 服务端注入
  上游 key; 调用方持本服务的转发令牌 (页面设置, 只写不回显)。
  每次转发记一条日志 (SQLite), 网页可看最近记录与导出 CSV。
- 项目内无任何验证代码: 身份网关在面板层 (平台规则)。Stdlib only。
"""
import csv
import html as html_mod
import http.client
import io
import ipaddress
import json
import math
import os
import re
import secrets as _sec
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(BASE_DIR, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(BASE_DIR, ".runtime")


DATA_DIR = _data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
CRED_PATH = os.path.join(DATA_DIR, "credentials.json")
TESTS_CSV = os.path.join(DATA_DIR, "tests.csv")
LAST_TESTS = os.path.join(DATA_DIR, "last_tests.json")
IP_CACHE = os.path.join(DATA_DIR, "ip_cache.json")
RELAY_DB = os.path.join(DATA_DIR, "relay_log.db")

TEST_PROMPT = "Answer with a single short sentence: what is the capital of France?"
TEST_MAX_TOKENS = 64
CHAT_FRAMING_TOKENS = 4
RATIO_HI = 1.5
RATIO_LO = 0.5

# ------------------------------------------------------------ store
_store_lock = threading.Lock()


def _load_store():
    try:
        with open(CRED_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            if not isinstance(d.get("upstreams"), dict):
                d["upstreams"] = {}
            if not isinstance(d.get("relay_token"), str):
                d["relay_token"] = ""
            return d
    except Exception:
        pass
    return {"upstreams": {}, "relay_token": ""}


def _save_store(d):
    tmp = CRED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, CRED_PATH)


# ------------------------------------------------- token estimation
try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")
    EST_METHOD = "tiktoken-cl100k"
except Exception:
    _enc = None
    EST_METHOD = "char-est"


def _est_tokens(text):
    """Local token estimate for the same text the upstream billed.
    tiktoken when available; else a character heuristic (CJK chars
    weigh ~0.67 token, other chars ~0.25). The method is recorded in
    every CSV row (est_method) so comparisons stay honest."""
    if not text:
        return 0
    if _enc is not None:
        return len(_enc.encode(text))
    cjk = 0
    other = 0
    for ch in text:
        o = ord(ch)
        if 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or o >= 0x20000:
            cjk += 1
        else:
            other += 1
    return int(math.ceil(cjk * 0.67 + other * 0.25))


# ------------------------------------------------------------ http
def _http(method, url, headers=None, body=None, ctimeout=10, rtimeout=30,
          max_read=None):
    """One HTTP(S) round trip via http.client so the actual peer IP
    is visible. Returns a dict; never raises. Every socket operation
    carries a timeout (platform rule)."""
    out = {"status": 0, "headers": [], "body": b"", "peer_ip": "",
           "ttfb_ms": "", "total_ms": "", "error": ""}
    t0 = time.time()
    conn = None
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "http").lower()
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        cls = (http.client.HTTPSConnection if scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(host, port, timeout=ctimeout)
        conn.connect()
        try:
            out["peer_ip"] = conn.sock.getpeername()[0]
        except Exception:
            pass
        if conn.sock is not None:
            conn.sock.settimeout(rtimeout)
        hdrs = {"User-Agent": "apirelay/1.0", "Accept-Encoding": "identity"}
        for k, v in (headers or {}).items():
            hdrs[k] = v
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        out["status"] = resp.status
        out["headers"] = resp.getheaders()
        out["ttfb_ms"] = int((time.time() - t0) * 1000)
        chunks = []
        got = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
            if max_read and got > max_read:
                break
        out["body"] = b"".join(chunks)
        out["total_ms"] = int((time.time() - t0) * 1000)
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        out["total_ms"] = int((time.time() - t0) * 1000)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


# ------------------------------------------------- ip intel (geo)
_ip_lock = threading.Lock()
_BIG_CLOUD = ("amazon", "aws", "google", "microsoft", "azure",
              "alibaba", "aliyun", "tencent", "oracle", "digitalocean",
              "vultr", "choopa", "linode", "akamai", "hetzner", "ovh",
              "cloudflare", "leaseweb", "contabo")


def _is_private_ip(ip):
    try:
        a = ipaddress.ip_address(ip)
        return (a.is_private or a.is_loopback or a.is_link_local
                or a.is_reserved or a.is_multicast)
    except Exception:
        return True


def _machine_guess(country, asn, org, hosting, proxy, mobile, private):
    """One-line inference from ASN/org/hosting signals. It is a
    guess and is labelled as such; raw fields sit beside it."""
    if private:
        return "本地/内网地址(未做公网归属查询)"
    low = ((org or "") + " " + (asn or "")).lower()
    if any(k in low for k in _BIG_CLOUD):
        g = "推测: 知名云厂商机房托管"
    elif hosting:
        g = "推测: 机房托管"
    elif mobile:
        g = "推测: 移动网络出口, 可能为家宽/蜂窝"
    else:
        g = "推测: 非托管段, 可能为家宽"
    if proxy:
        g += "; 带代理标记"
    return g


def _ip_info(ip):
    """Geo + fraud intel for one IP with a one-week cache (same
    approach as subconv). Private IPs short-circuit."""
    blank = {"country": "", "city": "", "asn": "", "org": "",
             "fraud": "", "guess": ""}
    if not ip:
        return blank
    if _is_private_ip(ip):
        return {"country": "内网", "city": "", "asn": "",
                "org": "私有地址", "fraud": "",
                "guess": _machine_guess("", "", "", False, False, False, True)}
    with _ip_lock:
        try:
            with open(IP_CACHE, encoding="utf-8") as fh:
                cache = json.load(fh)
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}
        ent = cache.get(ip)
        if ent and time.time() - float(ent.get("fetched_at", 0)) < 7 * 86400:
            return {"country": ent.get("country", ""),
                    "city": ent.get("city", ""),
                    "asn": ent.get("asn", ""), "org": ent.get("org", ""),
                    "fraud": ent.get("fraud", ""),
                    "guess": ent.get("guess", "")}
    info = {"country": "", "city": "", "asn": "", "org": "",
            "hosting": False, "proxy": False, "mobile": False,
            "fraud": ""}
    fields = "status,message,country,city,isp,org,as,hosting,proxy,mobile,query"
    r = _http("GET", "http://ip-api.com/json/%s?fields=%s" % (ip, fields),
              ctimeout=8, rtimeout=15, max_read=65536)
    if r["status"] == 200 and not r["error"]:
        try:
            d = json.loads(r["body"].decode("utf-8", "replace"))
            if d.get("status") == "success":
                info["country"] = d.get("country", "") or ""
                info["city"] = d.get("city", "") or ""
                info["asn"] = d.get("as", "") or ""
                info["org"] = d.get("org") or d.get("isp") or ""
                info["hosting"] = bool(d.get("hosting"))
                info["proxy"] = bool(d.get("proxy"))
                info["mobile"] = bool(d.get("mobile"))
        except Exception:
            pass
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0 Safari/537.36"}
    r2 = _http("GET", "https://scamalytics.com/ip/" + ip, headers=ua,
               ctimeout=8, rtimeout=15, max_read=400000)
    if r2["status"] == 200 and not r2["error"]:
        m = re.search(r"Fraud Score:\s*(\d+)",
                      r2["body"].decode("utf-8", "replace"))
        if m:
            info["fraud"] = int(m.group(1))
    guess = _machine_guess(info["country"], info["asn"], info["org"],
                           info["hosting"], info["proxy"], info["mobile"],
                           False)
    ent = {"fetched_at": time.time(), "country": info["country"],
           "city": info["city"], "asn": info["asn"], "org": info["org"],
           "fraud": info["fraud"], "guess": guess}
    with _ip_lock:
        try:
            with open(IP_CACHE, encoding="utf-8") as fh:
                cache = json.load(fh)
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}
        cache[ip] = ent
        tmp = IP_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)
        os.replace(tmp, IP_CACHE)
    return {"country": info["country"], "city": info["city"],
            "asn": info["asn"], "org": info["org"],
            "fraud": info["fraud"], "guess": guess}


# ------------------------------------------------------ detection
TESTS_HEADER = ["ts", "upstream", "model", "http_status", "error",
                "latency_ms", "ttfb_ms", "reported_prompt_tokens",
                "reported_completion_tokens", "reported_total_tokens",
                "est_prompt_tokens", "est_completion_tokens",
                "est_total_tokens", "token_ratio", "token_flag",
                "est_method", "peer_ip", "ip_country", "ip_city",
                "ip_asn", "ip_org", "fraud_score", "machine_guess"]
_csv_lock = threading.Lock()
_testing = {}
_testing_lock = threading.Lock()


def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _version_key(mid, family):
    low = mid.lower()
    dm = re.search(r"(20\d{2})(\d{2})(\d{2})", low)
    date = int("".join(dm.groups())) if dm else 0
    bare = re.sub(r"-?20\d{6}", "", low)
    idx = bare.find(family)
    rest = bare[idx + len(family):] if idx >= 0 else bare
    m = re.match(r"[-_. ]?(\d{1,3})(?:[-_.](\d{1,3}))?(?:[-_.](\d{1,3}))?",
                 rest)
    ver = (0, 0, 0)
    if m:
        ver = tuple(int(g) if g else 0 for g in m.groups())
    return (ver, date, bare)


def _pick_latest(ids, family):
    cands = [i for i in ids if family in i.lower()]
    if not cands:
        return ""
    return max(cands, key=lambda i: _version_key(i, family))


def _append_test_rows(rows):
    if not rows:
        return
    with _csv_lock:
        new = not os.path.exists(TESTS_CSV)
        with open(TESTS_CSV, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=TESTS_HEADER)
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in TESTS_HEADER})


def _save_last(name, rows):
    with _csv_lock:
        try:
            with open(LAST_TESTS, encoding="utf-8") as fh:
                d = json.load(fh)
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        d[name] = {"ts": _now_str(), "rows": rows}
        tmp = LAST_TESTS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        os.replace(tmp, LAST_TESTS)


def _load_last():
    try:
        with open(LAST_TESTS, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _blank_row(name, model):
    return {"ts": _now_str(), "upstream": name, "model": model,
            "est_method": EST_METHOD}


def _geo_into(row, peer_ip):
    info = _ip_info(peer_ip)
    row["peer_ip"] = peer_ip or ""
    row["ip_country"] = info["country"]
    row["ip_city"] = info["city"]
    row["ip_asn"] = info["asn"]
    row["ip_org"] = info["org"]
    row["fraud_score"] = info["fraud"]
    row["machine_guess"] = info["guess"]


def _chat_test(name, base, auth, mid):
    row = _blank_row(name, mid)
    payload = {"model": mid,
               "messages": [{"role": "user", "content": TEST_PROMPT}],
               "max_tokens": TEST_MAX_TOKENS, "stream": False}
    body = json.dumps(payload).encode("utf-8")
    hdrs = dict(auth)
    hdrs["Content-Type"] = "application/json"
    res = _http("POST", base + "/v1/chat/completions", headers=hdrs,
                body=body, ctimeout=10, rtimeout=90, max_read=2000000)
    row["http_status"] = res["status"] or ""
    row["latency_ms"] = res["total_ms"]
    row["ttfb_ms"] = res["ttfb_ms"]
    _geo_into(row, res.get("peer_ip", ""))
    if res["error"]:
        row["error"] = res["error"]
        return row
    text = res["body"].decode("utf-8", "replace")
    if res["status"] != 200:
        row["error"] = ("HTTP %d: %s" % (res["status"], text[:200])).strip()
        return row
    try:
        data = json.loads(text)
    except Exception:
        row["error"] = "响应不是 JSON"
        return row
    reply = ""
    try:
        ch = (data.get("choices") or [])[0]
        msg = ch.get("message") or {}
        reply = msg.get("content") or ch.get("text") or ""
    except Exception:
        reply = ""
    usage = data.get("usage") or {}
    rp = usage.get("prompt_tokens")
    rc = usage.get("completion_tokens")
    rt = usage.get("total_tokens")
    if rt is None and (rp is not None or rc is not None):
        rt = (rp or 0) + (rc or 0)
    est_p = _est_tokens(TEST_PROMPT) + CHAT_FRAMING_TOKENS
    est_c = _est_tokens(reply)
    row["est_prompt_tokens"] = est_p
    row["est_completion_tokens"] = est_c
    row["est_total_tokens"] = est_p + est_c
    if rp is not None:
        row["reported_prompt_tokens"] = rp
    if rc is not None:
        row["reported_completion_tokens"] = rc
    if rt is not None:
        row["reported_total_tokens"] = rt
        if est_p + est_c > 0:
            ratio = round(float(rt) / float(est_p + est_c), 3)
            row["token_ratio"] = ratio
            row["token_flag"] = ("异常" if ratio > RATIO_HI or ratio < RATIO_LO
                                 else "正常")
    else:
        row["token_flag"] = "无usage"
    return row


def _run_tests(name):
    with _testing_lock:
        if _testing.get(name):
            return
        _testing[name] = True
    try:
        store = _load_store()
        up = store["upstreams"].get(name)
        if not up:
            return
        base = (up.get("base_url") or "").rstrip("/")
        key = up.get("key") or ""
        auth = {"Authorization": "Bearer " + key} if key else {}
        rows = []
        mres = _http("GET", base + "/v1/models", headers=dict(auth),
                     ctimeout=10, rtimeout=25, max_read=2000000)
        ids = []
        if mres["status"] == 200 and not mres["error"]:
            try:
                data = json.loads(mres["body"].decode("utf-8", "replace"))
                for it in data.get("data", []):
                    if isinstance(it, dict) and it.get("id"):
                        ids.append(str(it["id"]))
            except Exception:
                ids = []
        if not ids:
            row = _blank_row(name, "(未提供)")
            row["http_status"] = mres["status"] or ""
            row["latency_ms"] = mres["total_ms"]
            row["ttfb_ms"] = mres["ttfb_ms"]
            why = mres["error"] or ("HTTP %d" % mres["status"])
            row["error"] = "模型列表不可用: " + why
            _geo_into(row, mres.get("peer_ip", ""))
            rows.append(row)
        else:
            for fam in ("opus", "sol"):
                mid = _pick_latest(ids, fam)
                if not mid:
                    row = _blank_row(name, fam + "(未提供)")
                    row["error"] = "上游 /v1/models 无该家族模型"
                    rows.append(row)
                    continue
                rows.append(_chat_test(name, base, auth, mid))
        _append_test_rows(rows)
        _save_last(name, rows)
    finally:
        with _testing_lock:
            _testing[name] = False


def _spawn_tests(name):
    th = threading.Thread(target=_run_tests, args=(name,), daemon=True)
    th.start()


# ------------------------------------------------------ relay log
_log_lock = threading.Lock()


def _relay_conn():
    conn = sqlite3.connect(RELAY_DB, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS relay_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, upstream TEXT,"
        " model TEXT, status INTEGER, latency_ms INTEGER,"
        " prompt_tokens TEXT, completion_tokens TEXT, total_tokens TEXT,"
        " error TEXT)")
    return conn


def _log_relay(upstream, model, status, latency_ms, usage, error):
    try:
        with _log_lock:
            conn = _relay_conn()
            conn.execute(
                "INSERT INTO relay_log (ts, upstream, model, status,"
                " latency_ms, prompt_tokens, completion_tokens,"
                " total_tokens, error) VALUES (?,?,?,?,?,?,?,?,?)",
                (_now_str(), upstream, model, status, latency_ms,
                 usage.get("prompt_tokens", ""),
                 usage.get("completion_tokens", ""),
                 usage.get("total_tokens", ""), error or ""))
            conn.commit()
            conn.close()
    except Exception:
        pass


def _recent_logs(limit=100):
    try:
        conn = _relay_conn()
        cur = conn.execute(
            "SELECT ts, upstream, model, status, latency_ms,"
            " prompt_tokens, completion_tokens, total_tokens, error"
            " FROM relay_log ORDER BY id DESC LIMIT ?", (int(limit),))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def _logs_csv_text():
    try:
        conn = _relay_conn()
        cur = conn.execute(
            "SELECT ts, upstream, model, status, latency_ms,"
            " prompt_tokens, completion_tokens, total_tokens, error"
            " FROM relay_log ORDER BY id DESC LIMIT 10000")
        cols = [c[0] for c in cur.description]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in cur.fetchall():
            w.writerow(r)
        conn.close()
        return buf.getvalue()
    except Exception:
        return ""


# ---------------------------------------------------------- relay
_HOP = {"connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailer", "transfer-encoding",
        "upgrade", "content-length", "host"}
_REQ_SKIP = {"host", "connection", "content-length", "authorization",
             "x-api-key"}


def _extract_usage(acc, tail, is_stream):
    def norm(u):
        if not isinstance(u, dict):
            return {}
        out = {}
        for src, dst in (("prompt_tokens", "prompt_tokens"),
                         ("input_tokens", "prompt_tokens"),
                         ("completion_tokens", "completion_tokens"),
                         ("output_tokens", "completion_tokens"),
                         ("total_tokens", "total_tokens")):
            if u.get(src) is not None and dst not in out:
                out[dst] = str(u.get(src))
        return out

    if not is_stream:
        try:
            d = json.loads(acc.decode("utf-8", "replace"))
            return norm(d.get("usage") or {})
        except Exception:
            return {}
    text = tail.decode("utf-8", "replace")
    found = re.findall(r'"usage"\s*:\s*(\{[^{}]*\})', text)
    for raw in reversed(found):
        try:
            return norm(json.loads(raw))
        except Exception:
            continue
    return {}


def _handle_relay(environ, start_response, name, rest):
    store = _load_store()
    up = store["upstreams"].get(name)
    if up is None:
        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "上游不存在: " + name})
    token = store.get("relay_token") or ""
    if not token:
        return _json(start_response, "503 Service Unavailable",
                     {"ok": False, "error": "转发令牌未设置, 请先在页面设置"})
    given = ""
    authz = environ.get("HTTP_AUTHORIZATION", "")
    if authz.lower().startswith("bearer "):
        given = authz[7:].strip()
    if not given:
        given = environ.get("HTTP_X_API_KEY", "")
    if not given or not _sec.compare_digest(given, token):
        return _json(start_response, "401 Unauthorized",
                     {"ok": False, "error": "转发令牌无效"})
    method = environ.get("REQUEST_METHOD", "GET")
    raw_len = environ.get("CONTENT_LENGTH") or ""
    try:
        length = int(raw_len) if raw_len else 0
    except Exception:
        length = 0
    if length > 32 * 1024 * 1024:
        return _json(start_response, "413 Payload Too Large",
                     {"ok": False, "error": "请求体过大"})
    wsgi_in = environ.get("wsgi.input")
    body = b""
    if wsgi_in is not None:
        if length > 0:
            chunks = []
            left = length
            while left > 0:
                chunk = wsgi_in.read(min(65536, left))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
            body = b"".join(chunks)
        else:
            chunks = []
            got = 0
            while True:
                chunk = wsgi_in.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
                if got > 32 * 1024 * 1024:
                    return _json(start_response, "413 Payload Too Large",
                                 {"ok": False, "error": "请求体过大"})
            body = b"".join(chunks)
    model = ""
    req_stream = False
    if body:
        try:
            reqj = json.loads(body.decode("utf-8", "replace"))
            if isinstance(reqj, dict):
                model = str(reqj.get("model") or "")
                req_stream = bool(reqj.get("stream"))
        except Exception:
            pass
    base = (up.get("base_url") or "").rstrip("/")
    url = base + "/v1/" + rest
    qs = environ.get("QUERY_STRING", "")
    if qs:
        url += "?" + qs
    fwd = {}
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            hname = k[5:].replace("_", "-").title()
            if hname.lower() in _REQ_SKIP:
                continue
            if hname.lower().startswith("x-forwarded-"):
                continue
            if hname.lower().startswith("x-hub-"):
                continue
            fwd[hname] = v
    ctype = environ.get("CONTENT_TYPE", "")
    if ctype:
        fwd["Content-Type"] = ctype
    fwd["Authorization"] = "Bearer " + (up.get("key") or "")
    if environ.get("HTTP_X_API_KEY"):
        fwd["X-Api-Key"] = up.get("key") or ""
    if body:
        fwd["Content-Length"] = str(len(body))
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    cls = (http.client.HTTPSConnection if scheme == "https"
           else http.client.HTTPConnection)
    t0 = time.time()
    conn = cls(host, port, timeout=15)
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(120)
        conn.request(method, path, body=body if body else None,
                     headers=fwd)
        resp = conn.getresponse()
    except Exception as exc:
        _log_relay(name, model, 0, int((time.time() - t0) * 1000), {},
                   "%s: %s" % (type(exc).__name__, exc))
        try:
            conn.close()
        except Exception:
            pass
        return _json(start_response, "502 Bad Gateway",
                     {"ok": False, "error": "上游连接失败: %s" % exc})
    status = resp.status
    reason = resp.reason or ""
    out_headers = []
    resp_ctype = ""
    for k, v in resp.getheaders():
        lk = k.lower()
        if lk in _HOP:
            continue
        if lk == "content-type":
            resp_ctype = v
        out_headers.append((k, v))
    is_stream = req_stream or ("text/event-stream" in resp_ctype)

    def gen():
        acc = bytearray()
        tail = bytearray()
        err = ""
        try:
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                room = 4000000 - len(acc)
                if room > 0:
                    acc += chunk[:room]
                tail += chunk
                if len(tail) > 131072:
                    del tail[:len(tail) - 131072]
                yield chunk
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            usage = _extract_usage(bytes(acc), bytes(tail), is_stream)
            _log_relay(name, model, status,
                       int((time.time() - t0) * 1000), usage, err)

    start_response("%d %s" % (status, reason), out_headers)
    return gen()


# ------------------------------------------------------------ web
def _json(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store")])
    return [body]


def _html_resp(start_response, status, text):
    body = text.encode("utf-8")
    start_response(status, [("Content-Type", "text/html; charset=utf-8"),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store")])
    return [body]


def _download(start_response, filename, body, ctype):
    start_response("200 OK", [("Content-Type", ctype),
                              ("Content-Disposition",
                               'attachment; filename="%s"' % filename),
                              ("Content-Length", str(len(body))),
                              ("Cache-Control", "no-store")])
    return [body]


def _read_body_json(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        length = 0
    raw = b""
    if length > 0:
        raw = environ["wsgi.input"].read(length)
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _api_status():
    store = _load_store()
    last = _load_last()
    ups = []
    for name in sorted(store["upstreams"]):
        u = store["upstreams"][name]
        ent = last.get(name) or {}
        ups.append({
            "name": name,
            "base_url": u.get("base_url", ""),
            "key_set": bool(u.get("key")),
            "created": u.get("created", ""),
            "testing": bool(_testing.get(name)),
            "last_ts": ent.get("ts", ""),
            "last": ent.get("rows", []),
        })
    return "200 OK", {"ok": True,
                      "relay_token_set": bool(store.get("relay_token")),
                      "est_method": EST_METHOD, "upstreams": ups}


def _api_add(environ):
    d = _read_body_json(environ)
    name = (d.get("name") or "").strip()
    base = (d.get("base_url") or "").strip().rstrip("/")
    key = (d.get("key") or "").strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$", name):
        return "400 Bad Request", {"ok": False,
                                   "error": "名称只允许字母数字与 _- (1-40 字符)"}
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "400 Bad Request", {"ok": False, "error": "base_url 不合法"}
    if not key:
        return "400 Bad Request", {"ok": False, "error": "API key 不能为空"}
    if len(key) > 500:
        return "400 Bad Request", {"ok": False, "error": "API key 过长"}
    with _store_lock:
        store = _load_store()
        if name in store["upstreams"]:
            return "400 Bad Request", {"ok": False, "error": "同名上游已存在"}
        store["upstreams"][name] = {"base_url": base, "key": key,
                                    "created": _now_str()}
        _save_store(store)
    _spawn_tests(name)
    return "200 OK", {"ok": True, "testing": True}


def _api_delete(name):
    with _store_lock:
        store = _load_store()
        if name not in store["upstreams"]:
            return "404 Not Found", {"ok": False, "error": "上游不存在"}
        del store["upstreams"][name]
        _save_store(store)
    with _csv_lock:
        last = _load_last()
        if name in last:
            del last[name]
            tmp = LAST_TESTS + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(last, fh, ensure_ascii=False)
            os.replace(tmp, LAST_TESTS)
    return "200 OK", {"ok": True}


def _test_all_worker():
    store = _load_store()
    for name in sorted(store["upstreams"]):
        _run_tests(name)


_PAGE_CSS = (
    "body{font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC',"
    "'Microsoft YaHei',sans-serif;margin:0;background:#f5f6f8;color:#1c2333}"
    ".wrap{max-width:960px;margin:0 auto;padding:16px}"
    "h1{font-size:20px;margin:6px 0 2px}"
    ".mut{color:#6b7280;font-size:12px}"
    ".sec{background:#fff;border:1px solid #e5e7eb;border-radius:10px;"
    "padding:14px;margin:12px 0}"
    ".sec>h2{font-size:15px;margin:0 0 10px}"
    ".row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}"
    ".inp{flex:1;min-width:140px;padding:8px 10px;border:1px solid #d1d5db;"
    "border-radius:8px;font-size:14px}"
    ".btn{background:#2563eb;color:#fff;border:0;border-radius:8px;"
    "padding:8px 14px;font-size:14px;cursor:pointer}"
    ".btn.sm{padding:5px 10px;font-size:12px}"
    ".btn.ghost{background:#eef2ff;color:#2563eb}"
    ".btn.danger{background:#fef2f2;color:#b91c1c}"
    ".card{border:1px solid #e5e7eb;border-radius:10px;padding:12px;"
    "margin:10px 0;background:#fcfcfd}"
    ".card>.hd{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}"
    ".card>.hd>b{font-size:15px}"
    "table.tbl{border-collapse:collapse;width:100%;font-size:12px;"
    "margin-top:8px}"
    ".tbl th,.tbl td{border:1px solid #e5e7eb;padding:4px 6px;"
    "text-align:left;vertical-align:top}"
    ".tbl th{background:#f9fafb;font-weight:600}"
    ".ok{color:#15803d}.bad{color:#b91c1c;font-weight:700}"
    ".warn{color:#b45309}"
    "code{background:#f3f4f6;padding:1px 5px;border-radius:5px;font-size:12px}"
    "a{color:#2563eb;text-decoration:none}"
)

PAGE_HOME = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>API 中转检测</title><style>__CSS__</style></head>
<body><div class="wrap">
<h1>API 中转检测</h1>
<div class="mut">AI API 集中转发 + 上游检测: 每 key 最多测两个模型 (最新 Opus / 最新 Sol), 比对 token 用量、延迟与对端 IP 归属。</div>

<div class="sec"><h2>透明转发</h2>
<div class="row">
<input class="inp" id="tok" type="password" placeholder="设置转发令牌 (至少 8 位, 只写不回显)" autocomplete="off">
<button class="btn" id="tokBtn">设置令牌</button>
<span class="mut" id="tokState"></span>
</div>
<div class="mut" style="margin-top:8px">调用格式: <code id="relayHint"></code> 请求头带 <code>Authorization: Bearer &lt;转发令牌&gt;</code>, 服务端自动注入对应上游的 key, 请求与响应 (含流式) 原样透传。</div>
</div>

<div class="sec"><h2>添加上游</h2>
<div class="row">
<input class="inp" id="fName" placeholder="名称 (如 星尘)" autocomplete="off">
<input class="inp" id="fBase" placeholder="base_url (如 https://api.example.com)" autocomplete="off">
<input class="inp" id="fKey" type="password" placeholder="API key (只写不回显)" autocomplete="off">
<button class="btn" id="addBtn">添加并检测</button>
</div>
<div class="mut" id="addMsg" style="margin-top:6px"></div>
</div>

<div class="sec"><h2>上游列表</h2>
<div class="row">
<button class="btn ghost" id="allBtn">全部重测</button>
<button class="btn ghost" id="refBtn">刷新</button>
<a class="btn ghost" style="display:inline-block" href="__PFX__/api/tests.csv">下载 tests.csv</a>
<a class="btn ghost" style="display:inline-block" href="__PFX__/logs">转发日志</a>
</div>
<div id="ups"></div>
</div>

<div class="sec"><h2>检测口径</h2>
<div class="mut">固定问题一句 (max_tokens 64, 非流式)。token 比值 = 上游自报 total / 本地估算 total (估算: <span id="estm"></span>, 含每条消息约 4 token 的框架量); 比值 &gt;1.5 或 &lt;0.5 标「异常」。对方 IP 为实际连接对端; 归属查 ip-api, 欺诈分查 Scamalytics (一周缓存); 「机器特点」是据 ASN/托管信号的推测, 原始字段并列在表中。全部历史在 tests.csv。</div>
</div>
</div>
<script>
var P = __PREFIX__;
function esc(s){ s = String(s == null ? '' : s); return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function api(path, opts){ return fetch(P + path, opts).then(function(r){ return r.json(); }); }
function post(path, obj){ return api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(obj || {})}); }
document.getElementById('relayHint').textContent = location.origin + P + '/r/<上游名>/v1/chat/completions';
function flagHtml(r){
  if (r.token_flag === '异常') return '<span class="bad">异常</span>';
  if (r.token_flag === '正常') return '<span class="ok">正常</span>';
  if (r.token_flag) return '<span class="warn">' + esc(r.token_flag) + '</span>';
  return '';
}
function rowHtml(r){
  var geo = [r.ip_country, r.ip_city].filter(Boolean).join(' ');
  var tok = '';
  if (r.reported_total_tokens !== '' && r.reported_total_tokens != null) {
    tok = '自报 ' + esc(r.reported_total_tokens) + ' / 估算 ' + esc(r.est_total_tokens) + ' = <b>' + esc(r.token_ratio) + '</b> ' + flagHtml(r);
  } else if (r.token_flag) { tok = flagHtml(r); }
  var st = r.http_status ? ('HTTP ' + esc(r.http_status)) : '';
  if (r.error) st += (st ? ' · ' : '') + '<span class="bad">' + esc(r.error) + '</span>';
  return '<tr><td>' + esc(r.model) + '</td><td>' + st + '</td><td>' + tok + '</td>' +
    '<td>' + esc(r.latency_ms) + (r.latency_ms !== '' && r.latency_ms != null ? ' ms' : '') + '</td>' +
    '<td>' + esc(r.peer_ip) + (geo ? ('<br>' + esc(geo)) : '') + '</td>' +
    '<td>' + esc(r.machine_guess) + (r.fraud_score !== '' && r.fraud_score != null ? ('<br>欺诈分 ' + esc(r.fraud_score)) : '') + '</td></tr>';
}
function render(d){
  document.getElementById('tokState').textContent = d.relay_token_set ? '已设置' : '未设置 (转发不可用)';
  document.getElementById('estm').textContent = d.est_method === 'tiktoken-cl100k' ? 'tiktoken cl100k' : '字符比例 (中文约 0.67/字, 其他约 0.25/字)';
  var box = document.getElementById('ups');
  if (!d.upstreams.length){ box.innerHTML = '<div class="mut" style="margin-top:8px">暂无上游</div>'; return; }
  var h = '';
  d.upstreams.forEach(function(u){
    h += '<div class="card"><div class="hd"><b>' + esc(u.name) + '</b><code>' + esc(u.base_url) + '</code>' +
      '<span class="mut">key ' + (u.key_set ? '已配置' : '未配置') + '</span>' +
      (u.testing ? '<span class="warn">检测中…</span>' : '') +
      '<span style="flex:1"></span>' +
      '<button class="btn sm ghost" data-act="test" data-name="' + esc(u.name) + '">重测</button>' +
      '<button class="btn sm danger" data-act="delete" data-name="' + esc(u.name) + '">删除</button></div>';
    if (u.last && u.last.length){
      h += '<table class="tbl"><tr><th>模型</th><th>状态</th><th>token 比值</th><th>延迟</th><th>对方 IP / 归属</th><th>机器特点</th></tr>';
      u.last.forEach(function(r){ h += rowHtml(r); });
      h += '</table><div class="mut">最近检测: ' + esc(u.last_ts) + ' UTC</div>';
    } else {
      h += '<div class="mut" style="margin-top:6px">暂无检测结果</div>';
    }
    h += '</div>';
  });
  box.innerHTML = h;
}
function refresh(){ api('/api/status').then(render).catch(function(){}); }
document.getElementById('ups').addEventListener('click', function(e){
  var b = e.target.closest('button[data-act]');
  if (!b) return;
  var name = b.getAttribute('data-name');
  if (b.getAttribute('data-act') === 'delete' && !confirm('删除上游 ' + name + ' ? (历史 CSV 行保留)')) return;
  post('/api/upstreams/' + encodeURIComponent(name) + '/' + b.getAttribute('data-act')).then(refresh);
});
document.getElementById('addBtn').addEventListener('click', function(){
  var msg = document.getElementById('addMsg');
  var body = {name: document.getElementById('fName').value, base_url: document.getElementById('fBase').value, key: document.getElementById('fKey').value};
  post('/api/upstreams', body).then(function(d){
    if (d.ok){ msg.textContent = '已添加, 正在自动检测…'; document.getElementById('fKey').value = ''; }
    else { msg.textContent = '失败: ' + (d.error || '未知'); }
    refresh();
  });
});
document.getElementById('tokBtn').addEventListener('click', function(){
  var v = document.getElementById('tok').value;
  post('/api/relay-token', {token: v}).then(function(d){
    document.getElementById('tokState').textContent = d.ok ? '已设置' : ('失败: ' + (d.error || ''));
    if (d.ok) document.getElementById('tok').value = '';
    refresh();
  });
});
document.getElementById('allBtn').addEventListener('click', function(){ post('/api/test-all').then(refresh); });
document.getElementById('refBtn').addEventListener('click', refresh);
refresh();
setInterval(refresh, 6000);
</script>
</body></html>"""

PAGE_LOGS = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>转发日志 · API 中转检测</title><style>__CSS__</style></head>
<body><div class="wrap">
<h1>转发日志</h1>
<div class="row">
<a class="btn ghost" style="display:inline-block" href="__PFX__/">返回首页</a>
<a class="btn ghost" style="display:inline-block" href="__PFX__/api/logs.csv">导出 CSV</a>
<button class="btn ghost" id="refBtn">刷新</button>
</div>
<div class="sec"><div id="logs"><span class="mut">加载中…</span></div></div>
</div>
<script>
var P = __PREFIX__;
function esc(s){ s = String(s == null ? '' : s); return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function load(){
  fetch(P + '/api/logs?limit=100').then(function(r){ return r.json(); }).then(function(d){
    var box = document.getElementById('logs');
    if (!d.logs || !d.logs.length){ box.innerHTML = '<span class="mut">暂无转发记录</span>'; return; }
    var h = '<table class="tbl"><tr><th>时间 (UTC)</th><th>上游</th><th>模型</th><th>状态</th><th>耗时</th><th>usage (入/出/总)</th><th>错误</th></tr>';
    d.logs.forEach(function(r){
      h += '<tr><td>' + esc(r.ts) + '</td><td>' + esc(r.upstream) + '</td><td>' + esc(r.model) + '</td><td>' + esc(r.status) + '</td><td>' + esc(r.latency_ms) + ' ms</td><td>' +
        esc(r.prompt_tokens) + ' / ' + esc(r.completion_tokens) + ' / ' + esc(r.total_tokens) + '</td><td>' + esc(r.error) + '</td></tr>';
    });
    box.innerHTML = h + '</table>';
  });
}
document.getElementById('refBtn').addEventListener('click', load);
load();
</script>
</body></html>"""


def _render(page, prefix):
    return (page.replace("__CSS__", _PAGE_CSS)
            .replace("__PREFIX__", json.dumps(prefix))
            .replace("__PFX__", prefix))


def app(environ, start_response):
    path = environ.get("PATH_INFO") or "/"
    method = environ.get("REQUEST_METHOD", "GET")
    prefix = environ.get("HTTP_X_FORWARDED_PREFIX", "") or ""
    try:
        if path == "/api/session":
            return _json(start_response, "200 OK",
                         {"authenticated": True, "via": "platform"})
        if path == "/" and method == "GET":
            return _html_resp(start_response, "200 OK",
                              _render(PAGE_HOME, prefix))
        if path == "/logs" and method == "GET":
            return _html_resp(start_response, "200 OK",
                              _render(PAGE_LOGS, prefix))
        if path == "/api/status" and method == "GET":
            st, obj = _api_status()
            return _json(start_response, st, obj)
        if path == "/api/upstreams" and method == "POST":
            st, obj = _api_add(environ)
            return _json(start_response, st, obj)
        m = re.match(r"^/api/upstreams/([^/]+)/(delete|test)$", path)
        if m and method == "POST":
            name = urllib.parse.unquote(m.group(1))
            if m.group(2) == "delete":
                st, obj = _api_delete(name)
                return _json(start_response, st, obj)
            store = _load_store()
            if name not in store["upstreams"]:
                return _json(start_response, "404 Not Found",
                             {"ok": False, "error": "上游不存在"})
            _spawn_tests(name)
            return _json(start_response, "200 OK",
                         {"ok": True, "testing": True})
        if path == "/api/test-all" and method == "POST":
            th = threading.Thread(target=_test_all_worker, daemon=True)
            th.start()
            return _json(start_response, "200 OK", {"ok": True})
        if path == "/api/relay-token" and method == "POST":
            d = _read_body_json(environ)
            tok = (d.get("token") or "").strip()
            if len(tok) < 8:
                return _json(start_response, "400 Bad Request",
                             {"ok": False, "error": "令牌至少 8 位"})
            with _store_lock:
                store = _load_store()
                store["relay_token"] = tok
                _save_store(store)
            return _json(start_response, "200 OK", {"ok": True})
        if path == "/api/tests.csv" and method == "GET":
            if not os.path.exists(TESTS_CSV):
                return _json(start_response, "404 Not Found",
                             {"ok": False, "error": "暂无检测数据"})
            with open(TESTS_CSV, "rb") as fh:
                data = fh.read()
            return _download(start_response, "tests.csv", data,
                             "text/csv; charset=utf-8")
        if path == "/api/logs" and method == "GET":
            qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
            try:
                limit = int((qs.get("limit") or ["100"])[0])
            except Exception:
                limit = 100
            limit = max(1, min(limit, 500))
            return _json(start_response, "200 OK",
                         {"ok": True, "logs": _recent_logs(limit)})
        if path == "/api/logs.csv" and method == "GET":
            return _download(start_response, "relay_logs.csv",
                             _logs_csv_text().encode("utf-8"),
                             "text/csv; charset=utf-8")
        if path.startswith("/r/"):
            seg = path[3:].split("/")
            if len(seg) >= 3 and seg[0] and seg[1] == "v1":
                return _handle_relay(environ, start_response,
                                     urllib.parse.unquote(seg[0]),
                                     "/".join(seg[2:]))
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "转发路径格式: /r/<上游名>/v1/<path>"})
        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "not found"})
    except Exception as exc:
        return _json(start_response, "500 Internal Server Error",
                     {"ok": False,
                      "error": "%s: %s" % (type(exc).__name__, exc)})
