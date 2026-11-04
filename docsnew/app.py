"""文档笔记 — personal knowledge notes on the App Hub.

Hosts the user's docsnew system ported from myweb: the manager/editor
page and the reader page, backed by his document logic (backend.py)
over markdown files in .runtime/docsnew_data. The notes data is not
part of the code package; it lives only in .runtime. Access is gated
by the hub's unified login for every route, including the reader.
"""
import json
import os
import threading
from urllib.parse import parse_qs, unquote

import auth
import panel_auth
import backend
from pages import PAGES
from webutil import json_response, read_json, serve_file

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE, "static")
COOKIE_NAME = "docsnew_session"

_account, _account_from_env = auth.load_account()
_account_lock = threading.Lock()


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
            return json_response(start_response,
                                 {"ok": False, "error": "setup_done", "message": "账户已设置，设置入口已关闭。"}, 409)
        data = read_json(environ) or {}
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
    data = read_json(environ) or {}
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


def _page_response(start_response, html):
    body = html.encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                              ("Content-Length", str(len(body)))])
    return [body]


def _first_values(query_string):
    parsed = parse_qs(query_string or "", keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


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
        start_response("302 Found", [("Location", "docs"), ("Content-Length", "0")])
        return [b""]
    if method == "GET" and path[1:] in PAGES:
        return _page_response(start_response, PAGES[path[1:]])
    if method == "GET" and path.startswith("/static/"):
        rel = path[len("/static/"):]
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if full.startswith(STATIC_DIR):
            return serve_file(start_response, full, environ)
        return json_response(start_response, {"error": "not found"}, 404)

    if path.startswith("/docapi/"):
        if not _authed(environ):
            return json_response(start_response,
                                 {"ok": False, "error": "unauthorized", "message": "请先登录。"}, 401)
        subpath = unquote(path[len("/docapi"):])
        args = _first_values(environ.get("QUERY_STRING", ""))
        data = read_json(environ) if method == "POST" else None
        status, payload = backend.handle(method, subpath, args, data)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        start_response("%d %s" % (status, {200: "OK", 400: "Bad Request", 404: "Not Found",
                                           409: "Conflict", 500: "Internal Server Error"}.get(status, "OK")),
                       [("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(body)))])
        return [body]

    return json_response(start_response,
                         {"ok": False, "error": "not_found", "message": "接口不存在。"}, 404)


app = application
