# -*- coding: utf-8 -*-
"""xlikes - X 收藏：浏览与标记已下载的 X 喜欢视频/图片。

Rows are written by the debi relay; this page READS rows and UPDATES
ONLY score/tags/preview_count (+ updated_at). It never deletes rows.
No auth in this project: identity verification lives only in the
panel platform layer (App Hub rule); loopback access is trusted.
Stdlib only. Single file: WSGI app + one inline HTML page.

Task sentence (UI canon P1): open this page to browse downloaded
X likes, watch them, and mark scores/tags, so the first screen must
let the user scan the list, open an item, and mark it in place.

Source-adapter design: SOURCES maps a source name to an adapter with
list_items / get_item / set_score / set_tags / bump_preview /
play_url. v1 implements ONLY the X source; a vidlib source may be
added later behind the same interface.

Routes:
  GET  /                page (list / detail / settings views)
  GET  /api/session     {authenticated: true}
  GET  /api/items?source=&kind=&q=&score=&page=&per_page=
  GET  /api/item?source=&tweet_id=&num=
  POST /api/score       JSON {tweet_id, num, score}
  POST /api/tags        JSON {tweet_id, num, tags}
  POST /api/preview     JSON {tweet_id, num}
  GET  /api/play?source=&tweet_id=&num=&which=main|cover
  GET  /api/file?source=&tweet_id=&num=&which=main|cover  (302 to signed URL)
  GET  /api/settings
  POST /api/settings/cookie  JSON {cookie}
  POST /api/settings/push    (re-push the stored cookie to shoc)

Cookie values are NEVER rendered, logged, or echoed in errors: the
page shows only configured yes/no, row count, and push status/time.
The stored cookie file name matches a shadow-sync exclusion pattern
(*secret*), so it never syncs to shoc; delivery to shoc is ONLY via
the dedicated forced-command SSH push.
"""
import datetime
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(BASE, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(BASE, ".runtime")


DATA_DIR = _data_dir()
DB_PATH = os.path.join(DATA_DIR, "xlikes.db")
CRED_PATH = os.path.join(DATA_DIR, "credentials.json")
# Name matches shadow-sync exclusion "*secret*": never synced to shoc.
COOKIE_FILE = os.path.join(DATA_DIR, "x_cookies_secret.txt")
SSH_DIR = os.path.join(DATA_DIR, "ssh")
PUSH_KEY = os.path.join(SSH_DIR, "id_ed25519")
PUSH_KNOWN_HOSTS = os.path.join(SSH_DIR, "known_hosts")
PUSH_STATE_FILE = os.path.join(DATA_DIR, "push_state.json")
SHOC_HOST = os.environ.get("XLIKES_SHOC_HOST", "100.113.251.60")
SHOC_USER = "root"
PUBLIC_BASE = os.environ.get("XLIKES_PUBLIC_BASE", "")

os.makedirs(DATA_DIR, exist_ok=True)

DDL_ITEMS = (
    "CREATE TABLE IF NOT EXISTS items ("
    "tweet_id TEXT NOT NULL, num INTEGER NOT NULL, kind TEXT NOT NULL, "
    "author TEXT, text TEXT, published_at TEXT, duration REAL, "
    "view_count INTEGER, favorite_count INTEGER, retweet_count INTEGER, "
    "reply_count INTEGER, quote_count INTEGER, stem TEXT, gpath TEXT, "
    "cover_gpath TEXT, size INTEGER, score INTEGER DEFAULT 0, "
    "tags TEXT DEFAULT '', preview_count INTEGER DEFAULT 0, "
    "created_at TEXT, updated_at TEXT, PRIMARY KEY (tweet_id, num))"
)

_write_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


with _write_lock:
    _c = _db()
    _c.execute(DDL_ITEMS)
    _c.execute("PRAGMA journal_mode=WAL")
    _c.commit()
    _c.close()


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row):
    d = dict(row)
    d["score"] = d.get("score") or 0
    d["tags"] = d.get("tags") or ""
    d["preview_count"] = d.get("preview_count") or 0
    return d


def _public_item(d):
    """API shape: gpath/cover_gpath never leave the server (P9: the
    page does not need them; media goes through /api/file)."""
    out = dict(d)
    out.pop("gpath", None)
    out.pop("cover_gpath", None)
    out["has_file"] = bool(d.get("gpath"))
    out["has_cover"] = bool(d.get("cover_gpath"))
    return out


def _normalize_tags(raw):
    """Comma list, dy-filter style: split on commas/whitespace, strip
    a leading '#', dedupe preserving order, join with ', '."""
    text = str(raw or "")
    tags = []
    for chunk in re.split(r"[,，、；;\s]+", text):
        tag = chunk.strip().lstrip("#").strip()
        if tag and len(tag) <= 30 and tag not in tags:
            tags.append(tag)
        if len(tags) >= 20:
            break
    return ", ".join(tags)


def _like_escape(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class XAdapter:
    """The X-likes source: SQLite rows + AList-backed playback."""

    def list_items(self, kind="all", q="", score="", page=1, per_page=15):
        t0 = time.monotonic()
        where = []
        args = []
        if kind in ("video", "image"):
            where.append("kind = ?")
            args.append(kind)
        if score in ("0", "1", "2", "3", "4", "5"):
            where.append("COALESCE(score, 0) = ?")
            args.append(int(score))
        terms = [t for t in re.split(r"\s+", (q or "").strip()) if t]
        for term in terms[:5]:
            where.append("(author LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')")
            pat = "%" + _like_escape(term) + "%"
            args.extend([pat, pat])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        order = ("ORDER BY COALESCE(published_at, '') DESC, "
                 "COALESCE(created_at, '') DESC, tweet_id DESC, num ASC")
        with _db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM items " + clause, args).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM items " + clause + " " + order
                + " LIMIT ? OFFSET ?", args + [per_page, (page - 1) * per_page]
            ).fetchall()
        items = [_public_item(_row_to_dict(r)) for r in rows]
        ms = (time.monotonic() - t0) * 1000.0
        return items, total, ms

    def get_item(self, tweet_id, num):
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM items WHERE tweet_id = ? AND num = ?",
                (tweet_id, num)).fetchone()
        return _row_to_dict(row) if row else None

    def set_score(self, tweet_id, num, score):
        with _write_lock:
            with _db() as conn:
                cur = conn.execute(
                    "UPDATE items SET score = ?, updated_at = ? "
                    "WHERE tweet_id = ? AND num = ?",
                    (score, _utcnow(), tweet_id, num))
                conn.commit()
                if cur.rowcount == 0:
                    return None
        return self.get_item(tweet_id, num)

    def set_tags(self, tweet_id, num, tags):
        normalized = _normalize_tags(tags)
        with _write_lock:
            with _db() as conn:
                cur = conn.execute(
                    "UPDATE items SET tags = ?, updated_at = ? "
                    "WHERE tweet_id = ? AND num = ?",
                    (normalized, _utcnow(), tweet_id, num))
                conn.commit()
                if cur.rowcount == 0:
                    return None
        return self.get_item(tweet_id, num)

    def bump_preview(self, tweet_id, num):
        """One increment per playback start (called once per play
        action by the page, never per Range request)."""
        with _write_lock:
            with _db() as conn:
                cur = conn.execute(
                    "UPDATE items SET preview_count = "
                    "COALESCE(preview_count, 0) + 1, updated_at = ? "
                    "WHERE tweet_id = ? AND num = ?",
                    (_utcnow(), tweet_id, num))
                conn.commit()
                if cur.rowcount == 0:
                    return None
        return self.get_item(tweet_id, num)

    def play_url(self, item, which="main"):
        path = item.get("cover_gpath") if which == "cover" else item.get("gpath")
        if not path and which == "cover" and item.get("kind") == "image":
            path = item.get("gpath")
        if not path:
            raise AlistError("缺少文件路径。")
        return _play_url(path)


