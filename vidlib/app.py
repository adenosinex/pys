"""视频库 (vidlib) - App Hub project.

Three layers, files never move after ingest:
  * Library (this app): media files are content-addressed by
    SHA-256. Two tables keep identity straight:
      files   - one row per distinct file (hash PK): size,
                duration, Drive path, archive status.
      entries - one row per *visible item*: display title,
                date folder (YYYY-MM), tags, source, playback
                progress. Several entries can point at one file.
    Renames and deletes touch entries only; the Drive object
    (named by hash) never moves. Orphaned files are reaped a
    week after their last entry disappears.
  * Link source (AList): loopback only; holds the Google Drive
    mount and hands signed URLs to this backend via its API.
  * Surfaces: the web UI (thumbnail wall, database search,
    watch page) and a WebDAV translation layer at /dav - the
    WebDAV tree is virtual: folders are entry months, file
    names are entry titles, every verb resolves through the
    database to a hash and then to AList. Nothing on disk and
    nothing in Drive carries an original filename.
"""

import base64
import hashlib
import html
import http.client
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import mimetypes
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
ORPHAN_GRACE = 7 * 24 * 3600
VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "m4v", "avi", "ts", "flv",
              "wmv", "mpg", "mpeg", "3gp", "rmvb"}
PLAYABLE_EXTS = {"mp4", "webm", "m4v", "mov"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "avif", "svg",
              "heic", "heif", "tif", "tiff"}


def _kind(ext):
    ext = (ext or "").lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return "other"


_MIME_OVERRIDES = {"mkv": "video/x-matroska", "ts": "video/mp2t",
                   "flv": "video/x-flv", "wmv": "video/x-ms-wmv",
                   "rmvb": "application/vnd.rn-realmedia-vbr",
                   "m4v": "video/mp4", "3gp": "video/3gpp"}


def _mime_for(ext):
    ext = (ext or "").lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    return (mimetypes.guess_type("file." + (ext or "bin"))[0]
            or "application/octet-stream")

