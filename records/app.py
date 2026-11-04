"""记录台 — the records hub for the App Hub.

One project hosting the user's record-keeping features ported from
myweb: daily stats, fuel log, history snapshots (+ tracker boards),
audio memos, snippets, and the /api2 universal store several pages
are built on (product / supplies / exercise). Each feature keeps its
original API shapes and its own SQLite file under .runtime/, so the
databases from the original project can be dropped in (via seed/ or
directly into .runtime/) and pick up where they left off. Access is
gated by the hub's unified login.
"""
import os
import re
import shutil
import threading

import auth
import panel_auth
import mod_audiom
import mod_history
import mod_snippets
import mod_stats
import mod_store
from pages import PAGES
from webutil import json_response, serve_file

BASE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(BASE, ".runtime")
SEED = os.path.join(BASE, "seed")
STATIC_DIR = os.path.join(BASE, "static")
COOKIE_NAME = "records_session"

_account, _account_from_env = auth.load_account()
_account_lock = threading.Lock()


def _init_storage():
    os.makedirs(RUNTIME, exist_ok=True)
    for name in ("universal_stats.db", "universal_datasmall.db", "snippets.db",
                 "vocal_data.db", "universal_data.db"):
        target = os.path.join(RUNTIME, name)
        source = os.path.join(SEED, name)
        if not os.path.exists(target) and os.path.exists(source):
            shutil.copyfile(source, target)
    for name in ("history", "assert-cloud", "audio_uploads", "raw_responses"):
        target = os.path.join(RUNTIME, name)
        source = os.path.join(SEED, name)
        if not os.path.exists(target) and os.path.isdir(source):
            shutil.copytree(source, target)
    mod_stats.init_db()
    mod_snippets.init_db()
    mod_history.init_db()
    mod_audiom.init_db()
    mod_store.init_db()


_init_storage()

# ---------------------------------------------------------------- auth


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


def _json_body(environ):
    from webutil import read_json
    return read_json(environ)


