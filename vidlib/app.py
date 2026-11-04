"""视频库 (vidlib) - App Hub project.

Architecture (three layers, files never move after ingest):
  * Ingest/archive (this app): chunked uploads land in a local staging
    area, are hashed (SHA-256) and deduplicated, probed with ffprobe,
    thumbnailed with ffmpeg, recorded in SQLite, then uploaded in the
    background to Google Drive *through AList* and removed locally.
    The database is the library's memory: hash -> original title,
    tags, source, duration, thumbnail, Drive path, status.
  * Link source (AList): runs on loopback only, holds the Google
    Drive mount, and hands out signed playback URLs via its API.
    Browsers never see AList credentials; they only get one signed
    URL per video from this app.
  * Pages (this app): thumbnail wall, database search (title/tags/
    duration), watch page with progress resume and signed-URL
    refresh. All metadata lives in SQLite in the central data dir.

Local disk is small: staging has a hard budget, uploads are chunked,
archived files are deleted locally as soon as the Drive copy is
verified, and batch downloads are exported as signed-URL lists
(aria2 format) instead of zip archives.
"""

import base64
import hashlib
import html
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from wsgiref.util import setup_testing_defaults

# ------------------------------------------------------------ data dirs

def _platform_data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if os.path.isdir(link):
        return link
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime")


DATA_DIR = _platform_data_dir()
RUNTIME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime")
DB_PATH = os.path.join(DATA_DIR, "vidlib.db")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
STAGING_DIR = os.path.join(RUNTIME_DIR, "staging")
CRED_PATH = os.path.join(DATA_DIR, "credentials.json")
for _d in (DATA_DIR, THUMB_DIR, BACKUP_DIR, STAGING_DIR):
    os.makedirs(_d, exist_ok=True)

PUBLIC_VIDEO_BASE = os.environ.get("VIDLIB_PUBLIC_BASE",
                                   "https://debi.su7.dpdns.org")
CHUNK_BYTES = 8 * 1024 * 1024
STAGING_BUDGET = 4 * 1024 * 1024 * 1024
DISK_MIN_FREE = 6 * 1024 * 1024 * 1024
VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "m4v", "avi", "ts", "flv",
              "wmv", "mpg", "mpeg", "3gp", "rmvb"}
PLAYABLE_EXTS = {"mp4", "webm", "m4v", "mov"}