_db_lock = threading.Lock()
_worker_started = False
_worker_lock = threading.Lock()
_dav_locks = {}
_dav_locks_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            hash TEXT PRIMARY KEY,
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
            orphaned_at REAL DEFAULT 0,
            created_at REAL DEFAULT 0,
            updated_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT NOT NULL,
            title TEXT NOT NULL,
            folder TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            source TEXT DEFAULT '',
            play_pos REAL DEFAULT 0,
            play_count INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0,
            updated_at REAL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_entries_folder
            ON entries(folder);
        CREATE INDEX IF NOT EXISTS idx_entries_hash
            ON entries(hash);
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY, value TEXT DEFAULT ''
        );
        """)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "videos" in tables:
            count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            if count == 0:
                for row in conn.execute("SELECT * FROM videos").fetchall():
                    conn.execute(
                        "INSERT OR IGNORE INTO files(hash, ext, size, "
                        "duration, width, height, status, gpath, error, "
                        "retries, dup_count, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row["hash"], row["ext"], row["size"],
                         row["duration"], row["width"], row["height"],
                         row["status"], row["gpath"], row["error"],
                         row["retries"], row["dup_count"],
                         row["created_at"], row["updated_at"]))
                    folder = time.strftime(
                        "%Y-%m", time.localtime(row["created_at"] or 0))
                    conn.execute(
                        "INSERT INTO entries(hash, title, folder, tags, "
                        "source, play_pos, play_count, created_at, "
                        "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (row["hash"], row["title"], folder, row["tags"],
                         row["source"], row["play_pos"], row["play_count"],
                         row["created_at"], row["updated_at"]))
            conn.execute("ALTER TABLE videos RENAME TO videos_legacy")


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
#   alist_base / alist_user / alist_pass - this app -> AList
#   google_* - AList -> Drive (client id/secret + refresh token)
#   dav_salt / dav_pass_hash - WebDAV Basic password (hashed)

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


def dav_password_set(password):
    creds = _load_creds()
    salt = secrets.token_hex(8)
    creds["dav_salt"] = salt
    creds["dav_pass_hash"] = hashlib.sha256(
        (salt + password).encode("utf-8")).hexdigest()
    _save_creds(creds)


def dav_password_check(password):
    creds = _load_creds()
    salt = creds.get("dav_salt") or ""
    want = creds.get("dav_pass_hash") or ""
    if not salt or not want:
        return False
    got = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return secrets.compare_digest(got, want)


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

    def ensure_drive_storage(self, refresh_token, root_folder_id="",
                             client_id="", client_secret=""):
        addition = json.dumps({
            "root_folder_id": root_folder_id or "",
            "client_id": client_id or "",
            "client_secret": client_secret or "",
            "refresh_token": refresh_token,
            "chunk_size": 5,
        })
        st = self.drive_storage()
        if st:
            payload = dict(st)
            payload["addition"] = addition
            payload["status"] = "work"
            self.api("POST", "/api/admin/storage/update", payload)
            return "updated"
        self.api("POST", "/api/admin/storage/create", {
            "mount_path": "/", "order": 0, "driver": "GoogleDrive",
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


def _check_capacity(extra_bytes):
    if _staging_bytes() + extra_bytes > STAGING_BUDGET:
        raise ValueError("暂存区预算不足（上限 %s），请等已上传的归档完成后再传。"
                         % _fmt_size(STAGING_BUDGET))
    if extra_bytes > _disk_free() - DISK_MIN_FREE:
        raise ValueError("服务器磁盘剩余空间不足。")


# --------------------------------------------------- files / entries

def get_file(hash_):
    if not re.fullmatch(r"[a-f0-9]{64}", hash_ or ""):
        return None
    with _db() as conn:
        return conn.execute("SELECT * FROM files WHERE hash = ?",
                            (hash_,)).fetchone()


def get_entry(entry_id):
    try:
        entry_id = int(entry_id)
    except Exception:
        return None
    with _db() as conn:
        return conn.execute(
            "SELECT e.*, f.ext AS f_ext, f.size AS f_size, "
            "f.duration AS f_duration, f.width AS f_width, "
            "f.height AS f_height, f.status AS f_status, "
            "f.gpath AS f_gpath, f.error AS f_error "
            "FROM entries e JOIN files f ON f.hash = e.hash "
            "WHERE e.id = ?", (entry_id,)).fetchone()


def _folder_of(ts):
    return time.strftime("%Y-%m", time.localtime(ts))


def _unique_title(conn, folder, title, exclude_id=None):
    base = title
    candidate = base
    n = 2
    while True:
        row = conn.execute(
            "SELECT id FROM entries WHERE folder = ? AND title = ?",
            (folder, candidate)).fetchone()
        if row is None or (exclude_id is not None
                           and row["id"] == exclude_id):
            return candidate
        candidate = "%s (%d)" % (base, n)
        n += 1


def insert_entry(hash_, title, folder, tags="", source=""):
    now = time.time()
    with _db() as conn:
        title = _unique_title(conn, folder, title[:200])
        cur = conn.execute(
            "INSERT INTO entries(hash, title, folder, tags, source, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (hash_, title, folder, (tags or "")[:200],
             (source or "")[:120], now, now))
        return cur.lastrowid


def delete_entry(entry_id):
    with _db() as conn:
        row = conn.execute("SELECT hash FROM entries WHERE id = ?",
                           (entry_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        left = conn.execute("SELECT COUNT(*) c FROM entries WHERE hash = ?",
                            (row["hash"],)).fetchone()["c"]
        if left == 0:
            conn.execute(
                "UPDATE files SET orphaned_at = ? WHERE hash = ? "
                "AND orphaned_at = 0", (time.time(), row["hash"]))
    return True


def _local_path(file_row):
    return os.path.join(STAGING_DIR,
                        file_row["hash"] + "." + (file_row["ext"] or "mp4"))


def ingest_staged_file(staged_path, display_name, folder=None, tags="",
                       source=""):
    """Hash a fully-received staging file, dedupe, and register it.

    Returns (entry_id, file_hash, is_duplicate_file). The staging
    file is consumed: deleted for duplicates, renamed to its hash
    name for new files (until the worker archives it)."""
    if os.path.getsize(staged_path) == 0:
        os.unlink(staged_path)
        raise ValueError("空文件（0 字节），已忽略。")
    digest = _sha256_file(staged_path)
    orig = display_name or "video.mp4"
    ext = ""
    if "." in orig:
        cand = orig.rsplit(".", 1)[1].lower()
        if re.fullmatch(r"[a-z0-9]{1,5}", cand):
            ext = cand
    title = os.path.splitext(orig)[0] or digest[:8]
    now = time.time()
    folder = folder or _folder_of(now)
    existing = get_file(digest)
    if existing is not None:
        os.unlink(staged_path)
        with _db() as conn:
            conn.execute(
                "UPDATE files SET dup_count = dup_count + 1, "
                "orphaned_at = 0, updated_at = ? WHERE hash = ?",
                (now, digest))
        entry_id = insert_entry(digest, title, folder, tags, source)
        return entry_id, digest, True
    if ext not in VIDEO_EXTS:
        ext = ext if ext else "mp4"
    final = os.path.join(STAGING_DIR, digest + "." + ext)
    os.replace(staged_path, final)
    meta = _probe(final)
    _probe_cache[digest] = meta
    _make_thumb(final, digest)
    size = os.path.getsize(final)
    with _db() as conn:
        conn.execute(
            "INSERT INTO files(hash, ext, size, duration, width, height, "
            "status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,'local',?,?)",
            (digest, ext, size, meta.get("duration") or 0,
             meta.get("width") or 0, meta.get("height") or 0, now, now))
    entry_id = insert_entry(digest, title, folder, tags, source)
    _ensure_worker()
    return entry_id, digest, False


# ------------------------------------------------------- web uploads

_uploads = {}
_uploads_lock = threading.Lock()
UPLOAD_CHUNK = 8 * 1024 * 1024


def upload_init(name, size, title, tags, source):
    size = int(size or 0)
    if size <= 0:
        raise ValueError("文件大小无效。")
    _check_capacity(size)
    uid = hashlib.sha256(
        ("%s:%s:%s" % (name, size, time.time())).encode()).hexdigest()[:20]
    part_path = os.path.join(STAGING_DIR, uid + ".part")
    with open(part_path, "wb"):
        pass
    with _uploads_lock:
        _uploads[uid] = {"name": name, "size": size, "title": title,
                         "tags": tags, "source": source,
                         "path": part_path, "at": time.time(),
                         "got": set()}
    return uid


def upload_chunk(uid, offset, data):
    with _uploads_lock:
        sess = _uploads.get(uid)
    if sess is None:
        raise ValueError("上传会话不存在或已过期。")
    with open(sess["path"], "r+b") as fh:
        fh.seek(int(offset))
        fh.write(data)
    with _uploads_lock:
        sess["got"].add(int(offset) // UPLOAD_CHUNK)
    return len(data)


def upload_complete(uid):
    with _uploads_lock:
        sess = _uploads.pop(uid, None)
    if sess is None:
        raise ValueError("上传会话不存在或已过期。")
    part = sess["path"]
    expected_chunks = -(-sess["size"] // UPLOAD_CHUNK)
    if len(sess["got"]) < expected_chunks:
        with _uploads_lock:
            _uploads[uid] = sess
        raise ValueError("分片不完整：收到 %d / %d 片，"
                         "请补传缺失分片后重试。"
                         % (len(sess["got"]), expected_chunks))
    actual = os.path.getsize(part)
    if actual != sess["size"]:
        raise ValueError("文件不完整：收到 %d / 应为 %d 字节。"
                         % (actual, sess["size"]))
    entry_id, digest, dup = ingest_staged_file(
        part, sess["name"], tags=sess["tags"], source=sess["source"])
    return {"entry_id": entry_id, "hash": digest, "duplicate": dup}


# ------------------------------------------------------------- worker

def _remote_size(remote_path):
    """Size of a remote object, or None when absent. Deliberately
    reads a refreshed folder listing: AList's fs/get resolves
    through a cached listing and reports a just-uploaded file as
    'object not found' until the cache catches up, which used to
    fail the post-upload check and re-upload the same file."""
    dir_path, _, name = remote_path.rpartition("/")
    for item in ALIST.fs_list(dir_path or "/"):
        if item.get("name") == name and not item.get("is_dir"):
            try:
                return int(item.get("size") or 0)
            except Exception:
                return 0
    return None


def _archive_one(row):
    local = _local_path(row)
    remote = "/videos/" + row["hash"] + "." + (row["ext"] or "mp4")
    if not kv_get("videos_dir_ready"):
        try:
            ALIST.fs_mkdir("/videos")
        except Exception:
            pass
        kv_set("videos_dir_ready", "1")
    # Idempotent: a matching object already on Drive (e.g. an
    # earlier attempt whose upload landed but whose bookkeeping
    # failed) is adopted, never uploaded again.
    remote_size = _remote_size(remote)
    if remote_size is None:
        ALIST.upload_file(local, remote)
        for attempt in range(6):
            time.sleep(2 if attempt == 0 else 5)
            remote_size = _remote_size(remote)
            if remote_size is not None:
                break
    if remote_size is None:
        raise AlistError("上传后仍无法在谷歌硬盘确认该文件。")
    if int(remote_size) != int(row["size"]):
        raise AlistError("归档校验失败：远端大小 %s 与本地 %s 不一致。"
                         % (remote_size, row["size"]))
    with _db() as conn:
        conn.execute(
            "UPDATE files SET status = 'archived', gpath = ?, error = '', "
            "updated_at = ? WHERE hash = ?",
            (remote, time.time(), row["hash"]))
    try:
        os.unlink(local)
    except Exception:
        pass


def _reap_orphans():
    """Delete Drive objects + rows for files with no entries left,
    after a grace period (protects against accidental deletes)."""
    cutoff = time.time() - ORPHAN_GRACE
    with _db() as conn:
        rows = conn.execute(
            "SELECT f.* FROM files f WHERE f.orphaned_at > 0 "
            "AND f.orphaned_at < ? AND NOT EXISTS "
            "(SELECT 1 FROM entries e WHERE e.hash = f.hash)",
            (cutoff,)).fetchall()
    for row in rows:
        try:
            if row["status"] == "archived" and row["gpath"]:
                dir_path, _, name = row["gpath"].rpartition("/")
                ALIST.fs_remove(dir_path or "/", [name])
            local = _local_path(row)
            if os.path.exists(local):
                os.unlink(local)
            thumb = os.path.join(THUMB_DIR, row["hash"] + ".jpg")
            if os.path.exists(thumb):
                os.unlink(thumb)
            with _db() as conn:
                conn.execute("DELETE FROM files WHERE hash = ?",
                             (row["hash"],))
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
                    # Rescue files stuck in 'uploading' by a crash
                    # or restart mid-archive.
                    conn.execute(
                        "UPDATE files SET status = 'local' "
                        "WHERE status = 'uploading' AND updated_at < ?",
                        (time.time() - 900,))
                    cand = conn.execute(
                        "SELECT hash FROM files WHERE status = 'local' "
                        "ORDER BY created_at LIMIT 1").fetchone()
                    row = None
                    if cand:
                        # Claim atomically: a second worker that
                        # loses the race sees rowcount 0 and skips.
                        cur = conn.execute(
                            "UPDATE files SET status = 'uploading', "
                            "updated_at = ? WHERE hash = ? "
                            "AND status = 'local'",
                            (time.time(), cand["hash"]))
                        if cur.rowcount:
                            row = conn.execute(
                                "SELECT * FROM files WHERE hash = ?",
                                (cand["hash"],)).fetchone()
                if row:
                    try:
                        _archive_one(row)
                    except Exception as exc:
                        retries = int(row["retries"] or 0) + 1
                        new_status = "error" if retries >= 5 else "local"
                        with _db() as conn:
                            conn.execute(
                                "UPDATE files SET status = ?, retries = ?, "
                                "error = ?, updated_at = ? WHERE hash = ?",
                                (new_status, retries, str(exc)[:300],
                                 time.time(), row["hash"]))
                _reap_orphans()
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

def _signed_url_raw(file_row):
    info = ALIST.fs_get(file_row["gpath"])
    return (info or {}).get("raw_url") or "", (info or {}).get("sign") or ""


def resolve_play_url(file_row):
    """Browser-usable URL for an archived file: prefer AList's
    raw_url when it already points at a public host (Google direct
    link); otherwise rewrite the AList-local URL onto the public
    video base (Caddy forwards /d/ and /p/ to loopback AList)."""
    raw, sign = _signed_url_raw(file_row)
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
        return (PUBLIC_VIDEO_BASE + "/d" + file_row["gpath"]
                + "?sign=" + urllib.parse.quote(sign))
    return PUBLIC_VIDEO_BASE + "/d" + file_row["gpath"]


def resolve_loopback_url(file_row):
    """Same signed URL but kept on the loopback AList origin, for
    server-side streaming (WebDAV GET)."""
    raw, sign = _signed_url_raw(file_row)
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        if parsed.hostname not in ("127.0.0.1", "localhost"):
            return raw
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
        return ALIST.base() + path
    if raw.startswith("/"):
        return ALIST.base() + raw
    if sign:
        return (ALIST.base() + "/d" + file_row["gpath"]
                + "?sign=" + urllib.parse.quote(sign))
    return ALIST.base() + "/d" + file_row["gpath"]


# -------------------------------------------------------------- pages

def _esc(v):
    return html.escape(str(v if v is not None else ""), quote=True)


def _status_chip(status):
    if status == "archived":
        return ""
    label = {"local": "待归档", "uploading": "归档中",
             "error": "归档失败"}.get(status, status)
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
button.danger { background: #7a2e2e; }
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
code { background: #232a3a; padding: 1px 6px; border-radius: 5px; font-size: 12.5px; }
"""


