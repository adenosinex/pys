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


DOCS_DATA_WIDGET = """
<div id="docw" style="position:fixed;right:14px;bottom:64px;z-index:9999;font-family:system-ui,-apple-system,'PingFang SC',sans-serif">
  <button id="docw-btn" type="button" style="padding:8px 14px;border-radius:999px;border:1px solid #c7cede;background:#fff;color:#333;font-size:13px;cursor:pointer;box-shadow:0 2px 10px rgba(20,30,60,.15)">数据备份</button>
  <div id="docw-panel" hidden style="position:absolute;right:0;bottom:46px;width:300px;background:#fff;border:1px solid #d8dee9;border-radius:12px;box-shadow:0 8px 28px rgba(20,30,60,.18);padding:14px">
    <div style="font-weight:600;font-size:14px;color:#222">笔记数据备份与恢复</div>
    <p style="font-size:12px;color:#778;margin:6px 0">导出为 zip（整个笔记目录）。导入只新增文件，同名笔记不会被覆盖；回收站不包含在备份内。</p>
    <button id="docw-export" type="button" style="width:100%;padding:8px;border:0;border-radius:8px;background:#2f6fd0;color:#fff;font-size:13px;cursor:pointer">导出 zip 备份</button>
    <input id="docw-file" type="file" accept=".zip,application/zip" style="margin:10px 0 0;font-size:12px;color:#778">
    <button id="docw-import" type="button" style="width:100%;padding:8px;margin-top:8px;border:1px solid #c7cede;border-radius:8px;background:#fff;font-size:13px;cursor:pointer">导入所选 zip</button>
    <div id="docw-msg" style="font-size:12px;color:#2c7a3f;margin-top:8px;min-height:16px"></div>
  </div>
</div>
<script>
(function () {
  var base = location.pathname.replace(/\\/docs\\/?$/, "");
  var $ = function (id) { return document.getElementById(id); };
  $("docw-btn").addEventListener("click", function () {
    var p = $("docw-panel"); p.hidden = !p.hidden;
  });
  $("docw-export").addEventListener("click", async function () {
    var r = await fetch(base + "/docapi/export", {cache: "no-store"});
    var a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = "docsnew-backup.zip";
    document.body.appendChild(a); a.click(); a.remove();
    $("docw-msg").textContent = "备份已导出。";
  });
  $("docw-import").addEventListener("click", async function () {
    var f = $("docw-file").files[0];
    if (!f) { $("docw-msg").textContent = "请先选择 zip 文件"; return; }
    try {
      var r = await fetch(base + "/docapi/import", {method: "POST",
        headers: {"Content-Type": "application/zip"}, body: await f.arrayBuffer()});
      var d = await r.json();
      if (!d.ok) throw new Error(d.error || "导入失败");
      $("docw-msg").textContent = "导入完成：新增 " + d.added + "，跳过 " + d.skipped + "（同名不覆盖）。刷新页面查看。";
    } catch (e) { $("docw-msg").textContent = "导入失败：" + e.message; }
  });
})();
</script>
"""


CRED_WIDGET = """
<div id="credw" style="position:fixed;right:14px;bottom:14px;z-index:9999;font-family:system-ui,-apple-system,'PingFang SC',sans-serif">
  <button id="credw-btn" type="button" style="padding:8px 14px;border-radius:999px;border:1px solid #c7cede;background:#fff;color:#333;font-size:13px;cursor:pointer;box-shadow:0 2px 10px rgba(20,30,60,.15)">AI 凭据</button>
  <div id="credw-panel" hidden style="position:absolute;right:0;bottom:46px;width:300px;background:#fff;border:1px solid #d8dee9;border-radius:12px;box-shadow:0 8px 28px rgba(20,30,60,.18);padding:14px">
    <div style="font-weight:600;font-size:14px;color:#222">OpenRouter 凭据（AI 标签）</div>
    <p style="font-size:12px;color:#778;margin:6px 0">只保存在服务器内部使用，页面永不回显已保存的值。保存空值可清除；环境变量优先。</p>
    <div id="credw-status" style="font-size:12px;color:#456;margin:6px 0">状态读取中…</div>
    <input id="credw-input" type="password" autocomplete="off" placeholder="sk-or-…" style="width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #c7cede;border-radius:8px;font-size:13px">
    <div style="display:flex;gap:8px;margin-top:10px">
      <button id="credw-save" type="button" style="flex:1;padding:8px;border:0;border-radius:8px;background:#2f6fd0;color:#fff;font-size:13px;cursor:pointer">保存</button>
      <button id="credw-clear" type="button" style="padding:8px 12px;border:1px solid #c7cede;border-radius:8px;background:#fff;font-size:13px;cursor:pointer">清除</button>
    </div>
    <div id="credw-msg" style="font-size:12px;color:#2c7a3f;margin-top:8px;min-height:16px"></div>
  </div>
</div>
<script>
(function () {
  var base = location.pathname.replace(/\\/docs\\/?$/, "");
  var $ = function (id) { return document.getElementById(id); };
  function load() {
    fetch(base + "/docapi/credentials", {cache: "no-store"}).then(function (r) { return r.json(); }).then(function (d) {
      var k = (d.keys || {})["OP_API_KEY"] || {};
      $("credw-status").textContent = k.configured ? ("已配置（" + (k.source === "env" ? "环境变量" : "页面保存") + "）") : "未配置";
    }).catch(function () { $("credw-status").textContent = "状态读取失败"; });
  }
  function save(clear) {
    var v = clear ? "" : $("credw-input").value.trim();
    if (!clear && !v) { $("credw-msg").textContent = "请先粘贴 Key"; return; }
    fetch(base + "/docapi/credentials", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({OP_API_KEY: v})})
      .then(function (r) { return r.json(); }).then(function () {
        $("credw-input").value = ""; $("credw-msg").textContent = clear ? "已清除" : "已保存"; load();
      }).catch(function () { $("credw-msg").textContent = "保存失败"; });
  }
  $("credw-btn").addEventListener("click", function () {
    var p = $("credw-panel"); p.hidden = !p.hidden; if (!p.hidden) load();
  });
  $("credw-save").addEventListener("click", function () { save(false); });
  $("credw-clear").addEventListener("click", function () { save(true); });
})();
</script>
"""


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
        html = PAGES[path[1:]]
        if path == "/docs":
            inject = CRED_WIDGET + DOCS_DATA_WIDGET
            html = (html.replace("</body>", inject + "</body>", 1)
                    if "</body>" in html else html + inject)
        return _page_response(start_response, html)
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
        if path == "/docapi/export" and method == "GET":
            return _docs_export(start_response)
        if path == "/docapi/import" and method == "POST":
            return _docs_import(environ, start_response)
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