_db_lock = threading.Lock()
_worker_started = False
_worker_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            hash TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            tags TEXT DEFAULT '',
            source TEXT DEFAULT '',
            orig_name TEXT DEFAULT '',
            ext TEXT DEFAULT '',
            size INTEGER DEFAULT 0,
            duration REAL DEFAULT 0,
            width INTEGER DEFAULT 0,
            height INTEGER DEFAULT 0,
            status TEXT DEFAULT 'local',
            gpath TEXT DEFAULT '',
            error TEXT DEFAULT '',
            retries INTEGER DEFAULT 0,
            dup_count INTEGER DEFAULT 0,
            play_pos REAL DEFAULT 0,
            play_count INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0,
            updated_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY, value TEXT DEFAULT ''
        );
        """)


init_db()


def kv_get(key, default=""):
    with _db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?",
                           (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key, value):
    with _db() as conn:
        conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))


# ------------------------------------------------------- credentials
# Import-only credential store (values are never rendered back):
#   alist_base / alist_user / alist_pass - this app -> AList (loopback)
#   google_refresh_token / google_root_folder_id - AList -> Drive

def _load_creds():
    try:
        with open(CRED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_creds(creds):
    tmp = CRED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(creds, fh, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CRED_PATH)


def _cred(key, env_key=None):
    if env_key and os.environ.get(env_key):
        return os.environ[env_key]
    return _load_creds().get(key) or ""


# ------------------------------------------------------------ AList

class AlistError(Exception):
    pass


class AlistClient:
    def __init__(self):
        self._token = None
        self._token_at = 0.0

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
        if (not force and self._token
                and time.time() - self._token_at < 40 * 3600):
            return self._token
        user = _cred("alist_user", "ALIST_USER") or "admin"
        password = _cred("alist_pass", "ALIST_PASS")
        if not password:
            raise AlistError("尚未配置 AList 管理员密码（见管理页）。")
        out = self._raw("POST", "/api/auth/login",
                        {"username": user, "password": password})
        token = (out.get("data") or {}).get("token")
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

    # -- storage ----------------------------------------------------
    def storages(self):
        data = self.api("GET", "/api/admin/storage/list")
        if isinstance(data, dict):
            return data.get("content") or []
        return data or []

    def drive_storage(self):
        for st in self.storages():
            if st.get("mount_path") == "/":
                return st
        return None

    def storage_ready(self):
        st = self.drive_storage()
        return bool(st) and st.get("status") == "work"

    def ensure_drive_storage(self, refresh_token, root_folder_id=""):
        addition = json.dumps({
            "root_folder_id": root_folder_id or "",
            "client_id": "", "client_secret": "",
            "refresh_token": refresh_token,
            "chunk_size": 5, "alist_version": "v3",
        })
        st = self.drive_storage()
        if st:
            payload = dict(st)
            payload["addition"] = addition
            payload["status"] = "work"
            self.api("POST", "/api/admin/storage/update", payload)
            return "updated"
        self.api("POST", "/api/admin/storage/create", {
            "mount_path": "/", "order": 0, "driver": "Google Drive",
            "cache_expiration": 30, "status": "work",
            "addition": addition,
        })
        return "created"

    # -- files --------------------------------------------------------
    def fs_get(self, path):
        return self.api("POST", "/api/fs/get", {"path": path}) or {}

    def fs_list(self, path):
        data = self.api("POST", "/api/fs/list",
                        {"path": path, "page": 1, "per_page": 0,
                         "refresh": True}) or {}
        return data.get("content") or []

    def fs_mkdir(self, path):
        return self.api("POST", "/api/fs/mkdir", {"path": path})

    def fs_remove(self, dir_path, names):
        return self.api("POST", "/api/fs/remove",
                        {"dir": dir_path, "names": names})

    def upload_file(self, local_path, remote_path):
        """Stream a local file to AList (PUT /api/fs/put)."""
        token = self.login()
        size = os.path.getsize(local_path)
        parsed = urllib.parse.urlparse(self.base())
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port,
                                          timeout=600)
        try:
            conn.putrequest("PUT", "/api/fs/put")
            conn.putheader("Authorization", token)
            conn.putheader("File-Path", urllib.parse.quote(remote_path))
            conn.putheader("As-Task", "false")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            with open(local_path, "rb") as fh:
                while True:
                    block = fh.read(1024 * 1024)
                    if not block:
                        break
                    conn.send(block)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")
            out = json.loads(body) if body else {}
            if out.get("code") not in (200, 0, None):
                raise AlistError("上传失败：%s"
                                 % (out.get("message") or resp.status))
            return out.get("data")
        finally:
            conn.close()


ALIST = AlistClient()


# ------------------------------------------------------------- media

def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, timeout=60)
        info = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return {}
    result = {"duration": 0.0, "width": 0, "height": 0}
    fmt = info.get("format") or {}
    try:
        result["duration"] = float(fmt.get("duration") or 0)
    except Exception:
        pass
    for stream in info.get("streams") or []:
        if stream.get("codec_type") == "video":
            result["width"] = int(stream.get("width") or 0)
            result["height"] = int(stream.get("height") or 0)
            if not result["duration"]:
                try:
                    result["duration"] = float(stream.get("duration") or 0)
                except Exception:
                    pass
            break
    return result


def _make_thumb(video_path, hash_):
    out_path = os.path.join(THUMB_DIR, hash_ + ".jpg")
    if os.path.exists(out_path):
        return True
    meta = _probe_cache.get(hash_) or {}
    duration = meta.get("duration") or 0
    seek = max(0.0, min(3.0, duration / 3.0)) if duration else 1.0
    try:
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "%.2f" % seek, "-i", video_path,
             "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5",
             out_path, "-y"],
            capture_output=True, timeout=60)
        return res.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


_probe_cache = {}


def _fmt_duration(sec):
    sec = int(sec or 0)
    if sec >= 3600:
        return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)
    return "%d:%02d" % (sec // 60, sec % 60)


def _fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit == "B" or n >= 100
                    else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def _staging_bytes():
    total = 0
    for name in os.listdir(STAGING_DIR):
        try:
            total += os.path.getsize(os.path.join(STAGING_DIR, name))
        except Exception:
            pass
    return total


def _disk_free():
    try:
        return shutil.disk_usage(DATA_DIR).free
    except Exception:
        return 0


# ------------------------------------------------------------ uploads

_uploads = {}
_uploads_lock = threading.Lock()


def upload_init(name, size, title, tags, source):
    size = int(size or 0)
    if size <= 0:
        raise ValueError("文件大小无效。")
    if _staging_bytes() + size > STAGING_BUDGET:
        raise ValueError("暂存区预算不足（上限 %s），请等已上传的归档完成后再传。"
                         % _fmt_size(STAGING_BUDGET))
    if size > _disk_free() - DISK_MIN_FREE:
        raise ValueError("服务器磁盘剩余空间不足。")
    uid = hashlib.sha256(
        ("%s:%s:%s" % (name, size, time.time())).encode()).hexdigest()[:20]
    part_path = os.path.join(STAGING_DIR, uid + ".part")
    with open(part_path, "wb"):
        pass
    with _uploads_lock:
        _uploads[uid] = {"name": name, "size": size, "title": title,
                         "tags": tags, "source": source,
                         "path": part_path, "at": time.time()}
    return uid


def upload_chunk(uid, offset, data):
    with _uploads_lock:
        sess = _uploads.get(uid)
    if sess is None:
        raise ValueError("上传会话不存在或已过期。")
    with open(sess["path"], "r+b") as fh:
        fh.seek(int(offset))
        fh.write(data)
    return len(data)


def upload_complete(uid):
    with _uploads_lock:
        sess = _uploads.pop(uid, None)
    if sess is None:
        raise ValueError("上传会话不存在或已过期。")
    part = sess["path"]
    actual = os.path.getsize(part)
    if actual != sess["size"]:
        raise ValueError("文件不完整：收到 %d / 应为 %d 字节。"
                         % (actual, sess["size"]))
    digest = _sha256_file(part)
    orig = sess["name"] or "video.mp4"
    ext = ""
    if "." in orig:
        cand = orig.rsplit(".", 1)[1].lower()
        if re.fullmatch(r"[a-z0-9]{1,5}", cand):
            ext = cand
    if ext not in VIDEO_EXTS:
        ext = ext if ext else "mp4"
    with _db() as conn:
        row = conn.execute("SELECT hash FROM videos WHERE hash = ?",
                           (digest,)).fetchone()
        if row:
            conn.execute(
                "UPDATE videos SET dup_count = dup_count + 1, "
                "updated_at = ? WHERE hash = ?", (time.time(), digest))
    if row:
        os.unlink(part)
        return {"hash": digest, "duplicate": True}
    final = os.path.join(STAGING_DIR, digest + "." + ext)
    os.replace(part, final)
    meta = _probe(final)
    _probe_cache[digest] = meta
    _make_thumb(final, digest)
    title = (sess["title"] or os.path.splitext(orig)[0] or digest[:8]).strip()
    now = time.time()
    with _db() as conn:
        conn.execute(
            "INSERT INTO videos(hash, title, tags, source, orig_name, ext, "
            "size, duration, width, height, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'local',?,?)",
            (digest, title[:200], (sess["tags"] or "")[:200],
             (sess["source"] or "")[:120], orig[:200], ext, actual,
             meta.get("duration") or 0, meta.get("width") or 0,
             meta.get("height") or 0, now, now))
    _ensure_worker()
    return {"hash": digest, "duplicate": False}


# ------------------------------------------------------------- worker

def _local_path(row):
    return os.path.join(STAGING_DIR, row["hash"] + "." + (row["ext"] or "mp4"))


def _archive_one(row):
    local = _local_path(row)
    remote = "/" + row["hash"] + "." + (row["ext"] or "mp4")
    ALIST.upload_file(local, remote)
    info = ALIST.fs_get(remote)
    remote_size = (info or {}).get("size")
    if remote_size is not None and int(remote_size) != int(row["size"]):
        raise AlistError("归档校验失败：远端大小 %s 与本地 %s 不一致。"
                         % (remote_size, row["size"]))
    with _db() as conn:
        conn.execute(
            "UPDATE videos SET status = 'archived', gpath = ?, error = '', "
            "updated_at = ? WHERE hash = ?",
            (remote, time.time(), row["hash"]))
    try:
        os.unlink(local)
    except Exception:
        pass


def _maybe_backup_db():
    last = float(kv_get("last_backup_at", "0") or 0)
    if time.time() - last < 24 * 3600:
        return
    stamp = time.strftime("%Y%m%d")
    local_copy = os.path.join(BACKUP_DIR, "vidlib-%s.db" % stamp)
    try:
        src = _db()
        dst = sqlite3.connect(local_copy)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        locals_ = sorted(f for f in os.listdir(BACKUP_DIR)
                         if f.startswith("vidlib-"))
        for old in locals_[:-3]:
            os.unlink(os.path.join(BACKUP_DIR, old))
        if ALIST.storage_ready():
            try:
                ALIST.fs_mkdir("/_vidlib_backups")
            except Exception:
                pass
            ALIST.upload_file(local_copy,
                              "/_vidlib_backups/vidlib-%s.db" % stamp)
            remote = ALIST.fs_list("/_vidlib_backups")
            names = sorted((e.get("name") or "") for e in remote
                           if (e.get("name") or "").startswith("vidlib-"))
            for old in names[:-7]:
                try:
                    ALIST.fs_remove("/_vidlib_backups", [old])
                except Exception:
                    pass
        kv_set("last_backup_at", str(time.time()))
    except Exception as exc:
        kv_set("last_backup_error", str(exc)[:200])


def _worker_loop():
    while True:
        try:
            now = time.time()
            for name in os.listdir(STAGING_DIR):
                if name.endswith(".part"):
                    p = os.path.join(STAGING_DIR, name)
                    try:
                        if now - os.path.getmtime(p) > 24 * 3600:
                            os.unlink(p)
                    except Exception:
                        pass
            ready = False
            try:
                ready = ALIST.storage_ready()
            except Exception:
                ready = False
            if ready:
                with _db() as conn:
                    row = conn.execute(
                        "SELECT * FROM videos WHERE status = 'local' "
                        "ORDER BY created_at LIMIT 1").fetchone()
                if row:
                    with _db() as conn:
                        conn.execute(
                            "UPDATE videos SET status = 'uploading' "
                            "WHERE hash = ?", (row["hash"],))
                    try:
                        _archive_one(row)
                    except Exception as exc:
                        retries = int(row["retries"] or 0) + 1
                        new_status = "error" if retries >= 5 else "local"
                        with _db() as conn:
                            conn.execute(
                                "UPDATE videos SET status = ?, retries = ?, "
                                "error = ?, updated_at = ? WHERE hash = ?",
                                (new_status, retries, str(exc)[:300],
                                 time.time(), row["hash"]))
                _maybe_backup_db()
        except Exception:
            pass
        time.sleep(5)


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True).start()


# --------------------------------------------------------- play links

def resolve_play_url(row):
    """Return a browser-usable URL for an archived video.

    Prefers the raw_url AList returns when it already points at a
    public host (e.g. a Google direct link); otherwise rewrites the
    AList-local /d/ or /p/ URL onto the public video base, where
    Caddy forwards only those two prefixes to the loopback AList.
    The URL is signed and expires - callers fetch it fresh."""
    info = ALIST.fs_get(row["gpath"])
    raw = (info or {}).get("raw_url") or ""
    sign = (info or {}).get("sign") or ""
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        if parsed.hostname not in ("127.0.0.1", "localhost"):
            return raw
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
        return PUBLIC_VIDEO_BASE + path
    if raw.startswith("/"):
        return PUBLIC_VIDEO_BASE + raw
    if sign:
        return (PUBLIC_VIDEO_BASE + "/d" + row["gpath"]
                + "?sign=" + urllib.parse.quote(sign))
    return PUBLIC_VIDEO_BASE + "/d" + row["gpath"]


# -------------------------------------------------------------- pages

def _esc(v):
    return html.escape(str(v if v is not None else ""), quote=True)


def _status_chip(row):
    st = row["status"]
    if st == "archived":
        return ""
    label = {"local": "待归档", "uploading": "归档中",
             "error": "归档失败"}.get(st, st)
    return '<span class="chip">%s</span>' % label


BASE_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #101319; color: #e8ebf1; }
a { color: inherit; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 16px 14px 90px; }
.topbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
.topbar h1 { font-size: 20px; margin: 0 10px 0 0; }
.topbar form { display: flex; gap: 8px; flex: 1; min-width: 240px; }
input[type=text], input[type=password], select, textarea {
  background: #1a1f2b; border: 1px solid #2c3446; color: #e8ebf1;
  border-radius: 9px; padding: 9px 11px; font-size: 14px; }
.topbar input[type=text] { flex: 1; }
button, .btn { background: #2f6fd0; color: #fff; border: 0; border-radius: 9px;
  padding: 9px 16px; font-size: 14px; cursor: pointer; text-decoration: none; display: inline-block; }
button.ghost, .btn.ghost { background: #232a3a; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 14px; }
@media (max-width: 560px) { .grid { grid-template-columns: repeat(3, 1fr); gap: 10px; } }
.card { background: #171c27; border: 1px solid #242c3f; border-radius: 13px; overflow: hidden;
  text-decoration: none; display: block; position: relative; }
.card .thumb { position: relative; aspect-ratio: 16/9; background: #0b0e14; }
.card .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.card .dur { position: absolute; right: 6px; bottom: 6px; background: rgba(0,0,0,.72);
  color: #fff; font-size: 11px; padding: 2px 6px; border-radius: 6px; }
.card .pick { position: absolute; top: 6px; left: 6px; width: 20px; height: 20px; accent-color: #2f6fd0; }
.card .t { font-size: 13.5px; line-height: 1.35; padding: 8px 9px 2px; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 38px; }
.card .m { font-size: 11.5px; color: #8b94a7; padding: 0 9px 9px; }
.chip { background: #6b5410; color: #ffd97a; font-size: 10.5px; padding: 1px 6px; border-radius: 5px; }
.tag { color: #7ea6e0; }
.batchbar { position: fixed; left: 50%; transform: translateX(-50%); bottom: 16px;
  background: #1d2536; border: 1px solid #33405c; border-radius: 999px; padding: 9px 18px;
  display: none; gap: 12px; align-items: center; box-shadow: 0 8px 30px rgba(0,0,0,.5); z-index: 30; }
.batchbar.on { display: flex; }
.panel { background: #171c27; border: 1px solid #242c3f; border-radius: 13px; padding: 16px; margin-bottom: 14px; }
.panel h2 { font-size: 16px; margin: 0 0 10px; }
.kv { font-size: 13.5px; line-height: 1.9; color: #c6ccd9; }
.kv b { color: #fff; }
.msg { font-size: 13px; color: #9fd49a; }
.err { font-size: 13px; color: #ff9c9c; }
.empty { color: #8b94a7; text-align: center; padding: 50px 0; }
.progress { height: 8px; background: #232a3a; border-radius: 99px; overflow: hidden; }
.progress i { display: block; height: 100%; background: #2f6fd0; width: 0; }
"""


