"""微书签 — a tiny personal link hub for the App Hub.

A single page of bookmarks the user can add to from the browser.
This app contains no identity verification: the App Hub platform
gate in front of /web/links/ performs it centrally, and direct
access on the local port is trusted.
"""
import json
import os
import secrets
import threading
import time
from urllib.parse import urlparse

import auth

BASE = os.path.dirname(os.path.abspath(__file__))

def _platform_data_dir(base):
    """Persistent data directory: the platform's DATA_DIR env var when
    provided, else the ./data symlink the panel creates inside the
    project folder (pointing at the central data directory), else
    ./.runtime for standalone local runs."""
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(base, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(base, ".runtime")

RUNTIME = _platform_data_dir(BASE)
LINKS_FILE = os.path.join(RUNTIME, "links.json")
OPENCODE_URL = "https://debi.su7.dpdns.org:31426/"
MAX_LINKS = 500

_account, _account_from_env = auth.load_account()



def _client_ip(environ):
    fwd = environ.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return environ.get("REMOTE_ADDR", "") or "unknown"

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
            "note": e.get("note", ""), "category": e.get("category", ""),
            "created_at": e.get("created_at")}


def _validate_link(data):
    name = str(data.get("name") or "").strip()
    url = str(data.get("url") or "").strip()
    note = str(data.get("note") or "").strip()
    category = str(data.get("category") or "").strip()
    if not (1 <= len(name) <= 80):
        return None, "名称需为 1–80 个字符。"
    if len(url) > 2048 or len(note) > 200 or len(category) > 40:
        return None, "链接、备注或分类过长。"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None, "链接需为完整的 http(s) 地址。"
    return {"name": name, "url": url, "note": note, "category": category}, None

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
    """Identity verification lives at the platform layer (the App Hub
    gate in front of /web/<slug>/). This app performs no verification
    itself: any request that reaches it has either passed the
    platform gate or arrived on the local port, which is trusted."""
    account = _current_account()
    return True, "platform", (account.username if account else "local")

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
        "setup_required": False,
        "authenticated": authed,
        "via": via,
        "username": username if authed else None,
    })

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
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0 auto; max-width: 880px; padding: 0 14px 56px; line-height: 1.5;
         -webkit-tap-highlight-color: transparent; }
  header { position: sticky; top: 0; z-index: 20; display: flex; gap: 8px; align-items: center;
           padding: 12px 2px 10px;
           background: color-mix(in srgb, Canvas 88%, transparent);
           backdrop-filter: blur(8px); }
  header h1 { font-size: 19px; margin: 0; white-space: nowrap; }
  #q { flex: 1; min-width: 0; padding: 9px 12px; border-radius: 999px; font-size: 15px;
       border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
       background: transparent; color: inherit; outline: none; }
  #q:focus { border-color: #2f6fd0; }
  .tbtn { padding: 8px 14px; border-radius: 999px; border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
          background: transparent; color: inherit; font-size: 14px; cursor: pointer; white-space: nowrap; }
  .tbtn.on { background: #2f6fd0; border-color: #2f6fd0; color: #fff; }
  .hintbar { display: none; font-size: 13px; opacity: .65; padding: 2px 2px 8px; }
  body.editing .hintbar { display: block; }
  h2.cat { font-size: 14px; margin: 20px 2px 10px; opacity: .75; font-weight: 600;
           display: flex; align-items: baseline; gap: 8px; }
  h2.cat .n { font-size: 12px; opacity: .6; font-weight: 400; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; }
  @media (max-width: 560px) { .grid { grid-template-columns: 1fr 1fr; } }
  .card { display: flex; gap: 10px; align-items: center; padding: 12px; border-radius: 14px;
          border: 1px solid color-mix(in srgb, currentColor 13%, transparent);
          background: color-mix(in srgb, currentColor 4%, transparent);
          text-decoration: none; color: inherit; position: relative; min-height: 64px;
          transition: transform .05s ease; }
  .card:active { transform: scale(.985); }
  .tile { flex: 0 0 auto; width: 40px; height: 40px; border-radius: 11px; color: #fff;
          display: flex; align-items: center; justify-content: center;
          font-size: 18px; font-weight: 700; background: hsl(var(--h, 210) 52% 44%); }
  .meta { min-width: 0; }
  .name { font-size: 15px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub2 { font-size: 12px; opacity: .55; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  body.editing .card { border-style: dashed; cursor: pointer; }
  body.editing .card::after { content: "编辑"; position: absolute; top: 6px; right: 8px;
          font-size: 11px; opacity: .55; }
  .addcard { display: none; align-items: center; justify-content: center; min-height: 64px;
          border-radius: 14px; border: 1.5px dashed color-mix(in srgb, currentColor 30%, transparent);
          font-size: 15px; opacity: .75; cursor: pointer; background: transparent; color: inherit; }
  body.editing .addcard { display: flex; }
  .empty { opacity: .55; font-size: 14px; padding: 26px 2px; }
  footer { margin-top: 30px; font-size: 12px; opacity: .45; padding: 0 2px; }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.48); z-index: 50;
             display: none; align-items: flex-end; justify-content: center; }
  .overlay.show { display: flex; }
  .sheet { background: Canvas; color: CanvasText; width: 100%; max-width: 520px;
           border-radius: 18px 18px 0 0; padding: 18px 18px 22px; }
  @media (min-width: 640px) { .overlay { align-items: center; } .sheet { border-radius: 18px; } }
  .sheet h3 { margin: 0 0 6px; font-size: 16px; }
  .sheet label { display: block; font-size: 13px; margin: 12px 0 4px; opacity: .8; }
  .sheet input { width: 100%; padding: 10px 12px; border-radius: 10px; font-size: 15px;
           border: 1px solid color-mix(in srgb, currentColor 28%, transparent);
           background: transparent; color: inherit; outline: none; }
  .sheet input:focus { border-color: #2f6fd0; }
  .srow { display: flex; gap: 8px; margin-top: 18px; align-items: center; }
  .sbtn { padding: 10px 18px; border-radius: 10px; border: 0; background: #2f6fd0; color: #fff;
          font-size: 14px; cursor: pointer; }
  .sbtn.ghost { background: transparent; color: inherit;
          border: 1px solid color-mix(in srgb, currentColor 28%, transparent); }
  .sbtn.danger { background: transparent; color: #dc2626;
          border: 1px solid color-mix(in srgb, #dc2626 45%, transparent); }
  .msg { min-height: 18px; font-size: 13px; margin-top: 8px; }
  .err { color: #dc2626; }
  #gate-note { display: none; padding: 40px 6px; font-size: 15px; opacity: .75; }
</style>
</head>
<body>
<header>
  <h1>微书签</h1>
  <input id="q" type="search" placeholder="搜索链接…" autocomplete="off">
  <button id="editToggle" class="tbtn" type="button">编辑</button>
</header>
<div class="hintbar">编辑模式：点任意卡片修改，点虚线框添加新链接；打开链接请先退出编辑模式。</div>
<main id="board"></main>
<div id="gate-note">这个页面由平台统一验证。请从控制台进入，或完成平台验证后刷新本页。</div>
<footer id="foot"></footer>

<div class="overlay" id="overlay">
  <form class="sheet" id="sheet">
    <h3 id="sheetTitle">添加链接</h3>
    <label>名称</label>
    <input id="s-name" type="text" required maxlength="80" placeholder="例如 OpenCode">
    <label>链接</label>
    <input id="s-url" type="url" required maxlength="2048" placeholder="https://…">
    <label>备注（可选）</label>
    <input id="s-note" type="text" maxlength="200">
    <label>分类（可选）</label>
    <input id="s-cat" type="text" maxlength="40" list="catlist" placeholder="例如 平台 / 工具 / 记录">
    <datalist id="catlist"></datalist>
    <div class="srow">
      <button id="s-del" type="button" class="sbtn danger" hidden>删除</button>
      <span style="flex:1"></span>
      <button id="s-cancel" type="button" class="sbtn ghost">取消</button>
      <button type="submit" class="sbtn">保存</button>
    </div>
    <p class="msg" id="sheetMsg"></p>
  </form>
</div>

<script>
const $ = id => document.getElementById(id);
let allLinks = [];
let editing = false;
let sheetId = null;

async function api(path, options) {
  const res = await fetch(path, {credentials: "same-origin", cache: "no-store", ...options});
  let data = {};
  try { data = await res.json(); } catch (e) {}
  return {status: res.status, data};
}
const post = (path, body) => api(path, {method: "POST",
  headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function hueOf(s) { let h = 0; for (const ch of s) h = (h * 31 + ch.codePointAt(0)) % 360; return h; }
function hostOf(u) { try { return new URL(u).host; } catch (e) { return u; } }

function groups() {
  const q = $("q").value.trim().toLowerCase();
  const filtered = allLinks.filter(l => !q ||
    (l.name + " " + (l.note || "") + " " + l.url + " " + (l.category || "")).toLowerCase().includes(q));
  const order = [];
  const byCat = new Map();
  for (const l of filtered) {
    const c = (l.category || "").trim();
    if (!byCat.has(c)) { byCat.set(c, []); order.push(c); }
    byCat.get(c).push(l);
  }
  order.sort((a, b) => (a === "" ? 1 : 0) - (b === "" ? 1 : 0));
  return order.map(c => ({cat: c, items: byCat.get(c)}));
}

function cardHtml(l) {
  const letter = esc(Array.from(l.name || "?")[0] || "?");
  const sub = esc(l.note || hostOf(l.url));
  const inner = `<span class="tile" style="--h:${hueOf(l.name || "")}">${letter}</span>` +
    `<span class="meta"><span class="name">${esc(l.name)}</span><br>` +
    `<span class="sub2">${sub}</span></span>`;
  if (editing) return `<div class="card" data-edit="${l.id}">${inner}</div>`;
  return `<a class="card" href="${esc(l.url)}" target="_blank" rel="noopener">${inner}</a>`;
}

function render() {
  const gs = groups();
  const board = $("board");
  if (!allLinks.length) {
    board.innerHTML = `<div class="empty">暂无链接。点右上角「编辑」添加第一个。</div>`;
  } else if (!gs.length) {
    board.innerHTML = `<div class="empty">没有匹配的链接。</div>`;
  } else {
    board.innerHTML = gs.map(g =>
      `<h2 class="cat">${esc(g.cat || "未分类")}<span class="n">${g.items.length}</span></h2>` +
      `<div class="grid">` + g.items.map(cardHtml).join("") +
      (editing && !($("q").value.trim()) ? `<button type="button" class="addcard" data-add="${esc(g.cat)}">＋ 添加</button>` : "") +
      `</div>`).join("");
  }
  board.querySelectorAll("[data-edit]").forEach(el => el.onclick = () => openSheet(el.dataset.edit));
  board.querySelectorAll("[data-add]").forEach(el => el.onclick = () => openSheet(null, el.dataset.add));
  const cats = [...new Set(allLinks.map(l => (l.category || "").trim()).filter(Boolean))];
  $("catlist").innerHTML = cats.map(c => `<option value="${esc(c)}">`).join("");
  $("foot").textContent = allLinks.length ? `共 ${allLinks.length} 条 · 点卡片打开，编辑是低频操作，收在右上角「编辑」里` : "";
}

async function loadLinks() {
  const {status, data} = await api("api/links");
  if (status !== 200) { $("board").innerHTML = `<div class="empty">加载失败，请刷新重试。</div>`; return; }
  allLinks = data.links || [];
  render();
}

function openSheet(id, presetCat) {
  const l = id ? allLinks.find(x => x.id === id) : null;
  sheetId = id || null;
  $("sheetTitle").textContent = l ? "编辑链接" : "添加链接";
  $("s-name").value = l ? l.name : "";
  $("s-url").value = l ? l.url : "";
  $("s-note").value = l ? (l.note || "") : "";
  $("s-cat").value = l ? (l.category || "") : (presetCat || "");
  $("s-del").hidden = !l;
  $("sheetMsg").textContent = "";
  $("overlay").classList.add("show");
  setTimeout(() => $("s-name").focus(), 30);
}
function closeSheet() { $("overlay").classList.remove("show"); }
$("s-cancel").onclick = closeSheet;
$("overlay").addEventListener("click", e => { if (e.target === $("overlay")) closeSheet(); });

$("sheet").onsubmit = async (e) => {
  e.preventDefault();
  const body = {name: $("s-name").value, url: $("s-url").value,
                note: $("s-note").value, category: $("s-cat").value};
  const path = sheetId ? "api/links/" + sheetId : "api/links";
  const {status, data} = await post(path, body);
  if (status !== 200) { $("sheetMsg").innerHTML = `<span class="err">${esc(data.message || "保存失败")}</span>`; return; }
  closeSheet();
  await loadLinks();
};
$("s-del").onclick = async () => {
  if (!sheetId || !confirm("删除这条链接？")) return;
  const {status, data} = await api("api/links/" + sheetId, {method: "DELETE"});
  if (status !== 200) { $("sheetMsg").innerHTML = `<span class="err">${esc(data.message || "删除失败")}</span>`; return; }
  closeSheet();
  await loadLinks();
};

$("editToggle").onclick = () => {
  editing = !editing;
  document.body.classList.toggle("editing", editing);
  $("editToggle").textContent = editing ? "完成" : "编辑";
  $("editToggle").classList.toggle("on", editing);
  render();
};
$("q").addEventListener("input", render);

(async () => {
  const {data} = await api("api/session");
  if (!data.authenticated) { $("gate-note").style.display = "block"; return; }
  await loadLinks();
})();
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
        if path == "/api/session" and method == "GET":
            return handle_session(environ, start_response)
        if path in ("/api/setup", "/api/login") and method == "POST":
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "not_found", "message": "接口不存在。"})
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
