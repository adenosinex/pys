"""订阅转化 — per-source subscription converter on the App Hub.

The user pastes an airport subscription link; the app fetches it,
keeps only the real nodes (info/pseudo entries and exact duplicates
are dropped), attaches the user's own rule set (the 2026-11-03
mihomo diversion rules, shared by every generated link), and writes
a converted subscription to a token-named file served over HTTPS by
the platform's /sub/ route. Each source gets its own link.

Every node is registered by a content fingerprint. When a source
subscription updates, already-registered nodes are skipped (their
registry entries — later: IP test results — are preserved); only
new fingerprints are processed. A forced reprocess clears the
registry for that source and rebuilds it from scratch.

This app contains no identity verification: the App Hub platform
gate in front of /web/subconv/ performs it centrally, and direct
access on the local port is trusted. Source URLs are stored in the
app's data directory only and are never rendered back (host only).
"""
import base64
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
import urllib.parse

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))


def _platform_data_dir(base):
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
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
STORE_FILE = os.path.join(DATA_DIR, "sources.json")
os.makedirs(OUTPUT_DIR, exist_ok=True)

PUBLIC_BASE = "https://debi.su7.dpdns.org"
STALE_SECONDS = 12 * 3600
MAX_SUB_BYTES = 8 * 1024 * 1024

RULESET = json.loads(r"""{"rule-providers": {"reject": {"type": "http", "behavior": "domain", "url": "https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/reject.txt", "path": "./ruleset/reject.yaml", "interval": 86400, "proxy": "节点选择"}, "cn-domain": {"type": "http", "behavior": "classical", "url": "https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/ChinaDomain.list", "path": "./ruleset/cn-domain.yaml", "interval": 86400, "proxy": "节点选择"}, "proxy-domains": {"type": "http", "behavior": "domain", "url": "https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/proxy.txt", "path": "./ruleset/proxy-domains.yaml", "interval": 86400, "proxy": "节点选择"}, "claude": {"type": "http", "behavior": "classical", "url": "https://gh-proxy.com/raw.githubusercontent.com/blackmatrix7/ios_rule_script/refs/heads/master/rule/Clash/Claude/Claude.list", "path": "./ruleset/claude.yaml", "interval": 86400, "proxy": "节点选择"}, "gemini": {"type": "http", "behavior": "classical", "url": "https://gh-proxy.com/raw.githubusercontent.com/blackmatrix7/ios_rule_script/refs/heads/master/rule/Clash/Gemini/Gemini.list", "path": "./ruleset/gemini.yaml", "interval": 86400, "proxy": "节点选择"}}, "rules": ["DOMAIN-SUFFIX,local,DIRECT", "DOMAIN-SUFFIX,localhost,DIRECT", "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve", "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve", "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve", "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve", "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve", "IP-CIDR,224.0.0.0/4,DIRECT,no-resolve", "IP-CIDR6,::1/128,DIRECT,no-resolve", "IP-CIDR6,fc00::/7,DIRECT,no-resolve", "IP-CIDR6,fe80::/10,DIRECT,no-resolve", "DOMAIN-SUFFIX,su7.dpdns.org,DIRECT", "RULE-SET,reject,REJECT", "GEOSITE,category-ads-all,REJECT", "GEOSITE,openai,AI", "DOMAIN-SUFFIX,openai.com,AI", "RULE-SET,claude,AI", "DOMAIN-SUFFIX,anthropic.com,AI", "DOMAIN-SUFFIX,claude.ai,AI", "RULE-SET,gemini,AI", "DOMAIN-SUFFIX,gemini.google.com,AI", "DOMAIN-SUFFIX,aistudio.google.com,AI", "DOMAIN-SUFFIX,generativelanguage.googleapis.com,AI", "DOMAIN-SUFFIX,grok.com,AI", "DOMAIN-SUFFIX,x.ai,AI", "DOMAIN-SUFFIX,perplexity.ai,AI", "DOMAIN-SUFFIX,deepseek.com,AI", "DOMAIN-SUFFIX,copilot.microsoft.com,AI", "DOMAIN-SUFFIX,githubcopilot.com,AI", "GEOSITE,netflix,流媒体", "GEOSITE,disney,流媒体", "GEOSITE,youtube,流媒体", "GEOSITE,spotify,流媒体", "GEOSITE,tiktok,流媒体", "DOMAIN-SUFFIX,tiktokv.com,流媒体", "DOMAIN-SUFFIX,tiktokcdn.com,流媒体", "GEOSITE,telegram,Telegram", "IP-CIDR,91.108.4.0/22,Telegram,no-resolve", "IP-CIDR,91.108.8.0/21,Telegram,no-resolve", "IP-CIDR,91.108.16.0/22,Telegram,no-resolve", "IP-CIDR,91.108.56.0/22,Telegram,no-resolve", "IP-CIDR,149.154.160.0/20,Telegram,no-resolve", "IP-CIDR6,2001:67c:4e8::/48,Telegram,no-resolve", "IP-CIDR6,2001:b28:f23d::/48,Telegram,no-resolve", "IP-CIDR6,2001:b28:f23f::/48,Telegram,no-resolve", "GEOSITE,twitter,社交", "GEOSITE,instagram,社交", "GEOSITE,facebook,社交", "DOMAIN-SUFFIX,whatsapp.com,社交", "DOMAIN-SUFFIX,whatsapp.net,社交", "DOMAIN-SUFFIX,services.googleapis.cn,DIRECT", "DOMAIN-SUFFIX,google.cn,DIRECT", "DOMAIN-SUFFIX,apple.com.cn,DIRECT", "DOMAIN-SUFFIX,icloud.com.cn,DIRECT", "GEOSITE,github,开发", "GEOSITE,google,节点选择", "GEOSITE,microsoft,节点选择", "GEOSITE,apple,节点选择", "GEOSITE,cloudflare,节点选择", "GEOSITE,steam,节点选择", "DOMAIN-SUFFIX,speedtest.net,节点选择", "PROCESS-NAME,qBittorrent,DIRECT", "PROCESS-NAME,Transmission,DIRECT", "PROCESS-NAME,aria2c,DIRECT", "PROCESS-NAME,Thunder,DIRECT", "GEOSITE,cn,DIRECT", "RULE-SET,cn-domain,DIRECT", "DOMAIN-SUFFIX,cn,DIRECT", "GEOIP,CN,DIRECT,no-resolve", "RULE-SET,proxy-domains,节点选择", "GEOSITE,category-porn,REJECT", "MATCH,节点选择"], "geox-url": {"geoip": "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.metadb", "geosite": "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geosite.metadb"}}""")

INFO_RE = re.compile(
    r"剩余|到期|流量|网址|官网|自动更新|套餐|重置|防失联|永久地址|公众号|公告|通知")

_lock = threading.Lock()


# ---------------------------------------------------------------- store

def _load_store():
    try:
        with open(STORE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("sources"), list):
            return data
    except Exception:
        pass
    return {"sources": []}


def _save_store(store):
    tmp = STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False)
    os.replace(tmp, STORE_FILE)


def _node_detail(entry):
    test = entry.get("test") or {}
    status = node_status(entry)
    china = bool(OPT_RE.search(entry.get("name") or ""))
    if entry.get("status") == "gone":
        label = "已消失"
    elif status == "ok":
        label = "正常"
    elif status == "dead":
        label = "待中国实测" if china else "已判死"
    else:
        label = "待中国实测" if china else "未测"
    attempts = entry.get("attempts")
    if attempts is None:
        attempts = 1 if test else 0
    successes = entry.get("successes")
    if successes is None:
        successes = attempts if test.get("ok") else 0
    ip_type = None
    if test.get("ok"):
        ip_type = test.get("quality_label") or quality_label(test, None)
    return {
        "name": entry.get("name"), "display": entry.get("display"),
        "status": label, "china_opt": china,
        "egress_ip": test.get("egress_ip"), "country": test.get("country"),
        "city": test.get("city"), "asn": test.get("asn"),
        "org": test.get("org"), "ip_type": ip_type,
        "quality_score": test.get("quality_score"),
        "speed_mbps": test.get("speed_mbps"), "latency_ms": test.get("latency_ms"),
        "youtube": test.get("youtube"),
        "fail_streak": entry.get("fail_streak") or 0,
        "attempts": attempts, "successes": successes,
        "success_rate": (round(successes / attempts, 2) if attempts else None),
        "tested_at": test.get("tested_at"),
    }


_DETAIL_ORDER = {"正常": 0, "待中国实测": 1, "未测": 2, "已判死": 3, "已消失": 4}


