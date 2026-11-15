# -*- coding: utf-8 -*-
"""ytplay - YouTube 播放：输入链接，在线看（无广告）。

为什么是这个架构：debi 出口直连 YouTube 会被机器人墙挡住，所以提取
与取流都走一条常驻 sing-box 客户端（经 shoc 的 AnyTLS 节点）；而
googlevideo 的 CORS 只白名单 youtube.com，浏览器无法直取分片，所以
服务端把清单与分片同源中继，并把播放列表里的每个 URI 改写到中继。
纯字节中继加清单改写，不做 ffmpeg、不混流、不转码。

路由：
  GET  /                    页面
  GET  /static/hls.min.js   播放器组件（本地 vendored，无 CDN 依赖）
  GET  /api/session         {authenticated: true}
  POST /api/resolve         JSON {url}，返回视频信息与清单入口
  GET  /api/manifest?v=ID   改写后的 master 清单（同源，hls.js 直接用）
  GET  /api/relay?u=URL&v=ID 白名单字节中继（播放列表自动改写）

应用本身不带验证：身份验证只在面板平台层，本地回环访问受信任。
"""
import atexit
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import io
from pathlib import Path

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
os.makedirs(DATA_DIR, exist_ok=True)

RESOLVE_LOG = os.path.join(DATA_DIR, "resolve_log.jsonl")
RELAY_LOG = os.path.join(DATA_DIR, "relay_log.jsonl")
OUTBOUND_FILE = os.path.join(DATA_DIR, "shoc_outbound.secret.json")
HLS_JS = os.path.join(BASE, "static", "hls.min.js")

_YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
_DENO_URL = ("https://github.com/denoland/deno/releases/latest/download/"
             "deno-x86_64-unknown-linux-gnu.zip")
_YTDLP_MAX_AGE = 14 * 24 * 3600
_DENO_MAX_AGE = 30 * 24 * 3600
SINGBOX = "/usr/local/bin/sing-box"

YTDLP_TIMEOUT = 90
RELAY_MAX_TIME = 100
PLAYLIST_MAX_TIME = 60
PLAYLIST_MAX_BYTES = 4 * 1024 * 1024
RELAY_MAX_BYTES = 64 * 1024 * 1024
RELAY_CONCURRENCY = 4
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
RANGE_RE = re.compile(r"^bytes=\d*-\d*$")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_bootstrap_lock = threading.Lock()
_resolve_lock = threading.Lock()
_proxy_lock = threading.Lock()
_manifests_lock = threading.Lock()
_log_lock = threading.Lock()
_relay_sem = threading.Semaphore(RELAY_CONCURRENCY)
_MANIFESTS = {}

_proxy = {"proc": None, "port": None, "cfg": None, "last_fail": 0.0}
_outbound_cache = {"data": None}


class AppError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


# ------------------------------------------------------------------ logging

def _log(path, obj):
    line = json.dumps(obj, ensure_ascii=False)
    with _log_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ------------------------------------------------------- yt-dlp/deno bootstrap

def _own_bin_dir():
    return Path(__file__).parent / ".runtime" / "bin"