def _page(title, body, extra_head="", base_href=""):
    base_tag = ('<base href="%s">' % _esc(base_href)) if base_href else ""
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>%s · 视频库</title>%s<style>%s</style>%s</head>"
            "<body><div class=\"wrap\">%s</div></body></html>"
            % (_esc(title), base_tag, BASE_CSS, extra_head,
               body)).encode("utf-8")


def page_wall(params, base_href=""):
    q = (params.get("q") or "").strip()
    dur = params.get("dur") or ""
    tag = (params.get("tag") or "").strip()
    page = max(1, int(params.get("page") or 1))
    where, args = [], []
    if q:
        where.append("(title LIKE ? OR tags LIKE ? OR source LIKE ?)")
        like = "%" + q + "%"
        args += [like, like, like]
    if tag:
        where.append("tags LIKE ?")
        args.append("%" + tag + "%")
    if dur == "0":
        where.append("duration > 0 AND duration < 60")
    elif dur == "1":
        where.append("duration >= 60 AND duration < 300")
    elif dur == "2":
        where.append("duration >= 300 AND duration < 1800")
    elif dur == "3":
        where.append("duration >= 1800")
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    per = 60
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM videos" + sql_where,
                             args).fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM videos" + sql_where
            + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            args + [per, (page - 1) * per]).fetchall()
        tag_rows = conn.execute(
            "SELECT tags FROM videos WHERE tags != ''").fetchall()
    tagset = {}
    for r in tag_rows:
        for t in re.split(r"[,，\s]+", r["tags"] or ""):
            if t:
                tagset[t] = tagset.get(t, 0) + 1
    tag_links = " ".join(
        '<a class="tag" href="?tag=%s">#%s(%d)</a>'
        % (urllib.parse.quote(t), _esc(t), n)
        for t, n in sorted(tagset.items(), key=lambda kv: -kv[1])[:24])
    cards = []
    for r in rows:
        date = time.strftime("%Y-%m-%d",
                             time.localtime(r["created_at"] or 0))
        cards.append(
            '<div class="card">'
            '<input type="checkbox" class="pick" value="%s">'
            '<a href="watch/%s"><span class="thumb" style="display:block">'
            '<img loading="lazy" src="thumb/%s" alt="">'
            '<span class="dur">%s</span></span>'
            '<span class="t">%s %s</span></a>'
            '<div class="m">%s · %s%s</div></div>'
            % (r["hash"], r["hash"], r["hash"],
               _fmt_duration(r["duration"]), _esc(r["title"]),
               _status_chip(r),
               _fmt_size(r["size"]), date,
               (" · " + _esc(r["tags"])) if r["tags"] else ""))
    pages = (total + per - 1) // per
    pager = ""
    if pages > 1:
        qs = urllib.parse.urlencode(
            {k: v for k, v in params.items() if k != "page" and v})
        links = []
        for p in range(1, pages + 1):
            if abs(p - page) <= 2 or p in (1, pages):
                links.append('<a href="?%spage=%d">%d</a>'
                             % ((qs + "&") if qs else "", p, p))
        pager = '<div style="margin-top:16px">%s</div>' % " ".join(links)
    body = (
        '<div class="topbar"><h1>视频库</h1>'
        '<form method="get"><input type="text" name="q" value="%s" '
        'placeholder="搜索标题 / 标签 / 来源…">'
        '<select name="dur">'
        '<option value="">全部时长</option>'
        '<option value="0"%s>&lt;1分钟</option>'
        '<option value="1"%s>1–5分钟</option>'
        '<option value="2"%s>5–30分钟</option>'
        '<option value="3"%s>30分钟+</option></select>'
        '<button type="submit">搜索</button></form>'
        '<a class="btn" href="upload">上传</a>'
        '<a class="btn ghost" href="admin">管理</a></div>'
        '<div style="margin-bottom:12px;font-size:13px">共 %d 个视频 %s</div>'
        '<div class="grid">%s</div>%s'
        '<div class="batchbar" id="batchbar"><span id="batchn"></span>'
        '<button type="button" id="batchexport">导出下载列表</button>'
        '<button type="button" class="ghost" id="batchclear">清空</button></div>'
        '<script>'
        'const bar=document.getElementById("batchbar");'
        'function sync(){const c=[...document.querySelectorAll(".pick:checked")];'
        'bar.classList.toggle("on",c.length>0);'
        'document.getElementById("batchn").textContent="已选 "+c.length+" 个";'
        'return c.map(x=>x.value);}'
        'document.addEventListener("change",e=>{if(e.target.classList.contains("pick"))sync();});'
        'document.getElementById("batchclear").onclick=()=>{'
        'document.querySelectorAll(".pick:checked").forEach(x=>x.checked=false);sync();};'
        'document.getElementById("batchexport").onclick=async()=>{'
        'const hs=sync();if(!hs.length)return;'
        'const r=await fetch("api/batch-export",{method:"POST",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify({hashes:hs})});'
        'const blob=await r.blob();const a=document.createElement("a");'
        'a.href=URL.createObjectURL(blob);a.download="download-list.txt";a.click();};'
        '</script>'
        % (_esc(q),
           ' selected' if dur == "0" else '', ' selected' if dur == "1" else '',
           ' selected' if dur == "2" else '', ' selected' if dur == "3" else '',
           total, ("· 标签：" + tag_links) if tag_links else "",
           "".join(cards) or '<div class="empty">还没有视频。点右上角「上传」添加第一个。</div>',
           pager))
    return _page("视频库", body, base_href=base_href)


