"""yt-audio: paste a YouTube link, get the audio file back.

Built for the Personal App Hub (stdlib-only WSGI, like zhihu-core).
Design notes:
- Downloads run through the open-source yt-dlp binary, fetched on first use
  from its GitHub releases and refreshed when older than 14 days.
- Fetches are queued jobs executed by one background thread with live
  progress (parsed from yt-dlp's progress template) and cancellation.
  The hub launcher forks after import, which kills import-time threads,
  so the worker thread and queue are per-process lazy singletons keyed
  by PID: they (re)start on first use inside the serving process.
- If YouTube demands a login for this server's IP, the user can paste a
  Netscape-format cookies.txt in the UI; it is stored server-side in
  .runtime (0600) and passed to yt-dlp via --cookies.
- Audio files live in .runtime/downloads and are purged automatically:
  anything older than RETENTION_HOURS (default 24) or beyond the total
  size cap (MAX_TOTAL_MB, default 1024) is deleted, checked on every call.
- External converter sites are offered as plain outbound links (the VPS
  does not proxy them); their list can be overridden with the
  EXTERNAL_SITES env var (JSON array of {name, template}).
"""
import io
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import urllib.request
import zipfile
from urllib.parse import parse_qs, quote, urlparse

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

RUNTIME = os.path.join(BASE, ".runtime")
DL_DIR = os.path.join(RUNTIME, "downloads")
BIN_DIR = os.path.join(RUNTIME, "bin")
INDEX_FILE = os.path.join(DL_DIR, "index.json")
DATA_DIR = _platform_data_dir(BASE)
COOKIE_FILE = os.path.join(DATA_DIR, "youtube-cookies.txt")
YTDLP_PATH = os.environ.get("YTDLP_BIN") or os.path.join(BIN_DIR, "yt-dlp")
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
YTDLP_MAX_AGE = 14 * 24 * 3600

RETENTION_SEC = int(float(os.environ.get("RETENTION_HOURS", "24")) * 3600)
MAX_TOTAL_BYTES = int(float(os.environ.get("MAX_TOTAL_MB", "1024")) * 1024 * 1024)
MAX_FILE_BYTES = 300 * 1024 * 1024
FETCH_TIMEOUT = 600

DEFAULT_SITES = [
    {"name": "Cobalt（干净无广告，需粘贴）", "template": "https://cobalt.tools/"},
    {"name": "Y2Mate（按视频打开）", "template": "https://www.y2mate.com/youtube/{id}"},
    {"name": "SSYouTube（按视频打开）", "template": "https://www.ssyoutube.com/watch?v={id}"},
]

MIME = {
    "m4a": "audio/mp4", "mp3": "audio/mpeg", "opus": "audio/ogg",
    "webm": "audio/webm", "ogg": "audio/ogg", "wav": "audio/wav",
}

_account, _account_from_env = auth.load_account()
_account_lock = threading.Lock()

# ---------------------------------------------------------------- storage

_index_lock = threading.Lock()


def _load_index():
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_index(index):
    tmp = INDEX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False)
    os.replace(tmp, INDEX_FILE)


def _remove_file(name):
    try:
        os.remove(os.path.join(DL_DIR, name))
    except OSError:
        pass


def purge_expired():
    """Delete expired/over-cap files and their index entries. Returns count."""
    now = time.time()
    removed = 0
    with _index_lock:
        index = _load_index()
        changed = False
        for fid in list(index):
            if not os.path.exists(os.path.join(DL_DIR, index[fid]["filename"])):
                del index[fid]
                changed = True
        for fid in list(index):
            if now - float(index[fid].get("created_at", 0)) > RETENTION_SEC:
                _remove_file(index[fid]["filename"])
                del index[fid]
                removed += 1
                changed = True
        total = sum(int(e.get("size", 0)) for e in index.values())
        if total > MAX_TOTAL_BYTES:
            for fid, entry in sorted(index.items(), key=lambda kv: kv[1].get("created_at", 0)):
                if total <= MAX_TOTAL_BYTES:
                    break
                _remove_file(entry["filename"])
                total -= int(entry.get("size", 0))
                del index[fid]
                removed += 1
                changed = True
        if changed:
            _save_index(index)
    try:
        for name in os.listdir(DL_DIR):
            path = os.path.join(DL_DIR, name)
            if name == "index.json":
                continue
            if os.path.isfile(path) and now - os.path.getmtime(path) > RETENTION_SEC:
                _remove_file(name)
    except FileNotFoundError:
        pass
    return removed