def _page(title, body, extra_head="", base_href=""):
    base_tag = ('<base href="%s">' % _esc(base_href)) if base_href else ""
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>%s · 视频库</title>%s<style>%s</style>%s</head>"
            "<body><div class=\"wrap\">%s</div></body></html>"
            % (_esc(title), base_tag, BASE_CSS, extra_head,
               body)).encode("utf-8")


_ENTRY_COLS = (
    "SELECT e.id AS id, e.hash AS hash, e.title AS title, "
    "e.folder AS folder, e.tags AS tags, e.source AS source, "
    "e.play_pos AS play_pos, e.play_count AS play_count, "
    "e.created_at AS created_at, f.ext AS ext, f.size AS size, "
    "f.duration AS duration, f.width AS width, f.height AS height, "
    "f.status AS status, f.gpath AS gpath, f.error AS error "
    "FROM entries e JOIN files f ON f.hash = e.hash ")


def page_wall(params, base_href=""):
    q = (params.get("q") or "").strip()
    dur = params.get("dur") or ""
    kind = params.get("kind") or ""
    tag = (params.get("tag") or "").strip()
    page = max(1, int(params.get("page") or 1))
    where, args = [], []
    if q:
        where.append("(e.title LIKE ? OR e.tags LIKE ? OR e.source LIKE ?)")
        like = "%" + q + "%"
        args += [like, like, like]
    if tag:
        where.append("e.tags LIKE ?")
        args.append("%" + tag + "%")
    if dur == "0":
        where.append("f.duration > 0 AND f.duration < 60")
    elif dur == "1":
        where.append("f.duration >= 60 AND f.duration < 300")
    elif dur == "2":
        where.append("f.duration >= 300 AND f.duration < 1800")
    elif dur == "3":
        where.append("f.duration >= 1800")
    if kind in ("video", "image"):
        exts = sorted(VIDEO_EXTS if kind == "video" else IMAGE_EXTS)
        where.append("LOWER(f.ext) IN (%s)" % ",".join("?" * len(exts)))
        args += exts
    elif kind == "other":
        exts = sorted(VIDEO_EXTS | IMAGE_EXTS)
        where.append("LOWER(f.ext) NOT IN (%s)" % ",".join("?" * len(exts)))
        args += exts
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    per = 60
    with _db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM entries e JOIN files f "
            "ON f.hash = e.hash" + sql_where, args).fetchone()["c"]
        rows = conn.execute(
            _ENTRY_COLS + sql_where
            + " ORDER BY e.created_at DESC LIMIT ? OFFSET ?",
            args + [per, (page - 1) * per]).fetchall()
        tag_rows = conn.execute(
            "SELECT tags FROM entries WHERE tags != ''").fetchall()
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
            '<input type="checkbox" class="pick" value="%d">'
            '<a href="watch/%d"><span class="thumb" style="display:block">'
            '<img loading="lazy" src="thumb/%s" alt="">'
            '<span class="dur">%s</span></span>'
            '<span class="t">%s %s</span></a>'
            '<div class="m">%s · %s%s</div></div>'
            % (r["id"], r["id"], r["hash"],
               _fmt_duration(r["duration"]), _esc(r["title"]),
               _status_chip(r["status"]),
               _fmt_size(r["size"]), date,
               {"image": " · 图片",
                "other": " · 文件(.%s)" % (r["ext"] or "?")}.get(
                    _kind(r["ext"]), "")
               + ((" · " + _esc(r["tags"])) if r["tags"] else "")))
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
        '<select name="kind">'
        '<option value="">全部类型</option>'
        '<option value="video"%s>视频</option>'
        '<option value="image"%s>图片</option>'
        '<option value="other"%s>其他</option></select>'
        '<button type="submit">搜索</button></form>'
        '<a class="btn" href="upload">上传</a>'
        '<a class="btn ghost" href="admin">管理</a></div>'
        '<div style="margin-bottom:12px;font-size:13px">共 %d 个条目 %s</div>'
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
        'const ids=sync();if(!ids.length)return;'
        'const r=await fetch("api/batch-export",{method:"POST",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify({ids:ids})});'
        'const blob=await r.blob();const a=document.createElement("a");'
        'a.href=URL.createObjectURL(blob);a.download="download-list.txt";a.click();};'
        '</script>'
        % (_esc(q),
           ' selected' if dur == "0" else '', ' selected' if dur == "1" else '',
           ' selected' if dur == "2" else '', ' selected' if dur == "3" else '',
           ' selected' if kind == "video" else '',
           ' selected' if kind == "image" else '',
           ' selected' if kind == "other" else '',
           total, ("· 标签：" + tag_links) if tag_links else "",
           "".join(cards) or '<div class="empty">还没有视频。点右上角「上传」添加第一个。</div>',
           pager))
    return _page("视频库", body, base_href=base_href)


def page_watch(entry, base_href=""):
    if entry is None:
        return None
    playable = (entry["f_ext"] or "") in PLAYABLE_EXTS
    kind = _kind(entry["f_ext"])
    if kind == "image":
        media_html = (
            '<img id="v" src="thumb/%s" alt="" '
            'style="width:100%%;border-radius:12px;background:#111">'
            % entry["hash"])
        note_html = ""
    elif kind == "other":
        media_html = (
            '<div id="v" class="panel" style="text-align:center;'
            'color:#9db2c8">这个文件（.%s）不支持在线预览，'
            '请用下方「下载」获取。</div>' % _esc(entry["f_ext"]))
        note_html = ""
    else:
        media_html = (
            '<video id="v" controls playsinline preload="metadata" '
            'poster="thumb/%s" '
            'style="width:100%%;background:#000;border-radius:12px"></video>'
            % entry["hash"])
        note_html = "" if playable else (
            '<div class="err" style="margin-top:10px">这个格式（%s）浏览器'
            '可能无法直接播放，可用下方「下载」。</div>'
            % _esc(entry["f_ext"]))
    tags_html = ""
    if entry["tags"]:
        tags_html = " · ".join(
            '<a class="tag" href="./?tag=%s">#%s</a>'
            % (urllib.parse.quote(t), _esc(t))
            for t in re.split(r"[,，\s]+", entry["tags"]) if t)
    body = (
        '<div class="topbar"><a class="btn ghost" href="./">← 返回</a>'
        '<h1 style="font-size:17px">%s</h1></div>'
        '%s'
        '%s'
        '<div class="panel" style="margin-top:14px"><div class="kv">'
        '时长 <b>%s</b> · 大小 <b>%s</b> · 分辨率 <b>%s</b><br>'
        '上传于 <b>%s</b> · 播放 <b>%d</b> 次 · 状态 <b>%s</b><br>'
        '%s'
        '来源：%s<br>'
        '<a class="btn" id="dl" href="#" target="_blank" rel="noopener">下载</a> '
        '<button class="ghost" id="rate">倍速 1×</button> '
        '<button class="danger" id="del">删除</button>'
        '</div></div>'
        '<div class="panel"><h2>编辑信息</h2>'
        '<form id="metaform"><div style="display:grid;gap:9px">'
        '<input type="text" id="m-title" value="%s" maxlength="200">'
        '<input type="text" id="m-tags" value="%s" placeholder="标签，逗号分隔" maxlength="200">'
        '<input type="text" id="m-source" value="%s" placeholder="来源" maxlength="120">'
        '<span><button type="submit">保存</button> <span class="msg" id="m-msg"></span></span>'
        '</div></form></div>'
        '<script>'
        'const EID=%d;const v=document.getElementById("v");'
        'let url="",lastSave=0;'
        'async function fresh(){const r=await fetch("api/play/"+EID);'
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
        'body:JSON.stringify({id:EID,pos:v.currentTime})});}});'
        'document.getElementById("rate").onclick=e=>{'
        'const rates=[1,1.25,1.5,2,0.75];let i=rates.indexOf(v.playbackRate);'
        'i=(i+1)%%rates.length;v.playbackRate=rates[i];'
        'e.target.textContent="倍速 "+rates[i]+"×";};'
        'document.getElementById("del").onclick=async()=>{'
        'if(!confirm("删除这个条目？源文件会在没有其他条目引用时于一周后清理。"))return;'
        'const r=await fetch("api/entries/delete",{method:"POST",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify({id:EID})});'
        'if((await r.json()).ok)location.href="./";};'
        'document.getElementById("metaform").onsubmit=async e=>{e.preventDefault();'
        'const r=await fetch("api/meta",{method:"POST",headers:{"Content-Type":"application/json"},'
        'body:JSON.stringify({id:EID,title:document.getElementById("m-title").value,'
        'tags:document.getElementById("m-tags").value,source:document.getElementById("m-source").value})});'
        'document.getElementById("m-msg").textContent=(await r.json()).ok?"已保存":"保存失败";};'
        'start();'
        '</script>'
        % (_esc(entry["title"]), media_html, note_html,
           _fmt_duration(entry["f_duration"]) if kind == "video" else "—",
           _fmt_size(entry["f_size"]),
           ("%dx%d" % (entry["f_width"], entry["f_height"]))
           if entry["f_width"] else "-",
           time.strftime("%Y-%m-%d %H:%M",
                         time.localtime(entry["created_at"] or 0)),
           int(entry["play_count"] or 0),
           {"archived": "已归档", "local": "待归档",
            "uploading": "归档中", "error": "归档失败"}.get(
                entry["f_status"], entry["f_status"]),
           ("标签：" + tags_html + "<br>") if tags_html else "",
           _esc(entry["source"] or "—"),
           _esc(entry["title"]), _esc(entry["tags"]), _esc(entry["source"]),
           entry["id"]))
    if kind != "video":
        body = body.replace(
            '<button class="ghost" id="rate">',
            '<button class="ghost" id="rate" style="display:none">')
    return _page(entry["title"], body, base_href=base_href)


