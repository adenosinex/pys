"""共享剪贴板 — multi-device shared clipboard for the App Hub.

Ported from the user's myweb in-memory chat blueprint: the page, its
polling protocol and the click-to-copy / send-clipboard behaviour are
preserved (see page.py); the Flask blueprint became this stdlib WSGI
app so it runs like the other hub projects. Messages live in process
memory only (single worker), rolling at 500 entries — a clipboard,
not an archive. Access is gated by the hub's unified login: the
project's own account, with the App Hub panel session accepted as
single sign-on (same pattern as links / yt-audio).
"""
import json
import os
import threading
import time
from collections import deque
from urllib.parse import parse_qs

import auth
import panel_auth
from page import PAGE_HTML

MAX_MESSAGES = 500
MAX_NAME_LEN = 20
MAX_TEXT_LEN = 1000
COOKIE_NAME = "clip_session"

_account, _account_from_env = auth.load_account()
_account_lock = threading.Lock()

# ---------------------------------------------------------------- message store


_lock = threading.Lock()
_messages = deque(maxlen=MAX_MESSAGES)
_next_id = 1


def _add_message(name, text):
    global _next_id
    with _lock:
        msg = {"id": _next_id, "name": name, "text": text, "ts": time.time()}
        _next_id += 1
        _messages.append(msg)
    return msg


def _snapshot(since_id):
    with _lock:
        msgs = [m for m in _messages if m["id"] > since_id]
        last_id = _next_id - 1
    return msgs, last_id

# ---------------------------------------------------------------- http helpers


def _json(start_response, status, payload, extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"),
               ("Content-Length", str(len(body)))]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def _read_body(environ, limit=65536):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > limit:
        return b""
    try:
        return environ["wsgi.input"].read(length)
    except Exception:
        return b""


def _read_json(environ):
    raw = _read_body(environ)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


def _read_form(environ):
    raw = _read_body(environ)
    if not raw:
        return {}
    parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


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
    """(authenticated, via, username): own account session wins; a valid
    App Hub panel session is accepted as fallback (single sign-on)."""
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

# ---------------------------------------------------------------- handlers


def handle_setup(environ, start_response):
    global _account
    with _account_lock:
        if _account is not None:
            return _json(start_response, "409 Conflict",
                         {"ok": False, "error": "setup_done", "message": "账户已设置，设置入口已关闭。"})
        data = _read_json(environ)
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not (1 <= len(username) <= 128):
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "error": "bad_username", "message": "账户名需为 1–128 个字符。"})
        if len(password) < 10:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "error": "short_password", "message": "密码至少 10 位。"})
        _account = auth.store_account(username, password)
        account = _account
    return _json(start_response, "200 OK", {"ok": True, "username": account.username},
                 [_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_login(environ, start_response):
    account = _current_account()
    if account is None:
        return _json(start_response, "503 Service Unavailable",
                     {"ok": False, "error": "not_configured", "message": "尚未设置账户。"})
    data = _read_json(environ)
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if username != account.username or not account.check_password(password):
        return _json(start_response, "401 Unauthorized",
                     {"ok": False, "error": "bad_credentials", "message": "账户名或密码不正确。"})
    return _json(start_response, "200 OK", {"ok": True, "username": account.username},
                 [_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_logout(environ, start_response):
    return _json(start_response, "200 OK", {"ok": True},
                 [_session_cookie(environ, "", 0)])


def handle_session(environ, start_response):
    account = _current_account()
    authed, via, username = _auth_state(environ)
    return _json(start_response, "200 OK", {
        "ok": True,
        "setup_required": account is None,
        "authenticated": authed,
        "via": via,
        "username": username if authed else None,
    })


def handle_messages_get(environ, start_response):
    if not _authed(environ):
        return _json(start_response, "401 Unauthorized",
                     {"ok": False, "error": "unauthorized", "message": "请先登录。"})
    params = parse_qs(environ.get("QUERY_STRING", ""))
    try:
        since = int((params.get("since") or ["0"])[0])
    except (TypeError, ValueError):
        since = 0
    if since < 0:
        since = 0
    msgs, last_id = _snapshot(since)
    return _json(start_response, "200 OK", {"messages": msgs, "last_id": last_id})


def handle_messages_post(environ, start_response):
    if not _authed(environ):
        return _json(start_response, "401 Unauthorized",
                     {"ok": False, "error": "unauthorized", "message": "请先登录。"})
    ctype = environ.get("CONTENT_TYPE", "")
    if "application/x-www-form-urlencoded" in ctype:
        data = _read_form(environ)
    else:
        data = _read_json(environ)
    name = (str(data.get("name") or "").strip() or "匿名")[:MAX_NAME_LEN]
    text = (str(data.get("text") or "").strip())[:MAX_TEXT_LEN]
    if not text:
        return _json(start_response, "400 Bad Request", {"error": "消息内容不能为空"})
    msg = _add_message(name, text)
    return _json(start_response, "201 Created", msg)


def handle_page(environ, start_response):
    body = PAGE_HTML.encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                              ("Content-Length", str(len(body)))])
    return [body]

# ---------------------------------------------------------------- router


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"
    if path == "/" and method == "GET":
        return handle_page(environ, start_response)
    if path == "/health" and method == "GET":
        return _json(start_response, "200 OK", {"ok": True})
    if path == "/api/session" and method == "GET":
        return handle_session(environ, start_response)
    if path == "/api/setup" and method == "POST":
        return handle_setup(environ, start_response)
    if path == "/api/login" and method == "POST":
        return handle_login(environ, start_response)
    if path == "/api/logout" and method == "POST":
        return handle_logout(environ, start_response)
    if path == "/api/messages" and method == "GET":
        return handle_messages_get(environ, start_response)
    if path == "/api/messages" and method == "POST":
        return handle_messages_post(environ, start_response)
    return _json(start_response, "404 Not Found",
                 {"ok": False, "error": "not_found", "message": "接口不存在。"})


app = application
