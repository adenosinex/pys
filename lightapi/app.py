#!/usr/bin/env python3
"""lightapi: the shared data gateway for light apps (HTML pages + a few
data APIs, backed by PocketBase).

Design (settled 2026-11-17):
- Identity verification lives ONLY in the panel layer. This app does no
  auth of its own: it sits behind the panel /web/ proxy gate, and the
  browser's panel session / direct code is the auth. Loopback is trusted.
- PocketBase has no scoped API keys and a superuser token bypasses all
  rules, so the browser must never see one. The gateway holds the
  superuser credential server-side (0600 file in its data dir) and is
  the ONLY process that talks to PocketBase on the apps' behalf.
- Per-app isolation is enforced HERE, by an allowlist: an app key maps
  to the collections it may touch, whether it may write, an optional
  fixed filter ANDed with every client filter, and an optional
  server-stamped field on create. Unknown app/collection -> 404.
  Only record CRUD (+ query params) is forwarded; /api/sql, admin and
  auth endpoints are never reachable through this gateway.
- Blob mode: one app key may expose a single-document KV endpoint
  (GET returns the bare payload, POST {payload} upserts it), for apps
  whose cloud store is one backed-up blob (Prompt Vault).

Config files in DATA_DIR (never in code, never in chat):
- gateway.secret.json  {email, password} 0600, the dedicated PB superuser
- allowlist.json       {app: {collections: {...}, blob: {...}}}

Fail-closed everywhere: missing credential, unreachable PB, unknown
app/collection, oversized body, bad method -> an error, never a guess.
No unbounded reads: every upstream call has a timeout; bodies are capped.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
PB_BASE = os.environ.get("PB_BASE", "http://127.0.0.1:8090")
MAX_BODY = 2 * 1024 * 1024
UPSTREAM_TIMEOUT = 8
TZ = ZoneInfo("Asia/Shanghai")

# Query params forwarded to PocketBase list endpoints (nothing else).
ALLOWED_PARAMS = ("filter", "sort", "page", "perPage", "fields", "skipTotal")


def _data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(BASE, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(BASE, ".runtime")


DATA_DIR = _data_dir()
SECRET_PATH = os.path.join(DATA_DIR, "gateway.secret.json")
ALLOWLIST_PATH = os.path.join(DATA_DIR, "allowlist.json")

_token = {"value": None, "got_at": 0.0}
_allowlist = {"data": None, "mtime": 0.0}


def _load_secret():
    try:
        with open(SECRET_PATH, "r", encoding="utf-8") as fh:
            cred = json.load(fh)
        email = cred.get("email")
        password = cred.get("password")
        if email and password:
            return email, password
    except Exception:
        pass
    return None, None


def _load_allowlist():
    try:
        mtime = os.path.getmtime(ALLOWLIST_PATH)
    except OSError:
        return {}
    if _allowlist["data"] is not None and mtime == _allowlist["mtime"]:
        return _allowlist["data"]
    try:
        with open(ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    _allowlist["data"] = data if isinstance(data, dict) else {}
    _allowlist["mtime"] = mtime
    return _allowlist["data"]


def _pb_request(method, path, body=None, auth=True, _retried=False):
    """One PocketBase call. Returns (status, parsed_json_or_None)."""
    url = PB_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if auth:
        token = _pb_token()
        if not token:
            return 503, {"error": "gateway credential unavailable"}
        req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            raw = resp.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                return 502, {"error": "upstream response too large"}
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 401 and auth and not _retried:
            _token["value"] = None
            return _pb_request(method, path, body, auth=True, _retried=True)
        try:
            payload = json.loads(e.read(MAX_BODY).decode("utf-8"))
        except Exception:
            payload = {"error": "upstream error"}
        return e.code, payload
    except Exception:
        return 502, {"error": "upstream unreachable"}


def _pb_token():
    if _token["value"]:
        return _token["value"]
    email, password = _load_secret()
    if not email:
        return None
    status, payload = _pb_request(
        "POST", "/api/collections/_superusers/auth-with-password",
        {"identity": email, "password": password}, auth=False)
    if status == 200 and isinstance(payload, dict) and payload.get("token"):
        _token["value"] = payload["token"]
        _token["got_at"] = time.time()
        return _token["value"]
    return None


def _pb_health():
    try:
        req = urllib.request.Request(PB_BASE + "/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


# ------------------------------------------------------------------ wsgi

def _send(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def _read_body(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return None, 400
    if length < 0 or length > MAX_BODY:
        return None, 413
    raw = environ["wsgi.input"].read(length) if length else b""
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, 400
    if not isinstance(parsed, (dict, list)):
        return None, 400
    return parsed, None


def _merge_filter(fixed, client):
    fixed = (fixed or "").strip()
    client = (client or "").strip()
    if fixed and client:
        return "(%s) && (%s)" % (fixed, client)
    return fixed or client


def _handle_records(environ, start_response, method, app, coll, rec_id, spec):
    if method not in ("GET", "POST", "PATCH", "DELETE"):
        return _send(start_response, "405 Method Not Allowed",
                     {"error": "method not allowed"})
    if method != "GET" and not spec.get("write"):
        return _send(start_response, "403 Forbidden",
                     {"error": "collection is read-only"})
    query = urllib.parse.parse_qs(
        environ.get("QUERY_STRING", ""), keep_blank_values=True)
    params = {}
    for key in ALLOWED_PARAMS:
        if key in query and query[key]:
            params[key] = query[key][0]
    if method == "GET":
        merged = _merge_filter(spec.get("fixedFilter"), params.get("filter"))
        if merged:
            params["filter"] = merged
        elif "filter" in params:
            del params["filter"]
    body = None
    if method in ("POST", "PATCH"):
        body, err = _read_body(environ)
        if err:
            return _send(start_response, "%d Error" % err,
                         {"error": "invalid body"})
        if not isinstance(body, dict):
            return _send(start_response, "400 Bad Request",
                         {"error": "object body required"})
        stamp = spec.get("stampField")
        if method == "POST" and stamp and stamp not in body:
            body[stamp] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    path = "/api/collections/%s/records" % urllib.parse.quote(coll, safe="")
    if rec_id:
        path += "/%s" % urllib.parse.quote(rec_id, safe="")
    if params:
        path += "?" + urllib.parse.urlencode(params)
    status, payload = _pb_request(method, path, body)
    return _send(start_response, "%d Gateway" % status, payload)


def _handle_blob(environ, start_response, method, spec):
    coll = spec["collection"]
    key_field = spec.get("keyField", "key")
    key_value = spec.get("keyValue", "backup")
    payload_field = spec.get("payloadField", "payload")
    filt = "%s = %s" % (key_field, json.dumps(key_value))
    if method == "GET":
        path = ("/api/collections/%s/records?%s"
                % (urllib.parse.quote(coll, safe=""),
                   urllib.parse.urlencode(
                       {"filter": filt, "perPage": "1", "sort": "created"})))
        status, payload = _pb_request("GET", path)
        if status != 200:
            return _send(start_response, "%d Gateway" % status, payload)
        items = payload.get("items") or []
        if not items:
            return _send(start_response, "404 Not Found",
                         {"error": "no cloud data"})
        return _send(start_response, "200 OK", items[0].get(payload_field))
    if method == "POST":
        body, err = _read_body(environ)
        if err:
            return _send(start_response, "%d Error" % err,
                         {"error": "invalid body"})
        payload = body.get("payload") if isinstance(body, dict) else body
        path = ("/api/collections/%s/records?%s"
                % (urllib.parse.quote(coll, safe=""),
                   urllib.parse.urlencode(
                       {"filter": filt, "perPage": "1", "sort": "created",
                        "fields": "id"})))
        status, found = _pb_request("GET", path)
        if status != 200:
            return _send(start_response, "%d Gateway" % status, found)
        items = found.get("items") or []
        record = {key_field: key_value, payload_field: payload}
        if items:
            rid = items[0]["id"]
            status, saved = _pb_request(
                "PATCH",
                "/api/collections/%s/records/%s"
                % (urllib.parse.quote(coll, safe=""),
                   urllib.parse.quote(rid, safe="")),
                record)
        else:
            status, saved = _pb_request(
                "POST",
                "/api/collections/%s/records"
                % urllib.parse.quote(coll, safe=""),
                record)
        if status in (200, 201):
            return _send(start_response, "200 OK", {"status": "success"})
        return _send(start_response, "%d Gateway" % status, saved)
    return _send(start_response, "405 Method Not Allowed",
                 {"error": "method not allowed"})


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = (environ.get("PATH_INFO", "/") or "/").strip("/") or "/"
    started = time.time()
    try:
        if path == "health" and method == "GET":
            return _send(start_response, "200 OK",
                         {"ok": True, "pb_up": _pb_health()})
        parts = path.split("/")
        allowlist = _load_allowlist()
        app = allowlist.get(parts[0]) if parts else None
        if not app:
            return _send(start_response, "404 Not Found",
                         {"error": "unknown app"})
        if len(parts) >= 2 and parts[1] == "blob" and len(parts) == 2:
            spec = app.get("blob")
            if not spec or not spec.get("collection"):
                return _send(start_response, "404 Not Found",
                             {"error": "no blob endpoint"})
            return _handle_blob(environ, start_response, method, spec)
        if len(parts) >= 3 and parts[1] == "records":
            coll = urllib.parse.unquote(parts[2])
            spec = (app.get("collections") or {}).get(coll)
            if not spec:
                return _send(start_response, "404 Not Found",
                             {"error": "unknown collection"})
            rec_id = urllib.parse.unquote(parts[3]) if len(parts) > 3 else None
            if len(parts) > 4:
                return _send(start_response, "404 Not Found",
                             {"error": "bad path"})
            return _handle_records(environ, start_response, method, app,
                                   coll, rec_id, spec)
        return _send(start_response, "404 Not Found", {"error": "bad path"})
    finally:
        elapsed = (time.time() - started) * 1000
        print("lightapi %s /%s %.0fms" % (method, path, elapsed),
              flush=True)


app = application

if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    port = int(os.environ.get("PORT", "0") or 0)
    if not port:
        raise SystemExit("PORT env var is required")
    httpd = make_server("127.0.0.1", port, application)
    print("lightapi listening on 127.0.0.1:%d" % port, flush=True)
    httpd.serve_forever()