def page_upload(base_href=""):
    body = (
        '<div class="topbar"><a class="btn ghost" href="./">← 返回</a>'
        '<h1>上传文件</h1></div>'
        '<div class="panel" id="dz"><div style="display:grid;gap:9px">'
        '<input type="file" id="files" multiple>'
        '<div style="color:#8b94a7;font-size:13px">也可以把文件直接拖到这个面板里松手。</div>'
        '<input type="text" id="tags" placeholder="标签（应用于本批，逗号分隔）" maxlength="200">'
        '<input type="text" id="source" placeholder="来源（应用于本批）" maxlength="120">'
        '<span><button id="go" type="button">开始上传</button> '
        '<span class="msg" id="msg"></span></span>'
        '<div id="sum" style="display:none">'
        '<div class="progress"><i id="sumbi"></i></div>'
        '<div id="sumtx" style="font-size:13px;margin-top:5px"></div></div>'
        '<div id="list" style="display:grid;gap:10px"></div>'
        '</div>'
        '<p style="color:#8b94a7;font-size:12.5px;line-height:1.7">'
        '分片上传（8MB/片）：最多 3 个文件并行、每个文件 3 片并行，失败分片自动重试 3 次。'
        '速度为近几秒的实时速度，不是全程平均。文件先到服务器暂存区，后台算哈希去重、'
        '抽缩略图，再自动归档到谷歌硬盘并删除本地副本；同哈希重复上传只新增一个条目，'
        '不重复占谷歌空间。暂存区预算 %s，归档跟不上时请稍后再传。</p></div>'
        '<script>'
        'const CH=8*1024*1024,PARF=3,PARC=3;'
        'const listEl=document.getElementById("list");'
        'const jobs=[];let samples=[],timer=null,cursor=0,workers=0;'
        'function fmtB(b){if(b>=1048576)return (b/1048576).toFixed(2)+" MB";'
        'if(b>=1024)return (b/1024).toFixed(1)+" KB";return b+" B";}'
        'function fmtS(b){return fmtB(b)+"/s";}'
        'function fmtEta(s){if(!isFinite(s))return "";if(s<60)return "约 "+Math.ceil(s)+" 秒";'
        'if(s<3600)return "约 "+Math.ceil(s/60)+" 分钟";return "约 "+(s/3600).toFixed(1)+" 小时";}'
        'function jpost(u,b){return fetch(u,{method:"POST",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}).then(r=>r.json());}'
        'function jobLoaded(j){let t=j.acked;for(const v of j.liveMap.values())t+=v;'
        'return Math.min(j.file.size,t);}'
        'function totals(){let l=0,s=0;for(const j of jobs){s+=j.file.size;'
        'l+=j.done?j.file.size:jobLoaded(j);}return [l,s];}'
        'function tick(){const now=Date.now();const tt=totals();'
        'samples.push([now,tt[0]]);'
        'while(samples.length>2&&now-samples[0][0]>6000)samples.shift();'
        'let sp=0;if(samples.length>1){const o=samples[0],dt=(now-o[0])/1000;'
        'if(dt>=1)sp=(tt[0]-o[1])/dt;}'
        'document.getElementById("sumbi").style.width=(tt[1]?(tt[0]/tt[1]*100):0)+"%%";'
        'let tx="已传 "+fmtB(tt[0])+" / "+fmtB(tt[1])+" · 当前速度 "+fmtS(sp);'
        'if(sp>0&&tt[0]<tt[1])tx+=" · 剩余 "+fmtB(tt[1]-tt[0])+" · "+fmtEta((tt[1]-tt[0])/sp);'
        'document.getElementById("sumtx").textContent=tx;'
        'for(const j of jobs){if(j.state==="up"){const p=j.file.size?jobLoaded(j)/j.file.size:0;'
        'j.barEl.style.width=(p*100)+"%%";'
        'j.stEl.textContent="上传中 "+Math.round(p*100)+"%% · "+fmtB(jobLoaded(j))+" / "+fmtB(j.file.size);}}}'
        'function sendChunk(job,idx){const f=job.file,off=idx*CH,'
        'end=Math.min(f.size,off+CH),blob=f.slice(off,end);let tries=0;'
        'const attempt=()=>new Promise((res,rej)=>{const x=new XMLHttpRequest();'
        'x.open("PUT","api/uploads/"+job.uid+"/chunk?offset="+off);'
        'x.upload.onprogress=e=>{job.liveMap.set(idx,e.loaded);};'
        'x.onload=()=>{try{const d=JSON.parse(x.responseText);'
        'if(d.ok)res();else rej(new Error(d.message||"分片被拒绝"));}'
        'catch(_){rej(new Error("分片响应异常"));}};'
        'x.onerror=()=>rej(new Error("网络错误"));x.send(blob);});'
        'const go=()=>attempt().catch(e=>{tries++;'
        'if(tries<3)return new Promise(r=>setTimeout(r,1000)).then(go);throw e;});'
        'return go();}'
        'async function runJob(job){const f=job.file;job.state="init";'
        'job.stEl.textContent="初始化…";'
        'let init;try{init=await jpost("api/uploads/init",{name:f.name,size:f.size,'
        'title:f.name.replace(/\\.[^.]+$/,""),'
        'tags:document.getElementById("tags").value,'
        'source:document.getElementById("source").value});}catch(e){init=null;}'
        'if(!init||!init.ok){job.state="fail";job.done=true;'
        'job.stEl.textContent="失败："+((init&&init.message)||"网络错误");showRetry(job);return;}'
        'job.uid=init.uid;job.state="up";job.acked=0;job.liveMap.clear();'
        'const n=Math.ceil(f.size/CH);let next=0,active=0,failed=null;'
        'await new Promise(fin=>{const launch=()=>{'
        'if(failed){if(!active)fin();return;}'
        'while(active<PARC&&next<n){const idx=next++;active++;'
        'sendChunk(job,idx).then(()=>{active--;'
        'job.acked+=Math.min(CH,f.size-idx*CH);job.liveMap.delete(idx);'
        'if(next>=n&&!active)fin();else launch();})'
        '.catch(e=>{active--;job.liveMap.delete(idx);if(!failed)failed=e;'
        'if(!active)fin();else launch();});}'
        'if(!n)fin();};launch();});'
        'if(failed){job.state="fail";job.done=true;'
        'job.stEl.textContent="失败："+failed.message;showRetry(job);return;}'
        'job.state="fin";job.stEl.textContent="入库中…";'
        'let done;try{done=await jpost("api/uploads/"+job.uid+"/complete",{});}catch(e){done=null;}'
        'job.done=true;'
        'if(done&&done.ok){job.state="ok";job.barEl.style.width="100%%";'
        'job.stEl.textContent=done.duplicate?"内容已存在，已新增条目（不重复占空间）":"已入库，等待归档";}'
        'else{job.state="fail";job.stEl.textContent="失败："+((done&&done.message)||"网络错误");showRetry(job);}}'
        'function showRetry(job){if(job.retryBtn)return;'
        'const b=document.createElement("button");b.type="button";'
        'b.className="ghost";b.textContent="重试";b.style.marginLeft="8px";'
        'b.onclick=()=>{b.remove();job.retryBtn=null;job.done=false;'
        'job.barEl.style.width="0%%";runJob(job);};'
        'job.stEl.after(b);job.retryBtn=b;}'
        'async function worker(){workers++;'
        'while(cursor<jobs.length){const j=jobs[cursor++];'
        'if(j.state==="queue")await runJob(j);}'
        'workers--;if(!workers){clearInterval(timer);timer=null;tick();'
        'document.getElementById("msg").textContent="本批处理完";}}'
        'function pump(){document.getElementById("sum").style.display="";'
        'if(!timer){samples=[];timer=setInterval(tick,500);}'
        'while(workers<PARF)worker();}'
        'function enqueue(fs){for(const f of fs){'
        'const row=document.createElement("div");'
        'row.innerHTML="<div style=\'font-size:13.5px;margin-bottom:5px\'>"+'
        'f.name.replace(/</g,"&lt;")+" · "+fmtB(f.size)+" · '
        '<span class=\'st\'>排队</span></div><div class=\'progress\'><i></i></div>";'
        'listEl.appendChild(row);'
        'jobs.push({file:f,row:row,stEl:row.querySelector(".st"),'
        'barEl:row.querySelector("i"),acked:0,liveMap:new Map(),'
        'state:"queue",done:false,retryBtn:null});}'
        'if(fs.length)pump();}'
        'document.getElementById("go").onclick=()=>{'
        'const fs=[...document.getElementById("files").files];'
        'if(!fs.length){document.getElementById("msg").textContent="先选择文件";return;}'
        'document.getElementById("msg").textContent="";enqueue(fs);};'
        'const dz=document.getElementById("dz");'
        'dz.addEventListener("dragover",e=>{e.preventDefault();dz.style.outline="2px dashed #4f7cff";});'
        'dz.addEventListener("dragleave",()=>{dz.style.outline="";});'
        'dz.addEventListener("drop",e=>{e.preventDefault();dz.style.outline="";'
        'document.getElementById("msg").textContent="";'
        'enqueue([...e.dataTransfer.files]);});'
        '</script>' % _fmt_size(STAGING_BUDGET))
    return _page("上传", body, base_href=base_href)


