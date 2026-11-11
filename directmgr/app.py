"""直连管理 — direct-domain list manager for the self-run subscription.

The user keeps one list of domains here; every entry means "this
domain and all its subdomains go DIRECT" (a DOMAIN-SUFFIX rule).
The list (list.json in the project DATA_DIR) is the single source
of truth; on every change the three self-run subscription files
are re-synced by text splice (see sync.py) with full assertions
and mihomo -t validation. The subconv template is NOT touched:
its RULESET keeps the two built-in entries baked at build time,
and the page states that the template picks the list up the next
time subconv itself is updated (stored generated links never
change retroactively).

Normalization lives in the FRONTEND (user ruling 2026-11-11):
the page tidies pasted text into a bare domain inside the input
box, visibly, before submit. This backend accepts ONLY the
canonical form (lowercase bare domain); anything else is rejected
with an explicit error -- the backend never silently fixes input.
Backend validation (syntax / duplicate) is validation, not
normalization.

This app contains no identity verification: the App Hub platform
gate in front of /web/directmgr/ performs it centrally, and direct
access on the local port is trusted.

Write channel to the root-owned subscription files: the debi file
is replaced through a narrow root helper (/usr/local/bin/
directmgr-sub-write, reachable by user apphub via a sudoers
NOPASSWD rule for that exact command; the helper whitelists this
box's subscription path, backs the old file up into this project's
data dir, then replaces atomically). The shoc files are written
over SSH with a dedicated per-service keypair generated on debi
for this project only (private half in this data dir's ssh/,
public half in shoc's authorized_keys): not the operator's key,
and it opens nothing else.
"""
import json
import os
import re
import subprocess
import threading
import time

import sync as syncmod
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
LIST_FILE = os.path.join(DATA_DIR, "list.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

BUILTIN = [
    {"domain": "su7.dpdns.org", "builtin": True,
     "note": "自有服务域名（debi / debi4 / apple 等）"},
    {"domain": "zin7.de", "builtin": True,
     "note": "自有域名（debi / shoc / pys 等入口）"},
]

_lock = threading.Lock()

# Canonical domain: lowercase labels, at least one dot, no scheme,
# no path, no port, no trailing dot. Anything else is rejected.
_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def _load_list():
    try:
        with open(LIST_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("domains") or []
    except Exception:
        items = []
    if not items:
        items = [dict(b, added_at=None) for b in BUILTIN]
    return items


def _save_list(items):
    tmp = LIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"domains": items}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, LIST_FILE)


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- ssh (shoc)

def _ssh_base_args(cfg):
    # Direct sshd-to-sshd with a dedicated per-service keypair
    # (generated on debi for this project only, public half in
    # shoc's authorized_keys; it is not the operator's key and
    # opens nothing else). ssh_proxy stays supported-but-empty.
    args = ["ssh", "-i", cfg["ssh_key"], "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
            "-o", "UserKnownHostsFile=" + cfg["ssh_known_hosts"],
            "-o", "StrictHostKeyChecking=yes"]
    if cfg.get("ssh_proxy"):
        args += ["-o", "ProxyCommand=" + cfg["ssh_proxy"]]
    args.append(cfg["ssh_target"])
    return args


