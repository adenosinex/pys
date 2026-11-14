# -*- coding: utf-8 -*-
"""chat - 跨平台即时中转：文字与文件在一条时间线里互传。

One timeline, two kinds of messages: text (tap to copy) and files
(upload once, download from any device). Persistent SQLite + files dir
in the platform data dir; rolling cap 500 messages, 5 GiB total file
storage with oldest-first eviction. No auth in this project: identity
verification lives only in the panel platform layer (App Hub rule);
loopback access is trusted. Stdlib only.

Routes:
  GET  /                page
  GET  /api/session     {authenticated: true}
  GET  /api/messages?after=<id>&limit=<n>
  POST /api/send        JSON {text, device}
  POST /api/upload      multipart (device field + one file part)
  GET  /dl/<id>         attachment download, streamed
  POST /api/delete      JSON {id}
  POST /api/clear       JSON {confirm: true}
"""
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.parse
import uuid

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
FILES_DIR = os.path.join(DATA_DIR, "files")
DB_PATH = os.path.join(DATA_DIR, "chat.db")
os.makedirs(FILES_DIR, exist_ok=True)

MAX_MESSAGES = 500
MAX_TEXT_CHARS = 65536
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024 * 1024
MIN_FREE = 2 * 1024 * 1024 * 1024
MAX_DEVICE_LEN = 20

_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


with _lock:
    _c = _db()
    _c.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
        "device TEXT NOT NULL, kind TEXT NOT NULL, text TEXT, "
        "filename TEXT, stored TEXT, size INTEGER)"
    )
    _c.execute("PRAGMA journal_mode=WAL")
    _c.commit()
    _c.close()


def _clean_device(value):
    s = str(value or "").strip()[:MAX_DEVICE_LEN]
    return s or "设备"


def _unlink(stored):
    if not stored or "/" in stored or "\\" in stored:
        return
    try:
        os.remove(os.path.join(FILES_DIR, stored))
    except OSError:
        pass


