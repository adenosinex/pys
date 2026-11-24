# -*- coding: utf-8 -*-
"""巫山客流预测：12306 采集 + 评分 + SQLite 存储（共享核心，供 app.py 与 collect.py 复用）.

设计要点（按 2026-11-24 修订后的最终方案）：
- 客源地 5 个核验点（CUW/ICW/HKN/HAN/ABN），解析按城市归类，不硬过滤单站码。
- 限售对照：每方向只多发 1 次干线终点查询并查表复用；疑似锁票降权 0.2，不直接踢掉。
- 评分：双边 > 70 即判“高”，提前 >=3 天破 70 标“极高”；去程窗口周五 17:00 后 + 周六 12:00 前。
- 异常车次：座位状态为 '*'（未起售）或停运车次直接排除，不计 100 分。
- 存储：daily_index 主键 (weekend_date, crawl_date, lead_days)，另记 valid_trains。
- 出口（用户定，2026-11-24）：服务器之间不直接 SSH，改走网络接口 + HTTPS。
  采集器经 hwd HTTPS API（DATA_DIR/api_url + api_token）查 12306，
  hwd 在本机直查并返回结果；未配 API 时才回落到代理/直连。
"""
import json
import os
import random
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(BASE, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(BASE, ".runtime")


DATA_DIR = _data_dir()
DB_PATH = os.path.join(DATA_DIR, "wushan_flow.db")
CREDS_PATH = os.path.join(DATA_DIR, "credentials.json")


def _api_config():
    """hwd 12306 查询 HTTPS API（用户定：服务器之间不直接 SSH，改走网络接口 + HTTPS）。

    DATA_DIR/api_url（如 https://139.9.100.221/wushan-12306）与
    DATA_DIR/api_token（Bearer token）同时存在时，查询改调该 API，
    由 hwd 在本机直接查 12306 并返回结果；采集器不再直连 12306，
    也不走代理。任一文件不存在或为空 = 不用 API。
    """
    try:
        with open(os.path.join(DATA_DIR, "api_url"), "r", encoding="utf-8") as f:
            url = f.read().strip()
    except OSError:
        return None
    try:
        with open(os.path.join(DATA_DIR, "api_token"), "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    if not (url and token):
        return None
    return (url.rstrip("/"), token)


def _query_via_api(api_url, api_token, frm, to, date_s):
    """经 hwd HTTPS API 查一次余票，返回 data.result 原始字符串列表。"""
    import ssl
    payload = json.dumps({
        "from_station": frm, "to_station": to, "date": date_s,
    }).encode("utf-8")
    req = urllib.request.Request(
        api_url + "/query", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_token})
    # hwd 用 IP 证书（139.9.100.221），urllib 校验 IP SAN；自签/过期则仍校验
    # 主机名——此处用 IP 直连，证书 CN/SAN 为该 IP，标准校验即可。
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    if not data.get("ok"):
        raise RuntimeError("hwd API 查询失败: %s" % (data.get("error") or ""))
    return data.get("result") or []


def _proxy_url():
    """12306 出口代理（如 hwd tinyproxy / SSH SOCKS），见 DATA_DIR/proxy_url。

    仅当未配置 hwd HTTPS API（_api_config）时作为后备。
    文件不存在或为空 = 不走代理（直连）。
    文件内容示例：http://127.0.0.1:18888 或 socks5://127.0.0.1:11080。
    注意：12306 的 IPv6 端点不稳，SOCKS 方式必须强制 IPv4。
    """
    try:
        with open(os.path.join(DATA_DIR, "proxy_url"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _socks_https_handler(proxy_url):
    """返回一个经 SOCKS5 代理的 urllib HTTPS handler（强制 IPv4）。

    12306 的 IPv6 解析会走不通（SOCKS 连 IPv6 地址失败），因此解析目标主机名时
    只取 IPv4 地址建 TCP 连接，TLS 仍用原主机名做 SNI/证书校验。
    """
    import http.client
    import socket

    parsed = urllib.parse.urlparse(proxy_url)
    socks_host = parsed.hostname or "127.0.0.1"
    socks_port = parsed.port or 1080

    class SocksHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            import socks
            # 只取 IPv4，避免 SOCKS 连 IPv6 失败。
            infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET,
                                       socket.SOCK_STREAM)
            if not infos:
                raise OSError("no IPv4 address for %s" % self.host)
            ipv4 = infos[0][4][0]
            sock = socks.socksocket()
            sock.set_proxy(socks.SOCKS5, socks_host, socks_port)
            sock.connect((ipv4, self.port))
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class SocksHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(SocksHTTPSConnection, req)

    return SocksHTTPSHandler()

# ---------------- 城市与车站 ----------------
# 12306 查询会带出同城站车次（如查 CUW 会同时返回 COE/CXW），解析时按城市归类。
CITY_STATIONS = {
    "重庆": {"CUW", "CXW", "COE"},
    "成都": {"ICW", "CDW"},
    "武汉": {"HKN", "WHN", "WCN"},
    "宜昌": {"HAN", "ABN"},
}
STATION_TO_CITY = {}
for _city, _codes in CITY_STATIONS.items():
    for _c in _codes:
        STATION_TO_CITY[_c] = _city

# 客源地核验点（代表站码；查询后按城市聚合去重）
ORIGIN_CODES = ["CUW", "ICW", "HKN", "HAN", "ABN"]
WOE = "WOE"  # 巫山

# 限售对照：每客源城市只多发 1 次干线终点查询，查表复用。
# key 为客源城市，value 为对照终点站码。
CONTROL_DEST = {
    "重庆": "ZAF",   # 重庆北 -> 郑州东
    "成都": "ZAF",   # 成都东 -> 郑州东
    "武汉": "CUW",   # 汉口 -> 重庆北
    "宜昌": "CUW",   # 宜昌东/宜昌北 -> 重庆北
}

# ---------------- 12306 客户端 ----------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# 12306 result 字段下标（实测 2026-11-24，shoc）：
# [3]=车次, [6]=出发站码, [7]=到达站码, [8]=出发时间, [9]=到达时间,
# [26]=无座, [30]=二等座, [31]=一等座
F_TRAIN = 3
F_FROM = 6
F_TO = 7
F_DEP = 8
F_ARR = 9
F_NOSEAT = 26
F_SECOND = 30
F_FIRST = 31


class Ticket12306:
    """12306 查询客户端。
    实测（2026-11-24，shoc）：init 拿到的会话/动态查询地址基本只够用一次，
    同一会话的第二次查询会返回 HTML 错误页。因此每次查询都用全新会话
    （新 CookieJar + 重新 init），宁可多一次请求，也要稳定。
    """

    def _headers(self, referer=None):
        h = {"User-Agent": UA}
        if referer:
            h["Referer"] = referer
        return h

    def query(self, frm, to, date_s, retries=2):
        """一次完整查询：优先走 hwd HTTPS API；否则新会话 -> init -> 查余票。
        返回 data.result 原始字符串列表；失败重试两次（退避 2s/5s）后抛异常。
        12306 会间歇性返回 HTML 错误页（疑似限流），重试 + 拉长间隔是常态。"""
        last_exc = None
        for attempt in range(retries + 1):
            try:
                api = _api_config()
                if api:
                    # hwd HTTPS API：hwd 本机直查 12306，采集器不再直连。
                    return _query_via_api(api[0], api[1], frm, to, date_s)
                import http.cookiejar
                cj = http.cookiejar.CookieJar()
                handlers = [urllib.request.HTTPCookieProcessor(cj)]
                proxy = _proxy_url()
                if proxy:
                    if proxy.startswith("socks5://"):
                        # SOCKS5（如 SSH -D 到 hwd），强制 IPv4。
                        handlers.append(_socks_https_handler(proxy))
                    else:
                        # HTTP 代理（如 hwd tinyproxy），12306 看到代理的 IP。
                        handlers.append(urllib.request.ProxyHandler(
                            {"http": proxy, "https": proxy}))
                op = urllib.request.build_opener(*handlers)
                init_url = (
                    "https://kyfw.12306.cn/otn/leftTicket/init"
                    "?linktypeid=dc&fs={}&ts={}&date={}&flag=N,N,Y"
                    .format(frm, to, date_s)
                )
                req = urllib.request.Request(init_url, headers=self._headers())
                with op.open(req, timeout=20) as r:
                    html = r.read().decode("utf-8-sig", "replace")
                m = re.search(r"var CLeftTicketUrl\s*=\s*'([^']+)'", html)
                if not m:
                    raise RuntimeError("init 未返回 CLeftTicketUrl")
                params = {
                    "leftTicketDTO.train_date": date_s,
                    "leftTicketDTO.from_station": frm,
                    "leftTicketDTO.to_station": to,
                    "purpose_codes": "ADULT",
                }
                url = ("https://kyfw.12306.cn/otn/" + m.group(1) + "?"
                       + urllib.parse.urlencode(params))
                req = urllib.request.Request(
                    url, headers=self._headers(
                        "https://kyfw.12306.cn/otn/leftTicket/init"))
                with op.open(req, timeout=20) as r:
                    raw = r.read()
                try:
                    data = json.loads(raw.decode("utf-8-sig", "replace"))
                except ValueError:
                    raise RuntimeError(
                        "12306 返回非 JSON（前 120 字节）: %r" % raw[:120])
                if not data.get("status"):
                    raise RuntimeError("12306 查询 status=False: %s"
                                       % (data.get("messages") or ""))
                return (data.get("data") or {}).get("result") or []
            except Exception as exc:  # noqa: BLE001 - 重试后统一上抛
                last_exc = exc
                time.sleep(2 if attempt == 0 else 5)
        raise RuntimeError("12306 查询失败: %s" % last_exc)


def _seat_value(raw):
    """把余票原始值归一化：'有'->inf, '无'->0, 数字->int, '*'/'--'/''->None（异常/未起售）。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "--", "*"):
        return None
    if s == "有":
        return 999
    if s == "无":
        return 0
    try:
        return int(s)
    except ValueError:
        return None


def _train_score(second_raw, first_raw):
    """单车次紧缺分：双无 100，仅二等无 75，余 1-20 张 40，有 0。None 表示异常车次，返回 None 排除。"""
    second = _seat_value(second_raw)
    first = _seat_value(first_raw)
    if second is None and first is None:
        return None
    if second == 0 and first == 0:
        return 100
    if second == 0:
        return 75
    if second is not None and 0 < second <= 20:
        return 40
    if second is not None and second > 20:
        return 0
    # 二等座无数据但一等座有数据时，按一等座判断
    if first == 0:
        return 75
    if first is not None and 0 < first <= 20:
        return 40
    return 0


def _is_short_or_terminating(train_no, frm_code, to_code):
    """短途/城际车（C 字头）或巫山始发/终到车，加权 1.3-1.5 倍。"""
    if train_no.startswith("C"):
        return True
    if frm_code == WOE or to_code == WOE:
        # 巫山站本身就是起讫点之一的车次（短途为主）
        return True
    return False


# ---------------- 目标周末 ----------------
def next_saturday(today=None):
    """目标周六：若今天是周六用今天，周日用下周六，其余用本周六。"""
    today = today or date.today()
    wd = today.weekday()  # Mon=0 ... Sun=6
    if wd == 5:
        return today
    if wd == 6:
        return today + timedelta(days=6)
    return today + timedelta(days=5 - wd)


def target_dates(saturday):
    """返回 (周五, 周六, 周日) 日期。"""
    return (saturday - timedelta(days=1), saturday, saturday + timedelta(days=1))


# ---------------- 存储 ----------------
def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = _connect()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crawl_time TEXT NOT NULL,
                target_date TEXT NOT NULL,
                direction TEXT NOT NULL,
                city TEXT NOT NULL,
                train_no TEXT NOT NULL,
                from_code TEXT, to_code TEXT,
                dep_time TEXT, arr_time TEXT,
                second_raw TEXT, first_raw TEXT,
                score INTEGER,
                weight REAL,
                locked INTEGER DEFAULT 0,
                UNIQUE(crawl_time, target_date, direction, train_no)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_index (
                weekend_date TEXT NOT NULL,
                crawl_date TEXT NOT NULL,
                lead_days INTEGER NOT NULL,
                inbound_score REAL,
                outbound_score REAL,
                level TEXT,
                valid_trains INTEGER,
                inbound_detail TEXT,
                outbound_detail TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (weekend_date, crawl_date, lead_days)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS crawl_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crawl_time TEXT NOT NULL,
                ok INTEGER NOT NULL,
                error TEXT,
                trains INTEGER DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                UNIQUE(kind, key)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        con.commit()
    finally:
        con.close()


def get_setting(key, default=""):
    con = _connect()
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?",
                          (key,)).fetchone()
        return row[0] if row and row[0] is not None else default
    finally:
        con.close()


def set_setting(key, value):
    con = _connect()
    try:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
        con.commit()
    finally:
        con.close()


def notified(kind, key):
    con = _connect()
    try:
        return con.execute(
            "SELECT 1 FROM notification_log WHERE kind=? AND key=?",
            (kind, key)).fetchone() is not None
    finally:
        con.close()


def mark_notified(kind, key):
    con = _connect()
    try:
        con.execute(
            "INSERT OR IGNORE INTO notification_log(kind,key,sent_at) "
            "VALUES(?,?,?)",
            (kind, key, datetime.now().isoformat(timespec="seconds")))
        con.commit()
    finally:
        con.close()


def last_successful_crawl():
    con = _connect()
    try:
        row = con.execute(
            "SELECT MAX(crawl_time) AS t FROM crawl_log WHERE ok=1"
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def consecutive_failures():
    """最近连续失败的采集轮数（按 crawl_log 倒序统计）。"""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT ok FROM crawl_log ORDER BY id DESC LIMIT 10"
        ).fetchall()
        n = 0
        for r in rows:
            if r[0] == 1:
                break
            n += 1
        return n
    finally:
        con.close()


def latest_index_for_weekend(weekend_date):
    """取指定目标周末最近一次的 daily_index（供单边缺失时沿用）。"""
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM daily_index WHERE weekend_date=? "
            "ORDER BY crawl_date DESC, created_at DESC LIMIT 1",
            (weekend_date,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def latest_index():
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM daily_index ORDER BY weekend_date DESC, "
            "crawl_date DESC, lead_days ASC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def index_history(limit=30):
    """按 (weekend_date, lead_days) 取历史，供周环比折线用。"""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT weekend_date, crawl_date, lead_days, inbound_score, "
            "outbound_score, level, valid_trains FROM daily_index "
            "ORDER BY weekend_date DESC, lead_days ASC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def latest_snapshots(limit=200):
    """返回各方向最新一次采集的逐车快照。

    12306 从机房 IP 出去不稳，整轮常丢一边（去程或返程查询失败）。
    若只取全局最新 crawl_time，丢的那边明细会整行消失。
    改为按方向各自取最新：去程取去程的最新一次，返程取返程的最新一次，
    与 daily_index 分数的 carry-forward 逻辑一致。
    """
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM snapshots s WHERE s.crawl_time = "
            "(SELECT MAX(crawl_time) FROM snapshots WHERE direction = s.direction) "
            "ORDER BY direction, city, score DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ---------------- 采集与评分 ----------------
RELAY_URL = "http://127.0.0.1:18500/send"


def _mail_send(token, to, subject, text):
    """经本地 alert-relay 发邮件（mailapi 优先，Resend 兜底），返回 (ok, via)。"""
    payload = json.dumps({
        "to": to, "from": "noreply@zin7.de",
        "subject": subject, "text": text,
    }).encode("utf-8")
    req = urllib.request.Request(
        RELAY_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        status = r.status
        body = r.read().decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except ValueError:
        data = {}
    ok = 200 <= status < 300
    if data.get("queued") is True or status == 202:
        via = "mailapi"
    elif "id" in data:
        via = "resend"
    else:
        via = "unknown"
    return ok, via


def _mail_token():
    try:
        with open(CREDS_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("mailapi_token", "") or ""
    except (OSError, ValueError):
        return ""


def _level(inbound, outbound, lead_days):
    """等级：双边 > 70 即“高”，提前 >=3 天破 70 标“极高”；双边 > 40 为“中”，否则“低”。"""
    if inbound is None or outbound is None:
        return "未知"
    if inbound > 70 and outbound > 70:
        return "极高" if lead_days >= 3 else "高"
    if inbound > 40 and outbound > 40:
        return "中"
    return "低"


def _parse_trains(result_rows, direction):
    """解析一批 12306 返回行，返回去重后的车次记录（按城市归类，排除异常车次）。"""
    seen = {}
    for row in result_rows:
        f = row.split("|")
        if len(f) <= F_FIRST:
            continue
        train_no = f[F_TRAIN]
        frm = f[F_FROM]
        to = f[F_TO]
        second_raw = f[F_SECOND]
        first_raw = f[F_FIRST]
        score = _train_score(second_raw, first_raw)
        if score is None:
            continue  # '*' 未起售或无有效座位数据，排除
        key = (train_no, frm, to)
        if key in seen:
            continue
        city = STATION_TO_CITY.get(frm if direction == "inbound" else to, "")
        seen[key] = {
            "train_no": train_no,
            "from_code": frm,
            "to_code": to,
            "dep_time": f[F_DEP],
            "arr_time": f[F_ARR],
            "second_raw": second_raw,
            "first_raw": first_raw,
            "score": score,
            "city": city,
        }
    return list(seen.values())


def _control_map(client, origin_city, date_s):
    """该客源城市的干线终点对照：查一次，返回 {车次: (二等座原始值, 一等座原始值)}。"""
    origin_code = None
    for code in ORIGIN_CODES:
        if STATION_TO_CITY.get(code) == origin_city:
            origin_code = code
            break
    if not origin_code:
        return {}
    dest = CONTROL_DEST.get(origin_city)
    if not dest:
        return {}
    try:
        rows = client.query(origin_code, dest, date_s)
    except Exception:
        return {}
    m = {}
    for row in rows:
        f = row.split("|")
        if len(f) > F_FIRST:
            m[f[F_TRAIN]] = (f[F_SECOND], f[F_FIRST])
    return m


def _dedupe_trains(trains):
    """跨查询去重：同一车次可能从同城多个站出发（如 G2434 经 COE/CXW/CUW），
    12306 会把同一趟车在多个站的出发记录都返回。按 (车次, 目标日期, 方向) 去重，
    只保留最早出发的那一条，避免同一趟车被算多次拉高分数。"""
    seen = {}
    for t in trains:
        key = (t["train_no"], t["_date"])
        if key not in seen or t["dep_time"] < seen[key]["dep_time"]:
            seen[key] = t
    return list(seen.values())


def _direction_score(trains, control, golden_only=True):
    """算一个方向的加权均值。返回 (score, valid_trains, detail_list)。
    golden_only=True 时只计黄金时段内的车次（去程周五17:00后/周六12:00前；返程周日13:00后）。
    """
    total = 0.0
    weight_sum = 0.0
    valid = 0
    detail = []
    for t in trains:
        dep = t["dep_time"]
        # 黄金时段过滤（按出发时间；返程同样按巫山出发时间）
        if golden_only:
            hh, mm = int(dep[:2]), int(dep[3:5])
            mins = hh * 60 + mm
            if t.get("_window") == "fri_eve" and mins < 17 * 60:
                continue
            if t.get("_window") == "sat_mor" and mins >= 12 * 60:
                continue
            if t.get("_window") == "sun_aft" and mins < 13 * 60:
                continue
        w = 1.0
        if _is_short_or_terminating(t["train_no"], t["from_code"], t["to_code"]):
            w = 1.4
        locked = False
        # 限售对照：对照表里该车有票、到巫山却无票 -> 疑似锁票，降权 0.2
        if t["train_no"] in control:
            c_second, c_first = control[t["train_no"]]
            if _seat_value(c_second) not in (None, 0) and t["score"] >= 75:
                locked = True
                w = 0.2
        total += t["score"] * w
        weight_sum += w
        valid += 1
        detail.append({**t, "weight": w, "locked": locked})
    score = round(total / weight_sum, 1) if weight_sum else None
    return score, valid, detail


def collect_once(today=None):
    """跑一轮完整采集 + 评分 + 入库。返回 dict（含 inbound/outbound/level/valid_trains）。
    失败时把错误写入 crawl_log 并抛异常（由调用方决定是否发运维提醒）。"""
    init_db()
    today = today or date.today()
    saturday = next_saturday(today)
    fri, sat, sun = target_dates(saturday)
    lead_days = (saturday - today).days
    crawl_time = datetime.now().isoformat(timespec="seconds")
    client = Ticket12306()
    con = _connect()
    all_trains = 0
    failed_queries = []
    try:
        # ---- 去程：周五晚 + 周六上午，到巫山 ----
        # 单个查询失败不 abort 整轮（12306 偶发返回 HTML 错误页），记下来继续采别的。
        inbound = []
        for origin in ORIGIN_CODES:
            for d, window in ((fri, "fri_eve"), (sat, "sat_mor")):
                try:
                    rows = client.query(origin, WOE, d.isoformat())
                except Exception as exc:
                    failed_queries.append("%s->WOE %s: %s" % (origin, d, exc))
                    continue
                time.sleep(random.uniform(12, 18))
                for t in _parse_trains(rows, "inbound"):
                    t["_window"] = window
                    t["_date"] = d.isoformat()
                    inbound.append(t)
        inbound = _dedupe_trains(inbound)
        # 去程对照表（按城市各 1 次；失败则该城市不做锁票纠偏）
        inbound_control = {}
        for city in ("重庆", "成都", "武汉", "宜昌"):
            inbound_control.update(_control_map(client, city, fri.isoformat()))
            time.sleep(random.uniform(12, 18))
        time.sleep(15)  # 去程与返程之间停 15 秒，缓一缓 12306 的限流
        # ---- 返程：周日下午/晚，从巫山出 ----
        outbound = []
        for origin in ORIGIN_CODES:
            try:
                rows = client.query(WOE, origin, sun.isoformat())
            except Exception as exc:
                failed_queries.append("WOE->%s %s: %s" % (origin, sun, exc))
                continue
            time.sleep(random.uniform(12, 18))
            for t in _parse_trains(rows, "outbound"):
                t["_window"] = "sun_aft"
                t["_date"] = sun.isoformat()
                outbound.append(t)
        outbound = _dedupe_trains(outbound)
        # 返程对照表（从巫山出发的长途车对照：巫山 -> 更远站）
        outbound_control = {}
        try:
            rows = client.query(WOE, "ZAF", sun.isoformat())
            for row in rows:
                f = row.split("|")
                if len(f) > F_FIRST:
                    outbound_control[f[F_TRAIN]] = (f[F_SECOND], f[F_FIRST])
        except Exception as exc:
            failed_queries.append("WOE->ZAF %s: %s" % (sun, exc))
        if not inbound and not outbound:
            raise RuntimeError("全部查询失败：%s" % "; ".join(failed_queries[:3]))
        # ---- 评分 ----
        in_score, in_valid, in_detail = _direction_score(inbound, inbound_control)
        out_score, out_valid, out_detail = _direction_score(outbound, outbound_control)
        # 若某一方向本轮没采到（12306 间歇性返回 HTML），沿用同一目标周末最近一次的有效分，
        # 并在结果里标明 carried，避免整轮因单边缺失而报“未知”。
        carried = []
        if in_score is None or out_score is None:
            prev = latest_index_for_weekend(sat.isoformat())
            if prev:
                if in_score is None and prev.get("inbound_score") is not None:
                    in_score = prev["inbound_score"]
                    carried.append("inbound")
                if out_score is None and prev.get("outbound_score") is not None:
                    out_score = prev["outbound_score"]
                    carried.append("outbound")
        level = _level(in_score, out_score, lead_days)
        valid_trains = in_valid + out_valid
        # ---- 入库 ----
        for t in in_detail:
            con.execute(
                "INSERT OR IGNORE INTO snapshots(crawl_time,target_date,direction,"
                "city,train_no,from_code,to_code,dep_time,arr_time,second_raw,"
                "first_raw,score,weight,locked) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (crawl_time, t["_date"], "inbound", t["city"], t["train_no"],
                 t["from_code"], t["to_code"], t["dep_time"], t["arr_time"],
                 t["second_raw"], t["first_raw"], t["score"], t["weight"],
                 1 if t["locked"] else 0))
        for t in out_detail:
            con.execute(
                "INSERT OR IGNORE INTO snapshots(crawl_time,target_date,direction,"
                "city,train_no,from_code,to_code,dep_time,arr_time,second_raw,"
                "first_raw,score,weight,locked) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (crawl_time, t["_date"], "outbound", t["city"], t["train_no"],
                 t["from_code"], t["to_code"], t["dep_time"], t["arr_time"],
                 t["second_raw"], t["first_raw"], t["score"], t["weight"],
                 1 if t["locked"] else 0))
        all_trains = len(in_detail) + len(out_detail)
        con.execute(
            "INSERT INTO daily_index(weekend_date,crawl_date,lead_days,"
            "inbound_score,outbound_score,level,valid_trains,inbound_detail,"
            "outbound_detail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(weekend_date,crawl_date,lead_days) DO UPDATE SET "
            "inbound_score=excluded.inbound_score,"
            "outbound_score=excluded.outbound_score,level=excluded.level,"
            "valid_trains=excluded.valid_trains,"
            "inbound_detail=excluded.inbound_detail,"
            "outbound_detail=excluded.outbound_detail,"
            "created_at=excluded.created_at",
            (sat.isoformat(), today.isoformat(), lead_days,
             in_score, out_score, level, valid_trains,
             json.dumps(in_detail, ensure_ascii=False),
             json.dumps(out_detail, ensure_ascii=False),
             crawl_time))
        con.execute(
            "INSERT INTO crawl_log(crawl_time,ok,error,trains) VALUES(?,?,?,?)",
            (crawl_time, 1, "", all_trains))
        con.commit()
        return {
            "weekend_date": sat.isoformat(),
            "crawl_date": today.isoformat(),
            "lead_days": lead_days,
            "inbound_score": in_score,
            "outbound_score": out_score,
            "level": level,
            "valid_trains": valid_trains,
            "inbound_detail": in_detail,
            "outbound_detail": out_detail,
            "crawl_time": crawl_time,
            "failed_queries": failed_queries,
            "carried": carried,
        }
    except Exception as exc:
        con.execute(
            "INSERT INTO crawl_log(crawl_time,ok,error,trains) VALUES(?,?,?,?)",
            (crawl_time, 0, str(exc)[:500], 0))
        con.commit()
        raise
    finally:
        con.close()