def page_watch(row, base_href=""):
    if row is None:
        return None
    playable = (row["ext"] or "") in PLAYABLE_EXTS
    tags_html = ""
    if row["tags"]:
        tags_html = " · ".join(
            '<a class="tag" href="./?tag=%s">#%s</a>'
            % (urllib.parse.quote(t), _esc(t))
            for t in re.split(r"[,，\s]+", row["tags"]) if t)
    body = (
        '<div class="topbar"><a class="btn ghost" href="./">← 返回</a>'
        '<h1 style="font-size:17px">%s</h1></div>'
        '<video id="v" controls playsinline preload="metadata" '
        'poster="thumb/%s" style="width:100%%;background:#000;border-radius:12px"></video>'
        '%s'
        '<div class="panel" style="margin-top:14px"><div class="kv">'
        '时长 <b>%s</b> · 大小 <b>%s</b> · 分辨率 <b>%s</b><br>'
        '上传于 <b>%s</b> · 播放 <b>%d</b> 次 · 状态 <b>%s</b><br>'
        '%s%s'
        '来源：%s<br>'
        '<a class="btn" id="dl" href="#" target="_blank" rel="noopener">下载</a> '
        '<button class="ghost" id="rate">倍速 1×</button>'
        '</div></div>'
        '<div class="panel"><h2>编辑信息</h2>'
        '<form id="metaform"><div style="display:grid;gap:9px">'
        '<input type="text" id="m-title" value="%s" maxlength="200">'
        '<input type="text" id="m-tags" value="%s" placeholder="标签，逗号分隔" maxlength="200">'
        '<input type="text" id="m-source" value="%s" placeholder="来源" maxlength="120">'
        '<span><button type="submit">保存</button> <span class="msg" id="m-msg"></span></span>'
        '</div></form></div>'
        '<script>'
        'const HASH="%s";const v=document.getElementById("v");'
        'let url="",pos=%s,lastSave=0;'
        'async function fresh(){const r=await fetch("api/play/"+HASH);'
        'const d=await r.json();if(d.ok){url=d.url;'
        'document.getElementById("dl").href=d.url;return d;}}'
        'async function start(){const d=await fresh();if(!d)return;'
        'v.src=d.url;if(d.pos>5)v.currentTime=d.pos;}'
        'v.addEventListener("error",async()=>{const t=v.currentTime;'
        'const d=await fresh();if(d){v.src=d.url;v.currentTime=t;v.play().catch(()=>{});}},true);'
        'setInterval(async()=>{if(!v.paused){const t=v.currentTime;'
        'const d=await fresh();if(d&&d.url!==url){v.src=d.url;v.currentTime=t;}}},45*60*1000);'
        'v.addEventListener("timeupdate",()=>{const n=Date.now();'
        'if(n-lastSave>10000){lastSave=n;fetch("api/progress",{method:"POST",'
        'headers:{"Content-Type":"application/json"},'
        'body:JSON.stringify({hash:HASH,pos:v.currentTime})});}});'
        'document.getElementById("rate").onclick=e=>{'
        'const rates=[1,1.25,1.5,2,0.75];let i=rates.indexOf(v.playbackRate);'
        'i=(i+1)%%rates.length;v.playbackRate=rates[i];'
        'e.target.textContent="倍速 "+rates[i]+"×";};'
        'document.getElementById("metaform").onsubmit=async e=>{e.preventDefault();'
        'const r=await fetch("api/meta",{method:"POST",headers:{"Content-Type":"application/json"},'
        'body:JSON.stringify({hash:HASH,title:document.getElementById("m-title").value,'
        'tags:document.getElementById("m-tags").value,source:document.getElementById("m-source").value})});'
        'document.getElementById("m-msg").textContent=(await r.json()).ok?"已保存":"保存失败";};'
        'start();'
        '</script>'
        % (_esc(row["title"]), row["hash"],
           "" if playable else
           '<div class="err" style="margin-top:10px">这个格式（%s）浏览器可能无法直接播放，可用下方「下载」。</div>'
           % _esc(row["ext"]),
           _fmt_duration(row["duration"]), _fmt_size(row["size"]),
           ("%dx%d" % (row["width"], row["height"])) if row["width"] else "-",
           time.strftime("%Y-%m-%d %H:%M",
                         time.localtime(row["created_at"] or 0)),
           int(row["play_count"] or 0),
           {"archived": "已归档", "local": "待归档",
            "uploading": "归档中", "error": "归档失败"}.get(
                row["status"], row["status"]),
           ("标签：" + tags_html + "<br>") if tags_html else "",
           "", _esc(row["source"] or "—"),
           _esc(row["title"]), _esc(row["tags"]), _esc(row["source"]),
           row["hash"], float(row["play_pos"] or 0)))
    return _page(row["title"], body, base_href=base_href)


