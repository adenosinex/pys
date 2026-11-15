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
import io
import json
import os
import threading
import zipfile
from datetime import datetime
from urllib.parse import parse_qs, unquote

import auth
import backend
from ai_page import AI_CONSOLE_HTML
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


def _docs_export(start_response):
    buf = io.BytesIO()
    root = backend.DOCS_DIR
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                full = os.path.join(dirpath, name)
                zf.write(full, os.path.relpath(full, root))
    body = buf.getvalue()
    fname = "docsnew-backup-%s.zip" % datetime.now().strftime("%Y%m%d")
    start_response("200 OK", [("Content-Type", "application/zip"),
                              ("Content-Disposition", 'attachment; filename="%s"' % fname),
                              ("Content-Length", str(len(body)))])
    return [body]


def _docs_import(environ, start_response):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > 67108864:
        return json_response(start_response, {"ok": False, "error": "文件为空或过大（上限 64 MB）。"}, 400)
    raw = environ["wsgi.input"].read(length)
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return json_response(start_response, {"ok": False, "error": "不是有效的 zip 备份。"}, 400)
    root = os.path.abspath(backend.DOCS_DIR)
    added = skipped = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if (info.is_dir() or name.startswith("/") or ".." in name.split("/")
                or name.startswith("__MACOSX") or info.file_size > 16777216):
            skipped += 1
            continue
        target = os.path.abspath(os.path.join(root, name))
        if not target.startswith(root + os.sep) or os.path.exists(target):
            skipped += 1
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(zf.read(info))
        added += 1
    if added:
        backend.build_memory_index()
    return json_response(start_response, {"ok": True, "added": added, "skipped": skipped})


def _import_script(start_response):
    path = os.path.join(BASE, "1docs_import_debi.py")
    try:
        with open(path, "rb") as fh:
            body = fh.read()
    except OSError:
        return json_response(start_response, {"ok": False, "error": "导入脚本文件不存在。"}, 404)
    start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8"),
                              ("Content-Disposition", 'attachment; filename="1docs_import_debi.py"'),
                              ("Content-Length", str(len(body)))])
    return [body]


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
    if method == "GET" and path == "/ai":
        return _page_response(start_response, AI_CONSOLE_HTML)
    if method == "GET" and path[1:].rstrip("/") in PAGES:
        html = PAGES[path[1:].rstrip("/")]
        return _page_response(start_response, html)
    if method == "GET" and path.startswith("/static/"):
        rel = path[len("/static/"):]
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if full.startswith(STATIC_DIR):
            return serve_file(start_response, full, environ, immutable=True)
        return json_response(start_response, {"error": "not found"}, 404)

    if path.startswith("/docapi/"):
        if not _authed(environ):
            return json_response(start_response,
                                 {"ok": False, "error": "unauthorized", "message": "请先登录。"}, 401)
        if path == "/docapi/export" and method == "GET":
            return _docs_export(start_response)
        if path == "/docapi/import" and method == "POST":
            return _docs_import(environ, start_response)
        if path == "/docapi/ai/import-script" and method == "GET":
            return _import_script(start_response)
        # WSGI servers percent-decode PATH_INFO into latin-1 (PEP 3333), so a
        # non-ASCII doc id arrives as mojibake and unquote() alone cannot
        # recover it (proven 2026-11-15: one Chinese-prefixed id 404'd).
        # Recover the UTF-8 bytes first; pure-ASCII paths round-trip unchanged.
        _raw_sub = path[len("/docapi"):]
        try:
            _raw_sub = _raw_sub.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        subpath = unquote(_raw_sub)
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