SOURCES = {"xlikes": XAdapter()}


def _get_source(name):
    return SOURCES.get(name or "xlikes")


# ------------------------------------------------------- credentials
# Import-only credential store (values are never rendered back):
#   alist_base / alist_user / alist_pass - this app -> AList
# Reader tolerates both the flat project convention and the nested
# {"values": {...}} shape.

def _load_creds():
    try:
        with open(CRED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            creds = dict(data)
            nested = data.get("values")
            if isinstance(nested, dict):
                creds.update(nested)
            return creds
    except Exception:
        pass
    return {}


def _cred(key, env_key=None):
    if env_key and os.environ.get(env_key):
        return os.environ[env_key]
    return _load_creds().get(key) or ""


# ------------------------------------------------------------ AList
# Same pattern as vidlib: login -> fs/get -> sign -> /d/ URL through
# Caddy. PUBLIC_BASE defaults to "" so media URLs are same-host
# absolute paths (/d/...) and work under every site hostname.

class AlistError(Exception):
    pass


class AlistClient:
    def __init__(self):
        self._token = ""
        self._token_at = 0.0
        self._lock = threading.Lock()

    def base(self):
        return (_cred("alist_base", "ALIST_BASE")
                or "http://127.0.0.1:5244").rstrip("/")

    def _raw(self, method, path, payload=None, headers=None, timeout=30):
        url = self.base() + path
        data = None
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:
                raise AlistError("AList HTTP %s" % exc.code)
        except Exception as exc:
            raise AlistError("AList 不可达：%s" % str(exc)[:100])

    def login(self, force=False):
        with self._lock:
            if (not force and self._token
                    and time.time() - self._token_at < 40 * 3600):
                return self._token
            user = _cred("alist_user", "ALIST_USER") or "admin"
            password = _cred("alist_pass", "ALIST_PASS")
            if not password:
                raise AlistError("尚未配置 AList 管理员密码。")
            out = self._raw("POST", "/api/auth/login",
                            {"username": user, "password": password})
            token = (out.get("data") or {}).get("token") or ""
            if not token:
                raise AlistError("AList 登录失败：%s"
                                 % (out.get("message") or "未知错误"))
            self._token = token
            self._token_at = time.time()
            return token

    def api(self, method, path, payload=None, timeout=30):
        token = self.login()
        out = self._raw(method, path, payload,
                        headers={"Authorization": token}, timeout=timeout)
        if out.get("code") == 401:
            token = self.login(force=True)
            out = self._raw(method, path, payload,
                            headers={"Authorization": token},
                            timeout=timeout)
        if out.get("code") not in (200, 0, None):
            raise AlistError("AList %s：%s"
                             % (path, out.get("message") or out.get("code")))
        return out.get("data")

    def fs_get(self, path):
        return self.api("POST", "/api/fs/get", {"path": path}) or {}

    def fs_list(self, path):
        data = self.api("POST", "/api/fs/list",
                        {"path": path, "page": 1, "per_page": 0,
                         "refresh": True}) or {}
        return data.get("content") or []


ALIST = AlistClient()


def _signed_url_raw(gpath):
    try:
        info = ALIST.fs_get(gpath)
    except AlistError as exc:
        if "not found" not in str(exc):
            raise
        # AList resolves paths through its cached folder listing,
        # which lags fresh uploads by minutes: refresh the parent
        # listing once and retry before giving up (vidlib pattern).
        parent = (gpath or "").rpartition("/")[0] or "/"
        try:
            ALIST.fs_list(parent)
        except Exception:
            pass
        info = ALIST.fs_get(gpath)
    return (info or {}).get("raw_url") or "", (info or {}).get("sign") or ""


def _resolve_play_url(gpath):
    """Browser-usable URL for a Drive file: prefer AList's raw_url
    when it already points at a public host (Google direct link);
    otherwise rewrite onto the same-host /d/ route (Caddy forwards
    /d/ and /p/ to loopback AList)."""
    p = "/" + (gpath or "").strip().lstrip("/")
    raw, sign = _signed_url_raw(p)
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        if parsed.hostname not in ("127.0.0.1", "localhost"):
            return raw
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
        return PUBLIC_BASE + path
    if raw.startswith("/"):
        return PUBLIC_BASE + raw
    if sign:
        return (PUBLIC_BASE + "/d" + p
                + "?sign=" + urllib.parse.quote(sign))
    return PUBLIC_BASE + "/d" + p


_play_url_cache = {}
_play_url_lock = threading.Lock()


def _play_url(gpath):
    """_resolve_play_url with a short in-process cache and retry:
    a transient AList/Drive hiccup (seconds) must not surface as
    a playback failure. Raises only after 3 failed attempts."""
    key = "/" + (gpath or "").strip().lstrip("/")
    with _play_url_lock:
        hit = _play_url_cache.get(key)
        if hit and time.time() - hit[1] < 600:
            return hit[0]
    last = None
    for attempt in range(3):
        try:
            url = _resolve_play_url(key)
            with _play_url_lock:
                _play_url_cache[key] = (url, time.time())
            return url
        except Exception as exc:
            last = exc
            print("xlikes: play url resolve failed %s attempt %d: %s"
                  % (key[:80], attempt + 1, str(exc)[:150]), flush=True)
            if attempt < 2:
                time.sleep(1.2)
    raise last


# --------------------------------------------------- X cookie input
# Accepts Netscape cookies.txt AND header-style "name=value; ..."
# input, mirroring yt-audio's _header_cookie_to_netscape converter.
# REQUIRE auth_token + ct0. Values are never logged or echoed.

_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")
_X_KEY_FIELDS = ("auth_token", "ct0")
_COOKIE_MAX_BYTES = 64 * 1024


def _is_x_domain(dom):
    dom = (dom or "").strip().lstrip(".").lower()
    return dom in ("x.com", "twitter.com") or dom.endswith(
        (".x.com", ".twitter.com"))


def _header_cookie_to_netscape(text):
    """Convert a raw Cookie header string into Netscape cookies.txt
    lines for .x.com and .twitter.com. Returns the converted text,
    or None when the input does not parse as a header string with
    the required login fields."""
    body = text.strip()
    if body.lower().startswith("cookie:"):
        body = body.split(":", 1)[1]
    pairs = {}
    for chunk in re.split(r"[;\n]", body):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name, value = name.strip(), value.strip()
        if value and _COOKIE_NAME_RE.match(name):
            pairs[name] = value
    if len(pairs) < 3:
        return None
    if not all(k in pairs for k in _X_KEY_FIELDS):
        return None
    lines = ["# Netscape HTTP Cookie File",
             "# converted from a raw Cookie header string"]
    for domain in (".x.com", ".twitter.com"):
        for name in sorted(pairs):
            lines.append("%s\tTRUE\t/\tTRUE\t1893456000\t%s\t%s"
                         % (domain, name, pairs[name]))
    return "\n".join(lines) + "\n"


def _parse_cookie_input(content):
    """Validate pasted input. Returns (netscape_text, row_count,
    note). Raises ValueError with a clear message otherwise;
    nothing is stored or pushed on failure."""
    text = (content or "").strip()
    if len(text) < 20:
        raise ValueError("Cookie 内容太短，请粘贴完整的 cookies.txt 或 Cookie 串。")
    if len(text.encode("utf-8")) > _COOKIE_MAX_BYTES:
        raise ValueError("Cookie 内容超过 64 KB，无法推送到下载机；请只保留 x.com / twitter.com 的条目后重试。")
    note = ""
    if "\t" not in text or ("x.com" not in text and "twitter.com" not in text):
        converted = _header_cookie_to_netscape(text)
        if converted is not None:
            text, note = converted, "已把浏览器 Cookie 串转换为 cookies.txt 格式保存。"
        else:
            raise ValueError(
                "无法识别为 X 的 Cookie：请粘贴 x.com / twitter.com 的 "
                "cookies.txt，或浏览器请求头里的 Cookie 串（须包含 "
                "auth_token 与 ct0）。")
    names = set()
    rows = 0
    has_x_domain = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        rows += 1
        names.add(parts[5].strip())
        if _is_x_domain(parts[0]):
            has_x_domain = True
    if rows == 0:
        raise ValueError("没有读到有效的 Cookie 条目。")
    if not has_x_domain:
        raise ValueError("这不像 X 的 Cookie（缺少 x.com / twitter.com 条目）。")
    missing = [k for k in _X_KEY_FIELDS if k not in names]
    if missing:
        raise ValueError("Cookie 里缺少登录关键字段 %s，可能没有登录或导错了站点。"
                         % "、".join(missing))
    if not text.endswith("\n"):
        text += "\n"
    return text, rows, note


def _count_cookie_rows():
    try:
        with open(COOKIE_FILE, encoding="utf-8") as fh:
            rows = 0
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "\t" in line:
                    rows += 1
            return rows
    except OSError:
        return 0


def _save_cookie_file(text):
    tmp = COOKIE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, COOKIE_FILE)


