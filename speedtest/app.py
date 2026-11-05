# -*- coding: utf-8 -*-
"""Speed test hub: browser<->server tests plus server self-tests.

Two kinds of measurement live here:

* Client tests run in the visitor's browser against this app's own
  /api/ping, /api/dl and /api/up endpoints.  Whatever the browser
  negotiated is what gets measured - HTTP/3 means the transfer rode
  QUIC/UDP, HTTP/2 or HTTP/1.1 means TCP - and the negotiated
  protocol is read back from Resource Timing and stored with the
  result.  Opening this page through the IPv6 name measures the
  direct IPv6 path; opening it through the IPv4 name measures the
  IPv4 path.

* Server self-tests run from the box itself against public speed
  endpoints (Cloudflare speed test, Hetzner Ashburn), per address
  family, so IPv6 and IPv4 egress quality are tracked separately.
  A lazy background thread runs them on a schedule (eco profile
  only) and prunes history.

Everything is sized to spend as little traffic as a usable answer
allows: transfers are time-boxed and byte-capped by profile, and
the byte cost of every run is stored and displayed.

The app carries no authentication of its own; the platform gate
decides who may open it.  Persistent state lives in the platform
data directory (DATA_DIR -> ./data symlink -> .runtime fallback).
"""

import html
import json
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PROFILES = {
    "eco": {"label": "省流", "c_down": 6 * 1024 * 1024,
            "c_up": 3 * 1024 * 1024, "s_down": 5 * 1024 * 1024,
            "s_down_alt": 1 * 1024 * 1024, "s_up": 2 * 1024 * 1024},
    "std": {"label": "标准", "c_down": 25 * 1024 * 1024,
            "c_up": 10 * 1024 * 1024, "s_down": 20 * 1024 * 1024,
            "s_down_alt": 5 * 1024 * 1024, "s_up": 5 * 1024 * 1024},
    "full": {"label": "充分", "c_down": 100 * 1024 * 1024,
             "c_up": 40 * 1024 * 1024, "s_down": 50 * 1024 * 1024,
             "s_down_alt": 10 * 1024 * 1024, "s_up": 10 * 1024 * 1024},
}
DEFAULT_CONFIG = {"profile": "eco", "sched_hours": 12}
RETENTION_DAYS = 90
TIME_CAP = 6.0

_buf_lock = threading.Lock()
_BUF = None


def _buffer():
    global _BUF
    with _buf_lock:
        if _BUF is None:
            _BUF = os.urandom(1024 * 1024)
        return _BUF


def _data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(BASE_DIR, "data")
    if os.path.isdir(link):
        return os.path.realpath(link)
    return os.path.join(BASE_DIR, ".runtime")


DATA_DIR = _data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "speed.db")

_db_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, "
        "metric TEXT, proto TEXT, target TEXT, value REAL, "
        "bytes INTEGER, detail TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, "
        "value TEXT)")
    return conn


def _config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with _db_lock, _db() as conn:
            row = conn.execute(
                "SELECT value FROM kv WHERE key='config'").fetchone()
        if row:
            cfg.update(json.loads(row["value"]))
    except Exception:
        pass
    if cfg.get("profile") not in PROFILES:
        cfg["profile"] = "eco"
    return cfg


def _save_config(cfg):
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES('config', ?)",
            (json.dumps(cfg),))


def _kv_get(key, default=""):
    try:
        with _db_lock, _db() as conn:
            row = conn.execute(
                "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def _kv_set(key, value):
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)",
            (key, value))


def _add_run(actor, metric, proto, target, value, nbytes, detail=None):
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO runs(ts, actor, metric, proto, target, value,"
            " bytes, detail) VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), actor, metric, proto, target,
             float(value), int(nbytes),
             json.dumps(detail or {}, ensure_ascii=False)))


def _esc(text):
    return html.escape(str(text if text is not None else ""), quote=True)


def _fmt_mbps(v):
    if v is None:
        return "-"
    if v >= 100:
        return "%.0f" % v
    if v >= 10:
        return "%.1f" % v
    return "%.2f" % v


def _fmt_ms(v):
    if v is None:
        return "-"
    return "%.0f ms" % v if v >= 10 else "%.1f ms" % v


def _fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else (
                "%.1f %s" % (n, unit))
        n /= 1024.0


