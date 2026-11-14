"""Shared WSGI helpers for the records hub: JSON responses, body and
multipart parsing, and static/media file serving with Range support."""
import json
import mimetypes
import os
import re
from email.utils import formatdate

STATUS = {200: "200 OK", 201: "201 Created", 204: "204 No Content",
          206: "206 Partial Content", 304: "304 Not Modified",
          400: "400 Bad Request",
          401: "401 Unauthorized", 404: "404 Not Found",
          409: "409 Conflict", 416: "416 Range Not Satisfiable",
          500: "500 Internal Server Error",
          503: "503 Service Unavailable"}


def json_response(start_response, payload, code=200, extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [("Content-Type", "application/json; charset=utf-8"),
               ("Content-Length", str(len(body)))]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(STATUS[code], headers)
    return [body]


def read_body(environ, limit=134217728):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > limit:
        return b""
    try:
        return environ["wsgi.input"].read(length)
    except Exception:
        return b""


def read_json(environ):
    raw = read_body(environ, limit=16777216)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def parse_multipart(environ):
    """Minimal multipart/form-data parser.
    Returns (fields, files): fields maps name -> str; files maps
    name -> (filename, bytes)."""
    ctype = environ.get("CONTENT_TYPE", "")
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return {}, {}
    boundary = m.group(1).strip().strip('"').encode("utf-8", "replace")
    body = read_body(environ)
    fields, files = {}, {}
    if not body:
        return fields, files
    delimiter = b"--" + boundary
    for part in body.split(delimiter):
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"--"):
            part = part[:-2]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        head, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        head_text = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]*)"', head_text)
        if not name_m:
            continue
        name = name_m.group(1)
        file_m = re.search(r'filename="([^"]*)"', head_text)
        if file_m is not None:
            files[name] = (file_m.group(1), content)
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


def serve_file(start_response, path, environ, download_name=None, immutable=False):
    if not os.path.isfile(path):
        return json_response(start_response, {"error": "not found"}, 404)
    size = os.path.getsize(path)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    headers = [("Content-Type", ctype), ("Accept-Ranges", "bytes")]
    if download_name:
        headers.append(("Content-Disposition", "attachment;filename=" + download_name))
    if immutable and not download_name:
        # 版本号写在文件名里的静态资源：内容永不变化，浏览器可缓存
        # 一年不再请求；带 ETag，显式刷新时走 304。
        st = os.stat(path)
        etag = '"%x-%x"' % (int(st.st_mtime), size)
        headers.append(("ETag", etag))
        headers.append(("Last-Modified", formatdate(st.st_mtime, usegmt=True)))
        headers.append(("Cache-Control", "public, max-age=31536000, immutable"))
        if environ.get("HTTP_IF_NONE_MATCH") == etag:
            start_response(STATUS[304], headers)
            return [b""]
    range_header = environ.get("HTTP_RANGE", "")
    start, end = 0, size - 1
    code = 200
    m = re.match(r"bytes=(\d*)-(\d*)$", range_header or "")
    if m and size > 0:
        if m.group(1):
            start = int(m.group(1))
        if m.group(2):
            end = min(int(m.group(2)), size - 1)
        if not m.group(1) and m.group(2):
            start = max(0, size - int(m.group(2)))
            end = size - 1
        if start > end or start >= size:
            headers.append(("Content-Range", "bytes */%d" % size))
            start_response(STATUS[416], headers)
            return [b""]
        code = 206
        headers.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))
    length = end - start + 1
    headers.append(("Content-Length", str(length)))
    start_response(STATUS[code], headers)

    def stream():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return stream()


def csv_response(start_response, text, filename):
    body = text.encode("utf-8")
    headers = [("Content-Type", "text/csv; charset=utf-8"),
               ("Content-Length", str(len(body))),
               ("Content-Disposition", "attachment;filename=" + filename)]
    start_response("200 OK", headers)
    return [body]
