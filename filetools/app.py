# -*- coding: utf-8 -*-
"""文件整理台 (filetools) — App Hub project.

Modelled on nameclean: the local client uploads ONLY relative paths
(indexing metadata for a big tree is slow, so paths are the default and
only payload). On the web page the user picks a function (a tool) and its
parameters; the server then tells the client exactly which extra fields
that function declared in META["needs"] (size / mtime), the client stats
just those and uploads them, the server runs the tool's plan() and hands
back an operation list (from -> to, relative paths only); the client
previews, executes the moves locally, and reports. The server never sees
file contents and never touches a real filesystem.

Functions are pluggable tools: one .py file per function in
DATA_DIR/tools/, exposing META + plan(files, params). Dropping a valid
file there (or uploading it on the web page) makes it selectable with no
code change here — the interface is the contract, the tools are the
user's own core logic. Data lives in DATA_DIR/filetools.db (SQLite) and
DATA_DIR/tools/. The web UI rides the platform gate; the client API uses
this project's own token (write-only) via the Caddy /ft-api entry.
"""
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback

TOOL_VERSION = 1
MAX_PATHS = 50000
FIELD_KEYS = ("size", "mtime")

# ---------------- data dir / db (per-thread connections) ----------------

def _data_dir():
    d = os.environ.get("DATA_DIR")
    if d:
        return d
    link = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if os.path.isdir(link) or os.path.islink(link):
        return link
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime")
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = _data_dir()
DB_PATH = os.path.join(DATA_DIR, "filetools.db")
TOOLS_DIR = os.path.join(DATA_DIR, "tools")
SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_tools")
_local = threading.local()
_init_lock = threading.Lock()
_inited = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',  -- open | fields | ready | done
  file_count INTEGER NOT NULL DEFAULT 0,
  tool_id TEXT NOT NULL DEFAULT '',
  params TEXT NOT NULL DEFAULT '{}',
  needs TEXT NOT NULL DEFAULT '[]',
  plan TEXT NOT NULL DEFAULT '[]',
  plan_count INTEGER NOT NULL DEFAULT 0,
  report TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS batch_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  fields TEXT NOT NULL DEFAULT '{}'
);
"""


def _conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        _local.conn = c
    return c


def _seed_tools():
    os.makedirs(TOOLS_DIR, exist_ok=True)
    if os.path.isdir(SEED_DIR):
        for fn in os.listdir(SEED_DIR):
            if fn.endswith(".py"):
                dst = os.path.join(TOOLS_DIR, fn)
                if not os.path.exists(dst):
                    with open(os.path.join(SEED_DIR, fn), "rb") as f:
                        data = f.read()
                    with open(dst, "wb") as f:
                        f.write(data)


def _init():
    global _inited
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        _conn().executescript(SCHEMA)
        _conn().commit()
        _seed_tools()
        _inited = True


def _meta_get(key, default=""):
    row = _conn().execute("SELECT value FROM meta WHERE key = ?",
                          (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(key, value):
    c = _conn()
    c.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
              (key, value))
    c.commit()


# ---------------- tools (pluggable plan() modules) -------------------------

_tools_lock = threading.Lock()
_tools_cache = {}  # filename -> (mtime, entry dict or error str)


def _validate_meta(meta, fname):
    if not isinstance(meta, dict):
        return "META 不是 dict"
    tid = meta.get("id")
    if not tid or not isinstance(tid, str):
        return "META.id 缺失"
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", tid):
        return "META.id 只能用小写字母/数字/-/_(且须与文件名一致更佳)"
    if not meta.get("name"):
        return "META.name 缺失"
    needs = meta.get("needs") or []
    if not isinstance(needs, list) or any(n not in FIELD_KEYS for n in needs):
        return "META.needs 只能是 %s 的子集" % (list(FIELD_KEYS),)
    params = meta.get("params") or []
    if not isinstance(params, list):
        return "META.params 必须是列表"
    for p in params:
        if not isinstance(p, dict) or not p.get("key") or not p.get("label"):
            return "params 每项需要 key 和 label"
        if p.get("type", "text") not in ("text", "number", "bool", "select"):
            return "params.type 只能是 text/number/bool/select"
        if p.get("type") == "select" and not p.get("options"):
            return "select 参数需要 options"
    return ""


def _load_tools():
    """Scan TOOLS_DIR and (re)import changed tools. Returns list of
    entries: {id, name, desc, needs, params, file, ok, error, module}."""
    _init()
    if TOOLS_DIR not in sys.path:
        sys.path.insert(0, TOOLS_DIR)
    entries = []
    seen_ids = {}
    try:
        fnames = sorted(os.listdir(TOOLS_DIR))
    except OSError:
        fnames = []
    for fn in fnames:
        if not fn.endswith(".py") or fn == "common.py" or fn.startswith("_"):
            continue
        path = os.path.join(TOOLS_DIR, fn)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        with _tools_lock:
            cached = _tools_cache.get(fn)
        if cached and cached[0] == mtime:
            entry = cached[1]
        else:
            entry = {"file": fn, "ok": False, "error": "", "module": None,
                     "id": "", "name": fn, "desc": "", "needs": [],
                     "params": []}
            try:
                spec = importlib.util.spec_from_file_location(
                    "fttool_" + re.sub(r"\W", "_", fn[:-3]), path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                meta = getattr(mod, "META", None)
                err = _validate_meta(meta, fn)
                if err:
                    entry["error"] = err
                elif not callable(getattr(mod, "plan", None)):
                    entry["error"] = "缺少 plan(files, params) 函数"
                elif meta["id"] in seen_ids:
                    entry["error"] = "id 与 %s 重复" % seen_ids[meta["id"]]
                else:
                    entry.update({"ok": True, "id": meta["id"],
                                  "name": meta["name"],
                                  "desc": meta.get("desc", ""),
                                  "needs": list(meta.get("needs") or []),
                                  "params": list(meta.get("params") or []),
                                  "module": mod})
            except Exception:
                entry["error"] = traceback.format_exc(limit=2)
            with _tools_lock:
                _tools_cache[fn] = (mtime, entry)
        if entry.get("id"):
            seen_ids[entry["id"]] = fn
        entries.append(entry)
    return entries


def _tool_by_id(tool_id):
    for e in _load_tools():
        if e["ok"] and e["id"] == tool_id:
            return e
    return None


def _coerce_params(tool, raw):
    out = {}
    for p in tool["params"]:
        key = p["key"]
        val = raw.get(key, p.get("default"))
        t = p.get("type", "text")
        if t == "number":
            try:
                val = float(val)
                if val == int(val):
                    val = int(val)
            except (TypeError, ValueError):
                raise ValueError("参数 %s 需要数字" % p["label"])
            if p.get("min") is not None and val < p["min"]:
                raise ValueError("参数 %s 不能小于 %s" % (p["label"], p["min"]))
            if p.get("max") is not None and val > p["max"]:
                raise ValueError("参数 %s 不能大于 %s" % (p["label"], p["max"]))
        elif t == "bool":
            val = bool(val) and val not in ("false", "0", "")
        elif t == "select":
            if val not in (p.get("options") or []):
                raise ValueError("参数 %s 取值不在选项内" % p["label"])
        else:
            val = "" if val is None else str(val)
        out[key] = val
    return out


# ---------------- batches & plan ------------------------------------------

def _batch_dict(row):
    return {"id": row["id"], "name": row["name"], "status": row["status"],
            "file_count": row["file_count"], "tool_id": row["tool_id"],
            "params": json.loads(row["params"] or "{}"),
            "needs": json.loads(row["needs"] or "[]"),
            "plan_count": row["plan_count"],
            "report": row["report"], "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def _norm_rel(p):
    """Normalize a relative path from a tool op; '' if it escapes."""
    p = str(p).replace("\\", "/").strip()
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return ""
        parts.append(seg)
    return "/".join(parts)


def compute_plan(batch_id):
    """Run the selected tool's plan() over the batch files and store the
    validated operation list. Returns (ok, message)."""
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?",
                  (batch_id,)).fetchone()
    if not b:
        return False, "批次不存在"
    tool = _tool_by_id(b["tool_id"])
    if not tool:
        return False, "工具不存在或无效: %s" % b["tool_id"]
    files = []
    for r in c.execute("SELECT path, fields FROM batch_files "
                       "WHERE batch_id = ? ORDER BY id", (batch_id,)):
        f = {"path": r["path"]}
        f.update(json.loads(r["fields"] or "{}"))
        files.append(f)
    params = json.loads(b["params"] or "{}")
    try:
        ops = tool["module"].plan(files, params)
    except Exception:
        return False, "工具 plan() 报错:\n" + traceback.format_exc(limit=3)
    if ops is None:
        ops = []
    if not isinstance(ops, list):
        return False, "plan() 必须返回列表"
    known = {f["path"] for f in files}
    clean, seen_from = [], set()
    for op in ops:
        if not isinstance(op, dict):
            continue
        src = _norm_rel(op.get("from", ""))
        dst = _norm_rel(op.get("to", ""))
        if not src or not dst or src == dst:
            continue
        if src not in known or src in seen_from:
            continue
        seen_from.add(src)
        clean.append({"from": src, "to": dst})
        if len(clean) > MAX_PATHS:
            return False, "操作数超过上限"
    c.execute("UPDATE batches SET plan = ?, plan_count = ?, status = 'ready', "
              "updated_at = ? WHERE id = ?",
              (json.dumps(clean, ensure_ascii=False), len(clean),
               time.time(), batch_id))
    c.commit()
    return True, "已生成 %d 条操作" % len(clean)


# ---------------- http helpers ---------------------------------------------

def _json_body(environ):
    try:
        n = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        n = 0
    raw = environ["wsgi.input"].read(n) if n > 0 else b""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _reply(start_response, obj, status="200 OK"):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def _token_ok(environ):
    stored = _meta_get("client_token_hash")
    if not stored:
        return False
    given = environ.get("HTTP_X_FILETOOLS_TOKEN", "")
    return bool(given) and \
        hashlib.sha256(given.encode("utf-8")).hexdigest() == stored


# ---------------- client API (token) ----------------------------------------

def api_client_create_batch(environ):
    data = _json_body(environ)
    paths = []
    seen = set()
    for x in (data.get("paths") or []):
        p = _norm_rel(x)
        if p and p not in seen:
            seen.add(p)
            paths.append(p)
    if not paths:
        return {"ok": False, "error": "paths 为空"}, "400 Bad Request"
    if len(paths) > MAX_PATHS:
        return {"ok": False, "error": "单批最多 %d 个文件" % MAX_PATHS}, \
            "400 Bad Request"
    c = _conn()
    now = time.time()
    cur = c.execute(
        "INSERT INTO batches (name, status, file_count, created_at, "
        "updated_at) VALUES (?, 'open', ?, ?, ?)",
        (str(data.get("name") or ""), len(paths), now, now))
    bid = cur.lastrowid
    c.executemany("INSERT INTO batch_files (batch_id, path) VALUES (?, ?)",
                  [(bid, p) for p in paths])
    c.commit()
    return {"ok": True, "batch_id": bid, "file_count": len(paths),
            "status": "open"}, "200 OK"


def api_client_batch(environ, batch_id):
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?",
                  (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    out = {"ok": True, "batch_id": batch_id, "status": b["status"],
           "tool_id": b["tool_id"],
           "needs": json.loads(b["needs"] or "[]")}
    if b["status"] == "ready":
        out["plan"] = json.loads(b["plan"] or "[]")
    return out, "200 OK"


def api_client_fields(environ, batch_id):
    data = _json_body(environ)
    fields = data.get("fields") or {}
    if not isinstance(fields, dict):
        return {"ok": False, "error": "fields 必须是对象"}, "400 Bad Request"
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?",
                  (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    if b["status"] != "fields":
        return {"ok": False, "error": "当前状态不等待字段",
                "status": b["status"]}, "409 Conflict"
    needs = set(json.loads(b["needs"] or "[]"))
    rows = {r["path"]: r for r in c.execute(
        "SELECT id, path, fields FROM batch_files WHERE batch_id = ?",
        (batch_id,))}
    n = 0
    for path, vals in fields.items():
        r = rows.get(path)
        if not r or not isinstance(vals, dict):
            continue
        cur = json.loads(r["fields"] or "{}")
        for k in needs:
            if k in vals:
                try:
                    cur[k] = float(vals[k])
                    if cur[k] == int(cur[k]):
                        cur[k] = int(cur[k])
                except (TypeError, ValueError):
                    pass
        c.execute("UPDATE batch_files SET fields = ? WHERE id = ?",
                  (json.dumps(cur), r["id"]))
        n += 1
    c.commit()
    ok, msg = compute_plan(batch_id)
    if not ok:
        return {"ok": False, "error": msg}, "500 Internal Server Error"
    return {"ok": True, "updated": n, "status": "ready"}, "200 OK"


def api_client_report(environ, batch_id):
    data = _json_body(environ)
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?",
                  (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    report = {"moved": int(data.get("moved") or 0),
              "skipped": int(data.get("skipped") or 0),
              "errors": int(data.get("errors") or 0),
              "at": time.time()}
    c.execute("UPDATE batches SET status = 'done', report = ?, "
              "updated_at = ? WHERE id = ?",
              (json.dumps(report), time.time(), batch_id))
    c.commit()
    return {"ok": True}, "200 OK"


# ---------------- web API (platform gate outside) ---------------------------

def api_session(environ):
    return {"ok": True, "authenticated": True, "auth_via": "platform",
            "token_configured": bool(_meta_get("client_token_hash"))}, "200 OK"


def api_batches(environ):
    rows = _conn().execute("SELECT * FROM batches ORDER BY id DESC "
                           "LIMIT 100").fetchall()
    return {"ok": True, "batches": [_batch_dict(r) for r in rows]}, "200 OK"


def api_batch_detail(environ, batch_id):
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?",
                  (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    out = _batch_dict(b)
    sample = [r["path"] for r in c.execute(
        "SELECT path FROM batch_files WHERE batch_id = ? ORDER BY id "
        "LIMIT 30", (batch_id,))]
    out["sample_paths"] = sample
    plan = json.loads(b["plan"] or "[]")
    out["plan"] = plan[:300]
    out["plan_truncated"] = len(plan) > 300
    return {"ok": True, "batch": out}, "200 OK"


def api_batch_select(environ, batch_id):
    data = _json_body(environ)
    tool = _tool_by_id(str(data.get("tool_id") or ""))
    if not tool:
        return {"ok": False, "error": "工具不存在或无效"}, "400 Bad Request"
    try:
        params = _coerce_params(tool, data.get("params") or {})
    except ValueError as e:
        return {"ok": False, "error": str(e)}, "400 Bad Request"
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?",
                  (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    needs = tool["needs"]
    # fields already supplied in an earlier round do not need re-asking
    if needs:
        row = c.execute("SELECT fields FROM batch_files WHERE batch_id = ? "
                        "LIMIT 1", (batch_id,)).fetchone()
        have = set(json.loads(row["fields"] or "{}")) if row else set()
        needs = [n for n in needs if n not in have]
    c.execute("UPDATE batches SET tool_id = ?, params = ?, needs = ?, "
              "plan = '[]', plan_count = 0, status = ?, updated_at = ? "
              "WHERE id = ?",
              (tool["id"], json.dumps(params, ensure_ascii=False),
               json.dumps(needs), "fields" if needs else "open",
               time.time(), batch_id))
    c.commit()
    if needs:
        return {"ok": True, "status": "fields", "needs": needs,
                "msg": "等待客户端补读字段: %s" % ", ".join(needs)}, "200 OK"
    ok, msg = compute_plan(batch_id)
    if not ok:
        return {"ok": False, "error": msg}, "500 Internal Server Error"
    return {"ok": True, "status": "ready", "msg": msg}, "200 OK"


def api_batch_delete(environ, batch_id):
    c = _conn()
    c.execute("DELETE FROM batch_files WHERE batch_id = ?", (batch_id,))
    c.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    c.commit()
    return {"ok": True}, "200 OK"


def api_tools(environ):
    out = []
    for e in _load_tools():
        out.append({"id": e["id"], "name": e["name"], "desc": e["desc"],
                    "needs": e["needs"], "params": e["params"],
                    "file": e["file"], "ok": e["ok"], "error": e["error"]})
    return {"ok": True, "tools": out}, "200 OK"


def api_tool_upload(environ):
    data = _json_body(environ)
    fn = os.path.basename(str(data.get("filename") or ""))
    content = str(data.get("content") or "")
    if not fn.endswith(".py") or fn in ("common.py",) or fn.startswith("_"):
        return {"ok": False,
                "error": "文件名须为 *.py(common.py 为共享库，请单独说明)"}, \
            "400 Bad Request"
    if len(content.encode("utf-8")) > 256 * 1024:
        return {"ok": False, "error": "文件过大(>256KB)"}, "400 Bad Request"
    try:
        compile(content, fn, "exec")
    except SyntaxError as e:
        return {"ok": False, "error": "语法错误: %s" % e}, "400 Bad Request"
    _init()
    with open(os.path.join(TOOLS_DIR, fn), "w", encoding="utf-8") as f:
        f.write(content)
    with _tools_lock:
        _tools_cache.pop(fn, None)
    tools = [t for t in _load_tools() if t["file"] == fn]
    t = tools[0] if tools else None
    if t and t["ok"]:
        return {"ok": True, "tool": t["id"], "msg": "工具已生效"}, "200 OK"
    return {"ok": False,
            "error": "已保存但校验未过: %s" % (t["error"] if t else "?")}, \
        "400 Bad Request"


def api_tool_delete(environ, fname):
    fn = os.path.basename(fname)
    if fn == "common.py":
        return {"ok": False, "error": "common.py 是共享库，不能删除"}, \
            "400 Bad Request"
    path = os.path.join(TOOLS_DIR, fn)
    if os.path.exists(path):
        os.remove(path)
    with _tools_lock:
        _tools_cache.pop(fn, None)
    return {"ok": True}, "200 OK"


def api_tool_source(environ, fname):
    fn = os.path.basename(fname)
    path = os.path.join(TOOLS_DIR, fn)
    if not os.path.exists(path):
        return {"ok": False, "error": "文件不存在"}, "404 Not Found"
    with open(path, encoding="utf-8") as f:
        return {"ok": True, "file": fn, "content": f.read()}, "200 OK"


def api_set_token(environ):
    data = _json_body(environ)
    tok = str(data.get("token") or "")
    if len(tok) < 8:
        return {"ok": False, "error": "令牌至少 8 位"}, "400 Bad Request"
    _meta_set("client_token_hash",
              hashlib.sha256(tok.encode("utf-8")).hexdigest())
    return {"ok": True}, "200 OK"


# ---------------- routing ----------------------------------------------------

def _route(environ):
    _init()
    path = environ.get("PATH_INFO") or "/"
    method = environ.get("REQUEST_METHOD") or "GET"
    # Direct client-API entry (Caddy /ft-api/* bypasses the panel gate so
    # the headless client can connect; only /api/client/* is reachable
    # through it and the project token is still required).
    if path.startswith("/ft-api/"):
        rest = path[len("/ft-api"):]
        if not rest.startswith("/api/client/"):
            return {"ok": False, "error": "not found"}, "404 Not Found"
        path = rest
    m = re.match(r"^/api/client/batches/(\d+)$", path)
    if path == "/api/client/batches" and method == "POST":
        if not _token_ok(environ):
            return {"ok": False, "error": "令牌无效或未设置"}, \
                "401 Unauthorized"
        return api_client_create_batch(environ)
    if m and method == "GET":
        if not _token_ok(environ):
            return {"ok": False, "error": "令牌无效或未设置"}, \
                "401 Unauthorized"
        return api_client_batch(environ, int(m.group(1)))
    m2 = re.match(r"^/api/client/batches/(\d+)/(fields|report)$", path)
    if m2 and method == "POST":
        if not _token_ok(environ):
            return {"ok": False, "error": "令牌无效或未设置"}, \
                "401 Unauthorized"
        if m2.group(2) == "fields":
            return api_client_fields(environ, int(m2.group(1)))
        return api_client_report(environ, int(m2.group(1)))
    # web API (behind the platform gate)
    if path == "/api/session" and method == "GET":
        return api_session(environ)
    if path == "/api/batches" and method == "GET":
        return api_batches(environ)
    m = re.match(r"^/api/batches/(\d+)$", path)
    if m and method == "GET":
        return api_batch_detail(environ, int(m.group(1)))
    if m and method == "DELETE":
        return api_batch_delete(environ, int(m.group(1)))
    m = re.match(r"^/api/batches/(\d+)/select$", path)
    if m and method == "POST":
        return api_batch_select(environ, int(m.group(1)))
    if path == "/api/tools" and method == "GET":
        return api_tools(environ)
    if path == "/api/tools/upload" and method == "POST":
        return api_tool_upload(environ)
    m = re.match(r"^/api/tools/([^/]+)$", path)
    if m and method == "DELETE":
        return api_tool_delete(environ, m.group(1))
    m = re.match(r"^/api/tools/([^/]+)/source$", path)
    if m and method == "GET":
        return api_tool_source(environ, m.group(1))
    if path == "/api/settings/token" and method == "POST":
        return api_set_token(environ)
    if path.startswith("/api/"):
        return {"ok": False, "error": "not found"}, "404 Not Found"
    return None


def application(environ, start_response):
    if (environ.get("PATH_INFO") or "") == "/download/filetools_client.py":
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "filetools_client.py")
        if os.path.exists(fp):
            with open(fp, "rb") as f:
                body = f.read()
            start_response("200 OK", [
                ("Content-Type", "text/x-python; charset=utf-8"),
                ("Content-Disposition",
                 "attachment; filename=filetools_client.py"),
                ("Content-Length", str(len(body)))])
            return [body]
        return _reply(start_response, {"ok": False, "error": "客户端文件缺失"},
                      "404 Not Found")
    routed = _route(environ)
    if routed is not None:
        obj, status = routed
        return _reply(start_response, obj, status)
    path = environ.get("PATH_INFO") or "/"
    if path in ("/", ""):
        body = PAGE.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]
    return _reply(start_response, {"ok": False, "error": "not found"},
                  "404 Not Found")


app = application


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件整理台</title><style>
body{font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f6f7f9;color:#1c2430}
header{display:flex;align-items:center;gap:10px;padding:12px 16px;background:#fff;border-bottom:1px solid #e3e6eb;position:sticky;top:0;z-index:5}
header h1{font-size:17px;margin:0}
.btn{background:#2563eb;color:#fff;border:0;border-radius:7px;padding:7px 13px;font-size:13px;cursor:pointer}
.btn.sm{padding:4px 10px;font-size:12px}
.btn.ghost{background:#e8ecf3;color:#24304a}
.btn.danger{background:#dc2626}
.btn:disabled{opacity:.5;cursor:default}
main{max-width:980px;margin:0 auto;padding:14px}
.card{background:#fff;border:1px solid #e3e6eb;border-radius:10px;padding:14px;margin-bottom:14px}
.card h3{margin:2px 0 10px;font-size:15px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:6px 8px;border-bottom:1px solid #eef1f5;text-align:left;vertical-align:top}
tr.sel td{background:#eef4ff}
input[type=text],input[type=number],input[type=password],select{padding:6px 8px;border:1px solid #c9d1dd;border-radius:6px;font-size:13px}
label.f{display:block;font-size:12px;color:#5a6a82;margin:8px 0 3px}
.mut{color:#7286a0;font-size:12px}
.badge{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;background:#e8ecf3;color:#445}
.badge.open{background:#fef3c7}.badge.fields{background:#fde68a}
.badge.ready{background:#bbf7d0}.badge.done{background:#dbeafe}
pre{background:#0f172a;color:#dbeafe;padding:10px;border-radius:8px;overflow:auto;font-size:12px;max-height:300px}
.err{color:#dc2626;font-size:13px;white-space:pre-wrap}
.ok{color:#16a34a;font-size:13px}
code{background:#eef1f5;padding:1px 5px;border-radius:4px;font-size:12px}
.planbox{max-height:320px;overflow:auto;border:1px solid #eef1f5;border-radius:8px}
</style></head><body>
<header><h1>文件整理台</h1>
<a class="btn sm" href="download/filetools_client.py" style="text-decoration:none;margin-left:auto">下载客户端</a>
</header><main>
<div class="card"><h3>批次</h3><div id="batches"><span class="mut">加载中…</span></div></div>
<div class="card" id="detailCard" style="display:none"><h3>批次详情</h3><div id="detail"></div></div>
<div class="card"><h3>功能工具</h3><div id="tools"></div>
<div style="margin-top:10px"><label class="f">上传新工具(.py，含 META + plan)</label>
<input type="file" id="toolFile" accept=".py"> <button class="btn sm" onclick="uploadTool()">上传并生效</button>
<span id="toolMsg"></span></div>
<div id="toolSource"></div></div>
<div class="card"><h3>设置</h3>
<label class="f">客户端令牌（只写入、不可读回；客户端用 X-Filetools-Token 携带）</label>
<input type="password" id="tok" size="34" placeholder="至少 8 位"> <button class="btn sm" onclick="setToken()">保存令牌</button>
<span id="tokState" class="mut"></span>
<p class="mut">流程：客户端扫描文件夹只上传相对路径 → 在此选功能并生成任务（功能若声明 needs 字段，服务器会让客户端补读后自动生成）→ 客户端拉取操作清单、预览、执行并回传。工具 = 数据目录 tools/ 下的 .py 文件，新增工具上传即生效，无需改本项目。</p>
</div>
</main><script>
let curBatch = null, toolsCache = [], batchSig = "";
async function jget(u){const r = await fetch(u); return r.json();}
async function jpost(u, body){const r = await fetch(u, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body||{})}); return r.json();}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function fmtT(ts){return ts ? new Date(ts*1000).toLocaleString("zh-CN",{hour12:false}) : "";}
async function loadBatches(){
  const d = await jget("api/batches"); if(!d.ok) return;
  const sig = JSON.stringify(d.batches.map(b=>[b.id,b.status,b.plan_count,b.tool_id]));
  if(sig === batchSig) return; batchSig = sig;
  let h = "<table><tr><th>ID</th><th>名称</th><th>文件</th><th>状态</th><th>功能</th><th>操作数</th><th>创建时间</th><th></th></tr>";
  for(const b of d.batches){
    h += "<tr class='"+(curBatch===b.id?"sel":"")+"'><td>"+b.id+"</td><td>"+esc(b.name)+"</td><td>"+b.file_count+"</td>"
      + "<td><span class='badge "+b.status+"'>"+b.status+"</span></td><td>"+esc(b.tool_id||"—")+"</td><td>"+(b.plan_count||"")+"</td>"
      + "<td class='mut'>"+fmtT(b.created_at)+"</td>"
      + "<td><button class='btn sm ghost' onclick='showBatch("+b.id+")'>打开</button> "
      + "<button class='btn sm danger' onclick='delBatch("+b.id+")'>删</button></td></tr>";
  }
  document.getElementById("batches").innerHTML = h || "<span class='mut'>暂无批次，等待客户端上传</span>";
  if(curBatch) showBatch(curBatch, true);
}
async function delBatch(id){ if(!confirm("删除批次 "+id+"？")) return; await fetch("api/batches/"+id, {method:"DELETE"}); if(curBatch===id){curBatch=null; document.getElementById("detailCard").style.display="none";} batchSig=""; loadBatches(); }
function paramInput(p, val){
  const v = val!==undefined ? val : (p.default!==undefined ? p.default : "");
  if(p.type==="bool") return "<select id='pp_"+p.key+"'><option value='1'"+(v?" selected":"")+">是</option><option value='0'"+(v?"":" selected")+">否</option></select>";
  if(p.type==="select") return "<select id='pp_"+p.key+"'>"+(p.options||[]).map(o=>"<option"+(o===v?" selected":"")+">"+esc(o)+"</option>").join("")+"</select>";
  if(p.type==="number") return "<input type='number' id='pp_"+p.key+"' value='"+esc(v)+"' style='width:130px'>";
  return "<input type='text' id='pp_"+p.key+"' value='"+esc(v)+"' size='28'>";
}
async function showBatch(id, keep){
  curBatch = id;
  const d = await jget("api/batches/"+id); if(!d.ok) return;
  const b = d.batch;
  document.getElementById("detailCard").style.display = "";
  let h = "<div class='mut'>批次 #"+b.id+" · "+esc(b.name)+" · "+b.file_count+" 个文件 · 状态 <b>"+b.status+"</b>"
        + (b.report ? " · 执行报告 "+esc(b.report) : "") + "</div>";
  h += "<label class='f'>选择功能</label><select id='toolSel' onchange='renderParams()'>";
  for(const t of toolsCache){ if(t.ok) h += "<option value='"+t.id+"'"+(b.tool_id===t.id?" selected":"")+">"+esc(t.name)+"（"+t.id+"）</option>"; }
  h += "</select> <span id='toolNeeds' class='mut'></span><div id='paramsBox'></div>";
  h += "<div style='margin-top:10px'><button class='btn' onclick='doSelect("+b.id+")'>生成任务</button> <span id='selMsg'></span></div>";
  if(b.plan && b.plan.length){
    h += "<h4 style='margin:14px 0 6px'>操作清单（共 "+b.plan_count+" 条"+(b.plan_truncated?"，仅显示前 300":"")+"）</h4><div class='planbox'><table>";
    for(const op of b.plan) h += "<tr><td>"+esc(op.from)+"</td><td>→</td><td>"+esc(op.to)+"</td></tr>";
    h += "</table></div>";
  }
  if(b.sample_paths && b.sample_paths.length){
    h += "<details style='margin-top:10px'><summary class='mut'>文件路径样本（前 30）</summary><pre>"+esc(b.sample_paths.join("\n"))+"</pre></details>";
  }
  document.getElementById("detail").innerHTML = h;
  renderParams(b.params||{});
}
function renderParams(saved){
  const sel = document.getElementById("toolSel"); if(!sel) return;
  const t = toolsCache.find(x=>x.id===sel.value);
  const box = document.getElementById("paramsBox");
  document.getElementById("toolNeeds").textContent = t ? (t.desc||"") + (t.needs && t.needs.length ? " · 需要字段: "+t.needs.join(", ") : " · 只需路径") : "";
  if(!t || !t.params || !t.params.length){ box.innerHTML = ""; return; }
  box.innerHTML = t.params.map(p=>"<label class='f'>"+esc(p.label)+"</label>"+paramInput(p, saved?saved[p.key]:undefined)).join("");
}
async function doSelect(id){
  const sel = document.getElementById("toolSel");
  const t = toolsCache.find(x=>x.id===sel.value);
  const params = {};
  if(t) for(const p of t.params){ const el = document.getElementById("pp_"+p.key); if(!el) continue;
    params[p.key] = p.type==="bool" ? (el.value==="1") : (p.type==="number" ? Number(el.value) : el.value); }
  const d = await jpost("api/batches/"+id+"/select", {tool_id: sel.value, params});
  const m = document.getElementById("selMsg");
  m.className = d.ok ? "ok" : "err"; m.textContent = d.ok ? (d.msg||"已生成") : (d.error||"失败");
  batchSig=""; loadBatches();
}
async function loadTools(){
  const d = await jget("api/tools"); if(!d.ok) return;
  toolsCache = d.tools;
  let h = "<table><tr><th>功能</th><th>id</th><th>文件</th><th>需要字段</th><th>状态</th><th></th></tr>";
  for(const t of d.tools){
    h += "<tr><td>"+esc(t.name)+"</td><td><code>"+esc(t.id||"—")+"</code></td><td>"+esc(t.file)+"</td>"
      + "<td>"+esc((t.needs||[]).join(", ")||"路径")+"</td>"
      + "<td>"+(t.ok ? "<span class='ok'>可用</span>" : "<span class='err'>"+esc((t.error||"").slice(0,120))+"</span>")+"</td>"
      + "<td><button class='btn sm ghost' onclick='viewTool(\""+esc(t.file)+"\")'>源码</button> "
      + "<button class='btn sm danger' onclick='delTool(\""+esc(t.file)+"\")'>删</button></td></tr>";
  }
  document.getElementById("tools").innerHTML = h + "</table>";
}
async function viewTool(fn){
  const d = await jget("api/tools/"+encodeURIComponent(fn)+"/source");
  document.getElementById("toolSource").innerHTML = d.ok ? "<h4 style='margin:12px 0 6px'>"+esc(fn)+"</h4><pre>"+esc(d.content)+"</pre>" : "<span class='err'>"+esc(d.error||"")+"</span>";
}
async function delTool(fn){ if(!confirm("删除工具 "+fn+"？")) return; await fetch("api/tools/"+encodeURIComponent(fn), {method:"DELETE"}); loadTools(); }
function uploadTool(){
  const f = document.getElementById("toolFile").files[0];
  const m = document.getElementById("toolMsg");
  if(!f){ m.textContent = "先选文件"; return; }
  const rd = new FileReader();
  rd.onload = async () => { const d = await jpost("api/tools/upload", {filename: f.name, content: rd.result});
    m.className = d.ok ? "ok" : "err"; m.textContent = d.ok ? d.msg : d.error; loadTools(); };
  rd.readAsText(f);
}
async function setToken(){
  const v = document.getElementById("tok").value;
  const d = await jpost("api/settings/token", {token: v});
  document.getElementById("tokState").textContent = d.ok ? "已保存" : (d.error||"失败");
  if(d.ok) document.getElementById("tok").value = "";
}
async function init(){
  const s = await jget("api/session");
  document.getElementById("tokState").textContent = s.token_configured ? "令牌已设置" : "令牌未设置";
  await loadTools(); await loadBatches();
  setInterval(()=>{ loadBatches(); }, 4000);
}
init();
</script></body></html>
"""
