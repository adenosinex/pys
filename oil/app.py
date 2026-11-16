"""Oil price backend: serves the legacy /api2/oil/domestic/latest shape
from the shared PocketBase (loopback :8090, collection oil_prices).

The province default lives in the project data dir config.json
({"province": "<name>"}); a ?province= query param overrides it per
request. No record, no config, or any upstream error -> {code: 1} and
the page falls back to its cache/defaults, exactly as before.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

PB_BASE = "http://127.0.0.1:8090"
TIMEOUT = 5


def _data_dir():
    d = os.environ.get("DATA_DIR")
    if d:
        return d
    here = os.path.dirname(os.path.abspath(__file__))
    link = os.path.join(here, "data")
    if os.path.islink(link) or os.path.isdir(link):
        return link
    return os.path.join(here, ".runtime")


def _config():
    try:
        with open(os.path.join(_data_dir(), "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _latest(province):
    filt = 'province = "%s"' % province.replace('"', "")
    qs = urllib.parse.urlencode(
        {"filter": filt, "sort": "-effective", "perPage": 1}
    )
    req = urllib.request.Request(PB_BASE + "/api/collections/oil_prices/records?" + qs)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        payload = json.load(r)
    items = payload.get("items") or []
    return items[0] if items else None


def _json(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(
        status,
        [("Content-Type", "application/json; charset=utf-8"),
         ("Content-Length", str(len(body)))],
    )
    return [body]


def app(environ, start_response):
    path = environ.get("PATH_INFO", "").rstrip("/") or "/"
    qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    if path == "/api2/oil/domestic/latest":
        province = (qs.get("province", [""])[0] or "").strip()
        if not province:
            province = str(_config().get("province", "") or "").strip()
        if not province:
            return _json(start_response, "200 OK",
                         {"code": 1, "msg": "province not configured"})
        try:
            rec = _latest(province)
        except Exception:
            return _json(start_response, "200 OK",
                         {"code": 1, "msg": "upstream error"})
        if not rec:
            return _json(start_response, "200 OK",
                         {"code": 1, "msg": "no data"})
        eff = str(rec.get("effective", "") or "")
        return _json(start_response, "200 OK", {
            "code": 0,
            "data": {
                "gasoline_92": rec.get("p92") or 0,
                "gasoline_95": rec.get("p95") or 0,
                "gasoline_98": rec.get("p98") or 0,
                "city": rec.get("province", ""),
                "date": eff[:10],
            },
        })
    if path in ("/", "/api/status"):
        cfg = _config()
        province = str(cfg.get("province", "") or "")
        latest = None
        if province:
            try:
                rec = _latest(province)
                if rec:
                    latest = {"province": rec.get("province"),
                              "effective": rec.get("effective"),
                              "p92": rec.get("p92")}
            except Exception:
                latest = None
        return _json(start_response, "200 OK", {
            "ok": True,
            "province_configured": bool(province),
            "province": province,
            "latest": latest,
        })
    return _json(start_response, "404 Not Found",
                 {"code": 1, "msg": "not found"})


application = app
