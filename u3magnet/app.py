"""u3 磁力获取 — u3magnet project (debi, App Hub).

Search surface over the u3c3.com crawl corpus. The crawler
(dual-box full crawl 2026-11-11, then a debi-local 30-minute
incremental timer) maintains ONE SQLite database in this
project's DATA_DIR:

    u3c3.db   table items(infohash PK, title, published_at_raw,
              published_at, size_text, size_bytes, category,
              magnet, first_seen_at, source_page)

This app is read-only over that corpus except for the exclusion
list, which lives in the same directory in its own small file:

    exclude.db  table excluded(infohash PK, added_at, source)

Exclusion semantics (user spec 2026-11-11):
  * upload a .txt with one entry per line -- a full magnet link,
    a bare 40-hex infohash, or any line carrying an xt=urn:btih
    value all parse; base32 (32-char) btih is converted to hex;
  * uploads APPEND to the set (deduplicated), the page shows the
    current total plus the last upload's file name / line counts;
  * excluded hashes never appear in search results -- enforced in
    SQL (NOT EXISTS), not hidden in the frontend;
  * a switch can temporarily disable exclusion, and the set can
    be cleared in one click.

Search: title keywords (whitespace-split, AND), optional category
filter, published-desc order, real server-side pagination
(15/page, UI canon P6) with a true hit count. Keywords of >= 3 chars go through
the FTS5 trigram index items_fts (trigger-synced with items);
shorter keywords fall back to a LIKE scan.

Export page (/export, user spec 2026-11-13): search keywords with a
latest-N cap and/or a Beijing-date range, then a fixed pipeline --
blacklist filter first (keyword blacklist stored in
export_blacklist.json, maintained on the page), then title dedup
with the method of the /site/p1791363651 tool (run in the browser,
same code and defaults, titles + size_bytes), then batch copy of
100 magnet links at a time. Confirming a batch, or copying the next
batch, records its infohashes in the processed table (exclude.db),
and later export searches exclude processed hashes in SQL. Search
results themselves are never stored; only the blacklist and the
processed set persist.

This app contains no identity verification: the App Hub platform
gate in front of /web/u3magnet/ performs it centrally, and direct
access on the local port is trusted.
"""
import base64
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse

from export_page import EXPORT_HTML
from page import PAGE_HTML
from stats import STATS_HTML

BASE = os.path.dirname(os.path.abspath(__file__))


def _platform_data_dir(base):
    """DATA_DIR env -> ./data symlink -> ./.runtime (platform rule)."""
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(base, "data")
    if os.path.isdir(link):
        return os.path.realpath(link)
    runtime = os.path.join(base, ".runtime")
    os.makedirs(runtime, exist_ok=True)
    return runtime


DATA_DIR = _platform_data_dir(BASE)
DB_PATH = os.path.join(DATA_DIR, "u3c3.db")
EXCLUDE_PATH = os.path.join(DATA_DIR, "exclude.db")

PAGE_SIZE = 15
MAX_UPLOAD = 16 * 1024 * 1024

EXPORT_BLACKLIST_PATH = os.path.join(DATA_DIR, "export_blacklist.json")
EXPORT_DEFAULT_LIMIT = 500
EXPORT_MAX_LIMIT = 5000
EXPORT_MAX_BLACKLIST = 500

# 分类下拉只列已知类别。站方坏分类（"1"/"1_1"/"Movie"/"test"，共 5 条）
# 原文保留入库、关键词仍可搜到，但不单列为分类选项（2026-11-11 数据审计）。
KNOWN_CATEGORIES = ("Video", "Book", "Photo", "Game", "Other", "U3C3", "Software")

_lock = threading.Lock()

_HEX40 = re.compile(r"\b([0-9a-fA-F]{40})\b")
_XT = re.compile(r"xt=urn:btih:([0-9a-zA-Z]{32,40})")
_B32 = re.compile(r"\b([A-Za-z2-7]{32})\b")


