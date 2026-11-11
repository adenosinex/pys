"""微书签 — a tiny personal link hub for the App Hub.

A single page of bookmarks the user can add to from the browser.
This app contains no identity verification: the App Hub platform
gate in front of /web/links/ performs it centrally, and direct
access on the local port is trusted.
"""
import json
import os
import re
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

# ---------------------------------------------------------------- platform project categories

PANEL_DB = "/srv/apphub/data/panel/apphub.sqlite3"
SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
SLUG_RESERVED = {"panel", "api", "static", "login", "logout", "backups",
                 "github", "settings", "data", "env", "root"}
_PROJCAT_LOCK = threading.Lock()
_PROJCAT_CACHE = {"ts": 0.0, "data": None}

# kind -> (display name, intro line, submit button label)
PROJCAT_KINDS = {
    "html": ("HTML 项目", "平台登记的静态网页项目，点卡片在新窗口打开。", "发布 HTML 项目"),
    "py": ("PY 项目", "平台登记的 Python Web 应用项目，点卡片在新窗口打开。", "发布 PY 项目"),
}


def _panel_db_connect():
    import sqlite3
    db = sqlite3.connect("file:%s?mode=ro" % PANEL_DB, uri=True, timeout=5)
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def _read_registry():
    """Read the panel project registry (read-only): {kind: [entries]}.
    Returns None when the registry cannot be read (missing / locked /
    schema drift) so callers can fall back to an empty view."""
    try:
        db = _panel_db_connect()
        try:
            rows = db.execute(
                "SELECT id, slug, name, project_type, description, created_at "
                "FROM projects WHERE project_type IN ('html', 'script', 'web') "
                "ORDER BY created_at ASC, id ASC").fetchall()
        finally:
            db.close()
    except Exception:
        return None
    out = {"html": [], "py": []}
    for pid, slug, name, ptype, desc, created in rows:
        kind = "html" if ptype == "html" else "py"
        # The panel's Python web apps register as project_type "web";
        # they form the "py" zone here and open at /web/<slug>/.
        # Pasted script projects register as "script" (/app/<slug>/).
        if ptype == "html":
            url = "/site/%s/" % slug
        elif ptype == "web":
            url = "/web/%s/" % slug
        else:
            url = "/app/%s/" % slug
        out[kind].append({
            "id": pid, "slug": slug, "name": name or slug, "kind": kind,
            "note": desc or "",
            "url": url,
            "created_at": created,
        })
    return out


def _registry_cached():
    """Serve the registry cache. A refresh is only a short read-only
    sqlite query, so it is ALWAYS done outside _PROJCAT_LOCK: the
    create path holds that lock across its registration bridge call,
    and a request thread that blocked on the lock would hang behind
    the whole bridge round-trip. Stale data (up to the 15 s TTL) is
    acceptable for display; the create path validates duplicates
    against fresh data itself."""
    now = time.time()
    cache = _PROJCAT_CACHE
    if cache["data"] is not None and now - cache["ts"] < 15:
        return cache["data"]
    data = _read_registry()
    if data is not None:
        _PROJCAT_CACHE.update(ts=time.time(), data=data)
        return data
    return cache["data"] or {"html": [], "py": []}


def _invalidate_registry_cache():
    _PROJCAT_CACHE.update(ts=0.0, data=None)


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


def _read_body(environ, limit):
    """Read exactly the declared request body.

    Contract: read AT MOST CONTENT_LENGTH bytes in total and return
    them. Never issue a second read once the declared length has
    been collected, and never read past it: behind the panel /web
    proxy the body stream is a spooled replay object that misbehaves
    under repeated or oversized reads, while a single bounded read
    (the pattern this app has always used) is reliable. A stream
    that returns fewer bytes per call than asked is handled by
    looping on the REMAINING count; a stream that returns more than
    asked is cut to size. Bounded total reads also means this loop
    always terminates within ceil(length / 1) iterations.
    """
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return b""
    if length > limit:
        return None
    stream = environ.get("wsgi.input")
    if stream is None:
        return b""
    chunks = []
    got = 0
    try:
        while got < length:
            chunk = stream.read(min(65536, length - got))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
    except Exception:
        return b""
    data = b"".join(chunks)
    return data[:length] if len(data) > length else data


def _read_json(environ, limit=65536):
    raw = _read_body(environ, limit)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8") or "{}")
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


def handle_links_export(environ, start_response):
    payload = {"format": "links-backup", "version": 1,
               "links": [{k: e.get(k, "") for k in ("name", "url", "note", "category")}
                         for e in _load_links()]}
    body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
    start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                              ("Content-Disposition", 'attachment; filename="links-backup.json"'),
                              ("Content-Length", str(len(body)))])
    return [body]


def _validate_import_link(data):
    # Imports additionally accept the app's internal relative URLs
    # ("/web/...") that seeded data uses; the create form still
    # requires full http(s) URLs.
    entry, err = _validate_link(data)
    if entry is not None:
        return entry, None
    name = str(data.get("name") or "").strip()
    url = str(data.get("url") or "").strip()
    note = str(data.get("note") or "").strip()
    category = str(data.get("category") or "").strip()
    if (1 <= len(name) <= 80 and url.startswith("/") and len(url) <= 2048
            and len(note) <= 200 and len(category) <= 40):
        return {"name": name, "url": url, "note": note, "category": category}, None
    return None, err


def handle_links_import(environ, start_response):
    data = _read_json(environ, limit=4194304)
    items = data.get("links") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "bad_backup", "message": "备份格式不对。"})
    links = _load_links()
    seen = {e.get("url") for e in links}
    added = skipped = 0
    for item in items:
        entry = None
        if isinstance(item, dict) and len(links) < MAX_LINKS:
            entry, _err = _validate_import_link(item)
        if entry is None or entry["url"] in seen:
            skipped += 1
            continue
        links.append({"id": secrets.token_hex(4), "created_at": time.time(), **entry})
        seen.add(entry["url"])
        added += 1
    if added:
        _save_links(links)
    return _json(start_response, "200 OK", {"ok": True, "added": added, "skipped": skipped})


def handle_links_delete(environ, start_response, lid):
    with _links_lock:
        links = _load_links()
        kept = [e for e in links if e["id"] != lid]
        if len(kept) == len(links):
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "not_found", "message": "链接不存在。"})
        _save_links(kept)
    return _json(start_response, "200 OK", {"ok": True})


def handle_projects_list(environ, start_response, kind):
    reg = _registry_cached()
    return _json(start_response, "200 OK",
                 {"ok": True, "kind": kind, "projects": reg.get(kind, [])})


