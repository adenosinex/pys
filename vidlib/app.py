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
import queue
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
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
CACHE_DIR = os.path.join(RUNTIME_DIR, "cache")
CRED_PATH = os.path.join(DATA_DIR, "credentials.json")
for _d in (DATA_DIR, THUMB_DIR, BACKUP_DIR, STAGING_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

PUBLIC_VIDEO_BASE = os.environ.get("VIDLIB_PUBLIC_BASE",
                                   "https://debi.su7.dpdns.org")
CHUNK_BYTES = 8 * 1024 * 1024
STAGING_BUDGET = 4 * 1024 * 1024 * 1024
SHORT_MAX_BYTES = 32 * 1024 * 1024
FULL_HASH_MAX = 32 * 1024 * 1024
CACHE_BUDGET_BYTES = 1 * 1024 * 1024 * 1024
DISK_MIN_FREE = 6 * 1024 * 1024 * 1024
ORPHAN_GRACE = 7 * 24 * 3600
VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "m4v", "avi", "ts", "flv",
              "wmv", "mpg", "mpeg", "3gp", "rmvb"}
PLAYABLE_EXTS = {"mp4", "webm", "m4v", "mov"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "avif", "svg",
              "heic", "heif", "tif", "tiff"}
AUDIO_EXTS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav"}


def _kind(ext):
    ext = (ext or "").lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "music"
    if ext == "txt":
        return "novel"
    return "file"


_MIME_OVERRIDES = {"mkv": "video/x-matroska", "ts": "video/mp2t",
                   "flv": "video/x-flv", "wmv": "video/x-ms-wmv",
                   "rmvb": "application/vnd.rn-realmedia-vbr",
                   "m4v": "video/mp4", "3gp": "video/3gpp",
                   "mp3": "audio/mpeg", "flac": "audio/flac",
                   "m4a": "audio/mp4", "aac": "audio/aac",
                   "ogg": "audio/ogg", "opus": "audio/opus",
                   "wav": "audio/x-wav"}


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
        _ecols = {r[1] for r in conn.execute(
            "PRAGMA table_info(entries)")}
        if "seen_at" not in _ecols:
            conn.execute(
                "ALTER TABLE entries ADD COLUMN seen_at "
                "REAL DEFAULT 0")
            conn.execute(
                "UPDATE entries SET seen_at = updated_at "
                "WHERE play_count > 0")
        if "mark" not in _ecols:
            conn.execute(
                "ALTER TABLE entries ADD COLUMN mark "
                "INTEGER NOT NULL DEFAULT 0")
        if "mark_at" not in _ecols:
            conn.execute(
                "ALTER TABLE entries ADD COLUMN mark_at "
                "REAL DEFAULT 0")
        # ---- v2: resource library. Kinds on files, libraries
        # on entries, novel index/read state, file category
        # rules. Everything below is idempotent.
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS libraries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            prefix TEXT DEFAULT '',
            is_default INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS novel_meta (
            hash TEXT PRIMARY KEY,
            encoding TEXT DEFAULT '',
            char_count INTEGER DEFAULT 0,
            chapters TEXT DEFAULT '[]',
            updated_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS novel_read (
            entry_id INTEGER PRIMARY KEY,
            chapter INTEGER DEFAULT 0,
            offset INTEGER DEFAULT 0,
            percent REAL DEFAULT 0,
            updated_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS file_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            exts TEXT DEFAULT '',
            sort INTEGER DEFAULT 0,
            builtin INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS novel_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            status TEXT DEFAULT 'unrated',
            note TEXT DEFAULT '',
            dislike_reason TEXT DEFAULT '',
            like_tags TEXT DEFAULT '',
            like_text TEXT DEFAULT '',
            link TEXT DEFAULT '',
            created_at REAL DEFAULT 0,
            updated_at REAL DEFAULT 0
        );
        """)
        _ncols = {r[1] for r in conn.execute(
            "PRAGMA table_info(novel_meta)")}
        if "title_len" not in _ncols:
            conn.execute(
                "ALTER TABLE novel_meta ADD COLUMN title_len "
                "INTEGER DEFAULT 0")
        if "split_stats" not in _ncols:
            conn.execute(
                "ALTER TABLE novel_meta ADD COLUMN split_stats "
                "TEXT DEFAULT ''")
        _fcols = {r[1] for r in conn.execute(
            "PRAGMA table_info(files)")}
        if "kind" not in _fcols:
            conn.execute(
                "ALTER TABLE files ADD COLUMN kind "
                "TEXT DEFAULT 'video'")
        if "category" not in _fcols:
            conn.execute(
                "ALTER TABLE files ADD COLUMN category "
                "TEXT DEFAULT ''")
        if "artist" not in _fcols:
            conn.execute(
                "ALTER TABLE files ADD COLUMN artist "
                "TEXT DEFAULT ''")
        if "album" not in _fcols:
            conn.execute(
                "ALTER TABLE files ADD COLUMN album "
                "TEXT DEFAULT ''")
        if "bit_rate" not in _fcols:
            conn.execute(
                "ALTER TABLE files ADD COLUMN bit_rate "
                "INTEGER DEFAULT 0")
        if "library_id" not in _ecols:
            conn.execute(
                "ALTER TABLE entries ADD COLUMN library_id "
                "INTEGER DEFAULT 0")
        for _k, _n, _p in (("video", "默认库", "视频/默认库"),
                           ("image", "默认库", "图片"),
                           ("novel", "默认库", "小说"),
                           ("file", "默认库", "文件"),
                           ("music", "默认库", "音乐")):
            if conn.execute(
                    "SELECT id FROM libraries WHERE kind = ? "
                    "AND is_default = 1", (_k,)).fetchone() is None:
                conn.execute(
                    "INSERT INTO libraries(name, kind, prefix, "
                    "is_default, created_at) VALUES(?,?,?,1,?)",
                    (_n, _k, _p, time.time()))
        if conn.execute(
                "SELECT value FROM kv WHERE key = 'schema_v2'"
        ).fetchone() is None:
            for _r in conn.execute(
                    "SELECT hash, ext FROM files").fetchall():
                conn.execute(
                    "UPDATE files SET kind = ? WHERE hash = ?",
                    (_kind(_r["ext"]), _r["hash"]))
            _defs = {r["kind"]: r["id"] for r in conn.execute(
                "SELECT id, kind FROM libraries "
                "WHERE is_default = 1")}
            for _r in conn.execute(
                    "SELECT e.id AS id, f.kind AS kind FROM entries e "
                    "JOIN files f ON f.hash = e.hash "
                    "WHERE e.library_id = 0").fetchall():
                conn.execute(
                    "UPDATE entries SET library_id = ? WHERE id = ?",
                    (_defs.get(_r["kind"], 0), _r["id"]))
            conn.execute(
                "INSERT OR REPLACE INTO kv(key, value) "
                "VALUES('schema_v2', '1')")
        if conn.execute(
                "SELECT COUNT(*) c FROM file_categories"
        ).fetchone()["c"] == 0:
            for _i, (_n, _e, _b) in enumerate((
                    ("安装包", "apk exe msi dmg ipa pkg deb rpm", 0),
                    ("压缩包", "zip 7z rar tar gz bz2 xz", 0),
                    ("镜像", "iso img bin cue", 0),
                    ("文档", "pdf doc docx xls xlsx ppt pptx odt "
                             "rtf csv md", 0),
                    ("其他", "", 1))):
                conn.execute(
                    "INSERT INTO file_categories(name, exts, sort, "
                    "builtin) VALUES(?,?,?,?)", (_n, _e, _i, _b))


init_db()


def kv_get(key, default=""):
    with _db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?",
                           (key,)).fetchone()
    return row["value"] if row else default


def _libraries(kind=None):
    with _db() as conn:
        if kind:
            return conn.execute(
                "SELECT * FROM libraries WHERE kind = ? "
                "ORDER BY is_default DESC, id", (kind,)).fetchall()
        return conn.execute(
            "SELECT * FROM libraries "
            "ORDER BY kind, is_default DESC, id").fetchall()


def _default_library(kind):
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM libraries WHERE kind = ? "
            "AND is_default = 1 ORDER BY id LIMIT 1",
            (kind,)).fetchone()


def _library_by_id(library_id):
    try:
        library_id = int(library_id)
    except Exception:
        return None
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM libraries WHERE id = ?",
            (library_id,)).fetchone()


def _resolve_library_id(kind, library_id):
    """The library an entry belongs to: the requested one when
    it exists and matches the kind, else the kind's default."""
    lib = _library_by_id(library_id) if library_id else None
    if lib is not None and lib["kind"] == kind:
        return lib["id"]
    d = _default_library(kind)
    return d["id"] if d else 0


def _file_categories():
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM file_categories ORDER BY sort, id"
        ).fetchall()


def _norm_exts(text):
    seen = []
    for e in re.split(r"[\s,，;；]+", text or ""):
        e = e.strip().lower().lstrip(".")
        if e and e not in seen:
            seen.append(e)
    return " ".join(seen)


def _novel_chapter_spans(meta):
    """Stored chapters are {t, o}; expand to {t, o, e} spans using
    the next chapter's offset (or the total char count)."""
    raw = (meta or {}).get("chapters") or []
    total = (meta or {}).get("char_count") or 0
    spans = []
    for i, c in enumerate(raw):
        o = c.get("o", 0)
        e = raw[i + 1].get("o", total) if i + 1 < len(raw) else total
        spans.append({"t": c.get("t", ""), "o": o, "e": e})
    return spans


def _category_for_ext(ext):
    """Match an extension against the editable category rules;
    the rule with an empty extension list is the fallback, so
    an unknown future format can never fail ingest."""
    ext = (ext or "").lower()
    fallback = "其他"
    for r in _file_categories():
        exts = {e.strip().lower() for e in re.split(
            r"[\s,，;；]+", r["exts"] or "") if e.strip()}
        if not exts:
            fallback = r["name"]
        elif ext in exts:
            return r["name"]
    return fallback


def _entry_library(hash_):
    with _db() as conn:
        row = conn.execute(
            "SELECT library_id FROM entries WHERE hash = ? "
            "ORDER BY id LIMIT 1", (hash_,)).fetchone()
    if row and row["library_id"]:
        return _library_by_id(row["library_id"])
    return None


def _target_gpath(row):
    """The single place physical Drive paths are composed:
    category -> library/category -> month -> hash name, so a
    future storage switch can move one category, one library
    or one month as a unit."""
    month = _folder_of(row["created_at"] or time.time())
    name = row["hash"] + "." + (row["ext"] or "bin")
    kind = row["kind"] or "video"
    if kind == "video":
        lib = _entry_library(row["hash"])
        prefix = (lib["prefix"] if lib and lib["prefix"]
                  else "视频/默认库")
        return "/" + prefix.strip("/") + "/" + month + "/" + name
    if kind == "image":
        return "/图片/" + month + "/" + name
    if kind == "novel":
        return "/小说/" + month + "/" + name
    if kind == "music":
        return "/音乐/" + month + "/" + name
    return ("/文件/" + (row["category"] or "其他") + "/"
            + month + "/" + name)


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

    def fs_move(self, src_dir, dst_dir, names):
        return self.api("POST", "/api/fs/move",
                        {"src_dir": src_dir, "dst_dir": dst_dir,
                         "names": names})

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


_SAMPLE = 1024 * 1024


def _file_digest(path):
    """Dedupe key for a staged file. Up to FULL_HASH_MAX the
    file is hashed in full (exact dedupe). Above that, a
    sampled digest - the size plus the first, middle and last
    1 MiB - keeps ingest CPU flat (~0.03 s) instead of ~7 s
    per GiB on this single-core box. A false duplicate would
    need the same size AND identical content at all three
    sample points."""
    size = os.path.getsize(path)
    if size <= FULL_HASH_MAX:
        return _sha256_file(path)
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        for off in (0, (size - _SAMPLE) // 2, size - _SAMPLE):
            fh.seek(off)
            h.update(fh.read(_SAMPLE))
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


def _make_image_thumb(path, hash_):
    out_path = os.path.join(THUMB_DIR, hash_ + ".jpg")
    if os.path.exists(out_path):
        return True
    try:
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-frames:v", "1",
             "-vf", "scale=480:-2", "-q:v", "5", out_path, "-y"],
            capture_output=True, timeout=60)
        return res.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def _probe_audio(path):
    """ffprobe for audio files: duration, bit rate and the
    common format tags (title / artist / album)."""
    result = {"duration": 0.0, "bit_rate": 0, "title": "",
              "artist": "", "album": ""}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, timeout=60)
        info = json.loads(out.stdout.decode("utf-8", "replace")
                          or "{}")
    except Exception:
        return result
    fmt = info.get("format") or {}
    try:
        result["duration"] = float(fmt.get("duration") or 0)
    except Exception:
        pass
    try:
        result["bit_rate"] = int(fmt.get("bit_rate") or 0)
    except Exception:
        pass
    tags = {}
    for k, v in (fmt.get("tags") or {}).items():
        tags[str(k).lower()] = v
    result["title"] = str(tags.get("title") or "").strip()
    result["artist"] = str(tags.get("artist") or "").strip()
    result["album"] = str(tags.get("album") or "").strip()
    return result


def _make_cover_thumb(path, hash_):
    """Extract the embedded cover image of an audio file, if
    it has one; pure audio without art yields no thumbnail
    (the frontend placeholder takes over)."""
    out_path = os.path.join(THUMB_DIR, hash_ + ".jpg")
    if os.path.exists(out_path):
        return True
    try:
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-frames:v", "1",
             "-vf", "scale=320:-2", "-q:v", "5", out_path, "-y"],
            capture_output=True, timeout=60)
        ok = (res.returncode == 0 and os.path.exists(out_path)
              and os.path.getsize(out_path) > 0)
        if not ok and os.path.exists(out_path):
            os.unlink(out_path)
        return ok
    except Exception:
        return False


# ------------------------------------------------------------- novels

NOVEL_TEXT_DIR = os.path.join(RUNTIME_DIR, "novel_text")
os.makedirs(NOVEL_TEXT_DIR, exist_ok=True)

_MONTH_RE = re.compile(r"\d{4}-\d{2}")


# ------------------------------------------------- AI title config
_AI_DEFAULT_MODEL = "google/gemini-2.5-flash"


def _ai_title_config():
    """(key, model, enabled) for AI title delimitation. The
    key is import-only in credentials.json (openrouter_key);
    the model name and the on/off switch are plain kv
    settings."""
    return (_cred("openrouter_key"),
            kv_get("ai_title_model") or _AI_DEFAULT_MODEL,
            kv_get("ai_title_enabled") == "1")


def _ai_cfg_if_active():
    key, model, enabled = _ai_title_config()
    if key and enabled:
        return {"key": key, "model": model,
                "batch": _ai_batch_size()}
    return None


# ------------------------------------------------------ chapter split
# Smart chapter splitting. Matching runs on a normalized detection
# copy (full-width chars folded to half-width, strictly one char to
# one char, so offsets stay valid for the original text); titles
# and body are always sliced from the original text. Anchors are
# separator runs / symbol-only lines and chapter heads (the Nth
# chapter pattern or a special name); embedded titles are
# delimited by structural breaks, a per-book inferred title
# length, or a fixed char count after the head. All title
# lengths are PURE char counts (see _is_pure_char): letters and
# numbers count, punctuation / symbols / spaces never do, so a
# count-based delimitation skips decoration inside the span and
# extends into the following text until the count is met.
# Stored titles are purified to pure chars only. As a last
# resort an AI model can judge the lengths of titles no rule
# can delimit (see _ai_delimit_batch).

_CH_NUM = "0-9零〇一二三四五六七八九十百千万两"
_HEAD_RE = re.compile(r"第([" + _CH_NUM + r"]+)[章节回卷]")
_LINE_TITLE_RE = re.compile(
    r"^\s*(第[" + _CH_NUM + r"]+[章节回卷]"
    r"|序章|楔子|引子|后记|尾声|番外[^\s]{0,6})")
_SEP_RUN_RE = re.compile(r"(?:[*※◇◆][ \t]*){3,}")
_SYMBOL_LINE_RE = re.compile(r"^[*※◇◆\-—–=~_#·•… ]+$")
_SPECIAL_RE = re.compile(r"(序章|楔子|引子|后记|尾声|番外)")
_TITLE_WINDOW = 60
_TITLE_MAX_AFTER = 20
_TITLE_FALLBACK = 20
_TITLE_DEFAULT_N = 4
_SEP_GAP_MAX = 40
_MERGE_MIN = 200
_MECH_MIN_CHARS = 50000
_MECH_SPAN = 9000

# Punctuation / symbols / spaces that may sit between a chapter
# head and its title ( \u3001\uff0c\uff0e\uff1a\uff1b\u300c\u300d\u300e\u300f\u30fb\u2026 ):
# decoration, not title chars. They never count toward a
# title length (pure-char semantics), and purification drops
# them from the stored title altogether.
_LEAD_PUNCT = "\u3001\uff0c\uff0e\uff1a\uff1b\u300c\u300d\u300e\u300f\u30fb\u2026 ,.:;\t"
_AI_WINDOW = 24
# Head recovery for bare anchors: a separator chapter whose
# own head the strict head pattern rejected (a corrupted
# numeral char, say) may still carry its head in the first
# chars of its fragment, right behind the decoration field.
# The scan only crosses decoration (non-pure chars) and the
# recovered span is the loose shape of a head: the head char
# plus up to _RECOVER_INNER_MAX arbitrary chars plus the
# chapter char, the numeral inside never validated. Real
# books stack separator fields up to about 52 chars, so the
# scan window is 64; the binding guard is that nothing but
# decoration may precede the head.
_RECOVER_SCAN = 64
_RECOVER_INNER_MAX = 6

_FW_TABLE = {0x3000: 0x20}
for _fw_i in range(0x21, 0x7F):
    _FW_TABLE[_fw_i + 0xFEE0] = _fw_i
_FW_TABLE = str.maketrans(_FW_TABLE)

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn2num(s):
    """Arabic or Chinese numeral string -> int, else None."""
    if not s:
        return None
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return None
    total = 0
    section = 0
    num = None
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                total += (section + (num or 0)) * unit
                section = 0
            else:
                section += (1 if num is None else num) * unit
            num = None
        else:
            return None
    result = total + section + (num or 0)
    return result if result > 0 else None


def _normalize_detect(text):
    """1:1 full-width -> half-width folding, detection only."""
    return text.translate(_FW_TABLE)


def _lead_punct_len(norm, pos):
    """Length of the punctuation / symbol / space run at pos
    in the detection copy: the decoration between a chapter
    head and its title proper. Skipped before any fixed or
    inferred title length is counted, and before an AI title
    window is cut."""
    j = pos
    n = len(norm)
    while j < n and norm[j] in _LEAD_PUNCT:
        j += 1
    return j - pos


def _is_pure_char(ch):
    """A pure title char: a letter or a number in the Unicode
    sense (CJK ideographs are Lo; full-width digits and
    letters count as themselves). Punctuation, symbols,
    spaces and other decoration are not pure: they never
    count toward a title length and never survive in a
    stored chapter title."""
    return unicodedata.category(ch)[:1] in ("L", "N")


def _pure_len(s):
    """How many pure chars a string contains."""
    return sum(1 for ch in s if _is_pure_char(ch))


def _purify_title(t):
    """A stored chapter title reduced to its pure chars:
    punctuation, symbols, spaces and decoration dropped,
    so a chapter name is nothing but text and numbers."""
    if not t:
        return ""
    return "".join(ch for ch in t if _is_pure_char(ch))


def _pure_take_end(norm, start, k, bound):
    """Offset just past the k-th pure char at/after start in
    the detection copy (offsets are 1:1 with the original
    text). Non-pure chars inside the span are skipped: they
    do not count, so a count-based title extends forward
    until k pure chars are gathered. The scan never crosses
    bound (the next anchor, or the text end); a span with
    fewer than k pure chars ends just past its last pure
    char (at start when it has none)."""
    if k <= 0:
        return start
    bound = min(bound, len(norm))
    end = start
    seen = 0
    j = start
    while j < bound:
        if _is_pure_char(norm[j]):
            seen += 1
            end = j + 1
            if seen >= k:
                break
        j += 1
    return end


def _match_head(norm, j):
    """Chapter head at position j in the detection copy:
    (head_end, num) or None; num is None for special names
    (序章 / 楔子 / 番外 etc.)."""
    m = _HEAD_RE.match(norm, j)
    if m:
        return m.end(), _cn2num(m.group(1))
    m = _SPECIAL_RE.match(norm, j)
    if m:
        return m.end(), None
    return None


def _recover_head(norm, o):
    """Loose head recovery at a bare anchor: skip the
    decoration field at o (non-pure chars only, at most
    _RECOVER_SCAN of them); when the first pure char is the
    head char and the chapter char follows within
    _RECOVER_INNER_MAX arbitrary chars, the span between
    them is a recovered head, returned as (hs, he). The
    numeral inside is never validated: a corrupted char
    there is exactly what the strict pattern rejected.
    None when the fragment does not open with such a head
    (plain prose, or decoration longer than the scan)."""
    n = len(norm)
    j = o
    while j < n and j - o < _RECOVER_SCAN \
            and not _is_pure_char(norm[j]):
        j += 1
    if j >= n or norm[j] != "第":
        return None
    for e in range(j + 2,
                   min(j + 2 + _RECOVER_INNER_MAX, n)):
        if norm[e] == "章":
            return j, e + 1
    return None


def _pure_chars_from(norm, start, bound):
    """The first _AI_WINDOW pure chars at/after start in the
    detection copy, gathered without crossing bound:
    non-pure chars are skipped, never counted, exactly the
    window a boundary-selection candidate is judged on."""
    out = []
    j = start
    bound = min(bound, len(norm))
    while j < bound and len(out) < _AI_WINDOW:
        if _is_pure_char(norm[j]):
            out.append(norm[j])
        j += 1
    return "".join(out)


def _seg_words(s):
    """jieba word list tiling s exactly, or None when jieba
    is unavailable, raises, or its tokens do not reassemble
    into s. Callers treat None as no segmentation: the
    chapter falls back, splitting never breaks over it."""
    if not s:
        return None
    try:
        import jieba
        try:
            jieba.setLogLevel(60)
        except Exception:
            pass
        words = [w for w in jieba.cut(s) if w]
    except Exception:
        return None
    if not words or "".join(words) != s:
        return None
    return words


def _strong_title_end(norm, hs, he):
    """Structural end of an embedded title: earliest newline,
    else earliest sentence punctuation, within the window and
    at most _TITLE_MAX_AFTER chars past the head. None when the
    title is glued to the body without a usable break."""
    window = norm[hs:hs + _TITLE_WINDOW]
    for marks in ("\n", "。！？!?"):
        best = None
        for mk in marks:
            i = window.find(mk)
            if i >= 0 and (best is None or i < best):
                best = i
        if best is not None and hs + best - he <= _TITLE_MAX_AFTER:
            return hs + best
    return None


def _comma_title_end(norm, hs, he):
    """Weak structural end: a comma shortly after the head
    (full-width commas fold to half-width in the detection
    copy, so both forms are searched)."""
    best = None
    for mk in ("，", ","):
        i = norm.find(mk, hs, hs + _TITLE_WINDOW)
        if i >= 0 and (best is None or i < best):
            best = i
    if best is not None and best - he <= _TITLE_MAX_AFTER:
        return best
    return None


def _is_symbol_line(nstrip):
    """A line made only of separator glyphs (a printed divider:
    ***, ※※※, ---, ———, ===, ◇◇◇ ...)."""
    if len(nstrip) < 3 or not _SYMBOL_LINE_RE.match(nstrip):
        return False
    return sum(1 for ch in nstrip if ch not in " \t") >= 3


def _first_fragment(text, start):
    """Auto-title fragment after a bare separator: the first
    pure chars (at most 12) of the chapter's first sentence.
    The leading separator field (stars / spaces / newlines,
    possibly several printed lines of it) is skipped first,
    so a decorative header can never leak into the name."""
    seg = text[start:start + 400]
    j = 0
    while j < len(seg) and not _is_pure_char(seg[j]):
        j += 1
    seg = seg[j:]
    cut = len(seg)
    for mk in "。！？\n":
        i = seg.find(mk)
        if 0 <= i < cut:
            cut = i
    out = []
    for ch in seg[:cut]:
        if _is_pure_char(ch):
            out.append(ch)
            if len(out) >= 12:
                break
    return "".join(out)


def _split_mechanical(text):
    """Fallback: cut a long markerless book every ~9000 chars at
    the nearest paragraph / sentence boundary."""
    chapters = []
    n = len(text)
    start = 0
    idx = 1
    while n - start > _MECH_SPAN:
        target = start + _MECH_SPAN
        lo = max(start + 1000, target - 1500)
        hi = min(n, target + 1500)
        cut = -1
        for marks in ("\n", "。！？!?"):
            best = None
            for p in range(lo, hi):
                if text[p] in marks and (
                        best is None
                        or abs(p - target) < abs(best - target)):
                    best = p
            if best is not None:
                cut = best + 1
                break
        if cut < 0:
            cut = target
        chapters.append({"t": "分段 %d" % idx, "o": start})
        idx += 1
        start = cut
        while start < n and text[start] in "\r\n":
            start += 1
        if start >= n:
            break
    if start < n and (not chapters or chapters[-1]["o"] != start):
        chapters.append({"t": "分段 %d" % idx, "o": start})
    return chapters or [{"t": "正文", "o": 0}]


def _decode_txt(raw):
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except Exception:
            pass
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", "replace"), "utf-8"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), "utf-8"


_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_AI_BATCH_DEFAULT = 10
_AI_MAX_WORKERS = 4
_AI_MARK = "（AI）"
_AI_RAW_KEEP = 2000
_AI_MAX_TOKENS_BASE = 40
_AI_MAX_TOKENS_PER_ITEM = 16
_AI_MAX_TOKENS_CAP = 512
_AI_RESP_BYTES_PER_ITEM = 256
_AI_RESP_BYTES_SLACK = 2048
_AI_RESP_CHUNK = 8192
_AI_PAIR_RE = re.compile(r"(\d{1,6})\s*[:：]\s*(\d{1,3})")
_AI_SYSTEM = (
    "你是小说章节标题定界助手。用户会给出带序号的文字片段，每条都"
    "是一章开头处标题与正文连排在一起的片段：标题在前，正文紧随"
    "其后，中间没有标点或换行。请判断每条片段中属于标题的字数："
    "字数只数文字（汉字、字母、数字），片段里混入的标点、符号、"
    "空格都不算字。标题是简短的名称性短语，正文是叙述句子的开始。逐条回答，每行"
    "一条，格式为「序号:字数」，序号与用户给出的序号相同；只输出"
    "这些行，不要输出任何其他文字。\n"
    "示例：片段「101: 江湖夜雨十年灯他推开客栈的门」中，标题是"
    "「江湖夜雨十年灯」共 7 字，应回答「101:7」；片段「102: 归乡"
    "他背着行囊沿着山路往回走」中，标题是「归乡」共 2 字，应回答"
    "「102:2」。")


def _ai_batch_size():
    """Configured AI batch size (kv ai_title_batch); the
    code default applies when unset or invalid."""
    try:
        v = int(kv_get("ai_title_batch") or "")
    except Exception:
        return _AI_BATCH_DEFAULT
    if v <= 0:
        return _AI_BATCH_DEFAULT
    return min(50, v)


def _ai_parse_batch(content, batch):
    """Tolerant per-batch reply parsing. Primary: explicit
    id:length pairs, aligned by id. Fallbacks when no pair
    matches a batch id: pair values in reply order (when
    their count equals the batch size), then all integers
    in the text (same count condition). Returns {id: k}
    with batch ids only; k still needs range validation."""
    want = [it["id"] for it in batch]
    pairs = []
    for m in _AI_PAIR_RE.finditer(content or ""):
        try:
            pairs.append((int(m.group(1)), int(m.group(2))))
        except Exception:
            pass
    by_id = {}
    for i, k in pairs:
        if i in want and i not in by_id:
            by_id[i] = k
    if by_id:
        return by_id
    if pairs and len(pairs) == len(batch):
        return {it["id"]: k for it, (_i, k) in zip(batch, pairs)}
    ints = []
    for x in re.findall(r"\d+", content or ""):
        try:
            ints.append(int(x))
        except Exception:
            pass
    if ints and len(ints) == len(batch):
        return {it["id"]: k for it, k in zip(batch, ints)}
    return {}


def _ai_max_tokens(n):
    """Request-side reply budget for one batch. A batch
    reply is one short id:length line per item, so the
    allowance scales with the batch size under a hard cap
    that also guards a bogus batch-size configuration."""
    try:
        n = int(n)
    except Exception:
        n = 1
    return min(_AI_MAX_TOKENS_CAP, _AI_MAX_TOKENS_BASE
               + _AI_MAX_TOKENS_PER_ITEM * max(1, n))


def _ai_resp_bytes(n):
    """Read-side hard cap, in bytes, on one batch response:
    the JSON envelope plus one short reply line per item,
    with slack. A reply anywhere near this cap is already
    far outside the expected shape (runaway generation,
    loops, long refusals); reading stops at the cap and
    the batch fails instead of consuming the whole reply."""
    try:
        n = int(n)
    except Exception:
        n = 1
    return _AI_RESP_BYTES_SLACK + _AI_RESP_BYTES_PER_ITEM * max(1, n)


