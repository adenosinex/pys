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
import urllib.request
import urllib.error
import http.client
from datetime import datetime

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

# ------------------------------------------------------------ AList
# Import-only credential store (values are never rendered back):
#   alist_base / alist_user / alist_pass - this app -> AList
CRED_PATH = os.path.join(DATA_DIR, "credentials.json")
DRIVE_DIR = "/\u6587\u4ef6\u4e2d\u8f6c\u7ad9"
DRIVE_TMP = os.path.join(DATA_DIR, ".drive-tmp")
os.makedirs(DRIVE_TMP, exist_ok=True)


def _load_creds():
    try:
        with open(CRED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _cred(key, env_key=None):
    if env_key and os.environ.get(env_key):
        return os.environ[env_key]
    return _load_creds().get(key) or ""


class AlistError(Exception):
    pass


class _Alist:
    def __init__(self):
        self._token = ""
        self._token_at = 0.0
        self._lock = threading.Lock()

    def base(self):
        return (_cred("alist_base", "ALIST_BASE")
                or "http://127.0.0.1:5244").rstrip("/")

    def _raw(self, method, path, payload=None, headers=None, timeout=30):
        data = None
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base() + path, data=data,
                                     method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:
                raise AlistError("AList HTTP %s" % exc.code)
        except Exception as exc:
            raise AlistError("AList unreachable: %s" % str(exc)[:100])

    def login(self, force=False):
        with self._lock:
            if (not force and self._token
                    and time.time() - self._token_at < 40 * 3600):
                return self._token
            password = _cred("alist_pass", "ALIST_PASS")
            if not password:
                raise AlistError("AList credential not configured")
            user = _cred("alist_user", "ALIST_USER") or "admin"
            out = self._raw("POST", "/api/auth/login",
                            {"username": user, "password": password})
            token = (out.get("data") or {}).get("token") or ""
            if not token:
                raise AlistError("AList login failed: %s"
                                 % (out.get("message") or "?"))
            self._token = token
            self._token_at = time.time()
            return token

    def api(self, method, path, payload=None, timeout=60):
        token = self.login()
        out = self._raw(method, path, payload,
                        headers={"Authorization": token}, timeout=timeout)
        if out.get("code") == 401:
            token = self.login(force=True)
            out = self._raw(method, path, payload,
                            headers={"Authorization": token},
                            timeout=timeout)
        if out.get("code") not in (200, 0, None):
            raise AlistError("AList %s: %s"
                             % (path, out.get("message") or out.get("code")))
        return out.get("data")

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

    def fs_get(self, path):
        return self.api("POST", "/api/fs/get", {"path": path}) or {}

    def upload_file(self, local_path, remote_path):
        token = self.login()
        size = os.path.getsize(local_path)
        parsed = urllib.parse.urlparse(self.base())
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port,
                                          timeout=900)
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
                raise AlistError("upload failed: %s"
                                 % (out.get("message") or resp.status))
            return out.get("data")
        finally:
            conn.close()


ALIST = _Alist()
DRIVE_NAME = DRIVE_DIR.lstrip("/")


def _drive_configured():
    return bool(_cred("alist_pass", "ALIST_PASS"))


def _drive_ensure():
    names = [it.get("name") for it in ALIST.fs_list("/")]
    if DRIVE_NAME not in names:
        ALIST.fs_mkdir(DRIVE_DIR)


def _mtime_of(item):
    raw = item.get("modified") or item.get("created") or ""
    try:
        return datetime.fromisoformat(
            str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def _drive_files():
    _drive_ensure()
    out = []
    for it in ALIST.fs_list(DRIVE_DIR):
        if it.get("is_dir"):
            continue
        out.append({"name": it.get("name") or "",
                    "size": it.get("size") or 0,
                    "mtime": _mtime_of(it)})
    out.sort(key=lambda x: -x["mtime"])
    return out


def handle_drive_upload(environ):
    result, status = handle_upload(environ, DRIVE_TMP)
    if not result.get("ok"):
        return result, status
    done = []
    try:
        _drive_ensure()
        existing = {it.get("name") for it in ALIST.fs_list(DRIVE_DIR)
                    if not it.get("is_dir")}
        for item in result["saved"]:
            local = os.path.join(DRIVE_TMP, item["name"])
            try:
                if item["name"] in existing:
                    ALIST.fs_remove(DRIVE_DIR, [item["name"]])
                ALIST.upload_file(local, DRIVE_DIR + "/" + item["name"])
                done.append(item)
            finally:
                try:
                    os.remove(local)
                except OSError:
                    pass
    except AlistError as exc:
        return {"ok": False,
                "error": "drive upload failed: %s" % exc}, "502 Bad Gateway"
    return {"ok": True, "saved": done}, "200 OK"


def handle_drive_download(name):
    safe = _safe_name(urllib.parse.unquote(name))
    if not safe:
        return None, None, 0
    path = DRIVE_DIR + "/" + safe
    try:
        info = ALIST.fs_get(path)
    except AlistError:
        return None, None, 0
    sign = info.get("sign") or ""
    url = ALIST.base() + "/d" + urllib.parse.quote(path)
    if sign:
        url += "?sign=" + urllib.parse.quote(sign)
    resp = None
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "transfer"}),
            timeout=60)
    except Exception:
        raw = info.get("raw_url") or ""
        if raw:
            try:
                resp = urllib.request.urlopen(
                    urllib.request.Request(
                        raw, headers={"User-Agent": "transfer"}),
                    timeout=60)
            except Exception:
                resp = None
    if resp is None:
        return None, None, 0
    return resp, safe, info.get("size") or 0



