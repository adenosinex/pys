"""DoH 解析 — dohrelay project page (debi, App Hub).

Architecture (user rulings 2026-11-11): Shock is the ONLY
user-facing resolver. It serves DoH at
https://shoc.zin7.de/<SEG>/dns-query (the random path segment is
the credential) and DoT at dot.zin7.de:853 (Android Private DNS
accepts hostnames only, so the phone entry is the DoT hostname;
there is no filing regime on Shock). The Guangzhou domestic
machine is NOT a service: it runs a latency probe that measures
the curated candidates (tcp:443 + TLS handshake from inside
China) and pushes results to Shock, where the gate ranks the
truth answers by them before serving user devices.

This project is the control surface only:
  * shows the two entry addresses + live Shock health (via the
    ctl channel) + probe freshness (via the gate-status endpoint),
  * offers the offline probe package for download -- its config
    carries the CURRENT segment + probe key and is materialized
    HERE, at download time, from the live values, so a downloaded
    package is always in step and no credentialed package ever
    sits in a shared directory,
  * resets the DoH credential (ctl rotate on Shock; the ctl also
    syncs the gate's own config copy and restarts it -- a rotate
    that skipped that once left the endpoint 404ing).

The Shock channel uses a dedicated per-service keypair generated
on debi for this project only (private half in this data dir's
ssh/, 0600; public half in shoc's authorized_keys RESTRICTED to
the forced command /usr/local/bin/dohrelay-ctl, subcommands
status/segment/rotate). It is not the operator's key and opens
nothing else. Shock's real SSH host keys were verified against
the box's /etc/ssh on 2026-11-11 (the fingerprint that looked
"wrong" in the previous round was the box's TLS certificate
fingerprint, a different object).

This app contains no identity verification: the App Hub platform
gate in front of /web/dohrelay/ performs it centrally, and direct
access on the local port is trusted.
"""
import io
import json
import os
import subprocess
import tarfile
import threading
import time
import urllib.request

from page import PAGE_HTML

BASE = os.path.dirname(os.path.abspath(__file__))


def _platform_data_dir(base):
    """DATA_DIR env -> ./data symlink -> ./.runtime (platform rule)."""
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(base, "data")
    if os.path.isdir(link):
        return os.path.realpath(link)
    runtime = os.path.join(base, ".runtime")
    os.makedirs(runtime, exist_ok=True)
    return runtime


DATA_DIR = _platform_data_dir(BASE)
SSH_DIR = os.path.join(DATA_DIR, "ssh")
SSH_KEY = os.path.join(SSH_DIR, "id_ed25519")
SSH_KNOWN = os.path.join(SSH_DIR, "known_hosts")
SSH_TARGET = "root@208.123.119.252"
DOH_HOST = "shoc.zin7.de"
DOT_HOST = "dot.zin7.de"
PKG_TEMPLATE = os.path.join(DATA_DIR, "probe-pkg-template.tar.gz")

_lock = threading.Lock()


def _ctl(cmd):
    args = ["ssh", "-i", SSH_KEY, "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
            "-o", "UserKnownHostsFile=" + SSH_KNOWN,
            "-o", "StrictHostKeyChecking=yes", SSH_TARGET, cmd]
    r = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError("ctl %s: %s" % (cmd, (r.stderr or "")[:160]))
    return json.loads(r.stdout)


def _segment():
    return _ctl("segment")["segment"]


