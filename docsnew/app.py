"""文档笔记 — personal knowledge notes on the App Hub.

Hosts the user's docsnew system ported from myweb: the manager/editor
page and the reader page, backed by his document logic (backend.py)
over markdown files in .runtime/docsnew_data. The notes data is not
part of the code package; it lives only in .runtime. This app
contains no identity verification: the App Hub platform gate in
front of /web/docsnew/ performs it centrally (this project's level
is console-only, set in the console's access-control page), and
direct access on the local port is trusted.
"""
import json
import os
import threading
from urllib.parse import parse_qs, unquote

import auth
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
    """Identity verification lives at the platform layer (the App Hub
    gate in front of /web/<slug>/, this project's level: console).
    This app performs no verification itself: any request that
    reaches it has either passed the platform gate or arrived on the
    local port, which is trusted."""
    account = _current_account()
    return True, "platform", (account.username if account else "local")

def _authed(environ):
    return _auth_state(environ)[0]

def handle_session(environ, start_response):
    account = _current_account()
    authed, via, username = _auth_state(environ)
    return json_response(start_response, {
        "ok": True,
        "setup_required": False,
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