def _prune(conn):
    rows = conn.execute(
        "SELECT id, stored FROM messages ORDER BY id DESC LIMIT -1 OFFSET ?",
        (MAX_MESSAGES,),
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM messages WHERE id=?", (r["id"],))
        _unlink(r["stored"])
    while True:
        total = conn.execute(
            "SELECT COALESCE(SUM(size),0) FROM messages WHERE kind='file'"
        ).fetchone()[0]
        if total <= MAX_TOTAL_BYTES:
            break
        old = conn.execute(
            "SELECT id, stored FROM messages WHERE kind='file' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not old:
            break
        conn.execute("DELETE FROM messages WHERE id=?", (old["id"],))
        _unlink(old["stored"])


def _json(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def _read_json(environ, limit=262144):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > limit:
        return None
    try:
        return json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
    except Exception:
        return None


# --- streaming multipart reader (pattern proven in transfer/app.py) ---
class _Reader:
    def __init__(self, stream, length):
        self.stream = stream
        self.left = length
        self.buf = b""

    def read_more(self):
        if self.left <= 0:
            return False
        chunk = self.stream.read(min(262144, self.left))
        if not chunk:
            self.left = 0
            return False
        self.left -= len(chunk)
        self.buf += chunk
        return True

    def until(self, marker):
        idx = self.buf.find(marker)
        while idx < 0:
            keep = len(marker) + 8
            if len(self.buf) > keep:
                cut = len(self.buf) - keep
                yield self.buf[:cut]
                self.buf = self.buf[cut:]
            if not self.read_more():
                if self.buf:
                    yield self.buf
                    self.buf = b""
                return
            idx = self.buf.find(marker)
        if idx:
            yield self.buf[:idx]
        self.buf = self.buf[idx + len(marker):]


def _parse_disposition(value):
    params = {}
    parts = []
    cur = ""
    in_quotes = False
    for ch in value or "":
        if ch == '"':
            in_quotes = not in_quotes
            cur += ch
        elif ch == ";" and not in_quotes:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    for part in parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1].replace('\\"', '"')
            params[k.strip().lower()] = v
    star = params.get("filename*")
    if star and "'" in star:
        try:
            bits = star.split("'", 2)
            charset = bits[0] or "utf-8"
            encoded = bits[2] if len(bits) > 2 else ""
            params["filename"] = urllib.parse.unquote_to_bytes(encoded).decode(
                charset, "replace"
            )
        except Exception:
            pass
    return params


def _safe_display_name(name):
    base = os.path.basename(str(name or ""))
    base = "".join(ch for ch in base if ord(ch) >= 32 and ch not in '/\\')
    return base[:120]


def handle_upload(environ):
    ctype = environ.get("CONTENT_TYPE", "")
    if "multipart/form-data" not in ctype or "boundary=" not in ctype:
        return {"ok": False, "error": "需要 multipart/form-data 上传"}, "400 Bad Request"
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
    marker = ("--" + boundary).encode()
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return {"ok": False, "error": "空请求"}, "400 Bad Request"
    if length > MAX_FILE_BYTES + 1024 * 1024:
        return {"ok": False, "error": "文件超过 200 MB 上限"}, "413 Payload Too Large"
    free = shutil.disk_usage(FILES_DIR).free
    if free - length < MIN_FREE:
        return {"ok": False, "error": "服务器剩余空间不足（需保留 2 GiB）"}, "507 Insufficient Storage"
    reader = _Reader(environ["wsgi.input"], length)
    delim = b"\r\n" + marker
    device = "设备"
    saved = []
    for _ in reader.until(marker):
        pass
    while True:
        while len(reader.buf) < 2 and reader.read_more():
            pass
        if reader.buf.startswith(b"--"):
            break
        if reader.buf.startswith(b"\r\n"):
            reader.buf = reader.buf[2:]
        header_blob = b""
        for chunk in reader.until(b"\r\n\r\n"):
            header_blob += chunk
        headers = {}
        for line in header_blob.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = _parse_disposition(headers.get("content-disposition", ""))
        filename = disp.get("filename", "")
        field = disp.get("name", "")
        if filename:
            safe = _safe_display_name(filename)
            if not safe:
                for _ in reader.until(delim):
                    pass
                continue
            stored = "f%s_%s" % (int(time.time() * 1000), uuid.uuid4().hex[:8])
            tmp_path = os.path.join(FILES_DIR, ".up-" + stored)
            size = 0
            oversized = False
            with open(tmp_path, "wb") as fh:
                for chunk in reader.until(delim):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        oversized = True
                        break
                    fh.write(chunk)
            if oversized:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return {"ok": False, "error": "文件超过 200 MB 上限"}, "413 Payload Too Large"
            dest_path = os.path.join(FILES_DIR, stored)
            os.replace(tmp_path, dest_path)
            saved.append({"filename": safe, "stored": stored, "size": size})
        else:
            acc = b""
            for chunk in reader.until(delim):
                if len(acc) < 4096:
                    acc += chunk[:4096 - len(acc)]
            if field == "device":
                device = _clean_device(acc.decode("utf-8", "replace"))
        if reader.left <= 0 and not reader.buf:
            break
    if not saved:
        return {"ok": False, "error": "没有收到文件"}, "400 Bad Request"
    ids = []
    with _lock:
        conn = _db()
        for s in saved:
            cur = conn.execute(
                "INSERT INTO messages (ts, device, kind, filename, stored, size) "
                "VALUES (?, ?, 'file', ?, ?, ?)",
                (time.time(), device, s["filename"], s["stored"], s["size"]),
            )
            ids.append(cur.lastrowid)
        _prune(conn)
        conn.commit()
        conn.close()
    return {"ok": True, "ids": ids}, "200 OK"


def _row_to_msg(r):
    return {
        "id": r["id"],
        "ts": r["ts"],
        "device": r["device"],
        "kind": r["kind"],
        "text": r["text"],
        "name": r["filename"],
        "size": r["size"],
    }


def application(environ, start_response):
    try:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/") or "/"
        if len(path) > 1:
            path = path.rstrip("/") or "/"

        if path == "/" and method == "GET":
            body = PAGE_HTML.encode("utf-8")
            start_response(
                "200 OK",
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        if path == "/api/session" and method == "GET":
            return _json(start_response, "200 OK", {"authenticated": True})

        if path == "/api/messages" and method == "GET":
            qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
            try:
                after = int(qs.get("after", ["0"])[0])
            except ValueError:
                after = 0
            try:
                limit = max(1, min(500, int(qs.get("limit", ["200"])[0])))
            except ValueError:
                limit = 200
            with _lock:
                conn = _db()
                rows = conn.execute(
                    "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (after, limit),
                ).fetchall()
                max_id = conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM messages"
                ).fetchone()[0]
                conn.close()
            return _json(
                start_response,
                "200 OK",
                {"messages": [_row_to_msg(r) for r in rows], "max_id": max_id},
            )

        if path == "/api/send" and method == "POST":
            data = _read_json(environ)
            if not isinstance(data, dict):
                return _json(start_response, "400 Bad Request",
                             {"ok": False, "error": "请求体不是 JSON"})
            text = str(data.get("text", ""))
            if not text.strip():
                return _json(start_response, "400 Bad Request",
                             {"ok": False, "error": "内容为空"})
            if len(text) > MAX_TEXT_CHARS:
                return _json(start_response, "400 Bad Request",
                             {"ok": False, "error": "文字超过 64K 字上限"})
            with _lock:
                conn = _db()
                cur = conn.execute(
                    "INSERT INTO messages (ts, device, kind, text) VALUES (?, ?, 'text', ?)",
                    (time.time(), _clean_device(data.get("device")), text),
                )
                mid = cur.lastrowid
                _prune(conn)
                conn.commit()
                conn.close()
            return _json(start_response, "200 OK", {"ok": True, "id": mid})

        if path == "/api/upload" and method == "POST":
            obj, status = handle_upload(environ)
            return _json(start_response, status, obj)

        m = re.fullmatch(r"/dl/(\d+)", path)
        if m and method == "GET":
            mid = int(m.group(1))
            with _lock:
                conn = _db()
                row = conn.execute(
                    "SELECT * FROM messages WHERE id=? AND kind='file'", (mid,)
                ).fetchone()
                conn.close()
            if not row or not row["stored"]:
                return _json(start_response, "404 Not Found",
                             {"ok": False, "error": "文件不存在"})
            fpath = os.path.join(FILES_DIR, row["stored"])
            if not os.path.isfile(fpath):
                return _json(start_response, "404 Not Found",
                             {"ok": False, "error": "文件不存在"})
            name = row["filename"] or "file"
            ascii_name = re.sub(r"[^\x20-\x7e]", "_", name) or "file"
            disp = (
                'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                % (ascii_name, urllib.parse.quote(name))
            )
            size = os.path.getsize(fpath)
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/octet-stream"),
                    ("Content-Disposition", disp),
                    ("Content-Length", str(size)),
                ],
            )
            fh = open(fpath, "rb")
            return iter(lambda: fh.read(1024 * 1024), b"")

        if path == "/api/delete" and method == "POST":
            data = _read_json(environ)
            if not isinstance(data, dict):
                return _json(start_response, "400 Bad Request",
                             {"ok": False, "error": "请求体不是 JSON"})
            try:
                mid = int(data.get("id", 0))
            except (TypeError, ValueError):
                return _json(start_response, "400 Bad Request",
                             {"ok": False, "error": "id 无效"})
            with _lock:
                conn = _db()
                row = conn.execute(
                    "SELECT stored FROM messages WHERE id=?", (mid,)
                ).fetchone()
                conn.execute("DELETE FROM messages WHERE id=?", (mid,))
                conn.commit()
                conn.close()
            if row:
                _unlink(row["stored"])
            return _json(start_response, "200 OK", {"ok": True})

        if path == "/api/clear" and method == "POST":
            data = _read_json(environ)
            if not isinstance(data, dict) or data.get("confirm") is not True:
                return _json(start_response, "400 Bad Request",
                             {"ok": False, "error": "需要确认"})
            with _lock:
                conn = _db()
                conn.execute("DELETE FROM messages")
                conn.commit()
                conn.close()
                for entry in os.scandir(FILES_DIR):
                    if entry.is_file():
                        try:
                            os.remove(entry.path)
                        except OSError:
                            pass
            return _json(start_response, "200 OK", {"ok": True})

        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "接口不存在"})
    except Exception as e:
        print("chat error: %r" % (e,))
        return _json(start_response, "500 Internal Server Error",
                     {"ok": False, "error": "服务器错误"})


