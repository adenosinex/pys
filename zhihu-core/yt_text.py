"""YouTube link -> text pipeline for zhihu-core.

Triggered from /api/extract when a YouTube URL arrives and no routing
rule is configured for it: the request returns immediately with a task
id and this module runs the pipeline on one background worker thread.

Pipeline (subtitles first, ASR as fallback):
  1. subtitles: this project's OWN yt-dlp/deno, self-bootstrapped
     into this project's own .runtime/bin from the official GitHub
     releases, plus this project's OWN YouTube cookie (the
     credentials.json key "youtube_cookie", write-only like the Mac
     code below). yt-dlp reads the video info; the subtitle text
     itself is fetched straight from the video's timedtext
     endpoint (the yt-dlp subtitle-file download is rate-limited
     separately by YouTube -- HTTP 429 from datacenter IPs -- so
     it is kept only as a second try), language priority
     zh -> en -> any, manual before auto within one language;
     the raw captions are cleaned (timestamps stripped, YouTube's
     rolling duplicate lines merged) into paragraph text.
  2. audio fallback (only when subtitles are unavailable): the audio
     is fetched through the yt-audio loopback API (contract:
     POST /api/fetch {"url"} -> {"job_id"}; GET /api/jobs/<id> ->
     job status/file_id; GET /files/<file_id> -> audio bytes;
     DELETE /api/files/<file_id> cleanup), uploaded to mac-compute
     (https://apple.su7.dpdns.org:5850, job type "asr") and the
     resulting text is collected from the job's result zip.

Dependency note (2026-11-10 remediation): this module must not read
any other project's files -- no sibling project directories, no
other project's .runtime binaries, no other project's cookie
files. The subtitle stage originally (v1.5.0) borrowed yt-audio's
.runtime yt-dlp/deno and its data-dir youtube-cookies.txt because
yt-audio's API exposes only audio-download jobs (there is no
video-info or subtitle endpoint to call); the dependency audit
flagged that shortcut and it was removed the same day: the
binaries are self-bootstrapped now and the cookie is this
project's own credential. The only remaining contact with
yt-audio is the loopback HTTP API in step 2.

Every stage writes its progress into the task record (JSON on disk in
the platform data directory), so the page can poll and render a live
progress bar, and a task can be re-opened by id after the page or even
the service process went away (the worker resumes unfinished tasks).

The mac-compute access code lives only in <data dir>/credentials.json
(key "mac_compute_access_code", file mode 0600). It is write-only:
the API can store or remove it and report whether it exists, never
read it back.
"""
import http.client
import io
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

MAC_BASE = "https://apple.su7.dpdns.org:5850"
YT_AUDIO_API = os.environ.get("YT_AUDIO_API", "http://127.0.0.1:18572")
CREDENTIALS_KEY = "mac_compute_access_code"

MAX_PENDING_TASKS = 5
RETENTION_SEC = 24 * 3600
DL_DEADLINE_SEC = 900          # waiting on the yt-audio job
UPLOAD_DEADLINE_SEC = 1800     # pushing the audio to the Mac
MAC_DEADLINE_SEC = 3600        # queue + transcription on the Mac
MAX_AUDIO_BYTES = 300 * 1024 * 1024
MAX_TEXT_CHARS = 1500000

STAGE_LABELS = {
    "queued": "排队等待…",
    "subtitle": "正在尝试获取字幕…",
    "download": "正在下载音频…",
    "upload": "正在上传到 Mac…",
    "mac_queue": "Mac 队列等待…",
    "asr": "Mac 转写中…",
    "assemble": "正在整理文本…",
    "done": "完成",
    "error": "失败",
}


class TaskFail(Exception):
    """Pipeline failure pinned to one stage, with a user-facing reason."""

    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage
        self.message = message


# ------------------------------------------------------------------ paths

def _data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return Path(env)
    here = Path(__file__).parent
    link = here / "data"
    if link.is_dir():
        return Path(os.path.realpath(link))
    return here / ".runtime"


def _root():
    root = _data_dir() / "yt-text"
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    return root


def _credentials_path():
    return _data_dir() / "credentials.json"


# ------------------------------------------------------- mac access code