def _client_family(environ):
    xff = environ.get("HTTP_X_FORWARDED_FOR") or ""
    ip = xff.split(",")[0].strip()
    if not ip:
        ip = environ.get("REMOTE_ADDR") or ""
    if ":" in ip:
        return "ipv6", ip
    if ip:
        return "ipv4", ip
    return "unknown", ""


# ------------------------------------------------------- server probes

def _rate_from_samples(samples):
    """samples: [(t, cum_bytes)] -> steady Mbps ignoring warmup."""
    if len(samples) < 2:
        return 0.0
    t_end, b_end = samples[-1]
    base = samples[0]
    for s in samples:
        if s[0] - samples[0][0] >= 0.4:
            base = s
            break
    dt = t_end - base[0]
    if dt <= 0.05:
        return 0.0
    return (b_end - base[1]) * 8.0 / dt / 1e6


def _probe(host, path, family, method="GET", up_bytes=0, max_read=0,
           extra=None):
    """One pinned-family HTTPS probe. Returns a dict with connect /
    ttfb timings and (t, bytes) samples for rate computation."""
    fam = socket.AF_INET6 if family == 6 else socket.AF_INET
    infos = socket.getaddrinfo(host, 443, fam, socket.SOCK_STREAM)
    ip = infos[0][4][0]
    out = {"ip": ip, "error": ""}
    t0 = time.time()
    raw = socket.create_connection((ip, 443), timeout=8)
    try:
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise
    out["connect_ms"] = (time.time() - t0) * 1000.0
    try:
        sock.settimeout(3)
        lines = ["%s %s HTTP/1.1" % (method, path),
                 "Host: %s" % host, "User-Agent: vidlib-speed/1.0",
                 "Accept: */*", "Connection: close"]
        if extra:
            lines += extra
        if method == "POST":
            lines.append("Content-Type: application/octet-stream")
            lines.append("Content-Length: %d" % up_bytes)
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        samples = []
        if method == "POST":
            buf = _buffer()
            sent = 0
            t_start = time.time()
            samples.append((0.0, 0))
            while sent < up_bytes:
                chunk = buf[:min(len(buf), up_bytes - sent)]
                sock.sendall(chunk)
                sent += len(chunk)
                samples.append((time.time() - t_start, sent))
            out["samples"] = samples
        buf = b""
        t_first = None
        got = 0
        t_start = time.time()
        if method == "GET":
            samples.append((0.0, 0))
        while True:
            if time.time() - t_start > TIME_CAP:
                break
            if max_read and got >= max_read:
                break
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            if t_first is None:
                t_first = time.time()
            if "head" not in out:
                buf += data
                head, sep, rest = buf.partition(b"\r\n\r\n")
                if not sep:
                    continue
                out["head"] = head.decode("latin1", "replace")
                data = rest
                if method != "GET":
                    break
                if not data:
                    continue
            got += len(data)
            samples.append((time.time() - t_start, got))
        out["ttfb_ms"] = ((t_first - t0) * 1000.0) if t_first else None
        if method == "GET":
            out["samples"] = samples
        out["bytes"] = got if method == "GET" else up_bytes
        return out
    finally:
        try:
            sock.close()
        except Exception:
            pass


_CF = "speed.cloudflare.com"
_HZ = "ash-speed.hetzner.com"


def _server_test(profile_key=None):
    cfg = _config()
    prof = PROFILES[profile_key or cfg["profile"]]
    results = []
    fams = []
    for fam in (6, 4):
        try:
            socket.getaddrinfo(_CF, 443,
                               socket.AF_INET6 if fam == 6
                               else socket.AF_INET,
                               socket.SOCK_STREAM)
            fams.append(fam)
        except Exception:
            pass
    if not fams:
        fams = [4]
    pref = 6 if 6 in fams else fams[0]
    for fam in fams:
        tag = "ipv%d" % fam
        down_cap = prof["s_down"] if fam == pref else prof["s_down_alt"]
        try:
            r = _probe(_CF, "/__down?bytes=%d" % down_cap, fam,
                       max_read=down_cap)
            rate = _rate_from_samples(r["samples"])
            if r.get("ttfb_ms"):
                _add_run("server", "ping", tag, "cloudflare",
                         r["ttfb_ms"], 0,
                         {"connect_ms": round(r["connect_ms"], 1),
                          "ip": r["ip"]})
            if rate > 0:
                _add_run("server", "down", tag, "cloudflare",
                         rate, r["bytes"], {"ip": r["ip"]})
            results.append(("cloudflare", tag, rate))
        except Exception as exc:
            results.append(("cloudflare", tag, "err: %s" % exc))
        try:
            r = _probe(_HZ, "/100MB.bin", fam, max_read=down_cap,
                       extra=["Range: bytes=0-%d" % (down_cap - 1)])
            rate = _rate_from_samples(r["samples"])
            if r.get("ttfb_ms"):
                _add_run("server", "ping", tag, "hetzner-ash",
                         r["ttfb_ms"], 0,
                         {"connect_ms": round(r["connect_ms"], 1),
                          "ip": r["ip"]})
            if rate > 0:
                _add_run("server", "down", tag, "hetzner-ash",
                         rate, r["bytes"], {"ip": r["ip"]})
            results.append(("hetzner-ash", tag, rate))
        except Exception as exc:
            results.append(("hetzner-ash", tag, "err: %s" % exc))
        if fam == pref:
            try:
                up = prof["s_up"]
                r = _probe(_CF, "/__up", fam, method="POST",
                           up_bytes=up)
                rate = _rate_from_samples(r["samples"])
                if rate > 0:
                    _add_run("server", "up", tag, "cloudflare",
                             rate, up, {"ip": r["ip"]})
                results.append(("cloudflare-up", tag, rate))
            except Exception as exc:
                results.append(("cloudflare-up", tag, "err: %s" % exc))
    return results


