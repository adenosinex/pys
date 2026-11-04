"""域名指向 — manual domain/IP mapping manager.

Repurposed from the user's myweb DNS panel: the original learned
each LAN host's public IPv6 from inside the home network and pushed
A/AAAA records to Cloudflare — meaningless on a cloud box. This
version keeps his host model (short name -> LAN IPv4, <name>4
domain for IPv4, <name> domain for IPv6, last recorded public IPv6)
and his validation + atomic state file, but the mappings are edited
manually here, nothing is pushed anywhere, and the status view
checks from the server what the domains currently resolve to in
public DNS versus the recorded mapping. A hosts-file export supports
temporary pointing on a single machine. Hub login gates everything.
"""
import copy
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import auth
from page import PAGE_HTML
from webutil import json_response, read_json

BASE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(BASE, ".runtime")
STATE_FILE = os.path.join(RUNTIME, "dns_state.json")
DOMAIN_SUFFIX = os.getenv("DNS_DOMAIN_SUFFIX", "su7.dpdns.org").strip().lower().rstrip(".")
STATUS_TTL = 15
COOKIE_NAME = "dnsmap_session"

# His seeded LAN hosts (name -> LAN IPv4), used only when no state
# file exists yet; afterwards the state file is the single source.
_SEED_LANS = {
    "one": "192.168.31.204",
    "fast": "192.168.31.82",
    "one2": "192.168.31.18",
    "15xa": "192.168.31.241",
    "15xb": "192.168.31.243",
    "15x": "192.168.31.240",
    "apple": "192.168.31.197",
}

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_data_lock = threading.RLock()
_status_lock = threading.Lock()
_status_cache = {"time": 0.0, "revision": -1, "data": None}
_revision = 0
HOSTS = {}


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- validation (his)

def _private_ipv4(value):
    try:
        ip = ipaddress.IPv4Address(value)
        nets = (ipaddress.IPv4Network("10.0.0.0/8"),
                ipaddress.IPv4Network("172.16.0.0/12"),
                ipaddress.IPv4Network("192.168.0.0/16"))
        if not any(ip in n for n in nets):
            raise ValueError("not an RFC1918 LAN address")
        return str(ip)
    except (ValueError, TypeError):
        raise ApiError("lan_ipv4 必须是有效的局域网 IPv4")


def _public_ipv6(value):
    try:
        ip = ipaddress.IPv6Address(value)
        if not ip.is_global or ip.ipv4_mapped is not None:
            raise ValueError("not globally routable")
        return str(ip)
    except (ValueError, TypeError):
        raise ApiError("IPv6 必须是有效的全球单播地址（不接受 fe80、fd、2001:db8 等地址）")


def _domain(value):
    value = str(value or "").strip().lower().rstrip(".")
    if not value.endswith("." + DOMAIN_SUFFIX):
        raise ApiError(f"域名必须属于 {DOMAIN_SUFFIX} 的子域名")
    labels = value.split(".")
    if len(value) > 253 or any(not _NAME_RE.fullmatch(label) for label in labels):
        raise ApiError("域名格式不合法")
    return value