def _admin_data():
    data = {"alist_ok": False, "alist_error": "", "storage": None,
            "counts": {}, "entries": 0, "staging": _staging_bytes(),
            "disk_free": _disk_free(),
            "alist_pass_set": bool(_cred("alist_pass", "ALIST_PASS")),
            "google_token_set": bool(_cred("google_refresh_token")),
            "google_client_set": bool(_cred("google_client_id")
                                      and _cred("google_client_secret")),
            "dav_set": bool(_load_creds().get("dav_pass_hash")),
            "last_backup": kv_get("last_backup_at", ""),
            "last_backup_error": kv_get("last_backup_error", ""),
            "queue": []}
    with _db() as conn:
        for r in conn.execute(
                "SELECT status, COUNT(*) c FROM files GROUP BY status"):
            data["counts"][r["status"]] = r["c"]
        data["entries"] = conn.execute(
            "SELECT COUNT(*) c FROM entries").fetchone()["c"]
        data["queue"] = [dict(r) for r in conn.execute(
            "SELECT hash, ext, status, error, retries FROM files "
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
        % (_esc(r["hash"][:10] + "…" + ("." + r["ext"] if r["ext"] else "")),
           {"local": "待归档", "uploading": "归档中",
            "error": "失败"}.get(r["status"], r["status"]),
           ("（重试 %d）" % r["retries"]) if r["retries"] else "",
           ('<button type="button" class="ghost retry" data-h="%s">重试</button>'
            % r["hash"]) if r["status"] == "error" else "")
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
        '文件：<b>%s</b> · 条目：<b>%d</b><br>'
        '暂存区：<b>%s</b> / %s · 磁盘剩余 <b>%s</b><br>'
        'AList 密码：<b>%s</b> · 谷歌令牌：<b>%s</b> · 谷歌客户端：<b>%s</b> · WebDAV 密码：<b>%s</b><br>'
        '数据库备份：<b>%s</b>%s</div>'
        '<p><button type="button" id="backup">立即备份数据库</button> '
        '<span class="msg" id="bmsg"></span></p></div>'
        '<div class="panel"><h2>WebDAV（RaiDrive / rclone 挂载）</h2>'
        '<div class="kv">地址：<code>https://debi.su7.dpdns.org/dav/</code>'
        '（IPv4 可用 debi4 同地址）<br>用户名：<code>dav</code> · 密码：<b>%s</b></div>'
        '<p style="color:#8b94a7;font-size:13px;line-height:1.7">'
        '目录按上传月份自动生成（YYYY-MM），文件名就是条目标题；改名、删除只动数据库条目，'
        '源文件按哈希存放在谷歌硬盘，删条目后无引用的源文件保留一周再清理。'
        '跨月份移动请在网页里改（WebDAV 内会拒绝）。</p>'
        '<div style="display:grid;gap:9px;max-width:420px">'
        '<input type="password" id="davpw" placeholder="设置 / 修改 WebDAV 密码（至少 8 位）" autocomplete="new-password">'
        '<span><button type="button" id="davset">保存 WebDAV 密码</button> '
        '<span class="msg" id="davmsg"></span></span></div></div>'
        '<div class="panel"><h2>连接谷歌硬盘</h2>'
        '<p style="color:#8b94a7;font-size:13px;line-height:1.75;margin-top:0">'
        '令牌工具需要用你自己的谷歌 OAuth 客户端（它不再提供可用的默认客户端），一次性设置：'
        '① 在 Google Cloud 控制台新建项目，启用 Google Drive API；'
        '② 在「OAuth 同意屏幕」选外部用户并点「发布应用」（不发布的话令牌 7 天就过期）；'
        '③ 在「凭据」里创建 OAuth 客户端 ID（类型选 Web 应用），已授权的重定向 URI 填 '
        'https://alistgo.com/tool/google/callback，得到客户端 ID 和密钥；'
        '④ 打开 alistgo.com/tool/google/request，填入客户端 ID 和密钥，点获取令牌并登录授权'
        '（出现「尚未验证此应用」时点「高级 → 转至你的项目」，这是你自己的应用，正常），'
        '页面会给出 refresh token；'
        '⑤ 把令牌、客户端 ID、客户端密钥三样一起粘贴到下面（只导入、不回显）。'
        '根目录文件夹 ID 可选（留空 = 整个硬盘根目录，建议填一个专用文件夹的 ID）。</p>'
        '<div style="display:grid;gap:9px">'
        '<input type="password" id="gtoken" placeholder="Google refresh token（留空 = 不修改已保存的）" autocomplete="off">'
        '<input type="text" id="gcid" placeholder="OAuth 客户端 ID（留空 = 不修改已保存的）" autocomplete="off">'
        '<input type="password" id="gcsecret" placeholder="OAuth 客户端密钥（留空 = 不修改已保存的）" autocomplete="off">'
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
        'client_id:document.getElementById("gcid").value,'
        'client_secret:document.getElementById("gcsecret").value,'
        'root_folder_id:document.getElementById("gfolder").value});'
        'm.textContent=d.ok?("完成：" + (d.message||"")):("失败：" + (d.message||""));'
        'if(d.ok)setTimeout(()=>location.reload(),1200);};'
        'document.getElementById("davset").onclick=async()=>{'
        'const d=await jpost("api/admin/dav-password",{'
        'password:document.getElementById("davpw").value});'
        'document.getElementById("davmsg").textContent=d.ok?"已保存":("失败：" + (d.message||""));};'
        'document.getElementById("backup").onclick=async()=>{'
        'const d=await jpost("api/admin/backup",{});'
        'document.getElementById("bmsg").textContent=d.ok?"备份完成":"失败：" + (d.message||"");};'
        'document.querySelectorAll(".retry").forEach(b=>b.onclick=async()=>{'
        'await jpost("api/admin/retry",{hash:b.dataset.h});location.reload();});'
        '</script>'
        % ("已连接" if d["alist_ok"] else "不可达",
           ("（" + _esc(d["alist_error"]) + "）") if d["alist_error"] else "",
           storage_line, counts, d["entries"],
           _fmt_size(d["staging"]), _fmt_size(STAGING_BUDGET),
           _fmt_size(d["disk_free"]),
           "已配置" if d["alist_pass_set"] else "未配置",
           "已导入" if d["google_token_set"] else "未导入",
           "已导入" if d["google_client_set"] else "未导入",
           "已设置" if d["dav_set"] else "未设置",
           last_backup,
           ("<br><span class='err'>上次备份错误：" + _esc(d["last_backup_error"]) + "</span>")
           if d["last_backup_error"] else "",
           "已设置" if d["dav_set"] else "未设置（WebDAV 暂不可用）",
           _esc(_cred("google_root_folder_id")),
           queue_rows))
    return _page("管理", body, base_href=base_href)


# ------------------------------------------------------------ WebDAV
# Virtual tree: /dav/ -> month folders -> entry files.
# Every verb resolves path -> entry -> file(hash) -> AList/staging.

_DAV_METHODS = {"PROPFIND", "GET", "HEAD", "PUT", "DELETE", "MOVE",
                "MKCOL", "LOCK", "UNLOCK", "OPTIONS", "PROPPATCH"}


def _dav_fix_path(raw):
    try:
        return raw.encode("latin-1").decode("utf-8")
    except Exception:
        return raw


def _dav_segments(path):
    parts = [p for p in path.split("/") if p != ""]
    if parts and parts[0] == "dav":
        parts = parts[1:]
    return parts


def _dav_href(environ, segments):
    scheme = environ.get("HTTP_X_FORWARDED_PROTO") or "https"
    host = environ.get("HTTP_HOST") or "debi.su7.dpdns.org"
    path = "/dav/" + "/".join(
        urllib.parse.quote(s, safe="") for s in segments)
    if segments == [] or (segments and _folder_exists(segments[0])
                          and len(segments) == 1):
        path += "/"
    return "%s://%s%s" % (scheme, host, path)


def _folder_exists(folder):
    if not re.fullmatch(r"\d{4}-\d{2}", folder or ""):
        return False
    if folder == _folder_of(time.time()):
        return True  # the current upload month always exists
    with _db() as conn:
        row = conn.execute("SELECT COUNT(*) c FROM entries WHERE folder = ?",
                           (folder,)).fetchone()
    return row["c"] > 0


def _dav_folders():
    with _db() as conn:
        return [r["folder"] for r in conn.execute(
            "SELECT DISTINCT folder FROM entries ORDER BY folder DESC")]


def _dav_entries_in(folder):
    with _db() as conn:
        return conn.execute(
            _ENTRY_COLS + "WHERE e.folder = ? ORDER BY e.title",
            (folder,)).fetchall()


def _dav_find(folder, filename):
    for row in _dav_entries_in(folder):
        if (row["title"] + "." + (row["ext"] or "")) == filename:
            return row
    return None


def _http_date(ts):
    import email.utils
    return email.utils.formatdate(ts or 0, usegmt=True)


def _dav_response_xml(environ, items):
    """items: list of (segments, is_collection, size, mtime, ctype, etag)"""
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<D:multistatus xmlns:D="DAV:">']
    quota = None
    for seg, is_col, size, mtime, ctype, etag in items:
        out.append("<D:response><D:href>%s</D:href>"
                   % _esc(_dav_href(environ, seg)))
        out.append("<D:propstat><D:prop>")
        out.append("<D:displayname>%s</D:displayname>"
                   % _esc(seg[-1] if seg else "dav"))
        if mtime:
            out.append("<D:getlastmodified>%s</D:getlastmodified>"
                       % _http_date(mtime))
            out.append("<D:creationdate>%s</D:creationdate>"
                       % time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(mtime)))
        if is_col:
            out.append("<D:resourcetype><D:collection/></D:resourcetype>")
            if quota is None:
                with _db() as conn:
                    used = conn.execute(
                        "SELECT COALESCE(SUM(size), 0) s FROM files"
                    ).fetchone()["s"]
                quota = (int(used), int(_disk_free()))
            out.append("<D:quota-used-bytes>%d</D:quota-used-bytes>"
                       % quota[0])
            out.append("<D:quota-available-bytes>%d</D:quota-available-bytes>"
                       % quota[1])
        else:
            out.append("<D:resourcetype/>")
            out.append("<D:getcontentlength>%d</D:getcontentlength>"
                       % (size or 0))
            if ctype:
                out.append("<D:getcontenttype>%s</D:getcontenttype>"
                           % _esc(ctype))
            if etag:
                out.append("<D:getetag>\"%s\"</D:getetag>" % _esc(etag))
        out.append("</D:prop><D:status>HTTP/1.1 200 OK</D:status>"
                   "</D:propstat></D:response>")
    out.append("</D:multistatus>")
    return "".join(out).encode("utf-8")