def _ssh_run(cfg, remote_cmd, stdin_text=None, timeout=60):
    r = subprocess.run(_ssh_base_args(cfg) + [remote_cmd],
                       input=stdin_text, capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("ssh failed: %s" % (r.stderr.strip()[:200]))
    return r.stdout


def _read_remote(cfg, path):
    return _ssh_run(cfg, "cat -- " + path)


def _write_remote(cfg, path, text, ts):
    """Backup + atomic replace on the peer box; mode preserved."""
    script = (
        "import sys, shutil, os\n"
        "path, ts = sys.argv[1], sys.argv[2]\n"
        "data = sys.stdin.buffer.read()\n"
        "shutil.copy2(path, path + '.bak-directmgr-' + ts)\n"
        "tmp = path + '.tmp-directmgr'\n"
        "open(tmp, 'wb').write(data)\n"
        "os.replace(tmp, path)\n"
    )
    import base64
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    cmd = ("python3 -c \"import base64;exec(base64.b64decode('%s'))\" %s %s"
           % (b64, path, ts))
    _ssh_run(cfg, cmd, stdin_text=text, timeout=60)


# ---------------------------------------------------------------- sync

def run_sync(items):
    """Sync all subscription files to the list. Returns state dict."""
    cfg = _load_config()
    domains = [it["domain"] for it in items]
    ts = syncmod.now_ts()
    state = {"last_sync_at": time.time(), "last_sync_ts": ts,
             "targets": [], "ok": False, "error": None}
    results, err = syncmod.sync_all(
        domains, cfg, ts,
        read_remote=lambda p: _read_remote(cfg, p),
        write_remote=lambda p, t, ts_: _write_remote(cfg, p, t, ts_))
    state["targets"] = results
    if err:
        state["error"] = err
    else:
        state["ok"] = True
    _save_state(state)
    return state


def current_file_domains():
    """What the subscription files actually carry right now (drift
    check shown on the page)."""
    cfg = _load_config()
    out = {}
    try:
        with open(cfg["debi_sub"], "r", encoding="utf-8") as fh:
            out["debi"] = syncmod.current_domains(fh.read())
    except Exception as exc:
        out["debi"] = "读取失败: %s" % exc
    try:
        out["shoc"] = syncmod.current_domains(
            _read_remote(cfg, cfg["shoc_subs"][0]))
    except Exception as exc:
        out["shoc"] = "读取失败: %s" % exc
    return out


# ---------------------------------------------------------------- http

def _json(start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def _read_json(environ, limit=65536):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > limit:
        return {}
    try:
        data = json.loads(environ["wsgi.input"].read(length).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _public_state(items):
    state = _load_state()
    drift = current_file_domains()
    list_domains = [it["domain"] for it in items]
    drift_notes = []
    for key in ("debi", "shoc"):
        v = drift.get(key)
        if isinstance(v, list) and v != list_domains:
            drift_notes.append("%s 与名单不一致（文件: %s）"
                               % (key, "、".join(v) or "空"))
        elif isinstance(v, str):
            drift_notes.append("%s %s" % (key, v))
    return {"ok": True, "domains": items, "sync": state,
            "file_domains": drift, "drift": drift_notes,
            "template_note": ("subconv 模板暂不自动同步：其规则集仍含内置两条，"
                              "名单将在下次 subconv 更新时同步；已生成的存量"
                              "转化链接不会被追溯修改。")}


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

    if path == "/api/list" and method == "GET":
        with _lock:
            items = _load_list()
            return _json(start_response, "200 OK", _public_state(items))

    if path == "/api/add" and method == "POST":
        data = _read_json(environ)
        domain = data.get("domain")
        note = str(data.get("note") or "").strip()[:80]
        # Backend accepts ONLY canonical form (no silent fixing).
        if not isinstance(domain, str) or domain != domain.strip() \
                or not _DOMAIN_RE.match(domain or ""):
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "error": "bad_format",
                          "message": "格式不标准，应为纯域名如 dedirock.com"
                                     "（小写、无协议/路径/端口/尾点）。"})
        with _lock:
            items = _load_list()
            if any(it["domain"] == domain for it in items):
                return _json(start_response, "200 OK",
                             {"ok": True, "duplicate": True,
                              "message": "已在名单中：" + domain,
                              **_public_state(items)})
            items.append({"domain": domain, "builtin": False,
                          "note": note,
                          "added_at": time.strftime(
                              "%Y-%m-%d %H:%M", time.localtime())})
            _save_list(items)
            state = run_sync(items)
            payload = _public_state(items)
            payload["message"] = ("已添加并同步。" if state.get("ok")
                                  else "已添加，但同步失败（见上方状态）。")
            return _json(start_response, "200 OK", payload)

    if path == "/api/delete" and method == "POST":
        data = _read_json(environ)
        domain = str(data.get("domain") or "")
        with _lock:
            items = _load_list()
            kept = [it for it in items if it["domain"] != domain]
            if len(kept) == len(items):
                return _json(start_response, "404 Not Found",
                             {"ok": False, "error": "not_found",
                              "message": "名单中没有这一条。"})
            _save_list(kept)
            state = run_sync(kept)
            payload = _public_state(kept)
            payload["message"] = ("已删除并同步。" if state.get("ok")
                                  else "已删除，但同步失败（见上方状态）。")
            return _json(start_response, "200 OK", payload)

    if path == "/api/resync" and method == "POST":
        with _lock:
            items = _load_list()
            state = run_sync(items)
            payload = _public_state(items)
            payload["message"] = ("同步完成。" if state.get("ok")
                                  else "同步失败（见上方状态）。")
            return _json(start_response, "200 OK", payload)

    return _json(start_response, "404 Not Found",
                 {"ok": False, "error": "not_found", "message": "路径不存在。"})


app = application