def extract_hash(text):
    """One uploaded line -> lowercase hex infohash or None.

    Order matters: an xt= value wins (it may be base32); then a
    bare 40-hex anywhere in the line; then a bare base32 token.
    """
    if not text:
        return None
    s = text.strip()
    m = _XT.search(s)
    if m:
        v = m.group(1)
        if len(v) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", v):
            return v.lower()
        if len(v) == 32:
            return _b32_to_hex(v)
        return None
    m = _HEX40.search(s)
    if m:
        return m.group(1).lower()
    m = _B32.search(s)
    if m:
        return _b32_to_hex(m.group(1))
    return None


def _b32_to_hex(v):
    try:
        raw = base64.b32decode(v.upper())
    except Exception:
        return None
    if len(raw) != 20:
        return None
    return raw.hex()


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _excl_db():
    conn = sqlite3.connect(EXCLUDE_PATH, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS excluded (
            infohash TEXT PRIMARY KEY,
            added_at TEXT NOT NULL,
            source TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed (
            infohash TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def _meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    conn.commit()


def _like_escape(word):
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_sql(q, category):
    """Shared keyword/category filter for search + stats.

    Returns (from_sql, where_sql, params). Full-text path
    (2026-11-11): when EVERY keyword is >= 3 chars, use the FTS5
    trigram index over items.title (table items_fts, kept in sync
    by triggers); trigram MATCH with quoted terms is
    substring-equivalent to LIKE for >= 3 chars. Any shorter
    keyword falls back to the LIKE scan. The CROSS JOIN pins the
    join order: the FTS index must drive (a plain JOIN + a
    category filter may scan items and evaluate MATCH per row,
    which takes minutes).
    """
    words = [w for w in (q or "").split() if w][:8]
    use_fts = bool(words) and all(len(w) >= 3 for w in words)
    where = []
    params = []
    from_sql = " FROM items"
    if use_fts:
        match = " AND ".join(
            '"' + w.replace('"', '""') + '"' for w in words)
        from_sql = (" FROM items_fts CROSS JOIN items "
                    "ON items.rowid = items_fts.rowid")
        where.append("items_fts MATCH ?")
        params.append(match)
    else:
        for w in words:
            where.append("title LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(w) + "%")
    if category:
        where.append("category = ?")
        params.append(category)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    return from_sql, wsql, params


def _search(q, category, page, use_exclude):
    from_sql, wsql, params = _filter_sql(q, category)
    excl_join = ""
    conn = _db()
    try:
        if use_exclude and os.path.exists(EXCLUDE_PATH):
            conn.execute("ATTACH DATABASE ? AS excl", (EXCLUDE_PATH,))
            excl_join = (" AND NOT EXISTS (SELECT 1 FROM excl.excluded e "
                         "WHERE e.infohash = items.infohash)")
            if not wsql:
                wsql = " WHERE 1=1"
        total = conn.execute(
            "SELECT COUNT(*)" + from_sql + wsql + excl_join,
            params).fetchone()[0]
        rows = conn.execute(
            "SELECT items.infohash, items.title, items.published_at, "
            "items.published_at_raw, items.size_text, items.category, "
            "items.magnet" + from_sql + wsql + excl_join +
            " ORDER BY items.published_at DESC, items.infohash "
            "LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        cats = [r[0] for r in conn.execute(
            "SELECT category, COUNT(*) c FROM items GROUP BY category "
            "ORDER BY c DESC").fetchall()]
        cats = [ct for ct in cats if ct in KNOWN_CATEGORIES]
        db_total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    items = [{
        "infohash": r["infohash"],
        "title": r["title"],
        "published_at": r["published_at"] or r["published_at_raw"] or "",
        "size_text": r["size_text"] or "",
        "category": r["category"] or "",
        "magnet": r["magnet"],
    } for r in rows]
    return {"total": total, "db_total": db_total, "categories": cats,
            "page": page, "page_size": PAGE_SIZE, "items": items}


def _blacklist_load():
    try:
        with open(EXPORT_BLACKLIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        kws = data.get("keywords")
        if isinstance(kws, list):
            return [str(k).strip() for k in kws if str(k).strip()]
    except (OSError, ValueError):
        pass
    return []


def _blacklist_save(keywords):
    cleaned = []
    seen = set()
    for k in keywords:
        k = str(k).strip()
        if not k or k in seen:
            continue
        seen.add(k)
        cleaned.append(k)
    cleaned = cleaned[:EXPORT_MAX_BLACKLIST]
    tmp = EXPORT_BLACKLIST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"keywords": cleaned,
                   "updated_at": int(time.time())}, f, ensure_ascii=False)
    os.replace(tmp, EXPORT_BLACKLIST_PATH)
    return cleaned


def _processed_count():
    conn = _excl_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
    finally:
        conn.close()


def _processed_add(hashes):
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    conn = _excl_db()
    try:
        added = 0
        for h in hashes:
            if not isinstance(h, str):
                continue
            h = h.strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", h):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed(infohash, processed_at) "
                "VALUES(?,?)", (h, now))
            added += cur.rowcount
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
        return {"added": added, "total": total}
    finally:
        conn.close()


def _export_search(q, limit, date_from, date_to, use_exclude):
    """Export working set: keyword + date-range filter, newest first,
    capped at limit, processed hashes always excluded in SQL, the
    uploaded exclusion set honored when enabled. The keyword
    blacklist is applied in Python after the fetch (bounded set),
    before the browser-side title dedup."""
    from_sql, wsql, params = _filter_sql(q, "")
    if date_from:
        wsql += (" AND " if wsql else " WHERE ")
        wsql += "substr(items.published_at, 1, 10) >= ?"
        params.append(date_from)
    if date_to:
        wsql += (" AND " if wsql else " WHERE ")
        wsql += "substr(items.published_at, 1, 10) <= ?"
        params.append(date_to)
    conn = _db()
    try:
        excl_join = ""
        if os.path.exists(EXCLUDE_PATH):
            conn.execute("ATTACH DATABASE ? AS excl", (EXCLUDE_PATH,))
            if use_exclude:
                excl_join += (" AND NOT EXISTS (SELECT 1 FROM excl.excluded e "
                              "WHERE e.infohash = items.infohash)")
            excl_join += (" AND NOT EXISTS (SELECT 1 FROM excl.processed p "
                          "WHERE p.infohash = items.infohash)")
            if not wsql:
                wsql = " WHERE 1=1"
        total = conn.execute(
            "SELECT COUNT(*)" + from_sql + wsql + excl_join,
            params).fetchone()[0]
        rows = conn.execute(
            "SELECT items.infohash, items.title, items.published_at, "
            "items.published_at_raw, items.size_text, items.size_bytes, "
            "items.category, items.magnet" + from_sql + wsql + excl_join +
            " ORDER BY items.published_at DESC, items.infohash "
            "LIMIT ?",
            params + [limit]).fetchall()
    finally:
        conn.close()
    items = [{
        "infohash": r["infohash"],
        "title": r["title"],
        "published_at": r["published_at"] or r["published_at_raw"] or "",
        "size_text": r["size_text"] or "",
        "size_bytes": r["size_bytes"] if r["size_bytes"] is not None else 0,
        "category": r["category"] or "",
        "magnet": r["magnet"],
    } for r in rows]
    keywords = _blacklist_load()
    folded = [k.casefold() for k in keywords]
    kept = []
    removed = 0
    for it in items:
        t = it["title"].casefold()
        if folded and any(k in t for k in folded):
            removed += 1
        else:
            kept.append(it)
    return {"total": total, "fetched": len(items),
            "blacklist_removed": removed, "items": kept,
            "blacklist_count": len(keywords)}


def _stats_daily(q, category):
    """Daily series for the stats page (no exclusion applied: the
    exclusion set hides search results only; stats measure the
    whole corpus, as stated on the page).

    Buckets by the WRITTEN calendar date (substr of the ISO +08:00
    timestamp = Beijing date). SQLite date() would shift
    early-morning rows to the previous UTC day. Rows without a
    published_at are counted in the totals but cannot appear in
    the series (undated_* fields carry them).
    """
    from_sql, wsql, params = _filter_sql(q, category)
    conn = _db()
    try:
        total, total_bytes, undated_n, undated_b = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(items.size_bytes), 0), "
            "COALESCE(SUM(CASE WHEN items.published_at IS NULL THEN 1 "
            "ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN items.published_at IS NULL THEN "
            "items.size_bytes ELSE 0 END), 0)"
            + from_sql + wsql, params).fetchone()
        dated_where = (wsql + " AND items.published_at IS NOT NULL"
                       if wsql else
                       " WHERE items.published_at IS NOT NULL")
        rows = conn.execute(
            "SELECT substr(items.published_at, 1, 10) AS d, COUNT(*), "
            "COALESCE(SUM(items.size_bytes), 0)"
            + from_sql + dated_where +
            " GROUP BY d ORDER BY d", params).fetchall()
        cats = [r[0] for r in conn.execute(
            "SELECT category, COUNT(*) c FROM items GROUP BY category "
            "ORDER BY c DESC").fetchall()]
        cats = [ct for ct in cats if ct in KNOWN_CATEGORIES]
    finally:
        conn.close()
    days = [{"d": r[0], "n": r[1], "b": r[2]} for r in rows]
    return {"total": total, "total_bytes": total_bytes,
            "undated_count": undated_n, "undated_bytes": undated_b,
            "min_date": days[0]["d"] if days else None,
            "max_date": days[-1]["d"] if days else None,
            "categories": cats, "days": days}