def _ai_ask_batch(batch, key, model):
    """One OpenRouter chat-completions call for one small
    batch of title windows (each window starts at its
    title's first char: chapter head and leading
    punctuation already skipped). Returns (lengths, raw,
    error): lengths maps item id -> judged length
    (range-validated) and may be partial or empty; raw is
    the model's reply text (truncated), or None when the
    request itself failed; error is a short reason or
    None. Never raises; the key only travels in the
    Authorization header. The reply size is bounded on
    both sides: the request disables reasoning (a
    reasoning model otherwise spends thousands of hidden
    reasoning tokens before the tiny visible answer,
    dwarfing the reply and eating any token budget),
    carries a max_tokens budget scaled to the batch, and
    the response is read under a byte cap; a reply that
    overruns the cap is cut off
    mid-read (connection closed) and the batch fails with
    an over-limit reason, keeping only the in-cap prefix
    as raw."""
    listing = "\n".join("%d: %s" % (it["id"], it["window"])
                        for it in batch)
    payload = {"model": model, "temperature": 0,
               "max_tokens": _ai_max_tokens(len(batch)),
               "reasoning": {"effort": "none"},
               "messages": [
                   {"role": "system", "content": _AI_SYSTEM},
                   {"role": "user", "content": listing}]}
    try:
        req = urllib.request.Request(
            _OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        cap = _ai_resp_bytes(len(batch))
        buf = bytearray()
        over = False
        with urllib.request.urlopen(req, timeout=90) as resp:
            while True:
                chunk = resp.read(min(_AI_RESP_CHUNK,
                                      cap + 1 - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > cap:
                    over = True
                    break
        if over:
            prefix = bytes(buf[:cap]).decode("utf-8", "replace")
            return {}, prefix[:_AI_RAW_KEEP], "AI 输出超限终止"
        data = json.loads(bytes(buf).decode("utf-8"))
        content = data["choices"][0]["message"]["content"] or ""
    except Exception as exc:
        return {}, None, "AI 请求失败：%s" % str(exc)[:120]
    raw = content[:_AI_RAW_KEEP]
    got = _ai_parse_batch(content, batch)
    if not got:
        return {}, raw, "AI 返回不可解析"
    out = {}
    winlen = {it["id"]: len(it["window"]) for it in batch}
    for i, k in got.items():
        if (isinstance(k, int) and not isinstance(k, bool)
                and 1 <= k <= winlen.get(i, _AI_WINDOW)):
            out[i] = k
    if not out:
        return {}, raw, "AI 返回全部越界"
    return out, raw, None


def _ai_delimit_batch(items, key, model,
                      batch_size=_AI_BATCH_DEFAULT):
    """Protocol v2: judge title lengths for unique windows
    in small numbered batches. items is a list of
    {"id", "window"} with unique windows and stable ids
    (the candidate's chapter number where available).
    Batches are independent requests and run concurrently
    (up to _AI_MAX_WORKERS in flight); each batch keeps its
    own tolerant parsing, fallback and raw record, and the
    records are reassembled in batch order, so the outcome
    is identical to running the batches one by one. Only
    the HTTP calls and parsing run in worker threads; the
    aggregation here is single-threaded. Returns
    (results, batches): results maps id -> validated
    length for the items the model settled; batches is a
    per-batch record {"ids", "raw", "error"} kept for
    observability (the raw reply text is what v1 never
    surfaced). A failed batch only leaves its own items
    unsettled. Never raises."""
    results = {}
    batches = []
    if not items:
        return results, batches
    try:
        bs = int(batch_size)
    except Exception:
        bs = _AI_BATCH_DEFAULT
    bs = max(1, min(50, bs))
    chunks = [items[s:s + bs] for s in range(0, len(items), bs)]
    slots = [None] * len(chunks)

    def run(pair):
        i, batch = pair
        try:
            got, raw, err = _ai_ask_batch(batch, key, model)
        except Exception as exc:
            # _ai_ask_batch never raises by contract; keep
            # the batch-isolation guarantee regardless.
            got, raw, err = {}, None, (
                "AI 请求失败：%s" % str(exc)[:120])
        return i, got, {"ids": [it["id"] for it in batch],
                        "raw": raw, "error": err}

    if len(chunks) > 1:
        from concurrent.futures import ThreadPoolExecutor
        workers = min(_AI_MAX_WORKERS, len(chunks))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, got, rec in ex.map(run, enumerate(chunks)):
                slots[i] = (got, rec)
    else:
        for pair in enumerate(chunks):
            i, got, rec = run(pair)
            slots[i] = (got, rec)
    for got, rec in slots:
        results.update(got)
        batches.append(rec)
    return results, batches


_AI_BOUND_SYSTEM = (
    "你是小说章节标题定界助手。用户会给出带序号的若干条片段，"
    "每条是一个章节的开头：先是章首（如「第土四章」，章首里可"
    "能有个别错字，不用管它），紧跟着是标题与正文连排在一起"
    "的文字：标题在前，正文紧随其后，中间没有标点或换行。"
    "每条的候选文字已经按词切分并逐词编号。请判断每条的标题"
    "在第几个词之后结束：标题是简短的名称性短语，正文是叙述"
    "句子的开始。逐条回答，每行一条，格式为「序号:界号」，"
    "序号与用户给出的序号相同；只输出这些行，不要输出任何"
    "其他文字。\n"
    "示例：片段「101: 章首：「第三章」候选（已分词编号）："
    "[1]江湖 [2]夜雨 [3]十年 [4]灯 [5]他 [6]推开 [7]客栈」中，"
    "标题是「江湖夜雨十年灯」，在第 4 个词之后结束，应回答"
    "「101:4」。")


def _ai_ask_bounds(batch, key, model):
    """One OpenRouter call for one small batch of recovered
    head boundary items (each item carries its head span and
    its candidate window already segmented into numbered
    words). Returns (bounds, raw, error): bounds maps item
    id -> chosen word boundary (range-validated against the
    item's word count) and may be partial or empty; raw is
    the model's reply text (truncated), or None when the
    request itself failed; error is a short reason or None.
    Same transport discipline as _ai_ask_batch: reasoning
    off, a max_tokens budget scaled to the batch, and a
    byte cap on the read with an over-limit abort. Never
    raises; the key only travels in the Authorization
    header."""
    listing = "\n".join(
        "%d: 章首：「%s」候选（已分词编号）：%s"
        % (it["id"], it["head"], it["numbered"])
        for it in batch)
    payload = {"model": model, "temperature": 0,
               "max_tokens": _ai_max_tokens(len(batch)),
               "reasoning": {"effort": "none"},
               "messages": [
                   {"role": "system", "content": _AI_BOUND_SYSTEM},
                   {"role": "user", "content": listing}]}
    try:
        req = urllib.request.Request(
            _OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        cap = _ai_resp_bytes(len(batch))
        buf = bytearray()
        over = False
        with urllib.request.urlopen(req, timeout=90) as resp:
            while True:
                chunk = resp.read(min(_AI_RESP_CHUNK,
                                      cap + 1 - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > cap:
                    over = True
                    break
        if over:
            prefix = bytes(buf[:cap]).decode("utf-8", "replace")
            return {}, prefix[:_AI_RAW_KEEP], "AI 输出超限终止"
        data = json.loads(bytes(buf).decode("utf-8"))
        content = data["choices"][0]["message"]["content"] or ""
    except Exception as exc:
        return {}, None, "AI 请求失败：%s" % str(exc)[:120]
    raw = content[:_AI_RAW_KEEP]
    got = _ai_parse_batch(content, batch)
    if not got:
        return {}, raw, "AI 返回不可解析"
    out = {}
    nwords = {it["id"]: it["nwords"] for it in batch}
    for i, k in got.items():
        if (isinstance(k, int) and not isinstance(k, bool)
                and 1 <= k <= nwords.get(i, 0)):
            out[i] = k
    if not out:
        return {}, raw, "AI 返回全部越界"
    return out, raw, None


def _ai_delimit_bounds(items, key, model,
                       batch_size=_AI_BATCH_DEFAULT):
    """Boundary selection for recovered heads: judge, in
    small numbered batches under the same concurrency and
    per-batch isolation as _ai_delimit_batch, after which
    word each item's title ends. items is a list of
    {"id", "head", "numbered", "nwords"} with stable unique
    ids. Returns (results, batches) with the same shapes as
    _ai_delimit_batch: results maps id -> validated word
    boundary for the items the model settled; a failed
    batch only leaves its own items unsettled. Never
    raises."""
    results = {}
    batches = []
    if not items:
        return results, batches
    try:
        bs = int(batch_size)
    except Exception:
        bs = _AI_BATCH_DEFAULT
    bs = max(1, min(50, bs))
    chunks = [items[s:s + bs] for s in range(0, len(items), bs)]
    slots = [None] * len(chunks)

    def run(pair):
        i, batch = pair
        try:
            got, raw, err = _ai_ask_bounds(batch, key, model)
        except Exception as exc:
            got, raw, err = {}, None, (
                "AI 请求失败：%s" % str(exc)[:120])
        return i, got, {"ids": [it["id"] for it in batch],
                        "raw": raw, "error": err}

    if len(chunks) > 1:
        from concurrent.futures import ThreadPoolExecutor
        workers = min(_AI_MAX_WORKERS, len(chunks))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, got, rec in ex.map(run, enumerate(chunks)):
                slots[i] = (got, rec)
    else:
        for pair in enumerate(chunks):
            i, got, rec = run(pair)
            slots[i] = (got, rec)
    for got, rec in slots:
        results.update(got)
        batches.append(rec)
    return results, batches


def _split_chapters_full(text, fixed_n=0, ai_cfg=None,
                         ai_all_glued=False, defer_ai=False,
                         ai_only_offsets=None):
    """Chapter splitting with a breakdown of how each title
    was delimited. Returns (chapters, stats, ai_debug):
    stats counts delimitation methods per anchor (pre-merge)
    as rule / ai / fixed, or a mechanical count when the
    whole book fell back to mechanical segmentation;
    ai_debug carries per-candidate detail for the admin
    dry-run test. ai_cfg is {"key", "model"} or None.
    defer_ai runs the rules only (any ai_cfg is ignored and
    no AI request is made) and instead marks the chapters
    AI could still improve: they carry a "pend" flag here
    and stats gains an ai_pending count. ai_only_offsets
    restricts AI candidacy to anchors at those offsets
    (the deferred pass for one book, run on first read)."""
    if defer_ai:
        ai_cfg = None
    norm = _normalize_detect(text)
    n = len(text)
    claimed = bytearray(n)
    anchors = []    # {"o","hs","he","num","title","ahl"}

    def claim(a, b):
        b = min(n, b)
        if b > a:
            claimed[a:b] = b"\x01" * (b - a)

    def gap_head(e):
        j = e
        while j < n and j - e <= _SEP_GAP_MAX and norm[j] in " \t\r\n":
            j += 1
        if j < n:
            hd = _match_head(norm, j)
            if hd:
                return j, hd[0], hd[1]
        return None

    def bare(o):
        return {"o": o, "hs": None, "he": None, "num": None,
                "title": None, "ahl": None}

    # Pass 1: inline separator runs (asterisk / ※ / ◇◆ groups).
    # A chapter head right after the run (same line or immediately
    # following) starts the chapter at the run; a bare run also
    # starts a chapter and gets an auto title below.
    for m in _SEP_RUN_RE.finditer(norm):
        s, e = m.start(), m.end()
        gh = gap_head(e)
        if gh:
            j, he, num = gh
            anchors.append({"o": s, "hs": j, "he": he, "num": num,
                            "title": None, "ahl": None})
            claim(s, j + _TITLE_WINDOW)
        else:
            anchors.append(bare(s))
            claim(s, e)

    # Pass 2: line pass — symbol-only separator lines, whole-line
    # titles, and paragraph-style lines starting with a head.
    pos = 0
    for line in text.splitlines(keepends=True):
        lend = pos + len(line)
        raw = line.rstrip("\r\n")
        stripped = raw.strip()
        nstrip = norm[pos:lend].rstrip("\r\n").strip()
        head_pos = pos + (len(raw) - len(raw.lstrip()))
        if stripped and not (head_pos < n and claimed[head_pos]):
            if _is_symbol_line(nstrip):
                gh = gap_head(lend)
                if gh:
                    j, he, num = gh
                    anchors.append({"o": pos, "hs": j, "he": he,
                                    "num": num, "title": None,
                                    "ahl": None})
                    claim(pos, j + _TITLE_WINDOW)
                else:
                    anchors.append(bare(pos))
                    claim(pos, lend)
            elif _LINE_TITLE_RE.match(norm[pos:lend].rstrip("\r\n")):
                hd = _match_head(norm, head_pos)
                if len(stripped) <= 60:
                    anchors.append({
                        "o": pos, "hs": head_pos,
                        "he": hd[0] if hd else None,
                        "num": hd[1] if hd else None,
                        "title": stripped[:80],
                        "tend": pos + len(raw),
                        "ahl": (_pure_len(stripped[:80]
                                          [hd[0] - head_pos:])
                                if hd else None)})
                elif hd:
                    anchors.append({"o": pos, "hs": head_pos,
                                    "he": hd[0], "num": hd[1],
                                    "title": None, "ahl": None})
        pos = lend

    anchors.sort(key=lambda c: c["o"])
    dedup = []
    for a in anchors:
        if dedup and dedup[-1]["o"] == a["o"]:
            if dedup[-1]["title"] is None and a["title"] is not None:
                dedup[-1] = a
            continue
        dedup.append(a)
    anchors = dedup

    stats = {"rule": 0, "ai": 0, "fixed": 0, "mechanical": 0,
             "chapters": 0, "model": None, "fallback": None}
    debug = []
    debug_by_idx = {}

    # Head recovery: a bare anchor whose fragment opens with
    # a loosely shaped head (corrupted numeral, decoration
    # the gap scan could not cross) is promoted to a
    # recovered head. Recovered heads are delimited only by
    # boundary selection further down; when that cannot
    # settle one (no AI, no segmentation, a failed batch),
    # the anchor is demoted back to bare and keeps the auto
    # title it would have had today.
    recovered_n = 0
    recovered_idx = set()
    for idx, a in enumerate(anchors):
        if a["hs"] is None:
            rec = _recover_head(norm, a["o"])
            if rec and idx + 1 < len(anchors) \
                    and rec[1] > anchors[idx + 1]["o"]:
                # The head lies past the next anchor: it is
                # that chapter's head, not this one's (a
                # stack of separator fields before one head
                # promotes only the closest anchor).
                rec = None
            if rec:
                a["hs"], a["he"] = rec
                a["recovered"] = True
                recovered_idx.add(idx)
                num = _cn2num(norm[rec[0] + 1:rec[1] - 1])
                if num is not None:
                    a["num"] = num
                recovered_n += 1
    if recovered_n:
        stats["recovered"] = recovered_n

    # Mechanical fallback: smart splitting found almost nothing
    # in a long book.
    if len(anchors) < 3 and n > _MECH_MIN_CHARS:
        chs = _split_mechanical(text)
        chs = [{"t": _purify_title(c["t"]), "o": c["o"]}
               for c in chs]
        stats["mechanical"] = len(chs)
        stats["chapters"] = len(chs)
        return chs, stats, debug

    # Resolve embedded titles: strong structural breaks first;
    # the after-head lengths they yield feed a per-book median
    # inference for titles glued to the body without a break.
    for a in anchors:
        if a["title"] is None and a["hs"] is not None \
                and not a.get("recovered"):
            end = _strong_title_end(norm, a["hs"], a["he"])
            if end is not None:
                a["title"] = text[a["hs"]:end].strip()[:60]
                a["how"] = "rule"
                a["tend"] = end
                a["ahl"] = _pure_len(norm[a["he"]:end])
    ahls = sorted(a["ahl"] for a in anchors
                  if a["ahl"] is not None and a["num"] is not None)
    num_anchors = sum(1 for a in anchors
                      if a["hs"] is not None and a["num"] is not None
                      and not a.get("recovered"))
    inferred = None
    if len(ahls) >= 3 and len(ahls) * 2 >= num_anchors:
        mid = len(ahls) // 2
        inferred = (ahls[mid] if len(ahls) % 2
                    else int((ahls[mid - 1] + ahls[mid]) / 2 + 0.5))
    # AI candidates: embedded titles still unsettled. In a
    # real split, titles the inference can settle are not
    # candidates; the dry-run test passes ai_all_glued=True
    # to judge every glued title. Each window is cut on the
    # detection copy starting at the title's first char
    # (head and leading punctuation already skipped), and a
    # returned length k is a PURE char count: the title end
    # is where the k-th pure char after the head sits in the
    # original text (symbols inside the span are skipped,
    # never counted).
    ai_map = {}
    if ai_cfg:
        stats["model"] = ai_cfg.get("model") or ""
        cand = []
        for idx, a in enumerate(anchors):
            if a["title"] is not None or a["hs"] is None \
                    or a.get("recovered"):
                continue
            if (not ai_all_glued and a["num"] is not None
                    and inferred):
                continue
            if ai_only_offsets is not None \
                    and a["o"] not in ai_only_offsets:
                continue
            lead = _lead_punct_len(norm, a["he"])
            window = norm[a["he"] + lead:
                          a["he"] + lead + _AI_WINDOW]
            if window:
                cand.append((idx, lead, window))
        if cand:
            # Protocol v2: identical windows are asked once
            # and the verdict maps back to every copy; the
            # rest go in small numbered batches, so a bad
            # batch can only hurt its own items.
            uniq = []
            by_win = {}
            used_ids = set()
            synth = 900000
            for ci, (idx, lead, window) in enumerate(cand):
                ent = by_win.get(window)
                if ent is None:
                    num = anchors[idx]["num"]
                    if num is not None and num not in used_ids:
                        uid = num
                    else:
                        while synth in used_ids:
                            synth += 1
                        uid = synth
                        synth += 1
                    used_ids.add(uid)
                    ent = {"id": uid, "members": []}
                    by_win[window] = ent
                    uniq.append(ent)
                ent["members"].append(ci)
            items = [{"id": e["id"],
                      "window": cand[e["members"][0]][2]}
                     for e in uniq]
            results, batches = _ai_delimit_batch(
                items, ai_cfg.get("key") or "",
                ai_cfg.get("model") or "",
                ai_cfg.get("batch") or _AI_BATCH_DEFAULT)
            raw_by_id = {}
            reasons = []
            for b in batches:
                for i in b["ids"]:
                    raw_by_id[i] = b["raw"]
                if b["error"] and b["error"] not in reasons:
                    reasons.append(b["error"])
            stats["ai_batches"] = [
                {"ids": b["ids"],
                 "raw": (b["raw"] or "")[:300] or None,
                 "error": b["error"]} for b in batches]
            unresolved = 0
            for e in uniq:
                k = results.get(e["id"])
                raw = raw_by_id.get(e["id"])
                for ci in e["members"]:
                    idx, lead, window = cand[ci]
                    if k is not None:
                        he_i = anchors[idx]["he"]
                        bnd = (anchors[idx + 1]["o"]
                               if idx + 1 < len(anchors) else n)
                        ai_map[idx] = _pure_take_end(
                            norm, he_i, k, max(bnd, he_i))
                    else:
                        unresolved += 1
                    debug_by_idx[idx] = {
                        "o": anchors[idx]["o"],
                        "num": anchors[idx]["num"],
                        "window": window,
                        "ai_len": k,
                        "ai_raw": (raw or "")[:1000] or None,
                        "ai_title": None}
            if unresolved:
                reasons.append(
                    "AI %d 项未判，已单独回退" % unresolved)
            if reasons:
                stats["fallback"] = "；".join(reasons[:3])
        debug = [debug_by_idx[i] for i in sorted(debug_by_idx)]

    # Boundary selection for recovered heads: the candidate
    # window after the recovered head is segmented into
    # numbered words and the model picks the word boundary
    # the title ends at, instead of counting raw chars over
    # a head the rules could not even recognise. A chapter
    # the model settles is applied below; every other
    # recovered anchor is demoted back to bare there and
    # keeps its auto title, so a recovered chapter can never
    # end up worse than it is today.
    bound_map = {}
    if ai_cfg:
        stats["model"] = ai_cfg.get("model") or ""
        rec_cand = []
        for idx, a in enumerate(anchors):
            if not a.get("recovered") or a["title"] is not None:
                continue
            if ai_only_offsets is not None \
                    and a["o"] not in ai_only_offsets:
                continue
            bnd = (anchors[idx + 1]["o"]
                   if idx + 1 < len(anchors) else n)
            pure = _pure_chars_from(norm, a["he"],
                                    max(bnd, a["he"]))
            words = _seg_words(pure) if pure else None
            if words and len(words) >= 2:
                rec_cand.append((idx, pure, words))
        if rec_cand:
            used_ids = set()
            synth = 800000
            items = []
            meta_by_id = {}
            for idx, pure, words in rec_cand:
                while synth in used_ids:
                    synth += 1
                uid = synth
                used_ids.add(uid)
                synth += 1
                numbered = " ".join(
                    "[%d]%s" % (i, w)
                    for i, w in enumerate(words, 1))
                items.append({
                    "id": uid,
                    "head": text[anchors[idx]["hs"]:
                                 anchors[idx]["he"]],
                    "numbered": numbered,
                    "nwords": len(words)})
                meta_by_id[uid] = (idx, pure, words)
            results, rbatches = _ai_delimit_bounds(
                items, ai_cfg.get("key") or "",
                ai_cfg.get("model") or "",
                ai_cfg.get("batch") or _AI_BATCH_DEFAULT)
            raw_by_id = {}
            reasons = []
            for b in rbatches:
                for i in b["ids"]:
                    raw_by_id[i] = b["raw"]
                if b["error"] and b["error"] not in reasons:
                    reasons.append(b["error"])
            stats["ai_batches"] = (stats.get("ai_batches")
                                   or []) + [
                {"ids": b["ids"],
                 "raw": (b["raw"] or "")[:300] or None,
                 "error": b["error"]} for b in rbatches]
            unresolved = 0
            for it in items:
                idx, pure, words = meta_by_id[it["id"]]
                k = results.get(it["id"])
                pure_k = None
                if k is not None:
                    pure_k = sum(len(w) for w in words[:k])
                    he_i = anchors[idx]["he"]
                    bnd = (anchors[idx + 1]["o"]
                           if idx + 1 < len(anchors) else n)
                    bound_map[idx] = _pure_take_end(
                        norm, he_i, pure_k, max(bnd, he_i))
                else:
                    unresolved += 1
                debug_by_idx[idx] = {
                    "o": anchors[idx]["o"],
                    "num": anchors[idx]["num"],
                    "window": pure,
                    "ai_len": pure_k,
                    "ai_raw": (raw_by_id.get(it["id"])
                               or "")[:1000] or None,
                    "ai_title": None}
            if unresolved:
                reasons.append(
                    "AI %d 项未判，已单独回退" % unresolved)
            if reasons:
                prev = stats.get("fallback")
                stats["fallback"] = "；".join(
                    ([prev] if prev else []) + reasons[:3])
            debug = [debug_by_idx[i] for i in sorted(debug_by_idx)]

    for idx, a in enumerate(anchors):
        if not a.get("recovered"):
            continue
        if idx in bound_map:
            a["title"] = text[a["hs"]:bound_map[idx]].strip()[:60]
            a["tend"] = bound_map[idx]
            a["how"] = "ai"
            a["rec_ok"] = True
        else:
            a["hs"] = None
            a["he"] = None
            a.pop("recovered", None)

    # Deferred-pass bare chapters (ai_only_offsets mode
    # only, i.e. the on-read completion pass for one marked
    # book): a bare anchor has no head for either protocol,
    # but its auto title is a blind slice of the opening
    # sentence, so the char-count protocol judges where the
    # opening fragment should end. The fragment is applied
    # by the numbering loop below; a chapter the model does
    # not settle keeps its auto title and stays pending.
    if ai_cfg and ai_only_offsets is not None:
        bare_cand = []
        for idx, a in enumerate(anchors):
            if a["hs"] is not None or a["title"] is not None \
                    or idx in recovered_idx:
                continue
            if a["o"] not in ai_only_offsets:
                continue
            j = a["o"]
            while j < n and not _is_pure_char(norm[j]):
                j += 1
            if j >= n:
                continue
            window = norm[j:j + _AI_WINDOW]
            if window:
                bare_cand.append((idx, j, window))
        if bare_cand:
            stats["model"] = ai_cfg.get("model") or ""
            used_ids = set()
            synth = 700000
            items = []
            meta_by_id = {}
            for idx, j, window in bare_cand:
                while synth in used_ids:
                    synth += 1
                uid = synth
                used_ids.add(uid)
                synth += 1
                items.append({"id": uid, "window": window})
                meta_by_id[uid] = (idx, j, window)
            results, bbatches = _ai_delimit_batch(
                items, ai_cfg.get("key") or "",
                ai_cfg.get("model") or "",
                ai_cfg.get("batch") or _AI_BATCH_DEFAULT)
            raw_by_id = {}
            reasons = []
            for b in bbatches:
                for i in b["ids"]:
                    raw_by_id[i] = b["raw"]
                if b["error"] and b["error"] not in reasons:
                    reasons.append(b["error"])
            stats["ai_batches"] = (stats.get("ai_batches")
                                   or []) + [
                {"ids": b["ids"],
                 "raw": (b["raw"] or "")[:300] or None,
                 "error": b["error"]} for b in bbatches]
            unresolved = 0
            for it in items:
                idx, j, window = meta_by_id[it["id"]]
                k = results.get(it["id"])
                if k is not None:
                    bnd = (anchors[idx + 1]["o"]
                           if idx + 1 < len(anchors) else n)
                    end = _pure_take_end(norm, j, k,
                                         max(bnd, j))
                    frag = text[j:end].strip()
                    if frag:
                        anchors[idx]["bare_frag"] = frag
                        anchors[idx]["how"] = "ai"
                    else:
                        k = None
                if k is None:
                    unresolved += 1
                debug_by_idx[idx] = {
                    "o": anchors[idx]["o"],
                    "num": anchors[idx]["num"],
                    "window": window,
                    "ai_len": k,
                    "ai_raw": (raw_by_id.get(it["id"])
                               or "")[:1000] or None,
                    "ai_title": None}
            if unresolved:
                reasons.append(
                    "AI %d 项未判，已单独回退" % unresolved)
            if reasons:
                prev = stats.get("fallback")
                stats["fallback"] = "；".join(
                    ([prev] if prev else []) + reasons[:3])
            debug = [debug_by_idx[i] for i in sorted(debug_by_idx)]

    # Deferred (lazy) splits: the anchors AI could still
    # improve are the recovered heads, every bare anchor
    # (auto titles), and the glued heads the eager candidate
    # collection would have sent to the model. Their
    # chapters are flagged "pend" below and counted into
    # stats["ai_pending"]; nothing was asked of the model.
    pending_idx = set()
    if defer_ai:
        for idx, a in enumerate(anchors):
            if idx in recovered_idx or a["hs"] is None:
                pending_idx.add(idx)
            elif a["title"] is None and not (
                    a["num"] is not None and inferred):
                pending_idx.add(idx)

    for idx, a in enumerate(anchors):
        if a["title"] is not None or a["hs"] is None:
            continue
        end = None
        bnd = (anchors[idx + 1]["o"] if idx + 1 < len(anchors)
               else n)
        if idx in ai_map:
            end = ai_map[idx]
            a["how"] = "ai"
        elif a["num"] is not None:
            if fixed_n and fixed_n > 0:
                end = _pure_take_end(norm, a["he"], fixed_n,
                                     max(bnd, a["he"]))
                a["how"] = "fixed"
            elif inferred:
                end = _pure_take_end(norm, a["he"], inferred,
                                     max(bnd, a["he"]))
                a["how"] = "rule"
        if end is None:
            end = _comma_title_end(norm, a["hs"], a["he"])
            if end is not None:
                a["how"] = "rule"
        if end is None:
            end = (_pure_take_end(norm, a["he"],
                                  _TITLE_DEFAULT_N,
                                  max(bnd, a["he"]))
                   if a["num"] is not None
                   else _pure_take_end(norm, a["hs"],
                                       _TITLE_FALLBACK,
                                       max(bnd, a["hs"])))
            a["how"] = "fixed"
        a["title"] = text[a["hs"]:end].strip()[:60]
        a["tend"] = end

    # Auto titles for bare separator chapters, numbered after the
    # previous known chapter number when there is one.
    ordinal = 0
    last_num = 0
    for a in anchors:
        ordinal += 1
        if a["title"] is None:
            num = last_num + 1 if last_num else ordinal
            frag = a.get("bare_frag") or _first_fragment(
                text, a["o"])
            a["title"] = ("第%d章 %s" % (num, frag)).strip()
            a["num"] = num
        elif a["num"] is None and a.pop("rec_ok", False):
            # A recovered head settled by boundary selection
            # still consumes the chapter-number slot its auto
            # title would have used, so the numbering of the
            # bare chapters after it does not drift.
            a["num"] = last_num + 1 if last_num else ordinal
        if a["num"]:
            last_num = a["num"]

    # Final titles: purify every title to its pure chars. A
    # head chapter whose delimited segment carried decoration
    # but no pure char beyond the head itself (an
    # all-decoration segment, e.g. a head followed by a star
    # field) is extended into the following body text to
    # collect its first pure chars: a chapter never ends up
    # with an empty or decoration-only name, and no
    # placeholder is ever invented. A clean bare head (a
    # heading-only line with nothing after it) is left as is.
    for idx, a in enumerate(anchors):
        if a["title"] is None:
            continue
        if a["hs"] is not None and a["he"] is not None:
            head_raw = text[a["hs"]:a["he"]]
            headpart = _purify_title(head_raw)
            pure = _purify_title(a["title"])
            content = (pure[len(headpart):]
                       if headpart and pure.startswith(headpart)
                       else pure)
            raw_after = a["title"][len(head_raw):] \
                if a["title"].startswith(head_raw) else ""
            if (not content and raw_after
                    and _pure_len(raw_after) < len(raw_after)):
                bnd = (anchors[idx + 1]["o"]
                       if idx + 1 < len(anchors) else n)
                start = a.get("tend") or a["he"]
                fend = _pure_take_end(norm, start, 12,
                                      max(bnd, start))
                if fend > start:
                    a["title"] = a["title"] + text[start:fend]
        a["title"] = _purify_title(a["title"])
        if idx in debug_by_idx and a.get("how") == "ai":
            debug_by_idx[idx]["ai_title"] = a["title"]

    chapters = []
    for aidx, a in enumerate(anchors):
        c = {"t": a["title"], "o": a["o"]}
        if aidx in pending_idx:
            c["pend"] = True
        if a.get("how") == "ai":
            # Per-chapter provenance: this title's length was
            # judged by the AI. Stored on the chapter itself
            # so the AI-delimited subset stays exactly
            # identifiable (batch ids and chapter numbers
            # cannot reconstruct it once duplicate windows
            # and merge folding have had their say) and can
            # be exported for the data repo.
            c["ai"] = True
        chapters.append(c)
    if not chapters:
        stats["chapters"] = 1
        return [{"t": "正文", "o": 0}], stats, debug
    if chapters[0]["o"] > 0:
        chapters.insert(0, {"t": "正文", "o": 0})

    # Merge protection (single pass): a chapter shorter than
    # _MERGE_MIN chars is folded into the following chapter.
    merged = []
    for c in chapters:
        if merged and c["o"] - merged[-1]["o"] < _MERGE_MIN:
            merged[-1] = c
        else:
            merged.append(c)
    for a in anchors:
        stats[a.get("how") or "rule"] += 1
    stats["chapters"] = len(merged)
    if defer_ai:
        pend_n = sum(1 for c in merged if c.get("pend"))
        if pend_n:
            stats["ai_pending"] = pend_n
    return merged, stats, debug


def _split_chapters(text, fixed_n=0):
    chapters, _stats, _debug = _split_chapters_full(text, fixed_n)
    return chapters


def _persist_novel_split(hash_, text, enc, chapters, stats,
                         fixed):
    """Single persistence point for a finished split: the
    AI first-chapter mark, the split timestamp, and the
    novel_meta row. Internal per-chapter "pend" flags (from
    a deferred split) never reach storage; the pending
    count lives in split_stats."""
    for c in chapters:
        c.pop("pend", None)
    # AI split marker: when this split settled at least
    # one title via AI, the first chapter's stored title
    # carries a trailing full-width "（AI）" mark, so every
    # surface (web reader, book source, admin) shows the
    # provenance. Any stale mark is stripped first, so
    # re-splits never stack it, and a pure-rule re-split
    # makes it disappear again: the mark always reflects
    # the current split.
    if chapters:
        t0 = chapters[0].get("t") or ""
        while t0.endswith(_AI_MARK):
            t0 = t0[:-len(_AI_MARK)]
        if (stats.get("ai") or 0) > 0:
            t0 += _AI_MARK
            stats["ai_marked"] = True
        chapters[0]["t"] = t0
    stats["at"] = time.time()
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO novel_meta(hash, encoding, "
            "char_count, chapters, title_len, split_stats, "
            "updated_at) VALUES(?,?,?,?,?,?,?)",
            (hash_, enc, len(text),
             json.dumps(chapters, ensure_ascii=False), fixed,
             json.dumps(stats, ensure_ascii=False), time.time()))
    return {"encoding": enc, "char_count": len(text),
            "chapters": chapters, "title_len": fixed}


def _store_novel_meta(hash_, text, enc, defer_ai=False):
    fixed = 0
    with _db() as conn:
        row = conn.execute(
            "SELECT title_len FROM novel_meta WHERE hash = ?",
            (hash_,)).fetchone()
        if row is not None:
            fixed = row["title_len"] or 0
    if defer_ai:
        # Import path: rules only, no AI request; the split
        # itself records how many chapters AI could still
        # improve (stats["ai_pending"]) and the first read
        # of the book completes them in the background.
        chapters, stats, _dbg = _split_chapters_full(
            text, fixed, defer_ai=True)
    else:
        chapters, stats, _dbg = _split_chapters_full(
            text, fixed, ai_cfg=_ai_cfg_if_active())
    return _persist_novel_split(hash_, text, enc, chapters,
                                stats, fixed)


# --------------------------------------- deferred AI completion
# A book imported with a pending mark is completed lazily:
# the first reader (web reader opening the book, or the
# phone book source pulling its table of contents) starts a
# background single-flight pass that runs AI delimitation
# for the pending chapters only. Readers always get the
# current titles immediately; a failed pass leaves the mark
# in place so the next visit retries.
_AI_FLIGHT = {}
_AI_FLIGHT_LOCK = threading.Lock()


def _set_ai_pending(hash_, count):
    """Rewrite only the ai_pending field of a book's stored
    split_stats (0 removes the mark)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT split_stats FROM novel_meta WHERE hash = ?",
            (hash_,)).fetchone()
        if row is None:
            return
        try:
            stats = json.loads(row["split_stats"] or "")
        except Exception:
            stats = {}
        if not isinstance(stats, dict):
            stats = {}
        if count:
            stats["ai_pending"] = count
        else:
            stats.pop("ai_pending", None)
        conn.execute(
            "UPDATE novel_meta SET split_stats = ? "
            "WHERE hash = ?",
            (json.dumps(stats, ensure_ascii=False), hash_))


def _ai_trigger_run(hash_, cfg):
    try:
        file_row = get_file(hash_)
        if file_row is None:
            return
        text = _novel_text(file_row)
        meta = _novel_meta(hash_)
        fixed = (meta or {}).get("title_len") or 0
        enc = (meta or {}).get("encoding") or ""
        stored = (meta or {}).get("chapters") or []
        d_chs, _dst, _dbg = _split_chapters_full(
            text, fixed, defer_ai=True)
        # Pending chapters are matched to the stored split
        # by chapter index, not by offset: for books whose
        # source had CRLF line endings, the stored offsets
        # sit on the untranslated text while _novel_text
        # reads the cache with newline translation, so the
        # two offset bases drift apart even though the
        # chapter sequence is identical. Index mapping is
        # exact whenever the chapter counts agree; offset
        # intersection is the fallback when they do not.
        pend_idx = {i for i, c in enumerate(d_chs)
                    if c.get("pend")}
        if len(stored) == len(d_chs):
            pend_idx = {i for i in pend_idx
                        if not stored[i].get("ai")}
            pend = {d_chs[i]["o"] for i in pend_idx}
        else:
            stored_open = {c.get("o") for c in stored
                           if not c.get("ai")}
            pend = {c["o"] for c in d_chs if c.get("pend")}
            pend &= stored_open
            pend_idx = {i for i, c in enumerate(d_chs)
                        if c["o"] in pend}
        if not pend:
            _set_ai_pending(hash_, 0)
            return
        chapters, stats, _dbg = _split_chapters_full(
            text, fixed, ai_cfg=cfg, ai_only_offsets=pend)
        residual = [c for i, c in enumerate(chapters)
                    if i in pend_idx and not c.get("ai")]
        if residual:
            stats["ai_pending"] = len(residual)
        _persist_novel_split(hash_, text, enc, chapters,
                             stats, fixed)
    except Exception:
        pass
    finally:
        with _AI_FLIGHT_LOCK:
            _AI_FLIGHT.pop(hash_, None)


def _maybe_ai_trigger(hash_):
    """Start the deferred AI pass for one book when it
    carries a pending mark and AI is active. Returns True
    when a pass is running for the book (just started or
    already in flight), for the reader's analysing hint."""
    cfg = _ai_cfg_if_active()
    if not cfg:
        return False
    meta = _novel_meta(hash_)
    if not meta:
        return False
    stats = meta.get("split_stats") or {}
    if not (stats.get("ai_pending") or 0):
        return False
    with _AI_FLIGHT_LOCK:
        if hash_ in _AI_FLIGHT:
            return True
        _AI_FLIGHT[hash_] = True
    threading.Thread(target=_ai_trigger_run, args=(hash_, cfg),
                     daemon=True).start()
    return True


def _read_text_file(path):
    """The single way novel text is read back from a
    text file: Python text mode with universal-newline
    translation (CRLF / CR become LF). Every stored
    chapter offset lives on this basis, so the import /
    index paths must split the text exactly as this
    function returns it — never the raw decoded string,
    whose carriage returns would silently shift every
    offset after the first line break (the CRLF drift
    rooted out 2026-11-07)."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _index_novel(path, hash_):
    with open(path, "rb") as fh:
        raw = fh.read()
    text, enc = _decode_txt(raw)
    cache = os.path.join(NOVEL_TEXT_DIR, hash_ + ".txt")
    with open(cache, "w", encoding="utf-8") as fh:
        fh.write(text)
    return _store_novel_meta(hash_, _read_text_file(cache),
                             enc, defer_ai=True)


def _novel_meta(hash_):
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM novel_meta WHERE hash = ?",
            (hash_,)).fetchone()
    if row is None:
        return None
    try:
        stats = json.loads(row["split_stats"] or "")
    except Exception:
        stats = None
    return {"encoding": row["encoding"],
            "char_count": row["char_count"],
            "title_len": row["title_len"] or 0,
            "split_stats": stats,
            "chapters": json.loads(row["chapters"] or "[]")}


def _novel_text(file_row):
    """Decoded full text of a novel: normalized cache first,
    else decode the original bytes (staging copy, server cache
    copy, or a loopback pull of the archived object)."""
    cache = os.path.join(NOVEL_TEXT_DIR, file_row["hash"] + ".txt")
    if os.path.exists(cache):
        return _read_text_file(cache)
    raw = None
    local = _local_path(file_row)
    cached = _cache_file(file_row)
    if os.path.exists(local):
        with open(local, "rb") as fh:
            raw = fh.read()
    elif cached:
        with open(cached, "rb") as fh:
            raw = fh.read()
    elif file_row["status"] == "archived":
        url = resolve_loopback_url(file_row)
        with urllib.request.urlopen(url, timeout=120) as resp:
            raw = resp.read()
    if raw is None:
        raise ValueError("小说文件不可用。")
    text, enc = _decode_txt(raw)
    with open(cache, "w", encoding="utf-8") as fh:
        fh.write(text)
    text = _read_text_file(cache)
    if _novel_meta(file_row["hash"]) is None:
        _store_novel_meta(file_row["hash"], text, enc,
                          defer_ai=True)
    return text



# ------------------------------------------------- ext book source
# Phone reader channel (Legado-format sources, e.g. Soushu Dashi /
# Yuedu): token-guarded JSON endpoints under /ext, reached from the
# internet via Caddy at /vidlib-api/* (the panel /web gate cannot
# serve phone app clients). The token lives in credentials.json as
# ext_token; the admin page shows the full import URL because it
# sits behind the panel gate.

EXT_BASE = "https://debi.su7.dpdns.org/vidlib-api"


def _ext_token():
    tok = _cred("ext_token")
    if not tok:
        creds = _load_creds()
        tok = secrets.token_urlsafe(24)
        creds["ext_token"] = tok
        _save_creds(creds)
    return tok


def _ext_import_url():
    return EXT_BASE + "/ext/source.json?t=" + _ext_token()


def _ext_check(params):
    given = params.get("t") or ""
    return bool(given) and secrets.compare_digest(given, _ext_token())


def _ext_words(chars):
    chars = chars or 0
    if chars >= 10000:
        return "%.1f \u4e07\u5b57" % (chars / 10000.0)
    return "%d \u5b57" % chars


def _ext_book_row(r, tok):
    try:
        raw = json.loads(r["chaps"] or "[]")
    except Exception:
        raw = []
    words = _ext_words(r["chars"])
    return {
        "id": r["e_id"],
        "title": r["e_title"] or "",
        "author": "",
        "intro": "%d \u7ae0 \u00b7 %s" % (len(raw), words),
        "chapters": len(raw),
        "words": words,
        "lastChapter": (raw[-1].get("t") or "") if raw else "",
        "tocUrl": "%s/ext/toc?id=%d&t=%s" % (EXT_BASE, r["e_id"], tok),
    }


def _ext_search(params):
    key = (params.get("key") or "").strip()
    tok = _ext_token()
    where = ["f.kind = 'novel'"]
    args = []
    if key:
        where.append("(e.title LIKE ? OR e.tags LIKE ?)")
        like = "%" + key + "%"
        args += [like, like]
    with _db() as conn:
        rows = conn.execute(
            "SELECT e.id AS e_id, e.title AS e_title, "
            "m.char_count AS chars, m.chapters AS chaps "
            "FROM entries e JOIN files f ON f.hash = e.hash "
            "LEFT JOIN novel_meta m ON m.hash = e.hash WHERE "
            + " AND ".join(where)
            + " ORDER BY e.id DESC LIMIT 50", args).fetchall()
    return {"books": [_ext_book_row(r, tok) for r in rows]}


def _ext_toc(params):
    entry = get_entry(params.get("id") or "")
    if entry is None or entry["f_kind"] != "novel":
        return None
    # Pulling the table of contents counts as reading the
    # book: a pending mark starts the deferred AI pass in
    # the background; this response still serves the
    # current titles.
    _maybe_ai_trigger(entry["hash"])
    tok = _ext_token()
    meta = _novel_meta(entry["hash"]) or {}
    spans = _novel_chapter_spans(meta)
    words = _ext_words(meta.get("char_count"))
    return {
        "title": entry["title"] or "",
        "author": "",
        "intro": "%d \u7ae0 \u00b7 %s" % (len(spans), words),
        "words": words,
        "tocUrl": "%s/ext/toc?id=%d&t=%s" % (EXT_BASE, entry["id"], tok),
        "chapters": [
            {"title": c["t"],
             "url": "%s/ext/chapter?id=%d&idx=%d&t=%s"
                    % (EXT_BASE, entry["id"], i, tok)}
            for i, c in enumerate(spans)],
    }


def _ext_chapter(params):
    entry = get_entry(params.get("id") or "")
    if entry is None or entry["f_kind"] != "novel":
        return None
    meta = _novel_meta(entry["hash"]) or {}
    spans = _novel_chapter_spans(meta)
    try:
        idx = int(params.get("idx") or "0")
    except Exception:
        idx = 0
    if not spans or idx < 0 or idx >= len(spans):
        return None
    text = _novel_text(get_file(entry["hash"]))
    ch = spans[idx]
    return {"title": ch["t"], "content": text[ch["o"]:ch["e"]]}


def _ext_source():
    tok = _ext_token()
    return [{
        "bookSourceName": "\u8d44\u6e90\u5e93 \u00b7 \u6211\u7684\u5c0f\u8bf4",
        "bookSourceGroup": "\u81ea\u5efa",
        "bookSourceUrl": EXT_BASE,
        "bookSourceType": 0,
        "bookSourceComment": "\u81ea\u5efa\u8d44\u6e90\u5e93\u5c0f\u8bf4\u4e66\u6e90",
        "customOrder": 0,
        "enabled": True,
        "enabledExplore": False,
        "searchUrl": (EXT_BASE + "/ext/search?key={{key}}"
                      "&page={{page}}&t=" + tok),
        "ruleSearch": {
            "bookList": "$.books",
            "name": "$.title",
            "author": "$.author",
            "intro": "$.intro",
            "lastChapter": "$.lastChapter",
            "wordCount": "$.words",
            "bookUrl": "$.tocUrl",
        },
        "ruleBookInfo": {
            "name": "$.title",
            "author": "$.author",
            "intro": "$.intro",
            "wordCount": "$.words",
            "tocUrl": "$.tocUrl",
        },
        "ruleToc": {
            "chapterList": "$.chapters",
            "chapterName": "$.title",
            "chapterUrl": "$.url",
        },
        "ruleContent": {
            "content": "$.content",
        },
    }]


def ext_handle(environ, start_response, params):
    if not _ext_check(params):
        return _json(start_response, "404 Not Found", {"ok": False})
    path = environ.get("PATH_INFO", "") or ""
    try:
        if path == "/ext/search":
            return _json(start_response, "200 OK", _ext_search(params))
        if path == "/ext/toc":
            data = _ext_toc(params)
            if data is None:
                return _json(start_response, "404 Not Found",
                             {"ok": False})
            return _json(start_response, "200 OK", data)
        if path == "/ext/chapter":
            data = _ext_chapter(params)
            if data is None:
                return _json(start_response, "404 Not Found",
                             {"ok": False})
            return _json(start_response, "200 OK", data)
        if path == "/ext/source.json":
            return _json(start_response, "200 OK", _ext_source())
    except Exception:
        return _json(start_response, "503 Service Unavailable",
                     {"ok": False})
    return _json(start_response, "404 Not Found", {"ok": False})


# ------------------------------------------------------- subsonic
# Phone music clients (Symfonium and friends) speak the Subsonic
# API. This is a browse / search / star subset over the single
# music library, exposed at /rest/* and reached from the internet
# via Caddy at /vidlib-api/subsonic/* (the panel /web gate cannot
# serve phone app clients). It carries the protocol's own
# credential from credentials.json (subsonic_user / subsonic_pass,
# import-only, same class as the book-source token and the WebDAV
# password). Until both are set, every request fails auth.
#
# Mark mapping (shared with the /music page tri-state marks):
#   star            -> like    (entries.mark = 1)
#   unstar          -> clear like only
#   setRating 1     -> dislike (entries.mark = 2)
#   setRating 0     -> clear dislike only
#   setRating 2..5  -> like    (entries.mark = 1)
# In song payloads a liked song reports starred=<mark time> and
# userRating 5; a disliked song reports userRating 1.
#
# ID scheme (defined here, nowhere else):
#   song   "s"  + entries.id            (e.g. s123)
#   artist "ar" + urlsafe_b64(artist)   (raw artist field, "" ok)
#   album  "al" + urlsafe_b64(artist + "\x00" + album)
#   root directory id is "root".

SUBSONIC_API_VERSION = "1.16.1"
SUBSONIC_PUBLIC_BASE = "https://debi.su7.dpdns.org/vidlib-api/subsonic"


def _sub_b64enc(text):
    return base64.urlsafe_b64encode(
        (text or "").encode("utf-8")).decode("ascii").rstrip("=")


def _sub_b64dec(text):
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(
            (text + pad).encode("ascii")).decode("utf-8")
    except Exception:
        return None


def _sub_song_id(entry_id):
    return "s%d" % int(entry_id)


def _sub_artist_id(artist):
    return "ar" + _sub_b64enc(artist or "")


def _sub_album_id(artist, album):
    return "al" + _sub_b64enc((artist or "") + "\x00" + (album or ""))


def _sub_parse_id(raw):
    """-> (kind, value): song value is an int entry id; artist
    value is the raw artist name; album value is (artist, album)."""
    raw = (raw or "").strip()
    if raw in ("root", "0", ""):
        return ("root", None)
    if raw.startswith("s") and raw[1:].isdigit():
        return ("song", int(raw[1:]))
    if raw.startswith("ar"):
        name = _sub_b64dec(raw[2:])
        if name is not None:
            return ("artist", name)
    if raw.startswith("al"):
        joined = _sub_b64dec(raw[2:])
        if joined is not None and "\x00" in joined:
            artist, album = joined.split("\x00", 1)
            return ("album", (artist, album))
    if raw.isdigit():
        return ("song", int(raw))
    return (None, None)


def _sub_iso(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _sub_artist_name(raw):
    return raw or "未知歌手"


def _sub_album_name(raw):
    return raw or "未知专辑"


def _sub_creds():
    creds = _load_creds()
    return (creds.get("subsonic_user") or "",
            creds.get("subsonic_pass") or "")


def _sub_auth(params):
    """True when the request carries a valid Subsonic credential.
    Supports u+p (plain or enc:hex) and u+t+s (md5(pass+salt))."""
    user, password = _sub_creds()
    if not user or not password:
        return False
    given_user = params.get("u") or ""
    if not given_user or not secrets.compare_digest(given_user, user):
        return False
    token = params.get("t") or ""
    salt = params.get("s") or ""
    if token and salt:
        expect = hashlib.md5(
            (password + salt).encode("utf-8")).hexdigest()
        return secrets.compare_digest(token.lower(), expect)
    p = params.get("p") or ""
    if p:
        if p.startswith("enc:"):
            try:
                p = bytes.fromhex(p[4:]).decode("utf-8")
            except Exception:
                return False
        return secrets.compare_digest(p, password)
    return False


def _sub_envelope(payload=None, error=None):
    resp = {"status": "ok" if error is None else "failed",
            "version": SUBSONIC_API_VERSION,
            "type": "vidlib",
            "serverVersion": "1.0.0",
            "openSubsonic": True}
    if error is not None:
        resp["error"] = {"code": error[0], "message": error[1]}
    if payload:
        resp.update(payload)
    return {"subsonic-response": resp}


def _sub_ok(start_response, payload=None):
    return _json(start_response, "200 OK", _sub_envelope(payload))


def _sub_fail(start_response, code, message, http="200 OK"):
    return _json(start_response, http,
                 _sub_envelope(error=(code, message)))


_SUB_MUSIC_COLS = (
    "SELECT e.id AS id, e.title AS title, e.tags AS tags, "
    "e.mark AS mark, e.mark_at AS mark_at, "
    "e.play_count AS play_count, e.created_at AS created_at, "
    "e.hash AS hash, f.ext AS ext, f.size AS size, "
    "f.duration AS duration, f.artist AS artist, "
    "f.album AS album, f.bit_rate AS bit_rate, "
    "f.status AS status "
    "FROM entries e JOIN files f ON f.hash = e.hash "
    "WHERE f.kind = 'music'")


def _sub_songs(where="", args=(), order="e.id DESC", limit=None):
    sql = _SUB_MUSIC_COLS
    if where:
        sql += " AND (" + where + ")"
    sql += " ORDER BY " + order
    if limit:
        sql += " LIMIT %d OFFSET %d" % (int(limit[0]), int(limit[1]))
    with _db() as conn:
        return conn.execute(sql, tuple(args)).fetchall()


def _sub_song_obj(r):
    artist = r["artist"] or ""
    album = r["album"] or ""
    obj = {
        "id": _sub_song_id(r["id"]),
        "parent": _sub_album_id(artist, album),
        "title": r["title"] or "",
        "album": _sub_album_name(album),
        "artist": _sub_artist_name(artist),
        "albumId": _sub_album_id(artist, album),
        "artistId": _sub_artist_id(artist),
        "isDir": False,
        "coverArt": _sub_song_id(r["id"]),
        "duration": int(r["duration"] or 0),
        "size": int(r["size"] or 0),
        "suffix": (r["ext"] or "").lower(),
        "contentType": _mime_for(r["ext"] or ""),
        "path": (r["title"] or "") + (
            "." + r["ext"] if r["ext"] else ""),
        "playCount": int(r["play_count"] or 0),
    }
    if r["bit_rate"]:
        obj["bitRate"] = int(round(r["bit_rate"] / 1000.0))
    created = _sub_iso(r["created_at"])
    if created:
        obj["created"] = created
    if r["mark"] == 1:
        starred = _sub_iso(r["mark_at"])
        if starred:
            obj["starred"] = starred
        obj["userRating"] = 5
    elif r["mark"] == 2:
        obj["userRating"] = 1
    return obj


def _sub_artist_rows():
    with _db() as conn:
        return conn.execute(
            "SELECT COALESCE(f.artist, '') AS artist, "
            "COUNT(*) AS n FROM entries e "
            "JOIN files f ON f.hash = e.hash "
            "WHERE f.kind = 'music' GROUP BY COALESCE(f.artist, '')"
        ).fetchall()


def _sub_album_rows():
    with _db() as conn:
        return conn.execute(
            "SELECT COALESCE(f.artist, '') AS artist, "
            "COALESCE(f.album, '') AS album, COUNT(*) AS n, "
            "COALESCE(SUM(f.duration), 0) AS dur, "
            "MAX(e.created_at) AS created FROM entries e "
            "JOIN files f ON f.hash = e.hash "
            "WHERE f.kind = 'music' "
            "GROUP BY COALESCE(f.artist, ''), COALESCE(f.album, '')"
        ).fetchall()


def _sub_artist_obj(artist):
    albums = [r for r in _sub_album_rows() if r["artist"] == artist]
    return {"id": _sub_artist_id(artist),
            "name": _sub_artist_name(artist),
            "albumCount": len(albums),
            "coverArt": _sub_artist_id(artist)}


def _sub_album_obj(artist, album, count, duration, created):
    return {"id": _sub_album_id(artist, album),
            "name": _sub_album_name(album),
            "artist": _sub_artist_name(artist),
            "artistId": _sub_artist_id(artist),
            "coverArt": _sub_album_id(artist, album),
            "songCount": int(count),
            "duration": int(duration or 0),
            "created": _sub_iso(created) or "1970-01-01T00:00:00Z"}


def _sub_index_groups():
    """Artists grouped by first letter of display name."""
    groups = {}
    for r in _sub_artist_rows():
        name = _sub_artist_name(r["artist"])
        letter = name[0].upper() if name else "#"
        if not letter.isalpha():
            letter = "#"
        groups.setdefault(letter, []).append(r["artist"])
    return [{"name": letter,
             "artist": [_sub_artist_obj(a)
                        for a in sorted(groups[letter],
                                        key=_sub_artist_name)]}
            for letter in sorted(groups)]


def _sub_dir_child_artist(artist):
    return {"id": _sub_artist_id(artist),
            "parent": "root",
            "title": _sub_artist_name(artist),
            "artist": _sub_artist_name(artist),
            "isDir": True,
            "coverArt": _sub_artist_id(artist)}


def _sub_dir_child_album(artist, album):
    return {"id": _sub_album_id(artist, album),
            "parent": _sub_artist_id(artist),
            "title": _sub_album_name(album),
            "album": _sub_album_name(album),
            "artist": _sub_artist_name(artist),
            "isDir": True,
            "coverArt": _sub_album_id(artist, album)}


def _sub_get_music_directory(params):
    kind, val = _sub_parse_id(params.get("id") or "")
    if kind == "root":
        children = [_sub_dir_child_artist(r["artist"])
                    for r in sorted(
                        _sub_artist_rows(),
                        key=lambda r: _sub_artist_name(r["artist"]))]
        return {"directory": {"id": "root", "name": "音乐",
                              "child": children}}
    if kind == "artist":
        children = []
        for r in _sub_album_rows():
            if r["artist"] == val and r["album"] != "":
                children.append(_sub_dir_child_album(val, r["album"]))
        children.sort(key=lambda c: c["title"])
        loose = _sub_songs(
            "COALESCE(f.artist, '') = ? AND COALESCE(f.album, '') = ''",
            (val,), order="e.title ASC")
        children += [_sub_song_obj(r) for r in loose]
        return {"directory": {"id": _sub_artist_id(val),
                              "parent": "root",
                              "name": _sub_artist_name(val),
                              "child": children}}
    if kind == "album":
        artist, album = val
        rows = _sub_songs(
            "COALESCE(f.artist, '') = ? AND COALESCE(f.album, '') = ?",
            (artist, album), order="e.title ASC")
        return {"directory": {"id": _sub_album_id(artist, album),
                              "parent": _sub_artist_id(artist),
                              "name": _sub_album_name(album),
                              "child": [_sub_song_obj(r) for r in rows]}}
    return None


def _sub_get_album(params):
    kind, val = _sub_parse_id(params.get("id") or "")
    if kind != "album":
        return None
    artist, album = val
    rows = _sub_songs(
        "COALESCE(f.artist, '') = ? AND COALESCE(f.album, '') = ?",
        (artist, album), order="e.title ASC")
    if not rows:
        return None
    obj = _sub_album_obj(artist, album, len(rows),
                         sum(r["duration"] or 0 for r in rows),
                         max(r["created_at"] or 0 for r in rows))
    obj["song"] = [_sub_song_obj(r) for r in rows]
    return {"album": obj}


def _sub_album_list(params):
    list_type = (params.get("type") or "recent").strip()
    try:
        size = max(1, int(params.get("size") or 10))
    except Exception:
        size = 10
    try:
        offset = max(0, int(params.get("offset") or 0))
    except Exception:
        offset = 0
    rows = list(_sub_album_rows())
    if list_type == "random":
        random.shuffle(rows)
    elif list_type == "alphabeticalByName":
        rows.sort(key=lambda r: (_sub_album_name(r["album"]),
                                 _sub_artist_name(r["artist"])))
    elif list_type == "alphabeticalByArtist":
        rows.sort(key=lambda r: (_sub_artist_name(r["artist"]),
                                 _sub_album_name(r["album"])))
    else:  # recent / newest / byYear fallback
        rows.sort(key=lambda r: -(r["created"] or 0))
    albums = [_sub_album_obj(r["artist"], r["album"], r["n"],
                             r["dur"], r["created"])
              for r in rows[offset:offset + size]]
    return albums


def _sub_search(params):
    q = (params.get("query") or "").strip()

    def _count(key, default=20):
        try:
            return max(0, int(params.get(key) or default))
        except Exception:
            return default

    result = {}
    if not q:
        return result
    like = "%" + q + "%"
    songs = _sub_songs(
        "(e.title LIKE ? OR f.artist LIKE ? OR f.album LIKE ? "
        "OR e.tags LIKE ?)", (like, like, like, like),
        order="e.id DESC",
        limit=(_count("songCount"), _count("songOffset", 0)))
    if songs:
        result["song"] = [_sub_song_obj(r) for r in songs]
    needle = q.lower()
    artists = [r["artist"] for r in _sub_artist_rows()
               if needle in _sub_artist_name(r["artist"]).lower()]
    artists.sort(key=_sub_artist_name)
    off = _count("artistOffset", 0)
    artists = artists[off:off + _count("artistCount")]
    if artists:
        result["artist"] = [_sub_artist_obj(a) for a in artists]
    albums = [r for r in _sub_album_rows()
              if needle in _sub_album_name(r["album"]).lower()
              or needle in _sub_artist_name(r["artist"]).lower()]
    albums.sort(key=lambda r: (_sub_album_name(r["album"]),
                               _sub_artist_name(r["artist"])))
    off = _count("albumOffset", 0)
    albums = albums[off:off + _count("albumCount")]
    if albums:
        result["album"] = [
            _sub_album_obj(r["artist"], r["album"], r["n"],
                           r["dur"], r["created"]) for r in albums]
    return result


def _sub_apply_mark(entry_id, mark):
    """Mirror of the /api/mark write semantics for one entry."""
    entry = get_entry(entry_id)
    if entry is None or entry["f_kind"] != "music":
        return False
    if int(entry["mark"] or 0) == mark:
        return True
    now = time.time()
    with _db() as conn:
        if mark == 2:
            conn.execute(
                "UPDATE entries SET mark = 2, mark_at = ?, "
                "seen_at = CASE WHEN seen_at IS NULL OR "
                "seen_at = 0 THEN ? ELSE seen_at END, "
                "updated_at = ? WHERE id = ?",
                (now, now, now, entry_id))
        else:
            conn.execute(
                "UPDATE entries SET mark = ?, mark_at = ?, "
                "updated_at = ? WHERE id = ?",
                (mark, now if mark else 0, now, entry_id))
    return True


def _sub_target_song_ids(params):
    ids = set()
    raws = []
    for key in ("id", "albumId", "artistId"):
        v = params.get(key)
        if v:
            raws += [x for x in v.split(",") if x]
    for raw in raws:
        kind, val = _sub_parse_id(raw)
        if kind == "song":
            ids.add(val)
        elif kind == "artist":
            rows = _sub_songs("COALESCE(f.artist, '') = ?", (val,),
                              order="e.id ASC")
            ids.update(r["id"] for r in rows)
        elif kind == "album":
            rows = _sub_songs(
                "COALESCE(f.artist, '') = ? "
                "AND COALESCE(f.album, '') = ?", val,
                order="e.id ASC")
            ids.update(r["id"] for r in rows)
    return sorted(ids)


def _sub_mark_action(params, action):
    ids = _sub_target_song_ids(params)
    if not ids:
        return False
    changed = False
    for entry_id in ids:
        entry = get_entry(entry_id)
        if entry is None or entry["f_kind"] != "music":
            continue
        cur = int(entry["mark"] or 0)
        if action == "star":
            new = 1
        elif action == "unstar":
            new = 0 if cur == 1 else cur
        else:  # setRating
            try:
                rating = int(params.get("rating") or 0)
            except Exception:
                rating = 0
            if rating == 1:
                new = 2
            elif rating == 0:
                new = 0 if cur == 2 else cur
            else:
                new = 1
        if new != cur:
            _sub_apply_mark(entry_id, new)
            changed = True
    if changed:
        _sync_mark_files()
    return True


def _sub_cover_hash(params):
    kind, val = _sub_parse_id(params.get("id") or "")
    rows = []
    if kind == "song":
        entry = get_entry(val)
        if entry is not None and entry["f_kind"] == "music":
            return entry["hash"]
        return None
    if kind == "artist":
        rows = _sub_songs("COALESCE(f.artist, '') = ?", (val,),
                          order="e.id ASC", limit=(1, 0))
    elif kind == "album":
        rows = _sub_songs(
            "COALESCE(f.artist, '') = ? AND COALESCE(f.album, '') = ?",
            val, order="e.id ASC", limit=(1, 0))
    if rows:
        return rows[0]["hash"]
    return None


def _sub_stream(environ, start_response, params):
    kind, val = _sub_parse_id(params.get("id") or "")
    if kind != "song":
        return _sub_fail(start_response, 70, "Song not found.")
    entry = get_entry(val)
    if entry is None or entry["f_kind"] != "music":
        return _sub_fail(start_response, 70, "Song not found.")
    with _db() as conn:
        conn.execute("UPDATE entries SET play_count = play_count + 1 "
                     "WHERE id = ?", (entry["id"],))
        conn.execute(
            "UPDATE entries SET seen_at = ? WHERE id = ? "
            "AND (seen_at IS NULL OR seen_at = 0)",
            (time.time(), entry["id"]))
    file_row = get_file(entry["hash"])
    if entry["f_status"] == "archived":
        url = ""
        try:
            url = resolve_play_url(file_row)
        except Exception:
            url = ""
        if url:
            start_response("302 Found",
                           [("Location", url), ("Content-Length", "0")])
            return [b""]
        return _dav_stream_archived(environ, start_response, file_row)
    local = _local_path(file_row)
    if not os.path.exists(local):
        return _sub_fail(start_response, 70, "Song not found.")
    return _serve_file_range(environ, start_response, local,
                             _mime_for(file_row["ext"]))


def subsonic_handle(environ, start_response):
    params = {}
    for k, v in urllib.parse.parse_qs(
            environ.get("QUERY_STRING", "")).items():
        params[k] = v[0]
    if environ.get("REQUEST_METHOD") == "POST":
        ctype = environ.get("CONTENT_TYPE") or ""
        if "application/x-www-form-urlencoded" in ctype:
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except Exception:
                length = 0
            length = max(0, min(length, 65536))
            if length:
                body = environ["wsgi.input"].read(length)
                for k, v in urllib.parse.parse_qs(
                        body.decode("utf-8", "replace")).items():
                    params[k] = v[0]
    path = environ.get("PATH_INFO", "") or ""
    name = path[len("/rest"):].strip("/")
    for suffix in (".view", ".json"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    if not _sub_auth(params):
        return _sub_fail(start_response, 40,
                         "Wrong username or password.",
                         http="401 Unauthorized")
    try:
        if name in ("ping", ""):
            return _sub_ok(start_response)
        if name == "getLicense":
            return _sub_ok(start_response, {"license": {
                "valid": True, "email": "vidlib@local",
                "licenseExpires": "2099-01-01T00:00:00Z",
                "trialExpires": "2099-01-01T00:00:00Z"}})
        if name == "getMusicFolders":
            return _sub_ok(start_response, {"musicFolders": {
                "musicFolder": [{"id": 1, "name": "音乐"}]}})
        if name == "getMusicDirectory":
            data = _sub_get_music_directory(params)
            if data is None:
                return _sub_fail(start_response, 70, "Not found.")
            return _sub_ok(start_response, data)
        if name == "getIndexes":
            return _sub_ok(start_response, {"indexes": {
                "shortcut": [], "index": _sub_index_groups(),
                "child": []}})
        if name == "getArtists":
            return _sub_ok(start_response, {"artists": {
                "ignoredArticles": "",
                "index": _sub_index_groups()}})
        if name == "getArtist":
            kind, val = _sub_parse_id(params.get("id") or "")
            if kind != "artist":
                return _sub_fail(start_response, 70, "Not found.")
            obj = _sub_artist_obj(val)
            obj["album"] = [
                _sub_album_obj(r["artist"], r["album"], r["n"],
                               r["dur"], r["created"])
                for r in _sub_album_rows() if r["artist"] == val]
            return _sub_ok(start_response, {"artist": obj})
        if name == "getAlbum":
            data = _sub_get_album(params)
            if data is None:
                return _sub_fail(start_response, 70, "Not found.")
            return _sub_ok(start_response, data)
        if name == "getAlbumList":
            return _sub_ok(start_response, {
                "albumList": {"album": _sub_album_list(params)}})
        if name == "getAlbumList2":
            return _sub_ok(start_response, {
                "albumList2": {"album": _sub_album_list(params)}})
        if name == "search2":
            return _sub_ok(start_response, {
                "searchResult2": _sub_search(params)})
        if name == "search3":
            return _sub_ok(start_response, {
                "searchResult3": _sub_search(params)})
        if name == "getSong":
            kind, val = _sub_parse_id(params.get("id") or "")
            rows = []
            if kind == "song":
                rows = _sub_songs("e.id = ?", (val,), limit=(1, 0))
            if not rows:
                return _sub_fail(start_response, 70, "Song not found.")
            return _sub_ok(start_response,
                           {"song": _sub_song_obj(rows[0])})
        if name in ("getStarred", "getStarred2"):
            rows = _sub_songs("e.mark = 1", order="e.mark_at DESC")
            key = "starred" if name == "getStarred" else "starred2"
            payload = {}
            if rows:
                payload["song"] = [_sub_song_obj(r) for r in rows]
            return _sub_ok(start_response, {key: payload})
        if name == "star":
            _sub_mark_action(params, "star")
            return _sub_ok(start_response)
        if name == "unstar":
            _sub_mark_action(params, "unstar")
            return _sub_ok(start_response)
        if name == "setRating":
            _sub_mark_action(params, "setRating")
            return _sub_ok(start_response)
        if name == "getCoverArt":
            hash_ = _sub_cover_hash(params)
            if hash_ is None:
                return _sub_fail(start_response, 70, "Not found.")
            thumb = os.path.join(THUMB_DIR, hash_ + ".jpg")
            if os.path.exists(thumb):
                with open(thumb, "rb") as fh:
                    data = fh.read()
                start_response("200 OK", [
                    ("Content-Type", "image/jpeg"),
                    ("Content-Length", str(len(data))),
                    ("Cache-Control", "public, max-age=86400")])
                return [data]
            start_response("200 OK", [
                ("Content-Type", "image/svg+xml"),
                ("Content-Length", str(len(_PLACEHOLDER_SVG)))])
            return [_PLACEHOLDER_SVG]
        if name in ("stream", "download"):
            return _sub_stream(environ, start_response, params)
        if name == "scrobble":
            return _sub_ok(start_response)
        if name == "getUser":
            return _sub_ok(start_response, {"user": {
                "username": params.get("u") or "",
                "scrobblingEnabled": False, "adminRole": False,
                "settingsRole": False, "downloadRole": True,
                "uploadRole": False, "playlistRole": False,
                "coverArtRole": True, "commentRole": False,
                "podcastRole": False, "streamRole": True,
                "jukeboxRole": False, "shareRole": False,
                "videoConversionRole": False, "folder": [1]}})
        if name == "getPlaylists":
            return _sub_ok(start_response,
                           {"playlists": {"playlist": []}})
        if name in ("getScanStatus", "startScan"):
            with _db() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM entries e "
                    "JOIN files f ON f.hash = e.hash "
                    "WHERE f.kind = 'music'").fetchone()["n"]
            return _sub_ok(start_response, {"scanStatus": {
                "scanning": False, "count": int(n)}})
        if name == "getRandomSongs":
            try:
                size = max(1, int(params.get("size") or 10))
            except Exception:
                size = 10
            rows = _sub_songs(order="RANDOM()", limit=(size, 0))
            return _sub_ok(start_response, {"randomSongs": {
                "song": [_sub_song_obj(r) for r in rows]}})
    except Exception:
        return _sub_fail(start_response, 0, "Internal error.")
    return _sub_fail(start_response, 0, "Unknown endpoint.")


def _subsonic_admin_panel():
    user, password = _sub_creds()
    if user and password:
        state = "已设置（账号：%s）" % _esc(user)
    else:
        state = "尚未设置，接口会拒绝所有请求"
    body = (
        '<div class="panel"><h2>手机播放（Subsonic）</h2>'
        '<div class="kv">Symfonium 等 Subsonic 客户端从这里接入'
        '音乐库。服务器地址：<b>' + SUBSONIC_PUBLIC_BASE + '</b>'
        '（IPv6 不通时把域名换成 debi4.su7.dpdns.org）。'
        '账号状态：<b>' + state + '</b>。用户名、密码只写不回显，'
        '留空的字段保持不变，密码至少 8 位。</div>'
        '<p>用户名 <input id="ss-user" type="text" autocomplete="off" '
        'style="width:150px"> 新密码 '
        '<input id="ss-pass" type="password" '
        'autocomplete="new-password" style="width:150px"> '
        '<button class="btn" id="ss-save" type="button">保存</button> '
        '<span id="ss-msg" style="color:#8b94a7"></span></p>'
        '<p style="color:#8b94a7;font-size:13px;line-height:1.7">'
        '标记口径（与音乐页共用同一套三态标记，双向同步）：'
        '星标 = 喜欢；评分 1 = 不喜欢；评分 0 = 清除不喜欢；'
        '评分 2-5 = 喜欢；取消星标 = 清除喜欢。'
        '歌曲在手机端显示为：喜欢 = 已星标 + 评分 5；'
        '不喜欢 = 评分 1。</p>'
        '<script>'
        'async function ssPost(u,b){const r=await fetch(u,'
        '{method:"POST",headers:{"Content-Type":"application/json"},'
        'body:JSON.stringify(b)});return r.json();}'
        'document.getElementById("ss-save").addEventListener('
        '"click",async function(){'
        'var u=document.getElementById("ss-user").value.trim();'
        'var p=document.getElementById("ss-pass").value;'
        'var d=await ssPost("api/admin/subsonic",'
        '{user:u,password:p});'
        'document.getElementById("ss-msg").textContent='
        'd.ok?"已保存，刷新页面查看状态。":(d.message||"保存失败");'
        '});'
        '</script></div>')
    return body


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


def _fmt_wchars(n):
    n = int(n or 0)
    if n >= 10000:
        return "%.1fw字" % (n / 10000.0)
    return "%d字" % n


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
            "f.gpath AS f_gpath, f.error AS f_error, "
            "f.kind AS f_kind, f.category AS f_category, "
            "f.artist AS f_artist, f.album AS f_album, "
            "f.bit_rate AS f_bit_rate "
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


def insert_entry(hash_, title, folder, tags="", source="",
                 library_id=None):
    now = time.time()
    if library_id is None:
        f = get_file(hash_)
        library_id = _resolve_library_id(
            (f["kind"] if f else "video") or "video", None)
    with _db() as conn:
        title = _unique_title(conn, folder, title[:200])
        cur = conn.execute(
            "INSERT INTO entries(hash, title, folder, tags, source, "
            "library_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (hash_, title, folder, (tags or "")[:200],
             (source or "")[:120], library_id, now, now))
        return cur.lastrowid


def _mark_filename(row):
    name = row["title"] or row["hash"]
    if row["ext"]:
        name += "." + row["ext"]
    return name


def _sync_mark_files():
    """Rewrite likes.txt / dislikes.txt in DATA_DIR from the DB:
    one line per marked entry, "<hash>  <original filename>",
    in the order the marks were made.  Best-effort export; the
    entries table stays the source of truth."""
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT e.hash AS hash, e.title AS title, "
                "f.ext AS ext, e.mark AS mark FROM entries e "
                "JOIN files f ON f.hash = e.hash "
                "WHERE e.mark IN (1, 2) "
                "ORDER BY e.mark_at ASC, e.id ASC").fetchall()
        for mark, fname in ((1, "likes.txt"), (2, "dislikes.txt")):
            lines = ["%s  %s" % (r["hash"], _mark_filename(r))
                     for r in rows if r["mark"] == mark]
            path = os.path.join(DATA_DIR, fname)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("".join(line + "\n" for line in lines))
            os.replace(tmp, path)
    except Exception:
        pass


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
    _sync_mark_files()
    return True


def _local_path(file_row):
    return os.path.join(STAGING_DIR,
                        file_row["hash"] + "." + (file_row["ext"] or "mp4"))


_warm_lock = threading.Lock()
_warm_queue = queue.Queue()
_warm_pending = set()
_warm_started = False


def _cache_path(file_row):
    return os.path.join(
        CACHE_DIR,
        file_row["hash"] + "." + (file_row["ext"] or "mp4"))


def _cache_file(file_row):
    p = _cache_path(file_row)
    return p if os.path.exists(p) else None


def _cache_enforce():
    """Threshold maintenance: while the cache is UNDER the budget
    nothing is touched at all. Only when over budget, evict LRU
    (oldest mtime first) in one batch down to 90% of the budget,
    so the cache does not hover at the line being nudged file by
    file."""
    try:
        files, total = [], 0
        for name in os.listdir(CACHE_DIR):
            if name.endswith(".tmp"):
                continue
            p = os.path.join(CACHE_DIR, name)
            try:
                st = os.stat(p)
            except Exception:
                continue
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if total <= CACHE_BUDGET_BYTES:
            return
        target = CACHE_BUDGET_BYTES * 9 // 10
        files.sort()
        for _mt, size, p in files:
            if total <= target:
                break
            try:
                os.unlink(p)
                total -= size
            except Exception:
                pass
    except Exception:
        pass


def _cache_keep_local(file_row, local):
    """After archiving, keep a short video's local copy in the
    cache instead of deleting it: the feed's fast path."""
    try:
        if ((file_row["kind"] if "kind" in file_row.keys() else "video")
                != "video"):
            return False
        if (int(file_row["size"] or 0) <= SHORT_MAX_BYTES
                and os.path.exists(local)):
            os.replace(local, _cache_path(file_row))
            _cache_enforce()
            return True
    except Exception:
        pass
    return False


def _cache_download(file_row):
    tmp = _cache_path(file_row) + ".tmp"
    try:
        url = resolve_loopback_url(file_row)
        with urllib.request.urlopen(url, timeout=300) as resp, \
                open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh, 262144)
        if os.path.getsize(tmp) == int(file_row["size"] or 0):
            os.replace(tmp, _cache_path(file_row))
        else:
            os.unlink(tmp)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _warm_loop():
    """One background worker draining the warm queue sequentially
    (batched updates: no parallel Drive pulls competing with
    playback). Cache maintenance runs once per drained batch."""
    while True:
        file_row = _warm_queue.get()
        with _warm_lock:
            _warm_pending.discard(file_row["hash"])
        try:
            if not _cache_file(file_row):
                _cache_download(file_row)
        except Exception:
            pass
        if _warm_queue.empty():
            _cache_enforce()


def _cache_warm(file_row):
    """Queue an archived short video for background caching.
    Callers only enqueue; the single worker downloads in order."""
    global _warm_started
    if file_row["status"] != "archived":
        return
    if int(file_row["size"] or 0) > SHORT_MAX_BYTES:
        return
    if _cache_file(file_row):
        return
    h = file_row["hash"]
    with _warm_lock:
        if h in _warm_pending:
            return
        _warm_pending.add(h)
        start = not _warm_started
        _warm_started = True
    _warm_queue.put({"hash": h, "ext": file_row["ext"],
                     "size": file_row["size"],
                     "gpath": file_row["gpath"],
                     "status": "archived"})
    if start:
        threading.Thread(target=_warm_loop, daemon=True).start()


def ingest_staged_file(staged_path, display_name, folder=None, tags="",
                       source="", library_id=None):
    """Hash a fully-received staging file, dedupe, and register it.

    Returns (entry_id, file_hash, is_duplicate_file). The staging
    file is consumed: deleted for duplicates, renamed to its hash
    name for new files (until the worker archives it)."""
    if os.path.getsize(staged_path) == 0:
        os.unlink(staged_path)
        raise ValueError("空文件（0 字节），已忽略。")
    digest = _file_digest(staged_path)
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
        entry_id = insert_entry(
            digest, title, folder, tags, source,
            library_id=_resolve_library_id(
                (existing["kind"] if "kind" in existing.keys()
                 else None) or _kind(existing["ext"]), library_id))
        return entry_id, digest, True
    if not ext:
        ext = "mp4"
    kind = _kind(ext)
    final = os.path.join(STAGING_DIR, digest + "." + ext)
    os.replace(staged_path, final)
    meta = {"duration": 0, "width": 0, "height": 0}
    category = ""
    audio = {"duration": 0.0, "bit_rate": 0, "title": "",
             "artist": "", "album": ""}
    if kind == "video":
        meta = _probe(final)
        _probe_cache[digest] = meta
        _make_thumb(final, digest)
    elif kind == "image":
        meta = _probe(final)
        _probe_cache[digest] = meta
        _make_image_thumb(final, digest)
    elif kind == "novel":
        try:
            _index_novel(final, digest)
        except Exception:
            pass
    elif kind == "file":
        category = _category_for_ext(ext)
    elif kind == "music":
        audio = _probe_audio(final)
        meta = {"duration": audio.get("duration") or 0,
                "width": 0, "height": 0}
        _make_cover_thumb(final, digest)
    size = os.path.getsize(final)
    with _db() as conn:
        conn.execute(
            "INSERT INTO files(hash, ext, size, duration, width, "
            "height, status, kind, category, artist, album, "
            "bit_rate, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,'local',?,?,?,?,?,?,?)",
            (digest, ext, size, meta.get("duration") or 0,
             meta.get("width") or 0, meta.get("height") or 0,
             kind, category, audio.get("artist") or "",
             audio.get("album") or "",
             audio.get("bit_rate") or 0, now, now))
    if kind == "music" and audio.get("title"):
        title = audio["title"]
    entry_id = insert_entry(
        digest, title, folder, tags, source,
        library_id=_resolve_library_id(kind, library_id))
    _ensure_worker()
    return entry_id, digest, False


# ------------------------------------------------------- web uploads

_uploads = {}
_uploads_lock = threading.Lock()
UPLOAD_CHUNK = 8 * 1024 * 1024


def _merge_chute(tags, relpath):
    """Append the folder-path tag derived from a relative path
    (the DAV chute convention), if any."""
    chute = ""
    if relpath:
        segs = [seg for seg in str(relpath).replace("\\", "/")
                .split("/") if seg][:-1]
        chute = "/".join(segs)[:120]
    if chute:
        tags = ((tags + ",") if tags else "") + chute
    return tags


def register_dup(digest, name, tags, source, library_id=None):
    """Create an entry for an already-stored file without any
    upload (the client-side precheck's fast path). Mirrors the
    duplicate branch of ingest_staged_file."""
    if get_file(digest) is None:
        return None
    title = os.path.splitext(name or "")[0] or digest[:8]
    now = time.time()
    with _db() as conn:
        conn.execute(
            "UPDATE files SET dup_count = dup_count + 1, "
            "orphaned_at = 0, updated_at = ? WHERE hash = ?",
            (now, digest))
    f = get_file(digest)
    return insert_entry(
        digest, title, _folder_of(now), tags, source,
        library_id=_resolve_library_id(
            (f["kind"] if f else "video") or "video", library_id))


def upload_init(name, size, title, tags, source, relpath="",
                library_id=None):
    tags = _merge_chute(tags, relpath)
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
                         "got": set(), "library_id": library_id}
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
        part, sess["name"], tags=sess["tags"], source=sess["source"],
        library_id=sess.get("library_id"))
    return {"entry_id": entry_id, "hash": digest, "duplicate": dup}


# ------------------------------------------------------------- worker

# ------------------------------------------- Drive direct verify
# Verifying an upload through AList means listing the whole
# /videos folder (1500+ objects and growing) once per check -
# several listings per archived file, each costing many seconds,
# and the listing still lags a just-finished upload, so files
# were re-uploaded or written off as unconfirmable. The Drive
# API answers the same question with one targeted name query,
# so verification goes straight there. AList keeps its link-
# source role (uploads and signed URLs) untouched.

_DRIVE = {"token": "", "at": 0.0}
_DRIVE_LOCK = threading.Lock()


class _DriveError(Exception):
    pass


def _drive_token():
    with _DRIVE_LOCK:
        if _DRIVE["token"] and time.time() - _DRIVE["at"] < 3000:
            return _DRIVE["token"]
        refresh = _cred("google_refresh_token")
        client_id = _cred("google_client_id")
        client_secret = _cred("google_client_secret")
        if not (refresh and client_id and client_secret):
            raise _DriveError("谷歌凭据未配置完整。")
        body = urllib.parse.urlencode({
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token"}).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=body,
            headers={"Content-Type":
                     "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8",
                                                     "replace"))
        except Exception as exc:
            raise _DriveError("谷歌令牌刷新失败：%s" % str(exc)[:100])
        token = data.get("access_token")
        if not token:
            raise _DriveError("谷歌令牌刷新失败：无 access_token。")
        _DRIVE["token"] = token
        _DRIVE["at"] = time.time()
        return token


def _drive_list(q):
    """One Drive files.list call; returns the files array."""
    params = urllib.parse.urlencode({
        "q": q, "fields": "files(id,name,size)", "spaces": "drive",
        "pageSize": "10"})
    url = "https://www.googleapis.com/drive/v3/files?" + params
    for attempt in (1, 2):
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + _drive_token()})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8",
                                                     "replace"))
            return data.get("files") or []
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 1:
                with _DRIVE_LOCK:
                    _DRIVE["token"] = ""
                continue
            raise _DriveError("Drive 查询失败：HTTP %s" % exc.code)
        except _DriveError:
            raise
        except Exception as exc:
            raise _DriveError("Drive 查询失败：%s" % str(exc)[:100])
    return []