def _read_credentials():
    try:
        data = json.loads(_credentials_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mac_code_configured():
    env = os.environ.get("MAC_COMPUTE_ACCESS_CODE", "")
    if env.strip():
        return True
    return bool(str(_read_credentials().get(CREDENTIALS_KEY) or "").strip())


def _mac_code():
    env = os.environ.get("MAC_COMPUTE_ACCESS_CODE", "")
    if env.strip():
        return env.strip()
    return str(_read_credentials().get(CREDENTIALS_KEY) or "").strip()


def save_mac_code(value):
    value = (value or "").strip()
    if not value or len(value) > 512:
        raise ValueError("invalid access code")
    path = _credentials_path()
    data = _read_credentials()
    data[CREDENTIALS_KEY] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def remove_mac_code():
    path = _credentials_path()
    data = _read_credentials()
    if CREDENTIALS_KEY in data:
        del data[CREDENTIALS_KEY]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    return mac_code_configured()


# ------------------------------------------------------------ task store

_lock = threading.Lock()
_worker_state = {"pid": None, "thread": None}
_queue = []  # task ids waiting for the worker, FIFO


def _task_path(task_id):
    return _root() / "tasks" / (task_id + ".json")


def _save(task):
    path = _task_path(task["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _load(task_id):
    try:
        data = json.loads(_task_path(task_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _all_tasks():
    out = []
    for path in sorted((_root() / "tasks").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("id"):
                out.append(data)
        except (OSError, ValueError):
            continue
    return out


def _update(task, **kw):
    task.update(kw)
    task["updated_at"] = time.time()
    _save(task)


def _position(task):
    """1-based place in the serial pipeline (0 once finished)."""
    if task["state"] in ("done", "error"):
        return 0
    ahead = 0
    for other in _all_tasks():
        if other["id"] == task["id"]:
            continue
        if other["state"] in ("queued", "running") \
                and other["created_at"] < task["created_at"]:
            ahead += 1
    return ahead + 1


def _overall(task):
    """Coarse whole-task progress estimate (0-100) for the bar."""
    state, stage = task["state"], task["stage"]
    pct = task.get("stage_percent")
    if state == "done":
        return 100
    if stage == "queued":
        return 0
    if stage == "subtitle":
        return 8
    if stage == "download":
        return 10 + int(0.35 * (pct or 0))
    if stage == "upload":
        return 45 + int(0.15 * (pct or 0))
    if stage == "mac_queue":
        return 62
    if stage == "asr":
        return 65 + int(0.30 * (pct or 0)) if pct is not None else 70
    if stage == "assemble":
        return 97
    return task.get("overall_percent") or 0


def _public(task):
    out = {k: v for k, v in task.items() if not k.startswith("_")}
    out["position"] = _position(task)
    out["overall_percent"] = _overall(task)
    out["elapsed_sec"] = max(0, int(time.time() - task["created_at"]))
    out["stage_label"] = STAGE_LABELS.get(task["stage"], task["stage"])
    return out


def get_task(task_id):
    if not re.fullmatch(r"[0-9a-f]{12}", task_id or ""):
        return None
    task = _load(task_id)
    return _public(task) if task else None


def _purge_old():
    cutoff = time.time() - RETENTION_SEC
    for task in _all_tasks():
        if task["created_at"] < cutoff and task["state"] in ("done", "error"):
            try:
                _task_path(task["id"]).unlink()
            except OSError:
                pass
            _clean_tmp(task["id"])


def _clean_tmp(task_id):
    folder = _root() / "tmp" / task_id
    if folder.is_dir():
        for child in folder.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass


def create_task(url, fmt):
    _ensure_worker()
    _purge_old()
    pending = [t for t in _all_tasks() if t["state"] in ("queued", "running")]
    if len(pending) >= MAX_PENDING_TASKS:
        return None
    task = {
        "id": secrets.token_hex(6),
        "url": url, "format": fmt,
        "state": "queued", "stage": "queued",
        "stage_label": STAGE_LABELS["queued"],
        "detail": "", "stage_percent": None,
        "title": "", "origin": "",
        "created_at": time.time(), "updated_at": time.time(),
        "result": None, "error": None,
        "_mac_job_id": "", "_audio_name": "",
    }
    _save(task)
    with _lock:
        _queue.append(task["id"])
    return _public(task)


# ---------------------------------------------------------------- worker

def _ensure_worker():
    pid = os.getpid()
    with _lock:
        thread = _worker_state.get("thread")
        if _worker_state.get("pid") == pid and thread is not None and thread.is_alive():
            return
        # Resume anything a previous process left unfinished.
        known = set(_queue)
        for task in sorted(_all_tasks(), key=lambda t: t["created_at"]):
            if task["state"] in ("queued", "running") and task["id"] not in known:
                _queue.append(task["id"])
        thread = threading.Thread(target=_worker, daemon=True, name="yt-text-worker")
        _worker_state.update(pid=pid, thread=thread)
        thread.start()


def _worker():
    while True:
        task_id = None
        with _lock:
            if _queue:
                task_id = _queue.pop(0)
        if task_id is None:
            time.sleep(1.0)
            continue
        task = _load(task_id)
        if not task or task["state"] in ("done", "error"):
            continue
        try:
            _run_task(task)
        except TaskFail as exc:
            _update(task, state="error", stage=exc.stage,
                    detail="", stage_percent=None,
                    error={"stage": exc.stage, "message": exc.message,
                           "code": "stage_failed"})
            _clean_tmp(task_id)
        except Exception as exc:  # noqa: BLE001 - surface, never hang the queue
            _update(task, state="error", stage=task.get("stage") or "subtitle",
                    detail="", stage_percent=None,
                    error={"stage": task.get("stage") or "subtitle",
                           "message": "内部处理失败（%s）。" % type(exc).__name__,
                           "code": "internal_error"})
            _clean_tmp(task_id)


def _set_stage(task, stage, detail="", percent=None):
    _update(task, state="running", stage=stage, detail=detail,
            stage_percent=percent)


def _run_task(task):
    if not task.get("_mac_job_id"):
        text, origin, fetched = _subtitle_path(task)
        if text is None:
            _audio_path(task)  # fills result itself or raises TaskFail
            return
        _finish(task, text, origin, fetched)
        return
    _mac_collect(task)  # resumed after a restart with a live Mac job


def _finish(task, text, origin, fetched_bytes):
    elapsed_ms = int((time.time() - task["created_at"]) * 1000)
    content = text
    if task["format"] == "json":
        doc = {"url": task["url"], "kind": "youtube", "id": task.get("_video_id", ""),
               "title": task.get("title") or "YouTube 视频", "author": "YouTube",
               "published": "", "text": text, "markdown": text,
               "source": "YouTube", "extra": {"origin": origin}}
        content = json.dumps(doc, ensure_ascii=False, indent=2)
    result = {
        "ok": True, "url": task["url"], "kind": "youtube",
        "id": task.get("_video_id", ""),
        "title": task.get("title") or "YouTube 视频",
        "author": "YouTube", "published": "",
        "source": "YouTube", "format": task["format"],
        "content": content, "origin": origin,
        "stats": {"elapsed_ms": elapsed_ms, "fetched_bytes": fetched_bytes,
                  "output_bytes": len(content.encode("utf-8")),
                  "output_chars": len(content)},
    }
    _update(task, state="done", stage="done", detail="", stage_percent=None,
            origin=origin, result=result)
    _clean_tmp(task["id"])


# ------------------------------------------------------------ yt-dlp side
#
# Self-contained tooling (2026-11-10 remediation): yt-dlp + deno are
# bootstrapped into THIS project's own .runtime/bin from the official
# GitHub releases -- the same self-bootstrap pattern yt-audio uses
# for itself, re-implemented here so that no project reads another
# project's files. The YouTube cookie is this project's own
# credential (credentials.json key "youtube_cookie", write-only,
# mirroring the mac-compute code store above); it is materialized
# into the task's tmp folder at use time and deleted with the task.

_YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
_DENO_URL = ("https://github.com/denoland/deno/releases/latest/download/"
             "deno-x86_64-unknown-linux-gnu.zip")
_YTDLP_MAX_AGE = 14 * 24 * 3600
_DENO_MAX_AGE = 30 * 24 * 3600
YT_COOKIE_KEY = "youtube_cookie"
_COOKIE_KEY_FIELDS = ("SAPISID", "HSID", "SID", "SSID", "APISID",
                      "__Secure-1PSID", "__Secure-3PSID")
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")

_bootstrap_lock = threading.Lock()


def _own_bin_dir():
    return Path(__file__).parent / ".runtime" / "bin"


def _ensure_ytdlp():
    """This project's own yt-dlp path, downloaded on first use (and
    refreshed when stale); "" when it cannot be obtained."""
    env = os.environ.get("YTDLP_BIN", "")
    if env:
        return env
    path = _own_bin_dir() / "yt-dlp"
    with _bootstrap_lock:
        try:
            if path.exists() \
                    and time.time() - path.stat().st_mtime < _YTDLP_MAX_AGE:
                return str(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            req = urllib.request.Request(
                _YTDLP_URL, headers={"User-Agent": "zhihu-core/yt-text"})
            with urllib.request.urlopen(req, timeout=180) as resp, \
                    open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            tmp.chmod(0o755)
            tmp.replace(path)
            return str(path)
        except (OSError, urllib.error.URLError):
            return str(path) if path.exists() else ""


def _ensure_deno():
    """Best-effort: this project's own deno path, or "" (deno is
    yt-dlp's JS runtime; many subtitle fetches also work without)."""
    path = _own_bin_dir() / "deno"
    with _bootstrap_lock:
        try:
            if path.exists() \
                    and time.time() - path.stat().st_mtime < _DENO_MAX_AGE:
                return str(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(
                _DENO_URL, headers={"User-Agent": "zhihu-core/yt-text"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = resp.read()
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                data = zf.read("deno")
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "wb") as fh:
                fh.write(data)
            tmp.chmod(0o755)
            tmp.replace(path)
            return str(path)
        except Exception:  # noqa: BLE001 -- best effort by design
            return str(path) if path.exists() else ""


def _header_cookie_to_netscape(text):
    """Convert a raw Cookie header string ("a=b; c=d", as copied from
    a browser's devtools) into Netscape cookies.txt lines for
    .youtube.com. Returns None when the input does not parse as a
    logged-in YouTube cookie header."""
    body = (text or "").strip()
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


def _youtube_cookie_text():
    return str(_read_credentials().get(YT_COOKIE_KEY) or "").strip()


def youtube_cookie_configured():
    return bool(_youtube_cookie_text())


def save_youtube_cookie(value):
    """Write-only store for this project's own YouTube cookie: accepts
    a Netscape cookies.txt or a raw Cookie header string; the value is
    never read back through the API (only "configured" is reported)."""
    value = (value or "").strip()
    if len(value) < 20 or len(value) > 512 * 1024:
        raise ValueError("invalid youtube cookie")
    looks = ("\t" in value and "youtube.com" in value
             and any(k in value for k in _COOKIE_KEY_FIELDS)) \
        or _header_cookie_to_netscape(value) is not None
    if not looks:
        raise ValueError("invalid youtube cookie")
    path = _credentials_path()
    data = _read_credentials()
    data[YT_COOKIE_KEY] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def remove_youtube_cookie():
    path = _credentials_path()
    data = _read_credentials()
    if YT_COOKIE_KEY in data:
        del data[YT_COOKIE_KEY]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    return youtube_cookie_configured()


def _youtube_cookie_path(folder):
    """Materialize this project's own YouTube cookie as a Netscape
    cookies.txt inside the task tmp folder; "" when none is stored or
    the stored value does not validate."""
    raw = _youtube_cookie_text()
    if not raw:
        return ""
    if "\t" in raw and "youtube.com" in raw:
        text = raw if raw.endswith("\n") else raw + "\n"
    else:
        text = _header_cookie_to_netscape(raw)
        if text is None:
            return ""
    path = Path(folder) / "youtube-cookies.txt"
    try:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        return ""
    return str(path)


def _ytdlp_assets(folder):
    """(yt-dlp, deno, cookie-file) -- every asset owned by THIS
    project; the cookie is materialized inside the task tmp folder."""
    return _ensure_ytdlp(), _ensure_deno(), _youtube_cookie_path(folder)


def _run_cmd(cmd, timeout):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), \
        proc.stderr.decode("utf-8", "replace")


_SUB_LANGS = ("zh-Hans", "zh-Hant", "zh", "en", "en-US", "en-GB")


def _subtitle_langs(info):
    """Language candidates for a direct timedtext fetch, best first.
    Priority follows _choose_track (zh -> en -> any); languages that
    have manual subtitles rank before auto-caption-only ones."""
    manual = list((info.get("subtitles") or {}).keys())
    auto = [k for k in (info.get("automatic_captions") or {}).keys()
            if k not in manual]
    prefs = list(_SUB_LANGS) + manual + auto
    seen, out = set(), []
    for lang in prefs:
        if lang not in seen and (lang in manual or lang in auto):
            seen.add(lang)
            out.append(lang)
    return out


def _timedtext_fetch(url, info, folder):
    """Best-effort direct subtitle fetch; returns (text, lang,
    is_auto) or None.

    Route: yt-dlp --print with a filter program re-extracts FRESH
    timedtext URLs (the ones in _video_info's dump go stale), then a
    plain HTTPS GET downloads the json3 track. This bypasses the
    yt-dlp subtitle-file download, which YouTube rate-limits
    separately (HTTP 429 from datacenter IPs, with or without a
    cookie). Any failure returns None; the caller then tries the
    classic file download."""
    binary, deno, cookie = _ytdlp_assets(folder)
    if not binary:
        return None
    langs = _subtitle_langs(info)
    if not langs:
        return None
    base = [binary, "--no-playlist", "--no-warnings",
            "--socket-timeout", "30", "--retries", "2"]
    rt = ["--js-runtimes", "deno:" + deno] if deno else []
    attempts = []
    if cookie:
        attempts.append(["--cookies", cookie])
    attempts.append([])
    attempts.append(["--extractor-args",
                     "youtube:player_client=android_vr"])
    chosen = None
    for extra in attempts:
        cmd = base + rt + extra + ["-j", url]
        try:
            rc, out, _err = _run_cmd(cmd, 180)
        except subprocess.TimeoutExpired:
            continue
        if rc != 0 or not out.strip():
            continue
        fresh = None
        for line in reversed(out.strip().splitlines()):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                fresh = data
                break
        if fresh is None:
            continue
        for key, is_auto in (("subtitles", False),
                             ("automatic_captions", True)):
            tracks = fresh.get(key) or {}
            hit = None
            for lang in langs:
                for t in tracks.get(lang) or []:
                    if t.get("ext") == "json3" and t.get("url"):
                        hit = (lang, is_auto, t["url"])
                        break
                if hit:
                    break
            if hit:
                chosen = hit
                break
        if chosen:
            break
    if not chosen:
        return None
    lang, is_auto, tt_url = chosen
    try:
        req = urllib.request.Request(
            tt_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError):
        return None
    if not raw:
        return None
    text = clean_subtitle(raw, "json3")
    if len(text) < 20:
        return None
    return text, lang, is_auto


def _ytdlp_attempts(url, extra, folder):
    """Run yt-dlp with the proven three-attempt pattern (own cookie,
    no cookie, android_vr player client); return stdout."""
    binary, deno, cookie = _ytdlp_assets(folder)
    if not binary:
        raise TaskFail("subtitle", "服务器缺少 yt-dlp（本站工具自举失败）。")
    base = [binary, "--no-playlist", "--no-warnings",
            "--socket-timeout", "30", "--retries", "2"]
    rt = ["--js-runtimes", "deno:" + deno] if deno else []
    attempts = []
    if cookie:
        attempts.append(base + rt + ["--cookies", cookie] + extra)
    attempts.append(base + rt + extra)
    attempts.append(base + rt + ["--extractor-args",
                                  "youtube:player_client=android_vr"] + extra)
    tail = ""
    for cmd in attempts:
        try:
            rc, out, err = _run_cmd(cmd + [url], 180)
        except subprocess.TimeoutExpired:
            tail = "yt-dlp 执行超时"
            continue
        if rc == 0 and out.strip():
            return out
        blob = (err or out).strip().splitlines()
        if blob:
            tail = blob[-1][:240]
        if "format is not available" not in (err + out).lower() \
                and "sign in to confirm" not in (err + out).lower() \
                and rc == 0:
            break
    raise TaskFail("subtitle", "无法读取视频信息：" + (tail or "yt-dlp 失败"))


def _video_info(url, folder):
    out = _ytdlp_attempts(url, ["-j"], folder)
    for line in reversed(out.strip().splitlines()):
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return data
        except ValueError:
            continue
    raise TaskFail("subtitle", "无法解析视频信息。")


def _lang_rank(lang):
    low = (lang or "").lower()
    if low.startswith("zh-hans") or low in ("zh-cn", "zh-sg") or low == "zh":
        return (0, 0, low)
    if low.startswith("zh-hant") or low in ("zh-tw", "zh-hk", "zh-mo"):
        return (0, 1, low)
    if low.startswith("zh"):
        return (0, 2, low)
    if low.startswith("en"):
        return (1, 0, low)
    return (2, 0, low)


_EXT_PREF = ["vtt", "srt", "json3", "srv3", "ttml", "srv2", "srv1"]


def _choose_track(info):
    """Pick (lang, ext, is_auto) or None. Language first (zh, en, any),
    manual subtitles before automatic ones within the same language."""
    candidates = []
    for lang, tracks in (info.get("subtitles") or {}).items():
        candidates.append((lang, tracks, False))
    for lang, tracks in (info.get("automatic_captions") or {}).items():
        candidates.append((lang, tracks, True))
    best = None
    for lang, tracks, is_auto in candidates:
        exts = {t.get("ext") for t in tracks if isinstance(t, dict)}
        ext = next((e for e in _EXT_PREF if e in exts), None)
        if not ext:
            continue
        key = _lang_rank(lang) + (1 if is_auto else 0,)
        if best is None or key < best[0]:
            best = (key, lang, ext, is_auto)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _download_subtitle(url, lang, ext, is_auto, folder):
    binary, deno, cookie = _ytdlp_assets(folder)
    if not binary:
        return None
    base = [binary, "--no-playlist", "--no-warnings", "--socket-timeout", "30",
            "--retries", "2", "--skip-download",
            "--write-auto-subs" if is_auto else "--write-subs",
            "--sub-langs", lang, "--sub-format", ext,
            "-o", str(folder / "dl")]
    rt = ["--js-runtimes", "deno:" + deno] if deno else []
    attempts = []
    if cookie:
        attempts.append(base + rt + ["--cookies", cookie])
    attempts.append(base + rt)
    attempts.append(base + rt + ["--extractor-args",
                                  "youtube:player_client=android_vr"])
    for cmd in attempts:
        for old in folder.glob("dl.*"):
            try:
                old.unlink()
            except OSError:
                pass
        try:
            rc, _out, _err = _run_cmd(cmd + [url], 240)
        except subprocess.TimeoutExpired:
            continue
        if rc != 0:
            continue
        for path in sorted(folder.glob("dl.*")):
            if path.suffix.lstrip(".") == ext and path.stat().st_size > 0:
                return path.read_bytes()
    return None


# -------------------------------------------------------- subtitle clean

_TAG_RE = re.compile(r"<[^>]+>")
_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})|(\d{2}):(\d{2})[.,](\d{3})")


def _ts_to_sec(text):
    m = _TIME_RE.search(text or "")
    if not m:
        return 0.0
    if m.group(1) is not None:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 \
            + int(m.group(3)) + int(m.group(4)) / 1000.0
    return int(m.group(5)) * 60 + int(m.group(6)) + int(m.group(7)) / 1000.0


def _parse_vtt(raw):
    cues, block = [], []
    blocks = re.split(r"\r?\n\r?\n", raw.replace("\r\n", "\n"))
    for block in blocks:
        lines = [ln.rstrip() for ln in block.split("\n")]
        timing_at = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_at is None:
            continue
        head = lines[0] if lines else ""
        if head.startswith(("NOTE", "STYLE", "REGION")):
            continue
        texts = []
        for ln in lines[timing_at + 1:]:
            clean = _TAG_RE.sub("", ln).strip()
            if clean:
                texts.append(clean)
        if texts:
            cues.append((_ts_to_sec(lines[timing_at]), texts))
    return cues


def _parse_srt(raw):
    cues = []
    for block in re.split(r"\r?\n\r?\n", raw.replace("\r\n", "\n")):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        timing_at = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_at is None:
            continue
        texts = [_TAG_RE.sub("", ln).strip() for ln in lines[timing_at + 1:]]
        texts = [t for t in texts if t]
        if texts:
            cues.append((_ts_to_sec(lines[timing_at]), texts))
    return cues


def _parse_json3(raw):
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    cues = []
    for event in data.get("events") or []:
        segs = event.get("segs") or []
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        if text:
            cues.append(((event.get("tStartMs") or 0) / 1000.0,
                         [ln.strip() for ln in text.split("\n") if ln.strip()]))
    return cues


def _parse_xml_caps(raw):
    cues = []
    for m in re.finditer(r'<text[^>]*start="([\d.]+)"[^>]*>(.*?)</text>',
                         raw, re.S):
        body = _TAG_RE.sub("", m.group(2))
        body = (body.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&apos;", "'")).strip()
        if body:
            cues.append((float(m.group(1)), [body]))
    if cues:
        return cues
    for m in re.finditer(r'<p[^>]*begin="([\d:.]+)"[^>]*>(.*?)</p>', raw, re.S):
        body = _TAG_RE.sub(" ", m.group(2)).strip()
        if body:
            cues.append((_ts_to_sec(m.group(1)), [body]))
    return cues


def cues_to_text(cues):
    """Merge caption cues into coherent paragraphs: drop rolling
    duplicates (repeat lines and prefix-growth lines), join lines,
    break paragraphs on long pauses or at sentence ends."""
    lines = []  # (start, text)
    for start, texts in cues:
        for text in texts:
            text = re.sub(r"\s+", " ", text).strip()
            if not text or text in ("♪", "[Music]", "[Applause]"):
                continue
            if lines and lines[-1][1] == text:
                continue
            if lines and len(text) > len(lines[-1][1]) \
                    and text.startswith(lines[-1][1]):
                lines[-1] = (lines[-1][0], text)
                continue
            if lines and lines[-1][1].startswith(text):
                continue
            lines.append((start, text))
    if not lines:
        return ""
    sample = "".join(t for _s, t in lines[:80])
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff") \
        > len(sample) * 0.3
    joiner = "" if cjk else " "
    paragraphs, current, prev_start = [], [], None
    length = 0
    for start, text in lines:
        if current and prev_start is not None and start - prev_start > 4.0 \
                and length > 120:
            paragraphs.append(joiner.join(current))
            current, length = [], 0
        current.append(text)
        length += len(text)
        if length >= 300 and text.rstrip()[-1:] in "。！？.!?…":
            paragraphs.append(joiner.join(current))
            current, length = [], 0
        elif length >= 700:
            paragraphs.append(joiner.join(current))
            current, length = [], 0
        prev_start = start
    if current:
        paragraphs.append(joiner.join(current))
    return "\n\n".join(p for p in paragraphs if p.strip())


def clean_subtitle(raw_bytes, ext):
    raw = raw_bytes.decode("utf-8", "replace")
    if ext == "vtt":
        cues = _parse_vtt(raw)
    elif ext == "srt":
        cues = _parse_srt(raw)
    elif ext == "json3":
        cues = _parse_json3(raw)
    else:
        cues = _parse_xml_caps(raw)
    return cues_to_text(cues)


def _subtitle_path(task):
    """Returns (text, origin, fetched_bytes) or (None, '', 0) to fall back."""
    _set_stage(task, "subtitle", "正在读取视频信息…")
    folder = _root() / "tmp" / task["id"]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        info = _video_info(task["url"], folder)
    except TaskFail:
        return None, "", 0
    task["_video_id"] = str(info.get("id") or "")
    if info.get("title"):
        task["title"] = str(info["title"])[:300]
    _save(task)
    _set_stage(task, "subtitle", "正在获取字幕文本…")
    direct = _timedtext_fetch(task["url"], info, folder)
    if direct is not None:
        text, lang, is_auto = direct
        kind = "自动字幕" if is_auto else "人工字幕"
        _set_stage(task, "assemble", "正在清洗字幕文本…")
        return text, "字幕直取（%s · %s）" % (kind, lang), 0
    track = _choose_track(info)
    if track is None:
        return None, "", 0
    lang, ext, is_auto = track
    kind = "自动字幕" if is_auto else "人工字幕"
    _set_stage(task, "subtitle", "正在下载字幕：%s（%s）…" % (lang, kind))
    raw = _download_subtitle(task["url"], lang, ext, is_auto, folder)
    if not raw:
        return None, "", 0
    _set_stage(task, "assemble", "正在清洗字幕文本…")
    text = clean_subtitle(raw, ext)
    if len(text) < 20:
        return None, "", 0
    return text, "字幕直取（%s · %s）" % (kind, lang), len(raw)


# --------------------------------------------------------- yt-audio client
#
# The ONLY contact with the yt-audio project: its loopback HTTP API.
# Contract (yt-audio serves on the local port, which the platform
# trusts; identity verification lives at the platform layer):
#   POST   /api/fetch           {"url": ...} -> {"job_id": ...}
#   GET    /api/jobs/<id>                    -> {"job": {"status",
#                                  "position", "progress", "file_id",
#                                  "message"}}
#   GET    /files/<file_id>                  -> audio bytes (streamed)
#   DELETE /api/files/<file_id>              -> removes our download
# zhihu-core never reads yt-audio's files, binaries or cookies.

def _http_json(method, url, payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _ytaudio_download(task, folder):
    """Fetch the audio through yt-audio; returns the local file path."""
    try:
        created = _http_json("POST", YT_AUDIO_API + "/api/fetch",
                             {"url": task["url"]}, timeout=30)
    except (urllib.error.URLError, OSError, ValueError):
        raise TaskFail("download", "yt-audio 下载服务不可用，请稍后重试。")
    job_id = created.get("job_id")
    if not job_id:
        raise TaskFail("download", "yt-audio 未接受下载任务。")
    deadline = time.time() + DL_DEADLINE_SEC
    entry = None
    while time.time() < deadline:
        try:
            data = _http_json("GET", YT_AUDIO_API + "/api/jobs/" + job_id,
                              timeout=30)
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(3)
            continue
        job = data.get("job") or {}
        status = job.get("status")
        if status == "queued":
            pos = job.get("position")
            _set_stage(task, "download",
                       "yt-audio 排队第 %s 位…" % pos if pos else "yt-audio 排队中…")
        elif status == "running":
            progress = job.get("progress") or {}
            pct = None
            raw_pct = str(progress.get("percent") or "").strip().rstrip("%")
            try:
                pct = float(raw_pct)
            except ValueError:
                pct = None
            bits = []
            if progress.get("speed"):
                bits.append("速度 " + str(progress["speed"]).strip())
            if progress.get("eta"):
                bits.append("剩余 " + str(progress["eta"]).strip())
            _set_stage(task, "download", " ".join(bits), pct)
        elif status == "done":
            entry = job
            break
        elif status in ("error", "cancelled"):
            raise TaskFail("download", "音频下载失败："
                           + str(job.get("message") or status))
        time.sleep(2)
    if entry is None:
        raise TaskFail("download", "音频下载超时（%d 秒）。" % DL_DEADLINE_SEC)
    file_id = entry.get("file_id")
    if not file_id:
        raise TaskFail("download", "yt-audio 未返回文件。")
    _set_stage(task, "download", "正在取回音频文件…")
    name = "audio"
    req = urllib.request.Request(YT_AUDIO_API + "/files/" + file_id)
    total = 0
    path = folder / "audio.bin"
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            ext = ""
            disp = resp.headers.get("Content-Disposition") or ""
            m = re.search(r"\.([A-Za-z0-9]{2,5})", disp)
            if m:
                ext = "." + m.group(1).lower()
            total = int(resp.headers.get("Content-Length") or 0)
            with open(path, "wb") as fh:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
    except (urllib.error.URLError, OSError):
        raise TaskFail("download", "从 yt-audio 取回音频失败。")
    if ext:
        named = folder / ("audio" + ext)
        path.replace(named)
        path = named
    try:
        _http_json("DELETE", YT_AUDIO_API + "/api/files/" + file_id, timeout=15)
    except Exception:
        pass  # yt-audio also purges on its own retention schedule
    size = path.stat().st_size
    if size <= 0:
        raise TaskFail("download", "下载到的音频为空。")
    if size > MAX_AUDIO_BYTES:
        raise TaskFail("download", "音频超过 300 MB 上限，无法转写。")
    task["_audio_name"] = path.name
    task["_audio_size"] = size
    _save(task)
    return path


# ------------------------------------------------------- mac-compute side

class _CountingBody:
    """File-like multipart body that reports upload progress."""

    def __init__(self, prefix, path, suffix, total, on_progress):
        self._prefix = io.BytesIO(prefix)
        self._file = open(path, "rb")
        self._suffix = io.BytesIO(suffix)
        self._sent = 0
        self.total = total
        self._on_progress = on_progress
        self._last_note = 0.0

    def read(self, size=65536):
        chunk = self._prefix.read(size)
        if not chunk:
            chunk = self._file.read(size)
        if not chunk:
            chunk = self._suffix.read(size)
        self._sent += len(chunk)
        now = time.time()
        if self._on_progress and (now - self._last_note > 1.5 or not chunk):
            self._last_note = now
            self._on_progress(self._sent, self.total)
        return chunk

    def close(self):
        try:
            self._file.close()
        except OSError:
            pass


def _mac_request(method, path, code, body=None, ctype=None, timeout=60):
    parts = urlsplit(MAC_BASE)
    conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443,
                                       timeout=timeout)
    headers = {"X-Access-Code": code}
    if ctype:
        headers["Content-Type"] = ctype
    if body is not None and hasattr(body, "total"):
        headers["Content-Length"] = str(body.total)
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _mac_submit(task, audio_path):
    code = _mac_code()
    boundary = "----yttext" + secrets.token_hex(8)
    prefix = (
        ("--%s\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\nasr\r\n"
         "--%s\r\nContent-Disposition: form-data; name=\"params\"\r\n\r\n{}\r\n"
         "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
         "Content-Type: application/octet-stream\r\n\r\n")
        % (boundary, boundary, boundary, audio_path.name)).encode("utf-8")
    suffix = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    total = len(prefix) + audio_path.stat().st_size + len(suffix)

    def on_progress(sent, whole):
        _set_stage(task, "upload", "已上传 %d%%" % int(sent * 100 / max(1, whole)),
                   sent * 100.0 / max(1, whole))

    body = _CountingBody(prefix, audio_path, suffix, total, on_progress)
    try:
        status, data = _mac_request(
            "POST", "/api/jobs", code, body=body,
            ctype="multipart/form-data; boundary=" + boundary,
            timeout=120)
    except (OSError, http.client.HTTPException):
        raise TaskFail("upload", "上传到 Mac 失败（网络或超时）。")
    finally:
        body.close()
    if status == 401:
        raise TaskFail("upload", "Mac 转写访问码被拒绝，请在页面下方更新访问码。")
    if status not in (200, 201):
        raise TaskFail("upload", "Mac 转写服务返回 HTTP %d。" % status)
    try:
        job_id = json.loads(data.decode("utf-8", "replace"))["id"]
    except (ValueError, KeyError):
        raise TaskFail("upload", "Mac 转写服务返回了无效响应。")
    return str(job_id)


def _mac_job(code, job_id):
    status, data = _mac_request("GET", "/api/jobs/" + job_id, code, timeout=60)
    if status == 401:
        raise TaskFail("asr", "Mac 转写访问码被拒绝，请在页面下方更新访问码。")
    if status != 200:
        raise TaskFail("asr", "查询 Mac 任务返回 HTTP %d。" % status)
    job = json.loads(data.decode("utf-8", "replace"))
    return job if isinstance(job, dict) else {}


def _mac_position(code, job):
    """Best-effort queue position from the job list; None when unknown."""
    try:
        status, data = _mac_request("GET", "/api/jobs", code, timeout=60)
        if status != 200:
            return None
        payload = json.loads(data.decode("utf-8", "replace"))
        jobs = payload.get("jobs") if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            return None
    except (OSError, http.client.HTTPException, ValueError):
        return None
    active = [j for j in jobs if isinstance(j, dict)
              and j.get("status") in ("queued", "running", "processing")]
    mine = job.get("id")
    for index, item in enumerate(active):
        if item.get("id") == mine:
            return index + 1
    return None


def _mac_collect(task):
    code = _mac_code()
    job_id = task["_mac_job_id"]
    started = task.get("_mac_started") or time.time()
    deadline = started + MAC_DEADLINE_SEC
    misses = 0
    while time.time() < deadline:
        try:
            job = _mac_job(code, job_id)
            misses = 0
        except TaskFail:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            misses += 1
            if misses >= 6:
                raise TaskFail("asr", "无法连接 Mac 转写服务，请稍后重试。")
            time.sleep(5)
            continue
        status = str(job.get("status") or "")
        if status == "queued":
            pos = job.get("position") or job.get("queue_position") \
                or _mac_position(code, job)
            detail = "排在第 %s 位" % pos if pos else "状态：排队中"
            _set_stage(task, "mac_queue", detail)
        elif status in ("running", "processing", "working"):
            pct = None
            raw = job.get("progress")
            if isinstance(raw, (int, float)):
                pct = raw * 100 if raw <= 1 else float(raw)
                pct = max(0.0, min(100.0, pct))
            note = str(job.get("message") or job.get("stage") or "")
            _set_stage(task, "asr", note[:120], pct)
        elif status == "done":
            _set_stage(task, "assemble", "正在取回转写文本…")
            text = _mac_fetch_text(task, code, job_id)
            _finish(task, text, "音频转写（Mac ASR）",
                    task.get("_audio_size") or 0)
            return
        elif status in ("failed", "dead", "error", "cancelled"):
            reason = str(job.get("error") or job.get("message") or status)
            raise TaskFail("asr", "Mac 转写失败：" + reason[:240])
        else:
            _set_stage(task, "mac_queue", "状态：" + (status or "等待中"))
        time.sleep(5)
    try:
        _mac_request("POST", "/api/jobs/" + job_id + "/cancel", code,
                     body=b"", timeout=30)
    except Exception:
        pass
    raise TaskFail("asr", "Mac 转写超时（%d 分钟上限）。" % (MAC_DEADLINE_SEC // 60))


def _mac_fetch_text(task, code, job_id):
    status, data = _mac_request("GET", "/api/jobs/" + job_id + "/download",
                                code, timeout=300)
    if status != 200:
        raise TaskFail("assemble", "取回转写结果返回 HTTP %d。" % status)
    try:
        bundle = zipfile.ZipFile(io.BytesIO(data))
        names = bundle.namelist()
    except zipfile.BadZipFile:
        raise TaskFail("assemble", "Mac 返回的结果包损坏。")
    if "result.txt" in names:
        text = bundle.read("result.txt").decode("utf-8", "replace").strip()
        if len(text) >= 20:
            return text
    if "result.srt" in names:
        text = cues_to_text(_parse_srt(
            bundle.read("result.srt").decode("utf-8", "replace")))
        if len(text) >= 20:
            return text
    raise TaskFail("assemble", "Mac 转写结果为空。")


def _audio_path(task):
    folder = _root() / "tmp" / task["id"]
    folder.mkdir(parents=True, exist_ok=True)
    audio = None
    saved = task.get("_audio_name")
    if saved and (folder / saved).exists():
        audio = folder / saved
    if audio is None:
        warn = "" if mac_code_configured() else "（注意：Mac 转写未配置访问码）"
        _set_stage(task, "download", "正在准备下载音频…" + warn)
        audio = _ytaudio_download(task, folder)
    if not mac_code_configured():
        raise TaskFail(
            "upload",
            "该视频没有可用字幕，需要音频转写，但 Mac 转写访问码尚未配置："
            "请在页面下方的“Mac 转写”面板中导入访问码后重试。")
    _set_stage(task, "upload", "正在上传到 Mac…")
    job_id = _mac_submit(task, audio)
    task["_mac_job_id"] = job_id
    task["_mac_started"] = time.time()
    _save(task)
    try:
        audio.unlink()
    except OSError:
        pass
    task["_audio_name"] = ""
    _save(task)
    _mac_collect(task)
