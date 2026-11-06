# -*- coding: utf-8 -*-
"""nameclean - 文件名清洗台 (App Hub project)

分析侧：接收批次文件名 -> 分段/文档频率分析 -> 候选词块待人工审查 ->
确认入库为核心规则（黑名单/替换/保留）。执行侧（独立客户端程序）通过
令牌接口上传批次、轮询状态、拉取「涉及本批」的规则子集完成本地改名。

核心数据（规则库）先行：每批分析前先用已有规则过滤，已判定的词块
（黑名单/替换/保留）不会再作为候选出现，只有新词块需要审查。

平台层负责网页访问验证（/web 代理门禁）；客户端接口使用本项目自己的
令牌（仅写入、不可读回），与 vidlib WebDAV 自有口令同理。
数据存 DATA_DIR/nameclean.db (SQLite)。
分段/套用算法与客户端 nameclean_client.py 保持一致（SEG_VERSION）。
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time

SEG_VERSION = 1

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


DB_PATH = os.path.join(_data_dir(), "nameclean.db")
_local = threading.local()
_init_lock = threading.Lock()
_inited = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,             -- blacklist | replace | keep | substr
  pattern TEXT NOT NULL,
  replace_to TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'core',   -- core | batch
  batch_id INTEGER NOT NULL DEFAULT 0,
  hits INTEGER NOT NULL DEFAULT 0,
  batches_seen INTEGER NOT NULL DEFAULT 0,
  last_seen_at REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0,
  UNIQUE(kind, pattern, scope, batch_id)
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'client',
  status TEXT NOT NULL DEFAULT 'review',  -- review | confirmed
  file_count INTEGER NOT NULL DEFAULT 0,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL DEFAULT 0,
  confirmed_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS batch_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  filename TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  token TEXT NOT NULL,
  cnt INTEGER NOT NULL DEFAULT 0,
  head INTEGER NOT NULL DEFAULT 0,
  tail INTEGER NOT NULL DEFAULT 0,
  samples TEXT NOT NULL DEFAULT '[]',
  hint TEXT NOT NULL DEFAULT '',
  decision TEXT NOT NULL DEFAULT '',      -- '' | blacklist | replace | keep | ignore
  replace_to TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'core',
  UNIQUE(batch_id, token)
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


def _init():
    global _inited
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        c = _conn()
        c.executescript(SCHEMA)
        c.commit()
        _inited = True


# ---------------- segmentation (shared spec with the client) ------------

SEP_CHARS = set("-_ \t|~·•・*+&@#%=,，;；'\"")
BRACKET_CHARS = set("[]()（）《》〈〉{}<>「」『』") | {chr(12304), chr(12305)}
DOMAIN_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9\-]{1,30}\."
    r"(?:com|net|org|cc|xyz|top|vip|cn|io|me|tv|co|info|site|club|online)"
    r"(?:\.[a-z]{2,3})?")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
EPISODE_RE = re.compile(
    r"(?i)^(s\d{1,2}e\d{1,3}|e\d{1,3}|ep\d{1,3}|cd\d|dvd\d|"
    r"第[0-9一二三四五六七八九十百零〇两]+[集期话話回讲講])$")
DIGITS_RE = re.compile(r"^\d+$")


def split_ext(name):
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        if stem and 0 < len(ext) <= 5 and ext.isalnum():
            return stem, "." + ext
    return name, ""


def strip_domains(text):
    return DOMAIN_RE.sub("\x00", text)


def segments(name):
    stem, _ext = split_ext(name)
    stem = strip_domains(stem)
    out, cur = [], []
    for ch in stem:
        if ch in SEP_CHARS or ch in BRACKET_CHARS or ch == "." or ch == "\x00":
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [t.strip() for t in out if t.strip()]


def is_protected(token):
    return bool(YEAR_RE.match(token) or EPISODE_RE.match(token)
                or DIGITS_RE.match(token))


# ---------------- hints for reviewers -----------------------------------

AD_HINTS = ["影视", "电影", "资源", "首发", "独家", "免费", "下载", "最新",
            "最全", "社区", "论坛", "出品", "呈现", "微信", "公众号", "导航",
            "官网", "发布", "交流群", "加群", "高清", "超清", "蓝光", "每日",
            "更新", "天堂", "之家", "在线", "观看", "完整版", "未删减"]
TECH_HINTS = ["bluray", "bdrip", "bd", "web-dl", "webdl", "hdtv", "x264",
              "x265", "h264", "h265", "hevc", "avc", "dts", "aac", "ac3",
              "10bit", "hdr", "remux", "dvdrip", "brrip", "2160p", "1080p",
              "720p", "480p", "4k", "2k", "uhd", "fps", "码率"]


def hint_of(token):
    low = token.lower()
    for h in AD_HINTS:
        if h in token:
            return "疑似广告"
    for h in TECH_HINTS:
        if h in low:
            return "技术标签"
    return ""


# ---------------- core rules ---------------------------------------------

def _meta_get(key, default=""):
    row = _conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(key, value):
    c = _conn()
    c.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    c.commit()


def core_token_map():
    """token -> (kind, replace_to) for all core-scope decided tokens."""
    m = {}
    for r in _conn().execute(
            "SELECT kind, pattern, replace_to FROM rules WHERE scope = 'core' "
            "AND kind IN ('blacklist', 'replace', 'keep')"):
        m[r["pattern"]] = (r["kind"], r["replace_to"])
    return m


def batch_rules(batch_id):
    """All rules a client needs for this batch: core rules + this batch's own,
    restricted to tokens actually involved in the batch's files."""
    c = _conn()
    toks = set()
    for r in c.execute("SELECT filename FROM batch_files WHERE batch_id = ?",
                       (batch_id,)):
        toks.update(segments(r["filename"]))
    out = {"blacklist": [], "replace": [], "keep": [], "substr": []}
    seen = set()
    for r in c.execute(
            "SELECT kind, pattern, replace_to FROM rules "
            "WHERE scope = 'core' OR (scope = 'batch' AND batch_id = ?) "
            "ORDER BY scope DESC", (batch_id,)):
        key = (r["kind"], r["pattern"])
        if key in seen:
            continue
        if r["kind"] == "substr":
            if any(r["pattern"] in t for t in toks):
                out["substr"].append(r["pattern"])
                seen.add(key)
        elif r["pattern"] in toks:
            seen.add(key)
            if r["kind"] == "replace":
                out["replace"].append([r["pattern"], r["replace_to"]])
            else:
                out[r["kind"]].append(r["pattern"])
    out["blacklist"].sort()
    out["keep"].sort()
    out["substr"].sort()
    out["replace"].sort()
    return out


