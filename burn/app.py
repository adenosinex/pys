"""Burn-after-reading relay （阅后即焚）, built on the user's myweb
cross-device text relay (tech/relay page + the universal kv store).
His semantics carry over: short custom pickup codes, time-based
expiry (30 min / 1 h / 24 h / never) with expired entries destroyed
on touch. This project adds the true burn: by default an entry is
deleted the moment it is successfully picked up, and a missing code
answers uniformly so a burned code is indistinguishable from one
that never existed. Storage is a small SQLite table in the platform
data directory. No authentication of its own: the App Hub platform
gate decides who gets in."""
import json
import os
import re
import sqlite3
import time
from urllib.parse import unquote

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


DATA_DIR = _platform_data_dir(BASE)
DB_PATH = os.path.join(DATA_DIR, "burn.db")
os.makedirs(DATA_DIR, exist_ok=True)

MAX_CONTENT = 256 * 1024
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>阅后即焚 · 跨端文字中转</title>
<style>
    body { font-family: system-ui; background: #f4f5f7; max-width: 800px; margin: 0 auto; padding: 20px; color: #333; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
    @media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }
    .card { background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
    textarea { width: 100%; height: 150px; padding: 10px; box-sizing: border-box; border: 1px solid #ddd; border-radius: 8px; resize: vertical; margin-bottom: 10px; font-family: monospace; }
    input, select, button { padding: 10px; box-sizing: border-box; border: 1px solid #ddd; border-radius: 8px; margin-bottom: 10px; width: 100%; }
    button { background: #007bff; color: white; border: none; font-weight: bold; cursor: pointer; transition: 0.2s; }
    button:hover { background: #0056b3; }
    .btn-green { background: #28a745; } .btn-green:hover { background: #218838; }
    .btn-orange { background: #fd7e14; } .btn-orange:hover { background: #e86e10; }
    .code-display { font-size: 24px; font-weight: bold; color: #d63384; text-align: center; letter-spacing: 2px; margin: 10px 0; }
    .hint { font-size: 13px; color: #888; margin: 4px 0 10px; }
    .burned { color: #d63384; font-weight: bold; }
</style>
</head>
<body>
    <a href="../links/" style="text-decoration: none; color: #007bff; margin-bottom: 20px; display: inline-block;">← 微书签</a>
    <h2 style="text-align: center;">🔥 阅后即焚 · 跨端文字中转</h2>
    <p class="hint" style="text-align:center;">默认提取一次即销毁；也可以只按时间过期、保留多次提取。</p>

    <div class="grid">
        <div class="card">
            <h3>📤 发送文字</h3>
            <textarea id="sendText" placeholder="粘贴你需要跨设备共享的文字或代码..."></textarea>

            <div style="display: flex; gap: 10px;">
                <input type="text" id="customCode" placeholder="提取码" title="可自定义提取码" style="flex: 1; text-align: center; font-weight: bold; font-size: 16px; color: #d63384;">
                <select id="burnMode" style="flex: 2;">
                    <option value="1">阅后即焚：提取一次即销毁</option>
                    <option value="0">不销毁：到期前可多次提取</option>
                </select>
            </div>
            <div style="display: flex; gap: 10px;">
                <select id="expireTime" style="flex: 2;">
                    <option value="30">过期: 30分钟后</option>
                    <option value="60">过期: 1小时后</option>
                    <option value="1440" selected>过期: 24小时后</option>
                    <option value="0">永不过期</option>
                </select>
                <button id="uploadBtn" onclick="uploadText()" style="flex: 1.5;">上传</button>
            </div>

            <div id="codeResult" style="display: none;">
                <p style="text-align: center; margin: 5px 0;">请在另一台设备输入此提取码：</p>
                <div class="code-display" id="shareCode"></div>
            </div>
        </div>

        <div class="card">
            <h3>📥 接收文字</h3>
            <div style="display: flex; gap: 10px;">
                <input type="text" id="receiveCode" placeholder="输入提取码..." style="flex: 2; font-size: 18px; text-align: center;">
                <button onclick="fetchText()" class="btn-green" style="flex: 1;">提取</button>
            </div>

            <textarea id="receiveText" placeholder="提取的内容将显示在这里..." readonly></textarea>
            <p class="hint" id="burnNote" style="display:none;"></p>

            <div style="display: flex; gap: 10px;">
                <button onclick="copyText()" style="flex: 1;">📋 复制到剪贴板</button>
            </div>

            <div style="display: flex; gap: 10px; margin-top: 10px; padding-top: 10px; border-top: 1px dashed #eee;">
                <input type="text" id="fileExt" value=".csv" placeholder="后缀(如 .csv, .py)" style="flex: 1;">
                <button onclick="downloadFile()" class="btn-orange" style="flex: 2;">💾 下载为文件</button>
            </div>
        </div>
    </div>

<script>
const BASE = location.pathname.replace(/\/?$/, "/");
function generateRandomCode(length) {
    const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
    let result = "";
    for (let i = 0; i < length; i++) result += chars.charAt(Math.floor(Math.random() * chars.length));
    return result;
}
window.onload = () => { document.getElementById('customCode').value = generateRandomCode(4); };

async function uploadText() {
    const text = document.getElementById('sendText').value;
    if (!text) return alert("内容为空，请输入需要中转的文字！");
    let code = document.getElementById('customCode').value.trim();
    if (!code) { code = generateRandomCode(4); document.getElementById('customCode').value = code; }
    const minutes = parseInt(document.getElementById('expireTime').value);
    const expireAt = minutes > 0 ? Math.floor(Date.now() / 1000) + minutes * 60 : null;
    const burn = document.getElementById('burnMode').value === "1";
    const btn = document.getElementById('uploadBtn');
    btn.innerText = "上传中..."; btn.disabled = true;
    try {
        const res = await fetch(BASE + "api/kv/" + encodeURIComponent(code), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: text, expire_at: expireAt, burn: burn })
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(d.message || ("HTTP " + res.status));
        document.getElementById('codeResult').style.display = 'block';
        document.getElementById('shareCode').innerText = code;
        document.getElementById('receiveCode').value = code;
        btn.innerText = "✅ 已上传"; btn.style.background = "#28a745";
        setTimeout(() => {
            btn.innerText = "上传"; btn.style.background = ""; btn.disabled = false;
            document.getElementById('customCode').value = generateRandomCode(4);
        }, 2000);
    } catch (err) {
        alert("上传失败：" + err.message);
        btn.innerText = "上传"; btn.disabled = false;
    }
}

async function fetchText() {
    const code = document.getElementById('receiveCode').value.trim();
    if (!code) return alert("请输入提取码！");
    const note = document.getElementById('burnNote');
    note.style.display = 'none';
    try {
        const res = await fetch(BASE + "api/kv/" + encodeURIComponent(code));
        const d = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(d.message || ("HTTP " + res.status));
        document.getElementById('receiveText').value = d.content || "";
        if (d.burned) {
            note.innerHTML = "🔥 这条内容已在提取后<span class='burned'>永久销毁</span>，无法再次提取。";
            note.style.display = 'block';
        } else if (d.expire_at) {
            note.textContent = "⏳ 有效期至 " + new Date(d.expire_at * 1000).toLocaleString() + "，到期自动销毁。";
            note.style.display = 'block';
        }
    } catch (err) {
        document.getElementById('receiveText').value = "";
        alert("提取失败：" + err.message);
    }
}

function copyText() {
    const el = document.getElementById('receiveText');
    if (!el.value) return alert("没有可复制的内容！");
    el.select();
    navigator.clipboard.writeText(el.value).then(() => alert("已复制到剪贴板")).catch(() => document.execCommand('copy'));
}

function downloadFile() {
    const text = document.getElementById('receiveText').value;
    if (!text) return alert("没有可下载的内容！");
    let ext = document.getElementById('fileExt').value.trim() || ".txt";
    if (!ext.startsWith('.')) ext = '.' + ext;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
    a.download = 'relay_' + Date.now() + ext;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 400);
}
</script>
</body>
</html>
"""


# ------------------------------------------------------------ storage

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kv ("
        " code TEXT PRIMARY KEY, content TEXT NOT NULL,"
        " expire_at REAL, burn INTEGER NOT NULL DEFAULT 1,"
        " created_at REAL NOT NULL)")
    return conn


def _json(start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def _read_json(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0 or length > MAX_CONTENT + 65536:
        return None
    try:
        return json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
    except Exception:
        return None


def h_set(environ, start_response, code):
    if not CODE_RE.match(code):
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "bad_code", "message": "提取码只能是 1–32 位字母、数字、- 或 _。"})
    data = _read_json(environ)
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "bad_content", "message": "内容为空或格式不对。"})
    content = data["content"]
    if len(content.encode("utf-8")) > MAX_CONTENT:
        return _json(start_response, "400 Bad Request",
                     {"ok": False, "error": "too_large", "message": "内容超过 256 KB。"})
    expire_at = data.get("expire_at")
    if expire_at is not None:
        try:
            expire_at = float(expire_at)
        except (TypeError, ValueError):
            expire_at = None
    burn = 1 if data.get("burn", True) else 0
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv (code, content, expire_at, burn, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (code, content, expire_at, burn, time.time()))
    return _json(start_response, "200 OK", {"ok": True, "code": code})


def h_get(environ, start_response, code):
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT content, expire_at, burn FROM kv WHERE code = ?", (code,)).fetchone()
        if row is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "not_found",
                          "message": "提取码不存在或已被销毁。"})
        content, expire_at, burn = row
        if expire_at is not None and expire_at <= now:
            conn.execute("DELETE FROM kv WHERE code = ?", (code,))
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "expired",
                          "message": "提取码已过期，数据已永久销毁。"})
        if burn:
            conn.execute("DELETE FROM kv WHERE code = ?", (code,))
    return _json(start_response, "200 OK",
                 {"ok": True, "content": content, "burned": bool(burn),
                  "expire_at": expire_at})


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"

    if path == "/" and method == "GET":
        body = PAGE.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]
    if path == "/api/session" and method == "GET":
        return _json(start_response, "200 OK", {"authenticated": True, "via": "platform"})
    m = re.fullmatch(r"/api/kv/(.+)", path)
    if m and method == "POST":
        return h_set(environ, start_response, unquote(m.group(1)))
    if m and method == "GET":
        return h_get(environ, start_response, unquote(m.group(1)))
    return _json(start_response, "404 Not Found", {"ok": False, "error": "not_found"})


app = application