def handle_setup(environ, start_response):
    global _account
    with _account_lock:
        if _account is not None:
            return json_response(start_response,
                                 {"ok": False, "error": "setup_done", "message": "账户已设置，设置入口已关闭。"}, 409)
        data = _json_body(environ) or {}
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not (1 <= len(username) <= 128):
            return json_response(start_response,
                                 {"ok": False, "error": "bad_username", "message": "账户名需为 1–128 个字符。"}, 400)
        if len(password) < 10:
            return json_response(start_response,
                                 {"ok": False, "error": "short_password", "message": "密码至少 10 位。"}, 400)
        _account = auth.store_account(username, password)
        account = _account
    return json_response(start_response, {"ok": True, "username": account.username},
                         extra_headers=[_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_login(environ, start_response):
    account = _current_account()
    if account is None:
        return json_response(start_response,
                             {"ok": False, "error": "not_configured", "message": "尚未设置账户。"}, 503)
    data = _json_body(environ) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if username != account.username or not account.check_password(password):
        return json_response(start_response,
                             {"ok": False, "error": "bad_credentials", "message": "账户名或密码不正确。"}, 401)
    return json_response(start_response, {"ok": True, "username": account.username},
                         extra_headers=[_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_session(environ, start_response):
    account = _current_account()
    authed, via, username = _auth_state(environ)
    return json_response(start_response, {
        "ok": True,
        "setup_required": account is None,
        "authenticated": authed,
        "via": via,
        "username": username if authed else None,
    })

# ---------------------------------------------------------------- pages


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>记录台</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font-family: system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; background:#0f1116; color:#e6e8ee; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 28px 16px 40px; }
  h1 { font-size: 20px; margin: 0 0 6px; }
  p.sub { color:#8b93a7; font-size: 13px; margin: 0 0 22px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }
  a.card { display:block; background:#161a22; border:1px solid #232a37; border-radius:12px; padding:16px 14px; color:inherit; text-decoration:none; transition:.15s; }
  a.card:hover { background:#1e2430; border-color:#3d6ea8; }
  a.card .t { font-size:15px; font-weight:600; margin-bottom:4px; }
  a.card .d { font-size:12px; color:#8b93a7; line-height:1.5; }
</style>
</head>
<body>
<div class="wrap">
  <h1>记录台</h1>
  <p class="sub">由 myweb 迁来的记录类功能，数据各自独立存储，互不干扰。</p>
  <div class="grid">
    <a class="card" href="daily"><div class="t">日常记录</div><div class="d">每日计数打卡，可导入导出 CSV</div></a>
    <a class="card" href="fuel"><div class="t">加油记录</div><div class="d">油量与里程，自动算油耗</div></a>
    <a class="card" href="history"><div class="t">历史快照</div><div class="d">生活切片照片，可选 AI 背景分析</div></a>
    <a class="card" href="audiom"><div class="t">录音备忘</div><div class="d">录音转文字与摘要（需转写接口）</div></a>
    <a class="card" href="snippets"><div class="t">片段库</div><div class="d">代码与文本片段</div></a>
    <a class="card" href="product"><div class="t">产品追踪</div><div class="d">通用追踪看板</div></a>
    <a class="card" href="supplies"><div class="t">日常物资</div><div class="d">物资清单与状态</div></a>
    <a class="card" href="assert"><div class="t">资产看板</div><div class="d">资产条目与凭证图</div></a>
    <a class="card" href="cloud"><div class="t">云端日常</div><div class="d">日常数据同步页</div></a>
    <a class="card" href="exercise"><div class="t">运动记录</div><div class="d">健身配置与记录</div></a>
  </div>
</div>
</body>
</html>
"""


def _page_response(start_response, html):
    body = html.encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                              ("Content-Length", str(len(body)))])
    return [body]

# ---------------------------------------------------------------- router


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"

    if path == "/health" and method == "GET":
        return json_response(start_response, {"ok": True})
    if path == "/api/session" and method == "GET":
        return handle_session(environ, start_response)
    if path == "/api/setup" and method == "POST":
        return handle_setup(environ, start_response)
    if path == "/api/login" and method == "POST":
        return handle_login(environ, start_response)
    if path == "/api/logout" and method == "POST":
        return json_response(start_response, {"ok": True},
                             extra_headers=[_session_cookie(environ, "", 0)])

    if path == "/" and method == "GET":
        return _page_response(start_response, INDEX_HTML)
    if method == "GET" and path[1:] in PAGES:
        return _page_response(start_response, PAGES[path[1:]])
    if method == "GET" and path.startswith("/static/"):
        rel = path[len("/static/"):]
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if full.startswith(STATIC_DIR):
            return serve_file(start_response, full, environ)
        return json_response(start_response, {"error": "not found"}, 404)

    if method == "OPTIONS" and path.startswith("/audiom/"):
        start_response("204 No Content", [("Content-Length", "0")])
        return [b""]

    if not _authed(environ):
        return json_response(start_response,
                             {"ok": False, "error": "unauthorized", "message": "请先登录。"}, 401)

    # /api2 universal store (kv before collections)
    m = re.fullmatch(r"/api2/kv/([^/]+)", path)
    if m and method in ("GET", "POST"):
        from urllib.parse import unquote
        return mod_store.kv_main(environ, start_response, unquote(m.group(1)))
    m = re.fullmatch(r"/api2/([^/]+)", path)
    if m and method in ("GET", "POST"):
        from urllib.parse import unquote
        return mod_store.collection_main(environ, start_response, unquote(m.group(1)))

    # daily + fuel
    if path == "/api/daily_stats" and method in ("GET", "POST"):
        return mod_stats.daily_main(environ, start_response)
    if path == "/api/daily_stats/export" and method == "GET":
        return mod_stats.daily_export(environ, start_response)
    if path == "/api/daily_stats/import" and method == "POST":
        return mod_stats.daily_import(environ, start_response)
    m = re.fullmatch(r"/api/daily_stats/(\d+)", path)
    if m and method == "DELETE":
        return mod_stats.daily_delete(environ, start_response, int(m.group(1)))
    if path == "/api/fuel_stats" and method in ("GET", "POST"):
        return mod_stats.fuel_main(environ, start_response)
    if path == "/api/fuel_stats/export" and method == "GET":
        return mod_stats.fuel_export(environ, start_response)
    if path == "/api/fuel_stats/import" and method == "POST":
        return mod_stats.fuel_import(environ, start_response)
    m = re.fullmatch(r"/api/fuel_stats/(\d+)", path)
    if m and method == "DELETE":
        return mod_stats.fuel_delete(environ, start_response, int(m.group(1)))

    # snippets
    if path == "/api/snippets/list" and method == "GET":
        return mod_snippets.list_snippets(environ, start_response)
    if path == "/api/snippets/save" and method == "POST":
        return mod_snippets.save_snippet(environ, start_response)
    m = re.fullmatch(r"/api/snippets/raw/(\d+)", path)
    if m and method == "GET":
        return mod_snippets.get_snippet(environ, start_response, int(m.group(1)))
    m = re.fullmatch(r"/api/snippets/delete/(\d+)", path)
    if m and method == "DELETE":
        return mod_snippets.delete_snippet(environ, start_response, int(m.group(1)))

    # history snapshots + tracker
    if path == "/api/snapshots" and method == "GET":
        return mod_history.list_snapshots(environ, start_response)
    if path == "/api/snapshots/upload" and method == "POST":
        return mod_history.upload_snapshot(environ, start_response)
    if path == "/api/snapshots/analyze" and method == "POST":
        return mod_history.analyze_snapshot(environ, start_response)
    m = re.fullmatch(r"/api/snapshots/([^/]+)", path)
    if m and method == "DELETE":
        from urllib.parse import unquote
        return mod_history.delete_snapshot(environ, start_response, unquote(m.group(1)))
    m = re.fullmatch(r"/api/img/snapshots/([^/]+)", path)
    if m and method == "GET":
        from urllib.parse import unquote
        return mod_history.snapshot_image(environ, start_response, unquote(m.group(1)))
    if path == "/tracker_data/sync/daily" and method in ("GET", "POST"):
        return mod_history.tracker_sync(environ, start_response, "daily_data")
    if path == "/tracker_data/sync/assets" and method in ("GET", "POST"):
        return mod_history.tracker_sync(environ, start_response, "assets_data")
    m = re.fullmatch(r"/tracker/image/([^/]+)", path)
    if m and method == "GET":
        from urllib.parse import unquote
        return mod_history.tracker_image(environ, start_response, unquote(m.group(1)))

    # audio memos
    if path == "/audiom/api/vocal" and method == "GET":
        return mod_audiom.get_records(environ, start_response)
    if path == "/audiom/api/vocal/upload" and method == "POST":
        return mod_audiom.upload_audio(environ, start_response)
    if path == "/audiom/api/vocal/start" and method == "POST":
        return mod_audiom.start_processing(environ, start_response)
    if path == "/audiom/api/vocal/retry" and method == "POST":
        return mod_audiom.retry_processing(environ, start_response)
    m = re.fullmatch(r"/audiom/api/vocal/audio/([^/]+)", path)
    if m and method == "GET":
        from urllib.parse import unquote
        return mod_audiom.get_audio(environ, start_response, unquote(m.group(1)))
    m = re.fullmatch(r"/audiom/api/vocal/([^/]+)", path)
    if m and method == "DELETE":
        from urllib.parse import unquote
        return mod_audiom.delete_record(environ, start_response, unquote(m.group(1)))

    return json_response(start_response,
                         {"ok": False, "error": "not_found", "message": "接口不存在。"}, 404)


app = application