# ---------------- batch analysis ------------------------------------------

def analyze_batch(batch_id):
    """(Re)compute candidates for a batch. Core-decided tokens and protected
    tokens never become candidates — the core data speaks first."""
    c = _conn()
    core = core_token_map()
    stats = {}
    order = []
    for r in c.execute("SELECT filename FROM batch_files WHERE batch_id = ? "
                       "ORDER BY id", (batch_id,)):
        toks = segments(r["filename"])
        last = len(toks) - 1
        for i, t in enumerate(toks):
            if t in core or is_protected(t) or len(t) < 2:
                continue
            s = stats.get(t)
            if s is None:
                s = stats[t] = {"cnt": 0, "head": 0, "tail": 0, "samples": []}
                order.append(t)
            s["cnt"] += 1
            if i == 0:
                s["head"] += 1
            if i == last and last > 0:
                s["tail"] += 1
            if len(s["samples"]) < 3 and r["filename"] not in s["samples"]:
                s["samples"].append(r["filename"])
    nfiles = c.execute("SELECT COUNT(*) AS n FROM batch_files WHERE batch_id = ?",
                       (batch_id,)).fetchone()["n"]
    threshold = 1 if nfiles < 10 else 2
    c.execute("DELETE FROM candidates WHERE batch_id = ?", (batch_id,))
    made = 0
    for t in order:
        s = stats[t]
        if s["cnt"] < threshold:
            continue
        c.execute(
            "INSERT OR IGNORE INTO candidates "
            "(batch_id, token, cnt, head, tail, samples, hint) VALUES (?,?,?,?,?,?,?)",
            (batch_id, t, s["cnt"], s["head"], s["tail"],
             json.dumps(s["samples"], ensure_ascii=False), hint_of(t)))
        made += 1
    c.execute("UPDATE batches SET candidate_count = ? WHERE id = ?",
              (made, batch_id))
    c.commit()
    return made


def create_batch(name, filenames, source):
    c = _conn()
    now = time.time()
    cur = c.execute(
        "INSERT INTO batches (name, source, status, file_count, created_at) "
        "VALUES (?, ?, 'review', ?, ?)",
        (name or ("批次 " + time.strftime("%m-%d %H:%M", time.localtime(now))),
         source, len(filenames), now))
    bid = cur.lastrowid
    c.executemany("INSERT INTO batch_files (batch_id, filename) VALUES (?, ?)",
                  [(bid, f) for f in filenames])
    c.commit()
    made = analyze_batch(bid)
    if made == 0:
        # nothing new to review: the core data already covers this batch
        confirm_batch(bid)
    row = c.execute("SELECT * FROM batches WHERE id = ?", (bid,)).fetchone()
    return dict(row)