def _ensure_ytdlp():
    """This project's own yt-dlp path, downloaded on first use and refreshed
    when stale; empty string when it cannot be obtained."""
    path = _own_bin_dir() / "yt-dlp"
    with _bootstrap_lock:
        try:
            if path.exists() and time.time() - path.stat().st_mtime < _YTDLP_MAX_AGE:
                return str(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            req = urllib.request.Request(_YTDLP_URL, headers={"User-Agent": "ytplay"})
            with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as out:
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
    """This project's own deno path, or empty string when unavailable."""
    path = _own_bin_dir() / "deno"
    with _bootstrap_lock:
        try:
            if path.exists() and time.time() - path.stat().st_mtime < _DENO_MAX_AGE:
                return str(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(_DENO_URL, headers={"User-Agent": "ytplay"})
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
        except Exception:
            return str(path) if path.exists() else ""


# ------------------------------------------------------------ proxy supervision

def _port_open(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _read_outbound():
    if _outbound_cache["data"] is not None:
        return _outbound_cache["data"]
    try:
        with open(OUTBOUND_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        raise AppError("代理节点配置缺失，无法连接（需先完成节点配置）", 502)
    if not isinstance(data, dict) or not data.get("type"):
        raise AppError("代理节点配置无效", 502)
    _outbound_cache["data"] = data
    return data


def _kill_stale_pidfile():
    pidfile = os.path.join(DATA_DIR, "sb.pid")
    try:
        with open(pidfile, encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            cmdline = fh.read()
    except OSError:
        cmdline = b""
    if b"sing-box" in cmdline and b"sb.secret." in cmdline:
        try:
            os.kill(pid, 15)
            for _ in range(15):
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            else:
                os.kill(pid, 9)
        except OSError:
            pass
    try:
        os.unlink(pidfile)
    except OSError:
        pass


def _stop_proxy_locked():
    proc = _proxy["proc"]
    _proxy["proc"] = None
    _proxy["port"] = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
    cfg = _proxy["cfg"]
    _proxy["cfg"] = None
    if cfg:
        try:
            os.unlink(cfg)
        except OSError:
            pass


def _stop_proxy_at_exit():
    with _proxy_lock:
        _stop_proxy_locked()
    try:
        os.unlink(os.path.join(DATA_DIR, "sb.pid"))
    except OSError:
        pass


atexit.register(_stop_proxy_at_exit)


def ensure_proxy():
    """Return the loopback SOCKS5 port of the supervised sing-box client,
    starting (or restarting) it when needed."""
    with _proxy_lock:
        proc = _proxy["proc"]
        if proc is not None and proc.poll() is None and _proxy["port"] and _port_open(_proxy["port"]):
            return _proxy["port"]
        if time.time() - _proxy["last_fail"] < 10:
            raise AppError("代理启动刚失败过，请稍候再试", 502)
        _stop_proxy_locked()
        outbound = _read_outbound()
        _kill_stale_pidfile()
        last_err = "sing-box 未能启动"
        for _attempt in range(3):
            port = _free_port()
            cfg_path = os.path.join(DATA_DIR, "sb.secret.%d.json" % os.getpid())
            sb_cfg = {"log": {"level": "error"},
                      "inbounds": [{"type": "mixed", "tag": "in",
                                    "listen": "127.0.0.1", "listen_port": port}],
                      "outbounds": [outbound],
                      "route": {"final": "out"}}
            try:
                fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    json.dump(sb_cfg, fh)
                proc = subprocess.Popen([SINGBOX, "run", "-c", cfg_path],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            except OSError as e:
                last_err = "sing-box 启动失败：%s" % e
                try:
                    os.unlink(cfg_path)
                except OSError:
                    pass
                continue
            deadline = time.time() + 10
            ok = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                if _port_open(port):
                    ok = True
                    break
                time.sleep(0.2)
            if ok:
                _proxy["proc"] = proc
                _proxy["port"] = port
                _proxy["cfg"] = cfg_path
                try:
                    with open(os.path.join(DATA_DIR, "sb.pid"), "w", encoding="utf-8") as fh:
                        fh.write(str(proc.pid))
                except OSError:
                    pass
                return port
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
            last_err = "sing-box 端口未就绪"
        _proxy["last_fail"] = time.time()
        raise AppError(last_err, 502)


# ------------------------------------------------------------------- allowlist

def allowlisted(url):
    """Default-deny: only hosts/paths observed in real YouTube HLS
    manifests (measured 2026-11-15). This endpoint must never be an
    open proxy."""
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if p.scheme != "https":
        return False
    if p.username or p.password:
        return False
    try:
        port = p.port
    except ValueError:
        return False
    if port is not None and port != 443:
        return False
    host = (p.hostname or "").lower()
    path = p.path or ""
    if host == "googlevideo.com" or host.endswith(".googlevideo.com"):
        return path.startswith("/api/manifest/") or path.startswith("/videoplayback")
    if host in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        return path == "/api/timedtext" or path.startswith("/api/timedtext/")
    return False


# ------------------------------------------------------------- manifest rewrite

_URI_ATTR_RE = re.compile(r'URI="([^"]*)"')


def _relay_ref(target_url, base_url, video_id):
    absolute = urllib.parse.urljoin(base_url, target_url)
    return "relay?u=%s&v=%s" % (urllib.parse.quote(absolute, safe=""), video_id)


def rewrite_playlist(text, base_url, video_id):
    """Rewrite every media/playlist URI in an m3u8 to the same-origin
    relay; all other lines byte-identical."""
    out = []
    for line in text.split("\n"):
        stripped = line.rstrip("\r")
        if stripped.startswith("#"):
            if "URI=" in stripped:
                def _sub(m):
                    return 'URI="%s"' % _relay_ref(m.group(1), base_url, video_id)
                stripped = _URI_ATTR_RE.sub(_sub, stripped)
            out.append(stripped)
        elif stripped.strip():
            out.append(_relay_ref(stripped.strip(), base_url, video_id))
        else:
            out.append(stripped)
    return "\n".join(out)


# -------------------------------------------------------------- curl fetch core

class _ProcReader:
    def __init__(self, proc):
        self._proc = proc
        self._buf = bytearray()
        self._eof = False

    def _fill(self):
        if self._eof:
            return False
        try:
            chunk = self._proc.stdout.read(65536)
        except (OSError, ValueError):
            chunk = b""
        if not chunk:
            self._eof = True
            return False
        self._buf += chunk
        return True

    def read_until(self, delim, cap):
        while True:
            idx = self._buf.find(delim)
            if idx >= 0:
                out = bytes(self._buf[:idx + len(delim)])
                del self._buf[:idx + len(delim)]
                return out
            if len(self._buf) > cap:
                raise AppError("上游响应头过大", 502)
            if not self._fill():
                out = bytes(self._buf)
                self._buf.clear()
                return out

    def read_chunk(self, size):
        if not self._buf and not self._eof:
            self._fill()
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def read_all(self, cap):
        chunks = []
        total = 0
        while True:
            chunk = self.read_chunk(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise AppError("上游响应超过大小上限", 502)
            chunks.append(chunk)
        return b"".join(chunks)


def _kill_proc(proc):
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except OSError:
        pass
    try:
        proc.stdout.close()
    except (OSError, ValueError):
        pass


def _read_head(reader):
    """Read header blocks until a final (non-1xx) status line."""
    for _ in range(5):
        raw = reader.read_until(b"\r\n\r\n", 65536)
        if not raw:
            raise AppError("上游无响应", 502)
        text = raw.decode("iso-8859-1")
        lines = text.split("\r\n")
        m = re.match(r"HTTP/\S+ (\d{3})", lines[0] if lines else "")
        if not m:
            raise AppError("上游响应异常", 502)
        status = int(m.group(1))
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if 100 <= status < 200:
            continue
        return status, headers
    raise AppError("上游响应头过多", 502)


def _start_fetch(url, proxy_port, range_hdr, max_time):
    """Spawn curl through the proxy; follow redirects manually so every
    hop re-checks the allowlist. Returns (proc, reader, status, headers,
    final_url). Raises AppError on failure."""
    current = url
    for _hop in range(4):
        cmd = ["curl", "-s", "-i", "--globoff",
               "--proxy", "socks5h://127.0.0.1:%d" % proxy_port,
               "--connect-timeout", "15",
               "--max-time", str(max_time),
               "--speed-limit", "1024", "--speed-time", "30",
               "-H", "User-Agent: " + UA]
        if range_hdr:
            cmd += ["-H", "Range: " + range_hdr]
        cmd.append(current)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as e:
            raise AppError("取流工具启动失败：%s" % e, 502)
        reader = _ProcReader(proc)
        try:
            status, headers = _read_head(reader)
        except AppError:
            _kill_proc(proc)
            raise
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("location")
            _kill_proc(proc)
            if not loc:
                raise AppError("上游重定向缺少目标", 502)
            nxt = urllib.parse.urljoin(current, loc)
            if not allowlisted(nxt):
                raise AppError("重定向目标不在白名单内", 403)
            current = nxt
            continue
        return proc, reader, status, headers, current
    raise AppError("重定向次数过多", 502)


def _is_playlist_response(headers, url):
    ctype = (headers.get("content-type") or "").lower()
    if "mpegurl" in ctype:
        return True
    path = urllib.parse.urlparse(url).path
    return ("hls_playlist" in path or "hls_variant" in path
            or "hls_timedtext_playlist" in path or path.endswith(".m3u8"))


def _relay_kind(headers, url):
    ctype = (headers.get("content-type") or "").lower()
    if "mpegurl" in ctype:
        return "manifest"
    if "timedtext" in url or "vtt" in ctype:
        return "subtitle"
    return "segment"


_FORWARD_TYPES = ("video/", "audio/", "application/vnd.apple.mpegurl",
                  "application/x-mpegurl", "text/vtt", "text/plain",
                  "application/octet-stream")


def _safe_content_type(headers):
    ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
    if any(ctype.startswith(p) for p in _FORWARD_TYPES):
        return ctype or "application/octet-stream"
    return "application/octet-stream"


# --------------------------------------------------------------------- resolve

_YT_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com",
             "music.youtube.com", "youtu.be")


def _valid_video_url(url):
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    return host in _YT_HOSTS or host.endswith(".youtube.com")


def _remember_manifest(video_id, master_url, title):
    with _manifests_lock:
        if len(_MANIFESTS) >= 32:
            oldest = min(_MANIFESTS, key=lambda k: _MANIFESTS[k]["at"])
            _MANIFESTS.pop(oldest, None)
        _MANIFESTS[video_id] = {"url": master_url, "title": title,
                                "at": time.time()}


def _lookup_manifest(video_id):
    with _manifests_lock:
        row = _MANIFESTS.get(video_id)
        return row["url"] if row else None


def do_resolve(url):
    with _resolve_lock:
        t0 = time.time()
        video_id = ""
        try:
            result = _resolve_locked(url)
            video_id = result["video_id"]
            _log(RESOLVE_LOG, {"ts": int(time.time()), "video_id": video_id,
                               "ok": True, "error": "",
                               "elapsed_ms": int((time.time() - t0) * 1000)})
            return result
        except AppError as e:
            _log(RESOLVE_LOG, {"ts": int(time.time()), "video_id": video_id,
                               "ok": False, "error": e.message,
                               "elapsed_ms": int((time.time() - t0) * 1000)})
            raise


def _resolve_locked(url):
    if not url or len(url) > 2048 or not _valid_video_url(url):
        raise AppError("请输入 youtube.com 或 youtu.be 的视频链接", 400)
    port = ensure_proxy()
    ytdlp = _ensure_ytdlp()
    if not ytdlp:
        raise AppError("yt-dlp 自举失败，请稍候再试", 502)
    deno = _ensure_deno()
    if not deno:
        raise AppError("deno 自举失败，请稍候再试", 502)
    fd, tmp_path = tempfile.mkstemp(prefix="ytplay-resolve-", suffix=".tmp",
                                    dir=DATA_DIR)
    os.close(fd)
    cmd = ["nice", "-n", "10", ytdlp, "--no-playlist", "--no-warnings",
           "--socket-timeout", "30", "--retries", "2",
           "--js-runtimes", "deno:" + deno,
           "--proxy", "socks5h://127.0.0.1:%d" % port,
           "-J", url]
    try:
        with open(tmp_path, "wb") as out_f:
            r = subprocess.run(cmd, stdout=out_f, stderr=subprocess.PIPE,
                               timeout=YTDLP_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise AppError("解析超时（90 秒），请重试", 502)
    finally:
        pass
    try:
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace").strip().splitlines()
            tail = err[-1][:240] if err else "yt-dlp 失败"
            if "Sign in to confirm" in tail or "not a bot" in tail:
                raise AppError("YouTube 拒绝了本次提取（机器人墙），请稍候再试", 502)
            raise AppError("无法读取视频信息：" + tail, 502)
        with open(tmp_path, encoding="utf-8") as fh:
            info = json.load(fh)
    except AppError:
        raise
    except (OSError, ValueError) as e:
        raise AppError("解析结果无法读取：%s" % e, 502)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    video_id = info.get("id") or ""
    if not video_id:
        raise AppError("解析结果缺少视频 ID", 502)
    fmts = info.get("formats") or []
    master_url = ""
    for f in fmts:
        if f.get("protocol") == "m3u8_native" and f.get("manifest_url"):
            master_url = f["manifest_url"]
            break
    if not master_url:
        raise AppError("该视频没有可用的 HLS 清单（可能为直播回放限制或地区限制）", 502)
    height_max = 0
    for f in fmts:
        h = f.get("height") or 0
        if h > height_max:
            height_max = h
    subs_manual = sorted((info.get("subtitles") or {}).keys())
    subs_auto = sorted((info.get("automatic_captions") or {}).keys())
    audio_langs = sorted({f.get("language") for f in fmts
                          if f.get("language")
                          and f.get("acodec") not in (None, "none")})
    combined = [f for f in fmts
                if f.get("vcodec") not in (None, "none")
                and f.get("acodec") not in (None, "none")
                and f.get("protocol") == "https"
                and (f.get("height") or 9999) <= 720
                and f.get("url")]
    combined.sort(key=lambda f: f.get("height") or 0, reverse=True)
    direct_rel = ""
    if combined:
        direct_rel = "api/relay?u=%s&v=%s" % (
            urllib.parse.quote(combined[0]["url"], safe=""), video_id)
    title = info.get("title") or ""
    _remember_manifest(video_id, master_url, title)
    return {"video_id": video_id, "title": title,
            "duration": info.get("duration") or 0,
            "height_max": height_max,
            "manifest": "api/manifest?v=" + video_id,
            "direct": direct_rel,
            "subtitles_manual": subs_manual,
            "subtitles_auto": subs_auto,
            "audio_langs": audio_langs}


# ------------------------------------------------------------------- WSGI views

def _json_response(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store")])
    return [body]


def _read_json_body(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > 4096:
        raise AppError("请求体异常", 400)
    raw = environ["wsgi.input"].read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AppError("请求体不是合法 JSON", 400)


def _view_resolve(environ, start_response):
    try:
        body = _read_json_body(environ)
        result = do_resolve((body or {}).get("url") or "")
    except AppError as e:
        return _json_response(start_response, "%d Error" % e.status,
                              {"ok": False, "error": e.message})
    result["ok"] = True
    return _json_response(start_response, "200 OK", result)


def _fetch_playlist_rewritten(url, video_id, proxy_port):
    """Fetch an m3u8 through the proxy and return the rewritten text."""
    proc, reader, status, headers, final_url = _start_fetch(
        url, proxy_port, None, PLAYLIST_MAX_TIME)
    try:
        if status != 200:
            raise AppError("上游返回 HTTP %d" % status, 502)
        raw = reader.read_all(PLAYLIST_MAX_BYTES)
        rc = proc.wait(timeout=10)
        if rc != 0:
            raise AppError("上游传输中断", 502)
    finally:
        _kill_proc(proc)
    text = raw.decode("utf-8", "replace")
    if "#EXTM3U" not in text[:200]:
        raise AppError("上游返回的不是播放列表", 502)
    return rewrite_playlist(text, final_url, video_id)


def _view_manifest(environ, start_response):
    qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    video_id = (qs.get("v") or [""])[0]
    if not VIDEO_ID_RE.match(video_id):
        return _json_response(start_response, "400 Error",
                              {"ok": False, "error": "缺少视频 ID"})
    master_url = _lookup_manifest(video_id)
    if not master_url:
        return _json_response(start_response, "404 Error",
                              {"ok": False, "error": "请先解析该视频"})
    try:
        port = ensure_proxy()
        text = _fetch_playlist_rewritten(master_url, video_id, port)
    except AppError as e:
        return _json_response(start_response, "%d Error" % e.status,
                              {"ok": False, "error": e.message})
    body = text.encode("utf-8")
    _log(RELAY_LOG, {"ts": int(time.time()), "video_id": video_id,
                     "kind": "manifest", "bytes": len(body)})
    start_response("200 OK", [("Content-Type", "application/vnd.apple.mpegurl"),
                              ("Content-Length", str(len(body))),
                              ("Cache-Control", "no-store")])
    return [body]


def _view_relay(environ, start_response):
    qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    raw_url = (qs.get("u") or [""])[0]
    video_id = (qs.get("v") or [""])[0]
    if not raw_url:
        return _json_response(start_response, "400 Error",
                              {"ok": False, "error": "缺少目标地址"})
    if not VIDEO_ID_RE.match(video_id):
        return _json_response(start_response, "400 Error",
                              {"ok": False, "error": "缺少视频 ID"})
    if not allowlisted(raw_url):
        return _json_response(start_response, "403 Error",
                              {"ok": False, "error": "目标地址不在白名单内"})
    range_hdr = environ.get("HTTP_RANGE") or ""
    if range_hdr and not RANGE_RE.match(range_hdr.strip()):
        range_hdr = ""
    if not _relay_sem.acquire(timeout=30):
        return _json_response(start_response, "503 Error",
                              {"ok": False, "error": "服务器忙，请稍候再试"})
    try:
        port = ensure_proxy()
    except AppError as e:
        _relay_sem.release()
        return _json_response(start_response, "%d Error" % e.status,
                              {"ok": False, "error": e.message})
    try:
        proc, reader, status, headers, final_url = _start_fetch(
            raw_url, port, range_hdr, RELAY_MAX_TIME)
    except AppError as e:
        _relay_sem.release()
        return _json_response(start_response, "%d Error" % e.status,
                              {"ok": False, "error": e.message})
    if status not in (200, 206):
        _kill_proc(proc)
        _relay_sem.release()
        return _json_response(start_response, "502 Error",
                              {"ok": False,
                               "error": "上游返回 HTTP %d" % status})
    kind = _relay_kind(headers, final_url)
    ctype = _safe_content_type(headers)
    if _is_playlist_response(headers, final_url):
        try:
            raw = reader.read_all(PLAYLIST_MAX_BYTES)
            rc = proc.wait(timeout=10)
            if rc != 0:
                raise AppError("上游传输中断", 502)
        except AppError as e:
            _kill_proc(proc)
            _relay_sem.release()
            return _json_response(start_response, "%d Error" % e.status,
                                  {"ok": False, "error": e.message})
        _kill_proc(proc)
        text = raw.decode("utf-8", "replace")
        if "#EXTM3U" not in text[:200]:
            _relay_sem.release()
            return _json_response(start_response, "502 Error",
                                  {"ok": False, "error": "上游返回的不是播放列表"})
        body = rewrite_playlist(text, final_url, video_id).encode("utf-8")
        _log(RELAY_LOG, {"ts": int(time.time()), "video_id": video_id,
                         "kind": kind, "bytes": len(body)})
        _relay_sem.release()
        start_response("200 OK",
                       [("Content-Type", "application/vnd.apple.mpegurl"),
                        ("Content-Length", str(len(body))),
                        ("Cache-Control", "no-store"),
                        ("X-Content-Type-Options", "nosniff")])
        return [body]
    try:
        content_length = int(headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    if content_length > RELAY_MAX_BYTES:
        _kill_proc(proc)
        _relay_sem.release()
        return _json_response(start_response, "502 Error",
                              {"ok": False, "error": "上游响应超过大小上限"})
    resp_headers = [("Content-Type", ctype),
                    ("Cache-Control", "private, max-age=600"),
                    ("X-Content-Type-Options", "nosniff"),
                    ("Accept-Ranges", "bytes")]
    if content_length:
        resp_headers.append(("Content-Length", str(content_length)))
    if status == 206 and headers.get("content-range"):
        resp_headers.append(("Content-Range", headers["content-range"]))
    start_response("%d OK" % status if status == 200 else "206 Partial Content",
                   resp_headers)
    return _stream_body(proc, reader, video_id, kind)


def _stream_body(proc, reader, video_id, kind):
    total = 0
    try:
        while True:
            chunk = reader.read_chunk(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > RELAY_MAX_BYTES:
                break
            yield chunk
    finally:
        _kill_proc(proc)
        _log(RELAY_LOG, {"ts": int(time.time()), "video_id": video_id,
                         "kind": kind, "bytes": total})
        _relay_sem.release()


def _view_page(start_response):
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                              ("Content-Length", str(len(PAGE_BYTES))),
                              ("Cache-Control", "no-store")])
    return [PAGE_BYTES]


def _view_hls_js(start_response):
    try:
        with open(HLS_JS, "rb") as fh:
            body = fh.read()
    except OSError:
        return _json_response(start_response, "404 Error",
                              {"ok": False, "error": "播放器组件缺失"})
    start_response("200 OK", [("Content-Type", "application/javascript"),
                              ("Content-Length", str(len(body))),
                              ("Cache-Control", "public, max-age=3600")])
    return [body]


def application(environ, start_response):
    path = environ.get("PATH_INFO", "")
    method = environ.get("REQUEST_METHOD", "GET")
    if path in ("", "/"):
        if method != "GET":
            return _json_response(start_response, "405 Error",
                                  {"ok": False, "error": "方法不支持"})
        return _view_page(start_response)
    if path == "/static/hls.min.js":
        return _view_hls_js(start_response)
    if path == "/api/session":
        return _json_response(start_response, "200 OK", {"authenticated": True})
    if path == "/api/resolve":
        if method != "POST":
            return _json_response(start_response, "405 Error",
                                  {"ok": False, "error": "方法不支持"})
        return _view_resolve(environ, start_response)
    if path == "/api/manifest":
        if method != "GET":
            return _json_response(start_response, "405 Error",
                                  {"ok": False, "error": "方法不支持"})
        return _view_manifest(environ, start_response)
    if path == "/api/relay":
        if method != "GET":
            return _json_response(start_response, "405 Error",
                                  {"ok": False, "error": "方法不支持"})
        return _view_relay(environ, start_response)
    return _json_response(start_response, "404 Error",
                          {"ok": False, "error": "没有这个地址"})


app = application


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YouTube 播放</title>
<style>
:root{--bg:#ffffff;--fg:#1a1a1a;--muted:#666666;--line:#e2e2e2;--card:#f7f7f7;--accent:#d30303;--ok:#1a7f37;--err:#d30303;--btnfg:#ffffff}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#e8e8e8;--muted:#9a9a9a;--line:#2c2c2c;--card:#1c1c1c;--accent:#ff4d4d;--ok:#3fb950;--err:#ff7b72;--btnfg:#ffffff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;font-size:16px;line-height:1.5}
.wrap{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 2px 0}
.hint{color:var(--muted);font-size:14px;margin:0 0 14px 0}
.row{display:flex;gap:8px}
.urlinput{flex:1;min-width:0;height:46px;padding:0 12px;font-size:16px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:8px}
.playbtn{height:46px;padding:0 22px;font-size:16px;font-weight:600;color:var(--btnfg);background:var(--accent);border:0;border-radius:8px;cursor:pointer;white-space:nowrap}
.playbtn:disabled{opacity:0.45;cursor:default}
.waitbox{margin-top:14px;display:none}
.bar{height:4px;background:var(--line);border-radius:2px;overflow:hidden}
.barfill{height:100%;width:40%;background:var(--accent);animation:slide 1s linear infinite}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(260%)}}
.waittime{color:var(--muted);font-size:14px;margin-top:6px}
.errbox{display:none;margin-top:14px;padding:12px;border:1px solid var(--err);border-radius:8px;color:var(--err);font-size:14px}
.errbox button{margin-left:10px;height:34px;padding:0 14px;font-size:14px;color:var(--btnfg);background:var(--err);border:0;border-radius:6px;cursor:pointer}
.videowrap{display:none;margin-top:16px}
video{width:100%;aspect-ratio:16/9;background:#000000;border-radius:8px;display:block}
.metatitle{font-size:17px;font-weight:600;margin:10px 0 2px 0}
.metaline{color:var(--muted);font-size:13px;margin:0}
.selrow{display:none;gap:8px;margin-top:10px;flex-wrap:wrap}
.selrow label{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px}
.selrow select{height:40px;font-size:14px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:0 8px;max-width:220px}
.tracknote{display:none;color:var(--muted);font-size:13px;margin-top:8px}
.fallbacknote{display:none;color:var(--muted);font-size:13px;margin-top:8px}
.recent{margin-top:22px}
.recent h2{font-size:15px;margin:0 0 8px 0}
.recentitem{display:flex;gap:10px;align-items:baseline;width:100%;text-align:left;padding:10px 8px;background:none;border:0;border-top:1px solid var(--line);color:var(--fg);font-size:14px;cursor:pointer}
.recentitem:last-child{border-bottom:1px solid var(--line)}
.recenttitle{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.recentdur{color:var(--muted);font-size:13px;white-space:nowrap}
.recentdate{color:var(--muted);font-size:13px;white-space:nowrap}
.emptyrecent{color:var(--muted);font-size:14px;padding:10px 8px;border-top:1px solid var(--line)}
</style>
</head>
<body>
<div class="wrap">
<h1>YouTube 播放</h1>
<p class="hint">粘贴视频链接，点击播放；播放走无广告流，字幕与配音默认中文。</p>
<div class="row">
<input class="urlinput" id="url" type="url" placeholder="https://www.youtube.com/watch?v=…" autocomplete="off" spellcheck="false">
<button class="playbtn" id="play">播放</button>
</div>
<div class="waitbox" id="wait">
<div class="bar"><div class="barfill"></div></div>
<div class="waittime" id="waittime">正在解析…</div>
</div>
<div class="errbox" id="error"><span id="errtext"></span><button id="retry" type="button">重试</button><button id="usehls" type="button" style="display:none">改用 hls.js 重试</button></div>
<div class="videowrap" id="videowrap">
<video id="player" controls playsinline preload="metadata"></video>
<div class="metatitle" id="title"></div>
<p class="metaline" id="metaline"></p>
<div class="selrow" id="selrow">
<label>字幕 <select id="selsub"></select></label>
<label>音轨 <select id="selaud"></select></label>
<label>清晰度 <select id="selq"></select></label>
</div>
<p class="tracknote" id="tracknote"></p>
<p class="fallbacknote" id="fallbacknote"></p>
</div>
<div class="recent">
<h2>最近看过</h2>
<div id="recentlist"></div>
</div>
</div>
<script>
var BASE = location.pathname;
if (BASE.slice(-1) !== "/") { BASE += "/"; }
var video = document.getElementById("player");
var NATIVE = false;
try {
  var _ua = navigator.userAgent || "";
  var _isSafari = /Safari/.test(_ua) && !/Chrome|Chromium|Edg|OPR|Firefox/.test(_ua);
  NATIVE = _isSafari && video.canPlayType("application/vnd.apple.mpegurl") !== "";
} catch (e) { NATIVE = false; }
var hls = null;
var hlsPromise = null;
var currentDirect = "";
var tracksDefaulted = false;
var lastManifest = "";
var timerId = null;
var startMs = 0;
var RECENT_KEY = "ytplay_recent_v1";

function $(id) { return document.getElementById(id); }
function show(el) { el.style.display = (el.id === "selrow") ? "flex" : (el.tagName === "BUTTON" ? "inline-block" : "block"); }
function hide(el) { el.style.display = "none"; }
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function fmtDur(s) {
  s = Math.round(s || 0);
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  var mm = (m < 10 ? "0" : "") + m;
  var ss = (sec < 10 ? "0" : "") + sec;
  if (h > 0) { return h + ":" + mm + ":" + ss; }
  return m + ":" + ss;
}
function fmtDate(ts) {
  var d = new Date(ts);
  var mo = d.getMonth() + 1;
  var da = d.getDate();
  return mo + "月" + da + "日";
}
function startWait() {
  startMs = Date.now();
  show($("wait"));
  $("waittime").textContent = "正在解析…";
  timerId = setInterval(function () {
    var el = (Date.now() - startMs) / 1000;
    $("waittime").textContent = "正在解析，已等待 " + el.toFixed(1) + " 秒";
  }, 100);
}
function stopWait() {
  if (timerId) { clearInterval(timerId); timerId = null; }
  hide($("wait"));
}
function showError(msg, offerHls) {
  $("errtext").textContent = msg;
  if (offerHls) { show($("usehls")); } else { hide($("usehls")); }
  show($("error"));
}
function hideError() { hide($("error")); }
function stopVideo() {
  try { video.pause(); } catch (e) {}
  video.removeAttribute("src");
  video.load();
  if (hls) { try { hls.destroy(); } catch (e) {} hls = null; }
  hide($("videowrap"));
  hide($("selrow"));
  hide($("tracknote"));
  hide($("fallbacknote"));
}
function loadHls() {
  if (window.Hls) { return Promise.resolve(); }
  if (hlsPromise) { return hlsPromise; }
  hlsPromise = new Promise(function (resolve, reject) {
    var s = document.createElement("script");
    s.src = BASE + "static/hls.min.js";
    s.onload = function () { resolve(); };
    s.onerror = function () { reject(new Error("播放器组件加载失败")); };
    document.head.appendChild(s);
  });
  return hlsPromise;
}
function pickIndex(tracks, prefs) {
  var i, j, lang;
  for (i = 0; i < prefs.length; i++) {
    for (j = 0; j < tracks.length; j++) {
      if ((tracks[j].lang || "") === prefs[i]) { return j; }
    }
  }
  for (j = 0; j < tracks.length; j++) {
    lang = tracks[j].lang || "";
    if (lang.indexOf("zh") === 0) { return j; }
  }
  return -1;
}
function buildSelectors() {
  var subSel = $("selsub");
  var audSel = $("selaud");
  var qSel = $("selq");
  var i, opt;
  subSel.innerHTML = "";
  opt = document.createElement("option");
  opt.value = "-1";
  opt.textContent = "关闭字幕";
  subSel.appendChild(opt);
  var subs = hls.subtitleTracks || [];
  var auds = hls.audioTracks || [];
  for (i = 0; i < subs.length; i++) {
    opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = (subs[i].name || subs[i].lang || ("字幕 " + (i + 1)));
    subSel.appendChild(opt);
  }
  audSel.innerHTML = "";
  for (i = 0; i < auds.length; i++) {
    opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = (auds[i].name || auds[i].lang || ("音轨 " + (i + 1)));
    audSel.appendChild(opt);
  }
  if (!tracksDefaulted && (subs.length > 0 || auds.length > 0)) {
    var subIdx = pickIndex(subs, ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK", "en"]);
    hls.subtitleTrack = subIdx;
    subSel.value = String(subIdx);
    var audIdx = pickIndex(auds, ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK"]);
    if (audIdx >= 0) { hls.audioTrack = audIdx; audSel.value = String(audIdx); }
    else { audSel.value = String(hls.audioTrack); }
    tracksDefaulted = true;
  } else {
    subSel.value = String(hls.subtitleTrack);
    audSel.value = String(hls.audioTrack);
  }
  var levels = hls.levels || [];
  var order = [];
  for (i = 0; i < levels.length; i++) { order.push(i); }
  order.sort(function (a, b) { return (levels[b].height || 0) - (levels[a].height || 0); });
  qSel.innerHTML = "";
  opt = document.createElement("option");
  opt.value = "-1";
  opt.textContent = "自动";
  qSel.appendChild(opt);
  for (i = 0; i < order.length; i++) {
    var lv = levels[order[i]];
    opt = document.createElement("option");
    opt.value = String(order[i]);
    var mbps = (lv.bitrate / 1000000).toFixed(1);
    opt.textContent = (lv.height || "?") + "p · " + mbps + " Mbps";
    qSel.appendChild(opt);
  }
  qSel.value = String(hls.nextLevel);
  show($("selrow"));
}
function useDirectFallback(msg) {
  if (currentDirect) {
    if (hls) { try { hls.destroy(); } catch (e) {} hls = null; }
    hide($("selrow"));
    video.src = BASE + currentDirect;
    video.play().catch(function () {});
    $("fallbacknote").textContent = "已切换直链播放（清晰度较低，无多音轨）。";
    show($("fallbacknote"));
  } else {
    showError(msg || "播放失败，请重试");
  }
}
async function playManifest(manifestUrl) {
  lastManifest = manifestUrl;
  show($("videowrap"));
  if (NATIVE) {
    video.src = manifestUrl;
    video.play().catch(function () {});
    $("tracknote").textContent = "系统播放器：音轨与字幕请用播放器自带菜单选择。";
    show($("tracknote"));
    return;
  }
  try {
    await loadHls();
  } catch (e) {
    showError(e.message);
    return;
  }
  if (hls) { try { hls.destroy(); } catch (e) {} hls = null; }
  tracksDefaulted = false;
  hls = new Hls({maxBufferLength: 30});
  hls.loadSource(manifestUrl);
  hls.attachMedia(video);
  hls.on(Hls.Events.MANIFEST_PARSED, function () {
    buildSelectors();
    video.play().catch(function () {});
  });
  hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, function () { buildSelectors(); });
  hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, function () { buildSelectors(); });
  hls.on(Hls.Events.ERROR, function (ev, data) {
    if (!data || !data.fatal) { return; }
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) { hls.startLoad(); }
    else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); }
    else { useDirectFallback("播放器无法恢复，请重试"); }
  });
}
function saveRecent(url, d) {
  var list = [];
  try { list = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); } catch (e) { list = []; }
  list = list.filter(function (r) { return r.url !== url; });
  list.unshift({url: url, title: d.title || "", dur: d.duration || 0, ts: Date.now()});
  list = list.slice(0, 10);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch (e) {}
}
function renderRecent() {
  var list = [];
  try { list = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); } catch (e) { list = []; }
  var box = $("recentlist");
  box.innerHTML = "";
  if (list.length === 0) {
    var empty = document.createElement("div");
    empty.className = "emptyrecent";
    empty.textContent = "还没有看过的视频。";
    box.appendChild(empty);
    return;
  }
  list.forEach(function (r) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "recentitem";
    var t = document.createElement("span");
    t.className = "recenttitle";
    t.textContent = r.title || r.url;
    var du = document.createElement("span");
    du.className = "recentdur";
    du.textContent = fmtDur(r.dur);
    var da = document.createElement("span");
    da.className = "recentdate";
    da.textContent = fmtDate(r.ts);
    b.appendChild(t);
    b.appendChild(du);
    b.appendChild(da);
    b.addEventListener("click", function () {
      $("url").value = r.url;
      resolve(r.url);
    });
    box.appendChild(b);
  });
}
async function resolve(url) {
  url = (url || "").trim();
  if (!url) { showError("请先粘贴视频链接"); return; }
  var btn = $("play");
  btn.disabled = true;
  hideError();
  hide($("metaline"));
  stopVideo();
  startWait();
  var t0 = Date.now();
  try {
    var r = await fetch(BASE + "api/resolve", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url: url})
    });
    var d = await r.json();
    stopWait();
    btn.disabled = false;
    if (!r.ok) {
      showError((d && d.error) || "解析失败");
      return;
    }
    var elapsed = ((Date.now() - t0) / 1000).toFixed(1);
    saveRecent(url, d);
    renderRecent();
    if (d.video_id) { try { history.replaceState(null, "", BASE + "?v=" + encodeURIComponent(d.video_id)); } catch (e) {} }
    $("title").textContent = d.title || "";
    var subCount = (d.subtitles_manual || []).length + (d.subtitles_auto || []).length;
    var audCount = (d.audio_langs || []).length;
    $("metaline").textContent = "时长 " + fmtDur(d.duration) + " · 最高 " + (d.height_max || "?") + "p · 字幕 " + subCount + " 种 · 配音 " + audCount + " 种 · 解析用时 " + elapsed + " 秒";
    show($("metaline"));
    currentDirect = d.direct || "";
    playManifest(BASE + d.manifest);
  } catch (e) {
    stopWait();
    btn.disabled = false;
    showError("网络异常：" + e.message);
  }
}
$("play").addEventListener("click", function () { resolve($("url").value); });
$("url").addEventListener("keydown", function (e) {
  if (e.key === "Enter") { resolve($("url").value); }
});
$("retry").addEventListener("click", function () { resolve($("url").value); });
$("usehls").addEventListener("click", function () {
  NATIVE = false;
  hideError();
  if (lastManifest) { playManifest(lastManifest); }
});
$("selsub").addEventListener("change", function () {
  if (hls) { hls.subtitleTrack = parseInt(this.value, 10); }
});
$("selaud").addEventListener("change", function () {
  if (hls) { hls.audioTrack = parseInt(this.value, 10); }
});
$("selq").addEventListener("change", function () {
  if (hls) { hls.nextLevel = parseInt(this.value, 10); }
});
video.addEventListener("error", function () {
  if (NATIVE && lastManifest && video.getAttribute("src")) {
    showError("系统播放器无法播放该视频，可以改用 hls.js 再试一次。", true);
  }
});
renderRecent();
(function () {
  var q = new URLSearchParams(location.search);
  var u = q.get("u");
  var v = q.get("v");
  if (v && /^[A-Za-z0-9_-]{11}$/.test(v)) {
    u = "https://www.youtube.com/watch?v=" + v;
  }
  if (u) {
    $("url").value = u;
    resolve(u);
  }
})();
</script>
</body>
</html>
"""

PAGE_BYTES = PAGE.encode("utf-8")


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    make_server("127.0.0.1", 18097, application).serve_forever()