# Stats snapshot cache (UI canon P11: serve settled data first, refresh
# silently). The unfiltered aggregation scans the whole corpus (~4.5s on
# this box) while the data only changes on the 30-minute increment
# cycle, so the unfiltered payload is snapshotted to a JSON file in the
# data dir and served instantly; a stale snapshot is served with
# stale=true and refreshed in a background thread. Filtered queries
# (unbounded combinations) always compute live and still report their
# real elapsed time.
STATS_CACHE_TTL = 30 * 60
STATS_CACHE_PATH = os.path.join(DATA_DIR, "stats_cache.json")
_stats_refreshing = False


def _stats_cache_read():
    try:
        with open(STATS_CACHE_PATH, "r", encoding="utf-8") as f:
            snap = json.load(f)
        if isinstance(snap.get("computed_at"), int) and isinstance(
                snap.get("payload"), dict):
            return snap
    except (OSError, ValueError):
        pass
    return None


def _stats_cache_write(payload, computed_at):
    tmp = STATS_CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"computed_at": computed_at, "payload": payload}, f)
    os.replace(tmp, STATS_CACHE_PATH)


def _stats_refresh_async():
    global _stats_refreshing
    with _lock:
        if _stats_refreshing:
            return
        _stats_refreshing = True

    def _run():
        global _stats_refreshing
        try:
            _stats_cache_write(_stats_daily("", ""), int(time.time()))
        except Exception as exc:  # keep the old snapshot; retry next hit
            print("stats refresh failed: %r" % (exc,), flush=True)
        finally:
            with _lock:
                _stats_refreshing = False
    threading.Thread(target=_run, daemon=True).start()