def _public_source(src):
    nodes = src.get("nodes") or {}
    return {
        "id": src["id"],
        "label": src.get("label") or src.get("host") or "",
        "host": src.get("host") or "",
        "link": "%s/sub/conv/%s.yaml" % (PUBLIC_BASE, src["token"]),
        "node_count": src.get("node_count", 0),
        "registered": sum(1 for e in nodes.values() if e.get("status") == "kept"),
        "gone": sum(1 for e in nodes.values() if e.get("status") == "gone"),
        "updated": src.get("updated"),
        "last_error": src.get("last_error"),
        "anomaly": _public_anomaly(src),
        "last_added": src.get("last_added", 0),
        "last_gone": src.get("last_gone", 0),
        "dropped_info": src.get("dropped_info", 0),
        "dropped_dup": src.get("dropped_dup", 0),
        "userinfo": src.get("userinfo"),
        "info_count": len(src.get("info_nodes") or []),
        "plan_stats": src.get("plan_stats") or {},
        "tested": sum(1 for e in nodes.values() if e.get("test")),
        "nodes_detail": sorted(
            (_node_detail(e) for e in nodes.values()),
            key=lambda d: (_DETAIL_ORDER.get(d["status"], 9),
                           -(d["speed_mbps"] or 0))),
        "test_state": _source_test_state(src),
    }


def _source_test_state(src):
    if _test_state.get("source_id") == src["id"]:
        return dict(_test_state)
    if any(j["source_id"] == src["id"] for j in _test_queue):
        return {"running": False, "queued": True, "source_id": src["id"],
                "done": 0, "total": 0,
                "message": "测试已排队，等待前面的测试完成…"}
    return None


# ---------------------------------------------------------------- core

def _fingerprint(node):
    blob = json.dumps(node, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _parse_subscription(text):
    try:
        doc = yaml.safe_load(text)
    except Exception:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("proxies"), list):
        return doc["proxies"]
    try:
        compact = "".join(text.split())
        decoded = base64.b64decode(compact + "=" * (-len(compact) % 4))
        doc = yaml.safe_load(decoded.decode("utf-8", "replace"))
    except Exception:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("proxies"), list):
        return doc["proxies"]
    return None


def fetch_subscription(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "clash.meta", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(MAX_SUB_BYTES + 1)
        headers = {k.lower(): v for k, v in resp.headers.items()}
    if len(raw) > MAX_SUB_BYTES:
        raise ValueError("订阅内容过大。")
    proxies = _parse_subscription(raw.decode("utf-8", "replace"))
    if proxies is None:
        raise ValueError("订阅内容无法解析（不是 Clash YAML，也不是 base64 包裹的 YAML）。")
    return proxies, headers


def _parse_userinfo(headers):
    """subscription-userinfo response header -> traffic/expiry dict."""
    raw = (headers or {}).get("subscription-userinfo") or ""
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip()] = int(float(v.strip()))
            except ValueError:
                pass
    if not out:
        return None
    total = out.get("total") or 0
    used = (out.get("upload") or 0) + (out.get("download") or 0)
    return {"upload": out.get("upload") or 0,
            "download": out.get("download") or 0,
            "total": total, "expire": out.get("expire"),
            "remaining": (total - used) if total else None}


_LOOPBACK_SERVERS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


def _is_info_node(node, name):
    if INFO_RE.search(name):
        return True
    return str(node.get("server") or "").strip().lower() in _LOOPBACK_SERVERS


def filter_nodes(proxies):
    kept, info_nodes, seen_fp, seen_name = [], [], set(), set()
    seen_info_name = set()
    dropped_info = dropped_dup = 0
    for node in proxies:
        if not isinstance(node, dict):
            dropped_info += 1
            continue
        name = str(node.get("name") or "").strip()
        if (not name or not node.get("server") or not node.get("port")
                or not node.get("type")):
            dropped_info += 1
            continue
        if _is_info_node(node, name):
            if name not in seen_info_name and len(info_nodes) < 16:
                seen_info_name.add(name)
                info_nodes.append(dict(node))
            continue
        fp = _fingerprint(node)
        if fp in seen_fp:
            dropped_dup += 1
            continue
        seen_fp.add(fp)
        if name in seen_name:
            node = dict(node)
            i = 2
            while "%s-%d" % (name, i) in seen_name:
                i += 1
            node["name"] = "%s-%d" % (name, i)
            name = node["name"]
        seen_name.add(name)
        kept.append((fp, node))
    return kept, info_nodes, dropped_info, dropped_dup


OPT_RE = re.compile(r"优化|电信|联通|移动|CN2|GIA|回国|直连|家宽|原生")

PROTO_LABELS = {"vless": "VLESS", "hysteria2": "HY2", "anytls": "AnyTLS",
                "vmess": "VMess", "trojan": "Trojan", "ss": "SS",
                "tuic": "TUIC", "wireguard": "WG"}

COUNTRY_LABELS = {
    "HK": "香港", "TW": "台湾", "JP": "日本", "SG": "新加坡", "KR": "韩国",
    "GB": "英国", "DE": "德国", "FR": "法国", "NL": "荷兰", "CA": "加拿大",
    "AU": "澳大利亚", "IN": "印度", "TH": "泰国", "VN": "越南", "MY": "马来西亚",
    "ID": "印尼", "PH": "菲律宾", "TR": "土耳其", "RU": "俄罗斯", "BR": "巴西",
    "MX": "墨西哥", "AE": "阿联酋", "IT": "意大利", "ES": "西班牙", "SE": "瑞典",
    "CH": "瑞士", "PL": "波兰", "UA": "乌克兰", "IL": "以色列", "ZA": "南非",
    "NG": "尼日利亚", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚",
    "AT": "奥地利", "BE": "比利时", "DK": "丹麦", "FI": "芬兰", "NO": "挪威",
    "IE": "爱尔兰", "PT": "葡萄牙", "CZ": "捷克", "HU": "匈牙利", "RO": "罗马尼亚",
}

US_WEST_TZ = {"America/Los_Angeles", "America/Denver", "America/Phoenix",
              "America/Anchorage", "Pacific/Honolulu", "America/Boise",
              "America/Tijuana"}

EUROPE_LABELS = {"德国", "英国", "法国", "荷兰", "意大利", "西班牙", "瑞典",
                 "瑞士", "波兰", "奥地利", "比利时", "丹麦", "芬兰", "挪威",
                 "爱尔兰", "葡萄牙", "捷克", "匈牙利", "罗马尼亚", "乌克兰"}


def region_label(test):
    cc = (test.get("country_code") or "").upper()
    if cc == "US":
        return "美国西" if test.get("timezone") in US_WEST_TZ else "美国东"
    if cc in COUNTRY_LABELS:
        return COUNTRY_LABELS[cc]
    return test.get("country") or cc or "未知"


def node_status(entry):
    test = entry.get("test") or {}
    if test.get("ok"):
        return "ok"
    if (entry.get("fail_streak") or 0) >= 3:
        return "dead"
    return "untested"


def _speed_key(test):
    speed = test.get("speed_mbps")
    latency = test.get("latency_ms")
    return (speed is None, -(speed or 0), latency is None, latency or 10 ** 9)


# ------------------------------------------------------------- ip quality

QUALITY_FILE = os.path.join(DATA_DIR, "ip_quality.json")
QUALITY_TTL = 7 * 86400

DC_KEYWORDS = ("amazon", "leaseweb", "frantech", "psychz", "fdcservers",
               "baxet", "ovh", "digitalocean", "choopa", "vultr", "linode",
               "google", "microsoft", "oracle", "hetzner", "hostdzire",
               "datacamp", "contabo", "kamatera", "serverius", "worldstream",
               "hosting", "datacenter", "data center", "readyserver")


def classify_ip(test):
    org = ((test.get("org") or "") + " " + (test.get("asn") or "")).lower()
    if test.get("hosting") or any(k in org for k in DC_KEYWORDS):
        return "机房"
    if test.get("mobile"):
        return "移动"
    return "家宽"


def _grade_word(score):
    if score is None:
        return ""
    if score <= 10:
        return "干净"
    if score <= 40:
        return "一般"
    if score <= 70:
        return "风险"
    return "高风险"


def quality_label(test, score):
    label = _grade_word(score) + classify_ip(test)
    if score is not None:
        label += " %d分" % score
    return label