def _probe_key_probe():
    """Fetch probe freshness through the public gate-status URL
    (segment-authenticated). Returns dict or None on failure."""
    seg = _segment()
    url = "https://%s/%s/gate-status" % (DOH_HOST, seg)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _public_state(message=None):
    out = {"ok": True, "dot_host": DOT_HOST}
    try:
        seg = _segment()
        out["doh_url"] = "https://%s/%s/dns-query" % (DOH_HOST, seg)
    except Exception as exc:
        out["doh_url"] = ""
        out["health"] = {"ok": False, "error": "通道异常: %s" % exc}
        out["probe"] = {}
        out["gate_stats"] = {}
        out["pkg_note"] = ""
        if message:
            out["message"] = message
        return out
    try:
        out["health"] = _ctl("status")
    except Exception as exc:
        out["health"] = {"ok": False, "error": str(exc)[:120]}
    gs = _probe_key_probe()
    if gs:
        out["probe"] = {"age_sec": gs.get("probe_age_sec"),
                        "stale": gs.get("probe_stale"),
                        "domains": gs.get("probe_domains") or 0}
        out["gate_stats"] = gs.get("stats") or {}
    else:
        out["probe"] = {}
        out["gate_stats"] = {}
    out["pkg_note"] = ("包内 config 已含当前凭据，下载后直接安装；"
                       "包 sha256 见部署文档。")
    if message:
        out["message"] = message
    return out


def _build_package():
    """Materialize the probe package tarball with a config.json
    carrying the CURRENT segment + probe key.

    The probe key never transits this app: the template package
    in the data dir already carries the key in its config (it is
    static per Shock deployment); only the segment inside
    ingest_base is rewritten here, from the live ctl value, so
    packages downloaded after a rotate stay valid for the probe
    push channel. Raises on any inconsistency."""
    seg = _segment()
    src = tarfile.open(PKG_TEMPLATE, "r:gz")
    members = src.getmembers()
    cfg_member = None
    for m in members:
        if m.name.endswith("config.json"):
            cfg_member = m
    if cfg_member is None:
        raise RuntimeError("template package has no config.json")
    cfg = json.loads(src.extractfile(cfg_member).read().decode())
    base = cfg.get("ingest_base") or ""
    host_part = base.split("/")[0:3]
    cfg["ingest_base"] = "/".join(host_part) + "/" + seg
    cfg_bytes = json.dumps(cfg, ensure_ascii=False, indent=1).encode()
    buf = io.BytesIO()
    dst = tarfile.open(fileobj=buf, mode="w:gz")
    for m in members:
        if m is cfg_member:
            nm = tarfile.TarInfo(m.name)
            nm.size = len(cfg_bytes)
            nm.mode = 0o600
            nm.mtime = int(time.time())
            dst.addfile(nm, io.BytesIO(cfg_bytes))
        else:
            fobj = src.extractfile(m) if m.isfile() else None
            dst.addfile(m, fobj)
    dst.close()
    src.close()
    return buf.getvalue()


def _json(start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO") or "/"

    if path == "/" and method == "GET":
        body = PAGE_HTML.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/api/session" and method == "GET":
        return _json(start_response, "200 OK",
                     {"ok": True, "authenticated": True, "via": "platform",
                      "username": "platform"})

    if path == "/api/state" and method == "GET":
        with _lock:
            return _json(start_response, "200 OK", _public_state())

    if path == "/api/rotate" and method == "POST":
        with _lock:
            try:
                _ctl("rotate")
            except Exception as exc:
                return _json(start_response, "200 OK",
                             {"ok": False, "message": "重置失败：%s" % exc})
            return _json(start_response, "200 OK",
                         _public_state("凭据已重置，新地址如下；"
                                       "旧地址已作废，探针包请重新下载。"))

    if path == "/probe-package" and method == "GET":
        with _lock:
            try:
                data = _build_package()
            except Exception as exc:
                return _json(start_response, "500 Internal Server Error",
                             {"ok": False, "message": "打包失败：%s" % exc})
        start_response("200 OK", [
            ("Content-Type", "application/gzip"),
            ("Content-Disposition",
             "attachment; filename=dohrelay-probe-pkg.tar.gz"),
            ("Content-Length", str(len(data)))])
        return [data]

    return _json(start_response, "404 Not Found",
                 {"ok": False, "error": "not_found", "message": "路径不存在。"})


app = application
