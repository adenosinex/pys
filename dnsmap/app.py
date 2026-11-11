"""域名指向 — manual domain/IP mapping manager + zin7.de DNS control plane.

2026-11-10: a second, separate book was added beside the LAN ledger:
hosted DNS records for the owned domain zin7.de, written through the
Cloudflare API by an agent-only channel (/dm-api, X-DM-Token). The
provider token lives only in the data dir credentials.json (0600,
write-only from the panel, never echoed); the channel whitelist is
upsert/list of A/AAAA records inside zin7.de only. The LAN ledger
below is unchanged and still pushes nothing anywhere.

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
import hashlib
import secrets
import socket
import tempfile
import urllib.error
import urllib.request
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import auth
from page import PAGE_HTML
from webutil import json_response, read_json

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

RUNTIME = _platform_data_dir(BASE)
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
    # family None means "any address family" (used for CNAME targets):
    # getaddrinfo itself wants AF_UNSPEC (0), not None.
    fam = socket.AF_UNSPEC if family is None else family
    try:
        rows = socket.getaddrinfo(name, None, fam, socket.SOCK_STREAM)
        return sorted({str(ipaddress.ip_address(row[4][0])) for row in rows})
    except (OSError, ValueError, TypeError):
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

# ---------------------------------------------------------------- credentials

CRED_FILE = os.path.join(RUNTIME, "credentials.json")
DM_TOKEN_FILE = os.path.join(RUNTIME, "dm_token.txt")
HOSTED_FILE = os.path.join(RUNTIME, "hosted_state.json")
AUDIT_FILE = os.path.join(RUNTIME, "dns_audit.jsonl")
ALLOWED_ZONE = "zin7.de"
CF_API = "https://api.cloudflare.com/client/v4"
_cred_lock = threading.RLock()
_zone_cache = {"id": None, "at": 0.0}


def _load_creds():
    try:
        with open(CRED_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_creds(data):
    os.makedirs(RUNTIME, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=RUNTIME,
                                         prefix=".credentials_", suffix=".tmp",
                                         delete=False) as fh:
            tmp_name = fh.name
            os.chmod(tmp_name, 0o600)
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, CRED_FILE)
        os.chmod(CRED_FILE, 0o600)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _cf_token():
    with _cred_lock:
        return str(_load_creds().get("cf_api_token") or "")


def _set_cf_token(value):
    with _cred_lock:
        data = _load_creds()
        if value:
            data["cf_api_token"] = value
        else:
            data.pop("cf_api_token", None)
        _save_creds(data)


def _cf_token_configured():
    return bool(_cf_token())


def _dm_expected_hash():
    """The agent channel token is generated on the box and stored only
    as sha256 (nameclean client_token_hash pattern); the plaintext
    lives in dm_token.txt (0600) for the on-box helper, never served."""
    with _cred_lock:
        data = _load_creds()
        stored = str(data.get("dm_token_hash") or "")
        if stored:
            return stored
        if os.path.exists(DM_TOKEN_FILE):
            try:
                plain = open(DM_TOKEN_FILE, encoding="utf-8").read().strip()
            except OSError:
                plain = ""
            if plain:
                digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
                data["dm_token_hash"] = digest
                _save_creds(data)
                return digest
        return ""


def _dm_check(environ):
    stored = _dm_expected_hash()
    given = environ.get("HTTP_X_DM_TOKEN", "") or ""
    if not stored or not given:
        return False
    digest = hashlib.sha256(given.encode("utf-8")).hexdigest()
    return secrets.compare_digest(digest, stored)


def _audit(action, name, rtype, old, new, result, detail=None):
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "action": action, "zone": ALLOWED_ZONE, "name": name,
           "type": rtype, "old": old, "new": new, "result": result}
    if detail is not None:
        row["detail"] = detail
    try:
        os.makedirs(RUNTIME, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _audit_tail(limit=50):
    try:
        lines = open(AUDIT_FILE, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out

# ---------------------------------------------------------------- cloudflare

class _CFError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _cf_req(method, path, payload=None, timeout=20):
    token = _cf_token()
    if not token:
        raise _CFError("CF 令牌未配置", 503)
    url = CF_API + path
    body = None
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
            msgs = "; ".join(str(x.get("message")) for x in detail.get("errors", []))
        except Exception:
            msgs = ""
        raise _CFError("CF API %d %s" % (e.code, msgs or e.reason), 502)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise _CFError("CF API 不可达: %s" % type(e).__name__, 502)


def _cf_zone_id():
    now = time.monotonic()
    if _zone_cache["id"] and now - _zone_cache["at"] < 600:
        return _zone_cache["id"]
    data = _cf_req("GET", "/zones?name=" + ALLOWED_ZONE)
    rows = data.get("result") or []
    if not rows:
        raise _CFError("令牌看不到 zone " + ALLOWED_ZONE, 403)
    zid = rows[0]["id"]
    _zone_cache.update(id=zid, at=now)
    return zid


def _cf_list_records(name=None, rtype=None):
    zid = _cf_zone_id()
    query = "per_page=100"
    if name:
        query += "&name=" + urllib.request.quote(name)
    if rtype:
        query += "&type=" + rtype
    data = _cf_req("GET", "/zones/%s/dns_records?%s" % (zid, query))
    return data.get("result") or []


def _cf_verify():
    data = _cf_req("GET", "/user/tokens/verify")
    result = data.get("result") or {}
    zones = _cf_req("GET", "/zones?per_page=50").get("result") or []
    return {"token_status": result.get("status"),
            "zones": [z.get("name") for z in zones]}

# ---------------------------------------------------------------- hosted ledger

_HOSTED_DEFAULT = {"zone": ALLOWED_ZONE, "provider": "cloudflare", "records": []}
_hosted_lock = threading.RLock()
HOSTED = None


def _load_hosted():
    global HOSTED
    try:
        raw = json.loads(open(HOSTED_FILE, encoding="utf-8").read())
        if isinstance(raw, dict) and isinstance(raw.get("records"), list):
            HOSTED = raw
            return
    except (OSError, ValueError):
        pass
    HOSTED = copy.deepcopy(_HOSTED_DEFAULT)


def _save_hosted():
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=RUNTIME,
                                         prefix=".hosted_", suffix=".tmp",
                                         delete=False) as fh:
            tmp_name = fh.name
            os.chmod(tmp_name, 0o600)
            json.dump(HOSTED, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, HOSTED_FILE)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _hosted_register(name, rtype, value):
    with _hosted_lock:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for row in HOSTED["records"]:
            if row["name"] == name and row["type"] == rtype:
                row["value"] = value
                row["updated_at"] = now
                _save_hosted()
                return
        HOSTED["records"].append({
            "name": name, "type": rtype, "value": value, "ttl": 1,
            "proxied": False, "managed_by": "dnsmap",
            "created_via": "dm-api", "updated_at": now})
        _save_hosted()


_load_hosted()


def _hosted_view():
    with _hosted_lock:
        rows = copy.deepcopy(HOSTED["records"])
        notes = copy.deepcopy(HOSTED.get("notes") or [])
    for row in rows:
        rtype = row["type"]
        if rtype == "A":
            row["resolved"] = _resolve(row["name"], socket.AF_INET)
        elif rtype == "AAAA":
            row["resolved"] = _resolve(row["name"], socket.AF_INET6)
        elif rtype == "CNAME":
            # A CNAME's value is a target hostname, not an address: the
            # address-family check cannot apply (it would flag every
            # healthy alias as mismatched). Ledger-only, like MX/TXT.
            row["resolved"] = []
        else:
            # MX / TXT have no address-family resolution: ledger-only rows,
            # verified against the zone when they were registered.
            row["resolved"] = []
        row["ok"] = row["value"] in row["resolved"] if row["resolved"] else None
    return {"zone": ALLOWED_ZONE, "provider": "cloudflare",
            "records": rows, "notes": notes}

# ---------------------------------------------------------------- dm-api handlers

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _fqdn_of(name):
    name = str(name or "").strip().lower().rstrip(".")
    if name == ALLOWED_ZONE:
        raise ApiError("根域记录不在白名单内", 403)
    if name.endswith("." + ALLOWED_ZONE):
        sub = name[: -(len(ALLOWED_ZONE) + 1)]
    else:
        sub = name
    labels = sub.split(".")
    if not sub or len(labels) > 2 or any(not _LABEL_RE.fullmatch(x) for x in labels):
        raise ApiError("记录名不合法或超出白名单深度", 400)
    return sub + "." + ALLOWED_ZONE


def api_dm_records(environ, start_response):
    rows = _cf_list_records()
    out = [{"name": r["name"], "type": r["type"], "content": r["content"],
            "proxied": bool(r.get("proxied")), "ttl": r.get("ttl")}
           for r in rows if r["type"] in ("A", "AAAA")]
    return json_response(start_response, {"zone": ALLOWED_ZONE, "records": out})


def api_dm_upsert(environ, start_response):
    data = read_json(environ)
    if not isinstance(data, dict):
        raise ApiError("请求必须是 JSON")
    fqdn = _fqdn_of(data.get("name"))
    rtype = str(data.get("type") or "").upper()
    if rtype not in ("A", "AAAA"):
        raise ApiError("只允许 A / AAAA 记录", 400)
    value = str(data.get("value") or "").strip()
    try:
        if rtype == "A":
            value = str(ipaddress.IPv4Address(value))
        else:
            ip6 = ipaddress.IPv6Address(value)
            if not ip6.is_global:
                raise ValueError("not global")
            value = str(ip6)
    except ValueError:
        raise ApiError("记录值不是合法的公网 IP", 400)
    existing = [r for r in _cf_list_records(fqdn, rtype)]
    payload = {"type": rtype, "name": fqdn, "content": value,
               "ttl": 1, "proxied": False}
    try:
        if existing:
            rec = existing[0]
            old = rec["content"]
            if old == value and not rec.get("proxied"):
                _audit("upsert-noop", fqdn, rtype, old, value, "ok")
                _hosted_register(fqdn, rtype, value)
                return json_response(start_response,
                                     {"status": "unchanged", "name": fqdn,
                                      "type": rtype, "value": value})
            _cf_req("PUT", "/zones/%s/dns_records/%s" % (_cf_zone_id(), rec["id"]),
                    payload)
            _audit("upsert-update", fqdn, rtype, old, value, "ok")
            status = "updated"
        else:
            _cf_req("POST", "/zones/%s/dns_records" % _cf_zone_id(), payload)
            _audit("upsert-create", fqdn, rtype, None, value, "ok")
            status = "created"
    except _CFError as e:
        _audit("upsert-failed", fqdn, rtype,
               existing[0]["content"] if existing else None, value,
               "error: %s" % e)
        raise ApiError(str(e), e.status)
    _hosted_register(fqdn, rtype, value)
    return json_response(start_response,
                         {"status": status, "name": fqdn, "type": rtype,
                          "value": value})


def api_dm_audit(environ, start_response):
    return json_response(start_response, {"audit": _audit_tail(50)})


def api_dm_verify(environ, start_response):
    try:
        info = _cf_verify()
    except _CFError as e:
        raise ApiError(str(e), e.status)
    return json_response(start_response, info)


def api_hosted(environ, start_response):
    return json_response(start_response, _hosted_view())


def api_credentials_get(environ, start_response):
    return json_response(start_response, {
        "cf_token_configured": _cf_token_configured(),
        "dm_token_configured": bool(_dm_expected_hash()),
        "zone": ALLOWED_ZONE})


def api_credentials_post(environ, start_response):
    data = read_json(environ)
    if not isinstance(data, dict):
        raise ApiError("请求必须是 JSON")
    if "cf_api_token" in data:
        _set_cf_token(str(data.get("cf_api_token") or "").strip())
    return json_response(start_response, {
        "ok": True, "cf_token_configured": _cf_token_configured()})


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

        if path.startswith("/dm-api/"):
            if not _dm_check(environ):
                return json_response(start_response,
                                     {"ok": False, "error": "unauthorized", "message": "dm token 无效。"}, 401)
            rest = path[len("/dm-api"):]
            if rest == "/records" and method == "GET":
                return api_dm_records(environ, start_response)
            if rest == "/records" and method == "POST":
                return api_dm_upsert(environ, start_response)
            if rest == "/audit" and method == "GET":
                return api_dm_audit(environ, start_response)
            if rest == "/verify" and method == "GET":
                return api_dm_verify(environ, start_response)
            return json_response(start_response,
                                 {"ok": False, "error": "not_found", "message": "接口不存在。"}, 404)

        if not _authed(environ):
            return json_response(start_response,
                                 {"ok": False, "error": "unauthorized", "message": "请先登录。"}, 401)

        if path == "/dns/api/credentials" and method == "GET":
            return api_credentials_get(environ, start_response)
        if path == "/dns/api/credentials" and method == "POST":
            return api_credentials_post(environ, start_response)
        if path == "/dns/api/hosted" and method == "GET":
            return api_hosted(environ, start_response)
        if path == "/dns/api/audit" and method == "GET":
            return json_response(start_response, {"audit": _audit_tail(50)})

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