_test_lock = threading.Lock()
_test_state = {"running": False, "last": "", "last_error": ""}


def _run_server_test_async(profile_key=None, scheduled=False):
    if not _test_lock.acquire(blocking=False):
        return False
    def job():
        _test_state["running"] = True
        try:
            _server_test(profile_key or ("eco" if scheduled else None))
            _test_state["last"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime())
            _test_state["last_error"] = ""
            if scheduled:
                _kv_set("last_sched", str(time.time()))
        except Exception as exc:
            _test_state["last_error"] = str(exc)
        finally:
            _test_state["running"] = False
            _test_lock.release()
    threading.Thread(target=job, daemon=True).start()
    return True


_worker_started = False


def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def loop():
        while True:
            try:
                cfg = _config()
                hours = float(cfg.get("sched_hours") or 0)
                if hours > 0:
                    last = float(_kv_get("last_sched", "0") or 0)
                    if time.time() - last >= hours * 3600:
                        _run_server_test_async(scheduled=True)
                with _db_lock, _db() as conn:
                    conn.execute(
                        "DELETE FROM runs WHERE ts < ?",
                        (time.time() - RETENTION_DAYS * 86400,))
            except Exception:
                pass
            time.sleep(60)

    threading.Thread(target=loop, daemon=True).start()


# ------------------------------------------------------------- stats

def _recent_runs(limit=40):
    with _db_lock, _db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM runs ORDER BY ts DESC LIMIT ?",
            (limit,))]


def _series(actor, metric, proto=None, limit=60):
    sql = ("SELECT ts, value FROM runs WHERE actor=? AND metric=?")
    args = [actor, metric]
    if proto:
        sql += " AND proto=?"
        args.append(proto)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _db_lock, _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [(r["ts"], r["value"]) for r in reversed(rows)]