def list_files():
    purge_expired()
    with _index_lock:
        index = _load_index()
    items = []
    for fid, e in sorted(index.items(), key=lambda kv: -float(kv[1].get("created_at", 0))):
        items.append({
            "id": fid,
            "title": e.get("title") or e.get("video_id") or fid,
            "video_id": e.get("video_id"),
            "source_url": e.get("source_url"),
            "ext": e.get("ext"),
            "size": e.get("size"),
            "created_at": e.get("created_at"),
            "expires_at": float(e.get("created_at", 0)) + RETENTION_SEC,
        })
    return items

# ---------------------------------------------------------------- cookie store


def cookie_status():
    try:
        st = os.stat(COOKIE_FILE)
        return {"configured": True, "updated_at": st.st_mtime, "size": st.st_size}
    except OSError:
        return {"configured": False, "updated_at": None, "size": 0}


_COOKIE_KEY_FIELDS = ("__Secure-1PSID", "SAPISID", "HSID", "SSID", "APISID")
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")


def _header_cookie_to_netscape(text):
    """Convert a raw Cookie header string ("a=b; c=d", as copied from a
    browser's devtools or a request header) into Netscape cookies.txt
    lines for .youtube.com. Returns the converted text, or None when
    the input does not parse as a header string."""
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
    if not any(k in pairs for k in _COOKIE_KEY_FIELDS):
        return None
    lines = ["# Netscape HTTP Cookie File",
             "# converted from a raw Cookie header string"]
    for name in sorted(pairs):
        lines.append(".youtube.com\tTRUE\t/\tTRUE\t1893456000\t%s\t%s"
                     % (name, pairs[name]))
    return "\n".join(lines) + "\n"


def save_cookies(content):
    """Validate lightly and store a Netscape cookies.txt (0600).

    Accepts either a real Netscape cookies.txt (as exported by browser
    extensions) or a raw Cookie header string, which is converted.
    Returns a note when a conversion happened, else ""."""
    text = (content or "").strip()
    if len(text) < 20 or len(text) > 512 * 1024:
        raise ValueError("Cookie 内容长度不合理。")
    note = ""
    if "\t" not in text or "youtube.com" not in text:
        converted = _header_cookie_to_netscape(text)
        if converted is not None:
            text, note = converted, "已把浏览器 Cookie 串转换为 cookies.txt 格式保存。"
    if "youtube.com" not in text:
        raise ValueError("这不像 YouTube 的 Cookie（缺少 youtube.com 条目）。请粘贴 youtube.com 的 cookies.txt，或浏览器请求头里的 Cookie 串。")
    if not any(k in text for k in _COOKIE_KEY_FIELDS):
        raise ValueError("Cookie 里缺少登录关键字段（如 SAPISID/HSID），可能没有登录或导错了站点。")
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    tmp = COOKIE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, COOKIE_FILE)
    return note


def delete_cookies():
    try:
        os.remove(COOKIE_FILE)
        return True
    except OSError:
        return False

# ---------------------------------------------------------------- yt-dlp

_ytdlp_lock = threading.Lock()


