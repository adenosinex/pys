# -*- coding: utf-8 -*-
"""文件整理台 (filetools) v2 — App Hub project.

Flow (nameclean-shaped): the local client uploads ONLY relative paths;
each batch appears as a card on the web page with one button per
function — ONE click runs it. If the function declares extra inputs in
its signature (size / mtime), the server asks the client to stat exactly
those fields and upload them, then the plan is computed and the client
executes the moves locally (no overwrite, undo log, report back). The
server never sees file contents.

A function is ONE pasted function — all peripheral machinery lives in
this project, the function only carries the core rule:

    def transform(path, size=None):
        \"\"\"按大小分夹\"\"\"
        ...

Contract (see SPEC_TEXT in the page help / the spec document):
  - the code defines `transform`; its first parameter is `path`
    (a relative path string);
  - return the new relative path, or None / "" / the same path to
    leave the file alone;
  - extra inputs are declared BY PARAMETER NAME: size, mtime
    (fetched by the client on demand), index, total (batch context,
    supplied by the server). Missing field values arrive as None;
  - the display name is the docstring's first line;
  - `common` helpers are pre-imported in the function's namespace.
Functions live as .py files in DATA_DIR/tools/; saving one on the web
page validates it (compile + signature) and makes it usable at once.
Data: DATA_DIR/filetools.db + DATA_DIR/tools/. The web UI rides the
platform gate; the client API uses this project's own write-only token
via the Caddy /ft-api entry.
"""
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback

MAX_PATHS = 50000
FIELD_PARAMS = ("size", "mtime")     # supplied by the client on demand
CTX_PARAMS = ("index", "total")      # supplied by the server
SEED_STAMP = "2"

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
SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "seed_tools")
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
  plan_note TEXT NOT NULL DEFAULT '',
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
    stamp = os.path.join(TOOLS_DIR, ".seed_version")
    cur = ""
    if os.path.exists(stamp):
        with open(stamp) as f:
            cur = f.read().strip()
    if cur == SEED_STAMP:
        return
    if os.path.isdir(SEED_DIR):
        for fn in os.listdir(SEED_DIR):
            if fn.endswith(".py"):
                with open(os.path.join(SEED_DIR, fn), "rb") as f:
                    data = f.read()
                with open(os.path.join(TOOLS_DIR, fn), "wb") as f:
                    f.write(data)
    with open(stamp, "w") as f:
        f.write(SEED_STAMP)