def confirm_batch(batch_id):
    """Write all decisions into the rule store and mark the batch confirmed.
    Undecided candidates count as 'ignore'."""
    c = _conn()
    now = time.time()
    touched_tokens = set()
    for r in c.execute("SELECT * FROM candidates WHERE batch_id = ?",
                       (batch_id,)):
        dec = r["decision"] or "ignore"
        if dec in ("blacklist", "replace", "keep"):
            scope = r["scope"] or "core"
            c.execute(
                "INSERT INTO rules (kind, pattern, replace_to, scope, batch_id, "
                "batches_seen, last_seen_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,1,?,?,?) "
                "ON CONFLICT(kind, pattern, scope, batch_id) DO UPDATE SET "
                "replace_to = excluded.replace_to, updated_at = excluded.updated_at",
                (dec, r["token"], r["replace_to"] if dec == "replace" else "",
                 scope, 0 if scope == "core" else batch_id, now, now, now))
            if scope == "core":
                touched_tokens.add(r["token"])
    # rules involved in this batch gain a batches_seen tick
    toks = set()
    for r in c.execute("SELECT filename FROM batch_files WHERE batch_id = ?",
                       (batch_id,)):
        toks.update(segments(r["filename"]))
    for r in c.execute("SELECT id, kind, pattern FROM rules "
                       "WHERE scope = 'core' OR (scope = 'batch' AND batch_id = ?)",
                       (batch_id,)):
        if r["kind"] == "substr":
            hit = any(r["pattern"] in t for t in toks)
        else:
            hit = r["pattern"] in toks
        if hit and r["pattern"] not in touched_tokens:
            c.execute("UPDATE rules SET batches_seen = batches_seen + 1, "
                      "last_seen_at = ? WHERE id = ?", (now, r["id"]))
    c.execute("UPDATE batches SET status = 'confirmed', confirmed_at = ? "
              "WHERE id = ?", (now, batch_id))
    c.commit()


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
    given = environ.get("HTTP_X_CLEAN_TOKEN", "")
    return bool(given) and hashlib.sha256(given.encode("utf-8")).hexdigest() == stored


# ---------------- client API (token) ----------------------------------------

def api_client_create_batch(environ):
    data = _json_body(environ)
    names = [str(x).strip() for x in (data.get("filenames") or []) if str(x).strip()]
    names = list(dict.fromkeys(names))
    if not names:
        return {"ok": False, "error": "filenames 为空"}, "400 Bad Request"
    if len(names) > 20000:
        return {"ok": False, "error": "单批最多 20000 个文件名"}, "400 Bad Request"
    b = create_batch(str(data.get("name") or ""), names, "client")
    pending = _conn().execute(
        "SELECT COUNT(*) AS n FROM candidates WHERE batch_id = ? AND decision = ''",
        (b["id"],)).fetchone()["n"]
    return {"ok": True, "batch_id": b["id"], "file_count": b["file_count"],
            "candidate_count": b["candidate_count"], "pending": pending,
            "status": b["status"]}, "200 OK"


def api_client_batch_status(environ, batch_id):
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    pending = c.execute(
        "SELECT COUNT(*) AS n FROM candidates WHERE batch_id = ? AND decision = ''",
        (batch_id,)).fetchone()["n"]
    return {"ok": True, "batch_id": batch_id, "status": b["status"],
            "candidate_count": b["candidate_count"], "pending": pending}, "200 OK"


def api_client_batch_rules(environ, batch_id):
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    if b["status"] != "confirmed":
        return {"ok": False, "error": "批次尚未确认", "status": b["status"],
                "pending": c.execute(
                    "SELECT COUNT(*) AS n FROM candidates WHERE batch_id = ? "
                    "AND decision = ''", (batch_id,)).fetchone()["n"]}, "409 Conflict"
    rules = batch_rules(batch_id)
    rules.update({"ok": True, "batch_id": batch_id, "seg_version": SEG_VERSION})
    return rules, "200 OK"


def api_client_report(environ, batch_id):
    data = _json_body(environ)
    hits = data.get("hits") or {}
    c = _conn()
    now = time.time()
    n = 0
    for pattern, cnt in hits.items():
        try:
            cnt = int(cnt)
        except (TypeError, ValueError):
            continue
        cur = c.execute(
            "UPDATE rules SET hits = hits + ?, last_seen_at = ? WHERE pattern = ? "
            "AND (scope = 'core' OR (scope = 'batch' AND batch_id = ?))",
            (cnt, now, str(pattern), batch_id))
        n += cur.rowcount
    c.commit()
    return {"ok": True, "updated": n}, "200 OK"


# ---------------- web API (platform gate outside) ----------------------------

def api_session(environ):
    return {"ok": True, "authenticated": True, "auth_via": "platform",
            "token_configured": bool(_meta_get("client_token_hash"))}, "200 OK"


def api_batches(environ):
    rows = _conn().execute("SELECT * FROM batches ORDER BY id DESC LIMIT 100").fetchall()
    out = []
    for b in rows:
        d = dict(b)
        d["pending"] = _conn().execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE batch_id = ? AND decision = ''",
            (b["id"],)).fetchone()["n"]
        out.append(d)
    return {"batches": out}, "200 OK"


def api_web_create_batch(environ):
    data = _json_body(environ)
    text = str(data.get("text") or "")
    names = [ln.strip() for ln in text.splitlines() if ln.strip()]
    names = list(dict.fromkeys(names))
    if not names:
        return {"ok": False, "error": "没有可用的文件名"}, "400 Bad Request"
    b = create_batch(str(data.get("name") or ""), names, "web")
    return {"ok": True, "batch_id": b["id"],
            "candidate_count": b["candidate_count"]}, "200 OK"