def _load_quality_cache():
    try:
        with open(QUALITY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_quality_cache(cache):
    tmp = QUALITY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    os.replace(tmp, QUALITY_FILE)


def fetch_fraud_score(ip):
    """Scamalytics public IP page -> fraud score (0-100, lower is
    cleaner). Returns None when unreachable or unparsable."""
    req = urllib.request.Request(
        "https://scamalytics.com/ip/" + ip,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            page = resp.read(400000).decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"Fraud Score:\s*(\d+)", page)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


_ORG_STOP = {"network", "networks", "information", "communication",
             "communications", "technology", "technologies", "limited",
             "ltd", "inc", "corp", "corporation", "pte", "llc", "group",
             "k.k.", "co", "company", "holdings", "and", "the", "solutions",
             "services", "international"}


def _org_short(test):
    org = (test.get("org") or "").strip()
    if not org:
        asn = (test.get("asn") or "").strip()
        org = re.sub(r"^AS\d+\s*", "", asn)
    if not org:
        return ""
    m = re.search(r"\(([A-Za-z0-9 .&-]{2,12})\)", org)
    if m:
        return m.group(1).strip()[:12]
    keep = []
    for w in re.split(r"[\s,]+", org):
        if not w or w.lower().strip(".") in _ORG_STOP:
            continue
        cand = (" ".join(keep + [w])) if keep else w
        if len(cand) > 14:
            break
        keep.append(w)
        if len(keep) == 2:
            break
    return " ".join(keep) if keep else org[:14]


def rich_display(base, test):
    """Client-visible name: region-protocol base plus the IP quality
    grade (from third-party scoring) and the measured speed."""
    parts = [base]
    label = test.get("quality_label") or quality_label(test, None)
    if label:
        parts.append(label)
    spd = test.get("speed_mbps")
    if spd:
        parts.append(("%.1fM" % spd) if spd < 1 else ("%dM" % round(spd)))
    return " | ".join(parts)


def plan_output(src, kept):
    """kept: [(fingerprint, node)] from the latest fetch. Applies the
    plan: dead non-optimized nodes drop out; ok nodes are renamed by
    measured egress (region-protocol, numbered within collisions);
    china-optimized untested/dead nodes keep their name behind the
    [待中国实测] prefix; other untested nodes get [未测]. Returns
    (yaml_text, stats)."""
    registry = src.get("nodes") or {}
    ok_items, china_items, untested_items = [], [], []
    dropped_dead = 0
    for fp, node in kept:
        entry = registry.get(fp) or {}
        status = node_status(entry)
        original = str(node.get("name") or "")
        if status == "dead" and not OPT_RE.search(original):
            dropped_dead += 1
            if fp in registry:
                registry[fp]["display"] = None
            continue
        if status == "ok":
            ok_items.append((fp, node, entry.get("test") or {}))
        elif OPT_RE.search(original):
            china_items.append((fp, node))
        else:
            untested_items.append((fp, node))

    # rename ok nodes: group by (region, protocol); same-egress dupes
    # keep only the best; collisions numbered by speed then latency.
    groups_map = {}
    for fp, node, test in ok_items:
        key = (region_label(test), PROTO_LABELS.get(
            str(node.get("type") or "").lower(), str(node.get("type") or "").upper()))
        groups_map.setdefault(key, []).append((fp, node, test))
    renamed = []  # (display_name, node, test, region)
    dropped_dup_ok = 0
    region_winners = {}
    for (region, proto), items in groups_map.items():
        by_ip = {}
        for fp, node, test in items:
            ip = test.get("egress_ip") or ""
            if ip and ip in by_ip:
                dropped_dup_ok += 1
                if _speed_key(test) < _speed_key(by_ip[ip][2]):
                    loser = by_ip[ip][0]
                    if loser in registry:
                        registry[loser]["display"] = None
                    by_ip[ip] = (fp, node, test)
                elif fp in registry:
                    registry[fp]["display"] = None
                continue
            by_ip[ip] = (fp, node, test)
        region_winners.setdefault(region, []).extend(by_ip.values())
    for region, winners in region_winners.items():
        winners = sorted(winners, key=lambda t: _speed_key(t[2]))
        for i, (_fp, node, test) in enumerate(winners):
            base = region if len(winners) == 1 else "%s-%02d" % (region, i + 1)
            display = rich_display(base, test)
            new_node = dict(node)
            new_node["name"] = display
            renamed.append((display, new_node, test, region))
            if _fp in registry:
                registry[_fp]["display"] = display

    proxies = [node for _name, node, _t, _r in renamed]
    ok_names = [name for name, _n, _t, _r in renamed]
    china_names = []
    for _fp, node in china_items:
        new_node = dict(node)
        new_node["name"] = "[待中国实测]" + str(node.get("name") or "")
        if _fp in registry:
            registry[_fp]["display"] = new_node["name"]
        china_names.append(new_node["name"])
        proxies.append(new_node)
    untested_names = []
    for _fp, node in untested_items:
        new_node = dict(node)
        new_node["name"] = "[未测]" + str(node.get("name") or "")
        if _fp in registry:
            registry[_fp]["display"] = new_node["name"]
        untested_names.append(new_node["name"])
        proxies.append(new_node)

    # Provider info entries (remaining traffic, expiry, notices) pass
    # through verbatim under their original names: never tested, never
    # renamed, and only listed at the end of the manual select group so
    # they can never be auto-picked.
    info_names = []
    used_names = {str(n.get("name") or "") for n in proxies}
    for node in src.get("info_nodes") or []:
        new_node = dict(node)
        name = str(new_node.get("name") or "")
        if name in used_names:
            i = 2
            while "%s (%d)" % (name, i) in used_names:
                i += 1
            name = "%s (%d)" % (name, i)
            new_node["name"] = name
        used_names.add(name)
        info_names.append(name)
        proxies.append(new_node)

    groups = []
    select_all = ((["自动选择"] if ok_names else []) + ok_names + china_names
                  + untested_names + info_names)
    groups.append({"name": "节点选择", "type": "select", "proxies": select_all})
    if ok_names:
        groups.append({"name": "自动选择", "type": "url-test",
                       "url": "http://www.gstatic.com/generate_204",
                       "interval": 300, "tolerance": 80, "proxies": ok_names})
        region_members = {}
        for name, _n, _t, region in renamed:
            bucket = "欧洲" if region in EUROPE_LABELS else region
            region_members.setdefault(bucket, []).append(name)
        for bucket in sorted(region_members):
            groups.append({"name": bucket + "组", "type": "url-test",
                           "url": "http://www.gstatic.com/generate_204",
                           "interval": 300, "tolerance": 80,
                           "proxies": region_members[bucket]})
    if china_names:
        groups.append({"name": "待中国实测组", "type": "select", "proxies": china_names})
    for gname in ("AI", "流媒体", "Telegram", "社交", "开发"):
        members = ["节点选择"] + (["自动选择"] if ok_names else []) + ok_names
        groups.append({"name": gname, "type": "select", "proxies": members})

    doc = {
        "mixed-port": 42381,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "geox-url": RULESET["geox-url"],
        "proxies": proxies,
        "proxy-groups": groups,
        "rule-providers": RULESET["rule-providers"],
        "rules": RULESET["rules"],
    }
    stats = {"ok": len(ok_names), "china": len(china_names),
             "untested": len(untested_names), "dropped_dead": dropped_dead,
             "dropped_dup_ok": dropped_dup_ok, "info": len(info_names)}
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), stats


def refresh_source(src, force=False):
    proxies, headers = fetch_subscription(src["url"])
    kept, info_nodes, dropped_info, dropped_dup = filter_nodes(proxies)
    src["info_nodes"] = info_nodes
    src["userinfo"] = _parse_userinfo(headers)
    registry = {} if force else dict(src.get("nodes") or {})
    now = time.time()
    added = 0
    present = set()
    for fp, node in kept:
        present.add(fp)
        if fp in registry:
            registry[fp]["last_seen"] = now
            registry[fp]["status"] = "kept"
            registry[fp]["name"] = node["name"]
        else:
            registry[fp] = {"name": node["name"], "first_seen": now,
                            "last_seen": now, "status": "kept"}
            added += 1
    gone = 0
    for fp, entry in registry.items():
        if fp not in present and entry.get("status") != "gone":
            entry["status"] = "gone"
            gone += 1
    src["nodes"] = registry
    text, plan_stats = plan_output(src, kept)
    out_path = os.path.join(OUTPUT_DIR, src["token"] + ".yaml")
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, out_path)
    src.update({
        "updated": now, "last_error": None,
        "node_count": len(kept), "last_added": added, "last_gone": gone,
        "dropped_info": dropped_info, "dropped_dup": dropped_dup,
        "plan_stats": plan_stats,
    })
    return src


def _find_source(store, sid):
    for src in store["sources"]:
        if src["id"] == sid:
            return src
    return None



# ---------------------------------------------------------------- tests

MIHOMO_VERSION = "v1.19.33"
MIHOMO_URL = ("https://github.com/MetaCubeX/mihomo/releases/download/"
              "v1.19.33/mihomo-linux-amd64-compatible-v1.19.33.gz")
TEST_PORT = 17950
BIN_DIR = os.path.join(DATA_DIR, "bin")
SCRATCH_DIR = os.path.join(DATA_DIR, ".test-scratch")

_test_state = {"running": False, "source_id": None, "done": 0,
               "total": 0, "started_at": None, "message": "",
               "trigger": None, "queued": 0, "last_run": None}

# Pending test jobs, consumed one at a time by _chain_worker so that
# sources.json keeps exactly one writer thread no matter which trigger
# (manual button / auto-add / daily timer) asked for the run.
_test_queue = []
_test_lock = threading.Lock()
RUNS_LOG = os.path.join(DATA_DIR, "test_runs.jsonl")