def _stats_daily_cached():
    """Unfiltered stats via the snapshot cache. Returns the payload plus
    cached/stale/computed_at flags (ok/compute_ms are added by the
    handler so every response reports its own real elapsed time)."""
    now = int(time.time())
    snap = _stats_cache_read()
    if snap is not None:
        stale = (now - snap["computed_at"]) > STATS_CACHE_TTL
        if stale:
            _stats_refresh_async()
        out = dict(snap["payload"])
        out.update({"cached": True, "stale": stale,
                    "computed_at": snap["computed_at"]})
        return out
    payload = _stats_daily("", "")
    try:
        _stats_cache_write(payload, now)
    except OSError:
        pass  # cache is best-effort; the computed payload is still served
    payload.update({"cached": False, "stale": False, "computed_at": now})
    return payload


def _exclude_state():
    conn = _excl_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM excluded").fetchone()[0]
        enabled = _meta_get(conn, "enabled", "1") == "1"
        last = _meta_get(conn, "last_upload")
        return {"count": count, "enabled": enabled,
                "last_upload": json.loads(last) if last else None}
    finally:
        conn.close()


def _exclude_add(lines, filename):
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    total_lines = recognized = added = 0
    conn = _excl_db()
    try:
        for line in lines:
            if not line.strip():
                continue
            total_lines += 1
            h = extract_hash(line)
            if not h:
                continue
            recognized += 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO excluded(infohash, added_at, source) "
                "VALUES(?,?,?)", (h, now, filename))
            added += cur.rowcount
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM excluded").fetchone()[0]
        info = {"filename": filename, "lines": total_lines,
                "recognized": recognized,
                "bad": total_lines - recognized,
                "added": added, "at": now}
        _meta_set(conn, "last_upload", json.dumps(info, ensure_ascii=False))
        return {"count": count, **info}
    finally:
        conn.close()


