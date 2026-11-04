"""微书签 — a tiny personal link hub for the App Hub.

A single page of bookmarks the user can add to from the browser, gated by
the same login as the other hub projects (its own account, with the App
Hub panel session accepted as single sign-on). It also exposes
/auth/verify, an endpoint Caddy's forward_auth can call to gate OTHER
services (e.g. OpenCode on its own port) behind the same unified login:
2xx means "let them through", otherwise it answers with a redirect to
the panel login page, which Caddy passes back to the browser.
"""
import json
import os
import secrets
import threading
import time
from urllib.parse import urlparse

import auth
import panel_auth

BASE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(BASE, ".runtime")
LINKS_FILE = os.path.join(RUNTIME, "links.json")
LOGIN_URL = os.environ.get("LINKS_LOGIN_URL", "https://debi.su7.dpdns.org:10443/")
MAX_LINKS = 500

_account, _account_from_env = auth.load_account()
_account_lock = threading.Lock()
_links_lock = threading.Lock()

# ---------------------------------------------------------------- links store


def _load_links():
    try:
        with open(LINKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict) and e.get("id")]
    except Exception:
        pass
    return []


def _save_links(links):
    os.makedirs(RUNTIME, exist_ok=True)
    tmp = LINKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(links, fh, ensure_ascii=False)
    os.replace(tmp, LINKS_FILE)


def _public_link(e):
    return {"id": e["id"], "name": e.get("name", ""), "url": e.get("url", ""),
            "note": e.get("note", ""), "created_at": e.get("created_at")}


def _validate_link(data):
    name = str(data.get("name") or "").strip()
    url = str(data.get("url") or "").strip()
    note = str(data.get("note") or "").strip()
    if not (1 <= len(name) <= 80):
        return None, "名称需为 1–80 个字符。"
    if len(url) > 2048 or len(note) > 200:
        return None, "链接或备注过长。"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None, "链接需为完整的 http(s) 地址。"
    return {"name": name, "url": url, "note": note}, None

# ---------------------------------------------------------------- http helpers

COOKIE_NAME = "links_session"


def _json(start_response, status, payload, extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"),
               ("Content-Length", str(len(body)))]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def _read_json(environ, limit=65536):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > limit:
        return {}
    try:
        return json.loads(environ["wsgi.input"].read(length).decode("utf-8") or "{}")
    except Exception:
        return {}


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


def handle_verify(environ, start_response):
    """Caddy forward_auth target. 200 + X-Auth-User when the unified login
    (panel session or this project's account session) is present; a 302 to
    the panel login page otherwise, which Caddy returns to the browser."""
    authed, via, username = _auth_state(environ)
    if authed:
        return _json(start_response, "200 OK", {"ok": True, "username": username},
                     [("X-Auth-User", username)])
    start_response("302 Found", [("Location", LOGIN_URL),
                                 ("Content-Length", "0")])
    return [b""]


def handle_links_list(environ, start_response):
    with _links_lock:
        links = _load_links()
    return _json(start_response, "200 OK",
                 {"ok": True, "links": [_public_link(e) for e in links]})


def handle_links_create(environ, start_response):
    entry, err = _validate_link(_read_json(environ))
    if err:
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "bad_link", "message": err})
    with _links_lock:
        links = _load_links()
        if len(links) >= MAX_LINKS:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "error": "too_many", "message": "链接数量已达上限。"})
        entry = {"id": secrets.token_hex(4), "created_at": time.time(), **entry}
        links.append(entry)
        _save_links(links)
    return _json(start_response, "200 OK", {"ok": True, "link": _public_link(entry)})


def handle_links_update(environ, start_response, lid):
    entry, err = _validate_link(_read_json(environ))
    if err:
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "bad_link", "message": err})
    with _links_lock:
        links = _load_links()
        for e in links:
            if e["id"] == lid:
                e.update(entry)
                _save_links(links)
                return _json(start_response, "200 OK", {"ok": True, "link": _public_link(e)})
    return _json(start_response, "404 Not Found",
                 {"ok": False, "error": "not_found", "message": "链接不存在。"})


def handle_links_delete(environ, start_response, lid):
    with _links_lock:
        links = _load_links()
        kept = [e for e in links if e["id"] != lid]
        if len(kept) == len(links):
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "not_found", "message": "链接不存在。"})
        _save_links(kept)
    return _json(start_response, "200 OK", {"ok": True})


os.makedirs(RUNTIME, exist_ok=True)