def page_upload(base_href=""):
    body = (
        '<div class="topbar"><a class="btn ghost" href="./">← 返回</a>'
        '<h1>上传视频</h1></div>'
        '<div class="panel"><div style="display:grid;gap:9px">'
        '<input type="file" id="files" multiple accept="video/*,.mkv,.ts,.flv,.rmvb">'
        '<input type="text" id="tags" placeholder="标签（应用于本批，逗号分隔）" maxlength="200">'
        '<input type="text" id="source" placeholder="来源（应用于本批）" maxlength="120">'
        '<span><button id="go" type="button">开始上传</button> '
        '<span class="msg" id="msg"></span></span>'
        '<div id="list" style="display:grid;gap:10px"></div>'
        '</div>'
        '<p style="color:#8b94a7;font-size:12.5px;line-height:1.7">'
        '分片上传（8MB/片），可多选排队。文件先到服务器暂存区，后台算哈希去重、'
        '抽缩略图，再自动归档到谷歌硬盘并删除本地副本；同哈希重复上传会直接跳过。'
        '暂存区预算 %s，归档跟不上时请稍后再传。</p></div>'
        '<script>'
        'const CH=8*1024*1024;const list=document.getElementById("list");'
        'async function jpost(u,b){const r=await fetch(u,{method:"POST",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});'
        'return r.json();}'
        'async function uploadOne(f,row){'
        'const bar=row.querySelector("i"),st=row.querySelector(".st");'
        'const init=await jpost("api/uploads/init",{name:f.name,size:f.size,'
        'title:f.name.replace(/\\.[^.]+$/,""),'
        'tags:document.getElementById("tags").value,'
        'source:document.getElementById("source").value});'
        'if(!init.ok){st.textContent="失败："+(init.message||"");return;}'
        'const uid=init.uid;let off=0;'
        'while(off<f.size){const end=Math.min(f.size,off+CH);'
        'const blob=f.slice(off,end);'
        'const r=await fetch("api/uploads/"+uid+"/chunk?offset="+off,'
        '{method:"PUT",body:blob});const d=await r.json();'
        'if(!d.ok){st.textContent="分片失败："+(d.message||"");return;}'
        'off=end;bar.style.width=(off/f.size*100)+"%%";'
        'st.textContent="上传中 "+Math.round(off/f.size*100)+"%%";}'
        'const done=await jpost("api/uploads/"+uid+"/complete",{});'
        'if(done.ok){st.textContent=done.duplicate?"已存在（同哈希），跳过":"已入库，等待归档";'
        'bar.style.width="100%%";}else{st.textContent="失败："+(done.message||"");}}'
        'document.getElementById("go").onclick=async()=>{'
        'const fs=[...document.getElementById("files").files];'
        'if(!fs.length){document.getElementById("msg").textContent="先选择文件";return;}'
        'document.getElementById("msg").textContent="";'
        'for(const f of fs){const row=document.createElement("div");'
        'row.innerHTML="<div style=\'font-size:13.5px;margin-bottom:5px\'>"+'
        'f.name.replace(/</g,"&lt;")+" · "+(f.size/1048576).toFixed(1)+" MB · '
        '<span class=\'st\'>排队</span></div><div class=\'progress\'><i></i></div>";'
        'list.appendChild(row);await uploadOne(f,row);}'
        'document.getElementById("msg").textContent="本批处理完";};'
        '</script>' % _fmt_size(STAGING_BUDGET))
    return _page("上传", body, base_href=base_href)