def _safe_name(name):
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", ".."):
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
            params["filename"] = urllib.parse.unquote_to_bytes(
                encoded).decode(charset, "replace")
        except Exception:
            pass
    return params


def handle_upload(environ, dest_dir=None):
    ctype = environ.get("CONTENT_TYPE", "")
    if "multipart/form-data" not in ctype or "boundary=" not in ctype:
        return {"ok": False, "error": "需要 multipart/form-data 上传"}, "400 Bad Request"
    boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
    marker = ("--" + boundary).encode()
    length = int(environ.get("CONTENT_LENGTH") or 0)
    if length <= 0:
        return {"ok": False, "error": "空请求"}, "400 Bad Request"
    dest_dir = dest_dir or FILES_DIR
    free = shutil.disk_usage(dest_dir).free
    if free - length < MIN_FREE:
        return {"ok": False, "error": "服务器剩余空间不足（需保留 2 GiB）"}, "507 Insufficient Storage"
    reader = _Reader(environ["wsgi.input"], length)
    delim = b"\r\n" + marker
    saved = []
    skipped = []
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
                skipped.append({"name": filename[:80],
                                "reason": "bad name"})
                for _ in reader.until(delim):
                    pass
                continue
            tmp_path = os.path.join(dest_dir, ".up-" + str(int(time.time() * 1000)) + "-" + safe)
            dest_path = os.path.join(dest_dir, safe)
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
    if not saved and skipped:
        return {"ok": False,
                "error": "file rejected: " + skipped[0]["name"]}, \
            "400 Bad Request"
    out = {"ok": True, "saved": saved}
    if skipped:
        out["skipped"] = skipped
    return out, "200 OK"


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
.gtitle{font-size:17px;margin:30px 0 4px}
.gsub{color:#8b949e;font-size:13px;margin-bottom:12px}
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
  <h2 class="gtitle">谷歌硬盘</h2>
  <div class="gsub">直传到谷歌硬盘里的新文件夹「文件中转站」，不占服务器磁盘；文件先经服务器中转，大文件转存要多等一会儿。</div>
  <div class="drop" id="gdrop">
    <strong>拖文件到这里，或点击选择</strong>
    <span>上传到谷歌硬盘 · 文件中转站文件夹</span>
  </div>
  <input type="file" id="gpicker" multiple hidden>
  <div class="queue" id="gqueue"></div>
  <div class="msg" id="gmsg"></div>
  <table>
    <thead><tr><th>文件</th><th>大小</th><th>时间</th><th></th><th></th></tr></thead>
    <tbody id="grows"></tbody>
  </table>
  <div class="meta" id="gmeta"></div>
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
      if(xhr.status===200&&d.ok&&d.saved&&d.saved.length){item.classList.add("done");fill.style.width="100%";st.textContent=d.skipped&&d.skipped.length?"完成（有文件被拒绝）":"完成";refresh();}
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
async function grefresh(){
  const tb=$("grows");
  try{
    const r=await fetch("api/drive/files");const d=await r.json();
    tb.innerHTML="";
    if(!d.ok){tb.innerHTML="<tr><td colspan=5 class=empty>"+(d.error||"谷歌硬盘不可用")+"</td></tr>";$("gmeta").textContent="";return;}
    if(!d.files.length){tb.innerHTML="<tr><td colspan=5 class=empty>文件夹还是空的</td></tr>";}
    for(const f of d.files){
      const tr=document.createElement("tr");
      const td1=document.createElement("td");td1.className="name";td1.textContent=f.name;tr.appendChild(td1);
      const td2=document.createElement("td");td2.className="size";td2.textContent=fmtSize(f.size);tr.appendChild(td2);
      const td3=document.createElement("td");td3.className="time";td3.textContent=fmtTime(f.mtime);tr.appendChild(td3);
      const td4=document.createElement("td");const a=document.createElement("a");a.className="dl";a.textContent="下载";a.href="gdl/"+encodeURIComponent(f.name);td4.appendChild(a);tr.appendChild(td4);const td5=document.createElement("td");const b=document.createElement("button");b.className="del";b.textContent="删除";b.onclick=async()=>{if(!b.classList.contains("armed")){b.classList.add("armed");b.textContent="确认删除？";setTimeout(()=>{b.classList.remove("armed");b.textContent="删除";},3000);return;}await fetch("api/drive/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:f.name})});grefresh();};td5.appendChild(b);tr.appendChild(td5);
      tb.appendChild(tr);
    }
    let total=0;for(const f of d.files){total+=f.size;}
    $("gmeta").textContent="谷歌硬盘「文件中转站」共 "+d.files.length+" 个文件，占用 "+fmtSize(total);
  }catch(e){tb.innerHTML="<tr><td colspan=5 class=empty>读取失败</td></tr>";}
}
function gupload(files){
  for(const file of files){
    const item=document.createElement("div");item.className="qitem";
    const label=document.createElement("span");label.textContent=file.name+"（"+fmtSize(file.size)+"）";item.appendChild(label);
    const st=document.createElement("span");st.className="st";st.textContent="准备上传";item.appendChild(st);
    const bar=document.createElement("div");bar.className="bar";const fill=document.createElement("i");bar.appendChild(fill);item.appendChild(bar);
    $("gqueue").appendChild(item);
    const fd=new FormData();fd.append("files",file);
    const xhr=new XMLHttpRequest();xhr.open("POST","api/drive/upload");
    xhr.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(100*e.loaded/e.total);fill.style.width=p+"%";st.textContent=p>=100?"转存谷歌硬盘中…":"上传 "+p+"%";}};
    xhr.onload=()=>{let d={};try{d=JSON.parse(xhr.responseText);}catch(_){}
      if(xhr.status===200&&d.ok&&d.saved&&d.saved.length){item.classList.add("done");fill.style.width="100%";st.textContent=d.skipped&&d.skipped.length?"已存入（有文件被拒绝）":"已存入谷歌硬盘";grefresh();}
      else{item.classList.add("err");st.textContent=(d.error||("失败 HTTP "+xhr.status));}};
    xhr.onerror=()=>{item.classList.add("err");st.textContent="网络错误";};
    xhr.send(fd);
  }
}
const gdrop=$("gdrop");
gdrop.onclick=()=>$("gpicker").click();
$("gpicker").onchange=e=>{gupload([...e.target.files]);e.target.value="";};
gdrop.ondragover=e=>{e.preventDefault();gdrop.classList.add("over");};
gdrop.ondragleave=()=>gdrop.classList.remove("over");
gdrop.ondrop=e=>{e.preventDefault();gdrop.classList.remove("over");gupload([...e.dataTransfer.files]);};
grefresh();
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
    if path == "/api/drive/files" and method == "GET":
        if not _drive_configured():
            return _json(start_response, "200 OK",
                         {"ok": False, "configured": False,
                          "error": "google drive not configured on server"})
        try:
            files = _drive_files()
        except AlistError as exc:
            return _json(start_response, "200 OK",
                         {"ok": False, "configured": True,
                          "error": str(exc)})
        return _json(start_response, "200 OK",
                     {"ok": True, "configured": True, "files": files})
    if path == "/api/drive/upload" and method == "POST":
        if not _drive_configured():
            return _json(start_response, "503 Service Unavailable",
                         {"ok": False, "error": "google drive not configured"})
        try:
            result, status = handle_drive_upload(environ)
        except Exception as exc:
            result, status = {"ok": False,
                              "error": "upload failed: %s" % exc}, \
                "500 Internal Server Error"
        return _json(start_response, status, result)
    if path == "/api/drive/delete" and method == "POST":
        if not _drive_configured():
            return _json(start_response, "503 Service Unavailable",
                         {"ok": False, "error": "google drive not configured"})
        data = _read_json(environ)
        safe = _safe_name(data.get("name"))
        if not safe:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "error": "bad name"})
        try:
            _drive_ensure()
            ALIST.fs_remove(DRIVE_DIR, [safe])
        except AlistError as exc:
            return _json(start_response, "502 Bad Gateway",
                         {"ok": False, "error": str(exc)})
        return _json(start_response, "200 OK", {"ok": True})
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
    if path.startswith("/gdl/") and method == "GET":
        if not _drive_configured():
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "not configured"})
        resp, safe, size = handle_drive_download(path[5:])
        if resp is None:
            return _json(start_response, "404 Not Found",
                         {"ok": False, "error": "file not found"})
        quoted = urllib.parse.quote(safe)
        headers = [("Content-Type", "application/octet-stream"),
                   ("Content-Disposition",
                    "attachment; filename*=UTF-8''" + quoted)]
        length = None
        try:
            length = resp.headers.get("Content-Length")
        except Exception:
            length = None
        if length:
            headers.append(("Content-Length", length))
        elif size:
            headers.append(("Content-Length", str(size)))
        start_response("200 OK", headers)

        def gstream():
            try:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    yield chunk
            finally:
                resp.close()
        return gstream()
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