def ensure_mihomo():
    path = os.path.join(BIN_DIR, "mihomo")
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    os.makedirs(BIN_DIR, exist_ok=True)
    req = urllib.request.Request(MIHOMO_URL, headers={"User-Agent": "subconv"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        blob = resp.read()
    raw = gzip.decompress(blob)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)
    return path


def _curl(args, timeout=30):
    try:
        r = subprocess.run(["curl", "-s"] + args, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _probe_204(url, timeout=15):
    out = _curl(["-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                 "--max-time", str(timeout), "-x",
                 "http://127.0.0.1:%d" % TEST_PORT, url], timeout=timeout + 5)
    parts = out.split()
    if len(parts) == 2:
        try:
            return parts[0], float(parts[1])
        except ValueError:
            pass
    return "000", None


def test_node(mihomo, node, light=False):
    """Run one node through a throwaway mihomo instance and measure
    egress / latency / speed. Returns a result dict (ok flag plus
    fields); never raises. In light mode the multi-MB speed download
    is skipped (connectivity, latency and egress identity only) and
    the caller keeps the previous full-run speed on record."""
    result = {"ok": False, "tested_at": time.time()}
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    try:
        os.chmod(SCRATCH_DIR, 0o700)
    except OSError:
        pass
    cfg = {
        "mixed-port": TEST_PORT, "allow-lan": False, "mode": "rule",
        "log-level": "warning", "ipv6": True,
        "dns": {"enable": True, "ipv6": True, "enhanced-mode": "redir-host",
                "nameserver": ["223.5.5.5", "119.29.29.29",
                               "https://doh.pub/dns-query"],
                "proxy-server-nameserver": ["223.5.5.5", "119.29.29.29",
                                            "https://doh.pub/dns-query"],
                "default-nameserver": ["223.5.5.5", "119.29.29.29"]},
        "proxies": [node],
        "proxy-groups": [{"name": "T", "type": "select",
                          "proxies": [node["name"]]}],
        "rules": ["MATCH,T"],
    }
    cfg_path = os.path.join(SCRATCH_DIR, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
    proc = subprocess.Popen([mihomo, "-d", SCRATCH_DIR, "-f", cfg_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import socket as _socket
        up = False
        for _ in range(40):
            try:
                with _socket.create_connection(("127.0.0.1", TEST_PORT), timeout=0.5):
                    up = True
                    break
            except OSError:
                time.sleep(0.25)
        if not up:
            result["error"] = "client_start_failed"
            return result
        fields = ("status,country,countryCode,regionName,city,timezone,"
                  "isp,org,as,hosting,proxy,mobile,query")
        api = None
        for _attempt in range(2):
            raw = _curl(["--max-time", "15", "-x",
                         "http://127.0.0.1:%d" % TEST_PORT,
                         "http://ip-api.com/json?fields=" + fields], timeout=20)
            try:
                cand = json.loads(raw)
                if cand.get("status") == "success":
                    api = cand
                    break
            except Exception:
                pass
            time.sleep(1)
        if not api:
            result["error"] = "egress_probe_failed"
            return result
        result.update({
            "egress_ip": api.get("query"), "country": api.get("country"),
            "country_code": api.get("countryCode"), "city": api.get("city"),
            "region_name": api.get("regionName"), "timezone": api.get("timezone"),
            "asn": api.get("as"), "org": api.get("org") or api.get("isp"),
            "hosting": bool(api.get("hosting")), "mobile": bool(api.get("mobile")),
        })
        times = []
        for _ in range(2):
            code, secs = _probe_204("https://www.google.com/generate_204")
            if code == "204" and secs is not None:
                times.append(secs)
        result["latency_ms"] = round(min(times) * 1000) if times else None
        code, _secs = _probe_204("https://www.youtube.com/generate_204")
        result["youtube"] = (code == "204")
        if light:
            # The speed download is the one expensive step of a full
            # test (a multi-MB file per node); light rounds skip it.
            result["light"] = True
        else:
            out = _curl(["-o", "/dev/null", "-w", "%{size_download} %{time_total}",
                         "--max-time", "8", "-x", "http://127.0.0.1:%d" % TEST_PORT,
                         "https://speed.cloudflare.com/__down?bytes=6000000"],
                        timeout=12)
            parts = out.split()
            if len(parts) == 2:
                try:
                    size, secs = float(parts[0]), float(parts[1])
                    result["dl_bytes"] = int(size)
                    if size >= 200000 and secs > 0:
                        result["speed_mbps"] = round(size * 8 / secs / 1_000_000, 2)
                except ValueError:
                    pass
        result.setdefault("speed_mbps", None)
        result["ok"] = True
        return result
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        try:
            os.unlink(cfg_path)
        except OSError:
            pass
        time.sleep(0.3)


def _needs_test(entry, force):
    if entry.get("status") != "kept":
        return False
    if force:
        return True
    test = entry.get("test") or {}
    if test.get("ok"):
        return False
    if (entry.get("fail_streak") or 0) >= 3:
        return False
    return True


def _record_run(rec):
    try:
        with open(RUNS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _recent_runs(limit=8):
    try:
        with open(RUNS_LOG, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


ANOMALY_KINDS = {"fetch_failed": "拉取失败", "empty": "空源",
                 "parse_failed": "解析失败"}


def _classify_fetch_error(exc):
    if isinstance(exc, ValueError) and "无法解析" in str(exc):
        return "parse_failed"
    return "fetch_failed"


def _mark_anomaly(source_id, kind, detail):
    """Record (or extend) a source-level anomaly: the subscription
    itself could not be fetched, could not be parsed, or yielded
    zero usable nodes. Callers return before any output rebuild, so
    the converted file keeps serving the last good version; the
    source is then paused from scheduled test rounds and only gets
    a bare liveness probe per round until it recovers."""
    now = time.time()
    with _lock:
        store = _load_store()
        src = _find_source(store, source_id)
        if src is None:
            return None
        cur = src.get("anomaly") or {}
        anomaly = {"kind": kind, "detail": detail,
                   "since": cur.get("since") or now,
                   "rounds": (cur.get("rounds") or 0) + 1,
                   "last_at": now}
        src["anomaly"] = anomaly
        label = src.get("label") or src.get("host") or ""
        _save_store(store)
    _record_run({"event": "source_anomaly", "source_id": source_id,
                 "label": label, "kind": kind, "detail": detail,
                 "rounds": anomaly["rounds"], "at": now})
    return anomaly


def _clear_anomaly(source_id):
    """Drop the anomaly record after a successful fetch. Returns
    True when an anomaly had been active (i.e. this is a recovery)."""
    with _lock:
        store = _load_store()
        src = _find_source(store, source_id)
        if src is None or not src.get("anomaly"):
            return False
        label = src.get("label") or src.get("host") or ""
        src["anomaly"] = None
        _save_store(store)
    _record_run({"event": "source_recovered", "source_id": source_id,
                 "label": label, "at": time.time()})
    return True


def _public_anomaly(src):
    anomaly = src.get("anomaly") or {}
    if not anomaly:
        return None
    return {"kind": anomaly.get("kind"),
            "kind_label": ANOMALY_KINDS.get(anomaly.get("kind"),
                                            anomaly.get("kind")),
            "since": anomaly.get("since"),
            "rounds": anomaly.get("rounds") or 0}


def _run_source_tests(source_id, force=False, mode="full"):
    """Run the whole test pipeline for one source (per-node probes,
    Scamalytics backfill, output rebuild) and return a run summary.
    The caller (_chain_worker) owns the running flag and the run log,
    so this function must not touch them. mode="light" probes every
    registered node for connectivity / latency / egress identity but
    skips the speed downloads, and preserves each node's last
    full-run speed figure."""
    summary = {"planned": 0, "tested": 0, "ok": 0, "traffic_bytes": 0,
               "label": "", "message": ""}
    try:
        mihomo = ensure_mihomo()
    except Exception as exc:
        summary["message"] = "mihomo 准备失败：%s" % str(exc)[:120]
        return summary
    with _lock:
        store = _load_store()
        src = _find_source(store, source_id)
    if src is None:
        summary["message"] = "订阅源不存在。"
        return summary
    summary["label"] = src.get("label") or src.get("host") or ""
    try:
        proxies, _headers = fetch_subscription(src["url"])
    except Exception as exc:
        kind = _classify_fetch_error(exc)
        _mark_anomaly(source_id, kind, str(exc)[:120])
        summary["message"] = "源异常（%s）：%s" % (
            ANOMALY_KINDS.get(kind, kind), str(exc)[:120])
        summary["anomaly"] = kind
        return summary
    kept, _info, _di, _dd = filter_nodes(proxies)
    if not kept:
        _mark_anomaly(source_id, "empty", "拉回节点数为 0")
        summary["message"] = "源异常（空源）：拉回节点数为 0。"
        summary["anomaly"] = "empty"
        return summary
    if _clear_anomaly(source_id):
        summary["recovered"] = True
    node_by_fp = dict(kept)
    with _lock:
        store = _load_store()
        src_now = _find_source(store, source_id)
        registry = (src_now or {}).get("nodes") or {}
    if mode == "light":
        # Light round: probe every registered node once — latency
        # and availability drift daily, speed is refreshed weekly.
        work = [fp for fp in node_by_fp
                if (registry.get(fp) or {}).get("status") == "kept"]
    else:
        work = [fp for fp in node_by_fp if _needs_test(registry.get(fp) or {}, force)]
    _test_state.update({"total": len(work), "done": 0})
    summary["planned"] = len(work)
    for fp in work:
        node = node_by_fp[fp]
        res = test_node(mihomo, node, light=(mode == "light"))
        summary["traffic_bytes"] += res.pop("dl_bytes", 0) or 0
        summary["tested"] += 1
        if res.get("ok"):
            summary["ok"] += 1
        with _lock:
            store = _load_store()
            src_now = _find_source(store, source_id)
            if src_now is None:
                break
            entry = (src_now.get("nodes") or {}).get(fp)
            if entry is None:
                continue
            old_test = entry.get("test") or {}
            had_test = bool(old_test)
            if mode == "light":
                # A light round never overwrites the speed measured
                # by the last full run: node names keep showing it.
                res["speed_mbps"] = old_test.get("speed_mbps")
            entry["test"] = res
            prev_attempts = entry.get("attempts")
            if prev_attempts is None:
                prev_attempts = 1 if had_test else 0
            entry["attempts"] = prev_attempts + 1
            prev_successes = entry.get("successes")
            if prev_successes is None:
                prev_successes = 1 if (had_test and old_test.get("ok")) else 0
            entry["successes"] = prev_successes + (1 if res.get("ok") else 0)
            entry["fail_streak"] = 0 if res.get("ok") else (entry.get("fail_streak") or 0) + 1
            _save_store(store)
        _test_state["done"] += 1
    # IP quality backfill: score every ok egress of this source via
    # Scamalytics (cached per egress IP for a week), then rebuild.
    with _lock:
        store = _load_store()
        src_now = _find_source(store, source_id)
        if src_now is not None:
            cache = _load_quality_cache()
            ips = []
            for e in (src_now.get("nodes") or {}).values():
                t = e.get("test") or {}
                if t.get("ok") and t.get("egress_ip") and t["egress_ip"] not in ips:
                    ips.append(t["egress_ip"])
            changed = False
            for ip in ips:
                if mode == "light":
                    # Light rounds never fetch fresh scores: the
                    # weekly cache is used exactly as it stands, and
                    # a cache miss simply leaves the label scoreless.
                    continue
                q = cache.get(ip)
                if not q or time.time() - (q.get("fetched_at") or 0) > QUALITY_TTL:
                    cache[ip] = {"score": fetch_fraud_score(ip),
                                 "fetched_at": time.time()}
                    changed = True
                    time.sleep(0.5)
            if changed:
                _save_quality_cache(cache)
            for e in (src_now.get("nodes") or {}).values():
                t = e.get("test") or {}
                if t.get("ok") and t.get("egress_ip"):
                    q = cache.get(t["egress_ip"]) or {}
                    t["quality_score"] = q.get("score")
                    t["quality_class"] = classify_ip(t)
                    t["quality_label"] = quality_label(t, q.get("score"))
            _save_store(store)
    # rebuild the output with the fresh results
    with _lock:
        store = _load_store()
        src_now = _find_source(store, source_id)
        if src_now is not None:
            try:
                refresh_source(src_now)
            except Exception as exc:
                src_now["last_error"] = str(exc)[:200]
            _save_store(store)
    summary["message"] = "%s完成：%d 个节点。" % (
        "轻测" if mode == "light" else "测试", summary["tested"])
    return summary


def _run_probe(source_id, mode):
    """Liveness probe for an anomaly-paused source: one bare
    subscription fetch, no node tests. Still dead -> the anomaly
    rounds counter grows and the source stays paused; alive again ->
    the anomaly clears inside _run_source_tests and this round's
    regular test run executes immediately, in the same queue turn."""
    summary = {"planned": 0, "tested": 0, "ok": 0, "traffic_bytes": 0,
               "label": "", "message": ""}
    with _lock:
        store = _load_store()
        src = _find_source(store, source_id)
    if src is None:
        summary["message"] = "订阅源不存在。"
        return summary
    summary["label"] = src.get("label") or src.get("host") or ""
    try:
        proxies, _headers = fetch_subscription(src["url"])
    except Exception as exc:
        kind = _classify_fetch_error(exc)
        _mark_anomaly(source_id, kind, str(exc)[:120])
        summary["message"] = "存活探测失败（%s），继续暂停。" % ANOMALY_KINDS.get(kind, kind)
        summary["anomaly"] = kind
        return summary
    kept, _info, _di, _dd = filter_nodes(proxies)
    if not kept:
        _mark_anomaly(source_id, "empty", "拉回节点数为 0")
        summary["message"] = "存活探测失败（空源），继续暂停。"
        summary["anomaly"] = "empty"
        return summary
    return _run_source_tests(source_id, force=(mode == "full"), mode=mode)


def _chain_worker():
    """Single consumer of the test queue: runs queued jobs strictly
    one at a time (sources.json has exactly one writer thread) and
    appends one metrics record per finished source run."""
    while True:
        with _test_lock:
            if not _test_queue:
                _test_state.update({"running": False, "queued": 0})
                return
            job = _test_queue.pop(0)
            _test_state.update({"running": True,
                                "source_id": job["source_id"], "done": 0,
                                "total": 0, "started_at": time.time(),
                                "message": "", "trigger": job["trigger"],
                                "queued": len(_test_queue)})
        started = time.time()
        try:
            if job.get("kind") == "probe":
                summary = _run_probe(job["source_id"], job.get("mode") or "light")
            else:
                summary = _run_source_tests(job["source_id"], job["force"],
                                            job.get("mode") or "full")
        except Exception as exc:  # a broken run must never wedge the queue
            summary = {"planned": 0, "tested": 0, "ok": 0,
                       "traffic_bytes": 0, "label": "",
                       "message": "测试异常：%s" % str(exc)[:120]}
        finished = time.time()
        _record_run({"trigger": job["trigger"],
                     "mode": job.get("mode") or "full",
                     "kind": job.get("kind") or "test",
                     "source_id": job["source_id"],
                     "label": summary.get("label") or "",
                     "started_at": started, "finished_at": finished,
                     "duration_s": round(finished - started, 1),
                     "planned": summary["planned"],
                     "tested": summary["tested"], "ok": summary["ok"],
                     "est_bytes": summary["traffic_bytes"],
                     "anomaly": summary.get("anomaly"),
                     "recovered": bool(summary.get("recovered")),
                     "message": summary["message"]})
        with _test_lock:
            _test_state.update({
                "message": summary["message"],
                "last_run": {"trigger": job["trigger"],
                             "source_id": job["source_id"],
                             "label": summary.get("label") or "",
                             "tested": summary["tested"],
                             "ok": summary["ok"],
                             "duration_s": round(finished - started, 1),
                             "est_bytes": summary["traffic_bytes"],
                             "finished_at": finished}})


def start_tests(source_id, force=False, trigger="manual", mode="full",
                kind="test"):
    """Enqueue one source's test run. Triggers: the page button
    ("manual"), a freshly added source ("auto-add"), the daily
    timer ("daily", light mode) and the weekly timer ("weekly",
    full mode). kind="probe" enqueues a bare liveness fetch for an
    anomaly-paused source instead of a node-test run. A source that
    is already running or queued is folded into the existing job;
    the returned state says so via "enqueued": False."""
    with _test_lock:
        busy = (_test_state.get("running")
                and _test_state.get("source_id") == source_id)
        queued = any(j["source_id"] == source_id for j in _test_queue)
        if busy or queued:
            state = dict(_test_state)
            state["enqueued"] = False
            state["queued"] = len(_test_queue)
            return state
        _test_queue.append({"source_id": source_id, "force": bool(force),
                            "trigger": trigger, "kind": kind, "mode": mode})
        spawn = not _test_state.get("running")
        if spawn:
            _test_state.update({"running": True, "source_id": source_id,
                                "done": 0, "total": 0,
                                "started_at": time.time(), "message": "",
                                "trigger": trigger,
                                "queued": len(_test_queue) - 1})
        else:
            _test_state["queued"] = len(_test_queue)
        state = dict(_test_state)
        state["enqueued"] = True
    if spawn:
        thread = threading.Thread(target=_chain_worker, daemon=True)
        thread.start()
    return state


def start_probe(source_id, mode="light", trigger="daily"):
    """Enqueue a liveness probe for an anomaly-paused source, with
    the same one-job-per-source dedupe as start_tests."""
    return start_tests(source_id, force=False, trigger=trigger,
                       mode=mode, kind="probe")


def _sweep(mode, trigger):
    """One scheduled round over every source. Healthy sources get a
    test job in the sweep's mode; anomaly-paused sources get a bare
    liveness probe instead (a single subscription fetch, no node
    tests), so a dead source stops costing test traffic until it
    comes back on its own. Returns (enqueued_ids, probed_ids)."""
    with _lock:
        store = _load_store()
        plan = [(s["id"], bool(s.get("anomaly"))) for s in store["sources"]]
    enqueued, probed = [], []
    for sid, paused in plan:
        if paused:
            state = start_probe(sid, mode=mode, trigger=trigger)
        else:
            state = start_tests(sid, force=(mode == "full"),
                                trigger=trigger, mode=mode)
        if state.get("enqueued"):
            (probed if paused else enqueued).append(sid)
    return enqueued, probed


def _register_source(store, url, label):
    """The one add-source pipeline, shared by the paste-add endpoint
    and the link-file import: build the record, fetch + convert,
    persist, and auto-test a successfully converted source. The
    caller holds _lock. A failed first fetch still registers the
    source with last_error set — exactly what the paste-add path has
    always done — and the caller decides how to report it."""
    parsed = urlparse(url)
    src = {"id": secrets.token_hex(4), "label": label, "url": url,
           "host": parsed.hostname, "token": secrets.token_hex(12),
           "created": time.time(), "updated": None, "nodes": {},
           "node_count": 0, "last_error": None}
    try:
        refresh_source(src)
    except Exception as exc:
        src["last_error"] = str(exc)[:200]
        src["updated"] = time.time()
    store["sources"].append(src)
    _save_store(store)
    state = None
    if not src["last_error"]:
        state = start_tests(src["id"], force=False, trigger="auto-add")
    return src, state


_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]）】」]+")
_URL_TRAIL = ",.;，。；：、!！?？*\"'"


def _extract_urls(text):
    """Extract subscription URLs from imported file text: every line
    is regex-scanned (a bare-URL line and a URL embedded in prose are
    both caught), hits are de-duplicated in first-seen order with
    trailing prose punctuation trimmed. Returns (urls, junk_lines),
    where junk_lines counts non-empty lines that yielded no URL."""
    urls, seen, junk = [], set(), 0
    for line in text.splitlines():
        found = _URL_RE.findall(line)
        if not found:
            if line.strip():
                junk += 1
            continue
        for u in found:
            u = u.rstrip(_URL_TRAIL)
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
    return urls, junk


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


def _is_loopback_direct(environ):
    """True only for a direct call from the box itself: the peer is
    loopback AND the request carries no proxy forwarding headers
    (requests arriving via the panel /web proxy are also loopback at
    the TCP level, but the panel stamps X-Forwarded-For / X-Hub-Passed
    on them, so they are told apart here)."""
    if (environ.get("REMOTE_ADDR") or "") not in ("127.0.0.1", "::1"):
        return False
    if environ.get("HTTP_X_FORWARDED_FOR") or environ.get("HTTP_X_HUB_PASSED"):
        return False
    return True


def _auto_refresh_stale(store):
    now = time.time()
    changed = False
    for src in store["sources"]:
        if now - (src.get("updated") or 0) > STALE_SECONDS:
            try:
                refresh_source(src)
            except Exception as exc:
                src["last_error"] = str(exc)[:200]
                src["updated"] = now
            changed = True
    if changed:
        _save_store(store)


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO") or "/"

    if path == "/" and method == "GET":
        body = PAGE.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    if path == "/api/session" and method == "GET":
        return _json(start_response, "200 OK",
                     {"ok": True, "authenticated": True, "via": "platform",
                      "username": "platform"})

    if path == "/api/sources" and method == "GET":
        with _lock:
            store = _load_store()
            _auto_refresh_stale(store)
            return _json(start_response, "200 OK",
                         {"ok": True,
                          "sources": [_public_source(s) for s in store["sources"]],
                          "recent_runs": _recent_runs(5)})

    if path == "/api/sources" and method == "POST":
        data = _read_json(environ)
        url = str(data.get("url") or "").strip()
        label = str(data.get("label") or "").strip()[:60]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "error": "bad_url", "message": "请填写完整的 http(s) 订阅链接。"})
        with _lock:
            store = _load_store()
            src, state = _register_source(store, url, label)
            return _json(start_response, "200 OK",
                         {"ok": True, "source": _public_source(src),
                          "test_state": state})

    if path == "/api/sources/import" and method == "POST":
        # Link-file import: the page sends the picked file as base64;
        # decoding (with a visible mojibake warning) and URL parsing
        # happen here. Each link then goes through the exact paste-add
        # pipeline; URLs already registered are skipped and reported
        # with their existing converted link.
        data = _read_json(environ, limit=4 * 1024 * 1024)
        try:
            raw = base64.b64decode(str(data.get("content_b64") or ""),
                                   validate=True)
        except Exception:
            raw = None
        if raw is None:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "error": "bad_file",
                          "message": "文件内容读取失败，请重新选择文件。"})
        if len(raw) > 2 * 1024 * 1024:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "error": "too_large",
                          "message": "文件超过 2 MB 上限。"})
        text = raw.decode("utf-8", errors="replace")
        decode_warning = "�" in text
        urls, junk = _extract_urls(text)
        items = []
        added = duplicates = failed = 0
        with _lock:
            store = _load_store()
            by_url = {s["url"]: s for s in store["sources"]}
            for u in urls:
                host = urlparse(u).hostname or ""
                if u in by_url:
                    old = by_url[u]
                    duplicates += 1
                    items.append({
                        "status": "duplicate", "host": host,
                        "label": old.get("label") or old.get("host") or "",
                        "link": "%s/sub/conv/%s.yaml" % (PUBLIC_BASE,
                                                         old["token"])})
                    continue
                src, _state = _register_source(store, u, "")
                by_url[u] = src
                if src["last_error"]:
                    failed += 1
                    items.append({"status": "failed", "host": host,
                                  "label": src.get("label") or host,
                                  "error": src["last_error"]})
                else:
                    added += 1
                    items.append({
                        "status": "added", "host": host,
                        "label": src.get("label") or host,
                        "link": "%s/sub/conv/%s.yaml" % (PUBLIC_BASE,
                                                         src["token"])})
        return _json(start_response, "200 OK",
                     {"ok": True, "found": len(urls), "added": added,
                      "duplicates": duplicates, "failed": failed,
                      "skipped_lines": junk,
                      "decode_warning": decode_warning, "items": items})

    if path.startswith("/api/sources/") and method == "GET":
        parts = path.split("/")
        if len(parts) == 5 and parts[4] == "results.csv":
            with _lock:
                store = _load_store()
                src = _find_source(store, parts[3])
            if src is None:
                return _json(start_response, "404 Not Found",
                             {"ok": False, "error": "not_found", "message": "订阅源不存在。"})
            import csv as _csv
            import io as _io
            buf = _io.StringIO()
            writer = _csv.writer(buf)
            writer.writerow(["node_name", "display_name", "status",
                             "egress_ip", "egress_country", "egress_region",
                             "egress_asn", "egress_org", "ip_type",
                             "speed_mbps", "latency_ms", "youtube",
                             "success_rate", "fail_streak", "is_china_optimized",
                             "fraud_score"])
            status_map = {"正常": "ok", "已判死": "dead", "已消失": "gone"}
            for entry in (src.get("nodes") or {}).values():
                d = _node_detail(entry)
                writer.writerow([
                    d["name"], d["display"] or "",
                    status_map.get(d["status"], "untested"),
                    d["egress_ip"] or "", d["country"] or "", d["city"] or "",
                    d["asn"] or "", d["org"] or "", d["ip_type"] or "",
                    d["speed_mbps"] if d["speed_mbps"] is not None else "",
                    d["latency_ms"] if d["latency_ms"] is not None else "",
                    "" if d["youtube"] is None else ("yes" if d["youtube"] else "no"),
                    d["success_rate"] if d["success_rate"] is not None else "",
                    d["fail_streak"], "yes" if d["china_opt"] else "no",
                    d["quality_score"] if d["quality_score"] is not None else ""])
            body = buf.getvalue().encode("utf-8")
            fname = (src.get("label") or src.get("host") or "source") + "-results.csv"
            start_response("200 OK", [
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition", 'attachment; filename="results.csv"; filename*=UTF-8\'\'' + urllib.parse.quote(fname)),
                ("Content-Length", str(len(body)))])
            return [body]
        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "not_found", "message": "接口不存在。"})

    if path.startswith("/api/sources/") and method == "POST":
        parts = path.split("/")
        if len(parts) == 5:
            sid, action = parts[3], parts[4]
            with _lock:
                store = _load_store()
                src = _find_source(store, sid)
                if src is None:
                    return _json(start_response, "404 Not Found",
                                 {"ok": False, "error": "not_found", "message": "订阅源不存在。"})
                if action == "delete":
                    store["sources"].remove(src)
                    _save_store(store)
                    try:
                        os.unlink(os.path.join(OUTPUT_DIR, src["token"] + ".yaml"))
                    except OSError:
                        pass
                    return _json(start_response, "200 OK", {"ok": True})
                if action == "test":
                    data = _read_json(environ)
                    state = start_tests(sid, force=bool(data.get("force")))
                    return _json(start_response, "200 OK",
                                 {"ok": True, "test_state": state})
                if action in ("refresh", "force"):
                    try:
                        refresh_source(src, force=(action == "force"))
                    except Exception as exc:
                        src["last_error"] = str(exc)[:200]
                        src["updated"] = time.time()
                    _save_store(store)
                    return _json(start_response, "200 OK",
                                 {"ok": True, "source": _public_source(src)})
        return _json(start_response, "404 Not Found",
                     {"ok": False, "error": "not_found", "message": "接口不存在。"})

    if path == "/internal/daily-tests" and method == "POST":
        # Box-local trigger for the daily systemd timer: a light
        # round (connectivity / latency / egress identity, no speed
        # downloads; speed figures carry over from the weekly full
        # round) over every registered source, through the same
        # queue/pipeline as the page button. Anomaly-paused sources
        # get a bare liveness probe instead. Loopback-only (see
        # _is_loopback_direct); the platform gate keeps this path
        # unreachable from outside.
        if not _is_loopback_direct(environ):
            return _json(start_response, "403 Forbidden",
                         {"ok": False, "error": "forbidden",
                          "message": "仅限盒上回环直连调用。"})
        enqueued, probed = _sweep("light", "daily")
        return _json(start_response, "200 OK",
                     {"ok": True, "mode": "light",
                      "enqueued": enqueued, "probed": probed,
                      "test_state": dict(_test_state)})

    if path == "/internal/weekly-tests" and method == "POST":
        # Box-local trigger for the weekly systemd timer: the full
        # round (with speed downloads) that refreshes the speed
        # figures the daily light rounds preserve. Same loopback-only
        # admission as the daily endpoint.
        if not _is_loopback_direct(environ):
            return _json(start_response, "403 Forbidden",
                         {"ok": False, "error": "forbidden",
                          "message": "仅限盒上回环直连调用。"})
        enqueued, probed = _sweep("full", "weekly")
        return _json(start_response, "200 OK",
                     {"ok": True, "mode": "full",
                      "enqueued": enqueued, "probed": probed,
                      "test_state": dict(_test_state)})

    return _json(start_response, "404 Not Found",
                 {"ok": False, "error": "not_found", "message": "路径不存在。"})


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>订阅转化</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f4f6fa; color: #1d2433; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 18px 14px 60px; }
  h1 { font-size: 20px; margin: 6px 0 4px; }
  .sub { color: #68707f; font-size: 13px; margin-bottom: 16px; line-height: 1.6; }
  .card { background: #fff; border: 1px solid #e2e7f0; border-radius: 14px; padding: 14px; margin-bottom: 12px; }
  label { display: block; font-size: 12px; color: #68707f; margin: 8px 0 4px; }
  input[type=text], input[type=url] { width: 100%; padding: 9px 10px; border: 1px solid #ccd4e2;
    border-radius: 9px; font-size: 14px; }
  button { padding: 8px 14px; border-radius: 9px; border: 1px solid #ccd4e2; background: #fff;
    font-size: 13px; cursor: pointer; }
  button.primary { background: #2f6fd0; border-color: #2f6fd0; color: #fff; }
  button.danger { color: #b3372f; border-color: #e5c1bd; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .title { font-weight: 600; font-size: 15px; }
  .meta { color: #68707f; font-size: 12px; margin-top: 3px; line-height: 1.7; }
  .err { color: #b3372f; }
  .linkbox { display: flex; gap: 8px; margin-top: 10px; }
  .linkbox input { flex: 1; font-size: 12px; color: #334; background: #f7f9fc; }
  .msg { font-size: 13px; color: #2c7a3f; min-height: 18px; margin-top: 8px; }
  .empty { color: #68707f; font-size: 14px; padding: 22px 0; text-align: center; }
  a.btnlink { display: inline-block; padding: 8px 14px; border-radius: 9px; border: 1px solid #ccd4e2;
    background: #fff; font-size: 13px; color: #1d2433; text-decoration: none; }
  .detailbox { overflow-x: auto; margin-top: 12px; }
  .detailbox table { border-collapse: collapse; font-size: 12px; white-space: nowrap; min-width: 760px; }
  .detailbox th, .detailbox td { padding: 5px 8px; border-bottom: 1px solid #edf0f6; text-align: left;
    vertical-align: top; }
  .detailbox th { color: #68707f; font-weight: 600; }
  .detailbox .orig { color: #8a93a5; font-size: 11px; }
  .st-ok { color: #2c7a3f; } .st-bad { color: #b3372f; } .st-wait { color: #a86a12; }
</style>
</head>
<body>
<div class="wrap">
  <h1>订阅转化</h1>
  <div class="sub">粘贴机场订阅链接，生成一个只含真实节点的新链接：信息条目和重复节点会被去掉，
  规则换成你自己的分流规则。每个源一个独立链接。源订阅更新后这里会自动跟进（已登记的节点跳过、不重复处理），
  也可以手动刷新；强制重新处理会清空登记表重建。源链接只保存在服务器上，页面不会再显示。</div>

  <div class="card">
    <label>备注名（可选，如：99吧）</label>
    <input id="f-label" type="text" maxlength="60" placeholder="备注名">
    <label>订阅链接</label>
    <input id="f-url" type="url" placeholder="https://…">
    <div class="row" style="margin-top:12px">
      <button id="f-add" class="primary" type="button">生成新链接</button>
      <span id="f-msg" class="msg"></span>
    </div>
    <div class="meta" style="margin-top:14px">或导入链接文件：一个文本文件里有多少订阅链接就导多少（每行一个、夹在文字里都可以），已注册的会跳过并给出原转化链接。</div>
    <div class="row" style="margin-top:6px">
      <input id="f-file" type="file" accept=".txt,.list,.md,text/plain">
      <button id="f-import" type="button">导入文件</button>
      <span id="f-import-msg" class="msg"></span>
    </div>
    <div id="import-result" style="margin-top:10px"></div>
  </div>

  <div class="card">
    <div class="title" style="font-weight:600">VPS 自建订阅（DediRock）</div>
    <div class="meta">自建节点的订阅链接（3 个节点：AnyTLS IPv6 / AnyTLS IPv4 / Hysteria2 IPv6）。
    默认是 IPv6 入口；IPv6 拉不到时把主机名 debi 换成 debi4 走 IPv4 即可。</div>
    <div class="linkbox">
      <input type="text" readonly value="https://debi.su7.dpdns.org/sub/6f09ed75fc16a785f191d741146f3fc7.yaml">
      <button type="button" class="copy" id="vps-copy">复制链接</button>
    </div>
  </div>

  <div class="sub" id="runs-line"></div>
  <div id="list"></div>
</div>
<script>
const $ = (id) => document.getElementById(id);
$("vps-copy").onclick = async () => {
  const input = $("vps-copy").parentElement.querySelector("input");
  try { await navigator.clipboard.writeText(input.value); $("vps-copy").textContent = "已复制"; }
  catch (e) { input.select(); document.execCommand("copy"); $("vps-copy").textContent = "已复制"; }
  setTimeout(() => { $("vps-copy").textContent = "复制链接"; }, 1500);
};
async function api(path, method, payload) {
  const opts = {method: method || "GET", headers: {}};
  if (payload) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(payload); }
  const r = await fetch(path, opts);
  return r.json();
}
function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function detailTable(rows) {
  if (!rows || !rows.length) return '<div class="meta">暂无节点记录。</div>';
  const head = ["节点", "状态", "出口 IP", "地区", "ASN / 组织", "IP质量", "速度", "延迟", "YouTube", "成功率"];
  let html = "<table><tr>" + head.map((h) => `<th>${h}</th>`).join("") + "</tr>";
  for (const d of rows) {
    const stCls = d.status === "正常" ? "st-ok" : ((d.status === "已判死" || d.status === "已消失") ? "st-bad" : "st-wait");
    const name = (d.display && d.display !== d.name)
      ? `${esc(d.display)}<br><span class="orig">${esc(d.name)}</span>` : esc(d.name);
    const geo = [d.city, d.country].filter(Boolean).join(" · ");
    const asn = [d.asn, d.org].filter(Boolean).join(" ");
    const spd = d.speed_mbps == null ? "—" : d.speed_mbps + " Mbps";
    const lat = d.latency_ms == null ? "—" : d.latency_ms + " ms";
    const yt = d.youtube == null ? "—" : (d.youtube ? "通" : "不通");
    const sr = d.success_rate == null ? "—" : Math.round(d.success_rate * 100) + "% (" + d.successes + "/" + d.attempts + ")";
    html += "<tr>" + [name, `<span class="${stCls}">${esc(d.status)}</span>`, esc(d.egress_ip || "—"),
      esc(geo || "—"), esc(asn || "—"), esc(d.ip_type || "—"), spd, lat, yt, sr]
      .map((c) => `<td>${c}</td>`).join("") + "</tr>";
  }
  return html + "</table>";
}
function fmtTime(ts) {
  if (!ts) return "尚未拉取";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", {hour12: false});
}
function render(sources) {
  const box = $("list");
  if (!sources.length) { box.innerHTML = '<div class="empty">还没有订阅源。在上面粘贴一个链接开始。</div>'; return; }
  box.innerHTML = "";
  for (const s of sources) {
    const card = document.createElement("div");
    card.className = "card";
    let meta = `源站：${s.host} · 节点 ${s.node_count} 个（已登记 ${s.registered}，已消失 ${s.gone}）`;
    meta += `<br>上次更新：${fmtTime(s.updated)}`;
    if (s.last_added || s.last_gone) meta += ` · 上次变化：新增 ${s.last_added}，消失 ${s.last_gone}`;
    if (s.dropped_info || s.dropped_dup) meta += `<br>过滤：不可用条目 ${s.dropped_info} 个，重复 ${s.dropped_dup} 个`;
    const ui = s.userinfo;
    if (ui && ui.total) {
      const gb = (b) => (b / 1e9).toFixed(1);
      let line = `<br>流量：剩余 ${gb(ui.remaining || 0)} GB / 共 ${gb(ui.total)} GB`;
      if (ui.expire) line += ` · 到期 ${new Date(ui.expire * 1000).toLocaleDateString("zh-CN")}`;
      meta += line;
    }
    if (s.info_count) meta += `<br>机场信息节点：${s.info_count} 条（原名保留，在节点选择末尾）`;
    const ps = s.plan_stats || {};
    if (s.tested) meta += `<br>出口实测：正常 ${ps.ok || 0} · 待中国实测 ${ps.china || 0} · 未测 ${ps.untested || 0}` + ((ps.dropped_dead || ps.dropped_dup_ok) ? ` · 已剔除 死节点 ${ps.dropped_dead || 0} / 同出口重复 ${ps.dropped_dup_ok || 0}` : "");
    if (s.test_state && s.test_state.running) meta += `<br>正在测试：${s.test_state.done}/${s.test_state.total}…`;
    else if (s.test_state && s.test_state.queued) meta += `<br>测试已排队，等待自动开始…`;
    else if (s.test_state && s.test_state.message) meta += `<br>${s.test_state.message}`;
    if (s.last_error) meta += `<br><span class="err">最近一次拉取失败：${s.last_error}</span>`;
    if (s.anomaly) meta += `<br><span class="err">源异常：${esc(s.anomaly.kind_label || s.anomaly.kind)}（自 ${fmtTime(s.anomaly.since)} 起，连续 ${s.anomaly.rounds} 轮）· 已暂停自动测试，源恢复后自动继续；上方链接仍是最后一次成功版本</span>`;
    card.innerHTML = `
      <div class="title"></div>
      <div class="meta">${meta}</div>
      <div class="linkbox">
        <input type="text" readonly value="${s.link}">
        <button type="button" class="copy">复制链接</button>
      </div>
      <div class="row" style="margin-top:10px">
        <button type="button" class="detail">实测明细</button>
        <a class="btnlink csv" target="_blank" rel="noopener">下载CSV</a>
        <button type="button" class="refresh">刷新</button>
        <button type="button" class="test">测试未测节点</button>
        <button type="button" class="retest">强制重测</button>
        <button type="button" class="force">强制重新处理</button>
        <button type="button" class="danger del">删除</button>
        <span class="msg opmsg"></span>
      </div>
      <div class="detailbox" hidden></div>`;
    card.querySelector(".title").textContent = s.label || s.host;
    const msg = card.querySelector(".opmsg");
    card.querySelector(".copy").onclick = async () => {
      const inp = card.querySelector(".linkbox input");
      try { await navigator.clipboard.writeText(inp.value); msg.textContent = "已复制。"; }
      catch (e) { inp.select(); document.execCommand("copy"); msg.textContent = "已复制。"; }
    };
    card.querySelector(".csv").href = `api/sources/${s.id}/results.csv`;
    const dbox = card.querySelector(".detailbox");
    dbox.innerHTML = detailTable(s.nodes_detail);
    card.querySelector(".detail").onclick = () => { dbox.hidden = !dbox.hidden; };
    card.querySelector(".refresh").onclick = () => act(s.id, "refresh", msg, "已刷新。");
    card.querySelector(".test").onclick = async () => {
      msg.textContent = "测试已开始，进度会自动刷新…";
      await api(`api/sources/${s.id}/test`, "POST", {});
      poll();
    };
    card.querySelector(".retest").onclick = async () => {
      if (!confirm("强制重测会重新测试该源全部节点（消耗机场流量），确定？")) return;
      msg.textContent = "重测已开始，进度会自动刷新…";
      await api(`api/sources/${s.id}/test`, "POST", {force: true});
      poll();
    };
    card.querySelector(".force").onclick = () => {
      if (confirm("强制重新处理会清空该源的节点登记表并重建，确定？")) act(s.id, "force", msg, "已重新处理。");
    };
    card.querySelector(".del").onclick = () => {
      if (confirm("删除这个订阅源和它生成的新链接？")) act(s.id, "delete", msg, "");
    };
    box.appendChild(card);
  }
}
async function act(id, action, msg, okText) {
  msg.textContent = "处理中…";
  const d = await api(`api/sources/${id}/${action}`, "POST", {});
  if (!d.ok) { msg.textContent = "失败：" + (d.message || action); return; }
  if (okText) msg.textContent = okText;
  load();
}
let pollTimer = null;
async function load() {
  const d = await api("api/sources");
  if (d.ok) {
    render(d.sources);
    const lr = (d.recent_runs || []).slice(-1)[0];
    if (lr) {
      const trig = {daily: "每日轻测", weekly: "每周全量", "auto-add": "新源自动", manual: "手动"}[lr.trigger] || lr.trigger;
      $("runs-line").textContent = `最近一轮测试（${trig}）：${lr.label || lr.source_id} · 测 ${lr.tested} 个，正常 ${lr.ok} 个 · 用时 ${lr.duration_s}s · 测速流量约 ${(lr.est_bytes / 1e6).toFixed(1)} MB · ${fmtTime(lr.finished_at)}`;
    }
    const running = d.sources.some((x) => x.test_state && (x.test_state.running || x.test_state.queued));
    if (running && !pollTimer) pollTimer = setInterval(load, 4000);
    if (!running && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
}
function poll() { if (!pollTimer) pollTimer = setInterval(load, 4000); load(); }
function renderImport(d) {
  const box = $("import-result");
  let head = `导入汇总：共找到链接 ${d.found} 个 · 新增 ${d.added} · 重复跳过 ${d.duplicates} · 失败 ${d.failed} · 跳过杂讯行 ${d.skipped_lines}`;
  if (d.decode_warning) head += `<br><span class="err">注意：文件含非 UTF-8 字节，已容错解码，个别字符可能损坏。</span>`;
  let html = `<div class="meta">${head}</div>`;
  for (const it of d.items || []) {
    if (it.status === "failed") {
      html += `<div class="meta"><span class="err">✗ 失败</span> ${esc(it.label || it.host)}：${esc(it.error || "")}</div>`;
    } else {
      const tag = it.status === "duplicate" ? "已存在，跳过（原转化链接）" : "✓ 新增";
      html += `<div class="meta">${tag} ${esc(it.label || it.host)}</div>` +
        `<div class="linkbox"><input type="text" readonly value="${esc(it.link)}"><button type="button" class="imp-copy">复制链接</button></div>`;
    }
  }
  box.innerHTML = html;
  box.querySelectorAll(".imp-copy").forEach((b) => {
    b.onclick = async () => {
      const inp = b.parentElement.querySelector("input");
      try { await navigator.clipboard.writeText(inp.value); b.textContent = "已复制"; }
      catch (e) { inp.select(); document.execCommand("copy"); b.textContent = "已复制"; }
      setTimeout(() => { b.textContent = "复制链接"; }, 1500);
    };
  });
}
$("f-import").onclick = async () => {
  const msg = $("f-import-msg");
  const file = $("f-file").files[0];
  if (!file) { msg.textContent = "先选择一个文本文件。"; return; }
  if (file.size > 2 * 1024 * 1024) { msg.textContent = "文件超过 2 MB 上限。"; return; }
  msg.textContent = "正在导入…";
  const bytes = new Uint8Array(await file.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 32768) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 32768));
  const d = await api("api/sources/import", "POST", {content_b64: btoa(bin)});
  if (!d.ok) { msg.textContent = "失败：" + (d.message || ""); return; }
  msg.textContent = "导入完成，转化链接见下方汇总。";
  renderImport(d);
  load();
  poll();
};
$("f-add").onclick = async () => {
  const url = $("f-url").value.trim();
  if (!url) { $("f-msg").textContent = "先粘贴订阅链接。"; return; }
  $("f-msg").textContent = "正在拉取并生成…";
  const d = await api("api/sources", "POST", {label: $("f-label").value.trim(), url});
  if (!d.ok) { $("f-msg").textContent = "失败：" + (d.message || ""); return; }
  $("f-url").value = ""; $("f-label").value = "";
  $("f-msg").textContent = d.source.last_error ? ("已保存，但首次拉取失败：" + d.source.last_error) : (d.test_state ? "已生成，新源已自动开始测试，进度见下方卡片。" : "已生成，去下面复制新链接。");
  if (d.test_state) poll(); else load();
};
load();
</script>
</body>
</html>
"""