def _admin_data():
    data = {"alist_ok": False, "alist_error": "", "storage": None,
            "counts": {}, "staging": _staging_bytes(),
            "disk_free": _disk_free(),
            "alist_pass_set": bool(_cred("alist_pass", "ALIST_PASS")),
            "google_token_set": bool(_cred("google_refresh_token")),
            "last_backup": kv_get("last_backup_at", ""),
            "last_backup_error": kv_get("last_backup_error", ""),
            "queue": []}
    with _db() as conn:
        for r in conn.execute(
                "SELECT status, COUNT(*) c FROM videos GROUP BY status"):
            data["counts"][r["status"]] = r["c"]
        data["queue"] = [dict(r) for r in conn.execute(
            "SELECT hash, title, status, error, retries FROM videos "
            "WHERE status IN ('local', 'uploading', 'error') "
            "ORDER BY created_at LIMIT 50")]
    try:
        st = ALIST.drive_storage()
        data["alist_ok"] = True
        if st:
            data["storage"] = {"driver": st.get("driver"),
                               "status": st.get("status")}
    except Exception as exc:
        data["alist_error"] = str(exc)[:200]
    return data


def page_admin(base_href=""):
    d = _admin_data()
    counts = " · ".join("%s %d" % ({"archived": "已归档", "local": "待归档",
                                    "uploading": "归档中",
                                    "error": "失败"}.get(k, k), v)
                         for k, v in sorted(d["counts"].items())) or "暂无"
    last_backup = ("从未" if not d["last_backup"] else time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(float(d["last_backup"]))))
    queue_rows = "".join(
        '<div class="kv">%s — %s%s %s</div>'
        % (_esc(r["title"]),
           {"local": "待归档", "uploading": "归档中",
            "error": "失败"}.get(r["status"], r["status"]),
           ("（重试 %d）" % r["retries"]) if r["retries"] else "",
           ('<button type="button" class="ghost retry" data-h="%s">重试</button>'
            % r["hash"]) if r["status"] == "error" else "",
           )
        + (('<div class="err">%s</div>' % _esc(r["error"]))
           if r["error"] else "")
        for r in d["queue"]) or '<div class="kv">队列为空。</div>'
    storage_line = "未连接"
    if d["storage"]:
        storage_line = "%s · 状态 %s" % (d["storage"]["driver"],
                                         d["storage"]["status"])
    body = (
        '<div class="topbar"><a class="btn ghost" href="./">← 返回</a>'
        '<h1>管理</h1></div>'
        '<div class="panel"><h2>状态</h2><div class="kv">'
        'AList：<b>%s</b>%s<br>谷歌硬盘存储：<b>%s</b><br>'
        '视频：<b>%s</b><br>暂存区：<b>%s</b> / %s · 磁盘剩余 <b>%s</b><br>'
        'AList 密码：<b>%s</b> · 谷歌令牌：<b>%s</b><br>'
        '数据库备份：<b>%s</b>%s</div>'
        '<p><button type="button" id="backup">立即备份数据库</button> '
        '<span class="msg" id="bmsg"></span></p></div>'
        '<div class="panel"><h2>连接谷歌硬盘</h2>'
        '<p style="color:#8b94a7;font-size:13px;line-height:1.7;margin-top:0">'
        '在本机浏览器打开 AList 官方令牌工具（alist.nn.ci 的 Google Drive 令牌页），'
        '用谷歌账号登录授权后，把得到的 refresh token 粘贴到下面（只导入、不回显）。'
        '根目录文件夹 ID 可选（留空 = 整个硬盘根目录，建议填一个专用文件夹的 ID）。</p>'
        '<div style="display:grid;gap:9px">'
        '<input type="password" id="gtoken" placeholder="Google refresh token（留空 = 不修改已保存的）" autocomplete="off">'
        '<input type="text" id="gfolder" placeholder="根目录文件夹 ID（可选）" value="%s">'
        '<span><button type="button" id="connect">保存并连接</button> '
        '<span class="msg" id="cmsg"></span></span></div></div>'
        '<div class="panel"><h2>归档队列</h2>%s</div>'
        '<script>'
        'async function jpost(u,b){const r=await fetch(u,{method:"POST",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});'
        'return r.json();}'
        'document.getElementById("connect").onclick=async()=>{'
        'const m=document.getElementById("cmsg");m.textContent="连接中…";'
        'const d=await jpost("api/admin/connect",{'
        'refresh_token:document.getElementById("gtoken").value,'
        'root_folder_id:document.getElementById("gfolder").value});'
        'm.textContent=d.ok?("完成：" + (d.message||"")):("失败：" + (d.message||""));'
        'if(d.ok)setTimeout(()=>location.reload(),1200);};'
        'document.getElementById("backup").onclick=async()=>{'
        'const d=await jpost("api/admin/backup",{});'
        'document.getElementById("bmsg").textContent=d.ok?"备份完成":"失败：" + (d.message||"");};'
        'document.querySelectorAll(".retry").forEach(b=>b.onclick=async()=>{'
        'await jpost("api/admin/retry",{hash:b.dataset.h});location.reload();});'
        '</script>'
        % ("已连接" if d["alist_ok"] else "不可达",
           ("（" + _esc(d["alist_error"]) + "）") if d["alist_error"] else "",
           storage_line, counts,
           _fmt_size(d["staging"]), _fmt_size(STAGING_BUDGET),
           _fmt_size(d["disk_free"]),
           "已配置" if d["alist_pass_set"] else "未配置",
           "已导入" if d["google_token_set"] else "未导入",
           last_backup,
           ("<br><span class='err'>上次备份错误：" + _esc(d["last_backup_error"]) + "</span>")
           if d["last_backup_error"] else "",
           _esc(_cred("google_root_folder_id")),
           queue_rows))
    return _page("管理", body, base_href=base_href)