def _read_push_state():
    try:
        with open(PUSH_STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"last_push_at": None, "status": "never", "detail": ""}


def _write_push_state(status, detail=""):
    state = {"last_push_at": _utcnow(), "status": status,
             "detail": (detail or "")[:200]}
    tmp = PUSH_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, PUSH_STATE_FILE)
    return state


def _push_cookie(cookie_bytes):
    """Push cookie bytes to shoc over the dedicated forced-command
    SSH channel. Returns (ok, status, detail). Detail comes only
    from ssh/receiver status output, never from cookie bytes."""
    if not os.path.isfile(PUSH_KEY) or not os.path.isfile(PUSH_KNOWN_HOSTS):
        return False, "failed", "推送密钥未配置。"
    cmd = ["ssh", "-i", PUSH_KEY,
           "-o", "IdentitiesOnly=yes",
           "-o", "StrictHostKeyChecking=yes",
           "-o", "UserKnownHostsFile=" + PUSH_KNOWN_HOSTS,
           "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=15",
           "%s@%s" % (SHOC_USER, SHOC_HOST)]
    try:
        proc = subprocess.run(cmd, input=cookie_bytes,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "failed", "推送超时（30 秒无响应）。"
    except Exception as exc:
        return False, "failed", "推送执行失败：%s" % str(exc)[:100]
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = lines[0][:200] if lines else "ssh 退出码 %d" % proc.returncode
        return False, "failed", detail
    return True, "ok", ""


def _do_push_stored_cookie():
    """Push the stored cookie file to shoc; record push state.
    Returns (state_dict, pushed_bool)."""
    try:
        with open(COOKIE_FILE, "rb") as fh:
            blob = fh.read()
    except OSError:
        return _read_push_state(), False
    if not blob or len(blob) > _COOKIE_MAX_BYTES:
        state = _write_push_state("failed", "本地 Cookie 文件异常，拒绝推送。")
        return state, False
    ok, status, detail = _push_cookie(blob)
    state = _write_push_state(status, detail)
    return state, ok


# ------------------------------------------------------------- page
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>X 收藏</title>
<style>
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6b6660; --card: #f6f5f2;
  --line: #e4e1db; --accent: #d30303; --badge-bg: #fdecec;
  --btn-bg: #1a1a1a; --btn-fg: #ffffff; --input-bg: #ffffff;
  --ok: #1a7f37; --warn-bg: #fff8e6; --warn-line: #e8a13a;
  --chip-bg: #efede8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121212; --fg: #e8e6e3; --muted: #a39f98; --card: #1d1d1b;
    --line: #2e2c29; --accent: #ff5a5a; --badge-bg: #3a1d1d;
    --btn-bg: #e8e6e3; --btn-fg: #121212; --input-bg: #1d1d1b;
    --ok: #4cc38a; --warn-bg: #2e250f; --warn-line: #e8a13a;
    --chip-bg: #2a2825;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 16px; line-height: 1.5; }
.wrap { max-width: 880px; margin: 0 auto; padding: 16px 16px 48px; }
.topbar { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
  border-bottom: 1px solid var(--line); padding-bottom: 10px; margin-bottom: 12px; }
.topbar h1 { font-size: 22px; margin: 0; }
.topbar nav { margin-left: auto; display: flex; gap: 4px; }
.topbar nav a { color: var(--fg); text-decoration: none; padding: 8px 12px;
  border-radius: 8px; min-height: 44px; display: inline-flex; align-items: center; }
.topbar nav a.active { background: var(--card); font-weight: 700; }
.banner { background: var(--warn-bg); border: 1px solid var(--warn-line);
  border-radius: 10px; padding: 12px 14px; margin: 0 0 14px; }
.banner b { font-size: 16px; }
.banner .act { margin-top: 8px; }
.banner a { color: var(--accent); font-weight: 600; }
.filters { display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
  margin-bottom: 6px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 10px;
  overflow: hidden; }
.seg button { border: 0; background: transparent; color: var(--fg);
  padding: 10px 14px; min-height: 44px; font-size: 15px; cursor: pointer; }
.seg button.on { background: var(--btn-bg); color: var(--btn-fg); font-weight: 700; }
.filters input[type="search"], .filters select {
  background: var(--input-bg); color: var(--fg); border: 1px solid var(--line);
  border-radius: 10px; padding: 10px 12px; min-height: 44px; font-size: 15px; }
.filters input[type="search"] { flex: 1 1 200px; min-width: 160px; }
.countline { color: var(--muted); font-size: 14px; margin: 6px 0 12px; }
.list { display: flex; flex-direction: column; gap: 10px; }
.item { display: flex; gap: 12px; background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 12px; cursor: pointer; text-align: left; width: 100%;
  color: var(--fg); font: inherit; }
.item:hover { border-color: var(--muted); }
.item .thumb { width: 96px; height: 72px; object-fit: cover; border-radius: 8px;
  background: var(--chip-bg); flex: none; }
.item .thumbwrap { width: 96px; height: 72px; border-radius: 8px; flex: none;
  background: var(--chip-bg); display: flex; align-items: center;
  justify-content: center; color: var(--muted); font-size: 13px; }
.item .body { flex: 1; min-width: 0; }
.item .topline { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.item .author { font-weight: 700; }
.item .date { color: var(--muted); font-size: 14px; }
.badge { display: inline-block; background: var(--badge-bg); color: var(--accent);
  border-radius: 6px; padding: 1px 7px; font-size: 13px; font-weight: 600; }
.item .excerpt { margin: 4px 0 0; color: var(--fg); overflow: hidden;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.item .metaline { margin-top: 6px; color: var(--muted); font-size: 14px;
  display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
.item .metaline .score { color: var(--accent); font-weight: 700; }
.chip { display: inline-block; background: var(--chip-bg); border-radius: 6px;
  padding: 1px 7px; font-size: 13px; color: var(--fg); }
.empty { border: 1px dashed var(--line); border-radius: 12px; padding: 32px 16px;
  text-align: center; color: var(--muted); }
.empty b { display: block; color: var(--fg); font-size: 17px; margin-bottom: 6px; }
.empty .act { margin-top: 14px; }
.pager { display: flex; gap: 10px; align-items: center; justify-content: center;
  margin-top: 16px; flex-wrap: wrap; }
.pager .info { color: var(--muted); font-size: 14px; }
button, .btn { font: inherit; }
.btn { background: var(--btn-bg); color: var(--btn-fg); border: 0; border-radius: 10px;
  padding: 10px 18px; min-height: 44px; cursor: pointer; font-size: 15px;
  text-decoration: none; display: inline-flex; align-items: center; }
.btn.ghost { background: transparent; color: var(--fg); border: 1px solid var(--line); }
.btn:disabled { opacity: 0.5; cursor: default; }
.btn.danger { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
.backlink { background: none; border: 0; color: var(--accent); font-size: 15px;
  cursor: pointer; padding: 10px 0; min-height: 44px; }
.player { background: #000; border-radius: 12px; overflow: hidden; }
.player video, .player img { display: block; width: 100%; max-height: 70vh;
  object-fit: contain; background: #000; }
.detail h2 { font-size: 18px; margin: 14px 0 4px; }
.detail .sub { color: var(--muted); font-size: 14px; }
.detail .text { white-space: pre-wrap; word-break: break-word;
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px; margin: 12px 0; }
.detail .counts { color: var(--muted); font-size: 14px; display: flex;
  gap: 14px; flex-wrap: wrap; margin: 8px 0; }
.mark { border: 1px solid var(--line); border-radius: 12px; padding: 14px;
  margin-top: 14px; }
.mark h3 { margin: 0 0 10px; font-size: 16px; }
.scorebtns { display: flex; gap: 8px; flex-wrap: wrap; }
.scorebtns button { min-width: 44px; min-height: 44px; border-radius: 10px;
  border: 1px solid var(--line); background: var(--input-bg); color: var(--fg);
  font-size: 15px; cursor: pointer; padding: 8px 10px; }
.scorebtns button.on { background: var(--btn-bg); color: var(--btn-fg);
  font-weight: 700; border-color: var(--btn-bg); }
.tagrow { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
.tagrow input { flex: 1 1 220px; background: var(--input-bg); color: var(--fg);
  border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
  min-height: 44px; font-size: 15px; }
.saved { color: var(--ok); font-size: 14px; margin-top: 8px; min-height: 20px; }
.err { color: var(--accent); font-size: 14px; margin-top: 8px; }
.card { border: 1px solid var(--line); border-radius: 12px; padding: 14px;
  margin-bottom: 14px; background: var(--card); }
.card h3 { margin: 0 0 8px; font-size: 16px; }
.kv { display: flex; gap: 8px; flex-wrap: wrap; font-size: 15px; }
.kv .k { color: var(--muted); }
.statgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 10px; margin-top: 8px; }
.statgrid .stat { background: var(--input-bg); border: 1px solid var(--line);
  border-radius: 10px; padding: 10px 12px; }
.statgrid .stat .n { font-size: 22px; font-weight: 700; }
.statgrid .stat .l { color: var(--muted); font-size: 13px; }
textarea { width: 100%; min-height: 140px; background: var(--input-bg);
  color: var(--fg); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 12px; font-size: 14px; font-family: ui-monospace, monospace; }
.hint { color: var(--muted); font-size: 14px; margin: 8px 0; }
.prog { display: none; margin-top: 10px; }
.prog .bar { height: 6px; border-radius: 3px; background: var(--line);
  overflow: hidden; }
.prog .bar i { display: block; height: 100%; width: 40%; background: var(--accent);
  border-radius: 3px; animation: slide 1s linear infinite; }
@keyframes slide { from { margin-left: -40%; } to { margin-left: 100%; } }
.prog .t { color: var(--muted); font-size: 14px; margin-top: 4px; }
.foot { color: var(--muted); font-size: 13px; margin-top: 20px; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1>X 收藏</h1>
    <nav>
      <a href="#/" id="navList">列表</a>
      <a href="#/settings" id="navSettings">设置</a>
    </nav>
  </div>
  <div id="banner" class="banner" style="display:none"></div>

  <section id="view-list">
    <div class="filters">
      <div class="seg" role="group" aria-label="类型">
        <button data-kind="all" class="on">全部</button><button data-kind="video">视频</button><button data-kind="image">图片</button>
      </div>
      <input type="search" id="q" placeholder="搜索作者或正文" autocomplete="off">
      <select id="scoreSel" aria-label="评分筛选">
        <option value="">全部评分</option>
        <option value="0">未评分</option>
        <option value="5">5 分</option>
        <option value="4">4 分</option>
        <option value="3">3 分</option>
        <option value="2">2 分</option>
        <option value="1">1 分</option>
      </select>
    </div>
    <div class="countline" id="countline"></div>
    <div class="list" id="list"></div>
    <div class="pager" id="pager" style="display:none">
      <button class="btn ghost" id="prevPage">上一页</button>
      <span class="info" id="pageInfo"></span>
      <button class="btn ghost" id="nextPage">下一页</button>
    </div>
    <div class="foot" id="listFoot"></div>
  </section>

  <section id="view-detail" style="display:none">
    <button class="backlink" id="backBtn">← 返回列表</button>
    <div class="detail" id="detail"></div>
  </section>

  <section id="view-settings" style="display:none">
    <div class="card">
      <h3>X Cookie</h3>
      <div id="cookieStatus"><span class="hint">加载中…</span></div>
      <div id="pushRow" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <h3>粘贴 Cookie</h3>
      <p class="hint">两种格式都可以：浏览器扩展导出的 cookies.txt，或浏览器请求头里的 Cookie 串（name=value; name=value 形式），后者会自动转换。须包含 auth_token 与 ct0。保存后立即推送到下载机；Cookie 值不会在页面上回显。</p>
      <textarea id="cookieInput" placeholder="在此粘贴 cookies.txt 或 Cookie 串…" spellcheck="false"></textarea>
      <div style="margin-top:10px; display:flex; gap:8px; flex-wrap:wrap">
        <button class="btn" id="saveCookieBtn">保存并推送</button>
        <button class="btn ghost" id="repushBtn">重新推送已保存的 Cookie</button>
      </div>
      <div class="prog" id="saveProg"><div class="bar"><i></i></div><div class="t" id="saveProgText"></div></div>
      <div class="saved" id="cookieMsg"></div>
      <div class="err" id="cookieErr"></div>
    </div>
    <div class="card">
      <h3>数据库</h3>
      <div class="statgrid" id="dbStats"></div>
      <p class="hint" id="syncInfo"></p>
    </div>
  </section>
</div>
"""

PAGE_HTML += r"""<script>
(function () {
"use strict";
var BASE = location.pathname;
if (BASE.slice(-1) !== "/") BASE += "/";
function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
  });
}
function fmtDate(s) {
  if (!s) return "";
  return String(s).replace("T", " ").slice(0, 16);
}
function fmtDur(sec) {
  if (sec == null || isNaN(sec)) return "";
  sec = Math.round(sec);
  var m = Math.floor(sec / 60), r = sec % 60;
  return m + ":" + (r < 10 ? "0" : "") + r;
}
function fmtSize(n) {
  if (n == null || isNaN(n)) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
function stars(n) {
  n = n || 0;
  if (!n) return "未评分";
  var s = "";
  for (var i = 0; i < n; i++) s += "★";
  return s + " " + n + " 分";
}
function chipHtml(tags) {
  var list = String(tags || "").split(",").map(function (t) { return t.trim(); })
    .filter(function (t) { return t; });
  if (!list.length) return "";
  var shown = list.slice(0, 3).map(function (t) {
    return '<span class="chip">' + esc(t) + "</span>";
  }).join(" ");
  if (list.length > 3) shown += ' <span class="chip">+' + (list.length - 3) + "</span>";
  return shown;
}
function jget(url) {
  return fetch(BASE + url).then(function (r) {
    return r.json().then(function (d) {
      if (!r.ok) throw new Error((d && d.error) || ("请求失败 " + r.status));
      return d;
    });
  });
}
function jpost(url, obj) {
  return fetch(BASE + url, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj) }).then(function (r) {
    return r.json().then(function (d) {
      if (!r.ok) throw new Error((d && d.error) || ("请求失败 " + r.status));
      return d;
    });
  });
}

var state = { kind: "all", q: "", score: "", page: 1, perPage: 15, total: 0 };
var previewSent = {};

function showView(name) {
  $("view-list").style.display = name === "list" ? "" : "none";
  $("view-detail").style.display = name === "detail" ? "" : "none";
  $("view-settings").style.display = name === "settings" ? "" : "none";
  $("navList").className = name === "list" || name === "detail" ? "active" : "";
  $("navSettings").className = name === "settings" ? "active" : "";
  window.scrollTo(0, 0);
}

function router() {
  var h = (location.hash || "#/").replace(/^#\//, "");
  var parts = h.split("/");
  if (parts[0] === "settings") { showView("settings"); loadSettings(); return; }
  if (parts[0] === "item" && parts[1]) {
    showView("detail");
    loadDetail(decodeURIComponent(parts[1]), parseInt(parts[2] || "0", 10) || 0);
    return;
  }
  showView("list");
  loadStatus();
  loadList();
}

function loadStatus() {
  jget("api/settings").then(function (d) {
    var b = $("banner");
    var c = d.cookie || {}, p = d.push || {};
    if (!c.configured) {
      b.style.display = "";
      b.innerHTML = "<b>尚未配置 X Cookie</b><br>下载机没有登录凭据，无法拉取新的喜欢内容。" +
        '<div class="act"><a href="#/settings">去设置 →</a></div>';
    } else if (p.status === "failed") {
      b.style.display = "";
      b.innerHTML = "<b>上次 Cookie 推送失败</b>（" + esc(fmtDate(p.last_push_at)) +
        "）：" + esc(p.detail || "") +
        '<div class="act"><a href="#/settings">去设置重试 →</a></div>';
    } else {
      b.style.display = "none";
    }
  }).catch(function () { $("banner").style.display = "none"; });
}

function filterEcho() {
  var bits = [];
  if (state.kind === "video") bits.push("视频");
  if (state.kind === "image") bits.push("图片");
  if (state.q) bits.push("含“" + state.q + "”");
  if (state.score === "0") bits.push("未评分");
  else if (state.score) bits.push(state.score + " 分");
  return bits.join(" · ");
}

function loadList() {
  var listEl = $("list");
  listEl.innerHTML = '<div class="empty"><b>加载中…</b></div>';
  $("pager").style.display = "none";
  $("countline").textContent = "";
  $("listFoot").textContent = "";
  var qs = "api/items?kind=" + encodeURIComponent(state.kind) +
    "&q=" + encodeURIComponent(state.q) +
    "&score=" + encodeURIComponent(state.score) +
    "&page=" + state.page + "&per_page=" + state.perPage;
  jget(qs).then(function (d) {
    state.total = d.total || 0;
    var echo = filterEcho();
    $("countline").textContent = "共 " + state.total + " 条" + (echo ? " · " + echo : "");
    $("listFoot").textContent = "实时查询 · 用时 " + Math.round(d.ms || 0) + " 毫秒";
    renderList(d.items || []);
    var pages = Math.max(1, Math.ceil(state.total / state.perPage));
    if (state.total > 0) {
      $("pager").style.display = "";
      $("pageInfo").textContent = "第 " + state.page + " / " + pages + " 页";
      $("prevPage").disabled = state.page <= 1;
      $("nextPage").disabled = state.page >= pages;
    }
  }).catch(function (e) {
    listEl.innerHTML = '<div class="empty"><b>加载失败</b>' + esc(e.message) +
      '<div class="act"><button class="btn" id="retryBtn">重试</button></div></div>';
    $("retryBtn").addEventListener("click", loadList);
  });
}

function renderList(items) {
  var listEl = $("list");
  if (!items.length) {
    if (state.total === 0 && !filterEcho()) {
      listEl.innerHTML = '<div class="empty"><b>还没有收藏内容</b>' +
        "下载机同步后，已下载的 X 喜欢视频与图片会出现在这里。" +
        '<div class="act"><a class="btn" href="#/settings">先去设置 X Cookie</a></div></div>';
    } else {
      listEl.innerHTML = '<div class="empty"><b>没有符合条件的内容</b>' +
        "当前筛选：" + esc(filterEcho() || "无") +
        '<div class="act"><button class="btn" id="clearFilterBtn">清除筛选</button></div></div>';
      $("clearFilterBtn").addEventListener("click", clearFilters);
    }
    return;
  }
  listEl.innerHTML = items.map(function (it) {
    var key = it.tweet_id + "/" + it.num;
    var thumb = "";
    if (it.has_cover || it.kind === "image") {
      thumb = '<img class="thumb" loading="lazy" alt="" src="' + BASE +
        "api/file?tweet_id=" + encodeURIComponent(it.tweet_id) +
        "&num=" + it.num + '&which=cover">';
    } else {
      thumb = '<span class="thumbwrap">' + (it.kind === "video" ? "视频" : "图片") + "</span>";
    }
    return '<button class="item" data-key="' + esc(key) + '">' + thumb +
      '<span class="body"><span class="topline">' +
      '<span class="badge">' + (it.kind === "video" ? "视频" : "图片") + "</span>" +
      '<span class="author">' + esc(it.author || "未知作者") + "</span>" +
      '<span class="date">' + esc(fmtDate(it.published_at)) + "</span></span>" +
      '<span class="excerpt">' + esc(it.text || "") + "</span>" +
      '<span class="metaline"><span class="score">' + esc(stars(it.score)) + "</span>" +
      "<span>看过 " + (it.preview_count || 0) + " 次</span>" +
      chipHtml(it.tags) + "</span></span></button>";
  }).join("");
  listEl.querySelectorAll(".item").forEach(function (el) {
    el.addEventListener("click", function () {
      location.hash = "#/item/" + encodeURIComponent(el.getAttribute("data-key").split("/")[0]) +
        "/" + el.getAttribute("data-key").split("/")[1];
    });
  });
  listEl.querySelectorAll("img.thumb").forEach(function (img) {
    img.addEventListener("error", function () {
      var s = document.createElement("span");
      s.className = "thumbwrap";
      s.textContent = "无封面";
      img.replaceWith(s);
    });
  });
}

function clearFilters() {
  state.kind = "all"; state.q = ""; state.score = ""; state.page = 1;
  $("q").value = "";
  $("scoreSel").value = "";
  document.querySelectorAll(".seg button").forEach(function (b) {
    b.className = b.getAttribute("data-kind") === "all" ? "on" : "";
  });
  loadList();
}

function loadDetail(tweetId, num) {
  delete previewSent[tweetId + "/" + num];
  var box = $("detail");
  box.innerHTML = '<div class="empty"><b>加载中…</b></div>';
  jget("api/item?tweet_id=" + encodeURIComponent(tweetId) + "&num=" + num).then(function (d) {
    renderDetail(d.item);
  }).catch(function (e) {
    box.innerHTML = '<div class="empty"><b>这条内容打不开了</b>' + esc(e.message) +
      '<div class="act"><a class="btn" href="#/">返回列表</a></div></div>';
  });
}

function mediaUrl(it, which) {
  return BASE + "api/file?tweet_id=" + encodeURIComponent(it.tweet_id) +
    "&num=" + it.num + "&which=" + which;
}

function renderDetail(it) {
  var box = $("detail");
  var key = it.tweet_id + "/" + it.num;
  var media = "";
  if (!it.has_file) {
    media = '<div class="empty"><b>文件尚未就绪</b>这条记录还没有对应的下载文件。</div>';
  } else if (it.kind === "video") {
    media = '<div class="player"><video id="playerEl" controls playsinline preload="metadata"' +
      (it.has_cover ? ' poster="' + mediaUrl(it, "cover") + '"' : "") +
      ' src="' + mediaUrl(it, "main") + '"></video></div>';
  } else {
    media = '<div class="player"><img id="playerEl" alt="" src="' + mediaUrl(it, "main") + '"></div>';
  }
  var counts = [];
  if (it.view_count != null) counts.push("播放 " + it.view_count);
  if (it.favorite_count != null) counts.push("喜欢 " + it.favorite_count);
  if (it.retweet_count != null) counts.push("转推 " + it.retweet_count);
  if (it.reply_count != null) counts.push("回复 " + it.reply_count);
  if (it.quote_count != null) counts.push("引用 " + it.quote_count);
  if (it.duration) counts.push("时长 " + fmtDur(it.duration));
  if (it.size) counts.push(fmtSize(it.size));
  var scoreBtns = "";
  for (var s = 0; s <= 5; s++) {
    scoreBtns += '<button data-score="' + s + '"' + (it.score === s ? ' class="on"' : "") +
      ">" + (s === 0 ? "清除" : s + " 分") + "</button>";
  }
  box.innerHTML = media +
    "<h2>" + esc(it.author || "未知作者") + "</h2>" +
    '<div class="sub">' + esc(fmtDate(it.published_at)) +
    ' · 看过 <span id="pvCount">' + (it.preview_count || 0) + "</span> 次" +
    ' · <a href="https://x.com/i/status/' + encodeURIComponent(it.tweet_id) +
    '" target="_blank" rel="noopener">在 X 上查看原帖 ↗</a></div>' +
    (counts.length ? '<div class="counts">' + counts.map(esc).join("<span></span>") + "</div>" : "") +
    (it.text ? '<div class="text">' + esc(it.text) + "</div>" : "") +
    '<div class="mark"><h3>标记</h3><div class="scorebtns">' + scoreBtns + "</div>" +
    '<div class="tagrow"><input id="tagInput" placeholder="标签，逗号分隔，如：风景, 收藏" value="' +
    esc(it.tags || "") + '">' +
    '<button class="btn" id="saveTagsBtn">保存标签</button></div>' +
    '<div id="tagChips">' + chipHtml(it.tags) + "</div>" +
    '<div class="saved" id="markMsg"></div><div class="err" id="markErr"></div></div>';

  var playerEl = $("playerEl");
  if (playerEl && !previewSent[key]) {
    var bump = function () {
      if (previewSent[key]) return;
      previewSent[key] = true;
      jpost("api/preview", { tweet_id: it.tweet_id, num: it.num }).then(function (d) {
        if (d.item) $("pvCount").textContent = d.item.preview_count;
      }).catch(function () { previewSent[key] = false; });
    };
    if (it.kind === "video") playerEl.addEventListener("playing", bump, { once: true });
    else playerEl.addEventListener("load", bump, { once: true });
  }
  box.querySelectorAll(".scorebtns button").forEach(function (b) {
    b.addEventListener("click", function () {
      var sc = parseInt(b.getAttribute("data-score"), 10);
      $("markErr").textContent = "";
      jpost("api/score", { tweet_id: it.tweet_id, num: it.num, score: sc }).then(function (d) {
        box.querySelectorAll(".scorebtns button").forEach(function (x) {
          x.className = parseInt(x.getAttribute("data-score"), 10) === d.item.score ? "on" : "";
        });
        $("markMsg").textContent = "评分已保存：" + stars(d.item.score);
      }).catch(function (e) { $("markErr").textContent = "保存失败：" + e.message; });
    });
  });
  $("saveTagsBtn").addEventListener("click", function () {
    $("markErr").textContent = "";
    jpost("api/tags", { tweet_id: it.tweet_id, num: it.num, tags: $("tagInput").value }).then(function (d) {
      $("tagInput").value = d.item.tags;
      $("tagChips").innerHTML = chipHtml(d.item.tags);
      $("markMsg").textContent = "标签已保存。";
    }).catch(function (e) { $("markErr").textContent = "保存失败：" + e.message; });
  });
}

function loadSettings() {
  jget("api/settings").then(function (d) {
    var c = d.cookie || {}, p = d.push || {}, db = d.db || {};
    var status = c.configured
      ? "已配置 · " + c.count + " 条 Cookie"
      : "未配置";
    var pushText = "尚未推送";
    if (p.status === "ok") pushText = "推送成功 · " + fmtDate(p.last_push_at);
    else if (p.status === "failed") pushText = "推送失败 · " + fmtDate(p.last_push_at) +
      (p.detail ? " · " + p.detail : "");
    $("cookieStatus").innerHTML =
      '<div class="kv"><span class="k">状态：</span><span>' + esc(status) + "</span></div>" +
      '<div class="kv"><span class="k">推送：</span><span>' + esc(pushText) + "</span></div>";
    $("dbStats").innerHTML =
      '<div class="stat"><div class="n">' + (db.total || 0) + '</div><div class="l">全部</div></div>' +
      '<div class="stat"><div class="n">' + (db.video || 0) + '</div><div class="l">视频</div></div>' +
      '<div class="stat"><div class="n">' + (db.image || 0) + '</div><div class="l">图片</div></div>' +
      '<div class="stat"><div class="n">' + (db.unrated || 0) + '</div><div class="l">未评分</div></div>';
    $("syncInfo").textContent = "最新入库：" + (fmtDate(db.newest_created_at) || "无") +
      " · 最新发布：" + (fmtDate(db.newest_published_at) || "无");
  }).catch(function (e) {
    $("cookieStatus").innerHTML = '<span class="err">加载失败：' + esc(e.message) + "</span>";
  });
}

function saveCookie() {
  var text = $("cookieInput").value;
  var btn = $("saveCookieBtn");
  $("cookieMsg").textContent = "";
  $("cookieErr").textContent = "";
  if (!text.trim()) { $("cookieErr").textContent = "请先粘贴 Cookie 内容。"; return; }
  btn.disabled = true;
  var prog = $("saveProg"), progText = $("saveProgText");
  prog.style.display = "block";
  var t0 = Date.now(), shown = false;
  var timer = setInterval(function () {
    progText.textContent = "保存并推送中 · " + ((Date.now() - t0) / 1000).toFixed(1) + " 秒";
    shown = true;
  }, 100);
  progText.textContent = "保存并推送中…";
  jpost("api/settings/cookie", { cookie: text }).then(function (d) {
    clearInterval(timer);
    var secs = ((Date.now() - t0) / 1000).toFixed(1);
    prog.style.display = "none";
    btn.disabled = false;
    $("cookieInput").value = "";
    var msg = "已保存 " + d.count + " 条 Cookie · 用时 " + secs + " 秒";
    if (d.note) msg += " · " + d.note;
    if (d.pushed) msg += " · 已推送到下载机。";
    else msg += " · 推送失败：" + ((d.push && d.push.detail) || "未知原因") + "，可点“重新推送”。";
    $("cookieMsg").textContent = msg;
    loadSettings();
  }).catch(function (e) {
    clearInterval(timer);
    var secs = ((Date.now() - t0) / 1000).toFixed(1);
    prog.style.display = "none";
    btn.disabled = false;
    $("cookieErr").textContent = e.message + "（用时 " + secs + " 秒，未保存、未推送）";
  });
}

function repush() {
  var btn = $("repushBtn");
  $("cookieMsg").textContent = "";
  $("cookieErr").textContent = "";
  btn.disabled = true;
  jpost("api/settings/push", {}).then(function (d) {
    btn.disabled = false;
    if (d.pushed) $("cookieMsg").textContent = "已重新推送到下载机。";
    else $("cookieErr").textContent = "推送失败：" + ((d.push && d.push.detail) || "未知原因");
    loadSettings();
  }).catch(function (e) {
    btn.disabled = false;
    $("cookieErr").textContent = e.message;
  });
}

document.querySelectorAll(".seg button").forEach(function (b) {
  b.addEventListener("click", function () {
    state.kind = b.getAttribute("data-kind");
    state.page = 1;
    document.querySelectorAll(".seg button").forEach(function (x) {
      x.className = x === b ? "on" : "";
    });
    loadList();
  });
});
var qTimer = null;
$("q").addEventListener("input", function () {
  clearTimeout(qTimer);
  qTimer = setTimeout(function () {
    state.q = $("q").value.trim();
    state.page = 1;
    loadList();
  }, 300);
});
$("scoreSel").addEventListener("change", function () {
  state.score = $("scoreSel").value;
  state.page = 1;
  loadList();
});
$("prevPage").addEventListener("click", function () {
  if (state.page > 1) { state.page -= 1; loadList(); }
});
$("nextPage").addEventListener("click", function () {
  var pages = Math.max(1, Math.ceil(state.total / state.perPage));
  if (state.page < pages) { state.page += 1; loadList(); }
});
$("backBtn").addEventListener("click", function () { location.hash = "#/"; });
$("saveCookieBtn").addEventListener("click", saveCookie);
$("repushBtn").addEventListener("click", repush);
window.addEventListener("hashchange", router);
router();
})();
</script>
</body>
</html>
"""


# -------------------------------------------------------------- WSGI

def _json(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def _err(start_response, status, message):
    return _json(start_response, status, {"ok": False, "error": message})


def _read_body(environ, limit):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > limit:
        return None
    return environ["wsgi.input"].read(length)


def _read_json(environ, limit=8192):
    raw = _read_body(environ, limit)
    if raw is None:
        return None, "请求体缺失或过大。"
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "请求体不是合法 JSON。"
    if not isinstance(data, dict):
        return None, "请求体须为 JSON 对象。"
    return data, ""


def _params(environ):
    qs = urllib.parse.parse_qs(environ.get("QUERY_STRING") or "",
                               keep_blank_values=True)
    return {k: v[0] for k, v in qs.items()}


def _parse_key(params):
    tweet_id = (params.get("tweet_id") or "").strip()
    num_raw = (params.get("num") or "").strip()
    if not tweet_id or len(tweet_id) > 64:
        return None, None, "缺少 tweet_id。"
    if not re.fullmatch(r"\d+", num_raw):
        return None, None, "num 须为整数。"
    return tweet_id, int(num_raw), ""


def _parse_key_json(data):
    tweet_id = str(data.get("tweet_id") or "").strip()
    num = data.get("num")
    if not tweet_id or len(tweet_id) > 64:
        return None, None, "缺少 tweet_id。"
    if isinstance(num, bool) or not isinstance(num, int) or num < 0:
        return None, None, "num 须为非负整数。"
    return tweet_id, num, ""


def _settings_payload():
    configured = os.path.isfile(COOKIE_FILE)
    count = _count_cookie_rows() if configured else 0
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        video = conn.execute(
            "SELECT COUNT(*) FROM items WHERE kind = 'video'").fetchone()[0]
        image = conn.execute(
            "SELECT COUNT(*) FROM items WHERE kind = 'image'").fetchone()[0]
        unrated = conn.execute(
            "SELECT COUNT(*) FROM items WHERE COALESCE(score, 0) = 0"
        ).fetchone()[0]
        newest = conn.execute(
            "SELECT MAX(created_at), MAX(published_at) FROM items").fetchone()
    try:
        db_bytes = os.path.getsize(DB_PATH)
    except OSError:
        db_bytes = 0
    return {
        "ok": True,
        "cookie": {"configured": configured, "count": count},
        "push": _read_push_state(),
        "db": {"total": total, "video": video, "image": image,
               "unrated": unrated, "newest_created_at": newest[0],
               "newest_published_at": newest[1], "db_bytes": db_bytes},
    }


def application(environ, start_response):
    try:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/") or "/"
        if len(path) > 1:
            path = path.rstrip("/") or "/"

        if path == "/" and method == "GET":
            body = PAGE_HTML.encode("utf-8")
            start_response("200 OK", [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body)))])
            return [body]

        if path == "/favicon.ico":
            start_response("204 No Content", [("Content-Length", "0")])
            return [b""]

        if path == "/api/session" and method == "GET":
            return _json(start_response, "200 OK", {"authenticated": True})

        if path == "/api/items" and method == "GET":
            params = _params(environ)
            adapter = _get_source(params.get("source"))
            if adapter is None:
                return _err(start_response, "404 Not Found", "未知的数据源。")
            kind = params.get("kind") or "all"
            if kind not in ("all", "video", "image"):
                return _err(start_response, "400 Bad Request", "kind 非法。")
            score = params.get("score") or ""
            if score not in ("", "0", "1", "2", "3", "4", "5"):
                return _err(start_response, "400 Bad Request", "score 非法。")
            try:
                page = max(1, int(params.get("page") or 1))
                per_page = min(50, max(1, int(params.get("per_page") or 15)))
            except ValueError:
                return _err(start_response, "400 Bad Request", "分页参数非法。")
            items, total, ms = adapter.list_items(
                kind=kind, q=params.get("q") or "", score=score,
                page=page, per_page=per_page)
            return _json(start_response, "200 OK",
                         {"ok": True, "items": items, "total": total,
                          "page": page, "per_page": per_page, "ms": round(ms, 1)})

        if path == "/api/item" and method == "GET":
            params = _params(environ)
            adapter = _get_source(params.get("source"))
            if adapter is None:
                return _err(start_response, "404 Not Found", "未知的数据源。")
            tweet_id, num, err = _parse_key(params)
            if err:
                return _err(start_response, "400 Bad Request", err)
            item = adapter.get_item(tweet_id, num)
            if item is None:
                return _err(start_response, "404 Not Found", "这条内容不存在。")
            return _json(start_response, "200 OK",
                         {"ok": True, "item": _public_item(item)})

        if path == "/api/score" and method == "POST":
            data, err = _read_json(environ)
            if err:
                return _err(start_response, "400 Bad Request", err)
            tweet_id, num, kerr = _parse_key_json(data)
            if kerr:
                return _err(start_response, "400 Bad Request", kerr)
            score = data.get("score")
            if isinstance(score, bool) or not isinstance(score, int) \
                    or score not in range(6):
                return _err(start_response, "400 Bad Request",
                            "评分必须为 0–5 的整数。")
            adapter = _get_source(data.get("source"))
            if adapter is None:
                return _err(start_response, "404 Not Found", "未知的数据源。")
            item = adapter.set_score(tweet_id, num, score)
            if item is None:
                return _err(start_response, "404 Not Found", "这条内容不存在。")
            return _json(start_response, "200 OK",
                         {"ok": True, "item": _public_item(item)})

        if path == "/api/tags" and method == "POST":
            data, err = _read_json(environ)
            if err:
                return _err(start_response, "400 Bad Request", err)
            tweet_id, num, kerr = _parse_key_json(data)
            if kerr:
                return _err(start_response, "400 Bad Request", kerr)
            tags = data.get("tags")
            if not isinstance(tags, str) or len(tags) > 500:
                return _err(start_response, "400 Bad Request",
                            "标签须为不超过 500 字的文本。")
            adapter = _get_source(data.get("source"))
            if adapter is None:
                return _err(start_response, "404 Not Found", "未知的数据源。")
            item = adapter.set_tags(tweet_id, num, tags)
            if item is None:
                return _err(start_response, "404 Not Found", "这条内容不存在。")
            return _json(start_response, "200 OK",
                         {"ok": True, "item": _public_item(item)})

        if path == "/api/preview" and method == "POST":
            data, err = _read_json(environ)
            if err:
                return _err(start_response, "400 Bad Request", err)
            tweet_id, num, kerr = _parse_key_json(data)
            if kerr:
                return _err(start_response, "400 Bad Request", kerr)
            adapter = _get_source(data.get("source"))
            if adapter is None:
                return _err(start_response, "404 Not Found", "未知的数据源。")
            item = adapter.bump_preview(tweet_id, num)
            if item is None:
                return _err(start_response, "404 Not Found", "这条内容不存在。")
            return _json(start_response, "200 OK",
                         {"ok": True, "item": _public_item(item)})

        if path in ("/api/play", "/api/file") and method == "GET":
            params = _params(environ)
            adapter = _get_source(params.get("source"))
            if adapter is None:
                return _err(start_response, "404 Not Found", "未知的数据源。")
            tweet_id, num, kerr = _parse_key(params)
            if kerr:
                return _err(start_response, "400 Bad Request", kerr)
            which = params.get("which") or "main"
            if which not in ("main", "cover"):
                return _err(start_response, "400 Bad Request", "which 非法。")
            item = adapter.get_item(tweet_id, num)
            if item is None:
                return _err(start_response, "404 Not Found", "这条内容不存在。")
            try:
                url = adapter.play_url(item, which)
            except AlistError as exc:
                return _err(start_response, "502 Bad Gateway", str(exc)[:200])
            if path == "/api/play":
                return _json(start_response, "200 OK", {"ok": True, "url": url})
            start_response("302 Found", [("Location", url),
                                         ("Content-Length", "0")])
            return [b""]

        if path == "/api/settings" and method == "GET":
            return _json(start_response, "200 OK", _settings_payload())

        if path == "/api/settings/cookie" and method == "POST":
            data, err = _read_json(environ, limit=128 * 1024)
            if err:
                return _err(start_response, "400 Bad Request", err)
            try:
                text, count, note = _parse_cookie_input(data.get("cookie"))
            except ValueError as exc:
                return _err(start_response, "400 Bad Request", str(exc))
            _save_cookie_file(text)
            state, pushed = _do_push_stored_cookie()
            return _json(start_response, "200 OK",
                         {"ok": True, "count": count, "note": note,
                          "pushed": pushed, "push": state})

        if path == "/api/settings/push" and method == "POST":
            _data, err = _read_json(environ)
            if err and _data is None and environ.get("CONTENT_LENGTH") not in (None, "", "0"):
                return _err(start_response, "400 Bad Request", err)
            if not os.path.isfile(COOKIE_FILE):
                return _err(start_response, "400 Bad Request", "尚未保存 Cookie。")
            state, pushed = _do_push_stored_cookie()
            return _json(start_response, "200 OK",
                         {"ok": True, "pushed": pushed, "push": state})

        return _err(start_response, "404 Not Found", "不存在。")
    except Exception:
        import traceback
        traceback.print_exc()
        return _err(start_response, "500 Internal Server Error", "服务器内部错误。")


app = application

if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    print("xlikes dev server on http://127.0.0.1:18099/ (DATA_DIR=%s)" % DATA_DIR)
    make_server("127.0.0.1", 18099, application).serve_forever()
