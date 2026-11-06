# -*- coding: utf-8 -*-
"""文件中转站: a dead-simple upload/download drop for exchanging files
with this server. Files live in the platform data dir (<data>/files).
No project-level auth (the panel /web gate owns identity, per the
platform rule). Stdlib only.

Routes:
  GET  /                page
  GET  /api/session     {authenticated: true}
  GET  /api/files       {files: [{name, size, mtime}], total, free}
  POST /api/upload      multipart (any file parts), streamed to disk
  GET  /dl/<name>       attachment download, streamed
  POST /api/delete      {name}
"""
import json
import os
import shutil
import threading
import time
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(BASE_DIR, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(BASE_DIR, ".runtime")


DATA_DIR = _data_dir()
FILES_DIR = os.path.join(DATA_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)
MIN_FREE = 2 * 1024 ** 3
_lock = threading.Lock()


def _safe_name(name):
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", "..") or name.startswith("."):
        return None
    if "/" in name or "\x00" in name:
        return None
    return name[:200]


def _list_files():
    out = []
    try:
        names = os.listdir(FILES_DIR)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(FILES_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if os.path.isfile(path):
            out.append({"name": name, "size": st.st_size,
                        "mtime": st.st_mtime})
    out.sort(key=lambda x: -x["mtime"])
    return out


def _json(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


class _Reader:
    """Buffered reader over wsgi.input for streaming multipart parse."""

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
        """Yield bytes up to marker (marker excluded); consumes marker."""
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
    parts = value.split(";")
    for part in parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            params[k.strip().lower()] = v.strip().strip('"')
    return params


def handle_upload(environ):
    ctype = environ.get("CONTENT_TYPE", "")
    if "multipart/form-data" not in ctype or "boundary=" not in ctype:
        return {"ok": False, "error": "需要 multipart/form-data 上传"}, "400 Bad Request"
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
    marker = ("--" + boundary).encode()
    length = int(environ.get("CONTENT_LENGTH") or 0)
    if length <= 0:
        return {"ok": False, "error": "空请求"}, "400 Bad Request"
    free = shutil.disk_usage(FILES_DIR).free
    if free - length < MIN_FREE:
        return {"ok": False, "error": "服务器剩余空间不足（需保留 2 GiB）"}, "507 Insufficient Storage"
    reader = _Reader(environ["wsgi.input"], length)
    delim = b"\r\n" + marker
    saved = []
    # skip preamble up to first boundary
    for _ in reader.until(marker):
        pass
    while True:
        # after a boundary: either -- (end) or CRLF then headers
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
        if filename:
            safe = _safe_name(filename)
            if not safe:
                for _ in reader.until(delim):
                    pass
                continue
            tmp_path = os.path.join(FILES_DIR, ".up-" + str(int(time.time() * 1000)) + "-" + safe)
            dest_path = os.path.join(FILES_DIR, safe)
            size = 0
            with _lock:
                with open(tmp_path, "wb") as fh:
                    for chunk in reader.until(delim):
                        fh.write(chunk)
                        size += len(chunk)
                os.replace(tmp_path, dest_path)
            saved.append({"name": safe, "size": size})
        else:
            for _ in reader.until(delim):
                pass
        # the delim consumed includes the leading CRLF + marker; loop
        # continues at the bytes right after the marker
        if reader.left <= 0 and not reader.buf:
            break
    return {"ok": True, "saved": saved}, "200 OK"


def handle_download(name):
    safe = _safe_name(urllib.parse.unquote(name))
    if not safe:
        return None, None
    path = os.path.join(FILES_DIR, safe)
    if not os.path.isfile(path):
        return None, None
    return path, safe


def _read_json(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    raw = environ["wsgi.input"].read(length) if length > 0 else b""
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件中转站</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0d1117;color:#e6edf3}
.wrap{max-width:860px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:4px 0 2px}
.sub{color:#8b949e;font-size:13px;margin-bottom:16px}
.drop{border:1.5px dashed #30363d;border-radius:14px;padding:34px 16px;text-align:center;cursor:pointer;transition:border-color .15s,background .15s;background:#161b22}
.drop.over{border-color:#58a6ff;background:#1c2333}
.drop strong{display:block;font-size:17px;margin-bottom:6px}
.drop span{color:#8b949e;font-size:13px}
.queue{margin:14px 0;display:flex;flex-direction:column;gap:8px}
.qitem{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:10px 12px;font-size:13px}
.qitem .bar{height:6px;background:#21262d;border-radius:3px;margin-top:8px;overflow:hidden}
.qitem .bar i{display:block;height:100%;background:#58a6ff;width:0}
.qitem.done .bar i{background:#3fb950}
.qitem.err .bar i{background:#f85149}
.qitem .st{color:#8b949e;margin-left:8px}
table{width:100%;border-collapse:collapse;margin-top:18px;font-size:14px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid #21262d;vertical-align:middle}
th{color:#8b949e;font-weight:600;font-size:12.5px}
td.size,td.time{color:#8b949e;white-space:nowrap;font-size:13px}
td.name{word-break:break-all}
a.dl{color:#58a6ff;text-decoration:none;font-weight:600;white-space:nowrap}
.del{background:none;border:none;color:#6e7681;font-size:12.5px;cursor:pointer;padding:4px 2px;opacity:.75}
.del.armed{color:#f85149;opacity:1;font-weight:700}
.meta{color:#8b949e;font-size:12.5px;margin-top:10px}
.empty{color:#6e7681;text-align:center;padding:26px 0}
.msg{font-size:13px;margin-top:10px;min-height:18px}
.msg.err{color:#f85149}
.msg.ok{color:#3fb950}
</style>
</head>
<body>
<div class="wrap">
  <h1>文件中转站</h1>
  <div class="sub">和这台服务器交换文件：拖进来上传，点下载取走。同名文件覆盖，删完不恢复。</div>
  <div class="drop" id="drop">
    <strong>拖文件到这里，或点击选择</strong>
    <span>可多选；单个文件别超过 500 MB</span>
  </div>
  <input type="file" id="picker" multiple hidden>
  <div class="queue" id="queue"></div>
  <div class="msg" id="msg"></div>
  <table>
    <thead><tr><th>文件</th><th>大小</th><th>时间</th><th></th><th></th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="meta" id="meta"></div>
</div>
<script>
"use strict";
const $=id=>document.getElementById(id);
function fmtSize(n){if(n<1024)return n+" B";const u=["KB","MB","GB"];let v=n;for(const s of u){v/=1024;if(v<1024)return v.toFixed(v<10?1:0)+" "+s;}return v.toFixed(1)+" TB";}
function fmtTime(ts){const d=new Date(ts*1000);const p=x=>String(x).padStart(2,"0");return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes());}
async function refresh(){
  const r=await fetch("api/files");const d=await r.json();
  const tb=$("rows");tb.innerHTML="";
  if(!d.files.length){tb.innerHTML="<tr><td colspan=5 class=empty>还没有文件</td></tr>";}
  for(const f of d.files){
    const tr=document.createElement("tr");
    const td1=document.createElement("td");td1.className="name";td1.textContent=f.name;tr.appendChild(td1);
    const td2=document.createElement("td");td2.className="size";td2.textContent=fmtSize(f.size);tr.appendChild(td2);
    const td3=document.createElement("td");td3.className="time";td3.textContent=fmtTime(f.mtime);tr.appendChild(td3);
    const td4=document.createElement("td");const a=document.createElement("a");a.className="dl";a.textContent="下载";a.href="dl/"+encodeURIComponent(f.name);td4.appendChild(a);tr.appendChild(td4);
    const td5=document.createElement("td");const b=document.createElement("button");b.className="del";b.textContent="删除";
    b.onclick=async()=>{
      if(!b.classList.contains("armed")){b.classList.add("armed");b.textContent="确认删除？";setTimeout(()=>{b.classList.remove("armed");b.textContent="删除";},3000);return;}
      await fetch("api/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:f.name})});
      refresh();
    };
    td5.appendChild(b);tr.appendChild(td5);tb.appendChild(tr);
  }
  $("meta").textContent="共 "+d.files.length+" 个文件，占用 "+fmtSize(d.total)+"；服务器剩余 "+fmtSize(d.free);
}
function upload(files){
  for(const file of files){
    const item=document.createElement("div");item.className="qitem";
    const label=document.createElement("span");label.textContent=file.name+"（"+fmtSize(file.size)+"）";item.appendChild(label);
    const st=document.createElement("span");st.className="st";st.textContent="准备上传";item.appendChild(st);
    const bar=document.createElement("div");bar.className="bar";const fill=document.createElement("i");bar.appendChild(fill);item.appendChild(bar);
    $("queue").appendChild(item);
    const fd=new FormData();fd.append("files",file);
    const xhr=new XMLHttpRequest();xhr.open("POST","api/upload");
    xhr.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(100*e.loaded/e.total);fill.style.width=p+"%";st.textContent="上传 "+p+"%";}};
    xhr.onload=()=>{let d={};try{d=JSON.parse(xhr.responseText);}catch(_){}
      if(xhr.status===200&&d.ok){item.classList.add("done");fill.style.width="100%";st.textContent="完成";refresh();}
      else{item.classList.add("err");st.textContent=(d.error||("失败 HTTP "+xhr.status));}};
    xhr.onerror=()=>{item.classList.add("err");st.textContent="网络错误";};
    xhr.send(fd);
  }
}
const drop=$("drop");
drop.onclick=()=>$("picker").click();
$("picker").onchange=e=>{upload([...e.target.files]);e.target.value="";};
drop.ondragover=e=>{e.preventDefault();drop.classList.add("over");};
drop.ondragleave=()=>drop.classList.remove("over");
drop.ondrop=e=>{e.preventDefault();drop.classList.remove("over");upload([...e.dataTransfer.files]);};
refresh();
</script>
</body>
</html>
"""


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"
    if path == "/" and method == "GET":
        body = PAGE.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]
    if path == "/api/session":
        return _json(start_response, "200 OK", {"authenticated": True})
    if path == "/api/files" and method == "GET":
        files = _list_files()
        total = sum(f["size"] for f in files)
        free = shutil.disk_usage(FILES_DIR).free
        return _json(start_response, "200 OK",
                     {"files": files, "total": total, "free": free})
    if path == "/api/upload" and method == "POST":
        try:
            result, status = handle_upload(environ)
        except Exception as exc:
            result, status = {"ok": False, "error": "上传失败：%s" % exc}, "500 Internal Server Error"
        return _json(start_response, status, result)
    if path == "/api/delete" and method == "POST":
        data = _read_json(environ)
        safe = _safe_name(data.get("name"))
        if not safe:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "error": "文件名无效"})
        try:
            os.remove(os.path.join(FILES_DIR, safe))
            deleted = True
        except OSError:
            deleted = False
        return _json(start_response, "200 OK", {"ok": True, "deleted": deleted})
    if path.startswith("/dl/") and method == "GET":
        found, safe = handle_download(path[4:])
        if not found:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "文件不存在"})
        size = os.path.getsize(found)
        quoted = urllib.parse.quote(safe)
        start_response("200 OK", [
            ("Content-Type", "application/octet-stream"),
            ("Content-Length", str(size)),
            ("Content-Disposition",
             "attachment; filename*=UTF-8''" + quoted)])

        def stream():
            with open(found, "rb") as fh:
                while True:
                    chunk = fh.read(262144)
                    if not chunk:
                        break
                    yield chunk
        return stream()
    return _json(start_response, "404 Not Found", {"ok": False, "error": "not found"})