def _dav_send(start_response, status, body=b"", headers=None,
              content_type="text/plain; charset=utf-8"):
    hdrs = [("Content-Type", content_type),
            ("Content-Length", str(len(body)))]
    if headers:
        hdrs += headers
    start_response(status, hdrs)
    return [body]


def _dav_auth_ok(environ):
    creds = _load_creds()
    if not creds.get("dav_pass_hash"):
        return None  # not configured
    header = environ.get("HTTP_AUTHORIZATION") or ""
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
    except Exception:
        return False
    user, _, password = decoded.partition(":")
    if user != "dav":
        return False
    return dav_password_check(password)


class _StreamBody:
    def __init__(self, resp, conn=None):
        self.resp = resp
        self.conn = conn

    def __iter__(self):
        return self

    def __next__(self):
        block = self.resp.read(65536)
        if not block:
            raise StopIteration
        return block

    def close(self):
        try:
            self.resp.close()
        except Exception:
            pass
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass


def _dav_stream_archived(environ, start_response, file_row):
    """Proxy a GET of an archived file: fetch the signed URL
    (loopback or Google direct), follow redirects keeping the
    Range header, retry once with a fresh link on 401."""
    range_hdr = environ.get("HTTP_RANGE") or ""
    last_error = None
    for _attempt in range(2):
        try:
            url = resolve_loopback_url(file_row)
        except Exception as exc:
            last_error = str(exc)[:150]
            break
        current = url
        try:
            for _hop in range(6):
                parsed = urllib.parse.urlparse(current)
                if parsed.scheme == "https":
                    conn = http.client.HTTPSConnection(
                        parsed.hostname, parsed.port or 443, timeout=120)
                else:
                    conn = http.client.HTTPConnection(
                        parsed.hostname, parsed.port or 80, timeout=120)
                target = parsed.path or "/"
                if parsed.query:
                    target += "?" + parsed.query
                headers = {}
                if range_hdr:
                    headers["Range"] = range_hdr
                conn.request("GET", target, headers=headers)
                resp = conn.getresponse()
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.getheader("Location")
                    resp.read()
                    conn.close()
                    if not location:
                        break
                    current = urllib.parse.urljoin(current, location)
                    continue
                break
            else:
                raise AlistError("重定向次数过多")
        except Exception as exc:
            last_error = str(exc)[:150]
            continue
        if resp.status == 401 and _attempt == 0:
            try:
                resp.read()
            except Exception:
                pass
            conn.close()
            try:
                ALIST.login(force=True)
            except Exception:
                pass
            continue
        if resp.status not in (200, 206):
            detail = ""
            try:
                detail = resp.read(200).decode("utf-8", "replace")
            except Exception:
                pass
            conn.close()
            return _dav_send(start_response, "502 Bad Gateway",
                             ("上游取流失败 %s %s"
                              % (resp.status, detail)).encode("utf-8"))
        headers = []
        for name in ("Content-Type", "Content-Length", "Content-Range",
                     "Accept-Ranges", "ETag", "Last-Modified"):
            value = resp.getheader(name)
            if value:
                headers.append((name, value))
        if not any(k == "Accept-Ranges" for k, _ in headers):
            headers.append(("Accept-Ranges", "bytes"))
        status_line = ("206 Partial Content" if resp.status == 206
                       else "200 OK")
        start_response(status_line, headers)
        if environ.get("REQUEST_METHOD") == "HEAD":
            resp.close()
            conn.close()
            return [b""]
        return _StreamBody(resp, conn)
    return _dav_send(start_response, "502 Bad Gateway",
                     ("取流失败：%s" % (last_error or "未知")).encode("utf-8"))