# ---------------------------------------------------------------- page

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微书签</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0 auto; max-width: 760px; padding: 20px 16px 48px; line-height: 1.55; }
  h1 { font-size: 22px; margin: 4px 0 2px; }
  .sub { opacity: .65; font-size: 13px; margin-bottom: 18px; }
  .card { border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
          border-radius: 12px; padding: 14px 16px; margin: 12px 0; }
  label { display: block; font-size: 13px; margin: 10px 0 4px; }
  input[type=text], input[type=url], input[type=password] {
    width: 100%; box-sizing: border-box; padding: 9px 10px; border-radius: 8px;
    border: 1px solid color-mix(in srgb, currentColor 30%, transparent);
    background: transparent; color: inherit; font-size: 15px; }
  button { padding: 9px 16px; border-radius: 8px; border: 0; cursor: pointer;
           background: #2563eb; color: #fff; font-size: 14px; }
  button.ghost { background: transparent; color: inherit;
                 border: 1px solid color-mix(in srgb, currentColor 30%, transparent); }
  button.danger { background: #dc2626; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .link { display: flex; gap: 10px; align-items: baseline; padding: 10px 2px;
          border-bottom: 1px solid color-mix(in srgb, currentColor 10%, transparent); }
  .link:last-child { border-bottom: 0; }
  .link a { font-size: 16px; text-decoration: none; word-break: break-all; }
  .link .note { opacity: .6; font-size: 13px; }
  .link .ops { margin-left: auto; white-space: nowrap; }
  .link .ops button { padding: 4px 10px; font-size: 12px; }
  .msg { min-height: 20px; font-size: 14px; margin: 8px 0; }
  .err { color: #dc2626; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<h1>微书签</h1>
<div class="sub">随手存、随时开的个人链接页。和面板共用登录；OpenCode 等受控入口也走同一道验证。</div>

<section id="setup-panel" class="card" hidden>
  <h2 style="font-size:16px;margin:0 0 6px">首次设置 · 创建账户</h2>
  <form id="setup-form">
    <label>账户名</label><input id="setup-username" type="text" autocomplete="username" required maxlength="128">
    <label>设置密码（至少 10 位）</label><input id="setup-password" type="password" autocomplete="new-password" required maxlength="1024">
    <label>再次输入密码</label><input id="setup-password2" type="password" autocomplete="new-password" required maxlength="1024">
    <div class="row" style="margin-top:12px"><button type="submit">创建账户并登录</button></div>
    <p class="msg" id="setup-msg"></p>
  </form>
</section>

<section id="login-panel" class="card" hidden>
  <h2 style="font-size:16px;margin:0 0 6px">登录后使用</h2>
  <form id="login-form">
    <label>账户名</label><input id="username" type="text" autocomplete="username" required maxlength="128">
    <label>密码</label><input id="password" type="password" autocomplete="current-password" required maxlength="1024">
    <div class="row" style="margin-top:12px"><button type="submit">登录</button></div>
    <p class="msg" id="login-msg"></p>
  </form>
</section>

<main id="workspace" hidden>
  <div class="row" style="justify-content:space-between">
    <span id="who" class="sub" style="margin:0"></span>
    <button id="logout" class="ghost" type="button">退出</button>
  </div>

  <section class="card">
    <h2 style="font-size:16px;margin:0 0 6px" id="form-title">添加链接</h2>
    <form id="add-form">
      <label>名称</label><input id="f-name" type="text" required maxlength="80" placeholder="例如 OpenCode">
      <label>链接</label><input id="f-url" type="url" required maxlength="2048" placeholder="https://…">
      <label>备注（可选）</label><input id="f-note" type="text" maxlength="200">
      <div class="row" style="margin-top:12px">
        <button type="submit" id="f-submit">添加</button>
        <button type="button" id="f-cancel" class="ghost" hidden>取消编辑</button>
      </div>
      <p class="msg" id="form-msg"></p>
    </form>
  </section>

  <section class="card">
    <h2 style="font-size:16px;margin:0 0 6px">我的链接</h2>
    <div id="links"></div>
    <p class="msg" id="list-msg"></p>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
let editingId = null;

async function api(path, options) {
  const res = await fetch(path, {credentials: "same-origin", cache: "no-store", ...options});
  let data = {};
  try { data = await res.json(); } catch (e) {}
  return {status: res.status, data};
}
const post = (path, body) => api(path, {method: "POST",
  headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

async function refresh() {
  const {data} = await api("api/session");
  if (!data.authenticated && data.setup_required) {
    $("setup-panel").hidden = false; $("login-panel").hidden = true; $("workspace").hidden = true;
    return;
  }
  if (!data.authenticated) {
    $("setup-panel").hidden = true; $("login-panel").hidden = false; $("workspace").hidden = true;
    return;
  }
  $("setup-panel").hidden = true; $("login-panel").hidden = true; $("workspace").hidden = false;
  $("who").textContent = "已登录：" + (data.username || "") + (data.via === "panel" ? "（面板免登）" : "");
  await loadLinks();
}

async function loadLinks() {
  const {status, data} = await api("api/links");
  const box = $("links");
  if (status !== 200) { $("list-msg").textContent = data.message || "加载失败"; return; }
  if (!data.links.length) { box.innerHTML = ""; $("list-msg").textContent = "暂无链接，用上面的表单添加第一个。"; return; }
  $("list-msg").textContent = "";
  box.innerHTML = data.links.map(l =>
    `<div class="link"><div><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.name)}</a>` +
    (l.note ? `<div class="note">${esc(l.note)}</div>` : "") +
    `</div><span class="ops"><button class="ghost" data-edit="${l.id}">编辑</button> ` +
    `<button class="danger" data-del="${l.id}">删除</button></span></div>`).join("");
  box.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => startEdit(b.dataset.edit, data.links));
  box.querySelectorAll("[data-del]").forEach(b => b.onclick = () => removeLink(b.dataset.del));
}

function startEdit(id, links) {
  const l = links.find(x => x.id === id);
  if (!l) return;
  editingId = id;
  $("f-name").value = l.name; $("f-url").value = l.url; $("f-note").value = l.note || "";
  $("form-title").textContent = "编辑链接"; $("f-submit").textContent = "保存";
  $("f-cancel").hidden = false;
  window.scrollTo({top: 0, behavior: "smooth"});
}
function resetForm() {
  editingId = null;
  $("add-form").reset();
  $("form-title").textContent = "添加链接"; $("f-submit").textContent = "添加";
  $("f-cancel").hidden = true; $("form-msg").textContent = "";
}
$("f-cancel").onclick = resetForm;

$("add-form").onsubmit = async (e) => {
  e.preventDefault();
  const body = {name: $("f-name").value, url: $("f-url").value, note: $("f-note").value};
  const path = editingId ? "api/links/" + editingId : "api/links";
  const {status, data} = await post(path, body);
  if (status !== 200) { $("form-msg").innerHTML = `<span class="err">${esc(data.message || "保存失败")}</span>`; return; }
  resetForm();
  await loadLinks();
};

async function removeLink(id) {
  if (!confirm("删除这条链接？")) return;
  const {status, data} = await api("api/links/" + id, {method: "DELETE"});
  if (status !== 200) { alert(data.message || "删除失败"); return; }
  if (editingId === id) resetForm();
  await loadLinks();
}

$("setup-form").onsubmit = async (e) => {
  e.preventDefault();
  if ($("setup-password").value !== $("setup-password2").value) {
    $("setup-msg").innerHTML = '<span class="err">两次输入的密码不一致。</span>'; return;
  }
  const {status, data} = await post("api/setup",
    {username: $("setup-username").value, password: $("setup-password").value});
  if (status !== 200) { $("setup-msg").innerHTML = `<span class="err">${esc(data.message || "设置失败")}</span>`; return; }
  await refresh();
};
$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const {status, data} = await post("api/login",
    {username: $("username").value, password: $("password").value});
  if (status !== 200) { $("login-msg").innerHTML = `<span class="err">${esc(data.message || "登录失败")}</span>`; return; }
  await refresh();
};
$("logout").onclick = async () => { await post("api/logout"); await refresh(); };

refresh();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------- router


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"
    try:
        if path == "/health":
            return _json(start_response, "200 OK", {"ok": True, "service": "links"})
        if path == "/auth/verify" and method == "GET":
            return handle_verify(environ, start_response)
        if path == "/api/session" and method == "GET":
            return handle_session(environ, start_response)
        if path == "/api/setup" and method == "POST":
            return handle_setup(environ, start_response)
        if path == "/api/login" and method == "POST":
            return handle_login(environ, start_response)
        if path == "/api/logout" and method == "POST":
            return handle_logout(environ, start_response)
        if path.startswith("/api/links"):
            if not _authed(environ):
                return _json(start_response, "401 Unauthorized",
                             {"ok": False, "error": "unauthorized", "message": "请先登录。"})
            if path == "/api/links" and method == "GET":
                return handle_links_list(environ, start_response)
            if path == "/api/links" and method == "POST":
                return handle_links_create(environ, start_response)
            lid = path[len("/api/links/"):]
            if lid and "/" not in lid:
                if method == "POST":
                    return handle_links_update(environ, start_response, lid)
                if method == "DELETE":
                    return handle_links_delete(environ, start_response, lid)
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "not_found", "message": "接口不存在。"})
        if path == "/" and method == "GET":
            body = PAGE.encode("utf-8")
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                      ("Content-Length", str(len(body))),
                                      ("Cache-Control", "no-store")])
            return [body]
        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "not_found", "message": "页面不存在。"})
    except Exception:  # noqa: BLE001
        return _json(start_response, "500 Internal Server Error",
                     {"ok": False, "error": "internal", "message": "内部错误。"})


app = application