def _drive_videos_dir():
    """Drive id of the /videos folder under the configured root
    folder, cached in kv. None when it cannot be determined;
    callers then query by the (globally unique) hash name alone."""
    cached = kv_get("drive_videos_dir_id")
    if cached:
        return cached
    root = _cred("google_root_folder_id")
    if not root:
        return None
    q = ("name = 'videos' and '%s' in parents and "
         "mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false" % root)
    try:
        files = _drive_list(q)
    except _DriveError:
        return None
    if not files:
        return None
    folder_id = files[0].get("id")
    if folder_id:
        kv_set("drive_videos_dir_id", folder_id)
    return folder_id


def _remote_size_drive(remote_path):
    """Targeted Drive lookup for one object: its size, or None
    when absent. Raises _DriveError when the API itself fails,
    so the caller can fall back to the AList listing."""
    _, _, name = remote_path.rpartition("/")
    base_q = "name = '%s' and trashed = false" % name
    folder = (_drive_videos_dir()
              if remote_path.startswith("/videos/") else None)
    files = []
    if folder:
        files = _drive_list(base_q + " and '%s' in parents" % folder)
        if not files:
            # A parent-constrained miss proves nothing while the
            # cached folder id could be stale; ask once more by
            # name alone and drop the cached id if that hits.
            files = _drive_list(base_q)
            if files:
                kv_set("drive_videos_dir_id", "")
    else:
        files = _drive_list(base_q)
    for item in files:
        if item.get("name") == name:
            try:
                return int(item.get("size") or 0)
            except Exception:
                return 0
    return None


def _remote_size_fast(remote_path):
    """Size of a remote object, or None when absent. Primary
    source is a targeted Drive API query; when the Drive API is
    unavailable (credentials, network), fall back to the old
    refreshed AList listing so archiving degrades instead of
    breaking."""
    try:
        return _remote_size_drive(remote_path)
    except _DriveError:
        return _remote_size(remote_path)



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


_mkdir_done = set()


def _archive_one(row):
    local = _local_path(row)
    remote = _target_gpath(row)
    dir_path = remote.rpartition("/")[0]
    if dir_path and dir_path not in _mkdir_done:
        try:
            ALIST.fs_mkdir(dir_path)
        except Exception:
            pass
        _mkdir_done.add(dir_path)
    # Idempotent: a matching object already on Drive (e.g. an
    # earlier attempt whose upload landed but whose bookkeeping
    # failed) is adopted, never uploaded again.
    remote_size = _remote_size_fast(remote)
    if remote_size is None:
        ALIST.upload_file(local, remote)
        for attempt in range(6):
            time.sleep((1, 2, 3, 5, 5, 5)[attempt])
            remote_size = _remote_size_fast(remote)
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
    if not _cache_keep_local(row, local):
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
            cached = _cache_path(row)
            if os.path.exists(cached):
                os.unlink(cached)
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