# ------------------------------------------------------------- router

def _json(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def _read_json(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        length = 0
    raw = environ["wsgi.input"].read(length) if length > 0 else b""
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


def _get_video(hash_):
    if not re.fullmatch(r"[a-f0-9]{64}", hash_ or ""):
        return None
    with _db() as conn:
        return conn.execute("SELECT * FROM videos WHERE hash = ?",
                            (hash_,)).fetchone()


_PLACEHOLDER_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='180'>"
    "<rect width='320' height='180' fill='#171c27'/>"
    "<polygon points='140,70 140,110 178,90' fill='#3a4a68'/></svg>"
).encode("utf-8")


class _RangeBody:
    def __init__(self, fh, start, length):
        self.fh = fh
        self.fh.seek(start)
        self.left = length

    def __iter__(self):
        return self

    def __next__(self):
        if self.left <= 0:
            raise StopIteration
        block = self.fh.read(min(512 * 1024, self.left))
        if not block:
            raise StopIteration
        self.left -= len(block)
        return block

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


def _serve_file_range(environ, start_response, path, content_type):
    size = os.path.getsize(path)
    range_hdr = environ.get("HTTP_RANGE") or ""
    start, end, status = 0, size - 1, "200 OK"
    m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
    if m:
        if m.group(1):
            start = int(m.group(1))
        if m.group(2):
            end = int(m.group(2)) if m.group(1) else size - int(m.group(2))
            if not m.group(1):
                start = max(0, size - int(m.group(2)))
                end = size - 1
        end = min(end, size - 1)
        status = "206 Partial Content"
    length = max(0, end - start + 1)
    headers = [("Content-Type", content_type),
               ("Accept-Ranges", "bytes"),
               ("Content-Length", str(length))]
    if status.startswith("206"):
        headers.append(("Content-Range",
                        "bytes %d-%d/%d" % (start, end, size)))
    start_response(status, headers)
    return _RangeBody(open(path, "rb"), start, length)


def application(environ, start_response):
    setup_testing_defaults(environ)
    _ensure_worker()
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"
    prefix = environ.get("HTTP_X_FORWARDED_PREFIX") or ""
    base_href = (prefix + "/") if prefix else "/"
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    params = {k: v[0] for k, v in query.items()}

    if path == "/api/session":
        return _json(start_response, "200 OK",
                     {"ok": True, "authenticated": True,
                      "via": "platform", "username": "platform"})

    if path == "/" and method == "GET":
        body = page_wall(params, base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path.startswith("/watch/") and method == "GET":
        row = _get_video(path[len("/watch/"):])
        if row is None:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        body = page_watch(row, base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/upload" and method == "GET":
        body = page_upload(base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/admin" and method == "GET":
        body = page_admin(base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path.startswith("/thumb/"):
        hash_ = path[len("/thumb/"):]
        thumb = os.path.join(THUMB_DIR, hash_ + ".jpg")
        if os.path.exists(thumb):
            with open(thumb, "rb") as fh:
                data = fh.read()
            start_response("200 OK", [("Content-Type", "image/jpeg"),
                                      ("Content-Length", str(len(data))),
                                      ("Cache-Control", "public, max-age=86400")])
            return [data]
        start_response("200 OK", [("Content-Type", "image/svg+xml"),
                                  ("Content-Length", str(len(_PLACEHOLDER_SVG)))])
        return [_PLACEHOLDER_SVG]

    if path.startswith("/file/") and method == "GET":
        row = _get_video(path[len("/file/"):])
        if row is None or row["status"] == "archived":
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        local = _local_path(row)
        if not os.path.exists(local):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        return _serve_file_range(environ, start_response, local,
                                 "video/mp4")

    if path.startswith("/api/play/") and method == "GET":
        row = _get_video(path[len("/api/play/"):])
        if row is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "视频不存在。"})
        with _db() as conn:
            conn.execute("UPDATE videos SET play_count = play_count + 1 "
                         "WHERE hash = ?", (row["hash"],))
        if row["status"] == "archived":
            try:
                url = resolve_play_url(row)
            except Exception as exc:
                return _json(start_response, "502 Bad Gateway",
                             {"ok": False, "message": str(exc)[:200]})
            return _json(start_response, "200 OK",
                         {"ok": True, "url": url, "archived": True,
                          "pos": row["play_pos"] or 0})
        return _json(start_response, "200 OK",
                     {"ok": True, "url": "file/" + row["hash"],
                      "archived": False, "pos": row["play_pos"] or 0})

    if path == "/api/progress" and method == "POST":
        data = _read_json(environ)
        row = _get_video(data.get("hash") or "")
        if row is not None:
            with _db() as conn:
                conn.execute("UPDATE videos SET play_pos = ? WHERE hash = ?",
                             (float(data.get("pos") or 0), row["hash"]))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/meta" and method == "POST":
        data = _read_json(environ)
        row = _get_video(data.get("hash") or "")
        if row is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "视频不存在。"})
        with _db() as conn:
            conn.execute(
                "UPDATE videos SET title = ?, tags = ?, source = ?, "
                "updated_at = ? WHERE hash = ?",
                ((data.get("title") or row["title"])[:200],
                 (data.get("tags") or "")[:200],
                 (data.get("source") or "")[:120],
                 time.time(), row["hash"]))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/uploads/init" and method == "POST":
        data = _read_json(environ)
        try:
            uid = upload_init(data.get("name") or "", data.get("size") or 0,
                              data.get("title") or "", data.get("tags") or "",
                              data.get("source") or "")
        except ValueError as exc:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": str(exc)})
        return _json(start_response, "200 OK", {"ok": True, "uid": uid})

    m = re.fullmatch(r"/api/uploads/([a-f0-9]{20})/chunk", path)
    if m and method == "PUT":
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except Exception:
            length = 0
        data = environ["wsgi.input"].read(length) if length > 0 else b""
        try:
            upload_chunk(m.group(1), int(params.get("offset") or 0), data)
        except ValueError as exc:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": str(exc)})
        return _json(start_response, "200 OK", {"ok": True})

    m = re.fullmatch(r"/api/uploads/([a-f0-9]{20})/complete", path)
    if m and method == "POST":
        try:
            result = upload_complete(m.group(1))
        except ValueError as exc:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": str(exc)})
        return _json(start_response, "200 OK",
                     {"ok": True, **result})

    if path == "/api/batch-export" and method == "POST":
        data = _read_json(environ)
        hashes = data.get("hashes") or []
        lines = ["# 视频库批量下载列表（aria2 -i 使用；链接为限时签名链接，过期请重新导出）"]
        for h in hashes[:100]:
            row = _get_video(h or "")
            if row is None:
                continue
            safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", row["title"] or row["hash"])
            if row["status"] != "archived":
                lines.append("# 跳过（未归档）：%s" % row["title"])
                continue
            try:
                url = resolve_play_url(row)
            except Exception as exc:
                lines.append("# 取链接失败：%s（%s）"
                             % (row["title"], str(exc)[:80]))
                continue
            lines.append(url)
            lines.append("  out=%s.%s" % (safe, row["ext"] or "mp4"))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Disposition", 'attachment; filename="download-list.txt"'),
            ("Content-Length", str(len(body)))])
        return [body]

    if path == "/api/admin/connect" and method == "POST":
        data = _read_json(environ)
        creds = _load_creds()
        token = (data.get("refresh_token") or "").strip()
        if token:
            creds["google_refresh_token"] = token
        creds["google_root_folder_id"] = (
            data.get("root_folder_id") or "").strip()
        _save_creds(creds)
        token = creds.get("google_refresh_token") or ""
        if not token:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "还没有导入 refresh token。"})
        try:
            action = ALIST.ensure_drive_storage(
                token, creds.get("google_root_folder_id") or "")
            st = None
            for _ in range(6):
                st = ALIST.drive_storage()
                if st and st.get("status") == "work":
                    break
                time.sleep(1.5)
            status = (st or {}).get("status") or "unknown"
            return _json(start_response, "200 OK",
                         {"ok": status == "work",
                          "message": "存储%s，状态：%s" % (action, status)})
        except Exception as exc:
            return _json(start_response, "502 Bad Gateway",
                         {"ok": False, "message": str(exc)[:300]})

    if path == "/api/admin/backup" and method == "POST":
        kv_set("last_backup_at", "0")
        _maybe_backup_db()
        if kv_get("last_backup_error"):
            err = kv_get("last_backup_error")
            kv_set("last_backup_error", "")
            return _json(start_response, "502 Bad Gateway",
                         {"ok": False, "message": err})
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/admin/retry" and method == "POST":
        data = _read_json(environ)
        row = _get_video(data.get("hash") or "")
        if row is not None:
            with _db() as conn:
                conn.execute(
                    "UPDATE videos SET status = 'local', retries = 0, "
                    "error = '' WHERE hash = ?", (row["hash"],))
            _ensure_worker()
        return _json(start_response, "200 OK", {"ok": True})

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]


app = application
