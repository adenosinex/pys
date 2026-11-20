"""mac-compute ASR client over the subscription-token API.

Base: https://cp.zin7.de. Auth: the X-Access-Code header carrying the
subscription token (the credentials.json key "mac_asr_token", read by
yt_text -- this module only ever receives the token value as an
argument, never reads credentials itself, and never echoes the token
or the full request URL in errors, logs, or task records).

Standard library only, http.client like yt_text's older Mac leg
(which talks to the previous endpoint with the older access code
and is left untouched). The token scope is asr/asr_batch only.

Contract (verified 2026-11-17 against the live service):
  POST /api/jobs                multipart file + type=asr -> {"id": ...}
  GET  /api/jobs/<id>           -> job status/progress/message
  GET  /api/jobs/<id>/download  -> result zip (result.txt/result.srt/...)
  POST /api/jobs/<id>/cancel    -> best-effort cancel
"""
import http.client
import io
import json
import secrets
import time
from urllib.parse import urlsplit

BASE = "https://cp.zin7.de"


class MacAsrError(Exception):
    """mac-compute failure with a user-safe message (no URL, no token)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.message = message
        self.status = status


def request(method, path, token, body=None, ctype=None, timeout=60):
    """One authenticated request; returns (status, raw bytes).

    Transport errors propagate as OSError / http.client.HTTPException
    so the caller can tell them apart from HTTP error statuses.
    """
    parts = urlsplit(BASE)
    conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443,
                                       timeout=timeout)
    headers = {"X-Access-Code": token}
    if ctype:
        headers["Content-Type"] = ctype
    if body is not None and hasattr(body, "total"):
        headers["Content-Length"] = str(body.total)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    finally:
        conn.close()
    return resp.status, data


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


def submit(audio_path, token, on_progress=None):
    """Upload one audio file as an "asr" job; returns the job id."""
    boundary = "----macasr" + secrets.token_hex(8)
    prefix = (
        ("--%s\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\nasr\r\n"
         "--%s\r\nContent-Disposition: form-data; name=\"params\"\r\n\r\n{}\r\n"
         "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
         "Content-Type: application/octet-stream\r\n\r\n")
        % (boundary, boundary, boundary, audio_path.name)).encode("utf-8")
    suffix = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    total = len(prefix) + audio_path.stat().st_size + len(suffix)
    body = _CountingBody(prefix, audio_path, suffix, total, on_progress)
    try:
        status, data = request(
            "POST", "/api/jobs", token, body=body,
            ctype="multipart/form-data; boundary=" + boundary,
            timeout=120)
    except (OSError, http.client.HTTPException):
        raise MacAsrError("上传到 Mac 失败（网络或超时）。") from None
    finally:
        body.close()
    if status == 401:
        raise MacAsrError("Mac 转写订阅 token 被拒绝。", status=401)
    if status not in (200, 201):
        raise MacAsrError("Mac 转写服务返回 HTTP %d。" % status, status=status)
    try:
        payload = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        raise MacAsrError("Mac 转写服务返回了无效响应。", status=status) from None
    job_id = None
    if isinstance(payload, dict):
        job_id = payload.get("id") or payload.get("job_id")
        if not job_id and isinstance(payload.get("job"), dict):
            job_id = payload["job"].get("id")
    if not job_id:
        raise MacAsrError("Mac 转写服务返回了无效响应。", status=status)
    return str(job_id)


def get_job(token, job_id):
    """Fetch one job record; transport errors raise MacAsrError
    with status None so the caller can retry them."""
    try:
        status, data = request("GET", "/api/jobs/" + job_id, token, timeout=60)
    except (OSError, http.client.HTTPException):
        raise MacAsrError("无法连接 Mac 转写服务。") from None
    if status == 401:
        raise MacAsrError("Mac 转写订阅 token 被拒绝。", status=401)
    if status != 200:
        raise MacAsrError("查询 Mac 任务返回 HTTP %d。" % status, status=status)
    try:
        job = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        raise MacAsrError("Mac 转写服务返回了无效响应。", status=status) from None
    return job if isinstance(job, dict) else {}


def download(token, job_id):
    """Fetch the finished job's result zip; returns raw bytes."""
    try:
        status, data = request("GET", "/api/jobs/" + job_id + "/download",
                               token, timeout=300)
    except (OSError, http.client.HTTPException):
        raise MacAsrError("取回转写结果失败（网络或超时）。") from None
    if status == 401:
        raise MacAsrError("Mac 转写订阅 token 被拒绝。", status=401)
    if status != 200:
        raise MacAsrError("取回转写结果返回 HTTP %d。" % status, status=status)
    return data


def cancel(token, job_id):
    """Best-effort cancel; every failure is swallowed."""
    try:
        request("POST", "/api/jobs/" + job_id + "/cancel", token,
                body=b"", timeout=30)
    except Exception:
        pass