def dav_handle(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = _dav_fix_path(environ.get("PATH_INFO", "/dav") or "/dav")
    segments = _dav_segments(path)

    configured = _dav_auth_ok(environ)
    if configured is None:
        return _dav_send(
            start_response, "403 Forbidden",
            "WebDAV 密码尚未设置，请先在视频库管理页设置。".encode("utf-8"))
    if not configured:
        return _dav_send(
            start_response, "401 Unauthorized", b"unauthorized",
            headers=[("WWW-Authenticate", 'Basic realm="vidlib-dav"')])

    raw_segments = list(segments)

    # A bare filename at the root names a file in the current
    # month (the same place a root-level PUT puts it).
    if (len(segments) == 1
            and method in ("PROPFIND", "GET", "HEAD", "DELETE", "MOVE")
            and not re.fullmatch(r"\d{4}-\d{2}", segments[0])):
        segments = [_folder_of(time.time()), segments[0]]

    # Folder-drop chute: when a client pushes a local folder tree
    # in, the first segment is a folder name, not a month. The
    # tree is flattened into the target month and the folder path
    # is kept as the entries' tag, so the grouping survives in
    # the web UI even though the DAV tree itself stays month-only.
    chute = ""
    if segments:
        first_is_month = bool(re.fullmatch(r"\d{4}-\d{2}", segments[0]))
        if method == "PUT":
            if len(segments) >= 2 and not first_is_month:
                chute = "/".join(segments[:-1])
                segments = [_folder_of(time.time()), segments[-1]]
            elif len(segments) >= 3 and first_is_month:
                chute = "/".join(segments[1:-1])
                segments = [segments[0], segments[-1]]
        elif method in ("GET", "HEAD", "DELETE", "MOVE"):
            if len(segments) >= 2 and not first_is_month:
                segments = [_folder_of(time.time()), segments[-1]]
            elif len(segments) >= 3 and first_is_month:
                segments = [segments[0], segments[-1]]

    if method == "OPTIONS":
        return _dav_send(
            start_response, "200 OK", b"",
            headers=[("DAV", "1, 2"),
                     ("Allow", ", ".join(sorted(_DAV_METHODS))),
                     ("MS-Author-Via", "DAV")])

    if method == "PROPFIND":
        depth = environ.get("HTTP_DEPTH", "1")
        if depth not in ("0", "1"):
            return _dav_send(start_response, "403 Forbidden",
                             "只支持 Depth 0 / 1。".encode("utf-8"))
        items = []
        if len(segments) == 0:
            items.append(([], True, 0, time.time(), "", ""))
            if depth == "1":
                folders = _dav_folders()
                current = _folder_of(time.time())
                if current not in folders:
                    folders.insert(0, current)
                for folder in folders:
                    items.append(([folder], True, 0, 0, "", ""))
        elif len(segments) == 1:
            folder = segments[0]
            if not _folder_exists(folder):
                return _dav_send(start_response, "404 Not Found",
                                 b"not found")
            items.append(([folder], True, 0, time.time(), "", ""))
            if depth == "1":
                for row in _dav_entries_in(folder):
                    name = row["title"] + "." + (row["ext"] or "")
                    items.append(([folder, name], False, row["size"],
                                  row["created_at"], _mime_for(row["ext"]),
                                  row["hash"]))
        elif len(segments) >= 2:
            # File paths may be nested (folder-drop trees): the
            # file itself lives in a month folder, so resolve by
            # month + basename. Folder paths are virtual chute
            # collections whose members are the entries tagged
            # with the folder path.
            first_is_month = bool(
                re.fullmatch(r"\d{4}-\d{2}", segments[0]))
            month = (segments[0] if first_is_month
                     else _folder_of(time.time()))
            row = _dav_find(month, segments[-1])
            if row is not None:
                items.append((raw_segments, False, row["size"],
                              row["created_at"],
                              _mime_for(row["ext"]),
                              row["hash"]))
            elif depth == "1" or path.endswith("/"):
                # A collection-shaped request (trailing slash, or
                # a listing): answer as the virtual chute folder.
                # A file-shaped Depth:0 check for a file that does
                # not exist keeps its plain 404 below - upload
                # clients depend on it before sending a PUT.
                if first_is_month:
                    tag = "/".join(segments[1:])
                else:
                    tag = "/".join(segments)
                items.append((raw_segments, True, 0, time.time(), "", ""))
                if depth == "1":
                    for entry in _dav_entries_in(month):
                        if (entry["tags"] or "") == tag:
                            name = entry["title"] + "." + (entry["ext"] or "")
                            items.append((raw_segments + [name], False,
                                          entry["size"], entry["created_at"],
                                          _mime_for(entry["ext"]),
                                          entry["hash"]))
            else:
                return _dav_send(start_response, "404 Not Found",
                                 b"not found")
        body = _dav_response_xml(environ, items)
        return _dav_send(start_response, "207 Multi-Status", body,
                         content_type="application/xml; charset=utf-8")

    if method in ("GET", "HEAD"):
        if len(segments) != 2:
            return _dav_send(start_response, "404 Not Found", b"not found")
        row = _dav_find(segments[0], segments[1])
        if row is None:
            return _dav_send(start_response, "404 Not Found", b"not found")
        file_row = get_file(row["hash"])
        if file_row is None:
            return _dav_send(start_response, "404 Not Found", b"not found")
        if file_row["status"] == "archived":
            return _dav_stream_archived(environ, start_response, file_row)
        local = _local_path(file_row)
        if not os.path.exists(local):
            return _dav_send(start_response, "404 Not Found", b"not found")
        return _serve_file_range(environ, start_response, local,
                                 _mime_for(file_row["ext"]))

    if method == "PUT":
        if len(segments) == 1:
            # Dropped on the drive root: files belong to the month
            # they arrive in, so the root means "this month".
            segments = [_folder_of(time.time()), segments[0]]
        if len(segments) != 2:
            return _dav_send(start_response, "409 Conflict",
                             "路径必须是 /dav/YYYY-MM/文件名。".encode("utf-8"))
        folder, filename = segments
        if not re.fullmatch(r"\d{4}-\d{2}", folder):
            return _dav_send(start_response, "409 Conflict",
                             "文件夹必须是 YYYY-MM 格式。".encode("utf-8"))
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except Exception:
            length = 0
        if length > 0:
            try:
                _check_capacity(length)
            except ValueError as exc:
                return _dav_send(start_response,
                                 "507 Insufficient Storage",
                                 str(exc).encode("utf-8"))
        tmp_path = os.path.join(
            STAGING_DIR,
            "dav-" + hashlib.sha256(
                (folder + "/" + filename).encode()).hexdigest()[:16]
            + ".part")
        stream = environ["wsgi.input"]
        remaining = length
        try:
            with open(tmp_path, "wb") as fh:
                while True:
                    if length > 0:
                        if remaining <= 0:
                            break
                        block = stream.read(min(262144, remaining))
                    else:
                        block = stream.read(262144)
                    if not block:
                        break
                    fh.write(block)
                    remaining -= len(block)
        except Exception as exc:
            return _dav_send(start_response, "500 Internal Server Error",
                             str(exc)[:150].encode("utf-8"))
        import sys
        print("DAVPUT %s clen=%r te=%r expect=%r got=%d"
              % (path, environ.get("CONTENT_LENGTH"),
                 environ.get("HTTP_TRANSFER_ENCODING"),
                 environ.get("HTTP_EXPECT"),
                 os.path.getsize(tmp_path)),
              file=sys.stderr, flush=True)
        if length > 0 and os.path.getsize(tmp_path) != length:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return _dav_send(start_response, "400 Bad Request",
                             "接收不完整。".encode("utf-8"))
        if os.path.getsize(tmp_path) == 0:
            # Clients (Explorer/RaiDrive) often PUT a 0-byte
            # placeholder before the real content. Acknowledge it
            # without registering anything - and never let it
            # replace a real entry of the same name.
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return _dav_send(start_response, "201 Created", b"")
        existing = _dav_find(folder, filename)
        replaced = existing is not None
        if replaced:
            # Free the title first so the new entry takes the clean
            # name; the replaced file itself is orphan-reaped later.
            delete_entry(existing["id"])
        try:
            entry_id, digest, _dup = ingest_staged_file(
                tmp_path, filename, folder=folder, tags=chute)
        except Exception as exc:
            return _dav_send(start_response, "500 Internal Server Error",
                             str(exc)[:200].encode("utf-8"))
        if replaced:
            return _dav_send(start_response, "204 No Content", b"",
                             headers=[("ETag", '"%s"' % digest)])
        return _dav_send(start_response, "201 Created", b"",
                         headers=[("ETag", '"%s"' % digest)])

    if method == "DELETE":
        if len(segments) != 2:
            return _dav_send(start_response, "404 Not Found", b"not found")
        row = _dav_find(segments[0], segments[1])
        if row is None:
            return _dav_send(start_response, "404 Not Found", b"not found")
        delete_entry(row["id"])
        return _dav_send(start_response, "204 No Content", b"")

    if method == "MOVE":
        if len(segments) != 2:
            return _dav_send(start_response, "404 Not Found", b"not found")
        row = _dav_find(segments[0], segments[1])
        if row is None:
            return _dav_send(start_response, "404 Not Found", b"not found")
        dest = environ.get("HTTP_DESTINATION") or ""
        parsed = urllib.parse.urlparse(dest)
        dest_path = _dav_fix_path(urllib.parse.unquote(parsed.path))
        dest_segments = _dav_segments(dest_path)
        if dest_segments and not re.fullmatch(r"\d{4}-\d{2}",
                                              dest_segments[0]):
            dest_segments = [_folder_of(time.time()), dest_segments[-1]]
        elif len(dest_segments) >= 3:
            dest_segments = [dest_segments[0], dest_segments[-1]]
        if len(dest_segments) != 2:
            return _dav_send(start_response, "400 Bad Request",
                             "Destination 无效。".encode("utf-8"))
        if dest_segments[0] != segments[0]:
            return _dav_send(
                start_response, "403 Forbidden",
                "不支持跨文件夹移动；改月份请在网页里操作。".encode("utf-8"))
        new_title = os.path.splitext(dest_segments[1])[0]
        if not new_title:
            return _dav_send(start_response, "400 Bad Request",
                             "新文件名无效。".encode("utf-8"))
        overwrite = (environ.get("HTTP_OVERWRITE") or "T").upper() != "F"
        target = _dav_find(dest_segments[0], dest_segments[1])
        if target is not None and target["id"] != row["id"]:
            if not overwrite:
                return _dav_send(start_response,
                                 "412 Precondition Failed",
                                 "目标已存在。".encode("utf-8"))
            delete_entry(target["id"])
            replaced = True
        else:
            replaced = False
        with _db() as conn:
            conn.execute("UPDATE entries SET title = ?, updated_at = ? "
                         "WHERE id = ?",
                         (new_title[:200], time.time(), row["id"]))
        return _dav_send(start_response,
                         "204 No Content" if replaced else "201 Created",
                         b"")

    if method == "PROPPATCH":
        # Explorer/RaiDrive set the source file's timestamps and
        # attributes after every copy. The store keeps its own
        # timestamps, so the update is acknowledged as a no-op -
        # answering 405 here makes clients retry and probe, which
        # is pure per-file overhead on a high-latency path.
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:multistatus xmlns:D="DAV:"><D:response>'
            '<D:href>%s</D:href><D:propstat><D:prop/>'
            '<D:status>HTTP/1.1 200 OK</D:status></D:propstat>'
            '</D:response></D:multistatus>'
            % _esc(_dav_href(environ, segments))).encode("utf-8")
        return _dav_send(start_response, "207 Multi-Status", body,
                         content_type="application/xml; charset=utf-8")

    if method == "MKCOL":
        if len(segments) == 0:
            return _dav_send(
                start_response, "405 Method Not Allowed",
                "不能对根目录 MKCOL。".encode("utf-8"),
                headers=[("Allow", ", ".join(sorted(_DAV_METHODS)))])
        # Virtual: folder names never become DAV folders (the tree
        # is months only), but accepting the creation lets clients
        # move whole folder trees in - the files land in the target
        # month with the folder path kept as a tag (see the chute
        # normalization above).
        return _dav_send(start_response, "201 Created", b"")

    if method == "LOCK":
        token = "opaquelocktoken:" + secrets.token_hex(16)
        with _dav_locks_lock:
            _dav_locks[token] = (path, time.time() + 2 * 3600)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
            '<D:locktype><D:write/></D:locktype>'
            '<D:lockscope><D:exclusive/></D:lockscope>'
            '<D:depth>0</D:depth>'
            '<D:timeout>Second-7200</D:timeout>'
            '<D:locktoken><D:href>%s</D:href></D:locktoken>'
            '<D:lockroot><D:href>%s</D:href></D:lockroot>'
            '</D:activelock></D:lockdiscovery></D:prop>'
            % (token, _esc(_dav_href(environ, segments)))).encode("utf-8")
        return _dav_send(start_response, "200 OK", body,
                         content_type="application/xml; charset=utf-8")

    if method == "UNLOCK":
        token_hdr = environ.get("HTTP_LOCK_TOKEN") or ""
        token = token_hdr.strip().strip("<>")
        with _dav_locks_lock:
            known = _dav_locks.pop(token, None)
        if known is None:
            return _dav_send(start_response, "400 Bad Request",
                             "未知锁。".encode("utf-8"))
        return _dav_send(start_response, "204 No Content", b"")

    return _dav_send(start_response, "405 Method Not Allowed",
                     b"method not supported",
                     headers=[("Allow", ", ".join(sorted(_DAV_METHODS)))])


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
    if environ.get("REQUEST_METHOD") == "HEAD":
        return [b""]
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

    # WebDAV lives outside the /web proxy (Caddy routes /dav here
    # directly); it carries its own Basic credential.
    if path == "/dav" or path.startswith("/dav/"):
        return dav_handle(environ, start_response)

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
        entry = get_entry(path[len("/watch/"):])
        if entry is None:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        body = page_watch(entry, base_href)
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
        file_row = get_file(path[len("/file/"):])
        if file_row is None or file_row["status"] == "archived":
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        local = _local_path(file_row)
        if not os.path.exists(local):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        return _serve_file_range(environ, start_response, local,
                                 _mime_for(file_row["ext"]))

    if path.startswith("/api/play/") and method == "GET":
        entry = get_entry(path[len("/api/play/"):])
        if entry is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "视频不存在。"})
        with _db() as conn:
            conn.execute("UPDATE entries SET play_count = play_count + 1 "
                         "WHERE id = ?", (entry["id"],))
        file_row = get_file(entry["hash"])
        if entry["f_status"] == "archived":
            try:
                url = resolve_play_url(file_row)
            except Exception as exc:
                return _json(start_response, "502 Bad Gateway",
                             {"ok": False, "message": str(exc)[:200]})
            return _json(start_response, "200 OK",
                         {"ok": True, "url": url, "archived": True,
                          "pos": entry["play_pos"] or 0})
        return _json(start_response, "200 OK",
                     {"ok": True, "url": "file/" + entry["hash"],
                      "archived": False, "pos": entry["play_pos"] or 0})

    if path == "/api/progress" and method == "POST":
        data = _read_json(environ)
        entry = get_entry(data.get("id"))
        if entry is not None:
            with _db() as conn:
                conn.execute("UPDATE entries SET play_pos = ? WHERE id = ?",
                             (float(data.get("pos") or 0), entry["id"]))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/meta" and method == "POST":
        data = _read_json(environ)
        entry = get_entry(data.get("id"))
        if entry is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "视频不存在。"})
        with _db() as conn:
            title = (data.get("title") or entry["title"])[:200]
            title = _unique_title(conn, entry["folder"], title,
                                  exclude_id=entry["id"])
            conn.execute(
                "UPDATE entries SET title = ?, tags = ?, source = ?, "
                "updated_at = ? WHERE id = ?",
                (title, (data.get("tags") or "")[:200],
                 (data.get("source") or "")[:120],
                 time.time(), entry["id"]))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/entries/delete" and method == "POST":
        data = _read_json(environ)
        entry = get_entry(data.get("id"))
        if entry is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "视频不存在。"})
        delete_entry(entry["id"])
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
        return _json(start_response, "200 OK", {"ok": True, **result})

    if path == "/api/batch-export" and method == "POST":
        data = _read_json(environ)
        ids = data.get("ids") or []
        lines = ["# 视频库批量下载列表（aria2 -i 使用；链接为限时签名链接，过期请重新导出）"]
        for eid in ids[:100]:
            entry = get_entry(eid)
            if entry is None:
                continue
            safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_",
                          entry["title"] or entry["hash"])
            if entry["f_status"] != "archived":
                lines.append("# 跳过（未归档）：%s" % entry["title"])
                continue
            try:
                url = resolve_play_url(get_file(entry["hash"]))
            except Exception as exc:
                lines.append("# 取链接失败：%s（%s）"
                             % (entry["title"], str(exc)[:80]))
                continue
            lines.append(url)
            lines.append("  out=%s.%s" % (safe, entry["f_ext"] or "mp4"))
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
        client_id = (data.get("client_id") or "").strip()
        if client_id:
            creds["google_client_id"] = client_id
        client_secret = (data.get("client_secret") or "").strip()
        if client_secret:
            creds["google_client_secret"] = client_secret
        creds["google_root_folder_id"] = (
            data.get("root_folder_id") or "").strip()
        _save_creds(creds)
        token = creds.get("google_refresh_token") or ""
        if not token:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "还没有导入 refresh token。"})
        try:
            action = ALIST.ensure_drive_storage(
                token, creds.get("google_root_folder_id") or "",
                creds.get("google_client_id") or "",
                creds.get("google_client_secret") or "")
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

    if path == "/api/admin/dav-password" and method == "POST":
        data = _read_json(environ)
        password = data.get("password") or ""
        if len(password) < 8:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "密码至少 8 位。"})
        dav_password_set(password)
        return _json(start_response, "200 OK", {"ok": True})

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
        file_row = get_file(data.get("hash") or "")
        if file_row is not None:
            with _db() as conn:
                conn.execute(
                    "UPDATE files SET status = 'local', retries = 0, "
                    "error = '' WHERE hash = ?", (file_row["hash"],))
            _ensure_worker()
        return _json(start_response, "200 OK", {"ok": True})

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]


app = application
