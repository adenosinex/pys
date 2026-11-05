"""Cloud files （云文件）: a small self-hosted file manager in the
platform data directory — browse folders, upload (streamed, with
progress), download (Range-aware, resumable), search by name,
create / rename / delete. Deliberately simple: one directory tree
is the whole product; upload and download are the import/export.
No authentication of its own: the App Hub platform gate decides
who gets in."""
import json
import os
import re
import shutil
import time
from urllib.parse import parse_qs, quote, unquote

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
ROOT = os.path.join(DATA_DIR, "files")
os.makedirs(ROOT, exist_ok=True)
MIN_FREE = 2 * 1024 ** 3
SEARCH_CAP = 300

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>云文件</title>
<style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; background: #0f1420; color: #e6ebf4; margin: 0; padding: 18px; }
    .wrap { max-width: 980px; margin: 0 auto; }
    h1 { font-size: 20px; margin: 4px 0 14px; }
    h1 small { color: #8b93a7; font-size: 13px; font-weight: normal; margin-left: 8px; }
    .bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
    .bar input[type=text] { flex: 1; min-width: 160px; background: #1a2233; border: 1px solid #2c3a57; color: #e6ebf4; border-radius: 8px; padding: 9px 12px; }
    button, .upbtn { background: #2f6fd0; color: #fff; border: none; border-radius: 8px; padding: 9px 14px; cursor: pointer; font-size: 14px; }
    button.ghost, .upbtn.ghost { background: #232a3d; border: 1px solid #2c3a57; }
    button:hover, .upbtn:hover { filter: brightness(1.15); }
    .crumbs { display: flex; gap: 4px; flex-wrap: wrap; margin: 8px 0 12px; color: #8b93a7; }
    .crumbs a { color: #7db4ff; text-decoration: none; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; background: #151c2c; border-radius: 12px; overflow: hidden; }
    th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #232c44; font-size: 14px; }
    th { color: #8b93a7; font-weight: 600; background: #182034; }
    tr.item:hover { background: #1a2338; }
    td.size, td.time { color: #9aa5bb; white-space: nowrap; }
    td.ops { white-space: nowrap; text-align: right; }
    td.ops button { padding: 5px 9px; font-size: 12px; margin-left: 4px; }
    .fname { cursor: pointer; color: #e6ebf4; text-decoration: none; }
    .fname.dir { color: #ffd479; font-weight: 600; }
    #drop { border: 2px dashed #2c3a57; border-radius: 12px; padding: 14px; text-align: center; color: #8b93a7; margin-bottom: 12px; }
    #drop.over { border-color: #2f6fd0; color: #e6ebf4; }
    .prog { display: none; margin-bottom: 12px; background: #151c2c; border-radius: 8px; padding: 10px 12px; font-size: 13px; }
    .prog .track { height: 8px; background: #232c44; border-radius: 4px; margin-top: 6px; overflow: hidden; }
    .prog .fill { height: 100%; background: #2f6fd0; width: 0%; }
    .empty { padding: 26px; text-align: center; color: #8b93a7; }
</style>
</head>
<body>
<div class="wrap">
    <h1>☁️ 云文件<small id="usage"></small></h1>
    <div class="bar">
        <input type="text" id="q" placeholder="搜索文件名（当前目录树内）..." onkeydown="if(event.key==='Enter')doSearch()">
        <button class="ghost" onclick="doSearch()">搜索</button>
        <label class="upbtn">上传文件<input type="file" id="fileInput" multiple style="display:none" onchange="uploadFiles(this.files); this.value=''"></label>
        <button class="ghost" onclick="mkdir()">新建文件夹</button>
        <button class="ghost" onclick="load(curPath)">刷新</button>
    </div>
    <div id="drop">把文件拖到这里上传到当前文件夹</div>
    <div class="prog" id="prog"><span id="progText"></span><div class="track"><div class="fill" id="progFill"></div></div></div>
    <div class="crumbs" id="crumbs"></div>
    <table>
        <thead><tr><th>名称</th><th style="width:110px">大小</th><th style="width:170px">修改时间</th><th style="width:210px"></th></tr></thead>
        <tbody id="rows"></tbody>
    </table>
</div>
<script>
const BASE = location.pathname.replace(/\/?$/, "/");
let curPath = "";
let searchMode = false;

function fmtSize(n) {
    if (n === null || n === undefined) return "-";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < 4) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
}
function fmtTime(ts) { return new Date(ts * 1000).toLocaleString(); }
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

async function api(path, opts) {
    const r = await fetch(BASE + path, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.message || ("HTTP " + r.status));
    return d;
}

function crumbs() {
    const el = document.getElementById('crumbs');
    if (searchMode) { el.innerHTML = '<a onclick="exitSearch()">← 返回文件夹</a><span>搜索结果</span>'; return; }
    const parts = curPath ? curPath.split("/") : [];
    let html = '<a onclick="go(\'\')">根目录</a>', acc = "";
    parts.forEach(p => { acc = acc ? acc + "/" + p : p; html += ' / <a onclick="go(\'' + esc(acc) + '\')">' + esc(p) + '</a>'; });
    el.innerHTML = html;
}

async function load(path) {
    searchMode = false; curPath = path || "";
    const d = await api("api/list?path=" + encodeURIComponent(curPath));
    document.getElementById('usage').textContent = "共 " + fmtSize(d.usage.total) + " · " + d.usage.files + " 个文件";
    render(d.items, true);
    crumbs();
}

function render(items, inFolder) {
    const tb = document.getElementById('rows');
    if (!items.length) { tb.innerHTML = '<tr><td colspan="4" class="empty">这里什么都没有</td></tr>'; return; }
    tb.innerHTML = items.map(it => {
        const full = it.path;
        const nameCell = it.dir
            ? '<a class="fname dir" onclick="go(\'' + esc(full) + '\')">📁 ' + esc(it.name) + '</a>'
            : '<a class="fname" href="' + BASE + 'file?path=' + encodeURIComponent(full) + '">📄 ' + esc(it.name) + '</a>';
        return '<tr class="item"><td>' + nameCell + '</td>' +
            '<td class="size">' + (it.dir ? "-" : fmtSize(it.size)) + '</td>' +
            '<td class="time">' + fmtTime(it.mtime) + '</td>' +
            '<td class="ops">' +
            (it.dir ? '' : '<button class="ghost" onclick="dl(\'' + esc(full) + '\')">下载</button>') +
            '<button class="ghost" onclick="renameItem(\'' + esc(full) + '\', \'' + esc(it.name) + '\')">改名</button>' +
            '<button class="ghost" onclick="delItem(\'' + esc(full) + '\', \'' + esc(it.name) + '\')">删除</button>' +
            '</td></tr>';
    }).join("");
}

function go(p) { load(p); }
function exitSearch() { load(curPath); }
function dl(p) { window.location = BASE + "file?path=" + encodeURIComponent(p) + "&dl=1"; }

async function doSearch() {
    const q = document.getElementById('q').value.trim();
    if (!q) { load(curPath); return; }
    const d = await api("api/search?q=" + encodeURIComponent(q));
    searchMode = true;
    render(d.items, false);
    crumbs();
}

async function mkdir() {
    const name = prompt("文件夹名称：");
    if (!name) return;
    try { await api("api/mkdir", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ path: curPath, name }) }); load(curPath); }
    catch (e) { alert(e.message); }
}

async function renameItem(full, oldName) {
    const nn = prompt("新名称：", oldName);
    if (!nn || nn === oldName) return;
    try { await api("api/rename", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ path: full, new_name: nn }) }); searchMode ? doSearch() : load(curPath); }
    catch (e) { alert(e.message); }
}

async function delItem(full, name) {
    if (!confirm("确定删除「" + name + "」？文件夹会连同内容一起删除。")) return;
    try { await api("api/delete", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ path: full }) }); searchMode ? doSearch() : load(curPath); }
    catch (e) { alert(e.message); }
}

function uploadFiles(files) {
    files = Array.from(files || []);
    if (!files.length) return;
    const prog = document.getElementById('prog'), txt = document.getElementById('progText'), fill = document.getElementById('progFill');
    prog.style.display = 'block';
    let idx = 0;
    const next = () => {
        if (idx >= files.length) { txt.textContent = "全部上传完成"; fill.style.width = "100%"; setTimeout(() => { prog.style.display = 'none'; load(curPath); }, 600); return; }
        const f = files[idx];
        txt.textContent = "上传 " + (idx + 1) + "/" + files.length + "：" + f.name;
        const xhr = new XMLHttpRequest();
        xhr.open("POST", BASE + "api/upload?path=" + encodeURIComponent(curPath) + "&name=" + encodeURIComponent(f.name));
        xhr.upload.onprogress = e => { if (e.lengthComputable) fill.style.width = Math.round(e.loaded / e.total * 100) + "%"; };
        xhr.onload = () => {
            let d = {}; try { d = JSON.parse(xhr.responseText); } catch (e) {}
            if (xhr.status !== 200) { alert("上传失败：" + (d.message || xhr.status)); prog.style.display = 'none'; return; }
            idx++; fill.style.width = "0%"; next();
        };
        xhr.onerror = () => { alert("上传出错：" + f.name); prog.style.display = 'none'; };
        xhr.send(f);
    };
    next();
}

const drop = document.getElementById('drop');
['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => uploadFiles(e.dataTransfer.files));

load("");
</script>
</body>
</html>
"""


# ------------------------------------------------------------ http utils

def _json(start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def _err(start_response, code, message):
    return _json(start_response, "%d Error" % code,
                 {"ok": False, "error": "e%d" % code, "message": message})


def _read_json(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0 or length > 65536:
        return {}
    try:
        return json.loads(environ["wsgi.input"].read(length).decode("utf-8") or "{}")
    except Exception:
        return {}


# ------------------------------------------------------------ fs helpers

def _safe_join(rel):
    """Resolve a user path inside ROOT; None when it escapes."""
    rel = (rel or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None, ""
    norm = "/".join(parts)
    full = os.path.join(ROOT, *parts) if parts else ROOT
    if os.path.abspath(full) != ROOT and not os.path.abspath(full).startswith(ROOT + os.sep):
        return None, ""
    return full, norm


def _valid_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or len(name) > 200:
        return None
    return name


def _usage():
    total = 0
    count = 0
    for dirpath, _dirs, files in os.walk(ROOT):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
                count += 1
            except OSError:
                pass
    return {"total": total, "files": count}


# ------------------------------------------------------------ handlers

def h_list(environ, start_response):
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    full, norm = _safe_join((qs.get("path") or [""])[0])
    if full is None or not os.path.isdir(full):
        return _err(start_response, 404, "文件夹不存在。")
    items = []
    for name in sorted(os.listdir(full)):
        p = os.path.join(full, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        is_dir = os.path.isdir(p)
        items.append({"name": name, "dir": is_dir,
                      "size": None if is_dir else st.st_size,
                      "mtime": st.st_mtime,
                      "path": (norm + "/" + name) if norm else name})
    items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return _json(start_response, "200 OK",
                 {"ok": True, "path": norm, "items": items, "usage": _usage()})


def h_search(environ, start_response):
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    q = ((qs.get("q") or [""])[0]).strip().lower()
    if not q:
        return _err(start_response, 400, "搜索词为空。")
    hits = []
    for dirpath, dirs, files in os.walk(ROOT):
        for name in dirs + files:
            if q in name.lower():
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                is_dir = os.path.isdir(p)
                hits.append({"name": name, "dir": is_dir,
                             "size": None if is_dir else st.st_size,
                             "mtime": st.st_mtime,
                             "path": os.path.relpath(p, ROOT)})
                if len(hits) >= SEARCH_CAP:
                    break
        if len(hits) >= SEARCH_CAP:
            break
    hits.sort(key=lambda x: (not x["dir"], x["path"]))
    return _json(start_response, "200 OK", {"ok": True, "items": hits})


def h_mkdir(environ, start_response):
    data = _read_json(environ)
    base, _ = _safe_join(data.get("path") or "")
    name = _valid_name(data.get("name") or "")
    if base is None or name is None:
        return _err(start_response, 400, "路径或名称不合法。")
    target = os.path.join(base, name)
    if os.path.exists(target):
        return _err(start_response, 400, "同名文件或文件夹已存在。")
    os.makedirs(target)
    return _json(start_response, "200 OK", {"ok": True})


def h_rename(environ, start_response):
    data = _read_json(environ)
    full, norm = _safe_join(data.get("path") or "")
    name = _valid_name(data.get("new_name") or "")
    if full is None or name is None or full == ROOT or not os.path.exists(full):
        return _err(start_response, 400, "路径或新名称不合法。")
    target = os.path.join(os.path.dirname(full), name)
    if os.path.exists(target):
        return _err(start_response, 400, "同名文件或文件夹已存在。")
    os.rename(full, target)
    return _json(start_response, "200 OK", {"ok": True})


def h_delete(environ, start_response):
    data = _read_json(environ)
    full, _ = _safe_join(data.get("path") or "")
    if full is None or full == ROOT or not os.path.exists(full):
        return _err(start_response, 400, "路径不合法。")
    if os.path.isdir(full):
        shutil.rmtree(full)
    else:
        os.remove(full)
    return _json(start_response, "200 OK", {"ok": True})


def h_upload(environ, start_response):
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    base, _ = _safe_join((qs.get("path") or [""])[0])
    name = _valid_name(unquote((qs.get("name") or [""])[0]))
    if base is None or name is None:
        return _err(start_response, 400, "路径或文件名不合法。")
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return _err(start_response, 400, "文件为空。")
    free = shutil.disk_usage(ROOT).free
    if length > free - MIN_FREE:
        return _err(start_response, 400, "服务器磁盘空间不足。")
    target = os.path.join(base, name)
    tmp = target + ".uploading"
    stream = environ["wsgi.input"]
    try:
        with open(tmp, "wb") as fh:
            remaining = length
            while remaining > 0:
                chunk = stream.read(min(262144, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            os.remove(tmp)
            return _err(start_response, 400, "上传中断，文件不完整。")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return _err(start_response, 500, "写入失败：%s" % exc)
    return _json(start_response, "200 OK", {"ok": True, "size": length})


_MIME = {".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
         ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
         ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg",
         ".pdf": "application/pdf", ".json": "application/json"}


def h_file(environ, start_response):
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    full, _ = _safe_join((qs.get("path") or [""])[0])
    if full is None or not os.path.isfile(full):
        return _err(start_response, 404, "文件不存在。")
    size = os.path.getsize(full)
    name = os.path.basename(full)
    dl = (qs.get("dl") or [""])[0] == "1"
    ext = os.path.splitext(name)[1].lower()
    ctype = "application/octet-stream" if dl else _MIME.get(ext, "application/octet-stream")
    disp = ("attachment" if dl or ctype == "application/octet-stream" else "inline") \
        + '; filename*=UTF-8\'\'' + quote(name)
    headers = [("Content-Type", ctype), ("Content-Disposition", disp),
               ("Accept-Ranges", "bytes")]
    start = 0
    end = size - 1
    status = "200 OK"
    range_hdr = environ.get("HTTP_RANGE", "")
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_hdr or "")
    if m and size > 0:
        s, e = m.group(1), m.group(2)
        if s:
            start = int(s)
            end = int(e) if e else size - 1
        elif e:
            start = max(0, size - int(e))
            end = size - 1
        end = min(end, size - 1)
        if start > end:
            start_response("416 Range Not Satisfiable",
                           [("Content-Range", "bytes */%d" % size)])
            return [b""]
        status = "206 Partial Content"
        headers.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))
    length = end - start + 1 if size > 0 else 0
    headers.append(("Content-Length", str(length)))

    def body_iter():
        with open(full, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    start_response(status, headers)
    return body_iter()


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
    if path == "/api/list" and method == "GET":
        return h_list(environ, start_response)
    if path == "/api/search" and method == "GET":
        return h_search(environ, start_response)
    if path == "/api/mkdir" and method == "POST":
        return h_mkdir(environ, start_response)
    if path == "/api/rename" and method == "POST":
        return h_rename(environ, start_response)
    if path == "/api/delete" and method == "POST":
        return h_delete(environ, start_response)
    if path == "/api/upload" and method == "POST":
        return h_upload(environ, start_response)
    if path == "/file" and method == "GET":
        return h_file(environ, start_response)
    return _err(start_response, 404, "接口不存在。")


app = application