def _hostname(value):
    name = str(value or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        raise ApiError("主机名只能包含小写字母、数字、连字符，长度最多 63")
    return name


# ---------------------------------------------------------------- state

def _default_host(name, lan):
    return {
        "lan_ipv4": lan,
        "ipv4dns": f"{name}4.{DOMAIN_SUFFIX}",
        "ipv6dns": f"{name}.{DOMAIN_SUFFIX}",
        "last_ipv6": None,
        "last_seen": 0,
        "note": "",
    }


def _validate_entry(name, item):
    host = {
        "lan_ipv4": _private_ipv4(item.get("lan_ipv4")),
        "ipv4dns": _domain(item.get("ipv4dns")),
        "ipv6dns": _domain(item.get("ipv6dns")),
        "last_ipv6": _public_ipv6(item["last_ipv6"]) if item.get("last_ipv6") else None,
        "last_seen": max(float(item.get("last_seen") or 0), 0),
        "note": str(item.get("note") or "")[:200],
    }
    return host


def _save_locked():
    os.makedirs(RUNTIME, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=RUNTIME,
                                         prefix=".dns_state_", suffix=".tmp",
                                         delete=False) as fh:
            tmp_name = fh.name
            os.chmod(tmp_name, 0o600)
            json.dump(HOSTS, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, STATE_FILE)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _load_state():
    global HOSTS
    if os.path.exists(STATE_FILE):
        try:
            raw = json.loads(open(STATE_FILE, encoding="utf-8").read())
            loaded = {}
            if isinstance(raw, dict):
                for name, item in raw.items():
                    if not isinstance(item, dict):
                        continue
                    try:
                        loaded[_hostname(name)] = _validate_entry(name, item)
                    except ApiError:
                        continue
            HOSTS = loaded
            return
        except (OSError, ValueError):
            HOSTS = {}
            return
    HOSTS = {name: _default_host(name, lan) for name, lan in _SEED_LANS.items()}
    with _data_lock:
        _save_locked()


def _snapshot():
    with _data_lock:
        return copy.deepcopy(HOSTS), _revision


def _commit(name, host):
    global _revision
    with _data_lock:
        old = copy.deepcopy(HOSTS.get(name))
        HOSTS[name] = copy.deepcopy(host)
        try:
            _save_locked()
        except OSError:
            if old is None:
                HOSTS.pop(name, None)
            else:
                HOSTS[name] = old
            raise ApiError("状态文件保存失败", 500)
        _revision += 1


def _remove(name):
    global _revision
    with _data_lock:
        old = HOSTS.pop(name, None)
        if old is None:
            raise ApiError("主机不存在", 404)
        try:
            _save_locked()
        except OSError:
            HOSTS[name] = old
            raise ApiError("状态文件保存失败", 500)
        _revision += 1


def _find_by_lan(lan):
    with _data_lock:
        for name, host in HOSTS.items():
            if host["lan_ipv4"] == lan:
                return name, copy.deepcopy(host)
    return None, None


_load_state()

# ---------------------------------------------------------------- status (public DNS view)

def _resolve(name, family):
    try:
        rows = socket.getaddrinfo(name, None, family, socket.SOCK_STREAM)
        return sorted({str(ipaddress.ip_address(row[4][0])) for row in rows})
    except (OSError, ValueError):
        return []


def _verify(name, host):
    v4 = _resolve(host["ipv4dns"], socket.AF_INET)
    v6 = _resolve(host["ipv6dns"], socket.AF_INET6)
    expected_v6 = host.get("last_ipv6")
    desired = ipaddress.IPv6Address(expected_v6) if expected_v6 else None
    v6_dns_ok = (any(ipaddress.IPv6Address(v) == desired for v in v6)
                 if desired else None)
    return name, {
        **host,
        "ipv4_ok": None,
        "dns_ok": host["lan_ipv4"] in v4,
        "ipv4_resolved_correctly": host["lan_ipv4"] in v4,
        "ipv6_dns_ok": v6_dns_ok,
        "ipv6_ok": None,
        "ipv4_resolved": v4,
        "ipv6_resolved": v6,
    }


def _status_all():
    hosts, revision = _snapshot()
    now = time.monotonic()
    with _status_lock:
        cached = _status_cache
        if (cached["data"] is not None and cached["revision"] == revision
                and now - cached["time"] < STATUS_TTL):
            return copy.deepcopy(cached["data"])
    result = {}
    if hosts:
        with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
            futures = {ex.submit(_verify, n, h): n for n, h in hosts.items()}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    _, data = fut.result()
                except Exception:
                    data = {**hosts[name], "ipv4_ok": None, "dns_ok": None,
                            "ipv4_resolved_correctly": None, "ipv6_dns_ok": None,
                            "ipv6_ok": None, "ipv4_resolved": [], "ipv6_resolved": []}
                result[name] = data
    with _data_lock:
        latest = _revision
    if latest == revision:
        with _status_lock:
            _status_cache.update(time=time.monotonic(), revision=revision,
                                 data=copy.deepcopy(result))
    return result


def _export_hosts_text():
    hosts, _ = _snapshot()
    lines = ["# su7 域名临时指向（hosts 格式）",
             "# 由 App Hub「域名指向」导出；粘贴到目标机器的 hosts 文件即可临时生效",
             ""]
    for name in sorted(hosts):
        host = hosts[name]
        lines.append(f"{host['lan_ipv4']}\t{host['ipv4dns']}")
        if host.get("last_ipv6"):
            lines.append(f"{host['last_ipv6']}\t{host['ipv6dns']}")
    return "\n".join(lines) + "\n"

# ---------------------------------------------------------------- auth shell

_account, _account_from_env = auth.load_account()


def _client_ip(environ):
    fwd = environ.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return environ.get("REMOTE_ADDR", "") or "unknown"

_account_lock = threading.Lock()


def _cookie_path(environ):
    prefix = environ.get("HTTP_X_FORWARDED_PREFIX") or ""
    return (prefix + "/") if prefix else "/"


def _session_cookie(environ, token, max_age):
    return ("Set-Cookie", "%s=%s; Path=%s; HttpOnly; SameSite=Strict; Max-Age=%d"
            % (COOKIE_NAME, token, _cookie_path(environ), max_age))


def _current_account():
    with _account_lock:
        return _account


def _auth_state(environ):
    """Identity verification lives at the platform layer (the App Hub
    gate in front of /web/<slug>/). This app performs no verification
    itself: any request that reaches it has either passed the
    platform gate or arrived on the local port, which is trusted."""
    account = _current_account()
    return True, "platform", (account.username if account else "local")

def _authed(environ):
    return _auth_state(environ)[0]


def handle_setup(environ, start_response):
    global _account
    with _account_lock:
        if _account is not None:
            return json_response(start_response,
                                 {"ok": False, "error": "setup_done", "message": "账户已设置，设置入口已关闭。"}, 409)
        data = read_json(environ) or {}
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not (1 <= len(username) <= 128):
            return json_response(start_response,
                                 {"ok": False, "error": "bad_username", "message": "账户名需为 1–128 个字符。"}, 400)
        if len(password) < 10:
            return json_response(start_response,
                                 {"ok": False, "error": "short_password", "message": "密码至少 10 位。"}, 400)
        _account = auth.store_account(username, password)
        account = _account
    return json_response(start_response, {"ok": True, "username": account.username},
                         extra_headers=[_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_login(environ, start_response):
    account = _current_account()
    if account is None:
        return json_response(start_response,
                             {"ok": False, "error": "not_configured", "message": "尚未设置账户。"}, 503)
    data = read_json(environ) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if username != account.username or not account.check_password(password):
        return json_response(start_response,
                             {"ok": False, "error": "bad_credentials", "message": "账户名或密码不正确。"}, 401)
    return json_response(start_response, {"ok": True, "username": account.username},
                         extra_headers=[_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_session(environ, start_response):
    account = _current_account()
    authed, via, username = _auth_state(environ)
    return json_response(start_response, {
        "ok": True,
        "setup_required": False,
        "authenticated": authed,
        "via": via,
        "username": username if authed else None,
    })

# ---------------------------------------------------------------- api handlers

def api_manual_update(environ, start_response):
    data = read_json(environ)
    if not isinstance(data, dict):
        raise ApiError("请求必须是 JSON")
    lan = _private_ipv4(data.get("lan_ipv4"))
    name, host = _find_by_lan(lan)
    if not host:
        raise ApiError("找不到对应主机", 404)
    if "ipv6" in data:
        raw = data.get("ipv6")
        host["last_ipv6"] = _public_ipv6(raw) if raw else None
    host["last_seen"] = time.time()
    _commit(name, host)
    return json_response(start_response, {"status": "ok", "ipv6": host.get("last_ipv6") or ""})


def api_add_host(environ, start_response):
    data = read_json(environ)
    if not isinstance(data, dict):
        raise ApiError("请求必须是 JSON")
    name = _hostname(data.get("name"))
    lan = _private_ipv4(data.get("lan_ipv4"))
    host = _default_host(name, lan)
    if data.get("ipv4dns"):
        host["ipv4dns"] = _domain(data["ipv4dns"])
    if data.get("ipv6dns"):
        host["ipv6dns"] = _domain(data["ipv6dns"])
    if data.get("ipv6"):
        host["last_ipv6"] = _public_ipv6(data["ipv6"])
    host["note"] = str(data.get("note") or "")[:200]
    host["last_seen"] = time.time()
    with _data_lock:
        if name in HOSTS:
            raise ApiError("主机名已存在", 409)
        if any(h["lan_ipv4"] == lan for h in HOSTS.values()):
            raise ApiError("该局域网 IPv4 已被其它主机占用", 409)
    _commit(name, host)
    return json_response(start_response, {"status": "created", "name": name, "host": host})


def api_edit_host(environ, start_response, name):
    data = read_json(environ)
    if not isinstance(data, dict):
        raise ApiError("请求必须是 JSON")
    with _data_lock:
        host = copy.deepcopy(HOSTS.get(name))
    if not host:
        raise ApiError("主机不存在", 404)
    if "lan_ipv4" in data:
        lan = _private_ipv4(data["lan_ipv4"])
        with _data_lock:
            if any(n != name and h["lan_ipv4"] == lan for n, h in HOSTS.items()):
                raise ApiError("该局域网 IPv4 已被其它主机占用", 409)
        host["lan_ipv4"] = lan
    if "ipv6" in data:
        raw = data.get("ipv6")
        host["last_ipv6"] = _public_ipv6(raw) if raw else None
    if "note" in data:
        host["note"] = str(data.get("note") or "")[:200]
    host["last_seen"] = time.time()
    _commit(name, host)
    return json_response(start_response, {"status": "ok", "host": host})


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"
    try:
        if path == "/health" and method == "GET":
            return json_response(start_response, {"ok": True})
        if path == "/api/session" and method == "GET":
            return handle_session(environ, start_response)
        if path in ("/api/setup", "/api/login") and method == "POST":
            return json_response(start_response,
                             {"ok": False, "error": "not_found", "message": "接口不存在。"}, 404)
        if path == "/api/logout" and method == "POST":
            return json_response(start_response, {"ok": True},
                                 extra_headers=[_session_cookie(environ, "", 0)])
        if path == "/" and method == "GET":
            start_response("302 Found", [("Location", "panel"), ("Content-Length", "0")])
            return [b""]
        if path == "/panel" and method == "GET":
            body = PAGE_HTML.encode("utf-8")
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                      ("Content-Length", str(len(body)))])
            return [body]

        if not _authed(environ):
            return json_response(start_response,
                                 {"ok": False, "error": "unauthorized", "message": "请先登录。"}, 401)

        if path == "/dns/api/hosts" and method == "GET":
            hosts, _ = _snapshot()
            return json_response(start_response, hosts)
        if path == "/dns/api/hosts" and method == "POST":
            return api_add_host(environ, start_response)
        if path == "/dns/api/status" and method == "GET":
            return json_response(start_response, _status_all())
        if path == "/dns/manual_update" and method == "POST":
            return api_manual_update(environ, start_response)
        if path == "/dns/api/export" and method == "GET":
            return json_response(start_response, {"content": _export_hosts_text()})
        m = re.fullmatch(r"/dns/api/hosts/([a-z0-9-]+)", path)
        if m and method == "POST":
            return api_edit_host(environ, start_response, m.group(1))
        if m and method == "DELETE":
            _remove(m.group(1))
            return json_response(start_response, {"status": "deleted", "name": m.group(1)})

        return json_response(start_response,
                             {"ok": False, "error": "not_found", "message": "接口不存在。"}, 404)
    except ApiError as e:
        return json_response(start_response, {"error": str(e)}, e.status)


app = application