def _json(start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def _read_json(environ, max_len=65536):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > max_len:
        return {}
    try:
        return json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
    except Exception:
        return {}


def _parse_multipart(environ):
    """Minimal multipart/form-data reader for the single file field
    (stdlib only, mirrors the panel lesson: read the raw body with
    get_data semantics -- here wsgi.input read by CONTENT_LENGTH)."""
    ctype = environ.get("CONTENT_TYPE", "")
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return None, None
    boundary = m.group(1).strip().strip('"').encode()
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > MAX_UPLOAD:
        return None, None
    body = environ["wsgi.input"].read(length)
    for part in body.split(b"--" + boundary):
        if b'name="file"' not in part:
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if not _:
            continue
        fm = re.search(rb'filename="([^"]*)"', head)
        filename = fm.group(1).decode("utf-8", "replace") if fm else ""
        if data.endswith(b"\r\n"):
            data = data[:-2]
        return filename, data
    return None, None


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO") or "/"

    if path == "/" and method == "GET":
        body = PAGE_HTML.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/stats" and method == "GET":
        body = STATS_HTML.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/api/stats/daily" and method == "GET":
        qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        q = (qs.get("q") or [""])[0]
        category = (qs.get("cat") or [""])[0]
        t0 = time.time()
        try:
            if not q and not category:
                out = _stats_daily_cached()
            else:
                out = _stats_daily(q, category)
                out.update({"cached": False, "stale": False,
                            "computed_at": int(t0)})
        except sqlite3.Error as exc:
            return _json(start_response, "500 Internal Server Error",
                         {"ok": False, "message": "数据库未就绪：%s" % exc})
        out.update({"ok": True,
                    "compute_ms": int((time.time() - t0) * 1000)})
        return _json(start_response, "200 OK", out)

    if path == "/api/session" and method == "GET":
        return _json(start_response, "200 OK",
                     {"ok": True, "authenticated": True, "via": "platform",
                      "username": "platform"})

    if path == "/api/search" and method == "GET":
        qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        q = (qs.get("q") or [""])[0]
        category = (qs.get("cat") or [""])[0]
        try:
            page = max(1, int((qs.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        with _lock:
            state = _exclude_state()
        try:
            out = _search(q, category, page, state["enabled"])
        except sqlite3.Error as exc:
            return _json(start_response, "500 Internal Server Error",
                         {"ok": False, "message": "数据库未就绪：%s" % exc})
        out.update({"ok": True, "exclude": state})
        return _json(start_response, "200 OK", out)

    if path == "/api/exclude/state" and method == "GET":
        with _lock:
            return _json(start_response, "200 OK",
                         {"ok": True, **_exclude_state()})

    if path == "/api/exclude/upload" and method == "POST":
        filename, data = _parse_multipart(environ)
        if data is None:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message":
                          "没收到文件（需 .txt，≤16 MB）。"})
        text = data.decode("utf-8", errors="replace")
        with _lock:
            result = _exclude_add(text.splitlines(),
                                  filename or "upload.txt")
            state = _exclude_state()
        return _json(start_response, "200 OK",
                     {"ok": True, "message":
                      "已追加 %(added)d 条（识别 %(recognized)d 行，"
                      "%(bad)d 行没认出来）" % result,
                      "upload": result, **state})

    if path == "/api/exclude/clear" and method == "POST":
        with _lock:
            conn = _excl_db()
            try:
                conn.execute("DELETE FROM excluded")
                conn.commit()
            finally:
                conn.close()
            state = _exclude_state()
        return _json(start_response, "200 OK",
                     {"ok": True, "message": "排除集已清空。", **state})

    if path == "/api/exclude/toggle" and method == "POST":
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        payload = {}
        if 0 < length <= 4096:
            try:
                payload = json.loads(
                    environ["wsgi.input"].read(length).decode("utf-8"))
            except Exception:
                payload = {}
        with _lock:
            conn = _excl_db()
            try:
                cur = _meta_get(conn, "enabled", "1") == "1"
                new = bool(payload.get("enabled", not cur))
                _meta_set(conn, "enabled", "1" if new else "0")
            finally:
                conn.close()
            state = _exclude_state()
        return _json(start_response, "200 OK",
                     {"ok": True, "message":
                      "排除筛选已%s。" % ("启用" if state["enabled"] else "停用"),
                      **state})

    if path == "/export" and method == "GET":
        body = EXPORT_HTML.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/api/export/search" and method == "GET":
        qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        q = (qs.get("q") or [""])[0]
        try:
            limit = int((qs.get("limit") or [""])[0] or EXPORT_DEFAULT_LIMIT)
        except ValueError:
            limit = EXPORT_DEFAULT_LIMIT
        limit = max(1, min(EXPORT_MAX_LIMIT, limit))
        date_from = (qs.get("from") or [""])[0]
        date_to = (qs.get("to") or [""])[0]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_from):
            date_from = ""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_to):
            date_to = ""
        with _lock:
            state = _exclude_state()
            processed_total = _processed_count()
        t0 = time.time()
        try:
            out = _export_search(q, limit, date_from, date_to,
                                 state["enabled"])
        except sqlite3.Error as exc:
            return _json(start_response, "500 Internal Server Error",
                         {"ok": False, "message": "数据库未就绪：%s" % exc})
        out.update({"ok": True, "processed_total": processed_total,
                    "compute_ms": int((time.time() - t0) * 1000)})
        return _json(start_response, "200 OK", out)

    if path == "/api/export/blacklist" and method == "GET":
        with _lock:
            keywords = _blacklist_load()
            processed_total = _processed_count()
        return _json(start_response, "200 OK",
                     {"ok": True, "keywords": keywords,
                      "processed_total": processed_total})

    if path == "/api/export/blacklist" and method == "POST":
        payload = _read_json(environ)
        keywords = payload.get("keywords")
        if not isinstance(keywords, list):
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "keywords 须为列表。"})
        with _lock:
            saved = _blacklist_save(keywords)
        return _json(start_response, "200 OK",
                     {"ok": True, "keywords": saved,
                      "message": "黑名单已保存，共 %d 个关键词，下次搜索生效。"
                      % len(saved)})

    if path == "/api/export/processed/add" and method == "POST":
        payload = _read_json(environ)
        hashes = payload.get("infohashes")
        if not isinstance(hashes, list):
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "infohashes 须为列表。"})
        with _lock:
            result = _processed_add(hashes[:5000])
        return _json(start_response, "200 OK", {"ok": True, **result})

    return _json(start_response, "404 Not Found",
                 {"ok": False, "error": "not_found", "message": "路径不存在。"})


app = application