def ensure_ytdlp():
    if os.environ.get("YTDLP_BIN"):
        return YTDLP_PATH
    with _ytdlp_lock:
        fresh = (
            os.path.exists(YTDLP_PATH)
            and time.time() - os.path.getmtime(YTDLP_PATH) < YTDLP_MAX_AGE
        )
        if fresh:
            return YTDLP_PATH
        os.makedirs(BIN_DIR, exist_ok=True)
        tmp = YTDLP_PATH + ".tmp"
        req = urllib.request.Request(YTDLP_URL, headers={"User-Agent": "yt-audio/1.1"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        os.chmod(tmp, 0o755)
        os.replace(tmp, YTDLP_PATH)
        return YTDLP_PATH


_deno_lock = threading.Lock()


def ensure_deno():
    """yt-dlp needs a JS runtime for the full YouTube format list; without
    one, some videos offer no audio formats at all ('Requested format is
    not available'). Best-effort: return the deno path or None."""
    path = os.path.join(BIN_DIR, "deno")
    with _deno_lock:
        try:
            if os.path.exists(path) and time.time() - os.path.getmtime(path) < 30 * 86400:
                return path
            os.makedirs(BIN_DIR, exist_ok=True)
            req = urllib.request.Request(DENO_URL, headers={"User-Agent": "yt-audio/1.1"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                data = zf.read("deno")
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o755)
            os.replace(tmp, path)
            return path
        except Exception:  # noqa: BLE001
            return path if os.path.exists(path) else None


_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}


def parse_youtube_url(raw):
    """Return (video_id, canonical_url) or (None, None) for non-YouTube input."""
    if not raw:
        return None, None
    text = raw.strip()
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/|/v/)([A-Za-z0-9_-]{11})", text)
    candidate = None
    if "://" not in text and _VIDEO_ID.match(text):
        candidate = text
    elif m:
        candidate = m.group(1)
    if not candidate:
        return None, None
    try:
        host = (urlparse(text).hostname or "").lower()
    except Exception:
        host = ""
    if host and host not in _YT_HOSTS:
        return None, None
    return candidate, "https://www.youtube.com/watch?v=" + candidate

# ---------------------------------------------------------------- jobs
# The hub launcher forks after importing this module, which silently kills
# any thread started at import time. The queue and worker thread are
# therefore created lazily inside the serving process and recreated when
# the PID changes, so a dead worker can never strand the queue.

_jobs = {}
_jobs_lock = threading.Lock()
_runtime_state = {"pid": None, "queue": None, "thread": None}
_state_lock = threading.Lock()

_PROGRESS_RE = re.compile(
    r"^downloading\|([^|]*)\|([^|]*)\|([^|]*)$")


def _ensure_worker():
    pid = os.getpid()
    with _state_lock:
        if _runtime_state["pid"] == pid and _runtime_state["thread"] is not None \
                and _runtime_state["thread"].is_alive():
            return
        q = queue.Queue()
        t = threading.Thread(target=_worker, args=(q,), daemon=True,
                             name="yt-audio-worker")
        _runtime_state.update(pid=pid, queue=q, thread=t)
        t.start()


def _public_job(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _new_job(video_id, url):
    _ensure_worker()
    jid = secrets.token_hex(6)
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid, "video_id": video_id, "url": url,
            "status": "queued", "message": "排队中",
            "progress": None, "file_id": None,
            "created_at": time.time(), "_proc": None, "_cancel": False,
        }
        # keep only the newest 50 jobs in memory
        if len(_jobs) > 50:
            for old in sorted(_jobs, key=lambda k: _jobs[k]["created_at"])[:-50]:
                if _jobs[old]["status"] in ("done", "error", "cancelled"):
                    del _jobs[old]
    _runtime_state["queue"].put(jid)
    return jid


def _set_job(jid, **kw):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(kw)


def _get_job(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
        if not job:
            return None
        out = _public_job(job)
        if job["status"] == "queued":
            ahead = sum(
                1 for j in _jobs.values()
                if j["created_at"] < job["created_at"] and j["status"] in ("queued", "running"))
            out["position"] = ahead + 1
        return out


def _cancel_job(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
        if not job or job["status"] in ("done", "error", "cancelled"):
            return False
        job["_cancel"] = True
        proc = job.get("_proc")
        if job["status"] == "queued":
            job["status"] = "cancelled"
            job["message"] = "已取消"
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass
    return True


def _clean_partials(video_id):
    try:
        for name in os.listdir(DL_DIR):
            if name.startswith(video_id + "."):
                _remove_file(name)
    except FileNotFoundError:
        pass


def _run_attempt(jid, cmd):
    """Run one yt-dlp attempt, streaming progress into the job.
    Returns (returncode, output_tail)."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return None, ["无法启动 yt-dlp：%s" % exc]
    _set_job(jid, _proc=proc, message="正在解析并下载…")
    watchdog = threading.Timer(FETCH_TIMEOUT, proc.kill)
    watchdog.start()
    output_tail = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            m = _PROGRESS_RE.match(line.strip())
            if m:
                pct, speed, eta = (g.strip() for g in m.groups())
                _set_job(jid, message="下载中",
                         progress={"percent": pct, "speed": speed, "eta": eta})
            elif line.strip():
                output_tail.append(line.strip())
                if len(output_tail) > 20:
                    output_tail.pop(0)
        proc.wait()
    finally:
        watchdog.cancel()
        _set_job(jid, _proc=None)
    return proc.returncode, output_tail


def _run_fetch(jid):
    job = _get_job(jid)
    video_id, url = job["video_id"], job["url"]
    with _jobs_lock:
        current = _jobs.get(jid)
        if not current or current.get("_cancel"):
            _set_job(jid, status="cancelled", message="已取消")
            return
    _set_job(jid, status="running", message="正在启动 yt-dlp…", progress=None)
    try:
        binary = ensure_ytdlp()
    except Exception as exc:  # noqa: BLE001
        _set_job(jid, status="error", message="yt-dlp 准备失败：%s" % exc)
        return
    out_tmpl = os.path.join(DL_DIR, "%s.%%(ext)s" % video_id)
    base = [
        binary, "--no-playlist", "--no-warnings",
        "-f", "ba[ext=m4a]/ba/bestaudio",
        "--max-filesize", str(MAX_FILE_BYTES),
        "--socket-timeout", "30", "--retries", "3",
        "--write-info-json",
        "--progress-template",
        "%(progress.status)s|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        "-o", out_tmpl,
    ]
    runtime = ensure_deno()
    runtime_args = ["--js-runtimes", "deno:" + runtime] if runtime else []
    attempts = [base + runtime_args
                + (["--cookies", COOKIE_FILE] if cookie_status()["configured"] else [])]
    # Second attempt for videos whose default format list carries no audio
    # format: a different player client sees a different list. Cookies are
    # not supported by that client, so the retry goes without them.
    attempts.append(base + runtime_args
                    + ["--extractor-args", "youtube:player_client=android_vr"])
    returncode, output_tail = None, []
    for attempt_no, attempt_cmd in enumerate(attempts):
        if attempt_no:
            _clean_partials(video_id)
            _remove_file(video_id + ".info.json")
            _set_job(jid, message="默认方式没有可用的纯音频格式，换一种方式重试…",
                     progress=None)
        returncode, output_tail = _run_attempt(jid, attempt_cmd + [url])
        with _jobs_lock:
            if _jobs.get(jid, {}).get("_cancel"):
                break
        if returncode == 0:
            break
        if "format is not available" not in " ".join(output_tail).lower():
            break
    with _jobs_lock:
        cancelled = bool(_jobs.get(jid, {}).get("_cancel"))
    media = None
    for name in sorted(os.listdir(DL_DIR)):
        if name.startswith(video_id + ".") and not name.endswith((".info.json", ".part", ".ytdl")):
            media = name
            break
    info = {}
    info_path = os.path.join(DL_DIR, video_id + ".info.json")
    if os.path.exists(info_path):
        try:
            with open(info_path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
        except Exception:
            info = {}
    if cancelled:
        _clean_partials(video_id)
        _set_job(jid, status="cancelled", message="已取消", progress=None)
        return
    if returncode != 0 or not media:
        tail = output_tail[-1][:300] if output_tail else "未知错误"
        blob = " ".join(output_tail)
        if "Sign in to confirm" in blob or "confirm you're not a bot" in blob:
            if cookie_status()["configured"]:
                tail = "YouTube 仍要求登录验证——已配置的 Cookie 可能已过期，请在下方重新粘贴，或改用外部解析站"
            else:
                tail = "YouTube 要求登录验证（机房 IP 被拦）：请在下方粘贴你的 YouTube Cookie 后重试，或改用外部解析站"
        elif "format is not available" in blob.lower():
            tail = "该视频没有可用的纯音频格式（已自动换方式重试仍失败）。可改用下方外部解析站，或稍后再试。"
        elif returncode == -9:
            tail = "下载超时（%d 秒），已终止" % FETCH_TIMEOUT
        _clean_partials(video_id)
        _set_job(jid, status="error", message="下载失败：" + tail, progress=None)
        return
    if os.path.exists(info_path):
        _remove_file(video_id + ".info.json")
    path = os.path.join(DL_DIR, media)
    fid = secrets.token_hex(6)
    with _index_lock:
        index = _load_index()
        for old_id, entry in list(index.items()):
            if entry.get("video_id") == video_id and entry.get("filename") != media:
                _remove_file(entry["filename"])
                del index[old_id]
            elif entry.get("filename") == media:
                del index[old_id]
        index[fid] = {
            "video_id": video_id,
            "title": info.get("title") or video_id,
            "duration": info.get("duration"),
            "source_url": url,
            "filename": media,
            "ext": media.rsplit(".", 1)[-1].lower(),
            "size": os.path.getsize(path),
            "created_at": time.time(),
        }
        _save_index(index)
    _set_job(jid, status="done", message="完成", file_id=fid, progress=None)
    purge_expired()


def _worker(q):
    while True:
        jid = q.get()
        try:
            _run_fetch(jid)
        except Exception as exc:  # noqa: BLE001
            _set_job(jid, status="error", message="内部错误：%s" % exc)
        finally:
            q.task_done()


os.makedirs(DL_DIR, exist_ok=True)
purge_expired()

# ---------------------------------------------------------------- http

COOKIE_NAME = "yta_session"


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


def _external_sites():
    raw = os.environ.get("EXTERNAL_SITES", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            sites = [{"name": str(s["name"]), "template": str(s["template"])}
                     for s in data if isinstance(s, dict) and s.get("name") and s.get("template")]
            if sites:
                return sites
        except Exception:
            pass
    return DEFAULT_SITES

def handle_session(environ, start_response):
    account = _current_account()
    authed, via, username = _auth_state(environ)
    return _json(start_response, "200 OK", {
        "ok": True,
        "setup_required": False,
        "authenticated": authed,
        "via": via,
        "username": username if authed else None,
        "retention_hours": RETENTION_SEC // 3600,
        "cookie": cookie_status(),
        "sites": _external_sites(),
    })


def handle_fetch(environ, start_response):
    data = _read_json(environ)
    video_id, url = parse_youtube_url(str(data.get("url") or ""))
    if not video_id:
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "bad_url",
                      "message": "只支持 YouTube 链接（watch / youtu.be / shorts / live）。"})
    jid = _new_job(video_id, url)
    return _json(start_response, "200 OK", {"ok": True, "job_id": jid})


def handle_job(environ, start_response, jid):
    _ensure_worker()
    job = _get_job(jid)
    if not job:
        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "no_job", "message": "任务不存在或服务已重启。"})
    return _json(start_response, "200 OK", {"ok": True, "job": job})


def handle_cancel(environ, start_response, jid):
    ok = _cancel_job(jid)
    return _json(start_response, "200 OK", {"ok": ok})


def handle_files(environ, start_response):
    return _json(start_response, "200 OK", {"ok": True, "files": list_files()})


def handle_delete(environ, start_response, fid):
    with _index_lock:
        index = _load_index()
        entry = index.pop(fid, None)
        if entry:
            _remove_file(entry["filename"])
            _save_index(index)
    return _json(start_response, "200 OK", {"ok": True, "deleted": bool(entry)})


def handle_cookie_save(environ, start_response):
    data = _read_json(environ, limit=600 * 1024)
    try:
        note = save_cookies(str(data.get("content") or ""))
    except ValueError as exc:
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "bad_cookie", "message": str(exc)})
    return _json(start_response, "200 OK",
                 {"ok": True, "cookie": cookie_status(), "message": note})


def handle_cookie_delete(environ, start_response):
    return _json(start_response, "200 OK", {"ok": True, "deleted": delete_cookies()})


def handle_download(environ, start_response, fid):
    with _index_lock:
        index = _load_index()
    entry = index.get(fid)
    if not entry:
        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "no_file", "message": "文件不存在或已被清理。"})
    path = os.path.join(DL_DIR, entry["filename"])
    if not os.path.exists(path):
        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "no_file", "message": "文件不存在或已被清理。"})
    size = os.path.getsize(path)
    query = parse_qs(environ.get("QUERY_STRING", ""))
    as_attach = query.get("dl", ["0"])[0] == "1"
    mime = MIME.get(entry.get("ext", ""), "application/octet-stream")
    safe_title = re.sub(r'[\r\n"\\/]+', "_", entry.get("title") or entry["filename"])[:120]
    disp = "%s; filename*=UTF-8''%s" % (
        "attachment" if as_attach else "inline",
        quote((safe_title + "." + entry.get("ext", "m4a")).encode("utf-8")),
    )
    start, end, status = 0, size - 1, "200 OK"
    range_hdr = environ.get("HTTP_RANGE", "")
    m = re.match(r"bytes=(\d*)-(\d*)$", range_hdr or "")
    if m and size:
        s_raw, e_raw = m.groups()
        if s_raw:
            start = int(s_raw)
            end = int(e_raw) if e_raw else size - 1
        elif e_raw:
            start = max(0, size - int(e_raw))
        end = min(end, size - 1)
        if start > end:
            start_response("416 Range Not Satisfiable",
                           [("Content-Range", "bytes */%d" % size), ("Content-Length", "0")])
            return [b""]
        status = "206 Partial Content"
    length = end - start + 1
    headers = [
        ("Content-Type", mime),
        ("Content-Length", str(length)),
        ("Accept-Ranges", "bytes"),
        ("Content-Disposition", disp),
    ]
    if status.startswith("206"):
        headers.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))
    start_response(status, headers)

    def stream():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1 << 16, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return stream()

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YouTube 音频下载</title>
<style>
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0; background: #0f1115; color: #e8eaed; }
  main { max-width: 720px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 20px; margin: 8px 0 4px; }
  .sub { color: #9aa0a6; font-size: 13px; margin-bottom: 20px; }
  .card { background: #171a21; border: 1px solid #262b36; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  label { display: block; font-size: 13px; color: #9aa0a6; margin: 10px 0 6px; }
  input[type=text], input[type=password], textarea { width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px; border: 1px solid #333a47; background: #0f1115; color: #e8eaed; font-size: 15px; }
  textarea { font-size: 12px; min-height: 110px; }
  button { background: #3b82f6; color: #fff; border: 0; border-radius: 8px; padding: 10px 16px; font-size: 15px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  button.ghost { background: #262b36; }
  button.danger { background: #5f2120; }
  button.small { padding: 6px 10px; font-size: 13px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .msg { font-size: 14px; margin-top: 10px; min-height: 20px; white-space: pre-wrap; word-break: break-all; }
  .err { color: #f28b82; } .ok { color: #81c995; }
  .bar { height: 8px; background: #262b36; border-radius: 4px; overflow: hidden; margin-top: 10px; }
  .bar > div { height: 100%; background: #3b82f6; width: 0%; transition: width .5s; }
  .file { border-top: 1px solid #262b36; padding: 12px 0; }
  .file:first-child { border-top: 0; }
  .file .t { font-size: 15px; margin-bottom: 4px; word-break: break-all; }
  .meta { color: #9aa0a6; font-size: 12px; margin-bottom: 8px; }
  audio { width: 100%; height: 36px; margin: 6px 0; }
  a { color: #8ab4f8; }
  .hidden { display: none; }
  details summary { cursor: pointer; font-size: 14px; }
</style>
</head>
<body>
<main>
  <h1>YouTube 音频下载</h1>
  <div class="sub">粘贴链接，服务器用 yt-dlp 取回音频（低码率优先，不转码）。文件保留 <span id="retention">24</span> 小时后自动清理。</div>

  <section id="setupCard" class="card hidden">
    <div>首次设置 · 创建管理员账户</div>
    <label>账户名</label><input id="setupUser" type="text" autocomplete="username">
    <label>设置密码（至少 10 位）</label><input id="setupPw" type="password" autocomplete="new-password">
    <label>再次输入密码</label><input id="setupPw2" type="password" autocomplete="new-password">
    <div class="row" style="margin-top:14px"><button id="setupBtn">创建账户并登录</button></div>
    <div class="msg err" id="setupMsg"></div>
  </section>

  <section id="loginCard" class="card hidden">
    <div>登录</div>
    <label>账户名</label><input id="loginUser" type="text" autocomplete="username">
    <label>密码</label><input id="loginPw" type="password" autocomplete="current-password">
    <div class="row" style="margin-top:14px"><button id="loginBtn">登录</button></div>
    <div class="msg err" id="loginMsg"></div>
  </section>

  <section id="mainCard" class="hidden">
    <div class="card">
      <label>YouTube 链接</label>
      <input id="url" type="text" placeholder="https://www.youtube.com/watch?v=...">
      <div class="row" style="margin-top:12px">
        <button id="fetchBtn">获取音频</button>
        <button id="pasteBtn" class="ghost">从剪贴板粘贴</button>
        <button id="cancelBtn" class="danger hidden">终止任务</button>
        <button id="logoutBtn" class="ghost">退出</button>
      </div>
      <div class="msg" id="jobMsg"></div>
      <div class="bar hidden" id="jobBar"><div></div></div>
      <div class="sub" style="margin:14px 0 6px">本站不可用时，可改用外部解析站（新标签页打开，本站只做入口）：</div>
      <div class="row" id="sites"></div>
    </div>
    <div class="card">
      <details>
        <summary>YouTube Cookie（被要求登录验证时用）</summary>
        <div class="meta" id="cookieStatus" style="margin-top:10px"></div>
        <div class="meta">两种格式都可以：浏览器扩展（如 “Get cookies.txt LOCALLY”）导出的 cookies.txt，或浏览器请求头里的 Cookie 串（name=value; name=value 形式），粘贴保存即可，后者会自动转换。Cookie 只存在服务器本地，供 yt-dlp 使用；过期后重新粘贴即可。</div>
        <textarea id="cookieText" placeholder="# Netscape HTTP Cookie File ... 或 SID=...; HSID=...; SAPISID=... 形式的 Cookie 串"></textarea>
        <div class="row" style="margin-top:10px">
          <button id="cookieSave" class="small">保存 Cookie</button>
          <button id="cookieDel" class="ghost small">删除已存 Cookie</button>
        </div>
        <div class="msg" id="cookieMsg"></div>
      </details>
    </div>
    <div class="card">
      <div style="margin-bottom:6px">已获取的音频</div>
      <div id="files"><div class="meta">暂无</div></div>
    </div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let session = null, pollTimer = null, currentJob = null;

async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {"Content-Type": "application/json"}}, opts || {}));
  return {status: r.status, data: await r.json().catch(() => ({}))};
}
function fmtSize(n) { if (!n && n !== 0) return "?"; const u = ["B","KB","MB","GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + u[i]; }
function fmtTime(ts) { return new Date(ts * 1000).toLocaleString(); }

async function boot() {
  const {data} = await api("api/session");
  session = data;
  $("retention").textContent = data.retention_hours;
  $("setupCard").classList.toggle("hidden", !data.setup_required || data.authenticated);
  $("loginCard").classList.toggle("hidden", data.setup_required || data.authenticated);
  $("mainCard").classList.toggle("hidden", !data.authenticated);
  if (data.authenticated) {
    renderSites(); loadFiles(); renderCookie();
    if (data.via === "panel") { $("jobMsg").className = "msg ok"; $("jobMsg").textContent = "已通过平台登录（" + data.username + "），无需再次验证。"; }
  }
}
function renderCookie() {
  const c = (session && session.cookie) || {configured: false};
  $("cookieStatus").textContent = c.configured ? ("已配置 Cookie（" + fmtTime(c.updated_at) + " 更新，" + fmtSize(c.size) + "）") : "未配置 Cookie";
}
function renderSites() {
  const box = $("sites"); box.innerHTML = "";
  (session.sites || []).forEach(s => {
    const b = document.createElement("button");
    b.className = "ghost small"; b.textContent = s.name;
    b.onclick = () => openSite(s.template);
    box.appendChild(b);
  });
}
function currentVideoId() {
  const v = $("url").value;
  const m = v.match(/(?:v=|youtu\\.be\\/|\\/shorts\\/|\\/live\\/|\\/embed\\/|\\/v\\/)([A-Za-z0-9_-]{11})/);
  if (m) return m[1];
  return /^[A-Za-z0-9_-]{11}$/.test(v.trim()) ? v.trim() : null;
}
async function openSite(template) {
  const url = $("url").value.trim(), id = currentVideoId();
  try { await navigator.clipboard.writeText(url); } catch (e) {}
  let target = template;
  if (target.includes("{id}")) {
    if (!id) { $("jobMsg").textContent = "先粘贴一个有效的 YouTube 链接，再用这个解析站。"; $("jobMsg").className = "msg err"; return; }
    target = target.replace("{id}", id);
  }
  target = target.replace("{url}", encodeURIComponent(url));
  window.open(target, "_blank");
}
async function loadFiles() {
  const {data} = await api("api/files");
  const box = $("files");
  if (!data.files || !data.files.length) { box.innerHTML = '<div class="meta">暂无</div>'; return; }
  box.innerHTML = "";
  data.files.forEach(f => {
    const div = document.createElement("div"); div.className = "file";
    const left = Math.max(0, Math.round((f.expires_at - Date.now() / 1000) / 3600));
    div.innerHTML = '<div class="t"></div><div class="meta"></div>' +
      '<audio controls preload="none"></audio>' +
      '<div class="row"><a class="dl" download>下载文件</a><button class="ghost small del">删除</button></div>';
    div.querySelector(".t").textContent = f.title;
    div.querySelector(".meta").textContent = fmtSize(f.size) + " · " + f.ext.toUpperCase() + " · " + fmtTime(f.created_at) + " · 约 " + left + " 小时后自动清理";
    div.querySelector("audio").src = "files/" + f.id;
    const dl = div.querySelector(".dl"); dl.href = "files/" + f.id + "?dl=1";
    div.querySelector(".del").onclick = async () => { await api("api/files/" + f.id, {method: "DELETE"}); loadFiles(); };
    box.appendChild(div);
  });
}
function setJobUI(active) {
  $("fetchBtn").disabled = active;
  $("cancelBtn").classList.toggle("hidden", !active);
  if (!active) $("jobBar").classList.add("hidden");
}
function stopPolling(finalText, cls) {
  clearInterval(pollTimer); pollTimer = null; currentJob = null;
  setJobUI(false);
  if (finalText !== undefined) { $("jobMsg").className = "msg " + (cls || ""); $("jobMsg").textContent = finalText; }
}
async function doFetch() {
  const url = $("url").value.trim();
  if (!url) return;
  setJobUI(true);
  $("jobMsg").className = "msg"; $("jobMsg").textContent = "已提交…";
  const {data} = await api("api/fetch", {method: "POST", body: JSON.stringify({url})});
  if (!data.ok) { stopPolling(data.message || "提交失败", "err"); return; }
  currentJob = data.job_id;
  const t0 = Date.now();
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const r = await api("api/jobs/" + currentJob);
    const job = r.data.job;
    const secs = Math.round((Date.now() - t0) / 1000);
    if (!job) { stopPolling("任务已失效（服务可能重启过），请重新提交。", "err"); return; }
    if (job.status === "done") {
      stopPolling("完成，已加入下方列表。", "ok"); loadFiles();
    } else if (job.status === "error") {
      stopPolling(job.message || "下载失败", "err");
    } else if (job.status === "cancelled") {
      stopPolling("已取消。", "");
    } else if (job.status === "queued") {
      $("jobMsg").className = "msg";
      $("jobMsg").textContent = "排队中（第 " + (job.position || 1) + " 位，已等 " + secs + " 秒）";
    } else if (job.status === "running") {
      $("jobMsg").className = "msg";
      const p = job.progress;
      if (p && p.percent && p.percent.includes("%")) {
        const pct = parseFloat(p.percent);
        $("jobBar").classList.remove("hidden");
        $("jobBar").firstElementChild.style.width = pct + "%";
        $("jobMsg").textContent = "下载中 " + p.percent + " · " + p.speed + " · 剩余 " + p.eta + "（已用 " + secs + " 秒）";
      } else {
        $("jobMsg").textContent = (job.message || "处理中") + "（" + secs + " 秒）";
      }
    }
  }, 1500);
}
$("fetchBtn").onclick = doFetch;
$("cancelBtn").onclick = async () => {
  if (!currentJob) return;
  $("cancelBtn").disabled = true;
  await api("api/jobs/" + currentJob, {method: "DELETE"});
  setTimeout(() => { $("cancelBtn").disabled = false; }, 1000);
};
$("pasteBtn").onclick = async () => { try { $("url").value = await navigator.clipboard.readText(); } catch (e) { $("jobMsg").textContent = "浏览器未授权读取剪贴板，请手动粘贴。"; $("jobMsg").className = "msg err"; } };
$("logoutBtn").onclick = async () => { await api("api/logout", {method: "POST", body: "{}"}); location.reload(); };
$("cookieSave").onclick = async () => {
  const {data} = await api("api/cookie", {method: "POST", body: JSON.stringify({content: $("cookieText").value})});
  $("cookieMsg").className = "msg " + (data.ok ? "ok" : "err");
  $("cookieMsg").textContent = data.ok ? (data.message || "已保存，下次下载生效。") : (data.message || "保存失败");
  if (data.ok) { session.cookie = data.cookie; renderCookie(); $("cookieText").value = ""; }
};
$("cookieDel").onclick = async () => {
  const {data} = await api("api/cookie", {method: "DELETE"});
  if (data.ok) { session.cookie = {configured: false}; renderCookie(); $("cookieMsg").className = "msg"; $("cookieMsg").textContent = "已删除。"; }
};
$("setupBtn").onclick = async () => {
  if ($("setupPw").value !== $("setupPw2").value) { $("setupMsg").textContent = "两次输入的密码不一致。"; return; }
  const {data} = await api("api/setup", {method: "POST", body: JSON.stringify({username: $("setupUser").value, password: $("setupPw").value})});
  if (data.ok) boot(); else $("setupMsg").textContent = data.message || "设置失败";
};
$("loginBtn").onclick = async () => {
  const {data} = await api("api/login", {method: "POST", body: JSON.stringify({username: $("loginUser").value, password: $("loginPw").value})});
  if (data.ok) boot(); else $("loginMsg").textContent = data.message || "登录失败";
};
boot();
</script>
</body>
</html>
"""


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"
    try:
        if path == "/health":
            return _json(start_response, "200 OK", {"ok": True, "service": "yt-audio"})
        if path == "/" and method == "GET":
            body = PAGE.encode("utf-8")
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                      ("Content-Length", str(len(body)))])
            return [body]
        if path == "/api/session" and method == "GET":
            return handle_session(environ, start_response)
        if path == "/api/logout" and method == "POST":
            return _json(start_response, "200 OK", {"ok": True},
                         [_session_cookie(environ, "", 0)])
        if not _authed(environ):
            return _json(start_response, "401 Unauthorized",
                         {"ok": False, "error": "login_required", "message": "请先登录。"})
        if path == "/api/fetch" and method == "POST":
            return handle_fetch(environ, start_response)
        if path.startswith("/api/jobs/") and method == "GET":
            return handle_job(environ, start_response, path.rsplit("/", 1)[-1])
        if path.startswith("/api/jobs/") and method == "DELETE":
            return handle_cancel(environ, start_response, path.rsplit("/", 1)[-1])
        if path == "/api/files" and method == "GET":
            return handle_files(environ, start_response)
        if path.startswith("/api/files/") and method == "DELETE":
            return handle_delete(environ, start_response, path.rsplit("/", 1)[-1])
        if path == "/api/cookie" and method == "POST":
            return handle_cookie_save(environ, start_response)
        if path == "/api/cookie" and method == "DELETE":
            return handle_cookie_delete(environ, start_response)
        if path.startswith("/files/") and method == "GET":
            return handle_download(environ, start_response, path.rsplit("/", 1)[-1])
        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "not_found", "message": "路径不存在。"})
    except Exception as exc:  # noqa: BLE001
        return _json(start_response, "500 Internal Server Error",
                     {"ok": False, "error": "internal", "message": "服务器内部错误：%s" % exc})


app = application