def _worker_loop(maintain=True):
    while True:
        try:
            now = time.time()
            for name in os.listdir(STAGING_DIR) if maintain else []:
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
                if maintain:
                    _reap_orphans()
                    _maybe_backup_db()
        except Exception:
            pass
        time.sleep(2)


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    # Three archivers in parallel (the per-file claim is atomic,
    # so workers cannot take the same file); only the primary
    # runs the periodic maintenance (orphan reaping, backups,
    # stale-part sweeping).
    threading.Thread(target=_worker_loop, args=(True,),
                     daemon=True).start()
    threading.Thread(target=_worker_loop, args=(False,),
                     daemon=True).start()
    threading.Thread(target=_worker_loop, args=(False,),
                     daemon=True).start()


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
            "<title>%s · 资源库</title>%s<style>%s</style>%s</head>"
            "<body><div class=\"wrap\">%s</div></body></html>"
            % (_esc(title), base_tag, BASE_CSS, extra_head,
               body)).encode("utf-8")



FEED_PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>刷视频 · 资源库</title>__BASE_TAG__
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#000;color:#eee;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;overflow:hidden}
a{color:#9ec1ff;text-decoration:none}
#bar{position:fixed;top:0;left:0;right:0;z-index:20;display:flex;gap:6px;align-items:center;padding:calc(8px + env(safe-area-inset-top)) 10px 8px;background:rgba(10,10,12,.62);backdrop-filter:blur(8px);transition:transform .28s ease,opacity .28s ease}
#bar .home{font-size:15px;padding:6px 4px;white-space:nowrap}
#bar input{flex:1;min-width:0;background:#1c1e24;color:#eee;border:1px solid #34363e;border-radius:8px;padding:7px 9px;font-size:14px}
#bar select{background:#1c1e24;color:#eee;border:1px solid #34363e;border-radius:8px;padding:7px 4px;font-size:14px}
#bar button{background:#2f6fd0;color:#fff;border:0;border-radius:8px;padding:7px 12px;font-size:14px}
#bar button.ico{background:#1c1e24;border:1px solid #34363e;padding:7px 9px;color:#eee}
body.chrome-off #bar{transform:translateY(-110%);opacity:0;pointer-events:none}
#peek{position:fixed;top:calc(8px + env(safe-area-inset-top));right:10px;z-index:21;width:40px;height:40px;border-radius:50%;background:rgba(20,20,26,.55);border:1px solid rgba(255,255,255,.22);color:#eee;font-size:19px;line-height:1;backdrop-filter:blur(6px);opacity:.55;transition:opacity .28s;cursor:pointer}
body:not(.chrome-off) #peek{opacity:0;pointer-events:none}
#feed{height:100vh;height:100dvh;overflow-y:auto;scroll-snap-type:y mandatory;scrollbar-width:none;background:#15151b}
#feed::-webkit-scrollbar{display:none}
.slide{height:100vh;height:100dvh;padding:13px 0;scroll-snap-align:start;scroll-snap-stop:always;position:relative;display:flex;align-items:center;justify-content:center;background:#15151b;overflow:hidden}
.slide .bgimg{position:absolute;inset:13px 0;border-radius:14px;width:100%;height:100%;object-fit:cover;filter:blur(26px) brightness(.55);transform:scale(1.25)}
.slide video{position:relative;width:100%;height:100%;background:transparent;border-radius:14px}
.slide video.fit-cover{object-fit:cover}
.slide video.fit-contain{object-fit:contain}
.meta{position:absolute;left:0;right:76px;bottom:47px;padding:0 14px;pointer-events:none;z-index:5;text-shadow:0 1px 4px rgba(0,0,0,.8)}
.meta .ttl{font-size:16px;font-weight:600;line-height:1.35;word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;pointer-events:auto}
.meta .ttl.open{display:block}
.meta .sub{font-size:12.5px;color:#dfe2e8;margin-top:5px;line-height:1.5;word-break:break-all}
.badge{display:inline-block;background:#2f6fd0;border-radius:4px;padding:0 5px;font-size:11px;color:#fff}
.badge.gray{background:#4a4d55}
.rail{position:absolute;right:10px;bottom:109px;z-index:6;display:flex;flex-direction:column;gap:14px;align-items:center}
.rail button{width:46px;height:46px;border-radius:50%;border:1px solid rgba(255,255,255,.25);background:rgba(18,18,22,.55);color:#fff;font-size:19px;backdrop-filter:blur(6px);cursor:pointer}
.rail button.on{background:#2f6fd0;border-color:#2f6fd0}
.rail button.like.on{background:#e5484d;border-color:#e5484d}
.rail button.dislike.on{background:#5a5b63;border-color:#5a5b63}
.seenflag{display:inline-block;width:15px;color:rgba(255,255,255,.42);font-size:11px;vertical-align:2px;visibility:hidden}
.seenflag.show{visibility:visible}
.rail .lab{font-size:10.5px;color:#dfe2e8;text-align:center;margin-top:-10px;text-shadow:0 1px 3px rgba(0,0,0,.9)}
.seek{position:absolute;left:0;right:0;bottom:13px;height:30px;z-index:7;touch-action:none;cursor:pointer}
.seek .track{position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(255,255,255,.22);transition:height .15s}
.seek .buf{position:absolute;left:0;bottom:0;height:100%;background:rgba(255,255,255,.35);width:0}
.seek .fill{position:absolute;left:0;bottom:0;height:100%;background:#e8442e;width:0}
.seek .knob{position:absolute;bottom:-5px;width:13px;height:13px;border-radius:50%;background:#e8442e;transform:translateX(-50%);display:none}
.seek.scrub .track{height:8px}
.seek.scrub .knob{display:block}
.seek .tbub{position:absolute;bottom:22px;transform:translateX(-50%);background:rgba(12,12,16,.85);border-radius:6px;padding:4px 9px;font-size:13px;display:none;white-space:nowrap}
.seek.scrub .tbub{display:block}
.slide .flash{position:absolute;top:38%;left:0;right:0;text-align:center;font-size:17px;color:#fff;text-shadow:0 1px 6px rgba(0,0,0,.9);opacity:0;transition:opacity .18s;pointer-events:none;z-index:8}
.flash.show{opacity:1}
.boost{position:absolute;top:calc(52px + env(safe-area-inset-top));left:50%;transform:translateX(-50%);background:rgba(12,12,16,.8);border-radius:999px;padding:5px 14px;font-size:13px;display:none;z-index:8}
.boost.show{display:block}
.endcard{flex-direction:column;gap:14px;text-align:center;padding:20px;color:#c9ccd4;font-size:15px;line-height:1.7}
.endcard button{background:#2f6fd0;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:15px}
#toast{position:fixed;left:50%;bottom:120px;transform:translateX(-50%);background:rgba(20,20,24,.9);padding:8px 16px;border-radius:999px;font-size:13.5px;display:none;z-index:30}
@media(min-width:760px){#feed{max-width:540px;margin:0 auto}#bar{max-width:540px;left:50%;right:auto;transform:translateX(-50%);width:100%}body.chrome-off #bar{transform:translateX(-50%) translateY(-110%)}#peek{right:calc(50% - 260px)}}
</style></head>
<body class="chrome-off">
<div id="bar">
  <a class="home" href="./">← 资源库</a>
  <input id="q" type="search" placeholder="搜索标题 / 标签 / 来源">
  <select id="mode"><option value="random">随机</option><option value="new">最新</option></select>
  <select id="seenf"><option value="unseen">未看</option><option value="all">全部</option><option value="seen">已看</option></select>
  <select id="markf"><option value="">标记:全部</option><option value="liked">❤ 喜欢</option><option value="disliked">不喜欢</option></select><select id="libf">__LIB_OPTIONS__</select>
  <button id="go">筛选</button>
  <button id="bsound" class="ico" title="声音">🔇</button>
  <button id="bdetail" class="ico" title="详情">⤢</button>
  <button id="bfull" class="ico" title="全屏">⛶</button>
</div>
<button id="peek" title="筛选与设置">⚙</button>
<div id="feed"></div>
<div id="toast"></div>
<script>
"use strict";
const $=id=>document.getElementById(id);
const feedEl=$("feed");
let items=[],seed=Math.floor(Math.random()*1e9),offset=0,total=0,
    loading=false,done=false,endCardShown=false;
let cur=-1,muted=true;
const blobs=new Map();let blobBytes=0;
const inflight=new Map();
const BLOB_BUDGET=80*1024*1024;
const CACHE_NAME="vidlib-feed-v1";
let storeOK=("caches" in window);
function manifest(){try{return JSON.parse(
  localStorage.getItem("vidlibFeedCache")||"{}");}catch(e){return{};}}
function saveManifest(m){try{
  localStorage.setItem("vidlibFeedCache",JSON.stringify(m));}catch(e){}}
function regBlob(it,b){
  const u=URL.createObjectURL(b);
  if(!blobs.has(it.id))blobBytes+=it.size;
  blobs.set(it.id,u);
  const m=manifest();
  m[it.id]={size:it.size,at:Date.now(),stream:it.stream};
  saveManifest(m);
  evictIfNeeded();
  return u;}
/* Policy (user-settled): under the budget NOTHING is evicted.
   Only when over budget, evict in one batch - farthest from the
   current slide first - down to 70% of the budget. */
function evictIfNeeded(){
  if(blobBytes<=BLOB_BUDGET)return;
  const m=manifest();
  const order=items.map((x,i)=>({id:x.id,d:Math.abs(i-cur)}))
    .sort((a,b)=>b.d-a.d);
  for(const o of order){
    if(blobBytes<=BLOB_BUDGET*0.7)break;
    if(cur>=0&&items[cur]&&o.id===items[cur].id)continue;
    const it=items.find(x=>x.id===o.id);
    const u=blobs.get(o.id);
    if(u){URL.revokeObjectURL(u);blobs.delete(o.id);
      if(it)blobBytes-=it.size;
      const k=items.findIndex(x=>x.id===o.id);
      const v=slideVideo(k);if(v)delete v.dataset.forId;}
    if(m[o.id]){const st=m[o.id].stream;delete m[o.id];
      if(storeOK)caches.open(CACHE_NAME)
        .then(c=>c.delete(st)).catch(()=>{});}}
  saveManifest(m);}
async function pruneStore(){
  const m=manifest();let tot=0;
  for(const k in m)tot+=m[k].size;
  if(tot<=BLOB_BUDGET)return;
  let c=null;
  if(storeOK)c=await caches.open(CACHE_NAME).catch(()=>null);
  const keys=Object.keys(m).sort((a,b)=>m[a].at-m[b].at);
  for(const k of keys){
    if(tot<=BLOB_BUDGET*0.7)break;
    tot-=m[k].size;
    if(c)await c.delete(m[k].stream).catch(()=>{});
    delete m[k];}
  saveManifest(m);}
function fetchShort(it){
  if(blobs.has(it.id))return Promise.resolve(blobs.get(it.id));
  if(inflight.has(it.id))return inflight.get(it.id);
  const p=(async()=>{
    if(storeOK){
      try{const c=await caches.open(CACHE_NAME);
        const hit=await c.match(it.stream);
        if(hit)return regBlob(it,await hit.blob());
      }catch(e){storeOK=false;}}
    const r=await fetch(it.stream);
    if(!r.ok)throw new Error("fetch failed");
    const b=await r.blob();
    if(storeOK){
      try{const c=await caches.open(CACHE_NAME);
        await c.put(it.stream,new Response(b.slice(0),
          {headers:{"Content-Type":b.type||"video/mp4"}}));
      }catch(e){}}
    return regBlob(it,b);})();
  inflight.set(it.id,p);
  p.catch(()=>{}).finally(()=>inflight.delete(it.id));
  return p;}
const seenSent=new Set();
let progTimer=null,wakeLock=null;

function fmtDur(s){s=Math.max(0,Math.round(s||0));
  const m=Math.floor(s/60);return m+":"+String(s%60).padStart(2,"0");}
function fmtSize(b){return (b/1048576).toFixed(1)+" MiB";}
function toast(t){const el=$("toast");el.textContent=t;
  el.style.display="block";setTimeout(()=>el.style.display="none",1800);}
async function jpost(u,obj){try{await fetch(u,{method:"POST",
  headers:{"Content-Type":"application/json"},
  body:JSON.stringify(obj)});}catch(e){}}

/* ---------- sound + top-bar actions ---------- */
/* Autoplay rules force videos to start muted; the intent is sound
   ON, so the first tap on the feed unlocks audio for the session.
   A deliberate tap on the bar's sound button always wins. */
let soundTouched=false;
function setMuted(m){
  muted=m;
  document.querySelectorAll("#feed video").forEach(x=>x.muted=m);
  $("bsound").textContent=m?"🔇":"🔊";
  if(!m){const v=slideVideo(cur);
    if(v&&v.paused)v.play().catch(()=>{});}
}
feedEl.addEventListener("pointerdown",()=>{
  if(soundTouched)return;soundTouched=true;setMuted(false);});
$("bsound").onclick=()=>{soundTouched=true;setMuted(!muted);};
$("bdetail").onclick=()=>{
  if(cur>=0&&items[cur])location.href="watch/"+items[cur].id;};
document.addEventListener("keydown",e=>{
  if(e.ctrlKey||e.metaKey||e.altKey)return;
  const k=e.key.toLowerCase();
  if(k!=="z"&&k!=="x")return;
  const ae=document.activeElement;
  if(ae&&/INPUT|TEXTAREA|SELECT/.test(ae.tagName))return;
  const sec=slideEl(cur);if(!sec)return;
  const b=sec.querySelector(k==="z"?".rail button.like"
                                   :".rail button.dislike");
  if(b)b.click();});
$("bfull").onclick=()=>{
  const el=document.documentElement,v=slideVideo(cur);
  if(document.fullscreenElement)document.exitFullscreen();
  else if(el.requestFullscreen)el.requestFullscreen().catch(()=>{});
  else if(v&&v.webkitEnterFullscreen)v.webkitEnterFullscreen();};

/* ---------- chrome (top bar): collapsed by default ---------- */
/* The bar NEVER shows on its own - not on touch, not on pause.
   The gear toggles it; while open, it re-hides after 4 s, and
   interacting with the bar itself re-arms that timer. */
let barTimer=null;
function showBar(){
  document.body.classList.remove("chrome-off");
  clearTimeout(barTimer);
  barTimer=setTimeout(
    ()=>document.body.classList.add("chrome-off"),4000);
}
function hideBar(){
  clearTimeout(barTimer);
  document.body.classList.add("chrome-off");
}
$("peek").onclick=()=>{
  if(document.body.classList.contains("chrome-off"))showBar();
  else hideBar();};
$("bar").addEventListener("pointerdown",()=>{
  if(!document.body.classList.contains("chrome-off"))showBar();});

/* ---------- feed engine ---------- */
const io=new IntersectionObserver(es=>{
  es.forEach(en=>{
    const idx=+en.target.dataset.idx;
    if(en.isIntersecting&&en.intersectionRatio>=0.6)activate(idx);
    else if(!en.isIntersecting)deactivate(idx);
  });
},{root:feedEl,threshold:[0,0.6]});

function slideEl(idx){return feedEl.children[idx]||null;}
function slideVideo(idx){const s=slideEl(idx);
  return s?s.querySelector("video"):null;}

function addSlide(it,idx){
  const sec=document.createElement("section");
  sec.className="slide";sec.dataset.idx=idx;
  const bg=document.createElement("img");
  bg.className="bgimg";bg.src=it.thumb;bg.alt="";
  const v=document.createElement("video");
  v.playsInline=true;v.setAttribute("playsinline","");
  v.preload="none";v.poster=it.thumb;v.muted=muted;
  v.className=(it.height>=it.width&&it.height>0)?"fit-cover":"fit-contain";
  const flash=document.createElement("div");flash.className="flash";
  const boost=document.createElement("div");boost.className="boost";
  boost.textContent="2 倍速播放中";
  const meta=document.createElement("div");meta.className="meta";
  const tags=it.tags?it.tags.split(/[,，、\s]+/).filter(Boolean)
    .map(t=>"#"+t).join(" "):"";
  meta.innerHTML='<div class="ttl"></div><div class="sub"></div>';
  const ttl=meta.querySelector(".ttl");
  const sflag=document.createElement("span");
  sflag.className="seenflag"+(it.seen?" show":"");
  sflag.textContent="✓";
  ttl.appendChild(sflag);
  ttl.appendChild(document.createTextNode(it.title));
  ttl.onclick=()=>ttl.classList.toggle("open");
  meta.querySelector(".sub").innerHTML=
    (tags?tags+" · ":"")+fmtDur(it.duration)+" · "+fmtSize(it.size)+" · "+

    (it.cached?' <span class="badge">本地缓存</span>':"");
  const rail=document.createElement("div");rail.className="rail";
  function railBtn(txt,lab,fn){
    const b=document.createElement("button");b.textContent=txt;
    b.onclick=e=>{e.stopPropagation();fn(b);};
    const l=document.createElement("div");l.className="lab";
    l.textContent=lab;rail.appendChild(b);rail.appendChild(l);
    return [b,l];}
  const [bLike]=railBtn("❤","喜欢",b=>{
    const nm=it.mark===1?0:1;it.mark=nm;
    jpost("api/mark",{id:it.id,mark:nm});
    b.classList.toggle("on",nm===1);
    if(nm===1)bDislike.classList.toggle("on",false);
  });
  bLike.classList.add("like");
  bLike.classList.toggle("on",it.mark===1);
  const [bDislike]=railBtn("👎","不喜欢",b=>{
    if(it.mark===2){
      it.mark=0;jpost("api/mark",{id:it.id,mark:0});
      b.classList.toggle("on",false);return;}
    it.mark=2;jpost("api/mark",{id:it.id,mark:2});
    b.classList.toggle("on",true);
    bLike.classList.toggle("on",false);
    if(!it.seen)markSeen(sec,it,true);
    const nx=slideEl(idx+1);
    if(nx)nx.scrollIntoView({behavior:"smooth"});
    else toast("已标记不喜欢（已是最后一条）");
  });
  bDislike.classList.add("dislike");
  bDislike.classList.toggle("on",it.mark===2);
  const seek=document.createElement("div");seek.className="seek";
  seek.innerHTML='<div class="tbub"></div><div class="track">'+
    '<div class="buf"></div><div class="fill"></div></div>'+
    '<div class="knob"></div>';
  attachSeek(seek,v,it);
  attachGestures(v,flash,boost);
  v.addEventListener("timeupdate",()=>{
    if(seek.classList.contains("scrub"))return;
    const f=seek.querySelector(".fill"),bf=seek.querySelector(".buf"),
          kb=seek.querySelector(".knob");
    if(v.duration>0){
      const p=v.currentTime/v.duration*100;
      f.style.width=p+"%";kb.style.left=p+"%";}
    try{if(v.buffered.length&&v.duration>0)
      bf.style.width=(v.buffered.end(v.buffered.length-1)
        /v.duration*100)+"%";}catch(e){}
    if(v.currentTime>=3&&!seenSent.has(it.id))markSeen(sec,it,true);
  });
  v.addEventListener("play",()=>{
    if(navigator.wakeLock&&!wakeLock)
      navigator.wakeLock.request("screen").then(w=>wakeLock=w)
        .catch(()=>{});});
  v.addEventListener("ended",()=>{
    if(!seenSent.has(it.id))markSeen(sec,it,true);
    const nx=slideEl(idx+1);
    if(nx)nx.scrollIntoView({behavior:"smooth"});
  });
  sec.appendChild(bg);sec.appendChild(v);sec.appendChild(flash);
  sec.appendChild(boost);sec.appendChild(meta);sec.appendChild(rail);
  sec.appendChild(seek);
  feedEl.appendChild(sec);io.observe(sec);
}

function markSeen(sec,it,ns){
  it.seen=ns;
  if(ns)seenSent.add(it.id);else seenSent.delete(it.id);
  jpost("api/seen",{id:it.id,seen:ns});
  const fl=sec.querySelector(".seenflag");
  if(fl)fl.classList.toggle("show",ns);
}

function attachSeek(seek,v,it){
  const fill=seek.querySelector(".fill"),knob=seek.querySelector(".knob"),
        bub=seek.querySelector(".tbub");
  let scrubbing=false,wasPlaying=false;
  function ratio(e){const r=seek.getBoundingClientRect();
    return Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));}
  function show(e){const p=ratio(e);
    fill.style.width=(p*100)+"%";knob.style.left=(p*100)+"%";
    bub.style.left=(p*100)+"%";
    if(v.duration>0)bub.textContent=fmtDur(p*v.duration)
      +" / "+fmtDur(v.duration);
    return p;}
  seek.addEventListener("pointerdown",e=>{
    if(!v.duration)return;
    scrubbing=true;wasPlaying=!v.paused;
    seek.classList.add("scrub");
    try{seek.setPointerCapture(e.pointerId);}catch(err){}
    v.pause();show(e);e.preventDefault();e.stopPropagation();});
  seek.addEventListener("pointermove",e=>{
    if(scrubbing)show(e);});
  function finish(e){
    if(!scrubbing)return;scrubbing=false;
    seek.classList.remove("scrub");
    const p=ratio(e);
    if(v.duration>0){v.currentTime=p*v.duration;
      jpost("api/progress",{id:it.id,pos:v.currentTime});}
    if(wasPlaying)v.play().catch(()=>{});}
  seek.addEventListener("pointerup",finish);
  seek.addEventListener("pointercancel",finish);
}

function attachGestures(v,flash,boost){
  let downT=0,downX=0,downY=0,lastTap=0,lpTimer=null,boosted=false,
      tapTimer=null;
  function showFlash(t){flash.textContent=t;
    flash.classList.add("show");
    setTimeout(()=>flash.classList.remove("show"),550);}
  v.addEventListener("pointerdown",e=>{
    downT=Date.now();downX=e.clientX;downY=e.clientY;boosted=false;
    clearTimeout(lpTimer);
    lpTimer=setTimeout(()=>{boosted=true;v.playbackRate=2;
      boost.classList.add("show");},420);});
  v.addEventListener("pointerup",e=>{
    clearTimeout(lpTimer);
    if(boosted){v.playbackRate=1;boost.classList.remove("show");return;}
    const dt=Date.now()-downT,
          moved=Math.hypot(e.clientX-downX,e.clientY-downY);
    if(dt>320||moved>12)return;
    const now=Date.now(),rect=v.getBoundingClientRect(),
          x=(e.clientX-rect.left)/rect.width;
    if(now-lastTap<300){
      clearTimeout(tapTimer);lastTap=0;
      if(v.duration>0){
        if(x<0.4){v.currentTime=Math.max(0,v.currentTime-10);
          showFlash("⟲ 快退 10 秒");}
        else if(x>0.6){v.currentTime=Math.min(v.duration,
          v.currentTime+10);showFlash("快进 10 秒 ⟳");}
        else{v.currentTime=0;showFlash("从头播放");}
      }
      return;}
    lastTap=now;
    tapTimer=setTimeout(()=>{
      if(v.paused)v.play().catch(()=>{});else v.pause();
    },290);});
  v.addEventListener("pointercancel",()=>{clearTimeout(lpTimer);
    if(boosted){v.playbackRate=1;boost.classList.remove("show");}});
}

function addEndCard(){
  if(endCardShown)return;endCardShown=true;
  const sec=document.createElement("section");
  sec.className="slide endcard";sec.dataset.idx=items.length;
  sec.innerHTML=items.length?"<div>— 刷完了 —</div>"
    :"<div>没有符合条件的视频。</div>";
  if($("seenf").value==="unseen"){
    const b=document.createElement("button");
    b.textContent="看全部视频（随机）";
    b.onclick=()=>{$("seenf").value="all";reset();};
    sec.appendChild(b);}
  const b2=document.createElement("button");
  b2.textContent="再随机一轮";b2.onclick=()=>reset();
  sec.appendChild(b2);
  feedEl.appendChild(sec);
}

function setSrc(idx){
  const it=items[idx],v=slideVideo(idx);if(!v)return;
  if(v.dataset.forId==String(it.id))return;
  v.dataset.forId=String(it.id);
  function seekPos(){
    if(it.pos>5&&(!it.duration||it.pos<it.duration-10)){
      v.addEventListener("loadedmetadata",()=>{
        try{v.currentTime=it.pos;}catch(e){}},{once:true});}}
  if(!it.short){v.src=it.stream;seekPos();return;}
  if(idx===0&&it.cached){
    /* Session opener: the server already holds this file in its
       local cache, so stream it at once for a fast start instead
       of waiting for a full blob download first. */
    v.src=it.stream;seekPos();return;}
  /* Short: play ONLY from the (persistent) blob cache - never
     start a parallel stream download of the same file. Fall
     back to streaming only if the blob takes >3 s or fails. */
  let fell=false;
  const timer=setTimeout(()=>{
    if(v.dataset.forId==String(it.id)&&!v.src){
      fell=true;v.src=it.stream;seekPos();
      if(idx===cur)v.play().catch(()=>{});}},3000);
  fetchShort(it).then(u=>{
    clearTimeout(timer);
    if(v.dataset.forId!=String(it.id)||fell)return;
    v.src=u;seekPos();
    if(idx===cur)v.play().catch(()=>{});})
  .catch(()=>{clearTimeout(timer);
    if(v.dataset.forId==String(it.id)&&!v.src){
      v.src=it.stream;seekPos();
      if(idx===cur)v.play().catch(()=>{});}});
}

function activate(idx){
  if(idx===cur||idx>=items.length)return;
  if(cur>=0)deactivate(cur);
  cur=idx;const v=slideVideo(idx);if(!v)return;
  setSrc(idx);v.muted=muted;
  v.play().catch(()=>{});
  clearInterval(progTimer);
  progTimer=setInterval(()=>{const cv=slideVideo(cur);
    if(cv&&!cv.paused)jpost("api/progress",
      {id:items[cur].id,pos:cv.currentTime});},10000);
  prefetchAround(idx);
  if(idx>=items.length-3)loadMore();
}

function deactivate(idx){
  const v=slideVideo(idx);if(!v)return;
  if(!v.paused){v.pause();
    if(idx<items.length)jpost("api/progress",
      {id:items[idx].id,pos:v.currentTime});}
}

function prefetchAround(idx){
  for(let j=idx+1;j<=Math.min(idx+2,items.length-1);j++){
    const it=items[j];
    if(it.short)fetchShort(it).catch(()=>{});
    else if(j===idx+1){
      const v=slideVideo(j);
      if(v&&v.dataset.forId!==String(it.id)){
        v.preload="auto";setSrc(j);v.pause();}}
  }
}

async function loadMore(){
  if(loading||done)return;loading=true;
  try{
    const qs=new URLSearchParams({mode:$("mode").value,
      seen:$("seenf").value,mark:$("markf").value,lib:$("libf").value,q:$("q").value.trim(),
      seed:seed,offset:offset,limit:12});
    const d=await (await fetch("api/feed?"+qs)).json();
    total=d.total;
    d.items.forEach(it=>{items.push(it);addSlide(it,items.length-1);});
    offset+=d.items.length;
    if(d.done)done=true;
    if(done)addEndCard();
    if(cur<0&&items.length)activate(0);
  }catch(e){toast("加载失败，检查网络");}
  loading=false;
}

function reset(){
  items=[];offset=0;total=0;done=false;endCardShown=false;cur=-1;
  seed=Math.floor(Math.random()*1e9);seenSent.clear();
  feedEl.innerHTML="";feedEl.scrollTop=0;
  loadMore();
}

$("go").onclick=()=>{reset();};
$("q").addEventListener("keydown",e=>{if(e.key==="Enter")reset();});
$("mode").onchange=reset;
$("seenf").onchange=reset;
$("markf").onchange=reset;$("libf").onchange=reset;
document.addEventListener("visibilitychange",()=>{
  if(document.hidden){const v=slideVideo(cur);
    if(v&&!v.paused)v.pause();}});
pruneStore();
loadMore();
</script>
</body></html>
"""


def page_feed(base_href=""):
    base_tag = ('<base href="%s">' % _esc(base_href)) \
        if base_href else ""
    return FEED_PAGE.replace("__BASE_TAG__", base_tag) \
        .replace("__LIB_OPTIONS__", _lib_options_html()).encode("utf-8")


_ENTRY_COLS = (
    "SELECT e.id AS id, e.hash AS hash, e.title AS title, "
    "e.folder AS folder, e.tags AS tags, e.source AS source, "
    "e.play_pos AS play_pos, e.play_count AS play_count, "
    "e.seen_at AS seen_at, e.mark AS mark, e.mark_at AS mark_at, "
    "e.created_at AS created_at, e.library_id AS library_id, "
    "f.kind AS kind, f.category AS category, "
    "f.artist AS artist, f.album AS album, "
    "f.bit_rate AS bit_rate, "
    "f.ext AS ext, f.size AS size, "
    "f.duration AS duration, f.width AS width, f.height AS height, "
    "f.status AS status, f.gpath AS gpath, f.error AS error "
    "FROM entries e JOIN files f ON f.hash = e.hash ")


def _safe_lib(value):
    try:
        return int(value)
    except Exception:
        return None


def _validate_lib_name(name, exclude_id=None):
    if not name or len(name) > 30:
        return "库名长度需在 1-30 字。"
    if "/" in name or "\\" in name:
        return "库名不能含斜杠。"
    if _MONTH_RE.fullmatch(name):
        return "库名不能是日期形（YYYY-MM）。"
    for l in _libraries("video"):
        if l["name"] == name and l["id"] != exclude_id:
            return "已有同名库。"
    return ""


def _lib_options_html():
    parts = []
    for l in _libraries("video"):
        parts.append('<option value="%d"%s>%s%s</option>' % (
            l["id"], " selected" if l["is_default"] else "",
            _esc(l["name"]), "（默认）" if l["is_default"] else ""))
    return "".join(parts)


def page_hub(params, base_href=""):
    with _db() as conn:
        counts = {}
        for r in conn.execute(
                "SELECT f.kind AS kind, COUNT(*) AS c FROM entries e "
                "JOIN files f ON f.hash = e.hash GROUP BY f.kind"):
            counts[r["kind"] or "video"] = r["c"]
        music_recent = conn.execute(
            _ENTRY_COLS + " WHERE f.kind = 'music' "
            "ORDER BY e.id DESC LIMIT 1").fetchone()
        vlibs = conn.execute(
            "SELECT l.*, (SELECT COUNT(*) FROM entries e "
            "WHERE e.library_id = l.id) AS n FROM libraries l "
            "WHERE l.kind = 'video' ORDER BY l.is_default DESC, l.id"
        ).fetchall()
    libbits = "、".join(
        "%s（%d）" % (_esc(l["name"]), l["n"]) for l in vlibs) or "默认库"
    music_suffix = ""
    if music_recent:
        music_suffix = " · 最近：%s" % _esc(music_recent["title"])
    cards = (
        '<a class="card" href="wall?kind=video">'
        '<span class="t">🎬 视频</span>'
        '<div class="m">%d 条 · 主题库：%s</div></a>'
        '<a class="card" href="feed">'
        '<span class="t">▶ 刷视频</span>'
        '<div class="m">默认只刷默认库，页面里可切库</div></a>'
        '<a class="card" href="images">'
        '<span class="t">🖼 图片</span>'
        '<div class="m">%d 张 · 网格浏览 + 灯箱</div></a>'
        '<a class="card" href="novels">'
        '<span class="t">📖 小说</span>'
        '<div class="m">%d 本 · txt，带章节与阅读进度</div></a>'
        '<a class="card" href="files">'
        '<span class="t">📦 文件</span>'
        '<div class="m">%d 个 · 按类别规则归档</div></a>'
        '<a class="card" href="music">'
        '<span class="t">🎵 音乐</span>'
        '<div class="m">%d 首%s</div></a>'
        '<a class="card" href="upload">'
        '<span class="t">⬆ 上传</span>'
        '<div class="m">视频 / 图片 / 音乐 / txt 小说 / 任意文件</div></a>'
        '<a class="card" href="admin">'
        '<span class="t">⚙ 管理</span>'
        '<div class="m">主题库、文件类别、DAV、补传、清理</div></a>'
    ) % (counts.get("video", 0), libbits, counts.get("image", 0),
         counts.get("novel", 0), counts.get("file", 0),
         counts.get("music", 0), music_suffix)
    body = ('<div class="topbar"><h1>资源库</h1></div>'
            '<div class="grid">' + cards + "</div>")
    return _page("资源库", body, base_href=base_href)


def page_images(params, base_href=""):
    try:
        page = max(1, int(params.get("page") or "1"))
    except Exception:
        page = 1
    q = (params.get("q") or "").strip()
    per = 60
    with _db() as conn:
        where = ["f.kind = 'image'"]
        args = []
        if q:
            where.append("(e.title LIKE ? OR e.tags LIKE ?)")
            like = "%" + q + "%"
            args += [like, like]
        total = conn.execute(
            "SELECT COUNT(*) FROM entries e JOIN files f "
            "ON f.hash = e.hash WHERE " + " AND ".join(where),
            args).fetchone()[0]
        rows = conn.execute(
            _ENTRY_COLS + " WHERE " + " AND ".join(where)
            + " ORDER BY e.id DESC LIMIT ? OFFSET ?",
            args + [per, (page - 1) * per]).fetchall()
    items = []
    cards = []
    for i, r in enumerate(rows):
        items.append({"i": i, "id": r["id"], "t": r["title"],
                      "f": "file/" + r["hash"]})
        cards.append(
            '<div class="card lbopen" data-i="%d" style="cursor:pointer">'
            '<img class="thumb" loading="lazy" src="%s" alt="">'
            '<span class="t">%s</span>'
            '<div class="m">%s · %s · <a href="watch/%d">详情</a></div></div>'
            % (i, _esc(base_href + "thumb/" + r["hash"]),
               _esc(r["title"]), r["folder"],
               _fmt_size(r["size"] or 0), r["id"]))
    pages = max(1, (total + per - 1) // per)
    pager = ""
    if pages > 1:
        bits = []
        for n in range(1, pages + 1):
            bits.append("<strong>%d</strong>" % n if n == page else
                        '<a href="?page=%d">%d</a>' % (n, n))
        pager = ('<div style="display:flex;gap:8px;justify-content:center;'
                 'margin:16px 0;font-size:14px">' + " ".join(bits) + "</div>")
    lb = (
        '<div id="lb" style="display:none;position:fixed;inset:0;'
        'background:rgba(5,6,10,.95);z-index:60;flex-direction:column;'
        'align-items:center;justify-content:center;padding:14px">'
        '<img id="lbimg" style="max-width:96vw;max-height:82vh;'
        'object-fit:contain;border-radius:10px">'
        '<div style="margin-top:12px;display:flex;gap:8px;'
        'align-items:center;flex-wrap:wrap;justify-content:center">'
        '<button type="button" id="lbprev">← 上一张</button>'
        '<span id="lbcap" style="color:#c8cfdd;font-size:13px"></span>'
        '<a class="btn" id="lbfull" target="_blank" rel="noopener">原图</a>'
        '<a class="btn ghost" id="lbwatch">详情</a>'
        '<button type="button" id="lbnext">下一张 →</button>'
        '<button type="button" class="ghost" id="lbclose">关闭</button>'
        '</div></div>')
    js = ('<script>const LB=' + json.dumps(items, ensure_ascii=False)
          + ';let lbi=0;const lb=document.getElementById("lb");'
          'function showLb(){const it=LB[lbi];if(!it)return;'
          'document.getElementById("lbimg").src=it.f;'
          'document.getElementById("lbcap").textContent='
          '"（"+(lbi+1)+" / "+LB.length+"）"+it.t;'
          'document.getElementById("lbfull").href=it.f;'
          'document.getElementById("lbwatch").href="watch/"+it.id;}'
          'function openLb(i){lbi=i;lb.style.display="flex";showLb();}'
          'document.querySelectorAll(".lbopen").forEach(function(el){'
          'el.addEventListener("click",function(ev){'
          'if(ev.target.tagName==="A")return;'
          'openLb(parseInt(el.dataset.i,10));});});'
          'function stepLb(d){if(!LB.length)return;'
          'lbi=(lbi+d+LB.length)%LB.length;showLb();}'
          'document.getElementById("lbprev").onclick=function(){stepLb(-1);};'
          'document.getElementById("lbnext").onclick=function(){stepLb(1);};'
          'document.getElementById("lbclose").onclick=function(){'
          'lb.style.display="none";};'
          'lb.addEventListener("click",function(ev){'
          'if(ev.target===lb)lb.style.display="none";});'
          'document.addEventListener("keydown",function(ev){'
          'if(lb.style.display!=="flex")return;'
          'if(ev.key==="ArrowLeft")stepLb(-1);'
          'if(ev.key==="ArrowRight")stepLb(1);'
          'if(ev.key==="Escape")lb.style.display="none";});'
          '</script>')
    body = (
        '<div class="topbar"><h1>图片</h1>'
        '<form class="search" method="get">'
        '<input type="search" name="q" value="%s" placeholder="搜索标题/标签">'
        '</form>'
        '<a class="btn ghost" href="./">← 资源库</a>'
        '<a class="btn ghost" href="upload">上传</a></div>'
        '<div style="margin-bottom:12px;font-size:13px">共 %d 张</div>'
        '<div class="grid">%s</div>%s%s%s'
        % (_esc(q), total, "".join(cards) or
           '<div class="muted">暂无图片。</div>', pager, lb, js))
    return _page("图片", body, base_href)


_MUSIC_PER_PAGE = 30

_MUSIC_CSS = (
    "<style>"
    ".muwrap{padding-bottom:124px}"
    ".mulist{display:flex;flex-direction:column;gap:8px}"
    ".murow{display:flex;gap:11px;align-items:center;background:#171c27;"
    "border:1px solid #242c3f;border-radius:11px;padding:9px 12px}"
    ".murow.playing{border-color:#2f6fd0}"
    ".mucover{width:46px;height:46px;border-radius:8px;object-fit:cover;"
    "background:#232a3a;flex:none}"
    ".mumain{flex:1;min-width:0}"
    ".mut{display:block;font-size:15px;color:#e8ebf1;white-space:nowrap;"
    "overflow:hidden;text-overflow:ellipsis}"
    ".mum{display:block;font-size:12.5px;color:#8b94a7;margin-top:2px;"
    "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".mubtn.on{color:#ff5d7a;border-color:#ff5d7a}"
    "#mubar{position:fixed;left:0;right:0;bottom:0;z-index:55;"
    "background:rgba(13,16,24,.97);border-top:1px solid #2a3350;"
    "padding:10px 14px}"
    "#munow{font-size:13.5px;color:#e8ebf1;white-space:nowrap;"
    "overflow:hidden;text-overflow:ellipsis;max-width:880px;"
    "margin:0 auto 7px}"
    "#museek{display:block;width:100%;max-width:880px;margin:0 auto}"
    ".murow2{display:flex;gap:8px;align-items:center;max-width:880px;"
    "margin:7px auto 0}"
    ".mutime{font-size:12px;color:#8b94a7;margin-left:auto}"
    "</style>")

_MUSIC_JS = r"""
function muShuffle(a){for(let i=a.length-1;i>0;i--){
const j=Math.floor(Math.random()*(i+1));const t=a[i];a[i]=a[j];a[j]=t;}
return a;}
function muBuildQueue(items,mode,startId){
const list=items.slice();
if(mode==="shuffle"){
const idx=list.findIndex(function(x){return x.id===startId;});
let head=null;
if(idx>=0){head=list.splice(idx,1)[0];}
muShuffle(list);
if(head)list.unshift(head);
return list;}
return list;}
function muQueuePos(queue,id){
return queue.findIndex(function(x){return x.id===id;});}
let MU={items:null,queue:[],pos:-1,mode:(function(){
try{return localStorage.getItem("mu_mode")||"seq";}
catch(e){return "seq";}})()};
const mau=document.getElementById("mplayer");
function muFmtT(s){s=Math.max(0,Math.floor(s||0));
const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),x=s%60;
return (h?h+":":"")+(h?String(m).padStart(2,"0"):m)+":"+String(x).padStart(2,"0");}
async function muItems(){
if(MU.items)return MU.items;
const r=await fetch("api/music/list?q="+encodeURIComponent(MUQ));
const d=await r.json();
MU.items=(d&&d.items)?d.items:[];
return MU.items;}
function muMarkRow(){
const cur=MU.queue[MU.pos];
document.querySelectorAll(".murow").forEach(function(el){
el.classList.toggle("playing",
!!cur&&parseInt(el.dataset.id,10)===cur.id);});}
function muSyncBtn(){
document.getElementById("muplay").textContent=
mau.paused?"▶ 播放":"⏸ 暂停";
document.getElementById("mushuffle").textContent=
MU.mode==="shuffle"?"🔀 随机：开":"🔀 随机：关";}
async function muPlayAt(pos){
if(pos<0||pos>=MU.queue.length)return;
MU.pos=pos;
const it=MU.queue[pos];
document.getElementById("munow").textContent="加载中… "+it.t;
muMarkRow();
try{
const r=await fetch("api/play/"+it.id);
const d=await r.json();
if(!d.ok)throw new Error("bad");
mau.src=d.url;
await mau.play();
document.getElementById("munow").textContent=
it.t+(it.a?" · "+it.a:"");
}catch(e){
document.getElementById("munow").textContent="播放失败："+it.t;}
muSyncBtn();}
async function muStart(id){
const items=await muItems();
if(!items.length)return;
MU.queue=muBuildQueue(items,MU.mode,id||null);
let pos=0;
if(id){const p=muQueuePos(MU.queue,id);if(p>=0)pos=p;}
muPlayAt(pos);}
async function muShuffleAll(){
MU.mode="shuffle";
try{localStorage.setItem("mu_mode","shuffle");}catch(e){}
const items=await muItems();
if(!items.length){muSyncBtn();return;}
MU.queue=muBuildQueue(items,"shuffle",null);
muSyncBtn();
muPlayAt(0);}
function muStep(d){
if(!MU.queue.length)return;
const n=MU.pos+d;
if(n<0||n>=MU.queue.length)return;
muPlayAt(n);}
document.querySelectorAll(".play").forEach(function(b){
b.addEventListener("click",function(){
const id=parseInt(b.dataset.id,10);
const cur=MU.queue[MU.pos];
if(cur&&cur.id===id){
if(mau.paused)mau.play();else mau.pause();
muSyncBtn();return;}
muStart(id);});});
document.querySelectorAll(".like").forEach(function(b){
b.addEventListener("click",async function(){
const on=b.classList.contains("on");
const r=await fetch("api/mark",{method:"POST",
headers:{"Content-Type":"application/json"},
body:JSON.stringify({id:parseInt(b.dataset.id,10),mark:on?0:1})});
const d=await r.json();
if(d.ok){b.classList.toggle("on",!on);
if(!on){const row=b.closest(".murow");
const dis=row?row.querySelector(".dis"):null;
if(dis)dis.classList.remove("on");}}});});
document.querySelectorAll(".dis").forEach(function(b){
b.addEventListener("click",async function(){
const on=b.classList.contains("on");
const r=await fetch("api/mark",{method:"POST",
headers:{"Content-Type":"application/json"},
body:JSON.stringify({id:parseInt(b.dataset.id,10),mark:on?0:2})});
const d=await r.json();
if(d.ok){b.classList.toggle("on",!on);
if(!on){const row=b.closest(".murow");
const like=row?row.querySelector(".like"):null;
if(like)like.classList.remove("on");}}});});
document.getElementById("muplay").onclick=function(){
if(!MU.queue.length){muStart(null);return;}
if(mau.paused)mau.play();else mau.pause();
muSyncBtn();};
document.getElementById("munext").onclick=function(){muStep(1);};
document.getElementById("muprev").onclick=function(){muStep(-1);};
document.getElementById("mushuffle").onclick=async function(){
MU.mode=(MU.mode==="shuffle")?"seq":"shuffle";
try{localStorage.setItem("mu_mode",MU.mode);}catch(e){}
const cur=MU.queue[MU.pos];
if(cur&&MU.items){
MU.queue=muBuildQueue(MU.items,MU.mode,cur.id);
MU.pos=muQueuePos(MU.queue,cur.id);}
muSyncBtn();};
document.getElementById("mushuffleall").onclick=function(){
muShuffleAll();};
mau.addEventListener("ended",function(){
if(MU.pos+1<MU.queue.length)muPlayAt(MU.pos+1);
else muSyncBtn();});
mau.addEventListener("play",muSyncBtn);
mau.addEventListener("pause",muSyncBtn);
const museek=document.getElementById("museek");
let museeking=false;
museek.addEventListener("input",function(){museeking=true;});
museek.addEventListener("change",function(){
if(mau.duration)mau.currentTime=mau.duration*museek.value/1000;
museeking=false;});
mau.addEventListener("timeupdate",function(){
if(!museeking&&mau.duration)
museek.value=Math.floor(mau.currentTime/mau.duration*1000);
document.getElementById("mutime").textContent=
muFmtT(mau.currentTime)+" / "+muFmtT(mau.duration);});
muSyncBtn();
"""


def _music_where(q):
    where = ["f.kind = 'music'"]
    args = []
    if q:
        where.append("(e.title LIKE ? OR f.artist LIKE ? "
                     "OR f.album LIKE ? OR e.tags LIKE ?)")
        like = "%" + q + "%"
        args += [like, like, like, like]
    return where, args


def _music_list(q):
    """The full current filter/search result set for the music
    player queue (lightweight fields only)."""
    where, args = _music_where(q)
    with _db() as conn:
        rows = conn.execute(
            "SELECT e.id AS id, e.title AS t, f.artist AS a "
            "FROM entries e JOIN files f ON f.hash = e.hash "
            "WHERE " + " AND ".join(where)
            + " ORDER BY e.id DESC LIMIT 2000", args).fetchall()
    return [{"id": r["id"], "t": r["t"], "a": r["a"] or ""}
            for r in rows]


def page_music(params, base_href=""):
    try:
        page = max(1, int(params.get("page") or "1"))
    except Exception:
        page = 1
    q = (params.get("q") or "").strip()
    per = _MUSIC_PER_PAGE
    where, args = _music_where(q)
    with _db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM entries e JOIN files f "
            "ON f.hash = e.hash WHERE " + " AND ".join(where),
            args).fetchone()[0]
        rows = conn.execute(
            _ENTRY_COLS + " WHERE " + " AND ".join(where)
            + " ORDER BY e.id DESC LIMIT ? OFFSET ?",
            args + [per, (page - 1) * per]).fetchall()
    cards = []
    for r in rows:
        meta = " · ".join(x for x in (
            r["artist"] or "", r["album"] or "",
            _fmt_duration(r["duration"] or 0),
            _fmt_size(r["size"] or 0)) if x)
        cards.append(
            '<div class="murow" data-id="%d">'
            '<img class="mucover" loading="lazy" src="%s" alt="">'
            '<div class="mumain"><span class="mut">%s</span>'
            '<span class="mum">%s</span></div>'
            '<button type="button" class="ghost mubtn play" '
            'data-id="%d">▶</button>'
            '<button type="button" class="ghost mubtn like%s" '
            'data-id="%d" title="喜欢">❤</button>'
            '<button type="button" class="ghost mubtn dis%s" '
            'data-id="%d" title="不喜欢">👎</button>'
            '<a class="btn ghost" href="watch/%d">详情</a></div>'
            % (r["id"], _esc(base_href + "thumb/" + r["hash"]),
               _esc(r["title"]), _esc(meta), r["id"],
               " on" if (r["mark"] or 0) == 1 else "", r["id"],
               " on" if (r["mark"] or 0) == 2 else "", r["id"],
               r["id"]))
    pages = max(1, (total + per - 1) // per)
    pager = ""
    if pages > 1:
        bits = []
        for n in range(1, pages + 1):
            qq = ("&q=" + urllib.parse.quote(q)) if q else ""
            bits.append("<strong>%d</strong>" % n if n == page else
                        '<a href="?page=%d%s">%d</a>' % (n, qq, n))
        pager = ('<div style="display:flex;gap:8px;'
                 'justify-content:center;margin:16px 0;'
                 'font-size:14px">' + " ".join(bits) + "</div>")
    bar = (
        '<div id="mubar">'
        '<div id="munow">未播放</div>'
        '<input type="range" id="museek" min="0" max="1000" '
        'value="0">'
        '<div class="murow2">'
        '<button type="button" id="muprev">⏮ 上一首</button>'
        '<button type="button" id="muplay">▶ 播放</button>'
        '<button type="button" id="munext">下一首 ⏭</button>'
        '<button type="button" class="ghost" id="mushuffle">'
        '🔀 随机：关</button>'
        '<span class="mutime" id="mutime">0:00 / 0:00</span>'
        '</div></div>'
        '<audio id="mplayer" preload="none"></audio>')
    js = ('<script>const MUQ='
          + json.dumps(q, ensure_ascii=False)
          + ';' + _MUSIC_JS + '</script>')
    body = (
        '<div class="muwrap">'
        '<div class="topbar"><h1>音乐</h1>'
        '<form class="search" method="get">'
        '<input type="search" name="q" value="%s" '
        'placeholder="搜索歌名 / 歌手 / 专辑">'
        '</form>'
        '<button type="button" id="mushuffleall">'
        '🔀 随机播放全部</button>'
        '<a class="btn ghost" href="./">← 资源库</a>'
        '<a class="btn ghost" href="upload">上传</a></div>'
        '<div style="margin-bottom:12px;font-size:13px">'
        '共 %d 首</div>'
        '<div class="mulist">%s</div>%s</div>%s%s'
        % (_esc(q), total, "".join(cards) or
           '<div class="muted">暂无音乐。</div>', pager, bar, js))
    return _page("音乐", body, extra_head=_MUSIC_CSS,
                 base_href=base_href)


_NOVELS_PER_PAGE = 20

_NOVELS_CSS = (
    "<style>"
    ".nvlist{display:flex;flex-direction:column;gap:8px}"
    ".nvrow{display:block;background:#171c27;border:1px solid #242c3f;"
    "border-radius:11px;padding:11px 13px;text-decoration:none}"
    ".nvrow:hover{border-color:#3a4a6b}"
    ".nvt{display:block;font-size:15px;line-height:1.45;color:#e8ebf1}"
    ".nvm{display:block;font-size:12.5px;color:#8b94a7;"
    "margin-top:3px;line-height:1.65}"
    ".pager{display:flex;gap:8px;align-items:center;"
    "flex-wrap:wrap;margin-top:14px}"
    ".pager a{background:#232a3a;border-radius:8px;"
    "padding:7px 12px;text-decoration:none;font-size:13.5px}"
    ".pgcur{background:#2f6fd0;color:#fff;border-radius:8px;"
    "padding:7px 12px;font-size:13.5px}"
    "</style>")


def _split_summary(stats):
    """One-line split composition for a novel, from its
    split_stats dict. 'AI 定界 N 章' leads when AI settled
    any title; a split settled by rules alone reads '纯规则'."""
    if not isinstance(stats, dict):
        return ""
    ai = stats.get("ai") or 0
    rule = stats.get("rule") or 0
    fixed = stats.get("fixed") or 0
    mech = stats.get("mechanical") or 0
    rec = stats.get("recovered") or 0
    if not (ai or rule or fixed or mech):
        return ""
    if rule and not (ai or fixed or mech) and not rec:
        return "纯规则"
    segs = []
    if ai:
        segs.append("AI 定界 %d 章" % ai)
    if rule:
        segs.append("规则 %d 章" % rule)
    if fixed:
        segs.append("固定 %d 章" % fixed)
    if mech:
        segs.append("机械 %d 段" % mech)
    if rec:
        segs.append("找回章首 %d 章" % rec)
    return " · ".join(segs)


def page_novels(params, base_href=""):
    q = (params.get("q") or "").strip()
    try:
        page = int(params.get("p") or 1)
    except Exception:
        page = 1
    where = ["f.kind = 'novel'"]
    args = []
    if q:
        where.append("(e.title LIKE ? OR e.tags LIKE ?)")
        like = "%" + q + "%"
        args += [like, like]
    wsql = " AND ".join(where)
    with _db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM entries e "
            "JOIN files f ON f.hash = e.hash WHERE " + wsql,
            args).fetchone()["c"]
        pages = max(1, (total + _NOVELS_PER_PAGE - 1)
                    // _NOVELS_PER_PAGE)
        page = min(max(1, page), pages)
        rows = conn.execute(
            "SELECT e.id AS e_id, e.title AS e_title, "
            "e.created_at AS created, "
            "m.char_count AS chars, m.chapters AS chaps, "
            "m.split_stats AS stats, "
            "r.chapter AS rchap, r.percent AS pct FROM entries e "
            "JOIN files f ON f.hash = e.hash "
            "LEFT JOIN novel_meta m ON m.hash = e.hash "
            "LEFT JOIN novel_read r ON r.entry_id = e.id WHERE "
            + wsql + " ORDER BY e.id DESC LIMIT ? OFFSET ?",
            args + [_NOVELS_PER_PAGE,
                    (page - 1) * _NOVELS_PER_PAGE]).fetchall()
    marks = _note_mark_map([r["e_id"] for r in rows])
    items = []
    for r in rows:
        try:
            chlist = json.loads(r["chaps"] or "[]")
        except Exception:
            chlist = []
        nch = len(chlist)
        try:
            stats = json.loads(r["stats"] or "")
        except Exception:
            stats = None
        if not isinstance(stats, dict):
            ai_n = sum(1 for c in chlist
                       if isinstance(c, dict) and c.get("ai"))
            stats = {"ai": ai_n} if ai_n else None
        bits = [_fmt_wchars(r["chars"] or 0), "%d 章" % nch]
        summ = _split_summary(stats)
        if summ:
            if (stats.get("ai") or 0) and stats.get("model"):
                summ += " · 模型 " + str(stats["model"])
            bits.append(summ)
        rchap = r["rchap"]
        pct = r["pct"] or 0
        if rchap is not None and ((rchap or 0) > 0 or pct > 0):
            seg = "读到第 %d 章" % ((rchap or 0) + 1)
            if pct:
                seg += " · 已读 %.0f%%" % pct
            bits.append(seg)
        if r["created"]:
            bits.append("入库 " + time.strftime(
                "%Y-%m-%d", time.localtime(r["created"])))
        stt = marks.get(r["e_id"])
        acts = ['<div class="nvacts">']
        acts.append(
            '<button type="button" class="nv-mark%s" '
            'data-eid="%d" data-act="candidate">%s</button>'
            % (" cur" if stt == "candidate" else "",
               r["e_id"],
               "已收藏" if stt == "candidate" else "收藏"))
        acts.append(
            '<button type="button" class="nv-mark ghost%s" '
            'data-eid="%d" data-act="dropped">%s</button>'
            % (" cur" if stt == "dropped" else "",
               r["e_id"],
               "已拉黑" if stt == "dropped" else "黑名单"))
        acts.append(
            '<span class="nvst"%s>笔记 · %s</span>'
            % ("" if stt else ' style="display:none"',
               _esc(_NOTE_STATUS_LABEL.get(stt, ""))))
        acts.append('</div>')
        items.append(
            '<div class="nvrow">'
            '<a class="nvmain" href="read/%d">'
            '<span class="nvt">%s</span>'
            '<span class="nvm">%s</span></a>%s</div>'
            % (r["e_id"], _esc(r["e_title"]),
               _esc(" · ".join(bits)), "".join(acts)))

    def _plink(n, label):
        u = "novels?p=%d" % n
        if q:
            u += "&q=" + urllib.parse.quote(q)
        return '<a href="%s">%s</a>' % (u, label)

    pager = ""
    if total:
        nav = []
        if page > 1:
            nav.append(_plink(page - 1, "← 上一页"))
        nums = sorted(set(
            [1, pages] + [n for n in range(page - 2, page + 3)
                          if 1 <= n <= pages]))
        last = 0
        for n in nums:
            if n - last > 1:
                nav.append('<span class="muted">…</span>')
            if n == page:
                nav.append('<span class="pgcur">%d</span>' % n)
            else:
                nav.append(_plink(n, "%d" % n))
            last = n
        if page < pages:
            nav.append(_plink(page + 1, "下一页 →"))
        pager = ('<div class="pager">%s</div>'
                 '<div class="muted" style="margin-top:6px">'
                 '第 %d / %d 页 · 共 %d 本</div>'
                 % (" ".join(nav), page, pages, total))
    body = (
        '<div class="topbar"><h1>小说</h1>'
        '<form class="search" method="get">'
        '<input type="search" name="q" value="%s" placeholder="搜索书名/标签">'
        '</form>'
        '<a class="btn ghost" href="./">← 资源库</a>'
        '<a class="btn ghost" href="upload">上传</a>'
        '<a class="btn ghost" href="notes">备注页</a></div>'
        '<div class="nvlist">%s</div>%s%s'
        % (_esc(q), "".join(items) or
           '<div class="muted">暂无小说。上传 .txt 即可入库。</div>',
           pager, _NOVELS_MARK_JS))
    return _page("小说", body, extra_head=_NOVELS_CSS + _MARK_CSS,
                 base_href=base_href)



# ------------------------------------------------------------ Novel notes
# Standalone notebook, deliberately NOT bound to the library:
# novel_notes has no foreign key to entries, so deleting a book
# never touches a note, and books that were never imported can
# be noted too. Statuses follow the user's four-pool frame.
# unrated is a legitimate permanent state: no badges, no
# progress bars, no nagging copy anywhere on the page. The
# candidate/reading caps are soft hints only, never a save gate.

_NOTE_STATUSES = (
    ("unrated", "未评估"),
    ("candidate", "候选"),
    ("reading", "在读"),
    ("favorite", "精品"),
    ("dropped", "不再推荐"),
)
_NOTE_STATUS_LABEL = dict(_NOTE_STATUSES)
_NOTE_LIKE_TAGS = ("人物塑造", "世界观", "节奏", "情绪体验")
_NOTE_CAPS = {"candidate": 30, "reading": 3}
_NOTES_PER_PAGE = 20

_NOTES_CSS = (
    "<style>"
    ".ntchips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}"
    ".ntchip{background:#232a3a;border-radius:999px;padding:6px 13px;"
    "text-decoration:none;font-size:13.5px;color:#c6ccd9}"
    ".ntchip.on{background:#2f6fd0;color:#fff}"
    ".ntcard{background:#171c27;border:1px solid #242c3f;"
    "border-radius:12px;padding:13px 15px;margin-bottom:10px}"
    ".ntcard .nt-t{font-size:16px;font-weight:600;color:#e8ebf1;"
    "line-height:1.45}"
    ".ntcard .nt-t a{color:#7ea6e0;text-decoration:none}"
    ".ntcard .nt-m{font-size:12.5px;color:#8b94a7;margin-top:3px;"
    "line-height:1.7}"
    ".ntcard .nt-note{font-size:14px;line-height:1.75;"
    "color:#d6dcea;margin-top:7px;white-space:pre-wrap;"
    "word-break:break-word}"
    ".ntst{display:inline-block;border-radius:5px;padding:1px 8px;"
    "font-size:12px;margin-left:8px;vertical-align:2px}"
    ".st-unrated{background:#2c3446;color:#aeb8cc}"
    ".st-candidate{background:#274b73;color:#a9ccff}"
    ".st-reading{background:#6b5410;color:#ffd97a}"
    ".st-favorite{background:#5d2a5e;color:#f0b6f5}"
    ".st-dropped{background:#4a2323;color:#ff9c9c}"
    ".ntlike{display:inline-block;background:#1d2b1f;"
    "color:#9fd49a;border-radius:5px;padding:1px 7px;"
    "font-size:12px;margin-right:5px}"
    ".ntacts{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}"
    ".ntacts button{padding:7px 13px;font-size:13px}"
    ".ntacts button.cur{outline:2px solid #7ea6e0}"
    ".ntform label{display:block;font-size:13px;color:#8b94a7;"
    "margin:10px 0 4px}"
    ".ntform input[type=text], .ntform textarea,"
    " .ntform select{width:100%}"
    ".ntform textarea{min-height:64px;resize:vertical}"
    ".ntlikes{display:flex;gap:14px;flex-wrap:wrap;margin-top:4px}"
    ".ntlikes label{display:inline-flex;gap:5px;"
    "align-items:center;margin:0;color:#d6dcea;font-size:14px}"
    "</style>")


def _note_get(note_id):
    try:
        nid = int(note_id)
    except Exception:
        return None
    with _db() as conn:
        return conn.execute(
            "SELECT * FROM novel_notes WHERE id = ?",
            (nid,)).fetchone()


def _note_clean_fields(data):
    status = (data.get("status") or "unrated").strip()
    if status not in _NOTE_STATUS_LABEL:
        status = "unrated"
    tags = data.get("like_tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [t for t in tags if t in _NOTE_LIKE_TAGS]
    return {
        "title": (data.get("title") or "").strip()[:200],
        "author": (data.get("author") or "").strip()[:100],
        "status": status,
        "note": (data.get("note") or "").strip()[:4000],
        "dislike_reason": (data.get("dislike_reason")
                           or "").strip()[:2000],
        "like_tags": json.dumps(tags, ensure_ascii=False),
        "like_text": (data.get("like_text") or "").strip()[:2000],
        "link": (data.get("link") or "").strip()[:1000],
    }


def _note_public(r):
    try:
        tags = json.loads(r["like_tags"] or "[]")
    except Exception:
        tags = []
    if not isinstance(tags, list):
        tags = []
    return {
        "id": r["id"], "title": r["title"],
        "author": r["author"], "status": r["status"],
        "status_label": _NOTE_STATUS_LABEL.get(
            r["status"], r["status"]),
        "note": r["note"],
        "dislike_reason": r["dislike_reason"],
        "like_tags": tags, "like_text": r["like_text"],
        "link": r["link"], "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }



def _note_read_id(link):
    """Entry id embedded in a reader-page link, else None. Lets the
    same book match its note even when the stored link carries a
    different host/prefix than the current visit."""
    clean = (link or "").split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"(?:^|/)read/(\d+)$", clean)
    return int(m.group(1)) if m else None


def _note_by_link(link):
    """Existing note for a link: exact match first, then same
    reader entry id. Manual notes without a link never match."""
    if not link:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM novel_notes WHERE link = ? "
            "ORDER BY id LIMIT 1", (link,)).fetchone()
        if row is not None:
            return row
        rid = _note_read_id(link)
        if rid is None:
            return None
        for r in conn.execute(
                "SELECT * FROM novel_notes WHERE link != '' "
                "ORDER BY id").fetchall():
            if _note_read_id(r["link"]) == rid:
                return r
    return None


def _note_mark_map(entry_ids):
    """{entry_id: note status} for books that already have a note,
    matched through the reader link stored on the note."""
    want = set()
    for i in entry_ids:
        try:
            want.add(int(i))
        except Exception:
            pass
    out = {}
    if not want:
        return out
    with _db() as conn:
        for r in conn.execute(
                "SELECT * FROM novel_notes WHERE link != '' "
                "ORDER BY id").fetchall():
            rid = _note_read_id(r["link"])
            if rid in want and rid not in out:
                out[rid] = r["status"]
    return out


_MARK_CSS = (
    "<style>"
    ".nvmain{display:block;text-decoration:none}"
    ".nvacts{display:flex;gap:8px;align-items:center;"
    "margin-top:9px}"
    ".nv-mark{padding:6px 13px;font-size:13px}"
    ".nv-mark.cur{outline:2px solid #7ea6e0}"
    ".nvst{font-size:12.5px;color:#8b94a7}"
    ".nvtoast{position:fixed;left:50%;bottom:26px;"
    "transform:translateX(-50%);background:#2f6fd0;color:#fff;"
    "padding:9px 18px;border-radius:999px;font-size:14px;"
    "display:none;z-index:60;"
    "box-shadow:0 4px 18px rgba(0,0,0,.4)}"
    ".bsrow{display:flex;gap:8px;align-items:center;"
    "padding:9px 0;border-bottom:1px solid #242c3f}"
    ".bsrow:last-child{border-bottom:none}"
    ".bsinfo{flex:1;min-width:0}"
    ".bst{font-size:15px;color:#e8ebf1;line-height:1.4}"
    ".bs-mark{padding:6px 13px;font-size:13px}"
    ".bs-mark.cur{outline:2px solid #7ea6e0}"
    ".ntdet>summary{cursor:pointer;font-size:15.5px;"
    "font-weight:600;color:#e8ebf1;padding:2px 0}"
    ".ntdet .ntform{margin-top:6px}"
    ".nt-nedit{margin-top:7px;display:flex;gap:8px;"
    "align-items:center;flex-wrap:wrap}"
    ".nt-nbox{display:inline-flex;gap:8px;align-items:center}"
    ".nt-ninput{min-width:220px}"
    "</style>")

_NOVELS_MARK_JS = (
    '<script>'
    'function nvToast(t){let el=document.getElementById'
    '("nvtoast");if(!el){el=document.createElement("div");'
    'el.id="nvtoast";el.className="nvtoast";'
    'document.body.appendChild(el);}'
    'el.textContent=t;el.style.display="block";'
    'clearTimeout(el._t);'
    'el._t=setTimeout(function(){el.style.display="none";}'
    ',1800);}'
    'document.querySelectorAll(".nv-mark").forEach('
    'function(b){b.onclick=async function(){'
    'const eid=b.dataset.eid;'
    'const row=b.closest(".nvrow");'
    'const title=row.querySelector(".nvt").textContent;'
    'const link=new URL("read/"+eid,location.href).href;'
    'const r=await fetch("api/notes/mark",{method:"POST",'
    'headers:{"Content-Type":"application/json"},'
    'body:JSON.stringify({title:title,link:link,'
    'status:b.dataset.act})});'
    'const j=await r.json();'
    'if(!j.ok){nvToast(j.message||"失败");return;}'
    'row.querySelectorAll(".nv-mark").forEach(function(x){'
    'const on=x.dataset.act===j.status;'
    'x.classList.toggle("cur",on);'
    'x.textContent=on?(j.status==="candidate"?"已收藏"'
    ':"已拉黑"):(x.dataset.act==="candidate"?"收藏"'
    ':"黑名单");});'
    'const st=row.querySelector(".nvst");'
    'if(st){st.textContent="笔记 · "+j.status_label;'
    'st.style.display="";}'
    'nvToast(j.status==="candidate"?"已收藏到备注页"'
    ':"已加入黑名单");};});'
    '</script>')


def page_notes(params, base_href=""):
    q = (params.get("q") or "").strip()
    st = (params.get("st") or "").strip()
    if st not in _NOTE_STATUS_LABEL:
        st = ""
    try:
        page = int(params.get("p") or 1)
    except Exception:
        page = 1
    where = []
    args = []
    if st:
        where.append("status = ?")
        args.append(st)
    if q:
        where.append("(title LIKE ? OR note LIKE ?)")
        like = "%" + q + "%"
        args += [like, like]
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    with _db() as conn:
        counts = {k: 0 for k, _ in _NOTE_STATUSES}
        for r in conn.execute(
                "SELECT status, COUNT(*) c FROM novel_notes "
                "GROUP BY status"):
            if r["status"] in counts:
                counts[r["status"]] = r["c"]
        total = conn.execute(
            "SELECT COUNT(*) c FROM novel_notes" + wsql,
            args).fetchone()["c"]
        pages = max(1, (total + _NOTES_PER_PAGE - 1)
                    // _NOTES_PER_PAGE)
        page = min(max(1, page), pages)
        rows = conn.execute(
            "SELECT * FROM novel_notes" + wsql
            + " ORDER BY updated_at DESC, id DESC"
            " LIMIT ? OFFSET ?",
            args + [_NOTES_PER_PAGE,
                    (page - 1) * _NOTES_PER_PAGE]).fetchall()

    def _nurl(stv, pv):
        parts = []
        if stv:
            parts.append("st=" + stv)
        if q:
            parts.append("q=" + urllib.parse.quote(q))
        if pv and pv > 1:
            parts.append("p=%d" % pv)
        return "notes" + ("?" + "&".join(parts) if parts else "")

    chips = ['<a class="ntchip%s" href="%s">全部（%d）</a>'
             % (" on" if not st else "", _nurl("", 0),
                sum(counts.values()))]
    for k, lab in _NOTE_STATUSES:
        chips.append('<a class="ntchip%s" href="%s">%s（%d）</a>'
                     % (" on" if st == k else "", _nurl(k, 0),
                        lab, counts[k]))

    hints = []
    for key in ("candidate", "reading"):
        cap = _NOTE_CAPS[key]
        if counts[key] > cap:
            hints.append("%s已有 %d 本（建议上限 %d）"
                         % (_NOTE_STATUS_LABEL[key],
                            counts[key], cap))
    hint_html = ""
    if hints:
        hint_html = ('<div class="muted" style="margin-bottom:10px">'
                     + "；".join(hints)
                     + "。只是温和提醒，不影响保存。</div>")

    cards = []
    for r in rows:
        pub = _note_public(r)
        title_html = _esc(pub["title"])
        if pub["link"]:
            title_html = ('<a href="%s" target="_blank" '
                          'rel="noopener">%s</a>'
                          % (_esc(pub["link"]), title_html))
        meta = []
        if pub["author"]:
            meta.append("作者 " + _esc(pub["author"]))
        meta.append("记于 " + time.strftime(
            "%Y-%m-%d", time.localtime(pub["created_at"] or 0)))
        if (pub["updated_at"] or 0) > (pub["created_at"] or 0) + 1:
            meta.append("改于 " + time.strftime(
                "%Y-%m-%d", time.localtime(pub["updated_at"] or 0)))
        segs = ['<div class="nt-t">%s'
                '<span class="ntst st-%s">%s</span></div>'
                '<div class="nt-m">%s</div>'
                % (title_html, _esc(pub["status"]),
                   _esc(pub["status_label"]),
                   " · ".join(meta))]
        segs.append(
            '<div class="nt-note" data-nv="%d"%s>%s</div>'
            % (pub["id"],
               "" if pub["note"] else ' style="display:none"',
               _esc(pub["note"])))
        segs.append(
            '<div class="nt-nedit">'
            '<button type="button" class="ghost nt-nbtn" '
            'data-id="%d">%s</button>'
            '<span class="nt-nbox" style="display:none">'
            '<input type="text" class="nt-ninput" '
            'data-id="%d" value="%s" placeholder="一句话备注">'
            '<button type="button" class="nt-nsave" '
            'data-id="%d">保存</button></span></div>'
            % (pub["id"],
               "改一句话备注" if pub["note"] else "＋ 一句话备注",
               pub["id"], _esc(pub["note"]), pub["id"]))
        if pub["like_tags"] or pub["like_text"]:
            seg = "".join('<span class="ntlike">%s</span>'
                          % _esc(t) for t in pub["like_tags"])
            if pub["like_text"]:
                seg += _esc(pub["like_text"])
            segs.append('<div class="nt-m" style="margin-top:7px">'
                        '喜欢的理由：%s</div>' % seg)
        if pub["dislike_reason"]:
            segs.append('<div class="nt-m" style="margin-top:4px">'
                        '不喜欢的原因：%s</div>'
                        % _esc(pub["dislike_reason"]))
        vbtns = []
        for v, lab in (("reading", "想继续"),
                       ("candidate", "暂时搁置"),
                       ("dropped", "不想再读")):
            vbtns.append(
                '<button type="button" class="ghost nt-verdict%s" '
                'data-id="%d" data-v="%s">%s</button>'
                % (" cur" if pub["status"] == v else "",
                   pub["id"], v, lab))
        segs.append(
            '<div class="ntacts">' + "".join(vbtns)
            + '<button type="button" class="ghost nt-edit" '
            'data-id="%d">编辑</button>'
            '<button type="button" class="ghost nt-del" '
            'data-id="%d">删除</button></div>' % (pub["id"],
                                                 pub["id"]))
        cards.append('<div class="ntcard">' + "".join(segs)
                     + '</div>')

    pager = ""
    if total:
        nav = []
        if page > 1:
            nav.append('<a href="%s">← 上一页</a>'
                       % _nurl(st, page - 1))
        nums = sorted(set(
            [1, pages] + [n for n in range(page - 2, page + 3)
                          if 1 <= n <= pages]))
        last = 0
        for n in nums:
            if n - last > 1:
                nav.append('<span class="muted">…</span>')
            if n == page:
                nav.append('<span class="pgcur">%d</span>' % n)
            else:
                nav.append('<a href="%s">%d</a>'
                           % (_nurl(st, n), n))
            last = n
        if page < pages:
            nav.append('<a href="%s">下一页 →</a>'
                       % _nurl(st, page + 1))
        pager = ('<div class="pager">%s</div>'
                 '<div class="muted" style="margin-top:6px">'
                 '第 %d / %d 页 · 共 %d 条</div>'
                 % (" ".join(nav), page, pages, total))

    status_opts = "".join(
        '<option value="%s">%s</option>' % (k, lab)
        for k, lab in _NOTE_STATUSES)
    like_boxes = "".join(
        '<label><input type="checkbox" class="nf-like" '
        'value="%s">%s</label>' % (t, t) for t in _NOTE_LIKE_TAGS)
    prefill_note = ""
    if (params.get("title") or params.get("link")):
        prefill_note = ('<div class="muted" style="margin-top:6px">'
                        '已从阅读页带入书名与链接，存不存随你。'
                        '</div>')

    form = (
        '<div class="ntform"><h2 id="nf-head">新增笔记</h2>'
        '<input type="hidden" id="nf-id">'
        '<label>书名（必填）</label>'
        '<input type="text" id="nf-title" value="%s" '
        'placeholder="书名">'
        '<label>作者</label>'
        '<input type="text" id="nf-author" placeholder="可选">'
        '<label>状态</label>'
        '<select id="nf-status">%s</select>'
        '<label>喜欢的理由（可多选）</label>'
        '<div class="ntlikes">%s</div>'
        '<label>喜欢的理由（补充）</label>'
        '<input type="text" id="nf-liketext" placeholder="可选">'
        '<label>不喜欢的原因</label>'
        '<input type="text" id="nf-dislike" placeholder="可选">'
        '<label>备注</label>'
        '<textarea id="nf-note" placeholder="随便记，不记也行">'
        '</textarea>'
        '<label>链接</label>'
        '<input type="text" id="nf-link" value="%s" '
        'placeholder="可选，如阅读页地址">'
        % (_esc(params.get("title") or ""), status_opts,
           like_boxes, _esc(params.get("link") or ""))
        + prefill_note
        + '<div style="display:flex;gap:8px;margin-top:12px;'
        'align-items:center">'
        '<button type="button" id="nf-save">保存</button>'
        '<button type="button" class="ghost" id="nf-cancel" '
        'style="display:none">取消编辑</button>'
        '<span class="msg" id="nf-msg"></span></div></div>')
    form = ('<details class="panel ntdet" id="nf-details"%s>'
            '<summary>手动添加（库里没有的书）</summary>'
            % (" open" if prefill_note else "")
            + form + '</details>')

    searchadd = (
        '<div class="panel"><h2>从书库搜索添加</h2>'
        '<div class="muted" style="margin-bottom:10px">'
        '先在书库里找到书，一键收藏或拉黑，自动记到这里，'
        '不用手填。</div>'
        '<div style="display:flex;gap:8px">'
        '<input type="search" id="bs-q" '
        'placeholder="输入书名关键词" style="flex:1">'
        '<button type="button" id="bs-go">搜索</button></div>'
        '<div id="bs-results" style="margin-top:6px"></div>'
        '<div id="bstoast" class="nvtoast"></div></div>')

    blind = (
        '<div class="panel"><h2>随机盲盒</h2>'
        '<div class="muted" style="margin-bottom:10px">'
        '从「未评估」和「候选」里随机抽一条，随手给它'
        '一个裁决，或者不理它。</div>'
        '<button type="button" id="bb-draw">随机抽一本</button>'
        '<div id="bb-box" style="margin-top:12px"></div></div>')

    js = (
        '<script>'
        'async function postJ(u,b){const r=await fetch(u,'
        '{method:"POST",headers:{"Content-Type":'
        '"application/json"},body:JSON.stringify(b)});'
        'return r.json();}'
        'function escHtml(s){const d=document.createElement'
        '("div");d.textContent=s||"";return d.innerHTML;}'
        'const nfMsg=document.getElementById("nf-msg");'
        'function nfReset(){'
        'document.getElementById("nf-id").value="";'
        'document.getElementById("nf-title").value="";'
        'document.getElementById("nf-author").value="";'
        'document.getElementById("nf-status").value="unrated";'
        'document.getElementById("nf-liketext").value="";'
        'document.getElementById("nf-dislike").value="";'
        'document.getElementById("nf-note").value="";'
        'document.getElementById("nf-link").value="";'
        'document.querySelectorAll(".nf-like").forEach('
        'function(c){c.checked=false;});'
        'document.getElementById("nf-head").textContent='
        '"新增笔记";'
        'document.getElementById("nf-save").textContent="保存";'
        'document.getElementById("nf-cancel").style.display'
        '="none";}'
        'document.getElementById("nf-cancel").onclick=nfReset;'
        'document.getElementById("nf-save").onclick='
        'async function(){'
        'const likes=[];'
        'document.querySelectorAll(".nf-like").forEach('
        'function(c){if(c.checked)likes.push(c.value);});'
        'const d={title:document.getElementById("nf-title")'
        '.value,author:document.getElementById("nf-author")'
        '.value,status:document.getElementById("nf-status")'
        '.value,like_tags:likes,'
        'like_text:document.getElementById("nf-liketext")'
        '.value,'
        'dislike_reason:document.getElementById("nf-dislike")'
        '.value,note:document.getElementById("nf-note").value,'
        'link:document.getElementById("nf-link").value};'
        'const idv=document.getElementById("nf-id").value;'
        'const j=idv?await postJ("api/notes/update",'
        'Object.assign({id:parseInt(idv,10)},d))'
        ':await postJ("api/notes",d);'
        'nfMsg.textContent=j.ok?"已保存。":'
        '("失败：" + (j.message || ""));'
        'if(j.ok)setTimeout(function(){location.href='
        '"notes";},600);};'
        'document.querySelectorAll(".nt-edit").forEach('
        'function(b){b.onclick=async function(){'
        'const r=await fetch("api/notes/get?id="+b.dataset.id);'
        'const j=await r.json();if(!j.ok||!j.note){'
        'alert(j.message||"读取失败");return;}'
        'const n=j.note;nfReset();'
        'document.getElementById("nf-id").value=String(n.id);'
        'document.getElementById("nf-title").value=n.title;'
        'document.getElementById("nf-author").value=n.author;'
        'document.getElementById("nf-status").value=n.status;'
        'document.querySelectorAll(".nf-like").forEach('
        'function(c){c.checked=n.like_tags.indexOf(c.value)'
        '>=0;});'
        'document.getElementById("nf-liketext").value='
        'n.like_text;'
        'document.getElementById("nf-dislike").value='
        'n.dislike_reason;'
        'document.getElementById("nf-note").value=n.note;'
        'document.getElementById("nf-link").value=n.link;'
        'document.getElementById("nf-head").textContent='
        '"编辑笔记";'
        'document.getElementById("nf-save").textContent='
        '"保存修改";'
        'document.getElementById("nf-cancel").style.display'
        '="";'
        'document.getElementById("nf-details").open=true;'
        'document.getElementById("nf-head").scrollIntoView();'
        '};});'
        'document.querySelectorAll(".nt-verdict").forEach('
        'function(b){b.onclick=async function(){'
        'const j=await postJ("api/notes/verdict",'
        '{id:parseInt(b.dataset.id,10),verdict:b.dataset.v});'
        'if(j.ok)location.reload();'
        'else alert(j.message||"失败");};});'
        'document.querySelectorAll(".nt-del").forEach('
        'function(b){let armed=false,t=null;'
        'b.onclick=async function(){'
        'if(!armed){armed=true;b.textContent="确认删除？";'
        'b.classList.add("danger");'
        't=setTimeout(function(){armed=false;'
        'b.textContent="删除";b.classList.remove("danger");'
        '},3000);return;}'
        'clearTimeout(t);'
        'const j=await postJ("api/notes/delete",'
        '{id:parseInt(b.dataset.id,10)});'
        'if(j.ok)location.reload();'
        'else alert(j.message||"删除失败");};});'
        'async function bbDraw(){'
        'const r=await fetch("api/notes/random");'
        'const j=await r.json();'
        'const box=document.getElementById("bb-box");'
        'if(!j.ok||!j.note){box.innerHTML='
        '"<div class=\\"muted\\">未评估和候选里暂时没有'
        '可抽的了。</div>";return;}'
        'const n=j.note;'
        'let h="<div class=\\"ntcard\\" style=\\"margin:0\\">'
        '<div class=\\"nt-t\\">"+escHtml(n.title)+'
        '"<span class=\\"ntst st-"+n.status+"\\">"+'
        'escHtml(n.status_label)+"</span></div>";'
        'if(n.author)h+="<div class=\\"nt-m\\">作者 "+'
        'escHtml(n.author)+"</div>";'
        'if(n.note)h+="<div class=\\"nt-note\\">"+'
        'escHtml(n.note)+"</div>";'
        'h+="<div class=\\"ntacts\\">'
        '<button type=\\"button\\" data-v=\\"reading\\">'
        '想继续</button>'
        '<button type=\\"button\\" class=\\"ghost\\" '
        'data-v=\\"candidate\\">暂时搁置</button>'
        '<button type=\\"button\\" class=\\"ghost\\" '
        'data-v=\\"dropped\\">不想再读</button></div></div>";'
        'box.innerHTML=h;'
        'box.querySelectorAll("button[data-v]").forEach('
        'function(b){b.onclick=async function(){'
        'const j2=await postJ("api/notes/verdict",'
        '{id:n.id,verdict:b.dataset.v});'
        'if(j2.ok)bbDraw();'
        'else alert(j2.message||"失败");};});}'
        'document.getElementById("bb-draw").onclick=bbDraw;'
        'document.querySelectorAll(".nt-nbtn").forEach('
        'function(b){b.onclick=function(){'
        'const card=b.closest(".ntcard");'
        'const box=card.querySelector(".nt-nbox");'
        'box.style.display="";'
        'box.querySelector(".nt-ninput").focus();};});'
        'document.querySelectorAll(".nt-nsave").forEach('
        'function(b){b.onclick=async function(){'
        'const card=b.closest(".ntcard");'
        'const inp=card.querySelector(".nt-ninput");'
        'const j=await postJ("api/notes/note",'
        '{id:parseInt(b.dataset.id,10),note:inp.value});'
        'if(!j.ok){alert(j.message||"保存失败");return;}'
        'const v=card.querySelector("[data-nv]");'
        'const t=inp.value.trim();'
        'v.textContent=t;v.style.display=t?"":"none";'
        'card.querySelector(".nt-nbtn").textContent='
        't?"改一句话备注":"＋ 一句话备注";'
        'card.querySelector(".nt-nbox").style.display'
        '="none";};});'
        'const BS={};let bsT=null;'
        'function bsToast(t){'
        'const el=document.getElementById("bstoast");'
        'el.textContent=t;el.style.display="block";'
        'clearTimeout(bsT);'
        'bsT=setTimeout(function(){'
        'el.style.display="none";},1800);}'
        'function bsRowState(row,st,lab){'
        'row.querySelectorAll(".bs-mark").forEach('
        'function(x){const on=x.dataset.act===st;'
        'x.classList.toggle("cur",on);'
        'x.textContent=on?(st==="candidate"?"已收藏"'
        ':"已拉黑"):(x.dataset.act==="candidate"?"收藏"'
        ':"黑名单");});'
        'let bd=row.querySelector(".ntst");'
        'if(st){if(!bd){bd=document.createElement("span");'
        'row.insertBefore(bd,row.querySelector(".bs-mark"));'
        '}bd.className="ntst st-"+st;bd.textContent=lab;}'
        'else if(bd){bd.remove();}}'
        'async function bsSearch(){'
        'const q=document.getElementById("bs-q").value.trim();'
        'const box=document.getElementById("bs-results");'
        'if(!q){box.innerHTML="";return;}'
        'const r=await fetch("api/notes/booksearch?q="+encodeURIComponent(q));'
        'const j=await r.json();'
        'box.innerHTML="";'
        'if(!j.ok){box.innerHTML="<div class=muted>搜索失败。</div>";return;}'
        'if(!j.books.length){box.innerHTML="<div class=muted>书库里没找到这本书，可以展开下面的手动添加。</div>";return;}'
        'j.books.forEach(function(bk){'
        'BS[bk.entry_id]=bk;'
        'const row=document.createElement("div");row.className="bsrow";'
        'const info=document.createElement("div");info.className="bsinfo";'
        'const t=document.createElement("div");t.className="bst";t.textContent=bk.title;'
        'const m=document.createElement("div");m.className="nt-m";m.textContent=bk.chars+" · "+bk.chapters+" 章";'
        'info.appendChild(t);info.appendChild(m);row.appendChild(info);'
        'if(bk.status){const bd=document.createElement("span");bd.className="ntst st-"+bk.status;bd.textContent=bk.status_label;row.appendChild(bd);}'
        'const b1=document.createElement("button");b1.type="button";b1.className="bs-mark"+(bk.status==="candidate"?" cur":"");b1.dataset.eid=bk.entry_id;b1.dataset.act="candidate";b1.textContent=bk.status==="candidate"?"已收藏":"收藏";b1.onclick=function(){bsMark(b1);};'
        'const b2=document.createElement("button");b2.type="button";b2.className="ghost bs-mark"+(bk.status==="dropped"?" cur":"");b2.dataset.eid=bk.entry_id;b2.dataset.act="dropped";b2.textContent=bk.status==="dropped"?"已拉黑":"黑名单";b2.onclick=function(){bsMark(b2);};'
        'row.appendChild(b1);row.appendChild(b2);box.appendChild(row);});}'
        'async function bsMark(b){'
        'const bk=BS[b.dataset.eid];if(!bk)return;'
        'const link=new URL("read/"+bk.entry_id,'
        'location.href).href;'
        'const j=await postJ("api/notes/mark",'
        '{title:bk.title,link:link,status:b.dataset.act});'
        'if(!j.ok){alert(j.message||"失败");return;}'
        'bk.status=j.status;'
        'bsRowState(b.closest(".bsrow"),j.status,'
        'j.status_label);'
        'bsToast(j.status==="candidate"'
        '?"已收藏到备注页":"已加入黑名单");}'
        'document.getElementById("bs-go").onclick=bsSearch;'
        'document.getElementById("bs-q").addEventListener('
        '"keydown",function(e){if(e.key==="Enter"){'
        'e.preventDefault();bsSearch();}});'
        '</script>')

    body = (
        '<div class="topbar"><h1>小说备注</h1>'
        '<form class="search" method="get">'
        '<input type="search" name="q" value="%s" '
        'placeholder="搜索书名/备注">'
        % _esc(q)
        + ('<input type="hidden" name="st" value="%s">'
           % _esc(st) if st else '')
        + '</form>'
        '<a class="btn ghost" href="./">← 资源库</a>'
        '<a class="btn ghost" href="novels">小说</a></div>'
        '<div class="ntchips">%s</div>%s%s%s%s%s%s%s'
        % ("".join(chips), hint_html, searchadd, blind, form,
           "".join(cards) or
           '<div class="muted">还没有笔记。从上方书库搜索添加，或展开手动添加。'
           '</div>',
           pager, js))
    return _page("小说备注", body, extra_head=_NOTES_CSS + _MARK_CSS,
                 base_href=base_href)


def page_reader(entry, base_href=""):
    body = (
        '<div class="topbar"><a class="btn ghost" href="novels">← 小说</a>'
        '<h1 id="bkTitle" style="font-size:17px">%s</h1>'
        '<select id="chsel" style="max-width:44vw"></select>'
        '<button type="button" class="ghost" id="fdec">A-</button>'
        '<button type="button" class="ghost" id="finc">A+</button>''<a class="btn ghost" id="noteLink" href="notes">记到备注页</a>' '<button type="button" class="ghost" id="mkFav">收藏</button>' '<button type="button" class="ghost" id="mkBan">黑名单</button></div>'
        '<div id="nmeta" class="muted" style="margin-bottom:10px"></div>'
        '<div id="novel" style="font-size:19px;line-height:1.95;'
        'max-width:760px;margin:0 auto;color:#dfe5f0"></div>'
        '<div style="display:flex;gap:10px;justify-content:center;'
        'margin:22px 0">'
        '<button type="button" id="prevch">← 上一章</button>'
        '<button type="button" id="nextch">下一章 →</button></div>'
        '<script>const EID=%d;let CH=[],CUR=0,TOTAL=0,READ=null,FS=19;'
        'try{FS=parseInt(localStorage.getItem("novelFont")||"19",10)||19;}'
        'catch(e){}'
        'const NOV=document.getElementById("novel");'
        'var NL=document.getElementById("noteLink");'
        'if(NL){NL.href="notes?title="+'
        'encodeURIComponent(document.getElementById("' 'bkTitle").textContent)+"&link="+'
        'encodeURIComponent(location.href.split("#")[0]' '.split("?")[0]);}'
                'var MKF=document.getElementById("mkFav"),'
        'MKB=document.getElementById("mkBan");'
        'function mkState(st){if(!MKF)return;'
        'MKF.textContent=st==="candidate"?"已收藏":"收藏";'
        'MKB.textContent=st==="dropped"?"已拉黑":"黑名单";'
        'MKF.style.outline=st==="candidate"'
        '?"2px solid #7ea6e0":"";'
        'MKB.style.outline=st==="dropped"'
        '?"2px solid #e07a7a":"";}'
        'async function mkSend(st){'
        'const link=location.href.split("#")[0]'
        '.split("?")[0];'
        'const j=await postJ2({title:document'
        '.getElementById("bkTitle").textContent,'
        'link:link,status:st});'
        'if(j&&j.ok)mkState(j.status);'
        'else alert((j&&j.message)||"失败");}'
        'async function postJ2(d){'
        'const r=await fetch("api/notes/mark",'
        '{method:"POST",headers:{"Content-Type":'
        '"application/json"},body:JSON.stringify(d)});'
        'return r.json();}'
        'if(MKF){MKF.onclick=function(){'
        'mkSend("candidate");};'
        'MKB.onclick=function(){mkSend("dropped");};'
        'fetch("api/notes/markstate?entry_id="+EID)'
        '.then(function(r){return r.json();})'
        '.then(function(j){if(j.ok)mkState(j.status||"");})'
        '.catch(function(){});}'
        'function applyFs(){NOV.style.fontSize=FS+"px";}'
        'document.getElementById("fdec").onclick=function(){'
        'FS=Math.max(14,FS-1);applyFs();'
        'try{localStorage.setItem("novelFont",FS);}catch(e){}};'
        'document.getElementById("finc").onclick=function(){'
        'FS=Math.min(30,FS+1);applyFs();'
        'try{localStorage.setItem("novelFont",FS);}catch(e){}};'
        'async function loadCh(i,frac){if(i<0||i>=CH.length)return;CUR=i;'
        'document.getElementById("chsel").value=String(i);'
        'const r=await fetch("api/novel/"+EID+"/chapter?i="+i);'
        'const j=await r.json();if(!j.ok)return;NOV.innerHTML="";'
        'const h=document.createElement("h2");h.textContent=CH[i].t;'
        'h.style.fontSize="1.25em";NOV.appendChild(h);'
        'j.text.split(/\\n+/).forEach(function(pa){if(!pa.trim())return;'
        'const p=document.createElement("p");p.textContent=pa;'
        'p.style.margin="0 0 .9em";p.style.textIndent="2em";'
        'NOV.appendChild(p);});'
        'if(frac&&frac>0){const y=frac*(document.documentElement.scrollHeight'
        '-window.innerHeight);window.scrollTo(0,Math.max(0,y));}'
        'else window.scrollTo(0,0);saveProg(frac||0);}'
        'let saving=false;'
        'async function saveProg(frac){if(saving||!CH.length)return;'
        'saving=true;'
        'try{const ch=CH[CUR];'
        'const off=Math.round(ch.o+(frac||0)*(ch.e-ch.o));'
        'const pct=TOTAL>0?Math.min(100,off/TOTAL*100):0;'
        'await fetch("api/novel/progress",{method:"POST",'
        'headers:{"Content-Type":"application/json"},'
        'body:JSON.stringify({entry_id:EID,chapter:CUR,offset:off,'
        'percent:pct})});}catch(e){}saving=false;}'
        'let tick=false;window.addEventListener("scroll",function(){'
        'if(tick)return;tick=true;setTimeout(function(){tick=false;'
        'const mx=document.documentElement.scrollHeight-window.innerHeight;'
        'saveProg(mx>0?window.scrollY/mx:0);},1800);});'
        'document.getElementById("prevch").onclick=function(){'
        'loadCh(CUR-1,0);};'
        'document.getElementById("nextch").onclick=function(){'
        'loadCh(CUR+1,0);};'
        'document.getElementById("chsel").onchange=function(ev){'
        'loadCh(parseInt(ev.target.value,10),0);};'
        '(async function(){applyFs();'
        'const r=await fetch("api/novel/"+EID);const j=await r.json();'
        'if(!j.ok){NOV.textContent=j.message||"加载失败";return;}'
        'CH=j.chapters;TOTAL=j.char_count||0;READ=j.read;'
        'document.getElementById("nmeta").textContent='
        '"共 "+CH.length+" 章 · "+TOTAL+" 字 · 编码 "+(j.encoding||"");'
        'const sel=document.getElementById("chsel");'
        'CH.forEach(function(c,ix){const o=document.createElement("option");'
        'o.value=String(ix);o.textContent=c.t;sel.appendChild(o);});'
        'let start=0,frac=0;'
        'if(READ){start=Math.min(READ.chapter||0,CH.length-1);'
        'const c=CH[start];'
        'frac=c&&c.e>c.o?Math.max(0,Math.min(1,'
        '(READ.offset-c.o)/(c.e-c.o))):0;}'
        'loadCh(start,frac);})();</script>'
        % (_esc(entry["title"]), entry["id"]))
    return _page(entry["title"], body, base_href=base_href)


def page_files(params, base_href=""):
    cat = (params.get("cat") or "").strip()
    q = (params.get("q") or "").strip()
    cats = _file_categories()
    with _db() as conn:
        counts = {}
        for r in conn.execute(
                "SELECT f.category AS category, COUNT(*) AS c "
                "FROM entries e JOIN files f ON f.hash = e.hash "
                "WHERE f.kind = 'file' GROUP BY f.category"):
            counts[r["category"] or "其他"] = r["c"]
        where = ["f.kind = 'file'"]
        args = []
        if cat:
            where.append("f.category = ?")
            args.append(cat)
        if q:
            where.append("(e.title LIKE ? OR e.tags LIKE ?)")
            like = "%" + q + "%"
            args += [like, like]
        rows = conn.execute(
            _ENTRY_COLS + " WHERE " + " AND ".join(where)
            + " ORDER BY e.id DESC LIMIT 300", args).fetchall()
    total_all = sum(counts.values())
    chips = ['<a class="tag" href="files">全部（%d）</a>' % total_all]
    for c in cats:
        n = counts.get(c["name"], 0)
        chips.append('<a class="tag" href="files?cat=%s">%s（%d）</a>'
                     % (urllib.parse.quote(c["name"]), _esc(c["name"]), n))
    opts = "".join('<option value="%s">%s</option>'
                   % (_esc(c["name"]), _esc(c["name"])) for c in cats)
    lines = []
    for r in rows:
        cur = _esc(r["category"] or "其他")
        sel = ('<select class="recat" data-id="%d">%s</select>'
               % (r["id"], opts)).replace(
                   'value="%s"' % cur, 'value="%s" selected' % cur, 1)
        lines.append(
            '<div class="panel" style="display:flex;gap:10px;'
            'align-items:center;flex-wrap:wrap;margin-bottom:8px">'
            '<a href="watch/%d" style="font-weight:600;color:#e8ecf4;'
            'text-decoration:none">%s</a>'
            '<span class="muted">.%s · %s · %s</span>%s'
            '<a class="btn ghost" href="file/%s">下载</a></div>'
            % (r["id"], _esc(r["title"]), _esc(r["ext"]),
               _fmt_size(r["size"] or 0),
               time.strftime("%Y-%m-%d", time.localtime(
                   r["created_at"] or 0)), sel, r["hash"]))
    js = ('<script>document.querySelectorAll(".recat").forEach(function(el){'
          'el.addEventListener("change",async function(){'
          'const r=await fetch("api/files/recategorize",{method:"POST",'
          'headers:{"Content-Type":"application/json"},'
          'body:JSON.stringify({id:parseInt(el.dataset.id,10),'
          'category:el.value})});const j=await r.json();'
          'if(j.ok){location.reload();}'
          'else{alert(j.message||"改类失败");location.reload();}});});'
          '</script>')
    body = (
        '<div class="topbar"><h1>文件</h1>'
        '<form class="search" method="get">'
        '<input type="search" name="q" value="%s" placeholder="搜索标题/标签">'
        '</form>'
        '<a class="btn ghost" href="./">← 资源库</a>'
        '<a class="btn ghost" href="upload">上传</a></div>'
        '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">'
        '%s</div>%s%s'
        % (_esc(q), "".join(chips),
           "".join(lines) or '<div class="muted">暂无文件。</div>', js))
    return _page("文件", body, base_href)


def page_wall(params, base_href=""):
    q = (params.get("q") or "").strip()
    dur = params.get("dur") or ""
    kind = params.get("kind") or ""
    markf = params.get("mark") or ""
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
    if markf == "liked":
        where.append("e.mark = 1")
    elif markf == "disliked":
        where.append("e.mark = 2")
    if kind in ("video", "image", "novel", "file"):
        where.append("f.kind = ?")
        args.append(kind)
    elif kind == "other":
        where.append("f.kind IN ('novel', 'file')")
    lib_param = (params.get("lib") or "").strip()
    lib_row = _library_by_id(lib_param) if lib_param else None
    if lib_row is not None and lib_row["kind"] == "video":
        where.append("e.library_id = ?")
        args.append(lib_row["id"])
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
               {"image": " · 图片", "novel": " · 小说",
                "file": " · 文件(.%s)" % (r["ext"] or "?")}.get(
                    r["kind"] or _kind(r["ext"]), "")
               + ((" · " + _esc(r["tags"])) if r["tags"] else "")
               + (" · ❤ 喜欢" if (r["mark"] or 0) == 1 else
                  " · 不喜欢" if (r["mark"] or 0) == 2 else "")))
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
    lib_tabs = ""
    vlibs = _libraries("video")
    if len(vlibs) > 1:
        tbits = ['<a class="tag" href="?kind=video">全部库</a>']
        for l in vlibs:
            mark = (" <strong>· 当前</strong>"
                    if lib_row is not None and lib_row["id"] == l["id"]
                    else "")
            tbits.append('<a class="tag" href="?kind=video&lib=%d">%s</a>%s'
                         % (l["id"], _esc(l["name"]), mark))
        lib_tabs = ('<div style="display:flex;gap:6px;flex-wrap:wrap;'
                    'margin-bottom:12px">' + "".join(tbits) + "</div>")
    body = (
        '<div class="topbar"><h1>资源库</h1>'
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
        '<option value="novel"%s>小说</option>'
        '<option value="file"%s>文件</option></select>'
        '<select name="mark">'
        '<option value="">全部标记</option>'
        '<option value="liked"%s>❤ 喜欢</option>'
        '<option value="disliked"%s>不喜欢</option></select>'
        '<button type="submit">搜索</button></form>'
        '<a class="btn ghost" href="./">总览</a>''<a class="btn" href="feed">刷视频</a>'
        '<a class="btn" href="upload">上传</a>'
        '<a class="btn ghost" href="admin">管理</a></div>'
        '<div style="margin-bottom:8px;font-size:13px">共 %d 个条目 %s</div>%s'
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
           ' selected' if kind == "novel" else '',
           ' selected' if kind == "file" else '',
           ' selected' if markf == "liked" else '',
           ' selected' if markf == "disliked" else '',
           total, ("· 标签：" + tag_links) if tag_links else "", lib_tabs,
           "".join(cards) or '<div class="empty">还没有视频。点右上角「上传」添加第一个。</div>',
           pager))
    return _page("视频", body, base_href=base_href)


def page_watch(entry, base_href=""):
    if entry is None:
        return None
    playable = (entry["f_ext"] or "") in PLAYABLE_EXTS
    kind = entry["f_kind"] or _kind(entry["f_ext"])
    if kind == "image":
        media_html = (
            '<img id="v" src="thumb/%s" alt="" '
            'style="width:100%%;border-radius:12px;background:#111">'
            % entry["hash"])
        note_html = ""
    elif kind == "novel":
        media_html = (
            '<div id="v" class="panel" style="text-align:center">'
            '<a class="btn" href="read/%d">📖 打开阅读</a>'
            '<div style="margin-top:8px;color:#9db2c8">txt 小说 · '
            '在线阅读带章节目录与进度记忆。</div></div>' % entry["id"])
        note_html = ""
    elif kind == "music":
        media_html = (
            '<div style="text-align:center">'
            '<img src="thumb/%s" alt="" '
            'style="max-width:240px;width:60%%;border-radius:12px;'
            'background:#111"></div>'
            '<audio id="v" controls preload="metadata" '
            'style="width:100%%;margin-top:10px"></audio>'
            % entry["hash"])
        note_html = ""
    elif kind == "file":
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
           _fmt_duration(entry["f_duration"])
           if kind in ("video", "music") else "—",
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
    if kind == "music":
        _mfields = (
            '<input type="text" id="m-artist" value="%s" '
            'placeholder="歌手" maxlength="200">'
            '<input type="text" id="m-album" value="%s" '
            'placeholder="专辑" maxlength="200">'
            % (_esc(entry["f_artist"] or ""),
               _esc(entry["f_album"] or "")))
        body = body.replace(
            '<input type="text" id="m-tags"',
            _mfields + '<input type="text" id="m-tags"', 1)
        body = body.replace(
            'tags:document.getElementById("m-tags").value,',
            'tags:document.getElementById("m-tags").value,'
            'artist:(document.getElementById("m-artist")||{}).value,'
            'album:(document.getElementById("m-album")||{}).value,',
            1)
        body = body.replace(
            '<a class="btn" id="dl"',
            ('歌手：%s · 专辑：%s<br>'
             % (_esc(entry["f_artist"] or "—"),
                _esc(entry["f_album"] or "—")))
            + '<a class="btn" id="dl"', 1)
    return _page(entry["title"], body, base_href=base_href)


UPLOAD_PAGE = r"""<div class="topbar"><a class="btn ghost" href="./">← 返回</a><h1>上传文件</h1></div>
<div class="panel" id="dz"><div style="display:grid;gap:9px">
<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<button type="button" id="pickf">选择文件</button>
<button type="button" id="pickd">选择文件夹</button>
<input type="file" id="files" multiple hidden>
<input type="file" id="dir" webkitdirectory hidden>
<span id="picked" style="font-size:13px;color:#8b94a7"></span>
</div>
<div style="color:#8b94a7;font-size:13px">也可以把文件或整个文件夹拖到这个面板里松手。文件夹上传会保留目录路径并自动变成标签（和 WebDAV 拖文件夹一致，如 trip/day1）；0 字节和系统文件（.DS_Store、Thumbs.db 等）会自动跳过。</div>
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span style="font-size:13px;color:#8b94a7">视频入库到</span><select id="libsel">__LIB_OPTIONS__</select><span style="font-size:12px;color:#8b94a7">（仅对视频生效；图片 / 音乐 / 小说 / 文件自动归类）</span></div>
<input type="text" id="tags" placeholder="标签（应用于本批，逗号分隔）" maxlength="200">
<input type="text" id="source" placeholder="来源（应用于本批）" maxlength="120">
<span><button id="go" type="button">开始上传</button> <span class="msg" id="msg"></span></span>
<div id="sum" style="display:none">
<div class="progress"><i id="sumbi"></i></div>
<div id="sumtx" style="font-size:13px;margin-top:5px"></div><div style="margin-top:6px"><button type="button" id="pausebtn" class="ghost">暂停全部</button></div><div id="alert" style="display:none;margin-top:8px;font-size:13.5px;color:#ffb86b;line-height:1.6"></div></div>
<div id="list-active" style="display:grid;gap:10px"></div>
<div id="list-queue" style="display:grid;gap:10px"></div>
<div id="queue-more" style="font-size:13px;color:#8b94a7;display:none"></div>
<details id="list-done-wrap" style="display:none">
<summary id="done-sum" style="font-size:13.5px;cursor:pointer;color:#c9ccd4"></summary>
<div id="list-done" style="display:grid;gap:10px;margin-top:8px"></div>
</details>
</div>
<p style="color:#8b94a7;font-size:12.5px;line-height:1.7">分片上传（8MB/片）：最多 3 个文件并行、每个文件 3 片并行，失败分片自动重试 3 次。速度为近几秒的实时速度，不是全程平均。文件先到服务器暂存区，后台算哈希去重、抽缩略图，再自动归档到谷歌硬盘并删除本地副本；同哈希重复上传只新增一个条目，不重复占谷歌空间。排队中的文件可点 ✕ 取消，上传中的也可中止（已传分片作废）。暂存区预算 __BUDGET__，归档跟不上时请稍后再传。</p></div>
<script>
const CH=8*1024*1024,PARF=3,PARC=3;
const elActive=document.getElementById("list-active");
const elQueue=document.getElementById("list-queue");
const elDone=document.getElementById("list-done");
const elDoneWrap=document.getElementById("list-done-wrap");
const elDoneSum=document.getElementById("done-sum");
const elQMore=document.getElementById("queue-more");
function layout(){
  let qShown=0,qHidden=0,doneN=0,okN=0,dupN=0,canN=0;
  for(const j of jobs){
    if(j.state==="ok"||j.state==="cancel"){
      elDone.appendChild(j.row);j.row.style.display="";
      doneN++;if(j.state==="ok"){if(j.dup)dupN++;else okN++;}
      else canN++;}
    else if(j.state==="queue"){
      elQueue.appendChild(j.row);
      if(qShown<5){j.row.style.display="";qShown++;}
      else{j.row.style.display="none";qHidden++;}}
    else{elActive.appendChild(j.row);j.row.style.display="";}}
  elQMore.style.display=qHidden?"":"none";
  elQMore.textContent=qHidden?("…还有 "+qHidden+" 个文件排队中"):"";
  elDoneWrap.style.display=doneN?"":"none";
  elDoneSum.textContent="已结束 "+doneN+" 个（完成 "+okN+
    " · 已存在 "+dupN+" · 已取消 "+canN+"）";}
const jobs=[];let samples=[],timer=null,cursor=0,workers=0;
const JUNK=new Set([".ds_store","thumbs.db","desktop.ini","ehthumbs.db"]);
const FULL_HASH=32*1024*1024,SMP=1024*1024;
const batchDone=new Map();
async function digestOf(f){
  if(!(window.crypto&&crypto.subtle))throw new Error("no webcrypto");
  const hex=b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,"0")).join("");
  if(f.size<=FULL_HASH)
    return hex(await crypto.subtle.digest("SHA-256",await f.arrayBuffer()));
  const pre=new TextEncoder().encode(String(f.size));
  const all=new Uint8Array(pre.length+3*SMP);
  all.set(pre,0);
  all.set(new Uint8Array(await f.slice(0,SMP).arrayBuffer()),pre.length);
  const midOff=Math.floor((f.size-SMP)/2);
  all.set(new Uint8Array(await f.slice(midOff,midOff+SMP).arrayBuffer()),pre.length+SMP);
  all.set(new Uint8Array(await f.slice(f.size-SMP).arrayBuffer()),pre.length+2*SMP);
  return hex(await crypto.subtle.digest("SHA-256",all));}
let pausedAll=false,pauseReason="",consecFails=0,probeTimer=null;
function noteNetOk(){consecFails=0;}
function noteNetFail(){consecFails++;
  if(consecFails>=3&&!pausedAll)setPaused(true,"auto");}
function setPaused(on,reason){
  pausedAll=on;pauseReason=on?(reason||"manual"):"";
  const bn=document.getElementById("alert");
  const pb=document.getElementById("pausebtn");
  if(on){
    bn.style.display="";
    bn.textContent=pauseReason==="auto"
      ?"⚠ 检测到网络异常，已自动暂停。已传分片都保留在服务器上，不会丢进度；网络恢复后会自动继续，你也可以点「继续」立即试一次。"
      :"已手动暂停。已传分片保留在服务器上，点「继续」后从断点接着传。";
    pb.textContent="继续";
    if(pauseReason==="auto")startProbe();
  }else{
    bn.style.display="none";pb.textContent="暂停全部";
    stopProbe();
    for(const j of jobs)if(j._launchChunks)j._launchChunks();
    pump();}
  layout();}
async function probeOnce(){
  try{const c=await jpost("api/uploads/check",{digest:"0".repeat(64)});
    return !!(c&&c.ok);}catch(e){return false;}}
function startProbe(){stopProbe();let okCount=0;
  probeTimer=setInterval(async()=>{
    if(await probeOnce()){okCount++;
      if(okCount>=2)setPaused(false);}
    else okCount=0;},20000);}
function stopProbe(){if(probeTimer){clearInterval(probeTimer);
  probeTimer=null;}}
document.getElementById("pausebtn").onclick=()=>{
  if(pausedAll)setPaused(false);else setPaused(true,"manual");};
window.addEventListener("online",()=>{
  if(pausedAll&&pauseReason==="auto")
    probeOnce().then(ok=>{if(ok)setPaused(false);});});
function isJunk(name,size){const n=(name||"").toLowerCase();
  return size<=0||JUNK.has(n)||n.startsWith("._");}
function fmtB(b){if(b>=1048576)return (b/1048576).toFixed(2)+" MB";
  if(b>=1024)return (b/1024).toFixed(1)+" KB";return b+" B";}
function fmtS(b){return fmtB(b)+"/s";}
function fmtEta(s){if(!isFinite(s))return "";if(s<60)return "约 "+Math.ceil(s)+" 秒";
  if(s<3600)return "约 "+Math.ceil(s/60)+" 分钟";return "约 "+(s/3600).toFixed(1)+" 小时";}
function jpost(u,b){return fetch(u,{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}).then(r=>r.json());}
function jobLoaded(j){let t=j.acked;for(const v of j.liveMap.values())t+=v;
  return Math.min(j.file.size,t);}
function totals(){let l=0,s=0;for(const j of jobs){s+=j.file.size;
  l+=(j.done&&j.state!=="cancel")?j.file.size:jobLoaded(j);}return [l,s];}
function counts(){const c={ok:0,dup:0,fail:0,up:0,q:0,cancel:0};
  for(const j of jobs){
    if(j.state==="ok"){if(j.dup)c.dup++;else c.ok++;}
    else if(j.state==="fail")c.fail++;
    else if(j.state==="cancel")c.cancel++;
    else if(j.state==="queue")c.q++;else c.up++;}
  return c;}
function tick(){layout();const now=Date.now();const tt=totals();
  samples.push([now,tt[0]]);
  while(samples.length>2&&now-samples[0][0]>6000)samples.shift();
  let sp=0;if(samples.length>1){const o=samples[0],dt=(now-o[0])/1000;
  if(dt>=1)sp=(tt[0]-o[1])/dt;}
  document.getElementById("sumbi").style.width=(tt[1]?(tt[0]/tt[1]*100):0)+"%";
  const c=counts();
  let tx="已传 "+fmtB(tt[0])+" / "+fmtB(tt[1])+" · 当前速度 "+fmtS(sp);
  if(sp>0&&tt[0]<tt[1])tx+=" · 剩余 "+fmtB(tt[1]-tt[0])+" · "+fmtEta((tt[1]-tt[0])/sp);
  tx+="　完成 "+(c.ok+c.dup)+"（新 "+c.ok+" · 已存在 "+c.dup+"）";
  if(c.up)tx+=" · 上传中 "+c.up;
  if(c.q)tx+=" · 排队 "+c.q;
  if(c.fail)tx+=" · 失败 "+c.fail;
  if(c.cancel)tx+=" · 已取消 "+c.cancel;
  document.getElementById("sumtx").textContent=tx;
  for(const j of jobs){if(j.state==="up"){const p=j.file.size?jobLoaded(j)/j.file.size:0;
  j.barEl.style.width=(p*100)+"%";
  j.stEl.textContent=(pausedAll?"已暂停 · ":"上传中 ")+Math.round(p*100)+"% · "+fmtB(jobLoaded(j))+" / "+fmtB(j.file.size);}}}
function sendChunk(job,idx){const f=job.file,off=idx*CH,
  end=Math.min(f.size,off+CH),blob=f.slice(off,end);let tries=0;
  const attempt=()=>new Promise((res,rej)=>{const x=new XMLHttpRequest();
  job.xhrs.add(x);
  x.open("PUT","api/uploads/"+job.uid+"/chunk?offset="+off);
  x.upload.onprogress=e=>{job.liveMap.set(idx,e.loaded);};
  x.onload=()=>{job.xhrs.delete(x);try{const d=JSON.parse(x.responseText);
  if(d.ok)res();else rej(new Error(d.message||"分片被拒绝"));}
  catch(_){rej(new Error("分片响应异常"));}};
  x.onabort=()=>{job.xhrs.delete(x);rej(new Error("已取消"));};
  x.onerror=()=>{job.xhrs.delete(x);const er=new Error("网络错误");
  er.net=true;rej(er);};
  x.send(blob);});
  const go=()=>attempt().catch(e=>{if(job.cancelled)throw e;
  if(pausedAll&&e.net){const er=new Error("已暂停");er.paused=true;throw er;}
  tries++;
  if(tries<3)return new Promise(r=>setTimeout(r,1000)).then(go);throw e;});
  return go();}
async function runJob(job){const f=job.file;
  if(job.cancelled){job.state="cancel";job.done=true;
  job.stEl.textContent="已取消";return;}
  job.state="init";job.stEl.textContent="本地校验是否已存在…";
  let digest=null;
  try{digest=await digestOf(f);}catch(e){digest=null;}
  job.digestCache=digest;
  if(digest){
    let exists=batchDone.has(digest);
    if(!exists){
      try{const c=await jpost("api/uploads/check",{digest:digest});
        exists=!!(c&&c.ok&&c.exists);}catch(e){exists=false;}}
    if(exists){
      let reg=null;
      try{reg=await jpost("api/uploads/register",{digest:digest,
        name:f.name,tags:document.getElementById("tags").value,
        source:document.getElementById("source").value,
        relpath:job.rel,
        library_id:document.getElementById("libsel").value});}catch(e){reg=null;}
      if(reg&&reg.ok){
        job.done=true;job.state="ok";job.dup=true;
        job.barEl.style.width="100%";hideX(job);
        job.stEl.textContent="已存在，秒传（跳过上传）";
        return;}}}
  job.stEl.textContent="初始化…";
  const base=job.rel.split("/").pop();
  let init;try{init=await jpost("api/uploads/init",{name:f.name,size:f.size,
  title:base.replace(/\.[^.]+$/,""),
  tags:document.getElementById("tags").value,
  source:document.getElementById("source").value,
  relpath:job.rel,
  library_id:document.getElementById("libsel").value});}catch(e){init=null;}
  if(!init){noteNetFail();job.state="queue";
  job.stEl.textContent="初始化遇网络异常，恢复后自动重试";layout();return;}
  if(!init.ok){job.state="fail";job.done=true;
  job.stEl.textContent="失败："+(init.message||"网络错误");showRetry(job);return;}
  job.uid=init.uid;job.state="up";job.acked=0;job.liveMap.clear();
  job.next=0;job.active=0;job.retryChunks=[];job.failed=null;
  job.sessionLost=false;job.phase="chunks";
  await waitChunks(job);
  await afterChunks(job);}
function launchChunks(job){
  const n=Math.ceil(job.file.size/CH);
  if(!job.failed&&!job.cancelled){
    while(job.active<PARC&&!pausedAll){
      let idx;
      if(job.retryChunks.length)idx=job.retryChunks.shift();
      else if(job.next<n)idx=job.next++;
      else break;
      job.active++;
      sendChunk(job,idx).then(()=>{job.active--;noteNetOk();
        job.acked+=Math.min(CH,job.file.size-idx*CH);
        job.liveMap.delete(idx);launchChunks(job);})
      .catch(e=>{job.active--;job.liveMap.delete(idx);
        if(job.cancelled){}
        else if(e.paused){job.retryChunks.push(idx);}
        else if(e.net){job.retryChunks.push(idx);noteNetFail();}
        else if((e.message||"").indexOf("会话不存在")>=0){
          job.sessionLost=true;job.failed=e;}
        else job.failed=e;
        launchChunks(job);});}}
  if(!job.active&&(job.failed||job.cancelled||
      (job.next>=n&&!job.retryChunks.length))){
    if(job._finChunks){const fin=job._finChunks;
      job._finChunks=null;fin();}}}
function waitChunks(job){
  return new Promise(fin=>{job._finChunks=fin;
    job._launchChunks=()=>launchChunks(job);launchChunks(job);});}
async function afterChunks(job){
  if(job.cancelled){job.state="cancel";job.done=true;
    job.stEl.textContent="已取消";layout();return;}
  if(job.failed){job.done=true;job.state="fail";
    job.stEl.textContent="失败："+job.failed.message+"（已传进度保留，可点继续）";
    showRetry(job);layout();return;}
  await finishJob(job);}
async function finishJob(job){
  job.phase="fin";job.state="fin";job.stEl.textContent="入库中…";
  let done;try{done=await jpost("api/uploads/"+job.uid+"/complete",{});}
  catch(e){done=null;}
  if(done&&done.ok){job.done=true;job.state="ok";
    job.dup=!!done.duplicate;
    if(job.digestCache)batchDone.set(job.digestCache,true);
    job.barEl.style.width="100%";hideX(job);
    job.stEl.textContent=done.duplicate?"内容已存在，已新增条目（不重复占空间）":"已入库，等待归档";
    layout();return;}
  if(!done&&job.digestCache){
    let landed=false;
    try{const c=await jpost("api/uploads/check",{digest:job.digestCache});
      landed=!!(c&&c.ok&&c.exists);}catch(e){landed=false;}
    if(landed){job.done=true;job.state="ok";
      job.barEl.style.width="100%";hideX(job);
      job.stEl.textContent="已入库（响应丢失后经校验确认）";
      layout();return;}}
  if(!done)noteNetFail();
  job.done=true;job.state="fail";
  job.stEl.textContent="失败："+((done&&done.message)||"网络错误")+"（可点继续重试入库）";
  showRetry(job);layout();}
function resumeJob(job){
  if(job.retryBtn){job.retryBtn.remove();job.retryBtn=null;}
  job.done=false;job.cancelled=false;
  if(job.sessionLost||!job.uid){
    job.uid=null;job.sessionLost=false;job.failed=null;
    job.acked=0;job.next=0;job.retryChunks=[];job.liveMap.clear();
    job.barEl.style.width="0%";runJob(job);return;}
  if(job.phase==="fin"){job.failed=null;finishJob(job);return;}
  job.failed=null;job.state="up";
  job.stEl.textContent="继续上传…";
  (async()=>{await waitChunks(job);await afterChunks(job);})();}
function hideX(job){if(job.xBtn)job.xBtn.style.display="none";}
function showRetry(job){hideX(job);if(job.retryBtn)return;
  const b=document.createElement("button");b.type="button";
  b.className="ghost";b.textContent="继续";b.style.marginLeft="8px";
  b.onclick=()=>{b.remove();job.retryBtn=null;resumeJob(job);};
  job.stEl.after(b);job.retryBtn=b;}
function cancelJob(job){
  if(job.state==="queue"){job.cancelled=true;job.state="cancel";
  job.done=true;job.stEl.textContent="已取消";hideX(job);layout();}
  else if(job.state==="up"||job.state==="init"){job.cancelled=true;
  for(const x of[...job.xhrs])x.abort();
  job.stEl.textContent="取消中…";
  if(job._launchChunks)job._launchChunks();}}
async function worker(){workers++;
  for(;;){
    if(pausedAll)break;
    const j=jobs.find(x=>x.state==="queue");
    if(!j)break;
    await runJob(j);}
  workers--;if(!workers){clearInterval(timer);timer=null;tick();
  const c=counts();
  document.getElementById("msg").textContent=
    (c.q||c.up)?"已暂停（进度保留）":"本批处理完";}}
function pump(){document.getElementById("sum").style.display="";
  if(!timer){samples=[];timer=setInterval(tick,500);}
  let need=jobs.filter(j=>j.state==="queue").length;
  while(workers<PARF&&need>0){worker();need--;}}
function esc(t){return t.replace(/</g,"&lt;");}
function enqueue(pairs){
  let skipped=0,added=0;
  for(const pr of pairs){
    if(isJunk(pr.f.name,pr.f.size)){skipped++;continue;}
    const row=document.createElement("div");
    const dir=pr.rel.slice(0,pr.rel.length-pr.f.name.length);
    row.innerHTML="<div style='font-size:13.5px;margin-bottom:5px'>"+
    (dir?"<span style='color:#8b94a7'>"+esc(dir)+"</span>":"")+
    esc(pr.f.name)+" · "+fmtB(pr.f.size)+" · <span class='st'>排队</span></div>"+
    "<div class='progress'><i></i></div>";
    const xb=document.createElement("button");xb.type="button";
    xb.className="ghost";xb.textContent="✕";xb.title="取消";
    xb.style.marginLeft="8px";
    row.querySelector("div").appendChild(xb);
    elQueue.appendChild(row);
    const job={file:pr.f,rel:pr.rel,row:row,
    stEl:row.querySelector(".st"),barEl:row.querySelector("i"),
    xBtn:xb,acked:0,liveMap:new Map(),xhrs:new Set(),
    state:"queue",done:false,cancelled:false,dup:false,retryBtn:null,
    uid:null,next:0,active:0,retryChunks:[],failed:null,
    sessionLost:false,phase:"",digestCache:null,
    _finChunks:null,_launchChunks:null};
    xb.onclick=()=>cancelJob(job);
    jobs.push(job);added++;}
  if(skipped)document.getElementById("msg").textContent=
    "已跳过 "+skipped+" 个空文件 / 系统文件";
  if(added){layout();pump();}}
function pickedPairs(){
  const out=[];
  for(const f of document.getElementById("files").files)
    out.push({f:f,rel:f.webkitRelativePath||f.name});
  for(const f of document.getElementById("dir").files)
    out.push({f:f,rel:f.webkitRelativePath||f.name});
  return out;}
function pickedNote(){const p=pickedPairs();
  let s=0;for(const x of p)s+=x.f.size;
  document.getElementById("picked").textContent=
    p.length?("已选 "+p.length+" 个文件 · "+fmtB(s)):"";}
document.getElementById("pickf").onclick=()=>document.getElementById("files").click();
document.getElementById("pickd").onclick=()=>document.getElementById("dir").click();
document.getElementById("files").addEventListener("change",pickedNote);
document.getElementById("dir").addEventListener("change",pickedNote);
document.getElementById("go").onclick=()=>{
  const pairs=pickedPairs();
  if(!pairs.length){document.getElementById("msg").textContent="先选择文件或文件夹";return;}
  document.getElementById("msg").textContent="";
  document.getElementById("files").value="";
  document.getElementById("dir").value="";
  document.getElementById("picked").textContent="";
  enqueue(pairs);};
async function collectFromDrop(dt){
  const items=[...(dt.items||[])].filter(i=>i.kind==="file");
  const entries=items.map(i=>i.webkitGetAsEntry&&i.webkitGetAsEntry()).filter(Boolean);
  if(!entries.length)
    return [...dt.files].map(f=>({f:f,rel:f.webkitRelativePath||f.name}));
  const out=[];
  async function walk(en,path){
    if(en.isFile){const f=await new Promise(r=>en.file(r));
      out.push({f:f,rel:path+en.name});}
    else if(en.isDirectory){const rd=en.createReader();let batch;
      do{batch=await new Promise(r=>rd.readEntries(r));
        for(const c of batch)await walk(c,path+en.name+"/");
      }while(batch.length);}}
  for(const en of entries)await walk(en,"");
  return out;}
const dz=document.getElementById("dz");
dz.addEventListener("dragover",e=>{e.preventDefault();dz.style.outline="2px dashed #4f7cff";});
dz.addEventListener("dragleave",()=>{dz.style.outline="";});
dz.addEventListener("drop",async e=>{e.preventDefault();dz.style.outline="";
  document.getElementById("msg").textContent="";
  enqueue(await collectFromDrop(e.dataTransfer));});
</script>
"""


def page_upload(base_href=""):
    body = UPLOAD_PAGE.replace(
        "__BUDGET__", _fmt_size(STAGING_BUDGET)).replace(
        "__LIB_OPTIONS__", _lib_options_html())
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
            "queue": [], "video_libraries": [], "file_categories": [],
            "novels": [], "kind_counts": {}, "kind_status": {}}
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
        data["video_libraries"] = [dict(r) for r in conn.execute(
            "SELECT l.*, (SELECT COUNT(*) FROM entries e "
            "WHERE e.library_id = l.id) AS n FROM libraries l "
            "WHERE l.kind = 'video' ORDER BY l.is_default DESC, l.id")]
        data["file_categories"] = _file_categories()
        data["novels"] = [dict(r) for r in conn.execute(
            "SELECT e.id AS id, e.title AS title, "
            "m.char_count AS chars, m.chapters AS chaps, "
            "COALESCE(m.title_len, 0) AS title_len, "
            "COALESCE(m.split_stats, '') AS split_stats "
            "FROM entries e JOIN files f ON f.hash = e.hash "
            "LEFT JOIN novel_meta m ON m.hash = e.hash "
            "WHERE f.kind = 'novel' ORDER BY e.id")]
        data["kind_counts"] = {r["kind"]: r["c"] for r in conn.execute(
            "SELECT f.kind AS kind, COUNT(*) AS c "
            "FROM entries e JOIN files f "
            "ON f.hash = e.hash GROUP BY f.kind")}
        data["kind_status"] = {}
        for r in conn.execute(
                "SELECT kind, status, COUNT(*) AS c "
                "FROM files GROUP BY kind, status"):
            data["kind_status"].setdefault(
                r["kind"], {})[r["status"]] = r["c"]
    try:
        st = ALIST.drive_storage()
        data["alist_ok"] = True
        if st:
            data["storage"] = {"driver": st.get("driver"),
                               "status": st.get("status")}
    except Exception as exc:
        data["alist_error"] = str(exc)[:200]
    return data


def _admin_sections():
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
    lib_rows = []
    for l in d["video_libraries"]:
        dele = ('<button type="button" class="danger" data-del="%d">删除</button>'
                % l["id"]) if (not l["is_default"] and not l["n"]) else ""
        lib_rows.append(
            '<div style="display:flex;gap:8px;align-items:center;margin:6px 0">'
            '<input type="text" class="libname" data-id="%d" value="%s" '
            'style="flex:1">'
            '<span class="muted">%d 条%s</span>%s</div>'
            % (l["id"], _esc(l["name"]), l["n"],
               " · 默认库" if l["is_default"] else "", dele))
    cat_rows = []
    for c in d["file_categories"]:
        cat_rows.append(
            '<div style="display:flex;gap:8px;align-items:center;margin:6px 0">'
            '<input type="text" class="fcname" value="%s" style="width:130px">'
            '<input type="text" class="fcexts" value="%s" style="flex:1" '
            'placeholder="扩展名，空格分隔；留空 = 兜底类别">'
            '<button type="button" class="danger fcdel">删除行</button></div>'
            % (_esc(c["name"]), _esc(c["exts"])))
    ntl_rows = []
    for nv in d["novels"]:
        try:
            _nch = len(json.loads(nv["chaps"] or "[]"))
        except Exception:
            _nch = 0
        try:
            _st = json.loads(nv["split_stats"] or "")
        except Exception:
            _st = None
        _stline = ""
        if isinstance(_st, dict) and (
                _st.get("rule") or _st.get("ai")
                or _st.get("fixed") or _st.get("mechanical")):
            _seg = []
            if _st.get("rule"):
                _seg.append("规则 %d 章" % _st["rule"])
            if _st.get("ai"):
                _seg.append("AI %d 章" % _st["ai"])
            if _st.get("fixed"):
                _seg.append("固定 %d 章" % _st["fixed"])
            if _st.get("mechanical"):
                _seg.append("机械 %d 段" % _st["mechanical"])
            _txt = "上次切分定界：" + " · ".join(_seg)
            if _st.get("ai") and _st.get("model"):
                _txt += "（模型 %s）" % _st["model"]
            if _st.get("fallback"):
                _txt += "；回退：%s" % _st["fallback"]
            _stline = ('<div class="muted" '
                       'style="margin:-2px 0 6px">%s</div>'
                       % _esc(_txt))
        ntl_rows.append(
            '<div class="ntlitem"><div style="display:flex;'
            'gap:8px;align-items:center;margin:6px 0">'
            '<span class="ntltitle" style="flex:1">%s</span>'
            '<span class="muted">%d 章 · %s · 标题字数</span>'
            '<input type="number" class="ntlen" data-id="%d" '
            'value="%d" min="0" max="30" style="width:64px">'
            '<button type="button" class="ntlsave" data-id="%d">'
            '保存并重切</button></div>'
            % (_esc(nv["title"] or ""), _nch, _fmt_wchars(nv["chars"]),
               nv["id"], nv["title_len"] or 0, nv["id"])
            + _stline + '</div>')
    sec_videos = (
        '<div class="panel"><h2>视频主题库</h2>'
        + "".join(lib_rows)
        + '<div style="display:flex;gap:8px;margin-top:10px">'
        '<button type="button" id="libsave">保存名称</button>'
        '<input type="text" id="libnew" placeholder="新库名" style="flex:1">'
        '<button type="button" id="libadd">＋ 添加库</button></div>'
        '<div id="libmsg" class="muted"></div></div>'
        )
    sec_filecats = (
        '<div class="panel"><h2>文件类别规则</h2>'
        '<div id="fcrows">' + "".join(cat_rows) + '</div>'
        + '<div style="display:flex;gap:8px;margin-top:10px">'
        '<button type="button" id="fcadd">＋ 添加一行</button>'
        '<button type="button" id="fcsave">保存全部规则</button></div>'
        '<div class="muted">入库时按扩展名自上而下命中第一个类别；'
        '扩展名留空的一行是兜底类别（命不中进它）。改规则只影响新入库；'
        '已入库文件在「文件」页逐个改类。</div>'
        '<div id="fcmsg" class="muted"></div></div>')
    sec_ext = (
        '<div class="panel"><h2>\u624b\u673a\u4e66\u6e90'
        '\uff08\u641c\u4e66\u5927\u5e08 / \u9605\u8bfb\uff09</h2>'
        '<div class="kv">\u5bfc\u5165\u5730\u5740\uff1a<code>'
        + _esc(_ext_import_url()) +
        '</code></div>'
        '<p style="color:#8b94a7;font-size:13px;line-height:1.7">'
        '\u5728\u624b\u673a\u9605\u8bfb App \u91cc\u9009'
        '\u300c\u5bfc\u5165\u4e66\u6e90 \u2192 \u7f51\u7edc'
        '\u5bfc\u5165\u300d\uff0c\u7c98\u8d34\u4e0a\u9762'
        '\u7684\u5730\u5740\uff0c\u5373\u53ef\u628a\u8d44'
        '\u6e90\u5e93\u91cc\u7684\u5c0f\u8bf4\u5f53\u4f5c'
        '\u4e00\u4e2a\u4e66\u6e90\u641c\u7d22\u3001\u9605'
        '\u8bfb\u3002\u91cd\u7f6e\u4ee4\u724c\u540e\u65e7'
        '\u5730\u5740\u7acb\u5373\u5931\u6548\u3002</p>'
        '<p><button type="button" id="extreset">'
        '\u91cd\u7f6e\u4ee4\u724c</button> '
        '<span class="msg" id="extmsg"></span></p></div>')
    sec_js_videos = (
        '<script>'
        'async function postJ(u,b){const r=await fetch(u,{method:"POST",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});'
        'return r.json();}'
        'const lm=document.getElementById("libmsg");'
        'document.getElementById("libsave").onclick=async function(){'
        'const ren={};'
        'document.querySelectorAll(".libname").forEach(function(el){'
        'ren[el.dataset.id]=el.value;});'
        'const j=await postJ("api/admin/libraries",{renames:ren});'
        'lm.textContent=j.message||(j.ok?"已保存。":"失败");'
        'if(j.ok)setTimeout(function(){location.reload();},700);};'
        'document.getElementById("libadd").onclick=async function(){'
        'const v=document.getElementById("libnew").value.trim();if(!v)return;'
        'const j=await postJ("api/admin/libraries",{add_name:v});'
        'lm.textContent=j.message||"";if(j.ok)location.reload();};'
        'document.querySelectorAll("[data-del]").forEach(function(el){'
        'el.onclick=async function(){'
        'if(!confirm("删除这个空库？"))return;'
        'const j=await postJ("api/admin/libraries",'
        '{delete_id:parseInt(el.dataset.del,10)});'
        'lm.textContent=j.message||"";if(j.ok)location.reload();};});'
        '</script>')
    sec_js_filecats = (
        '<script>'
        'async function postJ(u,b){const r=await fetch(u,{method:"POST",'
        'headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});'
        'return r.json();}'
        'const fcrows=document.getElementById("fcrows");'
        'const fm=document.getElementById("fcmsg");'
        'function bindDel(){document.querySelectorAll(".fcdel").forEach('
        'function(el){el.onclick=function(){'
        'el.parentElement.remove();};});}'
        'bindDel();'
        'document.getElementById("fcadd").onclick=function(){'
        'const dv=document.createElement("div");'
        'dv.style.cssText="display:flex;gap:8px;align-items:center;margin:6px 0";'
        'dv.innerHTML="<input type=\'text\' class=\'fcname\' '
        'style=\'width:130px\'>'
        '<input type=\'text\' class=\'fcexts\' style=\'flex:1\' '
        'placeholder=\'扩展名，空格分隔；留空 = 兜底类别\'>'
        '<button type=\'button\' class=\'danger fcdel\'>删除行</button>\";'
        'fcrows.appendChild(dv);bindDel();};'
        'document.getElementById("fcsave").onclick=async function(){'
        'const items=[];'
        'fcrows.querySelectorAll(":scope > div").forEach(function(row){'
        'const n=row.querySelector(".fcname"),e=row.querySelector(".fcexts");'
        'if(n&&e)items.push({name:n.value,exts:e.value});});'
        'const j=await postJ("api/admin/filecats",{items:items});'
        'fm.textContent=j.message||(j.ok?"已保存。":"失败");'
        'if(j.ok)setTimeout(function(){location.reload();},700);};'
        '</script>')
    sec_js_ext = (
        '<script>'
        'document.getElementById("extreset").onclick=async function(){'
        'if(!confirm("\u91cd\u7f6e\u540e\u65e7\u5bfc\u5165'
        '\u5730\u5740\u7acb\u5373\u5931\u6548\uff0c\u624b'
        '\u673a\u4e0a\u9700\u91cd\u65b0\u5bfc\u5165\u3002'
        '\u7ee7\u7eed\uff1f"))return;'
        'const j=await postJ("api/admin/ext-token",{});'
        'const m=document.getElementById("extmsg");'
        'm.textContent=j.ok?"\u5df2\u91cd\u7f6e\uff0c\u9875'
        '\u9762\u5237\u65b0\u4e2d\u2026":'
        '("\u5931\u8d25\uff1a"+(j.message||""));'
        'if(j.ok)setTimeout(function(){location.reload();},700);};'
        '</script>')
    _ai_key, _ai_model, _ai_on = _ai_title_config()
    _ai_batch = _ai_batch_size()
    _ai_stats_rows = []
    _ai_latest = None
    for nv in d["novels"]:
        try:
            _s = json.loads(nv["split_stats"] or "")
        except Exception:
            _s = None
        if not isinstance(_s, dict):
            _s = None
        _ai_stats_rows.append((nv, _s))
        if _s and (_s.get("ai") or 0) > 0:
            if _ai_latest is None or (_s.get("at") or 0) > (
                    _ai_latest[1].get("at") or 0):
                _ai_latest = (nv, _s)
    _ai_recent = "暂无"
    if _ai_latest is not None:
        _nv0, _s0 = _ai_latest
        _when0 = "时间未记录"
        if _s0.get("at"):
            _when0 = time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(_s0["at"]))
        _ai_recent = "《%s》 · %s · AI 定界 %d 章%s" % (
            _nv0["title"] or "", _when0, _s0.get("ai") or 0,
            (" · 模型 " + str(_s0["model"]))
            if _s0.get("model") else "")
    _ais_rows = []
    for nv, _s in _ai_stats_rows:
        if _s is None:
            _comp = "尚无切分记录"
            _badge = ""
            _when = ""
        else:
            _segs = []
            if _s.get("rule"):
                _segs.append("规则 %d" % _s["rule"])
            if _s.get("ai"):
                _segs.append("AI %d" % _s["ai"])
            if _s.get("fixed"):
                _segs.append("固定 %d" % _s["fixed"])
            if _s.get("mechanical"):
                _segs.append("机械 %d" % _s["mechanical"])
            _comp = " · ".join(_segs) if _segs else "无定界记录"
            if _s.get("model"):
                _comp += " · 模型 " + str(_s["model"])
            _badge = ('<span class="chip" style="background:#7a2e2e;'
                      'color:#ffc9c9">AI 定界</span> ') \
                if (_s.get("ai") or 0) > 0 else ""
            if (_s.get("ai_pending") or 0) > 0:
                _badge += ('<span class="chip" '
                           'style="background:#6b5510;'
                           'color:#ffe1a8">待分析 %d 章</span> '
                           % _s["ai_pending"])
            _when = ""
            if _s.get("at"):
                _when = " · " + time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(_s["at"]))
        _ais_rows.append(
            '<div class="aiitem"><div style="display:flex;'
            'gap:8px;align-items:center;margin:5px 0">'
            '<span class="aititle" style="flex:1">%s%s</span>'
            '<span class="muted">%s%s</span></div></div>'
            % (_badge, _esc(nv["title"] or ""),
               _esc(_comp), _esc(_when)))
    ai_block = (
        '<div style="border-top:1px solid #262c3d;'
        'margin-top:12px;padding-top:10px">'
        '<div class="kv">AI 标题定界（OpenRouter）：'
        'key <b>%s</b> · AI 定界 <b>%s</b></div>'
        % ("已配置" if _ai_key else "未配置",
           "已启用" if _ai_on else "未启用"))
    _ai_pend_books = sum(
        1 for _nv, _s in _ai_stats_rows
        if _s and (_s.get("ai_pending") or 0) > 0)
    _ai_pend_chs = sum(
        (_s.get("ai_pending") or 0)
        for _nv, _s in _ai_stats_rows if _s)
    ai_block += (
        '<div class="kv">状态总览：模型 <b>%s</b> · 批大小 '
        '<b>%d</b> · 并发 <b>%d</b> · 单批 max_tokens 上限 '
        '<b>%d</b>（硬顶 %d） · 单批响应字节顶 <b>%d</b></div>'
        '<div class="kv">最近一次 AI 切分：%s</div>'
        '<div class="kv">待分析：<b>%d 本 · %d 章</b>'
        '（导入时只切了规则；这些书被打开阅读或被手机书源'
        '拉目录时，才在后台补跑 AI）</div>'
        % (_esc(_ai_model), _ai_batch, _AI_MAX_WORKERS,
           _ai_max_tokens(_ai_batch), _AI_MAX_TOKENS_CAP,
           _ai_resp_bytes(_ai_batch), _esc(_ai_recent),
           _ai_pend_books, _ai_pend_chs))
    ai_block += (
        '<p style="color:#8b94a7;font-size:13px;line-height:1.7;'
        'margin:8px 0 2px">触发说明：导入新书只跑规则切分，'
        '不调用 AI；切分后仍留有可改善章节的书会被标为「待分析」。'
        '待分析的书第一次被打开阅读、或被手机书源拉目录时，'
        '后台才对那一本的待处理章节补跑 AI（找回章首走选界、'
        '其余走字数判定），完成后清标记；分析中页面照常显示'
        '旧标题，失败则保留标记、下次打开重试。管理页对某本书'
        '「保存并重切」是明确意图，立即全量跑完（含 AI）并清'
        '标记。平台没有后台定时调用。开关关闭或未配置 key 时，'
        '切分全程只用规则，待分析标记也不会被触发。</p>')
    ai_block += (
        '<div style="display:grid;gap:9px;max-width:520px;'
        'margin-top:8px">'
        '<input type="password" id="aiorkey" '
        'autocomplete="off" placeholder="填入 OpenRouter '
        'API key（只写不回显，留空不改）">'
        '<input type="text" id="aiormodel" value="%s" '
        'placeholder="模型名，如 google/gemini-2.5-flash">'
        '<label style="font-size:13px;color:#8b94a7">'
        '<input type="checkbox" id="aioren"%s> 启用 AI 定界'
        '（导入只切规则并标记待分析，阅读待分析的书时自动'
        '补跑；管理页手动重切立即全量跑 AI；失败自动回退'
        '规则）</label>'
        '<span><button type="button" id="aiorsave">'
        '保存 AI 设置</button> '
        '<span class="msg" id="aiormsg"></span></span>'
        '</div>'
        % (_esc(_ai_model), " checked" if _ai_on else ""))
    ai_block += (
        '<div style="border-top:1px solid #262c3d;'
        'margin-top:12px;padding-top:10px">'
        '<div style="display:flex;align-items:center;'
        'margin:6px 0"><b style="flex:1">全库切分构成</b>'
        '<button type="button" id="aistoggle" class="ghost">'
        '收起</button></div>'
        '<div id="aisbody">'
        '<div style="display:flex;gap:8px;align-items:center;'
        'margin:6px 0">'
        '<input type="text" id="aisearch" '
        'placeholder="搜索书名" style="flex:1">'
        '<span class="muted" id="aicount">共 %d 本</span></div>'
        '<div id="aisbox" style="max-height:40vh;'
        'overflow-y:auto">'
        % len(_ais_rows)
        + "".join(_ais_rows)
        + '</div></div></div></div>')
    sec_novels = (
        '<div class="panel"><h2>小说切分（标题字数）'
        '<button type="button" id="ntltoggle" class="ghost" '
        'style="float:right">收起</button></h2>'
        '<div id="ntlbody">'
        '<div style="display:flex;gap:8px;align-items:center;'
        'margin:6px 0">'
        '<input type="text" id="ntlsearch" '
        'placeholder="搜索书名" style="flex:1">'
        '<span class="muted" id="ntlcount">共 %d 本</span></div>'
        '<div id="ntlbox" style="max-height:50vh;overflow-y:auto">'
        % len(ntl_rows)
        + "".join(ntl_rows)
        + '</div>'
        + '<div class="muted">标题字数 0 = 自动：先用结构断点'
        '定界，定不出时按本书标题长度推断，再不行默认 4 字；'
        '填 N 时，标题与正文连排无标点则按「第X章」后固定 '
        'N 字定界。「第X章」后紧跟的顿号等标点不计入字数。'
        '保存后立即按新值重切该书。</div>'
        '<div id="ntlmsg" class="muted"></div>' + ai_block
        + '</div></div>')
    sec_js_novels = (
        '<script>'
        'document.querySelectorAll(".ntlsave").forEach(function(b){'
        'b.onclick=async function(){'
        'let inp=null;'
        'document.querySelectorAll(".ntlen").forEach(function(x){'
        'if(x.dataset.id===b.dataset.id)inp=x;});'
        'const j=await postJ("api/admin/novel/title-len",'
        '{entry_id:parseInt(b.dataset.id),'
        'title_len:parseInt((inp&&inp.value)||"0")});'
        'const m=document.getElementById("ntlmsg");'
        'm.textContent=j.ok?("已重切：" + j.chapters + " 章"):'
        '("失败：" + (j.message || ""));'
        'if(j.ok)setTimeout(function(){location.reload();},900);};});'
        'var ntg=document.getElementById("ntltoggle"),'
        'nbd=document.getElementById("ntlbody");'
        'if(ntg&&nbd)ntg.onclick=function(){'
        'var hid=nbd.style.display==="none";'
        'nbd.style.display=hid?"":"none";'
        'ntg.textContent=hid?"收起":"展开";};'
        'var nsr=document.getElementById("ntlsearch"),'
        'nct=document.getElementById("ntlcount"),'
        'nitems=document.querySelectorAll(".ntlitem");'
        'if(nsr)nsr.oninput=function(){'
        'var q=nsr.value.trim().toLowerCase(),vis=0;'
        'nitems.forEach(function(it){'
        'var t=it.querySelector(".ntltitle");'
        'var hit=!q||(t&&t.textContent.toLowerCase()'
        '.indexOf(q)>=0);'
        'it.style.display=hit?"":"none";if(hit)vis++;});'
        'if(nct)nct.textContent=q?(vis+" / "+nitems.length):'
        '("共 "+nitems.length+" 本");};'
        '</script>')
    sec_js_novels += (
        '<script>'
        'document.getElementById("aiorsave").onclick=async function(){'
        'const j=await postJ("api/admin/novel/ai-config",'
        '{openrouter_key:document.getElementById("aiorkey").value,'
        'model:document.getElementById("aiormodel").value,'
        'enabled:document.getElementById("aioren").checked});'
        'const m=document.getElementById("aiormsg");'
        'm.textContent=j.ok?"已保存，页面刷新中…":'
        '("失败：" + (j.message || ""));'
        'if(j.ok)setTimeout(function(){location.reload();},800);};'
        '</script>')
    sec_js_novels += (
        '<script>'
        'var atg=document.getElementById("aistoggle"),'
        'abd=document.getElementById("aisbody");'
        'if(atg&&abd)atg.onclick=function(){'
        'var hid=abd.style.display==="none";'
        'abd.style.display=hid?"":"none";'
        'atg.textContent=hid?"收起":"展开";};'
        'var asr=document.getElementById("aisearch"),'
        'act=document.getElementById("aicount"),'
        'aitems=document.querySelectorAll(".aiitem");'
        'if(asr)asr.oninput=function(){'
        'var q=asr.value.trim().toLowerCase(),vis=0;'
        'aitems.forEach(function(it){'
        'var t=it.querySelector(".aititle");'
        'var hit=!q||(t&&t.textContent.toLowerCase()'
        '.indexOf(q)>=0);'
        'it.style.display=hit?"":"none";if(hit)vis++;});'
        'if(act)act.textContent=q?(vis+" / "+aitems.length):'
        '("共 "+aitems.length+" 本");};'
        '</script>')
    sec_status = (
        '<div class="panel"><h2>状态</h2><div class="kv">'
        'AList：<b>%s</b>%s<br>谷歌硬盘存储：<b>%s</b><br>'
        '文件：<b>%s</b> · 条目：<b>%d</b><br>'
        '暂存区：<b>%s</b> / %s · 磁盘剩余 <b>%s</b><br>'
        'AList 密码：<b>%s</b> · 谷歌令牌：<b>%s</b> · 谷歌客户端：<b>%s</b> · WebDAV 密码：<b>%s</b><br>'
        '数据库备份：<b>%s</b>%s</div>'
        '<p><button type="button" id="backup">立即备份数据库</button> '
        '<span class="msg" id="bmsg"></span></p></div>'
        ) % ("已连接" if d["alist_ok"] else "不可达",
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
        )
    sec_dav = (
        '<div class="panel"><h2>WebDAV（RaiDrive / rclone 挂载）</h2>'
        '<div class="kv">地址：<code>https://debi.su7.dpdns.org/dav/</code>'
        '（IPv4 可用 debi4 同地址）<br>用户名：<code>dav</code> · 密码：<b>%s</b></div>'
        '<p style="color:#8b94a7;font-size:13px;line-height:1.7">'
        '顶层是视频库名，库内按上传月份自动生成（YYYY-MM），根目录上传仍进默认库当月；文件名就是条目标题；改名、删除只动数据库条目，'
        '源文件按哈希存放在谷歌硬盘，删条目后无引用的源文件保留一周再清理。'
        '跨月份移动请在网页里改（WebDAV 内会拒绝）。</p>'
        '<div style="display:grid;gap:9px;max-width:420px">'
        '<input type="password" id="davpw" placeholder="设置 / 修改 WebDAV 密码（至少 8 位）" autocomplete="new-password">'
        '<span><button type="button" id="davset">保存 WebDAV 密码</button> '
        '<span class="msg" id="davmsg"></span></span></div></div>'
        ) % ("已设置" if d["dav_set"] else "未设置（WebDAV 暂不可用）",)
    sec_connect = (
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
        ) % (_esc(_cred("google_root_folder_id")),)
    sec_queue = '<div class="panel"><h2>归档队列</h2>%s</div>' % queue_rows
    sec_js_settings = (
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
    )
    sec_settings = (sec_status + sec_dav + sec_connect + sec_queue)
    return {"videos": sec_videos, "js_videos": sec_js_videos,
            "filecats": sec_filecats, "js_filecats": sec_js_filecats,
            "ext": sec_ext, "js_ext": sec_js_ext,
            "novels": sec_novels, "js_novels": sec_js_novels,
            "settings": sec_settings, "js_settings": sec_js_settings}


_ADMIN_TITLES = {"videos": "视频", "images": "图片",
                 "novels": "小说", "files": "文件",
                 "music": "音乐", "settings": "通用设置"}


def _admin_topbar(section=None):
    if section is None:
        return ('<div class="topbar"><a class="btn ghost" '
                'href="./">← 返回</a><h1>管理</h1></div>')
    return ('<div class="topbar"><a class="btn ghost" '
            'href="admin">← 管理</a><h1>管理 · %s</h1></div>'
            % _ADMIN_TITLES[section])


def page_admin(base_href=""):
    d = _admin_data()
    kc = d["kind_counts"]
    ks = d["kind_status"]
    pend_books = 0
    pend_chs = 0
    for nv in d["novels"]:
        try:
            _s = json.loads(nv["split_stats"] or "")
        except Exception:
            _s = None
        if isinstance(_s, dict):
            _p = _s.get("ai_pending") or 0
            if _p:
                pend_books += 1
                pend_chs += _p
    queue_n = sum(d["counts"].get(k, 0)
                  for k in ("local", "uploading", "error"))
    last_backup = ("从未" if not d["last_backup"] else
                   time.strftime("%Y-%m-%d %H:%M",
                                 time.localtime(
                                     float(d["last_backup"]))))
    cards = (
        ("admin/videos", "视频",
         "%d 个主题库 · %d 条视频"
         % (len(d["video_libraries"]), kc.get("video", 0))),
        ("admin/images", "图片",
         "共 %d 张 · 已归档 %d"
         % (kc.get("image", 0),
            ks.get("image", {}).get("archived", 0))),
        ("admin/novels", "小说",
         "共 %d 本 · 待分析 %d 本 · %d 章"
         % (len(d["novels"]), pend_books, pend_chs)),
        ("admin/files", "文件",
         "%d 个类别 · %d 个文件"
         % (len(d["file_categories"]), kc.get("file", 0))),
        ("admin/music", "音乐",
         "共 %d 首 · 已归档 %d"
         % (kc.get("music", 0),
            ks.get("music", {}).get("archived", 0))),
        ("admin/settings", "通用设置",
         "AList %s · 待归档 %d · 上次备份 %s"
         % ("已连接" if d["alist_ok"] else "不可达",
            queue_n, last_backup)),
    )
    body = (_admin_topbar()
            + '<div class="grid">'
            + "".join(
                '<a class="card" href="%s"><div class="t">%s</div>'
                '<div class="m">%s</div></a>' % c for c in cards)
            + '</div>')
    return _page("管理", body, base_href=base_href)


def page_admin_videos(base_href=""):
    s = _admin_sections()
    return _page("管理 · 视频",
                 _admin_topbar("videos") + s["videos"]
                 + s["js_videos"],
                 base_href=base_href)


def page_admin_images(base_href=""):
    d = _admin_data()
    ks = d["kind_status"].get("image", {})
    labels = {"archived": "已归档", "local": "待归档",
              "uploading": "归档中", "error": "失败"}
    dist = " · ".join("%s %d" % (labels.get(k, k), v)
                      for k, v in sorted(ks.items())) or "暂无"
    body = (_admin_topbar("images")
            + '<div class="panel"><h2>图片</h2>'
            '<div class="kv">图片条目：<b>%d</b><br>'
            '文件状态：<b>%s</b></div>'
            '<p style="color:#8b94a7;font-size:13px;'
            'line-height:1.7">图片没有专属的管理项：上传与'
            '浏览在图片页完成，归档走全库统一管线（状态与'
            '队列在「通用设置」里看）。</p>'
            '<p><a class="btn" href="images">打开图片页</a>'
            '</p></div>'
            % (d["kind_counts"].get("image", 0), dist))
    return _page("管理 · 图片", body, base_href=base_href)


def page_admin_music(base_href=""):
    d = _admin_data()
    ks = d["kind_status"].get("music", {})
    labels = {"archived": "已归档", "local": "待归档",
              "uploading": "归档中", "error": "失败"}
    dist = " · ".join("%s %d" % (labels.get(k, k), v)
                      for k, v in sorted(ks.items())) or "暂无"
    with _db() as conn:
        agg = conn.execute(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(f.duration), 0) AS dur, "
            "COALESCE(SUM(f.size), 0) AS sz "
            "FROM entries e JOIN files f ON f.hash = e.hash "
            "WHERE f.kind = 'music'").fetchone()
    body = (_admin_topbar("music")
            + '<div class="panel"><h2>音乐</h2>'
            '<div class="kv">音乐条目：<b>%d</b><br>'
            '总时长：<b>%s</b> · 总占用：<b>%s</b><br>'
            '文件状态：<b>%s</b></div>'
            '<p style="color:#8b94a7;font-size:13px;'
            'line-height:1.7">单库：全部音乐按月归档到谷歌硬盘'
            '的「音乐」文件夹。上传与播放在音乐页完成，归档走'
            '全库统一管线（状态与队列在「通用设置」里看）。</p>'
            '<p><a class="btn" href="music">打开音乐页</a>'
            '</p></div>'
            % (agg["n"], _fmt_duration(agg["dur"] or 0),
               _fmt_size(agg["sz"] or 0), dist))
    return _page("管理 · 音乐", body + _subsonic_admin_panel(), base_href=base_href)


def page_admin_novels(base_href=""):
    s = _admin_sections()
    link = ('<div class="panel"><h2>小说备注页</h2>'
            '<div class="kv">独立的读书笔记：候选 / 在读 / '
            '精品 / 不再推荐。和书库不绑定，删书不影响笔记，'
            '没入库的书也能记。</div>'
            '<p><a class="btn" href="notes">打开备注页</a>'
            '</p></div>')
    return _page("管理 · 小说",
                 _admin_topbar("novels") + link + s["ext"]
                 + s["novels"] + s["js_ext"] + s["js_novels"],
                 base_href=base_href)


def page_admin_files(base_href=""):
    s = _admin_sections()
    return _page("管理 · 文件",
                 _admin_topbar("files") + s["filecats"]
                 + s["js_filecats"],
                 base_href=base_href)


def page_admin_settings(base_href=""):
    s = _admin_sections()
    return _page("管理 · 通用设置",
                 _admin_topbar("settings") + s["settings"]
                 + s["js_settings"],
                 base_href=base_href)


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


def _dav_library_by_name(name):
    if not name or _MONTH_RE.fullmatch(name):
        return None
    for l in _libraries("video"):
        if l["name"] == name:
            return l
    return None


def _dav_resolve(segments):
    """Split a DAV path into (library, rest): a leading library
    name picks that library; a leading month or anything else
    lands in the default library (legacy paths keep working)."""
    if segments:
        lib = _dav_library_by_name(segments[0])
        if lib is not None:
            return lib, list(segments[1:])
    return _default_library("video"), list(segments)


def _dav_href(environ, segments, is_col=None):
    scheme = environ.get("HTTP_X_FORWARDED_PROTO") or "https"
    host = environ.get("HTTP_HOST") or "debi.su7.dpdns.org"
    path = "/dav/" + "/".join(
        urllib.parse.quote(s, safe="") for s in segments)
    if is_col is None:
        is_col = (not segments or len(segments) == 1
                  or (len(segments) == 2
                      and bool(_MONTH_RE.fullmatch(segments[1] or ""))))
    if is_col:
        path += "/"
    return "%s://%s%s" % (scheme, host, path)


def _folder_exists(library_id, folder):
    if not re.fullmatch(r"\d{4}-\d{2}", folder or ""):
        return False
    if folder == _folder_of(time.time()):
        return True  # the current upload month always exists
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM entries WHERE folder = ? "
            "AND library_id = ?", (folder, library_id)).fetchone()
    return row["c"] > 0


def _dav_folders(library_id):
    with _db() as conn:
        return [r["folder"] for r in conn.execute(
            "SELECT DISTINCT e.folder AS folder FROM entries e "
            "JOIN files f ON f.hash = e.hash "
            "WHERE e.library_id = ? AND f.kind = 'video' "
            "ORDER BY e.folder DESC", (library_id,))]


def _dav_entries_in(library_id, folder):
    with _db() as conn:
        return conn.execute(
            _ENTRY_COLS + "WHERE e.folder = ? AND e.library_id = ? "
            "AND f.kind = 'video' ORDER BY e.title",
            (folder, library_id)).fetchall()


def _dav_find(library_id, folder, filename):
    for row in _dav_entries_in(library_id, folder):
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
                   % _esc(_dav_href(environ, seg, is_col)))
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
            "WebDAV 密码尚未设置，请先在资源库管理页设置。".encode("utf-8"))
    if not configured:
        return _dav_send(
            start_response, "401 Unauthorized", b"unauthorized",
            headers=[("WWW-Authenticate", 'Basic realm="vidlib-dav"')])

    raw_segments = list(segments)

    # Library-aware resolution. The tree is now
    # /dav/<库名>/<YYYY-MM>/<文件>; legacy /dav/<YYYY-MM>/<文件>
    # and bare filenames resolve into the default library.
    dav_lib, rest = _dav_resolve(segments)
    if dav_lib is None:
        return _dav_send(start_response, "404 Not Found", b"not found")
    current_month = _folder_of(time.time())
    chute = ""

    if method == "PUT":
        # Normalize the PUT shapes down to (library, month,
        # filename, chute): root drops and folder-tree drops land
        # in the current month; the folder path of a tree drop is
        # kept as the entries' tag (as before).
        if not rest:
            return _dav_send(start_response, "409 Conflict",
                             "路径里缺少文件名。".encode("utf-8"))
        filename = rest[-1]
        lead = rest[:-1]
        if not lead:
            month = current_month
        elif len(lead) == 1 and _MONTH_RE.fullmatch(lead[0]):
            month = lead[0]
        elif _MONTH_RE.fullmatch(lead[0]):
            chute = "/".join(lead[1:])
            month = lead[0]
        else:
            chute = "/".join(lead)
            month = current_month
        segments = [month, filename]
    else:
        # Read shapes: a bare filename names a file in the current
        # month; folder-tree paths flatten to month + basename
        # (PROPFIND keeps the raw path for its virtual chute
        # listings below).
        if (len(rest) == 1
                and method in ("PROPFIND", "GET", "HEAD", "DELETE",
                               "MOVE")
                and not _MONTH_RE.fullmatch(rest[0])):
            rest = [current_month, rest[0]]
        if method in ("GET", "HEAD", "DELETE", "MOVE"):
            if len(rest) >= 2 and not _MONTH_RE.fullmatch(rest[0]):
                rest = [current_month, rest[-1]]
            elif len(rest) >= 3:
                rest = [rest[0], rest[-1]]
        segments = rest

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
        if len(raw_segments) == 0:
            items.append(([], True, 0, time.time(), "", ""))
            if depth == "1":
                for l in _libraries("video"):
                    items.append(([l["name"]], True, 0, 0, "", ""))
        elif len(rest) == 0:
            items.append(([dav_lib["name"]], True, 0,
                          time.time(), "", ""))
            if depth == "1":
                folders = _dav_folders(dav_lib["id"])
                if current_month not in folders:
                    folders.insert(0, current_month)
                for folder in folders:
                    items.append(([dav_lib["name"], folder], True,
                                  0, 0, "", ""))
        elif len(rest) == 1 and _MONTH_RE.fullmatch(rest[0]):
            folder = rest[0]
            if not _folder_exists(dav_lib["id"], folder):
                return _dav_send(start_response, "404 Not Found",
                                 b"not found")
            items.append(([dav_lib["name"], folder], True, 0,
                          time.time(), "", ""))
            if depth == "1":
                for row in _dav_entries_in(dav_lib["id"], folder):
                    name = row["title"] + "." + (row["ext"] or "")
                    items.append(([dav_lib["name"], folder, name],
                                  False, row["size"],
                                  row["created_at"],
                                  _mime_for(row["ext"]), row["hash"]))
        elif len(rest) >= 2:
            # File paths may be nested (folder-drop trees): the
            # file itself lives in a month folder, so resolve by
            # month + basename. Folder paths are virtual chute
            # collections whose members are the entries tagged
            # with the folder path.
            first_is_month = bool(_MONTH_RE.fullmatch(rest[0]))
            month = (rest[0] if first_is_month
                     else current_month)
            row = _dav_find(dav_lib["id"], month, rest[-1])
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
                    tag = "/".join(rest[1:])
                else:
                    tag = "/".join(rest)
                items.append((raw_segments, True, 0, time.time(), "", ""))
                if depth == "1":
                    for entry in _dav_entries_in(dav_lib["id"], month):
                        if (entry["tags"] or "") == tag:
                            name = (entry["title"] + "."
                                    + (entry["ext"] or ""))
                            items.append((raw_segments + [name], False,
                                          entry["size"],
                                          entry["created_at"],
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
        row = _dav_find(dav_lib["id"], segments[0], segments[1])
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
        if _kind(filename.rpartition(".")[2].lower()
                 if "." in filename else "") != "video":
            return _dav_send(
                start_response, "409 Conflict",
                "WebDAV 只收视频；图片 / 小说 / 其他文件请走网页上传。"
                .encode("utf-8"))
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
        existing = _dav_find(dav_lib["id"], folder, filename)
        replaced = existing is not None
        if replaced:
            # Free the title first so the new entry takes the clean
            # name; the replaced file itself is orphan-reaped later.
            delete_entry(existing["id"])
        try:
            entry_id, digest, _dup = ingest_staged_file(
                tmp_path, filename, folder=folder, tags=chute,
                library_id=dav_lib["id"])
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
        row = _dav_find(dav_lib["id"], segments[0], segments[1])
        if row is None:
            return _dav_send(start_response, "404 Not Found", b"not found")
        delete_entry(row["id"])
        return _dav_send(start_response, "204 No Content", b"")

    if method == "MOVE":
        if len(segments) != 2:
            return _dav_send(start_response, "404 Not Found", b"not found")
        row = _dav_find(dav_lib["id"], segments[0], segments[1])
        if row is None:
            return _dav_send(start_response, "404 Not Found", b"not found")
        dest = environ.get("HTTP_DESTINATION") or ""
        parsed = urllib.parse.urlparse(dest)
        dest_path = _dav_fix_path(urllib.parse.unquote(parsed.path))
        dest_segments = _dav_segments(dest_path)
        dlib, drest = _dav_resolve(dest_segments)
        if drest and not re.fullmatch(r"\d{4}-\d{2}", drest[0]):
            drest = [_folder_of(time.time()), drest[-1]]
        elif len(drest) >= 3:
            drest = [drest[0], drest[-1]]
        if len(drest) != 2:
            return _dav_send(start_response, "400 Bad Request",
                             "Destination 无效。".encode("utf-8"))
        if dlib is None or dlib["id"] != dav_lib["id"]:
            return _dav_send(
                start_response, "403 Forbidden",
                "不支持跨库移动；换库请在网页里操作。".encode("utf-8"))
        if drest[0] != segments[0]:
            return _dav_send(
                start_response, "403 Forbidden",
                "不支持跨文件夹移动；改月份请在网页里操作。".encode("utf-8"))
        new_title = os.path.splitext(drest[1])[0]
        if not new_title:
            return _dav_send(start_response, "400 Bad Request",
                             "新文件名无效。".encode("utf-8"))
        overwrite = (environ.get("HTTP_OVERWRITE") or "T").upper() != "F"
        target = _dav_find(dav_lib["id"], drest[0], drest[1])
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
            % _esc(_dav_href(environ, raw_segments))).encode("utf-8")
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
            % (token, _esc(_dav_href(environ, raw_segments)))).encode("utf-8")
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

    # Phone reader book source: own token, reached via Caddy at
    # /vidlib-api/* (prefix stripped before it gets here).
    if path == "/ext" or path.startswith("/ext/"):
        return ext_handle(environ, start_response, params)

    # Phone music clients (Subsonic): the protocol's own
    # credential, reached via Caddy at /vidlib-api/subsonic/*
    # (prefix stripped before it gets here).
    if path == "/rest" or path.startswith("/rest/"):
        return subsonic_handle(environ, start_response)

    if path == "/api/session":
        return _json(start_response, "200 OK",
                     {"ok": True, "authenticated": True,
                      "via": "platform", "username": "platform"})

    if path == "/" and method == "GET":
        body = page_hub(params, base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/wall" and method == "GET":
        body = page_wall(params, base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/images" and method == "GET":
        body = page_images(params, base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/novels" and method == "GET":
        body = page_novels(params, base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/notes" and method == "GET":
        body = page_notes(params, base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/files" and method == "GET":
        body = page_files(params, base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/music" and method == "GET":
        body = page_music(params, base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path.startswith("/read/") and method == "GET":
        entry = get_entry(path[len("/read/"):])
        if entry is None or entry["f_kind"] != "novel":
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        body = page_reader(entry, base_href)
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

    if path == "/feed" and method == "GET":
        body = page_feed(base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Cache-Control", "no-cache"),
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

    if path == "/admin/videos" and method == "GET":
        body = page_admin_videos(base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/admin/images" and method == "GET":
        body = page_admin_images(base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/admin/novels" and method == "GET":
        body = page_admin_novels(base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/admin/files" and method == "GET":
        body = page_admin_files(base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/admin/music" and method == "GET":
        body = page_admin_music(base_href)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/admin/settings" and method == "GET":
        body = page_admin_settings(base_href)
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
        if file_row is None:
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        if file_row["status"] == "archived":
            cached = _cache_file(file_row)
            if cached:
                return _serve_file_range(environ, start_response, cached,
                                         _mime_for(file_row["ext"]))
            return _dav_stream_archived(environ, start_response, file_row)
        local = _local_path(file_row)
        if not os.path.exists(local):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        return _serve_file_range(environ, start_response, local,
                                 _mime_for(file_row["ext"]))

    if path == "/api/music/list" and method == "GET":
        return _json(start_response, "200 OK",
                     {"ok": True, "items": _music_list(
                         (params.get("q") or "").strip())})

    if path.startswith("/api/play/") and method == "GET":
        entry = get_entry(path[len("/api/play/"):])
        if entry is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "视频不存在。"})
        with _db() as conn:
            conn.execute("UPDATE entries SET play_count = play_count + 1 "
                         "WHERE id = ?", (entry["id"],))
            conn.execute(
                "UPDATE entries SET seen_at = ? WHERE id = ? "
                "AND (seen_at IS NULL OR seen_at = 0)",
                (time.time(), entry["id"]))
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

    m = re.fullmatch(r"/api/novel/(\d+)", path)
    if m and method == "GET":
        entry = get_entry(int(m.group(1)))
        if entry is None or entry["f_kind"] != "novel":
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "小说不存在。"})
        meta = _novel_meta(entry["hash"]) or {}
        # Opening the reader counts as reading the book: a
        # pending mark starts the deferred AI pass in the
        # background; this response still serves the current
        # titles, with an unobtrusive analysing hint while
        # the pass runs.
        analyzing = _maybe_ai_trigger(entry["hash"])
        with _db() as conn:
            rd = conn.execute(
                "SELECT chapter, offset, percent FROM novel_read "
                "WHERE entry_id = ?", (entry["id"],)).fetchone()
        return _json(start_response, "200 OK", {
            "ok": True, "title": entry["title"],
            "encoding": meta.get("encoding") or "",
            "char_count": meta.get("char_count") or 0,
            "chapters": _novel_chapter_spans(meta),
            "ai_analyzing": analyzing,
            "read": ({"chapter": rd["chapter"], "offset": rd["offset"],
                      "percent": rd["percent"]} if rd else None)})

    m = re.fullmatch(r"/api/novel/(\d+)/chapter", path)
    if m and method == "GET":
        entry = get_entry(int(m.group(1)))
        if entry is None or entry["f_kind"] != "novel":
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "小说不存在。"})
        meta = _novel_meta(entry["hash"]) or {}
        chapters = _novel_chapter_spans(meta)
        try:
            idx = int(params.get("i") or "0")
        except Exception:
            idx = 0
        if not chapters or idx < 0 or idx >= len(chapters):
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "章节不存在。"})
        try:
            text = _novel_text(get_file(entry["hash"]))
        except Exception:
            return _json(start_response, "503 Service Unavailable",
                         {"ok": False,
                          "message": "正文暂不可用（归档拉取失败）。"})
        ch = chapters[idx]
        return _json(start_response, "200 OK", {
            "ok": True, "index": idx, "title": ch["t"],
            "text": text[ch["o"]:ch["e"]]})

    if path == "/api/novel/progress" and method == "POST":
        data = _read_json(environ)
        try:
            eid = int(data.get("entry_id"))
            nch = int(data.get("chapter") or 0)
            noff = int(data.get("offset") or 0)
            npct = float(data.get("percent") or 0)
        except Exception:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "参数无效。"})
        entry = get_entry(eid)
        if entry is None or entry["f_kind"] != "novel":
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "小说不存在。"})
        with _db() as conn:
            conn.execute(
                "INSERT INTO novel_read(entry_id, chapter, offset, "
                "percent, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(entry_id) DO UPDATE SET "
                "chapter=excluded.chapter, offset=excluded.offset, "
                "percent=excluded.percent, "
                "updated_at=excluded.updated_at",
                (eid, nch, noff, max(0.0, min(100.0, npct)),
                 int(time.time())))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/files/recategorize" and method == "POST":
        data = _read_json(environ)
        try:
            eid = int(data.get("id"))
        except Exception:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "参数无效。"})
        newcat = (data.get("category") or "").strip()
        catnames = {c["name"] for c in _file_categories()}
        entry = get_entry(eid)
        if entry is None or entry["f_kind"] != "file":
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "文件不存在。"})
        if newcat not in catnames:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "类别不存在。"})
        f = get_file(entry["hash"])
        oldcat = f["category"] or "其他"
        if newcat != oldcat:
            with _db() as conn:
                conn.execute(
                    "UPDATE files SET category = ?, updated_at = ? "
                    "WHERE hash = ?",
                    (newcat, int(time.time()), entry["hash"]))
            f2 = get_file(entry["hash"])
            old_remote = f["gpath"] or ""
            new_remote = _target_gpath(f2)
            if (f["status"] == "archived" and old_remote
                    and new_remote != old_remote):
                odir, _, oname = old_remote.rpartition("/")
                ndir = new_remote.rpartition("/")[0]
                moved = False
                try:
                    ALIST.fs_mkdir(ndir)
                    ALIST.fs_move(odir, ndir, [oname])
                    moved = _remote_size_drive(new_remote) is not None
                except Exception:
                    moved = False
                if moved:
                    with _db() as conn:
                        conn.execute(
                            "UPDATE files SET gpath = ? WHERE hash = ?",
                            (new_remote, entry["hash"]))
                else:
                    with _db() as conn:
                        conn.execute(
                            "UPDATE files SET category = ? WHERE hash = ?",
                            (oldcat, entry["hash"]))
                    return _json(start_response, "200 OK", {
                        "ok": False,
                        "message": "云端移动失败，类别未更改。"})
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/admin/libraries" and method == "POST":
        data = _read_json(environ)
        if data.get("add_name") is not None:
            name = (data.get("add_name") or "").strip()
            err = _validate_lib_name(name)
            if err:
                return _json(start_response, "400 Bad Request",
                             {"ok": False, "message": err})
            with _db() as conn:
                conn.execute(
                    "INSERT INTO libraries(name, kind, prefix, "
                    "is_default, created_at) VALUES(?, 'video', ?, 0, ?)",
                    (name, "视频/" + name, int(time.time())))
            return _json(start_response, "200 OK", {"ok": True})
        if data.get("delete_id") is not None:
            row = _library_by_id(_safe_lib(data.get("delete_id")))
            if (row is None or row["kind"] != "video"
                    or row["is_default"]):
                return _json(start_response, "400 Bad Request",
                             {"ok": False, "message": "不能删除该库。"})
            with _db() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE library_id = ?",
                    (row["id"],)).fetchone()[0]
                if n:
                    return _json(start_response, "400 Bad Request", {
                        "ok": False, "message": "库里还有条目，不能删除。"})
                conn.execute("DELETE FROM libraries WHERE id = ?",
                             (row["id"],))
            return _json(start_response, "200 OK", {"ok": True})
        renames = data.get("renames") or {}
        if isinstance(renames, dict):
            for k, v in renames.items():
                row = _library_by_id(k)
                if row is None or row["kind"] != "video":
                    continue
                name = (v or "").strip()
                if not name or name == row["name"]:
                    continue
                err = _validate_lib_name(name, exclude_id=row["id"])
                if err:
                    return _json(start_response, "400 Bad Request",
                                 {"ok": False, "message": err})
                with _db() as conn:
                    conn.execute(
                        "UPDATE libraries SET name = ? WHERE id = ?",
                        (name, row["id"]))
            return _json(start_response, "200 OK", {"ok": True})
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "message": "无操作。"})

    if path == "/api/admin/filecats" and method == "POST":
        data = _read_json(environ)
        items = data.get("items") or []
        cleaned = []
        seen_names = set()
        empty_rows = 0
        for it in items:
            name = ((it or {}).get("name") or "").strip()
            exts = _norm_exts((it or {}).get("exts") or "")
            if not name:
                continue
            if "/" in name or "\\" in name or len(name) > 20:
                return _json(start_response, "400 Bad Request", {
                    "ok": False, "message": "类别名无效：" + name})
            if name in seen_names:
                return _json(start_response, "400 Bad Request", {
                    "ok": False, "message": "类别名重复：" + name})
            seen_names.add(name)
            if not exts:
                empty_rows += 1
            cleaned.append((name, exts))
        if not cleaned:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "至少保留一个类别。"})
        if empty_rows > 1:
            return _json(start_response, "400 Bad Request", {
                "ok": False,
                "message": "只能有一个兜底类别（扩展名留空）。"})
        if empty_rows == 0:
            cleaned.append(("其他", ""))
        with _db() as conn:
            conn.execute("DELETE FROM file_categories")
            for i, (name, exts) in enumerate(cleaned):
                conn.execute(
                    "INSERT INTO file_categories(name, exts, sort, "
                    "builtin) VALUES(?,?,?,?)",
                    (name, exts, i, 1 if not exts else 0))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/feed" and method == "GET":
        mode = params.get("mode") or "random"
        seen_f = params.get("seen") or "unseen"
        mark_f = params.get("mark") or ""
        q = (params.get("q") or "").strip()
        try:
            seed = int(params.get("seed") or 0)
        except Exception:
            seed = 0
        try:
            offset = max(0, int(params.get("offset") or 0))
        except Exception:
            offset = 0
        try:
            limit = min(20, max(1, int(params.get("limit") or 12)))
        except Exception:
            limit = 12
        where, args = [], []
        if q:
            where.append(
                "(e.title LIKE ? OR e.tags LIKE ? OR e.source LIKE ?)")
            like = "%" + q + "%"
            args += [like, like, like]
        if seen_f == "unseen":
            where.append("(e.seen_at IS NULL OR e.seen_at = 0)")
        elif seen_f == "seen":
            where.append("(e.seen_at IS NOT NULL AND e.seen_at > 0)")
        if mark_f == "liked":
            where.append("e.mark = 1")
        elif mark_f == "disliked":
            where.append("e.mark = 2")
        elif mark_f != "any":
            where.append("e.mark != 2")
        lib_row = _library_by_id(params.get("lib")) \
            if params.get("lib") else None
        if lib_row is None or lib_row["kind"] != "video":
            lib_row = _default_library("video")
        if lib_row is not None:
            where.append("e.library_id = ?")
            args.append(lib_row["id"])
        sql = _ENTRY_COLS
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY e.id ASC"
        with _db() as conn:
            rows = conn.execute(sql, args).fetchall()
        rows = [r for r in rows if (r["kind"] or _kind(r["ext"])) == "video"]
        total = len(rows)
        if mode == "random":
            pool = [r for r in rows if _cache_file(r)]
            random.Random(seed).shuffle(rows)
            if pool:
                # Fake-random opener: the first slot is drawn from
                # the server-cache pool (deterministic for a given
                # seed, so pagination stays stable) and starts
                # instantly; the genuinely shuffled rest buffers
                # behind it.  A thin pool is topped up in the
                # background with random archived shorts.
                pick = pool[seed % len(pool)]
                rows.remove(pick)
                rows.insert(0, pick)
                if offset == 0 and len(pool) < 3:
                    extra = [r for r in rows[1:]
                             if r["status"] == "archived"
                             and (r["size"] or 0) <= SHORT_MAX_BYTES
                             and not _cache_file(r)]
                    random.Random(seed + 7).shuffle(extra)
                    for r in extra[:3 - len(pool)]:
                        _cache_warm(r)
        else:
            rows.sort(key=lambda r: (-(r["created_at"] or 0),
                                     -(r["id"] or 0)))
        page_rows = rows[offset:offset + limit]
        items = []
        for r in page_rows:
            items.append({
                "id": r["id"], "title": r["title"], "tags": r["tags"],
                "source": r["source"], "duration": r["duration"] or 0,
                "size": r["size"] or 0, "ext": r["ext"],
                "width": r["width"] or 0, "height": r["height"] or 0,
                "seen": bool(r["seen_at"]), "pos": r["play_pos"] or 0,
                "mark": r["mark"] or 0,
                "thumb": "thumb/" + r["hash"],
                "stream": "file/" + r["hash"],
                "short": (r["size"] or 0) <= SHORT_MAX_BYTES,
                "cached": bool(_cache_file(r)),
            })
        for r in page_rows[:4]:
            if r["status"] == "archived":
                _cache_warm(r)
        return _json(start_response, "200 OK",
                     {"ok": True, "items": items, "total": total,
                      "done": offset + len(page_rows) >= total})

    if path == "/api/seen" and method == "POST":
        data = _read_json(environ)
        entry = get_entry(data.get("id"))
        if entry is not None:
            with _db() as conn:
                conn.execute(
                    "UPDATE entries SET seen_at = ? WHERE id = ?",
                    (time.time() if data.get("seen") else 0,
                     entry["id"]))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/mark" and method == "POST":
        data = _read_json(environ)
        entry = get_entry(data.get("id"))
        if entry is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "视频不存在。"})
        try:
            mark = int(data.get("mark"))
        except Exception:
            mark = -1
        if mark not in (0, 1, 2):
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "标记值无效。"})
        now = time.time()
        with _db() as conn:
            if mark == 2:
                conn.execute(
                    "UPDATE entries SET mark = 2, mark_at = ?, "
                    "seen_at = CASE WHEN seen_at IS NULL OR "
                    "seen_at = 0 THEN ? ELSE seen_at END, "
                    "updated_at = ? WHERE id = ?",
                    (now, now, now, entry["id"]))
            else:
                conn.execute(
                    "UPDATE entries SET mark = ?, mark_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (mark, now if mark else 0, now, entry["id"]))
        _sync_mark_files()
        return _json(start_response, "200 OK",
                     {"ok": True, "mark": mark})

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
        if entry["f_kind"] == "music" and (
                "artist" in data or "album" in data):
            with _db() as conn:
                conn.execute(
                    "UPDATE files SET artist = ?, album = ?, "
                    "updated_at = ? WHERE hash = ?",
                    ((data.get("artist") or "")[:200],
                     (data.get("album") or "")[:200],
                     time.time(), entry["hash"]))
        _sync_mark_files()
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/entries/delete" and method == "POST":
        data = _read_json(environ)
        entry = get_entry(data.get("id"))
        if entry is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "视频不存在。"})
        delete_entry(entry["id"])
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/notes" and method == "POST":
        data = _read_json(environ)
        f = _note_clean_fields(data)
        if not f["title"]:
            return _json(start_response, "400 Bad Request",
                         {"ok": False,
                          "message": "书名不能为空。"})
        now = time.time()
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO novel_notes(title, author, "
                "status, note, dislike_reason, like_tags, "
                "like_text, link, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f["title"], f["author"], f["status"],
                 f["note"], f["dislike_reason"], f["like_tags"],
                 f["like_text"], f["link"], now, now))
            nid = cur.lastrowid
        return _json(start_response, "200 OK",
                     {"ok": True, "id": nid})

    if path == "/api/notes/get" and method == "GET":
        row = _note_get(params.get("id"))
        if row is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False,
                          "message": "笔记不存在。"})
        return _json(start_response, "200 OK",
                     {"ok": True, "note": _note_public(row)})

    if path == "/api/notes/update" and method == "POST":
        data = _read_json(environ)
        row = _note_get(data.get("id"))
        if row is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False,
                          "message": "笔记不存在。"})
        f = _note_clean_fields(data)
        if not f["title"]:
            return _json(start_response, "400 Bad Request",
                         {"ok": False,
                          "message": "书名不能为空。"})
        with _db() as conn:
            conn.execute(
                "UPDATE novel_notes SET title = ?, "
                "author = ?, status = ?, note = ?, "
                "dislike_reason = ?, like_tags = ?, "
                "like_text = ?, link = ?, updated_at = ? "
                "WHERE id = ?",
                (f["title"], f["author"], f["status"],
                 f["note"], f["dislike_reason"], f["like_tags"],
                 f["like_text"], f["link"], time.time(),
                 row["id"]))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/notes/verdict" and method == "POST":
        data = _read_json(environ)
        row = _note_get(data.get("id"))
        if row is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False,
                          "message": "笔记不存在。"})
        verdict = (data.get("verdict") or "").strip()
        if verdict not in ("reading", "candidate", "dropped"):
            return _json(start_response, "400 Bad Request",
                         {"ok": False,
                          "message": "裁决无效。"})
        with _db() as conn:
            conn.execute(
                "UPDATE novel_notes SET status = ?, "
                "updated_at = ? WHERE id = ?",
                (verdict, time.time(), row["id"]))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/notes/delete" and method == "POST":
        data = _read_json(environ)
        row = _note_get(data.get("id"))
        if row is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False,
                          "message": "笔记不存在。"})
        with _db() as conn:
            conn.execute(
                "DELETE FROM novel_notes WHERE id = ?",
                (row["id"],))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/notes/random" and method == "GET":
        with _db() as conn:
            row = conn.execute(
                "SELECT * FROM novel_notes WHERE status IN "
                "('unrated', 'candidate') ORDER BY RANDOM() "
                "LIMIT 1").fetchone()
        return _json(start_response, "200 OK",
                     {"ok": True,
                      "note": (_note_public(row)
                               if row else None)})



    if path == "/api/notes/mark" and method == "POST":
        data = _read_json(environ)
        status = (data.get("status") or "").strip()
        title = (data.get("title") or "").strip()[:200]
        link = (data.get("link") or "").strip()[:1000]
        if status not in ("candidate", "dropped"):
            return _json(start_response, "400 Bad Request",
                         {"ok": False,
                          "message": "状态无效。"})
        if not title:
            return _json(start_response, "400 Bad Request",
                         {"ok": False,
                          "message": "书名不能为空。"})
        if not link:
            return _json(start_response, "400 Bad Request",
                         {"ok": False,
                          "message": "链接不能为空。"})
        now = time.time()
        row = _note_by_link(link)
        if row is not None:
            with _db() as conn:
                conn.execute(
                    "UPDATE novel_notes SET status = ?, "
                    "updated_at = ? WHERE id = ?",
                    (status, now, row["id"]))
            return _json(start_response, "200 OK",
                         {"ok": True, "id": row["id"],
                          "status": status,
                          "status_label":
                              _NOTE_STATUS_LABEL[status],
                          "existed": True})
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO novel_notes(title, author, "
                "status, note, dislike_reason, like_tags, "
                "like_text, link, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (title, "", status, "", "", "[]", "",
                 link, now, now))
            nid = cur.lastrowid
        return _json(start_response, "200 OK",
                     {"ok": True, "id": nid, "status": status,
                      "status_label": _NOTE_STATUS_LABEL[status],
                      "existed": False})

    if path == "/api/notes/markstate" and method == "GET":
        try:
            eid = int(params.get("entry_id") or 0)
        except Exception:
            eid = 0
        found = None
        if eid:
            with _db() as conn:
                for r in conn.execute(
                        "SELECT * FROM novel_notes "
                        "WHERE link != '' ORDER BY id").fetchall():
                    if _note_read_id(r["link"]) == eid:
                        found = r
                        break
        return _json(start_response, "200 OK",
                     {"ok": True,
                      "status": (found["status"]
                                 if found is not None else ""),
                      "id": (found["id"]
                             if found is not None else 0)})

    if path == "/api/notes/booksearch" and method == "GET":
        q = (params.get("q") or "").strip()
        books = []
        if q:
            like = "%" + q + "%"
            with _db() as conn:
                rows = conn.execute(
                    "SELECT e.id AS e_id, e.title AS e_title, "
                    "m.char_count AS chars, m.chapters AS chaps "
                    "FROM entries e "
                    "JOIN files f ON f.hash = e.hash "
                    "LEFT JOIN novel_meta m ON m.hash = e.hash "
                    "WHERE f.kind = 'novel' AND "
                    "(e.title LIKE ? OR e.tags LIKE ?) "
                    "ORDER BY e.id DESC LIMIT 20",
                    (like, like)).fetchall()
            bmarks = _note_mark_map([r["e_id"] for r in rows])
            for r in rows:
                try:
                    nch = len(json.loads(r["chaps"] or "[]"))
                except Exception:
                    nch = 0
                bst = bmarks.get(r["e_id"], "")
                books.append({
                    "entry_id": r["e_id"], "title": r["e_title"],
                    "chars": _fmt_wchars(r["chars"] or 0),
                    "chapters": nch, "status": bst,
                    "status_label":
                        _NOTE_STATUS_LABEL.get(bst, "")})
        return _json(start_response, "200 OK",
                     {"ok": True, "books": books})

    if path == "/api/notes/note" and method == "POST":
        data = _read_json(environ)
        row = _note_get(data.get("id"))
        if row is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False,
                          "message": "笔记不存在。"})
        note = (data.get("note") or "").strip()[:4000]
        with _db() as conn:
            conn.execute(
                "UPDATE novel_notes SET note = ?, "
                "updated_at = ? WHERE id = ?",
                (note, time.time(), row["id"]))
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/uploads/check" and method == "POST":
        data = _read_json(environ)
        digest = (data.get("digest") or "").strip().lower()
        exists = (re.fullmatch(r"[a-f0-9]{64}", digest) is not None
                  and get_file(digest) is not None)
        return _json(start_response, "200 OK",
                     {"ok": True, "exists": exists})

    if path == "/api/uploads/register" and method == "POST":
        data = _read_json(environ)
        digest = (data.get("digest") or "").strip().lower()
        if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "digest 无效。"})
        tags = _merge_chute(data.get("tags") or "",
                            data.get("relpath") or "")
        entry_id = register_dup(digest, data.get("name") or "",
                                tags, data.get("source") or "",
                                _safe_lib(data.get("library_id")))
        if entry_id is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "文件不存在。"})
        return _json(start_response, "200 OK",
                     {"ok": True, "entry_id": entry_id,
                      "duplicate": True})

    if path == "/api/uploads/init" and method == "POST":
        data = _read_json(environ)
        try:
            uid = upload_init(data.get("name") or "", data.get("size") or 0,
                              data.get("title") or "", data.get("tags") or "",
                              data.get("source") or "",
                              data.get("relpath") or "",
                              _safe_lib(data.get("library_id")))
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
        lines = ["# 资源库批量下载列表（aria2 -i 使用；链接为限时签名链接，过期请重新导出）"]
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

    if path == "/api/admin/subsonic" and method == "POST":
        data = _read_json(environ)
        ss_user = (data.get("user") or "").strip()
        ss_pass = data.get("password") or ""
        if ss_pass and len(ss_pass) < 8:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "密码至少 8 位。"})
        creds = _load_creds()
        if ss_user:
            creds["subsonic_user"] = ss_user[:64]
        if ss_pass:
            creds["subsonic_pass"] = ss_pass
        _save_creds(creds)
        return _json(start_response, "200 OK",
                     {"ok": True,
                      "configured": bool(creds.get("subsonic_user")
                                         and creds.get("subsonic_pass"))})

    if path == "/api/admin/ext-token" and method == "POST":
        creds = _load_creds()
        creds["ext_token"] = secrets.token_urlsafe(24)
        _save_creds(creds)
        return _json(start_response, "200 OK",
                     {"ok": True, "url": _ext_import_url()})

    if path == "/api/admin/novel/title-len" and method == "POST":
        data = _read_json(environ)
        try:
            entry = get_entry(int(data.get("entry_id") or 0))
        except Exception:
            entry = None
        if entry is None or entry["f_kind"] != "novel":
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "小说不存在。"})
        try:
            n = int(data.get("title_len") or 0)
        except Exception:
            n = 0
        n = max(0, min(30, n))
        with _db() as conn:
            conn.execute(
                "UPDATE novel_meta SET title_len = ? WHERE hash = ?",
                (n, entry["hash"]))
        old = _novel_meta(entry["hash"]) or {}
        try:
            text = _novel_text(get_file(entry["hash"]))
        except Exception:
            return _json(start_response, "503 Service Unavailable",
                         {"ok": False,
                          "message": "正文暂不可用（归档拉取失败）。"})
        meta = _store_novel_meta(entry["hash"], text,
                                 old.get("encoding") or "")
        return _json(start_response, "200 OK",
                     {"ok": True, "chapters": len(meta["chapters"]),
                      "title_len": n})

    if path == "/api/admin/novel/ai-config" and method == "POST":
        data = _read_json(environ)
        key_in = (data.get("openrouter_key") or "").strip()
        if key_in:
            creds = _load_creds()
            creds["openrouter_key"] = key_in
            _save_creds(creds)
        kv_set("ai_title_model",
               (data.get("model") or "").strip()
               or _AI_DEFAULT_MODEL)
        kv_set("ai_title_enabled",
               "1" if data.get("enabled") else "0")
        if data.get("batch") is not None:
            try:
                bv = int(data.get("batch"))
            except Exception:
                bv = 0
            kv_set("ai_title_batch",
                   str(min(50, bv)) if bv > 0 else "")
        key, model, enabled = _ai_title_config()
        return _json(start_response, "200 OK",
                     {"ok": True, "configured": bool(key),
                      "model": model, "enabled": enabled,
                      "batch": _ai_batch_size()})

    if path == "/api/admin/novel/ai-test" and method == "POST":
        data = _read_json(environ)
        try:
            entry = get_entry(int(data.get("entry_id") or 0))
        except Exception:
            entry = None
        if entry is None or entry["f_kind"] != "novel":
            return _json(start_response, "404 Not Found",
                         {"ok": False, "message": "小说不存在。"})
        key, model, _enabled = _ai_title_config()
        if not key:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message":
                          "OpenRouter API key 未配置：请先在管理页"
                          "「小说切分」面板填入 key 再试跑。"})
        meta = _novel_meta(entry["hash"]) or {}
        try:
            text = _novel_text(get_file(entry["hash"]))
        except Exception:
            return _json(start_response, "503 Service Unavailable",
                         {"ok": False,
                          "message": "正文暂不可用（归档拉取失败）。"})
        _chs, stats, debug = _split_chapters_full(
            text, meta.get("title_len") or 0,
            ai_cfg={"key": key, "model": model,
                    "batch": _ai_batch_size()},
            ai_all_glued=True)
        cur = {}
        for c in meta.get("chapters") or []:
            cur[c.get("o")] = c.get("t")
        items = [{"num": d["num"], "offset": d["o"],
                  "window": d["window"], "ai_len": d["ai_len"],
                  "ai_raw": d["ai_raw"], "ai_title": d["ai_title"],
                  "current_title": cur.get(d["o"])} for d in debug]
        return _json(start_response, "200 OK",
                     {"ok": True, "model": model,
                      "candidates": len(items), "items": items,
                      "fallback": stats.get("fallback")})

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