app = application

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>聊天中转</title>
<style>
:root { --bg:#fff; --panel:#f7f7f5; --text:#1a1a18; --muted:#8a8a84; --line:#e4e4de; --bubble:#f7f7f5; --accent:#2563eb; --accent-text:#fff; --danger:#dc2626; }
@media (prefers-color-scheme: dark) { :root { --bg:#161613; --panel:#1f1f1c; --text:#ececea; --muted:#9a9a94; --line:#2e2e2a; --bubble:#22221f; --accent:#3b82f6; --accent-text:#fff; --danger:#f87171; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:16px/1.5 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
.wrap { display:flex; flex-direction:column; min-height:100dvh; max-width:760px; margin:0 auto; }
header { position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:10px; padding:10px 14px; background:var(--bg); border-bottom:1px solid var(--line); }
header h1 { font-size:17px; margin:0; white-space:nowrap; }
header input { flex:1; min-width:0; font:inherit; font-size:14px; color:var(--text); background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:8px 10px; }
.linkbtn { background:none; border:0; color:var(--muted); font:inherit; font-size:14px; padding:10px 4px; min-height:44px; cursor:pointer; }
#status { display:none; padding:8px 14px; font-size:13px; color:var(--danger); border-bottom:1px solid var(--line); }
#timeline { flex:1; padding:12px 14px 20px; }
.empty { text-align:center; color:var(--muted); font-size:14px; padding:48px 20px; }
.msg { margin:0 0 14px; }
.msg .meta { display:flex; align-items:baseline; gap:8px; font-size:12px; color:var(--muted); margin-bottom:4px; }
.msg .meta .acts { margin-left:auto; display:flex; gap:2px; }
.msg .meta button { background:none; border:0; color:var(--muted); font:inherit; font-size:12px; padding:6px 8px; min-height:32px; cursor:pointer; }
.msg .body { background:var(--bubble); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
.msg .body pre { margin:0; white-space:pre-wrap; word-break:break-word; font:inherit; }
.filecard .name { word-break:break-all; font-weight:600; }
.filecard .size { color:var(--muted); font-size:13px; }
.filecard a.dl { display:inline-block; margin-top:8px; background:var(--accent); color:var(--accent-text); text-decoration:none; border-radius:8px; padding:10px 18px; min-height:44px; line-height:24px; }
#composer { position:sticky; bottom:0; background:var(--bg); border-top:1px solid var(--line); padding:10px 14px calc(10px + env(safe-area-inset-bottom)); }
#uprog { display:none; margin-bottom:8px; font-size:13px; color:var(--muted); }
#uprog .bar { height:6px; border-radius:3px; background:var(--line); overflow:hidden; margin-bottom:4px; }
#uprog .bar i { display:block; height:100%; width:0; background:var(--accent); }
.composer-row { display:flex; gap:8px; align-items:flex-end; }
#text { flex:1; font:inherit; color:var(--text); background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:11px 12px; resize:none; min-height:46px; max-height:132px; }
.attach { flex:none; width:46px; height:46px; border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:10px; font-size:20px; line-height:44px; text-align:center; cursor:pointer; }
#send { flex:none; min-width:64px; height:46px; border:0; border-radius:10px; background:var(--accent); color:var(--accent-text); font:inherit; font-size:16px; cursor:pointer; }
#send:disabled { opacity:.5; }
</style>
</head>
<body>
<div class="wrap">
<header>
<h1>聊天中转</h1>
<input id="device" maxlength="20" autocomplete="off" spellcheck="false" aria-label="本机名">
<button class="linkbtn" id="clearBtn" type="button">清空</button>
</header>
<div id="status" role="alert"></div>
<main id="timeline"><div class="empty" id="empty">还没有消息。在下方发一条文字或文件，另一台设备打开同一网址就能看到。</div></main>
<div id="composer">
<div id="uprog"><div class="bar"><i id="upbar"></i></div><span id="uptext"></span></div>
<div class="composer-row">
<label class="attach" for="fileInput" title="添加文件">+</label>
<input type="file" id="fileInput" multiple hidden>
<textarea id="text" rows="1" placeholder="输入文字，Enter 发送，Shift+Enter 换行" aria-label="消息文字"></textarea>
<button id="send" type="button">发送</button>
</div>
</div>
</div>
<script>
(function () {
"use strict";
var BASE = location.pathname;
if (BASE.slice(-1) !== "/") BASE += "/";
var lastId = 0, polling = false, failCount = 0;
var timeline = document.getElementById("timeline");
var emptyEl = document.getElementById("empty");
var statusEl = document.getElementById("status");
var textEl = document.getElementById("text");
var sendBtn = document.getElementById("send");
var deviceEl = document.getElementById("device");
var fileInput = document.getElementById("fileInput");
var uprog = document.getElementById("uprog");
var upbar = document.getElementById("upbar");
var uptext = document.getElementById("uptext");

function defaultDevice() {
  var ua = navigator.userAgent || "";
  if (/Android/.test(ua)) return "安卓手机";
  if (/iPhone|iPad/.test(ua)) return "iPhone";
  if (/Windows/.test(ua)) return "Windows 电脑";
  if (/Macintosh/.test(ua)) return "Mac";
  return "浏览器";
}
deviceEl.value = localStorage.getItem("chat_device") || defaultDevice();
deviceEl.addEventListener("change", function () {
  localStorage.setItem("chat_device", deviceEl.value);
});
function device() { return (deviceEl.value || "").trim() || defaultDevice(); }

function showStatus(msg) {
  if (!msg) { statusEl.style.display = "none"; statusEl.textContent = ""; return; }
  statusEl.style.display = "block";
  statusEl.textContent = msg;
}
function fmtTime(ts) {
  var d = new Date(ts * 1000), now = new Date();
  var hh = String(d.getHours()).padStart(2, "0"), mm = String(d.getMinutes()).padStart(2, "0");
  if (d.toDateString() === now.toDateString()) return hh + ":" + mm;
  return (d.getMonth() + 1) + "/" + d.getDate() + " " + hh + ":" + mm;
}
function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(2) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}
function nearBottom() {
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
}
function render(msg) {
  var wrap = document.createElement("div");
  wrap.className = "msg";
  wrap.dataset.id = msg.id;
  var meta = document.createElement("div");
  meta.className = "meta";
  var who = document.createElement("span");
  who.textContent = msg.device;
  var when = document.createElement("span");
  when.textContent = fmtTime(msg.ts);
  var acts = document.createElement("span");
  acts.className = "acts";
  if (msg.kind === "text") {
    var cp = document.createElement("button");
    cp.type = "button";
    cp.textContent = "复制";
    cp.addEventListener("click", function () { copyText(msg.text, cp); });
    acts.appendChild(cp);
  }
  var del = document.createElement("button");
  del.type = "button";
  del.textContent = "删除";
  del.addEventListener("click", function () { deleteMsg(msg.id, wrap); });
  acts.appendChild(del);
  meta.appendChild(who);
  meta.appendChild(when);
  meta.appendChild(acts);
  var body = document.createElement("div");
  body.className = "body";
  if (msg.kind === "text") {
    var pre = document.createElement("pre");
    pre.textContent = msg.text;
    body.appendChild(pre);
  } else {
    body.className += " filecard";
    var nm = document.createElement("div");
    nm.className = "name";
    nm.textContent = msg.name;
    var sz = document.createElement("div");
    sz.className = "size";
    sz.textContent = fmtSize(msg.size || 0);
    var a = document.createElement("a");
    a.className = "dl";
    a.href = BASE + "dl/" + msg.id;
    a.textContent = "下载";
    body.appendChild(nm);
    body.appendChild(sz);
    body.appendChild(a);
  }
  wrap.appendChild(meta);
  wrap.appendChild(body);
  timeline.appendChild(wrap);
}
function copyText(text, btn) {
  function done() {
    btn.textContent = "已复制";
    setTimeout(function () { btn.textContent = "复制"; }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () { fallback(); });
  } else { fallback(); }
  function fallback() {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    done();
  }
}
function deleteMsg(id, node) {
  if (!window.confirm("删除这条消息？文件也会一并删除。")) return;
  fetch(BASE + "api/delete", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: id }) }).then(function (r) { return r.json(); }).then(function (d) {
    if (d.ok) { node.remove(); syncEmpty(); }
    else showStatus("删除失败：" + (d.error || "未知错误"));
  }).catch(function () { showStatus("删除失败：网络错误"); });
}
function syncEmpty() {
  emptyEl.style.display = timeline.querySelector(".msg") ? "none" : "block";
}
function poll() {
  if (polling) return;
  polling = true;
  var stick = nearBottom();
  fetch(BASE + "api/messages?after=" + lastId + "&limit=200").then(function (r) {
    if (!r.ok) throw new Error("http " + r.status);
    return r.json();
  }).then(function (d) {
    failCount = 0;
    showStatus("");
    (d.messages || []).forEach(function (m) {
      if (m.id > lastId) lastId = m.id;
      render(m);
    });
    if (d.max_id > lastId) lastId = d.max_id;
    syncEmpty();
    if (stick) window.scrollTo(0, document.body.scrollHeight);
    polling = false;
    setTimeout(poll, 3000);
  }).catch(function () {
    polling = false;
    failCount += 1;
    showStatus("连接断开，正在重试…");
    setTimeout(poll, failCount > 3 ? 10000 : 5000);
  });
}
function sendText() {
  var v = textEl.value;
  if (!v.trim()) return;
  sendBtn.disabled = true;
  fetch(BASE + "api/send", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: v, device: device() }) }).then(function (r) { return r.json(); }).then(function (d) {
    sendBtn.disabled = false;
    if (d.ok) {
      textEl.value = "";
      autogrow();
      poll();
    } else showStatus("发送失败：" + (d.error || "未知错误"));
  }).catch(function () {
    sendBtn.disabled = false;
    showStatus("发送失败：网络错误");
  });
}
function autogrow() {
  textEl.style.height = "auto";
  textEl.style.height = Math.min(textEl.scrollHeight, 132) + "px";
}
textEl.addEventListener("input", autogrow);
textEl.addEventListener("keydown", function (e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendText(); }
});
sendBtn.addEventListener("click", sendText);
function uploadOne(file) {
  return new Promise(function (resolve) {
    var fd = new FormData();
    fd.append("device", device());
    fd.append("file", file);
    var xhr = new XMLHttpRequest();
    var t0 = Date.now(), timer = null, shown = false;
    xhr.open("POST", BASE + "api/upload");
    xhr.upload.addEventListener("progress", function (e) {
      if (!shown && Date.now() - t0 > 300) { shown = true; uprog.style.display = "block"; }
      if (shown && e.lengthComputable && e.total > 0) {
        upbar.style.width = Math.round(e.loaded * 100 / e.total) + "%";
        uptext.textContent = file.name + " · " + ((Date.now() - t0) / 1000).toFixed(1) + " 秒";
      }
    });
    timer = setInterval(function () {
      if (shown) uptext.textContent = file.name + " · " + ((Date.now() - t0) / 1000).toFixed(1) + " 秒";
    }, 100);
    xhr.addEventListener("load", function () {
      clearInterval(timer);
      var secs = ((Date.now() - t0) / 1000).toFixed(1);
      if (xhr.status === 200) {
        uprog.style.display = "block";
        upbar.style.width = "100%";
        uptext.textContent = "已发送 · " + secs + " 秒";
        setTimeout(function () { uprog.style.display = "none"; upbar.style.width = "0"; }, 4000);
        poll();
      } else {
        uprog.style.display = "none";
        var msg = "上传失败";
        try { msg = (JSON.parse(xhr.responseText).error) || msg; } catch (e) {}
        showStatus(msg);
      }
      resolve();
    });
    xhr.addEventListener("error", function () {
      clearInterval(timer);
      uprog.style.display = "none";
      showStatus("上传失败：网络错误");
      resolve();
    });
    xhr.send(fd);
  });
}
fileInput.addEventListener("change", function () {
  var files = Array.prototype.slice.call(fileInput.files || []);
  fileInput.value = "";
  var chain = Promise.resolve();
  files.forEach(function (f) { chain = chain.then(function () { return uploadOne(f); }); });
});
document.getElementById("clearBtn").addEventListener("click", function () {
  if (!window.confirm("清空全部消息和文件？此操作不可恢复。")) return;
  fetch(BASE + "api/clear", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }) }).then(function (r) { return r.json(); }).then(function (d) {
    if (d.ok) {
      timeline.querySelectorAll(".msg").forEach(function (n) { n.remove(); });
      lastId = 0;
      syncEmpty();
      poll();
    } else showStatus("清空失败：" + (d.error || "未知错误"));
  }).catch(function () { showStatus("清空失败：网络错误"); });
});
poll();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    print("chat dev server on http://127.0.0.1:18098/ (DATA_DIR=%s)" % DATA_DIR)
    make_server("127.0.0.1", 18098, application).serve_forever()