def api_batch_detail(environ, batch_id):
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    files = [r["filename"] for r in c.execute(
        "SELECT filename FROM batch_files WHERE batch_id = ? ORDER BY id",
        (batch_id,))]
    cands = []
    for r in c.execute("SELECT * FROM candidates WHERE batch_id = ? "
                       "ORDER BY cnt DESC, head DESC, token", (batch_id,)):
        d = dict(r)
        d["samples"] = json.loads(d["samples"] or "[]")
        cands.append(d)
    return {"batch": dict(b), "files": files, "candidates": cands}, "200 OK"


def api_candidate_decide(environ, cand_id):
    data = _json_body(environ)
    dec = str(data.get("decision") or "")
    if dec not in ("blacklist", "replace", "keep", "ignore", ""):
        return {"ok": False, "error": "decision 无效"}, "400 Bad Request"
    c = _conn()
    row = c.execute("SELECT * FROM candidates WHERE id = ?", (cand_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "候选不存在"}, "404 Not Found"
    b = c.execute("SELECT status FROM batches WHERE id = ?",
                  (row["batch_id"],)).fetchone()
    if b and b["status"] == "confirmed":
        return {"ok": False, "error": "批次已确认，不能再改"}, "409 Conflict"
    scope = "batch" if str(data.get("scope") or "core") == "batch" else "core"
    c.execute("UPDATE candidates SET decision = ?, replace_to = ?, scope = ? "
              "WHERE id = ?",
              (dec, str(data.get("replace_to") or "")[:80], scope, cand_id))
    c.commit()
    return {"ok": True}, "200 OK"


def api_batch_confirm(environ, batch_id):
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    if b["status"] == "confirmed":
        return {"ok": True, "already": True}, "200 OK"
    confirm_batch(batch_id)
    return {"ok": True}, "200 OK"


def api_batch_delete(environ, batch_id):
    c = _conn()
    c.execute("DELETE FROM candidates WHERE batch_id = ?", (batch_id,))
    c.execute("DELETE FROM batch_files WHERE batch_id = ?", (batch_id,))
    c.execute("DELETE FROM rules WHERE scope = 'batch' AND batch_id = ?", (batch_id,))
    c.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    c.commit()
    return {"ok": True}, "200 OK"


def api_batch_reanalyze(environ, batch_id):
    c = _conn()
    b = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not b:
        return {"ok": False, "error": "批次不存在"}, "404 Not Found"
    if b["status"] == "confirmed":
        return {"ok": False, "error": "已确认批次不能重分析"}, "409 Conflict"
    n = analyze_batch(batch_id)
    return {"ok": True, "candidate_count": n}, "200 OK"


def api_rules(environ):
    rows = _conn().execute(
        "SELECT * FROM rules ORDER BY scope, kind, pattern").fetchall()
    return {"rules": [dict(r) for r in rows]}, "200 OK"


def api_rule_save(environ):
    data = _json_body(environ)
    kind = str(data.get("kind") or "")
    pattern = str(data.get("pattern") or "").strip()
    if kind not in ("blacklist", "replace", "keep", "substr") or not pattern:
        return {"ok": False, "error": "kind 或 pattern 无效"}, "400 Bad Request"
    now = time.time()
    c = _conn()
    c.execute(
        "INSERT INTO rules (kind, pattern, replace_to, scope, batch_id, "
        "created_at, updated_at) VALUES (?,?,?,'core',0,?,?) "
        "ON CONFLICT(kind, pattern, scope, batch_id) DO UPDATE SET "
        "replace_to = excluded.replace_to, updated_at = excluded.updated_at",
        (kind, pattern, str(data.get("replace_to") or "")[:80], now, now))
    c.commit()
    return {"ok": True}, "200 OK"


def api_rule_delete(environ, rule_id):
    c = _conn()
    c.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    c.commit()
    return {"ok": True}, "200 OK"


def api_rules_export(environ):
    rules = _conn().execute(
        "SELECT kind, pattern, replace_to FROM rules WHERE scope = 'core' "
        "ORDER BY kind, pattern").fetchall()
    lines = ["# 黑名单"]
    lines += [r["pattern"] for r in rules if r["kind"] == "blacklist"]
    lines.append("# 替换")
    lines += ["%s => %s" % (r["pattern"], r["replace_to"])
              for r in rules if r["kind"] == "replace"]
    lines.append("# 保留")
    lines += [r["pattern"] for r in rules if r["kind"] == "keep"]
    lines.append("# 子串删除")
    lines += [r["pattern"] for r in rules if r["kind"] == "substr"]
    return {"text": "\n".join(lines) + "\n"}, "200 OK"


def api_token_set(environ):
    data = _json_body(environ)
    tok = str(data.get("token") or "").strip()
    if tok and len(tok) < 8:
        return {"ok": False, "error": "令牌至少 8 位"}, "400 Bad Request"
    _meta_set("client_token_hash",
              hashlib.sha256(tok.encode("utf-8")).hexdigest() if tok else "")
    return {"ok": True, "configured": bool(tok)}, "200 OK"


# ---------------- page -------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件名清洗台</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--dim:#8b949e;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;background:rgba(13,17,23,.92);backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:10px 14px;z-index:5}
header .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0 10px 0 0}
nav{display:flex;gap:6px}
nav button,.btn{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:14px}
nav button.on{border-color:var(--acc);color:var(--acc)}
.btn.primary{border-color:var(--acc);color:var(--acc)}
.btn.danger{border-color:var(--bad);color:var(--bad)}
.btn.sm{padding:2px 9px;font-size:13px}
main{max-width:980px;margin:0 auto;padding:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--dim);font-weight:600;white-space:nowrap}
input,select,textarea{background:#0d1117;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 9px;font-size:14px}
textarea{width:100%;min-height:110px}
.small{color:var(--dim);font-size:13px}
.tag{display:inline-block;border:1px solid var(--line);border-radius:20px;padding:0 8px;font-size:12px;color:var(--dim)}
.dec{display:flex;gap:4px;flex-wrap:wrap}
.dec button{background:#0d1117;color:var(--dim);border:1px solid var(--line);border-radius:6px;padding:2px 8px;cursor:pointer;font-size:13px}
.dec button.on[data-d="blacklist"]{background:var(--bad);border-color:var(--bad);color:#fff}
.dec button.on[data-d="replace"]{background:var(--warn);border-color:var(--warn);color:#111}
.dec button.on[data-d="keep"]{background:var(--ok);border-color:var(--ok);color:#111}
.dec button.on[data-d="ignore"]{background:#484f58;border-color:#484f58;color:#fff}
code{background:#0d1117;border:1px solid var(--line);border-radius:6px;padding:1px 5px;font-size:13px}
pre{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;white-space:pre-wrap;word-break:break-all}
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#21262d;border:1px solid var(--line);color:var(--txt);padding:8px 16px;border-radius:20px;display:none;z-index:9}
.samples{color:var(--dim);font-size:12px;max-width:300px;word-break:break-all}
</style>
</head>
<body>
<header><div class="row">
<h1>文件名清洗台</h1>
<nav>
<button data-v="batches" class="on">批次审查</button>
<button data-v="rules">核心规则库</button>
<button data-v="settings">设置</button>
</nav>
<a class="btn sm" href="download/nameclean_client.py" style="text-decoration:none;margin-left:auto">下载客户端</a>
</div></header>
<main id="app"></main>
<div id="toast"></div>
<script>
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const BASE = location.pathname.replace(/\/?$/, "/");
async function api(path, opt){
  const r = await fetch(BASE + path.replace(/^\//, ""), Object.assign({headers:{"Content-Type":"application/json"}}, opt || {}));
  const d = await r.json().catch(() => ({}));
  if(!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}
function toast(msg){ const t = $("#toast"); t.textContent = msg; t.style.display = "block"; setTimeout(() => t.style.display = "none", 2200); }
let state = {view: "batches", batchId: null};
let pollTimer = null;
function stopPoll(){ if(pollTimer){ clearInterval(pollTimer); pollTimer = null; } }
let blSig = "";
function batchSig(bs){ return bs.map(b => b.id + ":" + b.status + ":" + b.pending + ":" + b.candidate_count).join(","); }
function batchRowsHtml(bs){
  let h = "";
  for(const b of bs){
    h += '<tr><td>' + b.id + '</td><td>' + esc(b.name) + '</td><td>' + (b.source === "client" ? "客户端" : "网页") + '</td><td>' + b.file_count + '</td><td>' + b.candidate_count + '</td><td>' + b.pending + '</td><td>' + (b.status === "confirmed" ? '<span style="color:var(--ok)">已入库</span>' : '<span style="color:var(--warn)">待审查</span>') + '</td><td class="small">' + new Date(b.created_at * 1000).toLocaleString() + '</td>'
      + '<td style="white-space:nowrap"><button class="btn sm" data-review="' + b.id + '">审查</button> <button class="btn sm danger" data-del="' + b.id + '">删</button></td></tr>';
  }
  return h || '<tr><td colspan="9" class="small">暂无批次</td></tr>';
}
function bindBatchButtons(app){
  app.querySelectorAll("[data-review]").forEach(b => b.addEventListener("click", () => { state.batchId = +b.dataset.review; render(); }));
  app.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", async () => {
    if(!confirm("删除该批次（含其本批规则）？")) return;
    await api("api/batches/" + b.dataset.del + "/delete", {method: "POST", body: "{}"}); render();
  }));
}
document.querySelectorAll("nav button").forEach(b => b.addEventListener("click", () => { state.view = b.dataset.v; state.batchId = null; render(); }));
function syncNav(){ document.querySelectorAll("nav button").forEach(b => b.classList.toggle("on", b.dataset.v === state.view)); }

async function render(){
  stopPoll();
  syncNav();
  const app = $("#app");
  if(state.view === "batches" && !state.batchId) return renderBatches(app);
  if(state.view === "batches" && state.batchId) return renderReview(app);
  if(state.view === "rules") return renderRules(app);
  if(state.view === "settings") return renderSettings(app);
}

async function renderBatches(app){
  const d = await api("api/batches");
  let h = '<div class="card"><h3 style="margin:2px 0 8px">新建批次（粘贴文件名，一行一个）</h3>'
    + '<input id="nb-name" placeholder="批次名（可空）" style="width:100%;margin-bottom:8px">'
    + '<textarea id="nb-text" placeholder="把客户端导出的文件名清单粘贴到这里"></textarea>'
    + '<div style="margin-top:8px"><button class="btn primary" id="nb-go">建批次并分析</button></div>'
    + '<p class="small">客户端上传的批次也会出现在这里。分析前先用核心规则库过滤：已判定的词块不会再出现，只有新词块进审查。</p></div>';
  h += '<div class="card"><h3 style="margin:2px 0 8px">批次列表 <span class="small">（每 4 秒自动刷新，客户端上传即出现）</span></h3><table><thead><tr><th>ID</th><th>名称</th><th>来源</th><th>文件</th><th>候选</th><th>待确认</th><th>状态</th><th>时间</th><th></th></tr></thead><tbody id="bl-body"></tbody></table></div>';
  app.innerHTML = h;
  const tbody = $("#bl-body");
  tbody.innerHTML = batchRowsHtml(d.batches);
  bindBatchButtons(app);
  blSig = batchSig(d.batches);
  pollTimer = setInterval(async () => {
    try{
      const nd = await api("api/batches");
      const sig = batchSig(nd.batches);
      if(sig !== blSig){
        blSig = sig;
        tbody.innerHTML = batchRowsHtml(nd.batches);
        bindBatchButtons(app);
        toast("批次列表已更新");
      }
    }catch(e){}
  }, 4000);
  $("#nb-go").addEventListener("click", async () => {
    try{
      const r = await api("api/batches", {method: "POST", body: JSON.stringify({name: $("#nb-name").value, text: $("#nb-text").value})});
      toast("已建批次 #" + r.batch_id + "，候选 " + r.candidate_count + " 个"); state.batchId = r.batch_id; render();
    }catch(e){ toast(e.message); }
  });
}

async function renderReview(app){
  const d = await api("api/batches/" + state.batchId);
  const b = d.batch;
  let h = '<div style="margin-bottom:8px"><button class="btn sm" id="rv-back">← 批次列表</button></div>';
  h += '<div class="card"><h3 style="margin:2px 0">批次 #' + b.id + " " + esc(b.name) + '</h3>'
    + '<p class="small">文件 ' + b.file_count + " 个 · 候选 " + d.candidates.length + " 个 · 状态 " + (b.status === "confirmed" ? "已入库" : "待审查") + '</p>'
    + '<details><summary class="small">查看全部文件名</summary><pre>' + esc(d.files.join("\n")) + '</pre></details></div>';
  h += '<div class="card"><table><tr><th>词块</th><th>次数</th><th>首/尾</th><th>提示</th><th>示例</th><th>判定</th><th>替换为 / 范围</th></tr>';
  for(const c of d.candidates){
    h += '<tr data-cid="' + c.id + '"><td><b>' + esc(c.token) + '</b></td><td>' + c.cnt + '</td><td>' + c.head + "/" + c.tail + '</td>'
      + '<td>' + (c.hint ? '<span class="tag">' + esc(c.hint) + "</span>" : "") + '</td>'
      + '<td class="samples">' + c.samples.map(esc).join("<br>") + '</td>'
      + '<td><div class="dec">'
      + '<button data-d="blacklist" class="' + (c.decision === "blacklist" ? "on" : "") + '">拉黑</button>'
      + '<button data-d="replace" class="' + (c.decision === "replace" ? "on" : "") + '">替换</button>'
      + '<button data-d="keep" class="' + (c.decision === "keep" ? "on" : "") + '">保留</button>'
      + '<button data-d="ignore" class="' + (c.decision === "ignore" ? "on" : "") + '">忽略</button>'
      + '</div></td>'
      + '<td><input class="rp" value="' + esc(c.replace_to || "") + '" placeholder="替换文本" style="width:110px"> '
      + '<select class="sc"><option value="core"' + (c.scope !== "batch" ? " selected" : "") + '>核心</option><option value="batch"' + (c.scope === "batch" ? " selected" : "") + '>本批</option></select></td></tr>';
  }
  h += '</table>';
  if(b.status !== "confirmed"){
    h += '<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap"><button class="btn primary" id="rv-confirm">完成审查 · 入库</button>'
      + '<button class="btn" id="rv-re">重新分析（按当前核心库过滤）</button></div>'
      + '<p class="small">未判定的候选在入库时按「忽略」处理（不进规则库，下批可能再出现）。「保留」表示确认不是广告，进核心库后永不再问。</p>';
  }
  h += '</div>';
  app.innerHTML = h;
  $("#rv-back").addEventListener("click", () => { state.batchId = null; render(); });
  const reBtn = $("#rv-re"); if(reBtn) reBtn.addEventListener("click", async () => { await api("api/batches/" + b.id + "/reanalyze", {method: "POST", body: "{}"}); toast("已重新分析"); render(); });
  const cfBtn = $("#rv-confirm"); if(cfBtn) cfBtn.addEventListener("click", async () => {
    if(!confirm("确认入库？判定将写入规则库，客户端随后可拉取本批规则。")) return;
    await api("api/batches/" + b.id + "/confirm", {method: "POST", body: "{}"}); toast("已入库"); render();
  });
  app.querySelectorAll("tr[data-cid]").forEach(tr => {
    const cid = tr.dataset.cid;
    tr.querySelectorAll(".dec button").forEach(btn => btn.addEventListener("click", async () => {
      const cur = btn.classList.contains("on");
      const dec = cur ? "" : btn.dataset.d;
      tr.querySelectorAll(".dec button").forEach(x => x.classList.remove("on"));
      if(!cur) btn.classList.add("on");
      try{
        await api("api/candidates/" + cid + "/decide", {method: "POST", body: JSON.stringify({decision: dec, replace_to: tr.querySelector(".rp").value, scope: tr.querySelector(".sc").value})});
      }catch(e){ toast(e.message); }
    }));
    const saveMeta = async () => {
      const onBtn = tr.querySelector(".dec button.on");
      if(!onBtn) return;
      await api("api/candidates/" + cid + "/decide", {method: "POST", body: JSON.stringify({decision: onBtn.dataset.d, replace_to: tr.querySelector(".rp").value, scope: tr.querySelector(".sc").value})}).catch(() => {});
    };
    tr.querySelector(".rp").addEventListener("change", saveMeta);
    tr.querySelector(".sc").addEventListener("change", saveMeta);
  });
  let rvSig = b.status + "|" + d.candidates.map(c => c.id + ":" + c.decision).join(",");
  pollTimer = setInterval(async () => {
    try{
      const nd = await api("api/batches/" + b.id);
      const sig = nd.batch.status + "|" + nd.candidates.map(c => c.id + ":" + c.decision).join(",");
      if(sig !== rvSig){
        const ae = document.activeElement;
        if(ae && (ae.tagName === "INPUT" || ae.tagName === "SELECT" || ae.tagName === "TEXTAREA")) return;
        toast("页面已同步最新状态");
        render();
      }
    }catch(e){}
  }, 4000);
}

async function renderRules(app){
  const d = await api("api/rules");
  const kinds = {blacklist: "黑名单", replace: "替换", keep: "保留", substr: "子串删除"};
  let h = '<div class="card"><h3 style="margin:2px 0 8px">手动加规则（核心）</h3>'
    + '<div style="display:flex;gap:6px;flex-wrap:wrap"><select id="rl-kind"><option value="blacklist">黑名单</option><option value="replace">替换</option><option value="keep">保留</option><option value="substr">子串删除</option></select>'
    + '<input id="rl-pattern" placeholder="词块" style="flex:1;min-width:160px"><input id="rl-to" placeholder="替换为（仅替换类）" style="width:150px">'
    + '<button class="btn primary" id="rl-add">添加</button></div>'
    + '<p class="small">子串删除：词块里包含该子串就删掉子串部分（适合没有分隔符、和片名粘在一起的站名）。</p></div>';
  h += '<div class="card"><h3 style="margin:2px 0 8px">规则库（共 ' + d.rules.length + ' 条）</h3><table><tr><th>类型</th><th>词块</th><th>替换为</th><th>范围</th><th>命中</th><th>批次</th><th></th></tr>';
  for(const r of d.rules){
    h += '<tr><td>' + kinds[r.kind] + '</td><td><b>' + esc(r.pattern) + '</b></td><td>' + esc(r.replace_to) + '</td><td>' + (r.scope === "core" ? "核心" : "批次#" + r.batch_id) + '</td><td>' + r.hits + '</td><td>' + r.batches_seen + '</td>'
      + '<td><button class="btn sm danger" data-rdel="' + r.id + '">删</button></td></tr>';
  }
  h += '</table></div>';
  h += '<div class="card"><h3 style="margin:2px 0 8px">导出规则文本（给手动模式的客户端）</h3><pre id="rl-export">加载中…</pre><button class="btn" id="rl-copy">复制</button></div>';
  app.innerHTML = h;
  $("#rl-add").addEventListener("click", async () => {
    try{
      await api("api/rules", {method: "POST", body: JSON.stringify({kind: $("#rl-kind").value, pattern: $("#rl-pattern").value, replace_to: $("#rl-to").value})});
      toast("已添加"); render();
    }catch(e){ toast(e.message); }
  });
  app.querySelectorAll("[data-rdel]").forEach(b => b.addEventListener("click", async () => {
    if(!confirm("删除这条规则？")) return;
    await api("api/rules/" + b.dataset.rdel + "/delete", {method: "POST", body: "{}"}); render();
  }));
  const ex = await api("api/rules/export");
  $("#rl-export").textContent = ex.text;
  $("#rl-copy").addEventListener("click", () => { navigator.clipboard.writeText(ex.text).then(() => toast("已复制")); });
}

async function renderSettings(app){
  const s = await api("api/session");
  let h = '<div class="card"><h3 style="margin:2px 0 8px">客户端令牌</h3>'
    + '<p class="small">状态：' + (s.token_configured ? "已设置" : "未设置") + '。令牌只写入、不可读回；客户端配置里填同一个令牌。清空 = 填空提交。</p>'
    + '<div style="display:flex;gap:6px"><input id="st-token" type="password" placeholder="新令牌（至少 8 位）" style="flex:1"><button class="btn primary" id="st-save">保存</button></div></div>';
  h += '<div class="card"><h3 style="margin:2px 0 8px">客户端用法</h3><pre>下载：本页右上角「下载客户端」\n最简单：直接双击运行客户端，按提示输入令牌、把文件夹拖进去即可；\n也可以把文件夹拖到客户端脚本图标上启动。\n命令行：python3 nameclean_client.py --path /待清洗目录\n# 首次在同目录生成 nameclean_client.json，填入 server 与 token：\n#   server = 本页地址（https://debi.su7.dpdns.org/web/nameclean）\n#   token  = 上面设置的令牌\n# 令牌也可以走环境变量（优先于配置文件）：\n#   Linux/Mac: export NAMECLEAN_TOKEN=你的令牌\n#   Windows:   set NAMECLEAN_TOKEN=你的令牌\n# 流程：扫描上传 → 在本页审查 → 客户端自动拉取本批规则 → 预览确认 → 改名\n# 回滚：python3 nameclean_client.py --undo 日志文件.json</pre></div>';
  app.innerHTML = h;
  $("#st-save").addEventListener("click", async () => {
    try{
      const r = await api("api/settings/token", {method: "POST", body: JSON.stringify({token: $("#st-token").value})});
      toast(r.configured ? "令牌已保存" : "令牌已清空"); render();
    }catch(e){ toast(e.message); }
  });
}

const m = location.hash.match(/batch=(\d+)/);
if(m) state.batchId = +m[1];
render().catch(e => { $("#app").innerHTML = '<div class="card">加载失败：' + esc(e.message) + "</div>"; });
</script>
</body>
</html>
"""


# ---------------- router ------------------------------------------------------

def _route(environ):
    _init()
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO") or "/"
    # client API (token-guarded)
    if path.startswith("/api/client/"):
        if not _token_ok(environ):
            return {"ok": False, "error": "令牌无效或未设置"}, "401 Unauthorized"
        if path == "/api/client/batches" and method == "POST":
            return api_client_create_batch(environ)
        m = re.match(r"^/api/client/batches/(\d+)$", path)
        if m and method == "GET":
            return api_client_batch_status(environ, int(m.group(1)))
        m = re.match(r"^/api/client/batches/(\d+)/rules$", path)
        if m and method == "GET":
            return api_client_batch_rules(environ, int(m.group(1)))
        m = re.match(r"^/api/client/batches/(\d+)/report$", path)
        if m and method == "POST":
            return api_client_report(environ, int(m.group(1)))
        return {"ok": False, "error": "not found"}, "404 Not Found"
    # web API (platform gate has already passed the request)
    if path == "/api/session":
        return api_session(environ)
    if path == "/api/batches" and method == "GET":
        return api_batches(environ)
    if path == "/api/batches" and method == "POST":
        return api_web_create_batch(environ)
    m = re.match(r"^/api/batches/(\d+)$", path)
    if m and method == "GET":
        return api_batch_detail(environ, int(m.group(1)))
    m = re.match(r"^/api/batches/(\d+)/(confirm|delete|reanalyze)$", path)
    if m and method == "POST":
        fn = {"confirm": api_batch_confirm, "delete": api_batch_delete,
              "reanalyze": api_batch_reanalyze}[m.group(2)]
        return fn(environ, int(m.group(1)))
    m = re.match(r"^/api/candidates/(\d+)/decide$", path)
    if m and method == "POST":
        return api_candidate_decide(environ, int(m.group(1)))
    if path == "/api/rules" and method == "GET":
        return api_rules(environ)
    if path == "/api/rules" and method == "POST":
        return api_rule_save(environ)
    if path == "/api/rules/export":
        return api_rules_export(environ)
    m = re.match(r"^/api/rules/(\d+)/delete$", path)
    if m and method == "POST":
        return api_rule_delete(environ, int(m.group(1)))
    if path == "/api/settings/token" and method == "POST":
        return api_token_set(environ)
    return None


def application(environ, start_response):
    if (environ.get("PATH_INFO") or "") == "/download/nameclean_client.py":
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "nameclean_client.py")
        if os.path.exists(fp):
            with open(fp, "rb") as f:
                body = f.read()
            start_response("200 OK", [
                ("Content-Type", "text/x-python; charset=utf-8"),
                ("Content-Disposition",
                 "attachment; filename=nameclean_client.py"),
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