def _init():
    global _inited
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        c = _conn()
        c.executescript(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(batches)")]
        if "plan_note" not in cols:
            c.execute("ALTER TABLE batches ADD COLUMN plan_note "
                      "TEXT NOT NULL DEFAULT ''")
        c.commit()
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


# ---------------- tools: one transform() per file ---------------------------

_tools_lock = threading.Lock()
_tools_cache = {}   # filename -> (mtime, entry)
_common_cache = {"mtime": None, "module": None}


def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", str(s))]


def _load_common():
    path = os.path.join(TOOLS_DIR, "common.py")
    if not os.path.exists(path):
        return None
    mtime = os.path.getmtime(path)
    if _common_cache["mtime"] == mtime:
        return _common_cache["module"]
    spec = importlib.util.spec_from_file_location("ft_common", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return _common_cache["module"]
    _common_cache.update({"mtime": mtime, "module": mod})
    return mod


def _signature_of(func):
    """Validate transform's signature. Returns (argnames, needs, error)."""
    try:
        params = [p for p in inspect.signature(func).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        return None, None, "无法读取函数签名"
    if not params or params[0].name != "path":
        return None, None, "第一个参数必须是 path"
    argnames = [p.name for p in params]
    needs = []
    for name in argnames[1:]:
        if name in FIELD_PARAMS:
            needs.append(name)
        elif name in CTX_PARAMS:
            pass
        else:
            return None, None, ("不支持的参数 %s(只允许 size / mtime / "
                                "index / total)" % name)
    return argnames, needs, ""


def _load_one(fn):
    path = os.path.join(TOOLS_DIR, fn)
    entry = {"file": fn, "id": fn[:-3], "ok": False, "error": "",
             "name": fn[:-3], "desc": "", "needs": [], "argnames": [],
             "func": None}
    common = _load_common()
    ns = {"__name__": "fttool_" + re.sub(r"\W", "_", fn[:-3]),
          "common": common}
    try:
        with open(path, encoding="utf-8") as f:
            code = f.read()
        exec(compile(code, fn, "exec"), ns)
    except Exception:
        entry["error"] = traceback.format_exc(limit=2)
        return entry
    func = ns.get("transform")
    if not callable(func):
        entry["error"] = "代码里没有定义 transform 函数"
        return entry
    argnames, needs, err = _signature_of(func)
    if err:
        entry["error"] = err
        return entry
    doc = (inspect.getdoc(func) or "").strip()
    lines = doc.split("\n") if doc else []
    entry.update({"ok": True, "func": func, "argnames": argnames,
                  "needs": needs,
                  "name": lines[0].strip() if lines else fn[:-3],
                  "desc": "\n".join(lines[1:]).strip()})
    return entry


def _load_tools():
    _init()
    if TOOLS_DIR not in sys.path:
        sys.path.insert(0, TOOLS_DIR)
    entries = []
    try:
        fnames = sorted(os.listdir(TOOLS_DIR))
    except OSError:
        fnames = []
    for fn in fnames:
        if not fn.endswith(".py") or fn == "common.py" or fn.startswith("_"):
            continue
        try:
            mtime = os.path.getmtime(os.path.join(TOOLS_DIR, fn))
        except OSError:
            continue
        with _tools_lock:
            cached = _tools_cache.get(fn)
        if cached and cached[0] == mtime:
            entry = cached[1]
        else:
            entry = _load_one(fn)
            with _tools_lock:
                _tools_cache[fn] = (mtime, entry)
        entries.append(entry)
    return entries


def _tool_by_id(tool_id):
    for e in _load_tools():
        if e["ok"] and e["id"] == tool_id:
            return e
    return None


# ---------------- batches & plan ------------------------------------------

def _batch_dict(row):
    return {"id": row["id"], "name": row["name"], "status": row["status"],
            "file_count": row["file_count"], "tool_id": row["tool_id"],
            "needs": json.loads(row["needs"] or "[]"),
            "plan_count": row["plan_count"],
            "plan_note": row["plan_note"] if "plan_note" in row.keys()
            else "",
            "report": row["report"], "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def _norm_rel(p):
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
    """Run transform() over every file of the batch (natural-sort order)
    and store the validated operation list. Returns (ok, message)."""
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?",
                  (batch_id,)).fetchone()
    if not b:
        return False, "批次不存在"
    tool = _tool_by_id(b["tool_id"])
    if not tool:
        return False, "功能不存在或无效: %s" % b["tool_id"]
    files = []
    for r in c.execute("SELECT path, fields FROM batch_files "
                       "WHERE batch_id = ?", (batch_id,)):
        f = {"path": r["path"]}
        f.update(json.loads(r["fields"] or "{}"))
        files.append(f)
    files.sort(key=lambda f: _natural_key(f["path"]))
    total = len(files)
    ops, errors, seen_from = [], 0, set()
    for i, f in enumerate(files):
        kwargs = {}
        for name in tool["argnames"][1:]:
            if name == "index":
                kwargs["index"] = i
            elif name == "total":
                kwargs["total"] = total
            else:  # size / mtime: None when the client could not read it
                kwargs[name] = f.get(name)
        try:
            newp = tool["func"](f["path"], **kwargs)
        except Exception:
            errors += 1
            continue
        if not newp or not isinstance(newp, str):
            continue
        dst = _norm_rel(newp)
        if not dst or dst == f["path"] or f["path"] in seen_from:
            continue
        seen_from.add(f["path"])
        ops.append({"from": f["path"], "to": dst})
    note = ""
    if errors:
        note = "%d 个文件处理出错已跳过" % errors
    c.execute("UPDATE batches SET plan = ?, plan_count = ?, plan_note = ?, "
              "status = 'ready', updated_at = ? WHERE id = ?",
              (json.dumps(ops, ensure_ascii=False), len(ops), note,
               time.time(), batch_id))
    c.commit()
    msg = "已生成 %d 条操作" % len(ops)
    if note:
        msg += "(" + note + ")"
    return True, msg


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
    paths, seen = [], set()
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
    plan = json.loads(b["plan"] or "[]")
    out["plan"] = plan[:300]
    out["plan_truncated"] = len(plan) > 300
    return {"ok": True, "batch": out}, "200 OK"


def api_batch_run(environ, batch_id):
    """One click: attach a function to the batch; either the plan is
    computed at once (path/index/total only) or the client is asked for
    the declared fields first."""
    data = _json_body(environ)
    tool = _tool_by_id(str(data.get("tool_id") or ""))
    if not tool:
        return {"ok": False, "error": "功能不存在或无效"}, "400 Bad Request"
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?",
                  (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    needs = list(tool["needs"])
    if needs:
        row = c.execute("SELECT fields FROM batch_files WHERE batch_id = ? "
                        "LIMIT 1", (batch_id,)).fetchone()
        have = set(json.loads(row["fields"] or "{}")) if row else set()
        needs = [n for n in needs if n not in have]
    c.execute("UPDATE batches SET tool_id = ?, needs = ?, plan = '[]', "
              "plan_count = 0, plan_note = '', status = ?, updated_at = ? "
              "WHERE id = ?",
              (tool["id"], json.dumps(needs),
               "fields" if needs else "open", time.time(), batch_id))
    c.commit()
    if needs:
        return {"ok": True, "status": "fields", "needs": needs,
                "msg": "已下发：客户端补读 %s 后自动生成" % ", ".join(needs)}, \
            "200 OK"
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
                    "needs": e["needs"], "file": e["file"], "ok": e["ok"],
                    "error": e["error"]})
    return {"ok": True, "tools": out}, "200 OK"


def api_tool_upload(environ):
    data = _json_body(environ)
    fn = os.path.basename(str(data.get("filename") or "")).strip()
    content = str(data.get("content") or "")
    if not re.match(r"^[a-z0-9][a-z0-9_-]*\.py$", fn) or fn == "common.py":
        return {"ok": False,
                "error": "文件名须为小写英文/数字 .py(common.py 是共享库)"}, \
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
    entry = _load_one(fn)
    if entry["ok"]:
        return {"ok": True, "tool": entry["id"],
                "msg": "功能已生效: %s" % entry["name"]}, "200 OK"
    return {"ok": False,
            "error": "已保存但校验未过: %s" % entry["error"]}, \
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
    if tok == "":
        _meta_set("client_token_hash", "")
        return {"ok": True, "cleared": True}, "200 OK"
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
    if path == "/api/session" and method == "GET":
        return api_session(environ)
    if path == "/api/batches" and method == "GET":
        return api_batches(environ)
    m = re.match(r"^/api/batches/(\d+)$", path)
    if m and method == "GET":
        return api_batch_detail(environ, int(m.group(1)))
    if m and method == "DELETE":
        return api_batch_delete(environ, int(m.group(1)))
    m = re.match(r"^/api/batches/(\d+)/run$", path)
    if m and method == "POST":
        return api_batch_run(environ, int(m.group(1)))
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
:root{--bg:#f3f5f9;--card:#fff;--ink:#16233a;--mut:#71809a;--line:#e5eaf2;--acc:#4f46e5;--acc2:#7c6cf0;--ok:#0e9f6e;--warn:#d97706;--err:#dc2626;--chip:#eef1fb}
@media (prefers-color-scheme: dark){:root{--bg:#0d1320;--card:#16203a;--ink:#e6ecf7;--mut:#8fa1bd;--line:#24314f;--chip:#1d2a4a}}
*{box-sizing:border-box}
body{font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:var(--bg);color:var(--ink)}
header{display:flex;align-items:center;gap:10px;padding:14px 20px;position:sticky;top:0;z-index:6;background:linear-gradient(90deg,var(--acc),var(--acc2));color:#fff;box-shadow:0 2px 14px rgba(60,50,180,.25)}
header h1{font-size:18px;margin:0;font-weight:650;letter-spacing:.5px}
header.sp{flex:1}
.hbtn{background:rgba(255,255,255,.16);color:#fff;border:1px solid rgba(255,255,255,.35);border-radius:999px;padding:6px 14px;font-size:13px;cursor:pointer;text-decoration:none}
.hbtn:hover{background:rgba(255,255,255,.28)}
main{max-width:1020px;margin:0 auto;padding:18px 14px 40px}
h2.sec{font-size:15px;margin:20px 2px 10px;display:flex;align-items:center;gap:8px}
h2.sec.r{margin-left:auto;font-size:12px;color:var(--mut);font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:12px}
.fcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px 14px;box-shadow:0 1px 4px rgba(20,30,60,.05)}
.fcard.nm{font-weight:650;font-size:14.5px}
.fcard.fl{color:var(--mut);font-size:11.5px;margin-top:2px}
.fcard.ds{color:var(--mut);font-size:12px;margin-top:6px;white-space:pre-wrap}
.fcard.ops{margin-top:9px;display:flex;gap:6px;align-items:center}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;background:var(--chip);color:var(--acc)}
.badge.need{background:#fdf0e0;color:var(--warn)}
.link{background:none;border:0;color:var(--acc);cursor:pointer;font-size:12px;padding:2px 4px}
.link.red{color:var(--err)}
.bcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(20,30,60,.05)}
.bcard.top{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.bcard.nm{font-weight:650;font-size:15px}
.meta{color:var(--mut);font-size:12px}
.chip{display:inline-block;padding:2px 10px;border-radius:999px;font-size:11.5px;font-weight:600}
.chip.open{background:#eef1fb;color:var(--acc)}
.chip.fields{background:#fdf0e0;color:var(--warn)}
.chip.ready{background:#e2f6ec;color:var(--ok)}
.chip.done{background:#e8ecf3;color:var(--mut)}
.del{margin-left:auto;background:none;border:0;color:var(--mut);font-size:16px;cursor:pointer;line-height:1}
.del:hover{color:var(--err)}
.frow{display:flex;flex-wrap:wrap;gap:8px;margin-top:11px}
.fbtn{border:1.5px solid var(--acc);color:var(--acc);background:transparent;border-radius:999px;padding:7px 16px;font-size:13.5px;font-weight:600;cursor:pointer;transition:all.15s}
.fbtn:hover{background:var(--acc);color:#fff;transform:translateY(-1px);box-shadow:0 4px 12px rgba(79,70,229,.3)}
.fbtn small{font-weight:400;opacity:.75;font-size:11px;margin-left:4px}
.status-line{margin-top:10px;font-size:13px;color:var(--mut)}
.status-line b{color:var(--ink)}
.dotpulse::after{content:"";animation:dp 1.2s infinite}
@keyframes dp{0%{content:""}25%{content:"."}50%{content:".."}75%{content:"..."}}
.planbox{max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:10px;margin-top:8px}
.planbox table{width:100%;border-collapse:collapse;font-size:12.5px}
.planbox td{padding:5px 9px;border-bottom:1px solid var(--line)}
.btn{background:var(--acc);color:#fff;border:0;border-radius:999px;padding:8px 18px;font-size:13.5px;font-weight:600;cursor:pointer}
.btn.ghost{background:transparent;color:var(--acc);border:1.5px solid var(--acc)}
.btn:disabled{opacity:.5}
input[type=text],input[type=password]{padding:8px 11px;border:1px solid var(--line);border-radius:9px;font-size:13.5px;background:var(--card);color:var(--ink)}
textarea{width:100%;min-height:210px;padding:11px;border:1px solid var(--line);border-radius:10px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;background:var(--card);color:var(--ink)}
.modal{position:fixed;inset:0;background:rgba(10,15,30,.5);display:none;align-items:center;justify-content:center;z-index:20;padding:14px}
.modal.show{display:flex}
.mbox{background:var(--card);border-radius:16px;max-width:640px;width:100%;max-height:92vh;overflow:auto;padding:20px}
.mbox h3{margin:0 0 12px;font-size:16px}
.mbox label{display:block;font-size:12.5px;color:var(--mut);margin:10px 0 4px}
pre.spec{background:#0f172a;color:#c9d6f2;padding:13px;border-radius:10px;font-size:12px;overflow:auto;white-space:pre-wrap;line-height:1.65}
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);background:#16233a;color:#fff;padding:10px 20px;border-radius:999px;font-size:13.5px;opacity:0;pointer-events:none;transition:all.25s;z-index:30;max-width:92vw;text-align:center}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.err{background:var(--err)}
.err{color:var(--err);font-size:12.5px;white-space:pre-wrap}
.empty{color:var(--mut);font-size:13.5px;padding:16px 4px}
.setrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px}
</style></head><body>
<header><h1>文件整理台</h1><span class="sp"></span>
<button class="hbtn" onclick="openHelp()">编写规范</button>
<a class="hbtn" href="download/filetools_client.py">下载客户端</a>
</header><main>
<h2 class="sec">功能库<span class="r">点批次卡片上的功能按钮即可执行</span></h2>
<div class="grid" id="funcs"></div>
<div style="margin-top:12px"><button class="btn ghost" onclick="openNew()">＋ 新功能（粘贴代码）</button>
<label class="link" style="cursor:pointer">或上传.py<input type="file" id="upFile" accept=".py" hidden onchange="uploadFile(this)"></label></div>
<h2 class="sec">批次</h2>
<div id="batches"><div class="empty">加载中…</div></div>
<h2 class="sec">设置</h2>
<div class="fcard" style="max-width:640px"><div class="nm" style="font-size:13.5px">客户端令牌</div>
<div class="setrow"><input type="password" id="tok" size="30" placeholder="至少 8 位（只写入、不可读回）">
<button class="btn" onclick="setToken()">保存</button><span class="meta" id="tokState"></span></div>
<div class="meta" style="margin-top:6px">流程：客户端扫文件夹只传路径 → 批次出现在上方 → 点功能按钮 → 若功能声明了 size/mtime，客户端自动补读 → 清单生成后客户端执行并回传。</div></div>
</main>

<div class="modal" id="newModal"><div class="mbox">
<h3>新功能</h3>
<label>文件名（小写英文，如 by_size.py；同名即覆盖更新）</label>
<input type="text" id="nf-name" size="30" placeholder="by_size.py">
<label>核心代码（只需一个 transform 函数，外围项目全包）</label>
<textarea id="nf-code" spellcheck="false"></textarea>
<div style="margin-top:12px;display:flex;gap:8px;align-items:center">
<button class="btn" onclick="saveTool()">保存并生效</button>
<button class="btn ghost" onclick="closeNew()">取消</button>
<span id="nf-msg"></span></div>
</div></div>

<div class="modal" id="helpModal"><div class="mbox">
<h3>功能编写规范</h3>
<pre class="spec" id="specPre"></pre>
<div style="margin-top:12px"><button class="btn ghost" onclick="closeHelp()">关闭</button></div>
</div></div>

<div class="modal" id="srcModal"><div class="mbox">
<h3 id="srcTitle">源码</h3><pre class="spec" id="srcPre"></pre>
<div style="margin-top:12px"><button class="btn ghost" onclick="document.getElementById('srcModal').classList.remove('show')">关闭</button></div>
</div></div>

<div id="toast"></div>
<script>
let tools = [], batches = [], sig = "", modalOpen = false;
const SPEC = `
粘贴一段 Python 代码、起个文件名保存即生效。外围（扫描、传参、生成
清单、执行、回滚）全部由项目承担，代码只写核心规则。


必须定义函数 transform，第一个参数固定为 path —— 相对路径字符串，
如 "子目录/视频.mp4"。按自然排序逐个文件调用。


返回新的相对路径字符串；返回 None、空字符串或原路径 = 这个文件不动。
如 return "大文件/" + 文件名。


size 文件大小（字节，整数）—— 客户端会自动补读后再执行
mtime 修改时间（Unix 秒）—— 同上
index 该文件在整批中的序号（从 0 开始）—— 服务器直接提供
total 整批文件总数 —— 服务器直接提供
字段读不到时传入 None，代码里自己判空。参数名写错或写了表外参数，
保存时会被拒绝并告诉你原因。


函数文档字符串的第一行就是按钮上显示的名字，如：
def transform(path, size):
'''按大小分夹'''
没有文档字符串时显示文件名。


代码里已预置 common 模块，可直接用：
common.basename(path) 取文件名 common.ext_of(name) 取扩展名
common.size_bucket(size) 大小分档 common.safe_seg(s) 清洗路径段
也可以 import 标准库模块。


只做纯计算：不要读写文件、不要联网、不要 import os/sys 去碰磁盘。
函数只输入路径、输出路径；对某个文件抛异常只会跳过该文件并计数，
不影响其他文件。


集中到根目录：
def transform(path):
'''集中到根目录'''
if "/" in path:
return path.rsplit("/", 1)[-1]

按大小分夹（声明 size）：
def transform(path, size):
'''按大小分夹'''
if size is None: return None
name = path.rsplit("/", 1)[-1]
return ("大于500M/" if size > 500*1024*1024 else "小于500M/") + name

每组 2 个（声明 index）：
def transform(path, index):
'''分组到新文件夹'''
return "分组%03d/" % (index // 2 + 1) + path.rsplit("/", 1)[-1]`;
const TEMPLATE = `def transform(path):
'''功能名称（显示在按钮上）'''
# path 是相对路径，如 "子目录/文件.mp4"
# 返回新相对路径；返回 None 表示不动这个文件
return None
`;
async function jget(u){const r = await fetch(u); return r.json();}
async function jpost(u, body){const r = await fetch(u, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body||{})}); return r.json();}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function fmtT(ts){return ts? new Date(ts*1000).toLocaleString("zh-CN",{hour12:false}): "";}
let toastTimer = null;
function toast(msg, isErr){ const t = document.getElementById("toast"); t.textContent = msg; t.className = "show" + (isErr? " err": ""); clearTimeout(toastTimer); toastTimer = setTimeout(()=>{ t.className = "";}, 3400);}
function renderFuncs(){
let h = "";
for(const t of tools){
h += "<div class='fcard'><div class='nm'>"+esc(t.name)+"</div><div class='fl'>"+esc(t.file)+"</div>";
if(t.ok){ h += "<div style='margin-top:6px'>"+(t.needs.length? "<span class='badge need'>需要 "+esc(t.needs.join(" / "))+"</span>": "<span class='badge'>只需路径</span>")+"</div>";}
else { h += "<div class='err' style='margin-top:6px'>"+esc((t.error||"").slice(-160))+"</div>";}
if(t.desc) h += "<div class='ds'>"+esc(t.desc)+"</div>";
h += "<div class='ops'><button class='link' onclick='viewSrc(\""+esc(t.file)+"\")'>源码</button><button class='link red' onclick='delTool(\""+esc(t.file)+"\")'>删除</button></div></div>";
}
document.getElementById("funcs").innerHTML = h || "<div class='empty'>暂无功能</div>";
}
const CHIP = {open:"待选择功能", fields:"补读字段中", ready:"清单已生成", done:"已完成"};
function batchBody(b){
const okTools = tools.filter(t=>t.ok);
if(b.status === "open"){
if(!okTools.length) return "<div class='status-line'>暂无可用功能</div>";
return "<div class='frow'>" + okTools.map(t =>
"<button class='fbtn' onclick='runTool("+b.id+",\""+t.id+"\")'>"+esc(t.name)+(t.needs.length? "<small>需"+esc(t.needs.join("/"))+"</small>": "")+"</button>").join("") + "</div>";
}
if(b.status === "fields") return "<div class='status-line dotpulse'>已点选 <b>"+esc(toolName(b.tool_id))+"</b>，等待客户端补读字段（"+esc((b.needs||[]).join(", "))+"）</div>";
if(b.status === "ready") return "<div class='status-line'>已点选 <b>"+esc(toolName(b.tool_id))+"</b> · 生成 <b>"+b.plan_count+"</b> 条操作，等待客户端执行"+(b.plan_note? " · "+esc(b.plan_note): "")+"</div>"
+ "<div><button class='link' onclick='togglePlan("+b.id+", this)'>查看清单</button></div><div id='plan-"+b.id+"'></div>";
if(b.status === "done"){ let rep = {}; try{ rep = JSON.parse(b.report||"{}");}catch(e){}
return "<div class='status-line'>功能 <b>"+esc(toolName(b.tool_id))+"</b> · 移动 "+(rep.moved??"—")+" · 跳过 "+(rep.skipped??"—")+" · 失败 "+(rep.errors??"—")+"</div>";}
return "";
}
function toolName(id){ const t = tools.find(x=>x.id===id); return t? t.name: id;}
function renderBatches(){
const box = document.getElementById("batches");
if(!batches.length){ box.innerHTML = "<div class='empty'>暂无批次。运行客户端扫描一个文件夹，批次会出现在这里。</div>"; return;}
box.innerHTML = batches.map(b =>
"<div class='bcard'><div class='top'><span class='nm'>"+esc(b.name)+"</span>"
+ "<span class='chip "+b.status+"'>"+CHIP[b.status]+"</span>"
+ "<span class='meta'>"+b.file_count+" 个文件 · "+fmtT(b.created_at)+"</span>"
+ "<button class='del' title='删除批次' onclick='delBatch("+b.id+")'>×</button></div>"
+ batchBody(b) + "</div>").join("");
}
async function refresh(){
const td = await jget("api/tools"); if(td.ok) tools = td.tools;
const bd = await jget("api/batches"); if(bd.ok) batches = bd.batches;
const s = JSON.stringify([tools.map(t=>[t.id,t.ok,t.name]), batches.map(b=>[b.id,b.status,b.plan_count,b.tool_id])]);
if(s!== sig){ sig = s; renderFuncs(); renderBatches();}
}
async function runTool(bid, tid){
const d = await jpost("api/batches/"+bid+"/run", {tool_id: tid});
toast(d.ok? (d.msg||"已执行"): (d.error||"失败"),!d.ok);
sig = ""; refresh();
}
async function togglePlan(bid, btn){
const box = document.getElementById("plan-"+bid);
if(box.innerHTML){ box.innerHTML = ""; btn.textContent = "查看清单"; return;}
const d = await jget("api/batches/"+bid); if(!d.ok) return;
const plan = d.batch.plan || [];
box.innerHTML = "<div class='planbox'><table>" + plan.map(op=>"<tr><td>"+esc(op.from)+"</td><td>→</td><td>"+esc(op.to)+"</td></tr>").join("") + "</table></div>"
+ (d.batch.plan_truncated? "<div class='meta'>仅显示前 300 条</div>": "");
btn.textContent = "收起清单";
}
async function delBatch(id){ if(!confirm("删除批次 "+id+"？")) return; await fetch("api/batches/"+id, {method:"DELETE"}); sig=""; refresh();}
async function viewSrc(fn){
const d = await jget("api/tools/"+encodeURIComponent(fn)+"/source");
if(d.ok){ document.getElementById("srcTitle").textContent = fn; document.getElementById("srcPre").textContent = d.content; document.getElementById("srcModal").classList.add("show");}
}
async function delTool(fn){ if(!confirm("删除功能 "+fn+"？")) return; await fetch("api/tools/"+encodeURIComponent(fn), {method:"DELETE"}); sig=""; refresh();}
function openNew(){ modalOpen = true; document.getElementById("nf-code").value = TEMPLATE; document.getElementById("nf-msg").textContent = ""; document.getElementById("newModal").classList.add("show");}
function closeNew(){ modalOpen = false; document.getElementById("newModal").classList.remove("show");}
async function saveTool(){
const fn = document.getElementById("nf-name").value.trim();
const code = document.getElementById("nf-code").value;
const m = document.getElementById("nf-msg");
if(!fn){ m.textContent = "先填文件名"; return;}
const d = await jpost("api/tools/upload", {filename: fn, content: code});
if(d.ok){ closeNew(); toast(d.msg||"已生效"); sig=""; refresh();}
else { m.className = "err"; m.textContent = d.error||"保存失败";}
}
function uploadFile(inp){
const f = inp.files[0]; if(!f) return;
const rd = new FileReader();
rd.onload = async () => { const d = await jpost("api/tools/upload", {filename: f.name, content: rd.result}); toast(d.ok? d.msg: d.error,!d.ok); sig=""; refresh();};
rd.readAsText(f); inp.value = "";
}
function openHelp(){ modalOpen = true; document.getElementById("specPre").textContent = SPEC; document.getElementById("helpModal").classList.add("show");}
function closeHelp(){ modalOpen = false; document.getElementById("helpModal").classList.remove("show");}
async function setToken(){
const v = document.getElementById("tok").value;
const d = await jpost("api/settings/token", {token: v});
toast(d.ok? "令牌已保存": (d.error||"失败"),!d.ok);
if(d.ok){ document.getElementById("tok").value = ""; document.getElementById("tokState").textContent = "令牌已设置";}
}
(async function init(){
const s = await jget("api/session");
document.getElementById("tokState").textContent = s.token_configured? "令牌已设置": "令牌未设置（客户端连不上，先在这里保存一个）";
await refresh();
setInterval(()=>{ if(!modalOpen) refresh();}, 3000);
})();
</script></body></html>
"""