def handle_projects_create(environ, start_response, kind):
    data = _read_json(environ, limit=1048576)
    slug = str(data.get("slug") or "").strip()
    name = str(data.get("name") or slug).strip()
    desc = str(data.get("description") or "").strip()
    code = str(data.get("code") or "")
    if not SLUG_RE.match(slug) or slug.lower() in SLUG_RESERVED:
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "bad_slug",
                      "message": "标识（slug）需为 1–63 位字母、数字、连字符或下划线，且不与平台保留名冲突。"})
    if not (1 <= len(name) <= 100):
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "bad_name", "message": "名称需为 1–100 个字符。"})
    if len(desc) > 500 or len(code) > 1048576:
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "too_large", "message": "说明或代码过长。"})
    if not code.strip():
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "empty_code",
                      "message": ("请粘贴 HTML 代码。" if kind == "html" else "请粘贴 Python 代码。")})
    # Fresh registry snapshot for the fast-path duplicate check (the
    # DB existence check further down is authoritative). This read
    # never involves _PROJCAT_LOCK.
    reg_now = _registry_cached()
    with _PROJCAT_LOCK:
        if any(p["slug"] == slug for entries in reg_now.values() for p in entries):
            return _json(start_response, "409 Conflict",
                         {"ok": False, "error": "slug_taken", "message": "该标识已被占用，请换一个。"})
        try:
            db = _panel_db_connect()
            try:
                taken = db.execute(
                    "SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone()
            finally:
                db.close()
        except Exception:
            taken = None
        if taken:
            return _json(start_response, "409 Conflict",
                         {"ok": False, "error": "slug_taken", "message": "该标识已被占用，请换一个。"})
        # The panel bridge runs in its own interpreter next to the panel
        # (/tmp/apphub-register-bridge.py, written by the installer): it
        # imports the panel's own services and registers the project
        # through services.project_manager.create_or_update_project,
        # so registration semantics (code dir, data dir, Caddy site
        # config, audit-side effects) stay identical to the console.
        payload = {"slug": slug, "name": name, "description": desc,
                   "code": code, "kind": kind}
        try:
            env = dict(os.environ)
            env["APPHUB_REGISTER_PAYLOAD"] = json.dumps(payload, ensure_ascii=False)
            import subprocess
            cp = subprocess.run(
                ["/srv/apphub/envs/panel/bin/python",
                 "/tmp/apphub-register-bridge.py"],
                env=env, capture_output=True, text=True, timeout=20)
            try:
                result = json.loads((cp.stdout or "").strip().splitlines()[-1])
            except Exception:
                result = {}
            if cp.returncode != 0 or not result.get("ok"):
                return _json(start_response, "502 Bad Gateway",
                             {"ok": False, "error": "register_failed",
                              "message": "平台登记失败：" + str(result.get("error") or "未知错误")[:200]})
        except Exception as exc:  # noqa: BLE001
            return _json(start_response, "502 Bad Gateway",
                         {"ok": False, "error": "register_failed",
                          "message": "平台登记失败：" + str(exc)[:200]})
        _invalidate_registry_cache()
    reg = _registry_cached()
    proj = next((p for p in reg.get(kind, []) if p["slug"] == slug), None)
    if proj is None:
        # The bridge already confirmed registration; if the registry
        # read raced, still hand back the canonical entry shape.
        label, _intro, _btn = PROJCAT_KINDS[kind]
        proj = {"slug": slug, "name": name, "kind": kind, "note": desc,
                "url": ("/site/%s/" % slug) if kind == "html" else ("/app/%s/" % slug),
                "created_at": None}
    return _json(start_response, "200 OK", {"ok": True, "project": proj})


os.makedirs(RUNTIME, exist_ok=True)

# ---------------------------------------------------------------- page

PAGE_HOME = """<!DOCTYPE html>
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
  .editbar { display: none; gap: 8px; align-items: center; padding: 0 2px 10px; flex-wrap: wrap; }
  body.editing .editbar { display: flex; }
  #impMsg { font-size: 12px; opacity: .75; }
  h2.cat { font-size: 14px; margin: 20px 2px 10px; opacity: .75; font-weight: 600;
           display: flex; align-items: baseline; gap: 8px; }
  h2.cat .n { font-size: 12px; opacity: .6; font-weight: 400; }
  .zones { display: grid; gap: 10px; margin-top: 4px; }
  .zone { border: 1px solid color-mix(in srgb, currentColor 13%, transparent);
          background: color-mix(in srgb, currentColor 4%, transparent);
          border-radius: 16px; padding: 12px 12px 10px; }
  .zone-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 9px; }
  .zone-title { font-size: 15px; font-weight: 700; }
  .zone-head .n { font-size: 12px; opacity: .6; }
  .zone-all { margin-left: auto; font-size: 13px; color: #2f6fd0; text-decoration: none; white-space: nowrap; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { display: inline-flex; align-items: baseline; gap: 5px; padding: 6px 11px; border-radius: 999px;
          border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
          font-size: 13px; color: inherit; text-decoration: none; }
  .chip .n { font-size: 11px; opacity: .55; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 12px 8px; }
  @media (max-width: 560px) { .grid { grid-template-columns: repeat(4, 1fr); } }
  .card { display: flex; flex-direction: column; align-items: center; gap: 7px; min-width: 0;
          padding: 12px 4px 10px; border-radius: 16px;
          border: 1px solid color-mix(in srgb, currentColor 13%, transparent);
          background: color-mix(in srgb, currentColor 4%, transparent);
          text-decoration: none; color: inherit; position: relative;
          transition: transform .05s ease; }
  .card:active { transform: scale(.97); }
  .tile { flex: 0 0 auto; width: 52px; height: 52px; border-radius: 14px; color: #fff;
          display: flex; align-items: center; justify-content: center; overflow: hidden;
          font-size: 22px; font-weight: 700; background: hsl(var(--h, 210) 52% 44%); }
  .tile img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .meta { width: 100%; min-width: 0; text-align: center; }
  .name { display: block; min-width: 0; font-size: 13.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub2 { display: block; font-size: 11px; height: 15px; line-height: 15px; opacity: .55; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .date2 { display: block; font-size: 10px; height: 13px; line-height: 13px; opacity: .45; }
  body.editing .card { border-style: dashed; cursor: pointer; }
  body.editing .card::after { content: "编辑"; position: absolute; top: 6px; right: 8px;
          font-size: 11px; opacity: .55; }
  .addcard { display: none; flex-direction: column; align-items: center; justify-content: center; gap: 6px; min-height: 118px;
          border-radius: 16px; border: 1.5px dashed color-mix(in srgb, currentColor 30%, transparent);
          font-size: 14px; opacity: .75; cursor: pointer; background: transparent; color: inherit; }
  body.editing .addcard { display: flex; }
  .empty { opacity: .55; font-size: 14px; padding: 26px 2px; }
  .sheet textarea { width: 100%; padding: 10px 12px; border-radius: 10px; font-size: 13px;
           border: 1px solid color-mix(in srgb, currentColor 28%, transparent);
           background: transparent; color: inherit; outline: none; font-family: ui-monospace, Menlo, Consolas, monospace; }
  .sheet textarea:focus { border-color: #2f6fd0; }
  .crumb { font-size: 13px; padding: 2px 2px 0; }
  .crumb a { color: #2f6fd0; text-decoration: none; }
  .intro { font-size: 13px; opacity: .65; padding: 4px 2px 0; }
  body:not(.editing) .delcard { display: none !important; }
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
<div class="editbar">
  <button id="expBtn" class="tbtn" type="button">导出备份</button>
  <label class="tbtn" for="impFile" style="cursor:pointer">导入备份</label>
  <input id="impFile" type="file" accept=".json,application/json" hidden>
  <span id="impMsg"></span>
</div>
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
let regData = {html: [], py: []};
const PROJCAT = {html: {name: "HTML 项目"}, py: {name: "PY 项目"}};
const APPCATS = {
  py: [
    {name: "内容媒体", slugs: ["zhihu-core", "yt-audio", "vidlib", "beastlib"]},
    {name: "文件工具", slugs: ["transfer", "cloudfiles", "nameclean", "filetools"]},
    {name: "记录数据", slugs: ["records", "cards", "docsnew"]},
    {name: "网络工具", slugs: ["subconv", "dnsmap", "speedtest", "openrouter"]},
    {name: "平台效率", slugs: ["links", "clipboard", "burn", "medatlas"]}
  ],
  html: [
    {name: "实用工具", slugs: ["p1791363651", "p1791168459", "p1790594923", "p1789779981", "p1789648409", "p1785485829", "p1785306635", "p1785206347", "p1780063319", "p1778768844", "magnet", "textuni"]},
    {name: "AI 与模型", slugs: ["aicn", "p1788428731", "p1788428729", "p1787576892", "p1785741597", "p1780733162", "p1779539600", "aitest", "ormodel"]},
    {name: "健身与体能", slugs: ["p1783040627", "p1779874258", "p1779872310", "p1779868187", "p1779266591", "p1779202861", "p1779198872", "p1779109601", "p1779020616", "p1778982714", "p1778769593"]},
    {name: "健康与身体", slugs: ["p1790396626", "p1790396609", "p1783259896", "p1781665039", "p1779697722", "p1778981387"]},
    {name: "户外与出行", slugs: ["p1792850445", "p1787039714", "p1781923402", "p1780188079", "p1778984453", "p1778983199", "p1778983019", "p1778981876", "p1778981444", "p1778980516"]},
    {name: "设备与车辆", slugs: ["p1783476971", "p1782215175", "p1779544522", "p1779349290", "p1779001444", "p1778982865", "p1778982461", "p1778981955"]},
    {name: "认知与社会", slugs: ["p1792563415", "p1791946410", "p1790172372", "p1779853529", "p1779778997", "p1779546460", "p1779090887", "p1778991781", "p1778983287", "p1778982505"]},
    {name: "选购依据", slugs: ["vps-buying-guide", "vps-benchmark-compare", "vps-platform-limits"]},
  ]
};
function catOf(p) {
  const cats = p.kind === "py" ? APPCATS.py : APPCATS.html;
  for (const c of cats) if (c.slugs.indexOf(p.slug) >= 0) return c.name;
  return "其他";
}
function catOrder(kind) {
  const cats = kind === "py" ? APPCATS.py : APPCATS.html;
  return cats.map(c => c.name).concat(["其他"]);
}
function groupProjects(items, kind) {
  const by = new Map();
  for (const p of items) {
    const c = catOf(p);
    if (!by.has(c)) by.set(c, []);
    by.get(c).push(p);
  }
  return catOrder(kind).filter(c => by.has(c)).map(c => ({cat: c, items: by.get(c)}));
}

const viewMatch = location.pathname.match(/\/(html|py)\/?$/);
const catKind = viewMatch ? viewMatch[1] : null;
const basePath = location.pathname.replace(/\/(html|py)\/?$/, "").replace(/\/?$/, "/");

async function api(path, options) {
  const url = path.startsWith("/") ? path : basePath + path;
  const res = await fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
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

function slugOf(u) { const m = String(u || "").match(/\/web\/([a-z0-9\-_]+)\//); return m ? m[1] : null; }
function tileFallback(img) { const t = img.parentNode; t.textContent = t.dataset.letter || "?"; }
function dateLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const day = x => x.toDateString();
  if (day(d) === day(now)) return "今天添加";
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (day(d) === day(y)) return "昨天添加";
  return (d.getMonth() + 1) + "-" + String(d.getDate()).padStart(2, "0") + " 添加";
}
function cardHtml(l, withDate) {
  const letter = esc(Array.from(l.name || "?")[0] || "?");
  const sub = esc(l.note || "");
  const dateHtml = withDate ? `<span class="date2">${esc(dateLabel(l.created_at))}</span>` : "";
  const slug = slugOf(l.url);
  const tileInner = slug
    ? `<img src="/assets/logos/${slug}" alt="" loading="lazy" onerror="tileFallback(this)">`
    : letter;
  const inner = `<span class="tile" data-letter="${letter}" style="--h:${hueOf(l.name || "")}">${tileInner}</span>` +
    `<span class="meta"><span class="name">${esc(l.name)}</span>` +
    `<span class="sub2">${sub}</span>${dateHtml}</span>`;
  if (editing) return `<div class="card" data-edit="${l.id}">${inner}</div>`;
  return `<a class="card" href="${esc(l.url)}" target="_blank" rel="noopener">${inner}</a>`;
}

function projCard(p) {
  const letter = esc(Array.from(p.name || "?")[0] || "?");
  const tileInner = `<img src="/assets/logos/${p.slug}" alt="" loading="lazy" onerror="tileFallback(this)">`;
  const inner = `<span class="tile" data-letter="${letter}" style="--h:${hueOf(p.name || "")}">${tileInner}</span>` +
    `<span class="meta"><span class="name">${esc(p.name)}</span>` +
    `<span class="sub2">${esc(p.note || "")}</span></span>`;
  if (editing) return `<a class="card" href="${esc(p.url)}" target="_blank" rel="noopener" title="编辑模式下也能打开">${inner}</a>`;
  return `<a class="card" href="${esc(p.url)}" target="_blank" rel="noopener">${inner}</a>`;
}
function specialSectionsHtml() {
  if (editing || catKind || $("q").value.trim()) return "";
  const zoneNames = {py: "Python 应用", html: "HTML 页面"};
  const zones = ["py", "html"].map(k => {
    const items = regData[k] || [];
    const chips = groupProjects(items, k).map(g =>
      `<a class="chip" href="${k}/?cat=${encodeURIComponent(g.cat)}">${esc(g.cat)}<span class="n">${g.items.length}</span></a>`).join("");
    return `<div class="zone"><div class="zone-head">` +
      `<span class="zone-title">${zoneNames[k]}</span><span class="n">${items.length}</span>` +
      `<a class="zone-all" href="${k}/">进入分区 →</a></div>` +
      `<div class="chips">${chips}</div></div>`;
  }).join("");
  return `<h2 class="cat">应用分区</h2><div class="zones">${zones}</div>`;
}

function render() {
  const gs = groups();
  const board = $("board");
  if (!allLinks.length) {
    board.innerHTML = specialSectionsHtml() + `<div class="empty">暂无链接。点右上角「编辑」添加第一个。</div>`;
  } else if (!gs.length) {
    board.innerHTML = `<div class="empty">没有匹配的链接。</div>`;
  } else {
    let recentHtml = "";
    if (!editing && !$("q").value.trim()) {
      const recent = [...allLinks].filter(l => l.created_at)
        .sort((a, b) => b.created_at - a.created_at).slice(0, 8);
      if (recent.length >= 2) {
        recentHtml = `<h2 class="cat">最近添加</h2><div class="grid">` +
          recent.map(l => cardHtml(l, true)).join("") + `</div>`;
      }
    }
    board.innerHTML = recentHtml + specialSectionsHtml() + gs.map(g =>
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
  const pr = await api("api/projects");
  if (pr.status === 200 && pr.data.projects) regData = pr.data.projects;
  if (catKind) document.title = PROJCAT[catKind].name + " · 微书签";
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

$("expBtn").onclick = async () => {
  const r = await fetch(basePath + "api/links/export");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await r.blob());
  a.download = "links-backup.json";
  document.body.appendChild(a); a.click(); a.remove();
};
$("impFile").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const payload = JSON.parse(await f.text());
    const r = await fetch(basePath + "api/links/import", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const d = await r.json();
    if (!d.ok) throw new Error(d.message || "导入失败");
    $("impMsg").textContent = `导入完成：新增 ${d.added}，跳过 ${d.skipped}（按网址去重）`;
    await loadLinks();
  } catch (err) { $("impMsg").textContent = "导入失败：" + err.message; }
  e.target.value = "";
});

(async () => {
  const {data} = await api("api/session");
  if (!data.authenticated) {
    if (!sessionStorage.getItem("gateRetried")) { sessionStorage.setItem("gateRetried", "1"); location.reload(); return; }
    $("gate-note").style.display = "block"; return;
  }
  sessionStorage.removeItem("gateRetried");
  await loadLinks();
  if (window.__afterLoad) window.__afterLoad();
})();
</script>
</body>
</html>
"""

PAGE_HTML = """<!DOCTYPE html>
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
  .editbar { display: none; gap: 8px; align-items: center; padding: 0 2px 10px; flex-wrap: wrap; }
  body.editing .editbar { display: flex; }
  #impMsg { font-size: 12px; opacity: .75; }
  h2.cat { font-size: 14px; margin: 20px 2px 10px; opacity: .75; font-weight: 600;
           display: flex; align-items: baseline; gap: 8px; }
  h2.cat .n { font-size: 12px; opacity: .6; font-weight: 400; }
  .catnav { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 2px 0; }
  .catnav .chip { display: inline-flex; align-items: baseline; gap: 5px; padding: 6px 11px; border-radius: 999px;
          border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
          background: transparent; color: inherit; font-size: 13px; cursor: pointer; font-family: inherit; }
  .catnav .chip .n { font-size: 11px; opacity: .55; }
  h2.cat.sec { cursor: pointer; user-select: none; }
  h2.cat.sec .tog { margin-left: auto; font-size: 12px; opacity: .6; font-weight: 400; }
  .secgrid.off { display: none; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 12px 8px; }
  @media (max-width: 560px) { .grid { grid-template-columns: repeat(4, 1fr); } }
  .card { display: flex; flex-direction: column; align-items: center; gap: 7px; min-width: 0;
          padding: 12px 4px 10px; border-radius: 16px;
          border: 1px solid color-mix(in srgb, currentColor 13%, transparent);
          background: color-mix(in srgb, currentColor 4%, transparent);
          text-decoration: none; color: inherit; position: relative;
          transition: transform .05s ease; }
  .card:active { transform: scale(.97); }
  .tile { flex: 0 0 auto; width: 52px; height: 52px; border-radius: 14px; color: #fff;
          display: flex; align-items: center; justify-content: center; overflow: hidden;
          font-size: 22px; font-weight: 700; background: hsl(var(--h, 210) 52% 44%); }
  .tile img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .meta { width: 100%; min-width: 0; text-align: center; }
  .name { display: block; min-width: 0; font-size: 13.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub2 { display: block; font-size: 11px; height: 15px; line-height: 15px; opacity: .55; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .date2 { display: block; font-size: 10px; height: 13px; line-height: 13px; opacity: .45; }
  body.editing .card { border-style: dashed; cursor: pointer; }
  body.editing .card::after { content: "编辑"; position: absolute; top: 6px; right: 8px;
          font-size: 11px; opacity: .55; }
  .addcard { display: none; flex-direction: column; align-items: center; justify-content: center; gap: 6px; min-height: 118px;
          border-radius: 16px; border: 1.5px dashed color-mix(in srgb, currentColor 30%, transparent);
          font-size: 14px; opacity: .75; cursor: pointer; background: transparent; color: inherit; }
  body.editing .addcard { display: flex; }
  .empty { opacity: .55; font-size: 14px; padding: 26px 2px; }
  .sheet textarea { width: 100%; padding: 10px 12px; border-radius: 10px; font-size: 13px;
           border: 1px solid color-mix(in srgb, currentColor 28%, transparent);
           background: transparent; color: inherit; outline: none; font-family: ui-monospace, Menlo, Consolas, monospace; }
  .sheet textarea:focus { border-color: #2f6fd0; }
  .crumb { font-size: 13px; padding: 2px 2px 0; }
  .crumb a { color: #2f6fd0; text-decoration: none; }
  .intro { font-size: 13px; opacity: .65; padding: 4px 2px 0; }
  body:not(.editing) .delcard { display: none !important; }
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
<div class="editbar">
  <button id="expBtn" class="tbtn" type="button">导出备份</button>
  <label class="tbtn" for="impFile" style="cursor:pointer">导入备份</label>
  <input id="impFile" type="file" accept=".json,application/json" hidden>
  <span id="impMsg"></span>
</div>
<main id="board"></main>
<style>
  #pubwrap { margin-top: 6px; }
  .pubform label { display: block; font-size: 13px; margin: 12px 0 4px; opacity: .8; }
  .pubform input { width: 100%; padding: 10px 12px; border-radius: 10px; font-size: 15px;
           border: 1px solid color-mix(in srgb, currentColor 28%, transparent);
           background: transparent; color: inherit; outline: none; }
  .pubform input:focus { border-color: #2f6fd0; }
  .pubform textarea { width: 100%; padding: 10px 12px; border-radius: 10px; font-size: 13px;
           border: 1px solid color-mix(in srgb, currentColor 28%, transparent);
           background: transparent; color: inherit; outline: none; font-family: ui-monospace, Menlo, Consolas, monospace; }
  .pubform textarea:focus { border-color: #2f6fd0; }
  .uprow { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .uprow .tbtn { display: inline-block; margin: 0; padding: 8px 14px; border-radius: 999px;
          border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
          background: transparent; color: inherit; font-size: 14px; cursor: pointer; white-space: nowrap; }
  .fnote { font-size: 12px; opacity: .7; }
  #pf-fhint { margin: 6px 0 0; }
</style>
<section id="pubwrap" hidden>
  <h2 class="cat" style="font-size:17px;opacity:1;">发布 HTML 项目</h2>
  <form id="pubform" class="pubform">
    <label>名称</label>
    <input id="pf-name" type="text" required maxlength="100" placeholder="例如 天气看板">
    <label>标识 slug（小写字母 / 数字 / 连字符，定后即网址的一部分）</label>
    <input id="pf-slug" type="text" required maxlength="63" placeholder="例如 weather-board" pattern="[a-z0-9][a-z0-9\-_]*">
    <label>说明（可选，显示为卡片小字）</label>
    <input id="pf-desc" type="text" maxlength="500">
    <label>上传 HTML 文件（可选，选中后自动填入下方代码框，与粘贴二选一）</label>
    <div class="uprow">
      <label class="tbtn" for="pf-file">选择 .html 文件</label>
      <input id="pf-file" type="file" accept=".html,.htm,text/html" hidden>
      <span id="pf-fname" class="fnote"></span>
    </div>
    <p class="fnote" id="pf-fhint">单个文件内容上限 1 MB（与粘贴发布一致），按 UTF-8 读取；名称自动取文件中的标题（&lt;title&gt;），没有则用文件名，填好后可再改。</p>
    <label>HTML 代码（将保存为 index.html）</label>
    <textarea id="pf-code" rows="10" required spellcheck="false" placeholder="粘贴完整的 HTML 代码（单个文件）"></textarea>
    <div class="srow">
      <span style="flex:1"></span>
      <button type="submit" class="sbtn">发布</button>
    </div>
    <p class="msg" id="pf-msg"></p>
  </form>
</section>
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
let regData = {html: [], py: []};
const PROJCAT = {html: {name: "HTML 项目"}, py: {name: "PY 项目"}};
const APPCATS = {
  py: [
    {name: "内容媒体", slugs: ["zhihu-core", "yt-audio", "vidlib", "beastlib"]},
    {name: "文件工具", slugs: ["transfer", "cloudfiles", "nameclean", "filetools"]},
    {name: "记录数据", slugs: ["records", "cards", "docsnew"]},
    {name: "网络工具", slugs: ["subconv", "dnsmap", "speedtest", "openrouter"]},
    {name: "平台效率", slugs: ["links", "clipboard", "burn", "medatlas"]}
  ],
  html: [
    {name: "实用工具", slugs: ["p1791363651", "p1791168459", "p1790594923", "p1789779981", "p1789648409", "p1785485829", "p1785306635", "p1785206347", "p1780063319", "p1778768844", "magnet", "textuni"]},
    {name: "AI 与模型", slugs: ["aicn", "p1788428731", "p1788428729", "p1787576892", "p1785741597", "p1780733162", "p1779539600", "aitest", "ormodel"]},
    {name: "健身与体能", slugs: ["p1783040627", "p1779874258", "p1779872310", "p1779868187", "p1779266591", "p1779202861", "p1779198872", "p1779109601", "p1779020616", "p1778982714", "p1778769593"]},
    {name: "健康与身体", slugs: ["p1790396626", "p1790396609", "p1783259896", "p1781665039", "p1779697722", "p1778981387"]},
    {name: "户外与出行", slugs: ["p1792850445", "p1787039714", "p1781923402", "p1780188079", "p1778984453", "p1778983199", "p1778983019", "p1778981876", "p1778981444", "p1778980516"]},
    {name: "设备与车辆", slugs: ["p1783476971", "p1782215175", "p1779544522", "p1779349290", "p1779001444", "p1778982865", "p1778982461", "p1778981955"]},
    {name: "认知与社会", slugs: ["p1792563415", "p1791946410", "p1790172372", "p1779853529", "p1779778997", "p1779546460", "p1779090887", "p1778991781", "p1778983287", "p1778982505"]},
    {name: "选购依据", slugs: ["vps-buying-guide", "vps-benchmark-compare", "vps-platform-limits"]},
  ]
};
function catOf(p) {
  const cats = p.kind === "py" ? APPCATS.py : APPCATS.html;
  for (const c of cats) if (c.slugs.indexOf(p.slug) >= 0) return c.name;
  return "其他";
}
function catOrder(kind) {
  const cats = kind === "py" ? APPCATS.py : APPCATS.html;
  return cats.map(c => c.name).concat(["其他"]);
}
function groupProjects(items, kind) {
  const by = new Map();
  for (const p of items) {
    const c = catOf(p);
    if (!by.has(c)) by.set(c, []);
    by.get(c).push(p);
  }
  return catOrder(kind).filter(c => by.has(c)).map(c => ({cat: c, items: by.get(c)}));
}

const viewMatch = location.pathname.match(/\/(html|py)\/?$/);
const catKind = viewMatch ? viewMatch[1] : null;
const basePath = location.pathname.replace(/\/(html|py)\/?$/, "").replace(/\/?$/, "/");

async function api(path, options) {
  const url = path.startsWith("/") ? path : basePath + path;
  const res = await fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
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

function slugOf(u) { const m = String(u || "").match(/\/web\/([a-z0-9\-_]+)\//); return m ? m[1] : null; }
function tileFallback(img) { const t = img.parentNode; t.textContent = t.dataset.letter || "?"; }
function dateLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const day = x => x.toDateString();
  if (day(d) === day(now)) return "今天添加";
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (day(d) === day(y)) return "昨天添加";
  return (d.getMonth() + 1) + "-" + String(d.getDate()).padStart(2, "0") + " 添加";
}
function cardHtml(l, withDate) {
  const letter = esc(Array.from(l.name || "?")[0] || "?");
  const sub = esc(l.note || "");
  const dateHtml = withDate ? `<span class="date2">${esc(dateLabel(l.created_at))}</span>` : "";
  const slug = slugOf(l.url);
  const tileInner = slug
    ? `<img src="/assets/logos/${slug}" alt="" loading="lazy" onerror="tileFallback(this)">`
    : letter;
  const inner = `<span class="tile" data-letter="${letter}" style="--h:${hueOf(l.name || "")}">${tileInner}</span>` +
    `<span class="meta"><span class="name">${esc(l.name)}</span>` +
    `<span class="sub2">${sub}</span>${dateHtml}</span>`;
  if (editing) return `<div class="card" data-edit="${l.id}">${inner}</div>`;
  return `<a class="card" href="${esc(l.url)}" target="_blank" rel="noopener">${inner}</a>`;
}

function projCard(p) {
  const letter = esc(Array.from(p.name || "?")[0] || "?");
  const tileInner = `<img src="/assets/logos/${p.slug}" alt="" loading="lazy" onerror="tileFallback(this)">`;
  const inner = `<span class="tile" data-letter="${letter}" style="--h:${hueOf(p.name || "")}">${tileInner}</span>` +
    `<span class="meta"><span class="name">${esc(p.name)}</span>` +
    `<span class="sub2">${esc(p.note || "")}</span></span>`;
  if (editing) return `<a class="card" href="${esc(p.url)}" target="_blank" rel="noopener" title="编辑模式下也能打开">${inner}</a>`;
  return `<a class="card" href="${esc(p.url)}" target="_blank" rel="noopener">${inner}</a>`;
}
function render() {
  renderCatPage($("board"));
}

let expandedCats = null;
let focusScrolled = false;
const focusCat = (() => { try { return new URLSearchParams(location.search).get("cat"); } catch (e) { return null; } })();
function renderCatPage(board) {
  const meta = PROJCAT[catKind];
  const items = regData[catKind] || [];
  const q = $("q").value.trim().toLowerCase();
  const searching = !!q;
  const filtered = items.filter(p => !searching ||
    (p.name + " " + (p.note || "") + " " + p.slug).toLowerCase().includes(q));
  if (expandedCats === null) {
    const gs0 = groupProjects(items, catKind);
    expandedCats = new Set();
    if (focusCat && gs0.some(g => g.cat === focusCat)) expandedCats.add(focusCat);
    else if (gs0.length) expandedCats.add(gs0[0].cat);
  }
  let h = `<div class="crumb"><a href="${basePath}">← 返回微书签</a></div>` +
    `<h2 class="cat" style="font-size:17px;opacity:1;margin-top:10px;">${meta.name}<span class="n">${items.length}</span></h2>` +
    `<div class="intro">${catKind === "html" ? "平台登记的静态网页项目，点卡片在新窗口打开。" : "平台登记的 Python Web 应用项目，点卡片在新窗口打开。"}</div>`;
  if (!editing && !searching && !focusCat) {
    const recent = [...items].filter(p => p.created_at)
      .sort((a, b) => (b.created_at - a.created_at) || ((b.id || 0) - (a.id || 0))).slice(0, 5);
    if (recent.length >= 2) {
      h += `<h2 class="cat">最近添加</h2><div class="grid">` +
        recent.map(p => projCard(p)).join("") + `</div>`;
    }
  }
  const gs = groupProjects(filtered, catKind);
  if (gs.length) {
    h += `<div class="catnav">` + gs.map(g =>
      `<button type="button" class="chip" data-jump="${esc(g.cat)}">${esc(g.cat)}<span class="n">${g.items.length}</span></button>`).join("") + `</div>`;
  }
  if (!items.length) {
    h += `<div class="empty">本类暂无项目。用下方表单发布第一个。</div>`;
  } else if (!gs.length) {
    h += `<div class="empty">没有匹配的项目。</div>`;
  } else {
    h += gs.map(g => {
      const open = searching || expandedCats.has(g.cat);
      return `<h2 class="cat sec" data-sec="${esc(g.cat)}">${esc(g.cat)}<span class="n">${g.items.length}</span><span class="tog">${open ? "收起 ▴" : "展开 ▾"}</span></h2>` +
        `<div class="grid secgrid${open ? "" : " off"}" data-grid="${esc(g.cat)}">` + g.items.map(p => projCard(p)).join("") + `</div>`;
    }).join("");
  }
  board.innerHTML = h;
  board.querySelectorAll("[data-sec]").forEach(el => {
    el.addEventListener("click", () => {
      const c = el.dataset.sec;
      const grid = board.querySelector(`[data-grid="${c}"]`);
      if (!grid) return;
      const nowOff = grid.classList.toggle("off");
      if (nowOff) expandedCats.delete(c); else expandedCats.add(c);
      el.querySelector(".tog").textContent = nowOff ? "展开 ▾" : "收起 ▴";
    });
  });
  board.querySelectorAll("[data-jump]").forEach(el => {
    el.addEventListener("click", () => {
      const c = el.dataset.jump;
      expandedCats.add(c);
      const head = board.querySelector(`[data-sec="${c}"]`);
      const grid = board.querySelector(`[data-grid="${c}"]`);
      if (grid && grid.classList.contains("off")) {
        grid.classList.remove("off");
        const tog = head ? head.querySelector(".tog") : null;
        if (tog) tog.textContent = "收起 ▴";
      }
      if (head) head.scrollIntoView({behavior: "smooth", block: "start"});
    });
  });
  if (focusCat && !focusScrolled) {
    const head = board.querySelector(`[data-sec="${focusCat}"]`);
    if (head) { focusScrolled = true; setTimeout(() => head.scrollIntoView({block: "start"}), 60); }
  }
  $("foot").textContent = `共 ${items.length} 个${meta.name} · 由平台项目登记驱动，登记增减自动同步`;
}

async function loadLinks() {
  const {status, data} = await api("api/links");
  if (status !== 200) { $("board").innerHTML = `<div class="empty">加载失败，请刷新重试。</div>`; return; }
  allLinks = data.links || [];
  const pr = await api("api/projects");
  if (pr.status === 200 && pr.data.projects) regData = pr.data.projects;
  if (catKind) document.title = PROJCAT[catKind].name + " · 微书签";
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

$("expBtn").onclick = async () => {
  const r = await fetch(basePath + "api/links/export");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await r.blob());
  a.download = "links-backup.json";
  document.body.appendChild(a); a.click(); a.remove();
};
$("impFile").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const payload = JSON.parse(await f.text());
    const r = await fetch(basePath + "api/links/import", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const d = await r.json();
    if (!d.ok) throw new Error(d.message || "导入失败");
    $("impMsg").textContent = `导入完成：新增 ${d.added}，跳过 ${d.skipped}（按网址去重）`;
    await loadLinks();
  } catch (err) { $("impMsg").textContent = "导入失败：" + err.message; }
  e.target.value = "";
});

let uploadWarn = false;
function decodeEntities(s) {
  return s.replace(/&(#x?[0-9a-fA-F]+|amp|lt|gt|quot|apos|nbsp);/g, (m, g) => {
    if (g.charAt(0) === "#") {
      const n = (g.charAt(1) === "x" || g.charAt(1) === "X") ? parseInt(g.slice(2), 16) : parseInt(g.slice(1), 10);
      return String.fromCodePoint(n);
    }
    return {amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " "}[g] || m;
  });
}
function titleFromHtml(text, fname) {
  const m = text.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  if (m) {
    const t = decodeEntities(m[1]).trim().replace(/\s+/g, " ");
    if (t) return t.slice(0, 100);
  }
  return fname.replace(/\.[^.]*$/, "").slice(0, 100);
}
$("pf-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  uploadWarn = false;
  if (!f) return;
  let text, badEnc = false;
  const buf = await f.arrayBuffer();
  try { text = new TextDecoder("utf-8", {fatal: true}).decode(buf); }
  catch (err) { badEnc = true; text = new TextDecoder("utf-8").decode(buf); }
  if (text.length > 1048576) {
    $("pf-msg").innerHTML = `<span class="err">\u6587\u4ef6\u5185\u5bb9\u8fc7\u957f\uff1a\u7ea6 ${(text.length / 1048576).toFixed(2)} MB\uff0c\u4e0a\u9650 1 MB\uff08\u4e0e\u7c98\u8d34\u53d1\u5e03\u7684\u4ee3\u7801\u4e0a\u9650\u4e00\u81f4\uff09\uff0c\u8bf7\u7cbe\u7b80\u540e\u91cd\u8bd5\u3002</span>`;
    $("pf-fname").textContent = "";
    e.target.value = "";
    return;
  }
  $("pf-code").value = text;
  $("pf-name").value = titleFromHtml(text, f.name);
  if (!$("pf-slug").value) {
    const s = f.name.replace(/\.[^.]*$/, "").toLowerCase().replace(/[^a-z0-9\-_]+/g, "-").replace(/^-+|-+$/g, "");
    if (/^[a-z0-9]/.test(s)) $("pf-slug").value = s.slice(0, 63);
  }
  $("pf-fname").textContent = f.name + "\uff08" + (f.size / 1024).toFixed(1) + " KB\uff09";
  if (badEnc) {
    uploadWarn = true;
    $("pf-msg").innerHTML = `<span class="err">\u6ce8\u610f\uff1a\u8be5\u6587\u4ef6\u4e0d\u662f UTF-8 \u7f16\u7801\uff0c\u5df2\u6309 UTF-8 \u5f3a\u884c\u8bfb\u53d6\uff0c\u90e8\u5206\u5185\u5bb9\u53ef\u80fd\u4e71\u7801\uff08\u53d1\u5e03\u540e\u9875\u9762\u540c\u6837\u4f1a\u4e71\u7801\uff09\u3002\u5efa\u8bae\u5148\u7528\u7f16\u8f91\u5668\u53e6\u5b58\u4e3a UTF-8 \u518d\u4e0a\u4f20\uff1b\u82e5\u4ecd\u8981\u53d1\u5e03\uff0c\u8bf7\u518d\u70b9\u4e00\u6b21\u300c\u53d1\u5e03\u300d\u786e\u8ba4\u3002</span>`;
  } else {
    $("pf-msg").textContent = "\u6587\u4ef6\u5df2\u8bfb\u5165\u4e0b\u65b9\u4ee3\u7801\u6846\uff0c\u540d\u79f0\u5df2\u81ea\u52a8\u586b\u5199\uff0c\u68c0\u67e5\u65e0\u8bef\u540e\u70b9\u300c\u53d1\u5e03\u300d\u3002";
  }
});
$("pubform").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (uploadWarn) { uploadWarn = false; $("pf-msg").innerHTML = `<span class="err">该文件按 UTF-8 读取后可能含乱码，确认仍要发布请再点一次「发布」。</span>`; return; }
  const body = {name: $("pf-name").value, slug: $("pf-slug").value,
                description: $("pf-desc").value, code: $("pf-code").value};
  $("pf-msg").textContent = "正在登记到平台…";
  const {status, data} = await post("api/projects/" + catKind, body);
  if (status !== 200) { $("pf-msg").innerHTML = `<span class="err">${esc(data.message || "发布失败")}</span>`; return; }
  $("pubform").reset();
  $("pf-fname").textContent = "";
  $("pf-msg").textContent = "已发布，列表已刷新。";
  await loadLinks();
});
window.__afterLoad = () => { $("pubwrap").hidden = false; };

(async () => {
  const {data} = await api("api/session");
  if (!data.authenticated) {
    if (!sessionStorage.getItem("gateRetried")) { sessionStorage.setItem("gateRetried", "1"); location.reload(); return; }
    $("gate-note").style.display = "block"; return;
  }
  sessionStorage.removeItem("gateRetried");
  await loadLinks();
  if (window.__afterLoad) window.__afterLoad();
})();
</script>
</body>
</html>
"""

PAGE_PY = """<!DOCTYPE html>
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
  .editbar { display: none; gap: 8px; align-items: center; padding: 0 2px 10px; flex-wrap: wrap; }
  body.editing .editbar { display: flex; }
  #impMsg { font-size: 12px; opacity: .75; }
  h2.cat { font-size: 14px; margin: 20px 2px 10px; opacity: .75; font-weight: 600;
           display: flex; align-items: baseline; gap: 8px; }
  h2.cat .n { font-size: 12px; opacity: .6; font-weight: 400; }
  .catnav { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 2px 0; }
  .catnav .chip { display: inline-flex; align-items: baseline; gap: 5px; padding: 6px 11px; border-radius: 999px;
          border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
          background: transparent; color: inherit; font-size: 13px; cursor: pointer; font-family: inherit; }
  .catnav .chip .n { font-size: 11px; opacity: .55; }
  h2.cat.sec { cursor: pointer; user-select: none; }
  h2.cat.sec .tog { margin-left: auto; font-size: 12px; opacity: .6; font-weight: 400; }
  .secgrid.off { display: none; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 12px 8px; }
  @media (max-width: 560px) { .grid { grid-template-columns: repeat(4, 1fr); } }
  .card { display: flex; flex-direction: column; align-items: center; gap: 7px; min-width: 0;
          padding: 12px 4px 10px; border-radius: 16px;
          border: 1px solid color-mix(in srgb, currentColor 13%, transparent);
          background: color-mix(in srgb, currentColor 4%, transparent);
          text-decoration: none; color: inherit; position: relative;
          transition: transform .05s ease; }
  .card:active { transform: scale(.97); }
  .tile { flex: 0 0 auto; width: 52px; height: 52px; border-radius: 14px; color: #fff;
          display: flex; align-items: center; justify-content: center; overflow: hidden;
          font-size: 22px; font-weight: 700; background: hsl(var(--h, 210) 52% 44%); }
  .tile img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .meta { width: 100%; min-width: 0; text-align: center; }
  .name { display: block; min-width: 0; font-size: 13.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub2 { display: block; font-size: 11px; height: 15px; line-height: 15px; opacity: .55; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .date2 { display: block; font-size: 10px; height: 13px; line-height: 13px; opacity: .45; }
  body.editing .card { border-style: dashed; cursor: pointer; }
  body.editing .card::after { content: "编辑"; position: absolute; top: 6px; right: 8px;
          font-size: 11px; opacity: .55; }
  .addcard { display: none; flex-direction: column; align-items: center; justify-content: center; gap: 6px; min-height: 118px;
          border-radius: 16px; border: 1.5px dashed color-mix(in srgb, currentColor 30%, transparent);
          font-size: 14px; opacity: .75; cursor: pointer; background: transparent; color: inherit; }
  body.editing .addcard { display: flex; }
  .empty { opacity: .55; font-size: 14px; padding: 26px 2px; }
  .sheet textarea { width: 100%; padding: 10px 12px; border-radius: 10px; font-size: 13px;
           border: 1px solid color-mix(in srgb, currentColor 28%, transparent);
           background: transparent; color: inherit; outline: none; font-family: ui-monospace, Menlo, Consolas, monospace; }
  .sheet textarea:focus { border-color: #2f6fd0; }
  .crumb { font-size: 13px; padding: 2px 2px 0; }
  .crumb a { color: #2f6fd0; text-decoration: none; }
  .intro { font-size: 13px; opacity: .65; padding: 4px 2px 0; }
  body:not(.editing) .delcard { display: none !important; }
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
<div class="editbar">
  <button id="expBtn" class="tbtn" type="button">导出备份</button>
  <label class="tbtn" for="impFile" style="cursor:pointer">导入备份</label>
  <input id="impFile" type="file" accept=".json,application/json" hidden>
  <span id="impMsg"></span>
</div>
<main id="board"></main>
<style>
  #pubwrap { margin-top: 6px; }
  .pubform label { display: block; font-size: 13px; margin: 12px 0 4px; opacity: .8; }
  .pubform input { width: 100%; padding: 10px 12px; border-radius: 10px; font-size: 15px;
           border: 1px solid color-mix(in srgb, currentColor 28%, transparent);
           background: transparent; color: inherit; outline: none; }
  .pubform input:focus { border-color: #2f6fd0; }
  .pubform textarea { width: 100%; padding: 10px 12px; border-radius: 10px; font-size: 13px;
           border: 1px solid color-mix(in srgb, currentColor 28%, transparent);
           background: transparent; color: inherit; outline: none; font-family: ui-monospace, Menlo, Consolas, monospace; }
  .pubform textarea:focus { border-color: #2f6fd0; }
</style>
<section id="pubwrap" hidden>
  <h2 class="cat" style="font-size:17px;opacity:1;">发布 PY 项目</h2>
  <form id="pubform" class="pubform">
    <label>名称</label>
    <input id="pf-name" type="text" required maxlength="100" placeholder="例如 天气看板">
    <label>标识 slug（小写字母 / 数字 / 连字符，定后即网址的一部分）</label>
    <input id="pf-slug" type="text" required maxlength="63" placeholder="例如 weather-board" pattern="[a-z0-9][a-z0-9\-_]*">
    <label>说明（可选，显示为卡片小字）</label>
    <input id="pf-desc" type="text" maxlength="500">
    <label>Python 代码（将保存为 main.py）</label>
    <textarea id="pf-code" rows="10" required spellcheck="false" placeholder="粘贴完整的 Python 脚本代码（单个文件）"></textarea>
    <div class="srow">
      <span style="flex:1"></span>
      <button type="submit" class="sbtn">发布</button>
    </div>
    <p class="msg" id="pf-msg"></p>
  </form>
</section>
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
let regData = {html: [], py: []};
const PROJCAT = {html: {name: "HTML 项目"}, py: {name: "PY 项目"}};
const APPCATS = {
  py: [
    {name: "内容媒体", slugs: ["zhihu-core", "yt-audio", "vidlib", "beastlib"]},
    {name: "文件工具", slugs: ["transfer", "cloudfiles", "nameclean", "filetools"]},
    {name: "记录数据", slugs: ["records", "cards", "docsnew"]},
    {name: "网络工具", slugs: ["subconv", "dnsmap", "speedtest", "openrouter"]},
    {name: "平台效率", slugs: ["links", "clipboard", "burn", "medatlas"]}
  ],
  html: [
    {name: "实用工具", slugs: ["p1791363651", "p1791168459", "p1790594923", "p1789779981", "p1789648409", "p1785485829", "p1785306635", "p1785206347", "p1780063319", "p1778768844", "magnet", "textuni"]},
    {name: "AI 与模型", slugs: ["aicn", "p1788428731", "p1788428729", "p1787576892", "p1785741597", "p1780733162", "p1779539600", "aitest", "ormodel"]},
    {name: "健身与体能", slugs: ["p1783040627", "p1779874258", "p1779872310", "p1779868187", "p1779266591", "p1779202861", "p1779198872", "p1779109601", "p1779020616", "p1778982714", "p1778769593"]},
    {name: "健康与身体", slugs: ["p1790396626", "p1790396609", "p1783259896", "p1781665039", "p1779697722", "p1778981387"]},
    {name: "户外与出行", slugs: ["p1792850445", "p1787039714", "p1781923402", "p1780188079", "p1778984453", "p1778983199", "p1778983019", "p1778981876", "p1778981444", "p1778980516"]},
    {name: "设备与车辆", slugs: ["p1783476971", "p1782215175", "p1779544522", "p1779349290", "p1779001444", "p1778982865", "p1778982461", "p1778981955"]},
    {name: "认知与社会", slugs: ["p1792563415", "p1791946410", "p1790172372", "p1779853529", "p1779778997", "p1779546460", "p1779090887", "p1778991781", "p1778983287", "p1778982505"]},
    {name: "选购依据", slugs: ["vps-buying-guide", "vps-benchmark-compare", "vps-platform-limits"]},
  ]
};
function catOf(p) {
  const cats = p.kind === "py" ? APPCATS.py : APPCATS.html;
  for (const c of cats) if (c.slugs.indexOf(p.slug) >= 0) return c.name;
  return "其他";
}
function catOrder(kind) {
  const cats = kind === "py" ? APPCATS.py : APPCATS.html;
  return cats.map(c => c.name).concat(["其他"]);
}
function groupProjects(items, kind) {
  const by = new Map();
  for (const p of items) {
    const c = catOf(p);
    if (!by.has(c)) by.set(c, []);
    by.get(c).push(p);
  }
  return catOrder(kind).filter(c => by.has(c)).map(c => ({cat: c, items: by.get(c)}));
}

const viewMatch = location.pathname.match(/\/(html|py)\/?$/);
const catKind = viewMatch ? viewMatch[1] : null;
const basePath = location.pathname.replace(/\/(html|py)\/?$/, "").replace(/\/?$/, "/");

async function api(path, options) {
  const url = path.startsWith("/") ? path : basePath + path;
  const res = await fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
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

function slugOf(u) { const m = String(u || "").match(/\/web\/([a-z0-9\-_]+)\//); return m ? m[1] : null; }
function tileFallback(img) { const t = img.parentNode; t.textContent = t.dataset.letter || "?"; }
function dateLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const day = x => x.toDateString();
  if (day(d) === day(now)) return "今天添加";
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (day(d) === day(y)) return "昨天添加";
  return (d.getMonth() + 1) + "-" + String(d.getDate()).padStart(2, "0") + " 添加";
}
function cardHtml(l, withDate) {
  const letter = esc(Array.from(l.name || "?")[0] || "?");
  const sub = esc(l.note || "");
  const dateHtml = withDate ? `<span class="date2">${esc(dateLabel(l.created_at))}</span>` : "";
  const slug = slugOf(l.url);
  const tileInner = slug
    ? `<img src="/assets/logos/${slug}" alt="" loading="lazy" onerror="tileFallback(this)">`
    : letter;
  const inner = `<span class="tile" data-letter="${letter}" style="--h:${hueOf(l.name || "")}">${tileInner}</span>` +
    `<span class="meta"><span class="name">${esc(l.name)}</span>` +
    `<span class="sub2">${sub}</span>${dateHtml}</span>`;
  if (editing) return `<div class="card" data-edit="${l.id}">${inner}</div>`;
  return `<a class="card" href="${esc(l.url)}" target="_blank" rel="noopener">${inner}</a>`;
}

function projCard(p) {
  const letter = esc(Array.from(p.name || "?")[0] || "?");
  const tileInner = `<img src="/assets/logos/${p.slug}" alt="" loading="lazy" onerror="tileFallback(this)">`;
  const inner = `<span class="tile" data-letter="${letter}" style="--h:${hueOf(p.name || "")}">${tileInner}</span>` +
    `<span class="meta"><span class="name">${esc(p.name)}</span>` +
    `<span class="sub2">${esc(p.note || "")}</span></span>`;
  if (editing) return `<a class="card" href="${esc(p.url)}" target="_blank" rel="noopener" title="编辑模式下也能打开">${inner}</a>`;
  return `<a class="card" href="${esc(p.url)}" target="_blank" rel="noopener">${inner}</a>`;
}
function render() {
  renderCatPage($("board"));
}

let expandedCats = null;
let focusScrolled = false;
const focusCat = (() => { try { return new URLSearchParams(location.search).get("cat"); } catch (e) { return null; } })();
function renderCatPage(board) {
  const meta = PROJCAT[catKind];
  const items = regData[catKind] || [];
  const q = $("q").value.trim().toLowerCase();
  const searching = !!q;
  const filtered = items.filter(p => !searching ||
    (p.name + " " + (p.note || "") + " " + p.slug).toLowerCase().includes(q));
  if (expandedCats === null) {
    const gs0 = groupProjects(items, catKind);
    expandedCats = new Set();
    if (focusCat && gs0.some(g => g.cat === focusCat)) expandedCats.add(focusCat);
    else if (gs0.length) expandedCats.add(gs0[0].cat);
  }
  let h = `<div class="crumb"><a href="${basePath}">← 返回微书签</a></div>` +
    `<h2 class="cat" style="font-size:17px;opacity:1;margin-top:10px;">${meta.name}<span class="n">${items.length}</span></h2>` +
    `<div class="intro">${catKind === "html" ? "平台登记的静态网页项目，点卡片在新窗口打开。" : "平台登记的 Python Web 应用项目，点卡片在新窗口打开。"}</div>`;
  if (!editing && !searching && !focusCat) {
    const recent = [...items].filter(p => p.created_at)
      .sort((a, b) => b.created_at - a.created_at).slice(0, 8);
    if (recent.length >= 2) {
      h += `<h2 class="cat">最近添加</h2><div class="grid">` +
        recent.map(p => projCard(p)).join("") + `</div>`;
    }
  }
  const gs = groupProjects(filtered, catKind);
  if (gs.length) {
    h += `<div class="catnav">` + gs.map(g =>
      `<button type="button" class="chip" data-jump="${esc(g.cat)}">${esc(g.cat)}<span class="n">${g.items.length}</span></button>`).join("") + `</div>`;
  }
  if (!items.length) {
    h += `<div class="empty">本类暂无项目。用下方表单发布第一个。</div>`;
  } else if (!gs.length) {
    h += `<div class="empty">没有匹配的项目。</div>`;
  } else {
    h += gs.map(g => {
      const open = searching || expandedCats.has(g.cat);
      return `<h2 class="cat sec" data-sec="${esc(g.cat)}">${esc(g.cat)}<span class="n">${g.items.length}</span><span class="tog">${open ? "收起 ▴" : "展开 ▾"}</span></h2>` +
        `<div class="grid secgrid${open ? "" : " off"}" data-grid="${esc(g.cat)}">` + g.items.map(p => projCard(p)).join("") + `</div>`;
    }).join("");
  }
  board.innerHTML = h;
  board.querySelectorAll("[data-sec]").forEach(el => {
    el.addEventListener("click", () => {
      const c = el.dataset.sec;
      const grid = board.querySelector(`[data-grid="${c}"]`);
      if (!grid) return;
      const nowOff = grid.classList.toggle("off");
      if (nowOff) expandedCats.delete(c); else expandedCats.add(c);
      el.querySelector(".tog").textContent = nowOff ? "展开 ▾" : "收起 ▴";
    });
  });
  board.querySelectorAll("[data-jump]").forEach(el => {
    el.addEventListener("click", () => {
      const c = el.dataset.jump;
      expandedCats.add(c);
      const head = board.querySelector(`[data-sec="${c}"]`);
      const grid = board.querySelector(`[data-grid="${c}"]`);
      if (grid && grid.classList.contains("off")) {
        grid.classList.remove("off");
        const tog = head ? head.querySelector(".tog") : null;
        if (tog) tog.textContent = "收起 ▴";
      }
      if (head) head.scrollIntoView({behavior: "smooth", block: "start"});
    });
  });
  if (focusCat && !focusScrolled) {
    const head = board.querySelector(`[data-sec="${focusCat}"]`);
    if (head) { focusScrolled = true; setTimeout(() => head.scrollIntoView({block: "start"}), 60); }
  }
  $("foot").textContent = `共 ${items.length} 个${meta.name} · 由平台项目登记驱动，登记增减自动同步`;
}

async function loadLinks() {
  const {status, data} = await api("api/links");
  if (status !== 200) { $("board").innerHTML = `<div class="empty">加载失败，请刷新重试。</div>`; return; }
  allLinks = data.links || [];
  const pr = await api("api/projects");
  if (pr.status === 200 && pr.data.projects) regData = pr.data.projects;
  if (catKind) document.title = PROJCAT[catKind].name + " · 微书签";
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

$("expBtn").onclick = async () => {
  const r = await fetch(basePath + "api/links/export");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await r.blob());
  a.download = "links-backup.json";
  document.body.appendChild(a); a.click(); a.remove();
};
$("impFile").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const payload = JSON.parse(await f.text());
    const r = await fetch(basePath + "api/links/import", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const d = await r.json();
    if (!d.ok) throw new Error(d.message || "导入失败");
    $("impMsg").textContent = `导入完成：新增 ${d.added}，跳过 ${d.skipped}（按网址去重）`;
    await loadLinks();
  } catch (err) { $("impMsg").textContent = "导入失败：" + err.message; }
  e.target.value = "";
});

$("pubform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {name: $("pf-name").value, slug: $("pf-slug").value,
                description: $("pf-desc").value, code: $("pf-code").value};
  $("pf-msg").textContent = "正在登记到平台…";
  const {status, data} = await post("api/projects/" + catKind, body);
  if (status !== 200) { $("pf-msg").innerHTML = `<span class="err">${esc(data.message || "发布失败")}</span>`; return; }
  $("pubform").reset();
  $("pf-msg").textContent = "已发布，列表已刷新。";
  await loadLinks();
});
window.__afterLoad = () => { $("pubwrap").hidden = false; };

(async () => {
  const {data} = await api("api/session");
  if (!data.authenticated) {
    if (!sessionStorage.getItem("gateRetried")) { sessionStorage.setItem("gateRetried", "1"); location.reload(); return; }
    $("gate-note").style.display = "block"; return;
  }
  sessionStorage.removeItem("gateRetried");
  await loadLinks();
  if (window.__afterLoad) window.__afterLoad();
})();
</script>
</body>
</html>
"""

PAGES = {None: PAGE_HOME, "html": PAGE_HTML, "py": PAGE_PY}

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
        if path.startswith("/api/projects"):
            if not _authed(environ):
                return _json(start_response, "401 Unauthorized",
                             {"ok": False, "error": "unauthorized", "message": "请先登录。"})
            if path == "/api/projects" and method == "GET":
                reg = _registry_cached()
                return _json(start_response, "200 OK", {"ok": True, "projects": reg})
            pk = path[len("/api/projects/"):]
            if pk in PROJCAT_KINDS:
                if method == "GET":
                    return handle_projects_list(environ, start_response, pk)
                if method == "POST":
                    return handle_projects_create(environ, start_response, pk)
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "not_found", "message": "接口不存在。"})
        if path.startswith("/api/links"):
            if not _authed(environ):
                return _json(start_response, "401 Unauthorized",
                             {"ok": False, "error": "unauthorized", "message": "请先登录。"})
            if path == "/api/links" and method == "GET":
                return handle_links_list(environ, start_response)
            if path == "/api/links" and method == "POST":
                return handle_links_create(environ, start_response)
            if path == "/api/links/export" and method == "GET":
                return handle_links_export(environ, start_response)
            if path == "/api/links/import" and method == "POST":
                return handle_links_import(environ, start_response)
            lid = path[len("/api/links/"):]
            if lid and "/" not in lid:
                if method == "POST":
                    return handle_links_update(environ, start_response, lid)
                if method == "DELETE":
                    return handle_links_delete(environ, start_response, lid)
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "not_found", "message": "接口不存在。"})
        if path in ("/", "/html", "/html/", "/py", "/py/") and method == "GET":
            kind = None
            if path.startswith("/html"):
                kind = "html"
            elif path.startswith("/py"):
                kind = "py"
            body = PAGES[kind].encode("utf-8")
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