def _agg(actor, metric, since_days=7):
    since = time.time() - since_days * 86400
    with _db_lock, _db() as conn:
        vals = [r["value"] for r in conn.execute(
            "SELECT value FROM runs WHERE actor=? AND metric=? "
            "AND ts>=? ORDER BY value", (actor, metric, since))]
    if not vals:
        return None
    return {"n": len(vals), "min": vals[0], "max": vals[-1],
            "avg": sum(vals) / len(vals),
            "p50": vals[len(vals) // 2]}


def _chart_svg(series_list, width=720, height=170):
    """series_list: [(label, color, [(ts, value), ...]), ...]"""
    all_vals = [v for _, _, pts in series_list for _, v in pts]
    if not all_vals:
        return ('<div style="color:#8b94a7;font-size:13px">'
                '还没有数据，先跑一次测速。</div>')
    vmax = max(all_vals) * 1.15 or 1
    tmin = min(ts for _, _, pts in series_list for ts, _ in pts)
    tmax = max(ts for _, _, pts in series_list for ts, _ in pts)
    span = max(tmax - tmin, 1)
    pad_l, pad_r, pad_t, pad_b = 8, 8, 10, 18
    out = ['<svg viewBox="0 0 %d %d" style="width:100%%;background:#12161f;'
           'border:1px solid #242c3f;border-radius:10px">' % (width, height)]
    for gy in (0.25, 0.5, 0.75, 1.0):
        y = pad_t + (height - pad_t - pad_b) * (1 - gy)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" '
                   'stroke="#242c3f" stroke-width="1"/>' % (pad_l, y,
                                                           width - pad_r, y))
        out.append('<text x="%d" y="%.1f" fill="#5f6b84" font-size="10">'
                   '%s</text>' % (pad_l + 2, y - 2, _fmt_mbps(vmax * gy)))
    for label, color, pts in series_list:
        if len(pts) < 1:
            continue
        coords = []
        for ts, v in pts:
            x = pad_l + (width - pad_l - pad_r) * (ts - tmin) / span
            y = pad_t + (height - pad_t - pad_b) * (1 - v / vmax)
            coords.append("%.1f,%.1f" % (x, y))
        if len(coords) == 1:
            x, y = coords[0].split(",")
            out.append('<circle cx="%s" cy="%s" r="3" fill="%s"/>'
                       % (x, y, color))
        else:
            out.append('<polyline fill="none" stroke="%s" '
                       'stroke-width="2" points="%s"/>'
                       % (color, " ".join(coords)))
    out.append("</svg>")
    legend = " · ".join(
        '<span style="color:%s">■</span> %s' % (c, _esc(l))
        for l, c, pts in series_list if pts)
    return "".join(out) + ('<div style="font-size:12px;color:#8b94a7;'
                           'margin-top:5px">%s（纵轴 Mbps）</div>' % legend)


BASE_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "PingFang SC",
  "Microsoft YaHei", sans-serif; background: #0b0e14; color: #e6e9f0; }
.wrap { max-width: 860px; margin: 0 auto; padding: 18px 14px 40px; }
h1 { font-size: 20px; margin: 0; }
h2 { font-size: 15.5px; margin: 0 0 10px; }
.topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.panel { background: #171c27; border: 1px solid #242c3f; border-radius: 13px;
  padding: 15px 16px; margin-bottom: 14px; }
button, .btn { background: #2f6fd0; color: #fff; border: 0; border-radius: 9px;
  padding: 8px 15px; font-size: 14px; cursor: pointer; text-decoration: none;
  display: inline-block; }
button.ghost, .btn.ghost { background: #232a3a; color: #c6cfdf; }
button:disabled { opacity: .5; cursor: default; }
.msg { color: #8b94a7; font-size: 13px; margin-left: 8px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px; }
.card { background: #12161f; border: 1px solid #242c3f; border-radius: 11px;
  padding: 11px 12px; }
.card .k { color: #8b94a7; font-size: 12px; }
.card .v { font-size: 21px; font-weight: 600; margin-top: 3px; }
.card .v small { font-size: 12px; color: #8b94a7; font-weight: 400; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
td, th { padding: 6px 7px; border-bottom: 1px solid #20263588;
  text-align: left; white-space: nowrap; }
th { color: #8b94a7; font-weight: 500; }
select, input { background: #12161f; color: #e6e9f0; border: 1px solid #2b3448;
  border-radius: 8px; padding: 7px 9px; font-size: 14px; }
.progress { height: 8px; background: #232a3a; border-radius: 5px; overflow: hidden; }
.progress i { display: block; height: 100%; width: 0; background: #4f7cff; }
.tag { color: #7ea4e8; }
"""


def _page(title, body, base_href=""):
    base = ('<base href="%s">' % _esc(base_href)) if base_href else ""
    doc = ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,"
           "initial-scale=1'><title>%s</title>%s<style>%s</style></head>"
           "<body><div class='wrap'>%s</div></body></html>"
           % (_esc(title), base, BASE_CSS, body))
    return doc.encode("utf-8")


def _latest(actor, metric, proto=None):
    sql = ("SELECT * FROM runs WHERE actor=? AND metric=?")
    args = [actor, metric]
    if proto:
        sql += " AND proto=?"
        args.append(proto)
    sql += " ORDER BY ts DESC LIMIT 1"
    with _db_lock, _db() as conn:
        row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def _ago(ts):
    if not ts:
        return "从未"
    d = time.time() - ts
    if d < 90:
        return "刚刚"
    if d < 3600:
        return "%d 分钟前" % (d // 60)
    if d < 86400:
        return "%.1f 小时前" % (d / 3600)
    return "%.0f 天前" % (d / 86400)


def page_home(base_href=""):
    cfg = _config()
    prof = PROFILES[cfg["profile"]]
    c_ping = _latest("client", "ping")
    c_down = _latest("client", "down")
    c_up = _latest("client", "up")

    def ccard(label, run, fmt):
        if not run:
            return ('<div class="card"><div class="k">%s</div>'
                    '<div class="v">-</div></div>' % label)
        return ('<div class="card"><div class="k">%s · %s</div>'
                '<div class="v">%s <small>%s · %s</small></div></div>'
                % (label, _ago(run["ts"]), fmt(run["value"]),
                   _esc(run["proto"] or ""),
                   _esc(json.loads(run["detail"] or "{}").get(
                       "family", ""))))

    cards = (ccard("客户端延迟", c_ping, _fmt_ms)
             + ccard("客户端下载", c_down,
                     lambda v: _fmt_mbps(v) + " Mbps")
             + ccard("客户端上传", c_up,
                     lambda v: _fmt_mbps(v) + " Mbps"))
    s_rows = []
    for target in ("cloudflare", "hetzner-ash"):
        for proto in ("ipv6", "ipv4"):
            d = _latest("server", "down", proto)
            if d and d["target"] == target:
                p = _latest("server", "ping", proto)
                pv = "-"
                if p and p["target"] == target:
                    pv = _fmt_ms(p["value"])
                s_rows.append(
                    "<tr><td>%s</td><td>%s</td><td>%s</td>"
                    "<td>%s Mbps</td><td>%s</td></tr>"
                    % (_esc(target), proto, pv, _fmt_mbps(d["value"]),
                       _ago(d["ts"])))
    s_up = None
    for proto in ("ipv6", "ipv4"):
        u = _latest("server", "up", proto)
        if u and (s_up is None or u["ts"] > s_up["ts"]):
            s_up = u
    if s_up:
        s_rows.append(
            "<tr><td>cloudflare</td><td>%s</td><td>-</td>"
            "<td>上传 %s Mbps</td><td>%s</td></tr>"
            % (s_up["proto"], _fmt_mbps(s_up["value"]),
               _ago(s_up["ts"])))
    chart = _chart_svg([
        ("客户端下载", "#4f7cff", _series("client", "down")),
        ("客户端上传", "#38b2a8", _series("client", "up")),
        ("服务器下载", "#e8a13a", _series("server", "down")),
        ("服务器上传", "#b06ab3", _series("server", "up")),
    ])
    aggs = []
    for label, actor, metric, fmt in (
            ("客户端下载", "client", "down", _fmt_mbps),
            ("客户端上传", "client", "up", _fmt_mbps),
            ("客户端延迟", "client", "ping", _fmt_ms),
            ("服务器下载", "server", "down", _fmt_mbps),
            ("服务器上传", "server", "up", _fmt_mbps),
            ("服务器延迟", "server", "ping", _fmt_ms)):
        a = _agg(actor, metric)
        if a:
            aggs.append(
                "<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td>%s</td></tr>"
                % (label, a["n"], fmt(a["avg"]), fmt(a["p50"]),
                   fmt(a["min"]), fmt(a["max"])))
    recent = []
    for r in _recent_runs(30):
        unit = "ms" if r["metric"] == "ping" else "Mbps"
        val = _fmt_ms(r["value"]) if r["metric"] == "ping" else (
            "%s Mbps" % _fmt_mbps(r["value"]))
        recent.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td></tr>"
            % (time.strftime("%m-%d %H:%M", time.localtime(r["ts"])),
               "客户端" if r["actor"] == "client" else "服务器",
               {"ping": "延迟", "down": "下载", "up": "上传"}[r["metric"]],
               _esc(r["proto"] or ""), _esc(r["target"] or ""),
               val, _fmt_bytes(r["bytes"])))
        del unit
    sched_opts = "".join(
        '<option value="%d"%s>%s</option>'
        % (h, " selected" if float(cfg["sched_hours"]) == h else "",
           {0: "关闭", 6: "每 6 小时", 12: "每 12 小时",
            24: "每天"}[h])
        for h in (0, 6, 12, 24))
    prof_opts = "".join(
        '<option value="%s"%s>%s</option>'
        % (k, " selected" if cfg["profile"] == k else "", v["label"])
        for k, v in PROFILES.items())
    body = (
        '<div class="topbar"><h1>测速</h1>'
        '<span class="msg">客户端 ↔ 本服务器 · 服务器 ↔ 公网</span></div>'
        '<div class="panel"><h2>客户端测速（浏览器到这台服务器）</h2>'
        '<div class="cards">%s</div>'
        '<div style="margin-top:12px"><button id="cgo">开始测速</button>'
        '<span class="msg" id="cmsg"></span></div>'
        '<div class="progress" style="margin-top:10px"><i id="cbar"></i></div>'
        '<p style="color:#8b94a7;font-size:12.5px;line-height:1.7">'
        '测的是你现在打开本页的这条连接：协商出 HTTP/3 就是 UDP(QUIC)，'
        'HTTP/2 或 1.1 就是 TCP；经 IPv6 域名打开测直连 IPv6，'
        '经 IPv4 域名打开测 IPv4 路径，可对比。当前档位「%s」：'
        '一次完整测速最多约 %s 流量。</p></div>'
        '<div class="panel"><h2>服务器自测（这台 VPS 到公网）</h2>'
        '<table><tr><th>目标</th><th>协议</th><th>延迟</th>'
        '<th>速度</th><th>时间</th></tr>%s</table>'
        '<div style="margin-top:10px">'
        '<button class="ghost" id="sgo">立即自测</button>'
        '<span class="msg" id="smsg">%s</span></div>'
        '<p style="color:#8b94a7;font-size:12.5px;line-height:1.7">'
        '目标为 Cloudflare 测速网与 Hetzner 阿什本机房，IPv6 / IPv4 分开测。'
        '定时自测固定用省流档，与手动档位无关。</p></div>'
        '<div class="panel"><h2>趋势</h2>%s</div>'
        '<div class="panel"><h2>近 7 天统计</h2>'
        '<table><tr><th>项目</th><th>次数</th><th>平均</th><th>中位</th>'
        '<th>最小</th><th>最大</th></tr>%s</table></div>'
        '<div class="panel"><h2>设置</h2>'
        '<form id="cfg"><div style="display:flex;gap:10px;flex-wrap:wrap;'
        'align-items:center">'
        '<label>测速档位 <select id="profile">%s</select></label>'
        '<label>服务器定时自测 <select id="sched">%s</select></label>'
        '<button type="submit">保存</button>'
        '<span class="msg" id="cfgmsg"></span></div></form>'
        '<p style="color:#8b94a7;font-size:12.5px">省流档：客户端下载上限 '
        '6MB / 上传 3MB，服务器单目标下载 5MB；标准与充分档逐步放大。'
        '历史保留 %d 天。</p></div>'
        '<div class="panel"><h2>最近记录</h2>'
        '<table><tr><th>时间</th><th>来源</th><th>项目</th><th>协议</th>'
        '<th>目标</th><th>结果</th><th>流量</th></tr>%s</table></div>'
        '<script>%s</script>'
        % (cards, _esc(prof["label"]),
           _fmt_bytes(prof["c_down"] + prof["c_up"] + 2048),
           "".join(s_rows) or '<tr><td colspan="5">还没有自测记录</td></tr>',
           _esc(("上次自测 " + _test_state["last"]) if _test_state["last"]
                else "尚未自测"),
           chart,
           "".join(aggs) or '<tr><td colspan="6">暂无数据</td></tr>',
           prof_opts, sched_opts, RETENTION_DAYS,
           "".join(recent) or '<tr><td colspan="7">暂无记录</td></tr>',
           CLIENT_JS))
    return _page("测速", body, base_href=base_href)


CLIENT_JS = r"""
const $=id=>document.getElementById(id);
async function jpost(u,b){const r=await fetch(u,{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});
return r.json();}
function setBar(p){$("cbar").style.width=(p*100)+"%";}
function msg(t){$("cmsg").textContent=t;}
async function pingTest(){
  window._fam="unknown";
  try{const r=await fetch("api/ping",{cache:"no-store"});
  const d=await r.json();if(d.family)window._fam=d.family;}catch(e){}
  const ts=[];
  for(let i=0;i<14;i++){
    const t0=performance.now();
    const r=await fetch("api/ping",{cache:"no-store"});
    await r.text();
    if(i>=2)ts.push(performance.now()-t0);
  }
  const s=[...ts].sort((a,b)=>a-b);
  const avg=ts.reduce((a,b)=>a+b,0)/ts.length;
  let jit=0;for(let i=1;i<ts.length;i++)jit+=Math.abs(ts[i]-ts[i-1]);
  jit/=(ts.length-1);
  return {avg:avg,min:s[0],p95:s[Math.floor(s.length*0.95)],jitter:jit,
          bytes:ts.length*24};
}
async function downTest(cap){
  let got=0,firstT=0,firstB=0;const t0=performance.now();
  const probes=[262144,1048576];let idx=0,stop=false;
  while(got<cap&&!stop){
    const want=idx<probes.length?probes[idx++]:cap-got;
    const n=Math.min(want,cap-got);
    const r=await fetch("api/dl?bytes="+n,{cache:"no-store"});
    const rd=r.body.getReader();
    while(true){
      const {done,value}=await rd.read();
      if(done)break;
      got+=value.length;
      const el=(performance.now()-t0)/1000;
      if(!firstT&&el>0.35){firstT=el;firstB=got;}
      setBar(0.15+0.45*Math.min(1,got/cap));
      if(el>8){stop=true;try{await rd.cancel();}catch(e){}break;}
    }
  }
  const total=(performance.now()-t0)/1000;
  const secs=total-(firstT||0),bytes=got-(firstB||0);
  const mbps=(secs>0.25?bytes*8/secs:got*8/total)/1e6;
  return {mbps:mbps,bytes:got};
}
function upOnce(blob,base){
  return new Promise((res,rej)=>{
    const x=new XMLHttpRequest();
    x.open("POST","api/up");
    x.upload.onprogress=e=>{window._upCur=base+e.loaded;};
    x.onload=()=>res();
    x.onerror=()=>rej(new Error("上传中断"));
    x.send(blob);
  });
}
async function upTest(cap){
  const buf=new Uint8Array(1048576);
  if(window.crypto)crypto.getRandomValues(buf.subarray(0,65536));
  let sent=0,firstT=0,firstS=0;const t0=performance.now();
  const sizes=[524288,1048576];let idx=0;
  window._upCur=0;
  while(sent<cap){
    const want=idx<sizes.length?sizes[idx++]:cap-sent;
    const n=Math.min(want,cap-sent);
    const parts=[];let left=n;
    while(left>0){const c=Math.min(left,buf.length);
      parts.push(buf.subarray(0,c));left-=c;}
    await upOnce(new Blob(parts),sent);
    sent+=n;
    const el=(performance.now()-t0)/1000;
    if(!firstT&&el>0.35){firstT=el;firstS=sent;}
    setBar(0.6+0.3*Math.min(1,sent/cap));
    if(el>8)break;
  }
  const total=(performance.now()-t0)/1000;
  const secs=total-(firstT||0),bytes=sent-(firstS||0);
  const mbps=(secs>0.25?bytes*8/secs:sent*8/total)/1e6;
  return {mbps:mbps,bytes:sent};
}
function protoUsed(){
  try{
    const es=performance.getEntriesByType("resource")
      .filter(e=>e.name.indexOf("api/dl")>=0||e.name.indexOf("api/ping")>=0);
    for(let i=es.length-1;i>=0;i--)
      if(es[i].nextHopProtocol)return es[i].nextHopProtocol;
  }catch(e){}
  return "unknown";
}
$("cgo").onclick=async()=>{
  $("cgo").disabled=true;setBar(0.02);
  try{
    const cfg=await (await fetch("api/config")).json();
    msg("测延迟…");const p=await pingTest();setBar(0.15);
    msg("测下载…");const d=await downTest(cfg.caps.c_down);setBar(0.6);
    msg("测上传…");const u=await upTest(cfg.caps.c_up);setBar(0.92);
    const proto=protoUsed();
    await jpost("api/client-result",{ping:p,down:d,up:u,proto:proto,
      family:window._fam||"unknown",host:location.host});
    setBar(1);
    msg("完成：延迟 "+p.avg.toFixed(0)+" ms · 下载 "+d.mbps.toFixed(1)
      +" Mbps（"+proto+"） · 上传 "+u.mbps.toFixed(1)
      +" Mbps。刷新页面看趋势与统计。");
  }catch(e){msg("测速中断："+e.message);}
  $("cgo").disabled=false;
};
$("sgo").onclick=async()=>{
  const d=await jpost("api/server-test",{});
  $("smsg").textContent=d.started?"自测进行中，约半分钟后刷新页面看结果"
    :"自测已经在进行中";
};
$("cfg").onsubmit=async e=>{
  e.preventDefault();
  const d=await jpost("api/config-save",
    {profile:$("profile").value,sched_hours:$("sched").value});
  $("cfgmsg").textContent=d.ok?"已保存":"保存失败";
};
"""


# ------------------------------------------------------------- router

def _json(start_response, status, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))),
                            ("Cache-Control", "no-store")])
    return [body]


def _dl_iter(n):
    buf = _buffer()
    left = n
    while left > 0:
        chunk = buf[:min(len(buf), left)]
        yield chunk
        left -= len(chunk)


def application(environ, start_response):
    _ensure_worker()
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO") or "/"
    params = {k: v[0] for k, v in urllib.parse.parse_qs(
        environ.get("QUERY_STRING", "")).items()}
    prefix = environ.get("HTTP_X_FORWARDED_PREFIX") or ""
    base_href = (prefix + "/") if prefix else ""

    if path == "/" and method == "GET":
        body = page_home(base_href)
        start_response("200 OK",
                       [("Content-Type", "text/html; charset=utf-8"),
                        ("Content-Length", str(len(body)))])
        return [body]

    if path == "/api/session":
        return _json(start_response, "200 OK",
                     {"ok": True, "authenticated": True,
                      "via": "platform"})

    if path == "/api/ping" and method == "GET":
        fam, _ip = _client_family(environ)
        return _json(start_response, "200 OK",
                     {"ok": True, "family": fam})

    if path == "/api/dl" and method == "GET":
        try:
            n = int(params.get("bytes") or 0)
        except Exception:
            n = 0
        n = max(0, min(n, PROFILES["full"]["c_down"]))
        start_response("200 OK",
                       [("Content-Type", "application/octet-stream"),
                        ("Content-Length", str(n)),
                        ("Cache-Control", "no-store")])
        return _dl_iter(n)

    if path == "/api/up" and method == "POST":
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except Exception:
            length = 0
        if length > PROFILES["full"]["c_up"]:
            return _json(start_response, "400 Bad Request",
                         {"ok": False, "message": "上传体积超过上限。"})
        stream = environ["wsgi.input"]
        left = length
        while left > 0:
            chunk = stream.read(min(262144, left))
            if not chunk:
                break
            left -= len(chunk)
        return _json(start_response, "200 OK",
                     {"ok": True, "bytes": length - left})

    if path == "/api/config" and method == "GET":
        cfg = _config()
        prof = PROFILES[cfg["profile"]]
        return _json(start_response, "200 OK",
                     {"ok": True, "profile": cfg["profile"],
                      "caps": {"c_down": prof["c_down"],
                               "c_up": prof["c_up"]},
                      "sched_hours": cfg["sched_hours"]})

    if path == "/api/config-save" and method == "POST":
        try:
            data = json.loads(
                environ["wsgi.input"].read(
                    int(environ.get("CONTENT_LENGTH") or 0) or 0)
                or b"{}")
        except Exception:
            data = {}
        cfg = _config()
        if data.get("profile") in PROFILES:
            cfg["profile"] = data["profile"]
        try:
            hours = float(data.get("sched_hours"))
            if hours in (0, 6, 12, 24):
                cfg["sched_hours"] = hours
        except Exception:
            pass
        _save_config(cfg)
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/client-result" and method == "POST":
        try:
            data = json.loads(
                environ["wsgi.input"].read(
                    int(environ.get("CONTENT_LENGTH") or 0) or 0)
                or b"{}")
        except Exception:
            data = {}
        proto = str(data.get("proto") or "unknown")[:20]
        family = str(data.get("family") or "unknown")[:10]
        host = str(data.get("host") or "")[:60]
        ping = data.get("ping") or {}
        down = data.get("down") or {}
        up = data.get("up") or {}
        if ping.get("avg"):
            _add_run("client", "ping", proto, host,
                     float(ping["avg"]), int(ping.get("bytes") or 0),
                     {"family": family,
                      "min": ping.get("min"), "p95": ping.get("p95"),
                      "jitter": ping.get("jitter")})
        if down.get("mbps"):
            _add_run("client", "down", proto, host,
                     float(down["mbps"]), int(down.get("bytes") or 0),
                     {"family": family})
        if up.get("mbps"):
            _add_run("client", "up", proto, host,
                     float(up["mbps"]), int(up.get("bytes") or 0),
                     {"family": family})
        return _json(start_response, "200 OK", {"ok": True})

    if path == "/api/server-test" and method == "POST":
        started = _run_server_test_async()
        return _json(start_response, "200 OK",
                     {"ok": True, "started": started})

    if path == "/api/status" and method == "GET":
        return _json(start_response, "200 OK",
                     {"ok": True, **_test_state})

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]


app = application
