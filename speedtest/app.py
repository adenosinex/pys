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

_MB = 1024 * 1024
# Bulk measurements are stepped: transfers grow through the step
# sizes and stop as soon as the measured rate converges (two
# successive windows within ~10%), so the headline number is a
# cruise rate rather than a slow-start artefact, while the traffic
# spent stays close to the minimum the path needs to reveal it.
# "steps" caps the preferred path, "alt_steps" the secondary one.
PROFILES = {
    "eco": {"label": "省流", "steps": [4 * _MB, 16 * _MB],
            "alt_steps": [4 * _MB]},
    "std": {"label": "标准",
            "steps": [4 * _MB, 16 * _MB, 64 * _MB],
            "alt_steps": [4 * _MB, 16 * _MB]},
    "full": {"label": "充分",
             "steps": [4 * _MB, 16 * _MB, 64 * _MB, 256 * _MB],
             "alt_steps": [4 * _MB, 16 * _MB, 64 * _MB]},
}
DEFAULT_CONFIG = {"profile": "eco", "sched_hours": 12}
RETENTION_DAYS = 90
TIME_CAP = 10.0

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


def _mib_short(v):
    """Bare number for a value already in MiB/s."""
    return ("%.0f" % v) if v >= 100 else (
        ("%.1f" % v) if v >= 10 else ("%.2f" % v))


def _fmt_axis(v):
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
    """Volumes in the same MiB family as the speeds (B below 1 KiB)."""
    n = float(n or 0)
    if n < 1024:
        return "%.0f B" % n
    mib = n / 1048576.0
    if mib < 1024:
        return "%.1f MiB" % mib
    return "%.2f GiB" % (mib / 1024.0)


def _cst(ts):
    """Asia/Shanghai wall time (the box itself runs UTC)."""
    return time.gmtime(ts + 8 * 3600)


def _to_mibs(mbps):
    """Stored values are Mbps (decimal bits); the UI speaks MiB/s,
    the unit the transferred chunks are actually counted in."""
    return (mbps or 0) * 1e6 / 8.0 / 1048576.0


def _fmt_speed(mbps):
    v = _to_mibs(mbps)
    if v >= 100:
        return "%.0f MiB/s" % v
    if v >= 10:
        return "%.1f MiB/s" % v
    return "%.2f MiB/s" % v


# One scoring algorithm for any measured link, used to guide daily
# use.  It scores EXPERIENCE, not raw speed: each bandwidth part is
# a saturating curve 100*x/(x+k), so the score climbs fast while
# extra speed still changes what the link feels like and flattens
# once it stops mattering - for this household's proxy/library use
# ~2 MiB/s is already a good experience (~80), 0.5 MiB/s is the
# half-way point, and the best performance actually seen on these
# links approaches, but mechanically never needs, 100.
DOWN_K = 0.5    # MiB/s at which download experience reaches 50
UP_K = 0.3      # uploads are background work: saturates earlier
LAT_FULL_MS = 80.0    # at or below: latency feels instant
LAT_ZERO_MS = 500.0   # at or above: latency scores zero


def _sat(x_mibs, k):
    if not x_mibs or x_mibs <= 0:
        return 0.0
    return 100.0 * x_mibs / (x_mibs + k)


def _link_score(down_mbps, up_mbps, latency_ms):
    parts = {}
    if down_mbps:
        parts["down"] = _sat(_to_mibs(down_mbps), DOWN_K)
    if up_mbps:
        parts["up"] = _sat(_to_mibs(up_mbps), UP_K)
    if latency_ms:
        parts["lat"] = 100.0 * max(
            0.0, min(1.0, (LAT_ZERO_MS - latency_ms)
                     / (LAT_ZERO_MS - LAT_FULL_MS)))
    if not parts:
        return None, parts
    weights = {"down": 0.45, "up": 0.25, "lat": 0.30}
    total_w = sum(weights[k] for k in parts)
    score = sum(parts[k] * weights[k] for k in parts) / total_w
    return score, parts


def _score_grade(score):
    if score is None:
        return "-", "#8b94a7"
    if score >= 85:
        return "优秀", "#3fb950"
    if score >= 70:
        return "良好", "#7ee787"
    if score >= 50:
        return "一般", "#e8a13a"
    if score >= 30:
        return "较差", "#e0684b"
    return "差", "#d33a3a"


def _bar_strip(points, color, short_fmt, title_fmt):
    """Excel-style data bars: one cell per run, a single colour
    whose filled LENGTH is the value's share of the row maximum -
    a value at the maximum fills the cell completely, a value at
    30% of it fills 30% of the cell.  The number rides inside."""
    if not points:
        return ('<span style="color:#8b94a7;font-size:12px">'
                '暂无数据</span>')
    vmax = max(v for _, v in points) or 1
    cells = []
    for ts, v in points:
        pct = int(round(100.0 * v / vmax))
        title = "%s · %s" % (
            time.strftime("%m-%d %H:%M", _cst(ts)),
            title_fmt(v))
        cells.append(
            '<span class="bar" title="%s" style="background:'
            'linear-gradient(90deg,%s %d%%,%s26 %d%%)">%s</span>'
            % (_esc(title), color, pct, color, pct, short_fmt(v)))
    return "".join(cells)


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


def _window_rate(samples, t_from, t_to):
    """Rate between the samples bracketing [t_from, t_to]."""
    lo = None
    hi = None
    for s in samples:
        if s[0] <= t_from:
            lo = s
        if s[0] <= t_to:
            hi = s
    if lo is None or hi is None or hi[0] - lo[0] <= 0.05:
        return 0.0
    return (hi[1] - lo[1]) * 8.0 / (hi[0] - lo[0]) / 1e6


def _size_rates(samples, marks):
    """Cumulative trimmed rate up to each byte mark reached."""
    out = {}
    for mark in marks:
        part = [s for s in samples if s[1] <= mark]
        if part and samples and samples[-1][1] >= mark:
            out[mark] = _rate_from_samples(part)
    return out


def _cruise_rate(samples):
    """Rate over the settled tail: after both 1s and 4MB have
    passed, so slow start is excluded.  Falls back to the trimmed
    whole-transfer rate for short samples."""
    if len(samples) < 2:
        return 0.0
    start = None
    for s in samples:
        if s[0] >= 1.0 and s[1] >= 4 * 1024 * 1024:
            start = s
            break
    if start is None:
        return _rate_from_samples(samples)
    dt = samples[-1][0] - start[0]
    if dt <= 0.1:
        return _rate_from_samples(samples)
    return (samples[-1][1] - start[1]) * 8.0 / dt / 1e6


def _converged(samples, min_bytes=4 * 1024 * 1024):
    """True when the trailing 1.2s window rate and the window
    before it agree within 10% after a meaningful amount of data."""
    if len(samples) < 3:
        return False
    t_end, b_end = samples[-1]
    if b_end < min_bytes or t_end < 2.4:
        return False
    r_now = _window_rate(samples, t_end - 1.2, t_end)
    r_prev = _window_rate(samples, t_end - 2.4, t_end - 1.2)
    if r_now <= 0 or r_prev <= 0:
        return False
    return abs(r_now - r_prev) / max(r_now, r_prev) < 0.10


def _probe(host, path, family, method="GET", up_bytes=0, max_read=0,
           extra=None, converge=False):
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
        t_send0 = time.time()
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
            if converge and _converged(samples):
                out["converged"] = True
                break
        out["ttfb_ms"] = ((t_first - t0) * 1000.0) if t_first else None
        if method == "GET":
            out["samples"] = samples
        out["bytes"] = got if method == "GET" else up_bytes
        out["elapsed"] = time.time() - t_send0
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
    steps = prof["steps"]
    alt_steps = prof["alt_steps"]
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

    def down_probe(target, host, path, fam, fam_steps, extra=None):
        tag = "ipv%d" % fam
        cap = fam_steps[-1]
        r = _probe(host, path, fam, max_read=cap, extra=extra,
                   converge=True)
        if r["bytes"] < 512 * 1024:
            # one retry: a bulk probe returning almost nothing is a
            # transient (rate limit, reset), not a measurement
            r2 = _probe(host, path, fam, max_read=cap, extra=extra,
                        converge=True)
            if r2["bytes"] > r["bytes"]:
                r = r2
        if r.get("ttfb_ms"):
            _add_run("server", "ping", tag, target,
                     r["ttfb_ms"], 0,
                     {"connect_ms": round(r["connect_ms"], 1),
                      "ip": r["ip"]})
        cruise = _cruise_rate(r["samples"])
        if cruise >= 0.5 and r["bytes"] >= 512 * 1024:
            sizes = {("%.0fMiB" % (m / 1048576.0)): round(v, 1)
                     for m, v in _size_rates(
                         r["samples"],
                         [s for s in fam_steps
                          if s <= r["bytes"]]).items()}
            _add_run("server", "down", tag, target, cruise,
                     r["bytes"],
                     {"ip": r["ip"], "sizes": sizes,
                      "converged": bool(r.get("converged")),
                      "head": (r.get("head") or "").split("\r\n")[0]})
        results.append((target, tag, cruise))
        return r

    for fam in fams:
        tag = "ipv%d" % fam
        fam_steps = steps if fam == pref else alt_steps
        try:
            down_probe("cloudflare", _CF,
                       "/__down?bytes=%d" % fam_steps[-1], fam,
                       fam_steps)
        except Exception as exc:
            results.append(("cloudflare", tag, "err: %s" % exc))
        try:
            down_probe("hetzner-ash", _HZ, "/100MB.bin", fam,
                       fam_steps,
                       extra=["Range: bytes=0-%d" % (fam_steps[-1] - 1)])
        except Exception as exc:
            results.append(("hetzner-ash", tag, "err: %s" % exc))
        if fam == pref:
            try:
                sizes = {}
                prev_rate = 0.0
                rate = 0.0
                spent = 0
                for step in steps:
                    r = _probe(_CF, "/__up", fam, method="POST",
                               up_bytes=step)
                    rate = _rate_from_samples(r["samples"])
                    if rate <= 0 and r.get("elapsed", 0) > 0.05:
                        # Fast paths drain into kernel buffers
                        # before the send clock sees any blocking;
                        # the server answers only once the last
                        # byte arrived, so whole-request duration
                        # is the honest estimate there.
                        rate = step * 8.0 / r["elapsed"] / 1e6
                    spent += step
                    sizes["%.0fMiB" % (step / 1048576.0)] = round(
                        rate, 1)
                    if (prev_rate > 0 and rate > 0
                            and abs(rate - prev_rate)
                            / max(rate, prev_rate) < 0.10):
                        break
                    prev_rate = rate
                if rate > 0:
                    _add_run("server", "up", tag, "cloudflare",
                             rate, spent,
                             {"ip": r["ip"], "sizes": sizes})
                results.append(("cloudflare-up", tag, rate))
            except Exception as exc:
                results.append(("cloudflare-up", tag, "err: %s" % exc))
    return results

_test_lock = threading.Lock()
_test_state = {"running": False, "last": "", "last_error": ""}


def _last_test_text():
    if _test_state["running"]:
        return "自测进行中…"
    try:
        with _db_lock, _db() as conn:
            row = conn.execute(
                "SELECT MAX(ts) AS m FROM runs WHERE actor='server'"
            ).fetchone()
        if row and row["m"]:
            return "上次自测 " + _ago(row["m"])
    except Exception:
        pass
    return "尚未自测"


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


def _srv_rtt(row):
    """Estimated round trip for a server probe: half the TCP+TLS
    connect time.  (The stored value is HTTPS time-to-first-byte,
    roughly three round trips, which reads as a scary 'latency'.)"""
    try:
        conn = json.loads(row["detail"] or "{}").get("connect_ms")
    except Exception:
        conn = None
    if conn:
        return float(conn) / 2.0
    return (row["value"] or 0) / 3.0


def _agg(actor, metric, since_days=7, transform=None):
    since = time.time() - since_days * 86400
    with _db_lock, _db() as conn:
        vals = sorted(
            (transform(r) if transform else r["value"])
            for r in conn.execute(
                "SELECT value, detail FROM runs WHERE actor=? "
                "AND metric=? AND ts>=?", (actor, metric, since)))
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
    step = next((c for c in (1, 2, 5, 10, 20, 25, 50, 100, 200,
                             500) if vmax / c <= 5), 500)
    tick = step
    while tick <= vmax:
        y = pad_t + (height - pad_t - pad_b) * (1 - tick / vmax)
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" '
                   'stroke="#242c3f" stroke-width="1"/>' % (pad_l, y,
                                                           width - pad_r, y))
        out.append('<text x="%d" y="%.1f" fill="#5f6b84" font-size="10">'
                   '%s</text>' % (pad_l + 2, y - 2, _fmt_axis(tick)))
        tick += step
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
                           'margin-top:5px">%s（纵轴 MiB/s）</div>' % legend)


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
.tblwrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.score { font-size: 30px; font-weight: 700; }
.modal { position: fixed; inset: 0; background: rgba(4,6,10,.62);
  display: none; z-index: 60; padding: 5vh 10px 12px; }
.modal.open { display: block; }
.modal .box { background: #171a21; border: 1px solid #262b38;
  border-radius: 12px; max-width: 560px; margin: 0 auto;
  max-height: 86vh; overflow-y: auto; padding: 16px; }
details.panel summary { cursor: pointer; color: #c8cfdd;
  font-size: 15px; font-weight: 600; list-style: none; }
details.panel summary::-webkit-details-marker { display: none; }
details.panel summary::after { content: " ›"; color: #5f6b84; }
details.panel .tblwrap { margin-top: 10px; }
.heatrow { display: flex; align-items: flex-start; margin: 5px 0; }
.heatrow .hl { flex: 0 0 86px; color: #8b94a7; font-size: 12.5px;
  padding-top: 2px; }
.heatrow .hc { flex: 1; }
.bar { display: inline-block; width: 72px; padding: 2.5px 6px;
  margin: 1.5px; border-radius: 4px; font-size: 11px;
  color: #dbe2f0; text-align: right; }
@media (max-width: 560px) {
  .wrap { padding: 12px 9px 30px; }
  h1 { font-size: 18px; }
  .panel { padding: 12px 11px; }
  td, th { padding: 5px 5px; white-space: normal; }
  table { font-size: 12px; }
  .card .v { font-size: 18px; }
  .score { font-size: 25px; }
  .heatrow .hl { flex-basis: 70px; }
  .bar { width: 60px; }
  button, .btn { padding: 8px 12px; }
}
"""


def _page(title, body, base_href=""):
    base = ('<base href="%s">' % _esc(base_href)) if base_href else ""
    doc = ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,"
           "initial-scale=1'><title>%s</title>%s<style>%s</style></head>"
           "<body><div class='wrap'>%s</div></body></html>"
           % (_esc(title), base, BASE_CSS, body))
    return doc.encode("utf-8")


def _latest_t(actor, metric, target, proto=None):
    with _db_lock, _db() as conn:
        if proto:
            row = conn.execute(
                "SELECT * FROM runs WHERE actor=? AND metric=? "
                "AND target=? AND proto=? ORDER BY id DESC LIMIT 1",
                (actor, metric, target, proto)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM runs WHERE actor=? AND metric=? "
                "AND target=? ORDER BY id DESC LIMIT 1",
                (actor, metric, target)).fetchone()
    return dict(row) if row else None


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
             + ccard("客户端下载", c_down, _fmt_speed)
             + ccard("客户端上传", c_up, _fmt_speed))
    s_rows = []
    for target in ("cloudflare", "hetzner-ash"):
        for proto in ("ipv6", "ipv4"):
            d = _latest_t("server", "down", target, proto)
            if d:
                p = _latest_t("server", "ping", target, proto)
                pv = "%.0f" % _srv_rtt(p) if p else "-"
                s_rows.append(
                    "<tr><td>%s</td><td>%s</td><td>%s</td>"
                    "<td>%s</td><td>%s</td></tr>"
                    % (_esc(target), proto, pv,
                       _mib_short(_to_mibs(d["value"])),
                       _ago(d["ts"])))
    s_up = None
    for proto in ("ipv6", "ipv4"):
        u = _latest_t("server", "up", "cloudflare", proto)
        if u and (s_up is None or u["ts"] > s_up["ts"]):
            s_up = u
    if s_up:
        s_rows.append(
            "<tr><td>cloudflare · 上传</td><td>%s</td><td>-</td>"
            "<td>%s</td><td>%s</td></tr>"
            % (s_up["proto"], _mib_short(_to_mibs(s_up["value"])),
               _ago(s_up["ts"])))
    def _mib(points):
        return [(ts, _to_mibs(v)) for ts, v in points]

    chart = _chart_svg([
        ("客户端下载", "#4f7cff", _mib(_series("client", "down"))),
        ("客户端上传", "#38b2a8", _mib(_series("client", "up"))),
        ("服务器下载", "#e8a13a", _mib(_series("server", "down"))),
        ("服务器上传", "#b06ab3", _mib(_series("server", "up"))),
    ])
    aggs = []
    for label, actor, metric, fmt in (
            ("客户端下载 · MiB/s", "client", "down",
             lambda v: _mib_short(_to_mibs(v))),
            ("客户端上传 · MiB/s", "client", "up",
             lambda v: _mib_short(_to_mibs(v))),
            ("客户端延迟 · ms", "client", "ping",
             lambda v: "%.0f" % v),
            ("服务器下载 · MiB/s", "server", "down",
             lambda v: _mib_short(_to_mibs(v))),
            ("服务器上传 · MiB/s", "server", "up",
             lambda v: _mib_short(_to_mibs(v))),
            ("服务器延迟 · ms", "server", "ping",
             lambda v: "%.0f" % v)):
        a = _agg(actor, metric, transform=(
            _srv_rtt if (actor, metric) == ("server", "ping")
            else None))
        if a:
            aggs.append(
                "<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td>%s</td></tr>"
                % (label, a["n"], fmt(a["avg"]), fmt(a["p50"]),
                   fmt(a["min"]), fmt(a["max"])))
    recent = []
    for r in _recent_runs(15):
        unit = "ms" if r["metric"] == "ping" else "Mbps"
        val = _fmt_ms(_srv_rtt(r)) if (
            r["metric"] == "ping" and r["actor"] == "server") else (
            _fmt_ms(r["value"]) if r["metric"] == "ping" else
            _fmt_speed(r["value"]))
        recent.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td></tr>"
            % (time.strftime("%m-%d %H:%M", _cst(r["ts"])),
               "客户端" if r["actor"] == "client" else "服务器",
               {"ping": "延迟", "down": "下载", "up": "上传"}[r["metric"]],
               _esc(r["proto"] or ""),
               _esc((r["target"] or "").split(":")[0]
                    if r["actor"] == "client" else (r["target"] or "")),
               val, _fmt_bytes(r["bytes"])))
        del unit
    # ---- link scores: one algorithm scores both links
    def _guidance(score, parts):
        if score is None:
            return "先跑一次测速"
        if score >= 85:
            band = "状态优秀"
        elif score >= 70:
            band = "日常够用"
        elif score >= 50:
            band = "勉强可用"
        elif score >= 30:
            band = "状态较差"
        else:
            band = "状态差"
        weak_txt = ""
        if parts:
            weak = min(parts, key=lambda k: parts[k])
            if parts[weak] < 50:
                weak_txt = {"down": "短板在下载", "up": "短板在上传",
                            "lat": "短板在延迟"}[weak]
        return " · ".join(x for x in (band, weak_txt) if x)

    def _detail(run):
        if not run:
            return {}
        try:
            return json.loads(run["detail"] or "{}")
        except Exception:
            return {}

    c_lat = None
    if c_ping:
        c_lat = c_ping["value"] + 0.5 * float(
            _detail(c_ping).get("jitter") or 0)
    c_score, c_parts = _link_score(
        c_down["value"] if c_down else None,
        c_up["value"] if c_up else None, c_lat)

    s_down_run = s_up_run = s_ping_run = None
    for proto in ("ipv6", "ipv4"):
        d = _latest_t("server", "down", "cloudflare", proto)
        if d:
            s_down_run = d
            s_ping_run = _latest_t("server", "ping", "cloudflare",
                                   proto)
            s_up_run = _latest_t("server", "up", "cloudflare", proto)
            break
    s_lat = None
    if s_ping_run:
        conn = _detail(s_ping_run).get("connect_ms")
        s_lat = (float(conn) / 2.0) if conn else (
            s_ping_run["value"] / 3.0)
    s_score, s_parts = _link_score(
        s_down_run["value"] if s_down_run else None,
        s_up_run["value"] if s_up_run else None, s_lat)

    def _score_card(title, score, parts, when, guidance, mid):
        grade, color = _score_grade(score)
        if score is None:
            return ('<div class="card"><div class="k">%s</div>'
                    '<div class="v">暂无</div>'
                    '<div style="font-size:12px;color:#8b94a7">'
                    '%s</div></div>' % (title, guidance))
        det = " · ".join(
            "%s %.0f" % ({"down": "下载", "up": "上传",
                          "lat": "延迟"}[k], v)
            for k, v in parts.items())
        return ('<div class="card" style="cursor:pointer" '
                'onclick="openModal(\'%s\')">'
                '<div class="k">%s · %s</div>'
                '<div class="score" style="color:%s">%.0f '
                '<small style="font-size:14px">%s</small></div>'
                '<div style="font-size:12px;color:#8b94a7">'
                '%s<br>%s<br>'
                '<span style="color:#5f6b84;font-size:11px">'
                '原始数据 ›</span></div></div>'
                % (mid, title, when, color, score, grade, det,
                   guidance))

    score_cards = (
        _score_card("客户端链路得分", c_score, c_parts,
                    _ago(c_down["ts"]) if c_down else "",
                    _guidance(c_score, c_parts), "client")
        + _score_card("服务器链路得分", s_score, s_parts,
                      _ago(s_down_run["ts"]) if s_down_run else "",
                      _guidance(s_score, s_parts), "server"))

    # ---- raw-data popups behind the score cards
    def _sizes_str(detail):
        items = []
        for k, v in (detail.get("sizes") or {}).items():
            if k in ("1MB", "1MiB"):
                continue
            key = k if k.endswith("MiB") else k.replace("MB", "MiB")
            items.append("%s %s" % (_esc(key), _fmt_speed(v)))
        return " · ".join(items) if items else "-"

    def _calc_text(parts, score, lat_note):
        bits = []
        if "down" in parts:
            bits.append("下载子分 %.0f" % parts["down"])
        if "up" in parts:
            bits.append("上传子分 %.0f" % parts["up"])
        if "lat" in parts:
            bits.append("延迟子分 %.0f" % parts["lat"])
        if not bits:
            return "暂无可计算的数据。"
        return ("计算：%s，按权重 45/25/30 加权 = %.0f 分。%s"
                % ("，".join(bits), score, lat_note))

    def _raw_modal(mid, title, pairs, calc):
        if not pairs:
            pairs = [("数据", "暂无")]
        rows = "".join(
            '<tr><td style="color:#8b94a7;white-space:nowrap;'
            'width:30%%">%s</td><td>%s</td></tr>' % (_esc(k), v)
            for k, v in pairs)
        return (
            '<div class="modal" id="modal-%s" '
            'onclick="if(event.target===this)closeModal()">'
            '<div class="box"><div style="display:flex;'
            'justify-content:space-between;align-items:center">'
            '<h3 style="margin:0">%s · 原始数据</h3>'
            '<button class="btn" onclick="closeModal()">关闭</button>'
            '</div>'
            '<div class="tblwrap"><table>%s</table></div>'
            '<p style="color:#8b94a7;font-size:12.5px;'
            'line-height:1.8">%s</p></div></div>'
            % (mid, title, rows, calc))

    c_pairs = []
    if c_ping:
        d = _detail(c_ping)
        c_pairs.append(("延迟", "%s（最小 %s · 抖动 %s）· %s · %s · %s"
                        % (_fmt_ms(c_ping["value"]),
                           _fmt_ms(d.get("min")),
                           _fmt_ms(d.get("jitter")),
                           _esc(c_ping["proto"] or "-"),
                           _esc(c_ping["target"] or "-"),
                           _ago(c_ping["ts"]))))
    if c_down:
        c_pairs.append(("下载", "%s（%s Mbps）· 实传 %s · 阶梯 %s · %s"
                        % (_fmt_speed(c_down["value"]),
                           _fmt_mbps(c_down["value"]),
                           _fmt_bytes(c_down["bytes"]),
                           _sizes_str(_detail(c_down)),
                           _ago(c_down["ts"]))))
    if c_up:
        c_pairs.append(("上传", "%s（%s Mbps）· 实传 %s · 阶梯 %s · %s"
                        % (_fmt_speed(c_up["value"]),
                           _fmt_mbps(c_up["value"]),
                           _fmt_bytes(c_up["bytes"]),
                           _sizes_str(_detail(c_up)),
                           _ago(c_up["ts"]))))
    s_pairs = []
    if s_down_run:
        d = _detail(s_down_run)
        s_pairs.append(("下载 cloudflare/%s" % s_down_run["proto"],
                        "%s（%s Mbps）· 实传 %s · 阶梯 %s · 出口 %s · %s"
                        % (_fmt_speed(s_down_run["value"]),
                           _fmt_mbps(s_down_run["value"]),
                           _fmt_bytes(s_down_run["bytes"]),
                           _sizes_str(d),
                           _esc(d.get("ip") or "-"),
                           _ago(s_down_run["ts"]))))
    if s_up_run:
        d = _detail(s_up_run)
        s_pairs.append(("上传 cloudflare/%s" % s_up_run["proto"],
                        "%s（%s Mbps）· 实传 %s · 阶梯 %s · %s"
                        % (_fmt_speed(s_up_run["value"]),
                           _fmt_mbps(s_up_run["value"]),
                           _fmt_bytes(s_up_run["bytes"]),
                           _sizes_str(d), _ago(s_up_run["ts"]))))
    if s_ping_run:
        d = _detail(s_ping_run)
        s_pairs.append(("延迟 cloudflare/%s" % s_ping_run["proto"],
                        "首字节 %s · 建连 %s · 估算往返 %s · %s"
                        % (_fmt_ms(s_ping_run["value"]),
                           _fmt_ms(d.get("connect_ms")),
                           _fmt_ms(s_lat),
                           _ago(s_ping_run["ts"]))))
    modals_html = (
        _raw_modal("client", "客户端链路得分", c_pairs,
                   _calc_text(c_parts, c_score or 0,
                              "延迟取平均往返加一半抖动；"
                              "带宽子分 = 100×速度/(速度+k)，"
                              "下载 k=0.5、上传 k=0.3（MiB/s）。"))
        + _raw_modal("server", "服务器链路得分", s_pairs,
                     _calc_text(s_parts, s_score or 0,
                                "延迟取建连时间的一半近似往返；"
                                "带宽子分曲线同客户端。")))

    # ---- history: colour strips per series + score history
    def _score_history(actor):
        with _db_lock, _db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM runs WHERE actor=? ORDER BY ts",
                (actor,))]
        buckets = {}
        for r in rows:
            key = int(r["ts"] // 180)
            buckets.setdefault(key, {})[r["metric"]] = r
        out = []
        for key in sorted(buckets):
            b = buckets[key]
            if "down" not in b:
                continue
            lat = None
            if "ping" in b:
                lat = (b["ping"]["value"] if actor == "client"
                       else b["ping"]["value"] / 3.0)
            sc, _p = _link_score(
                b["down"]["value"],
                b["up"]["value"] if "up" in b else None, lat)
            if sc is not None:
                out.append((key * 180, sc))
        return out[-24:]

    def _score_strip(points):
        """Scores on the same data-bar rule, except the scale is
        absolute: the fill length IS the score (100 = full cell),
        and the bar colour carries the grade."""
        if not points:
            return ('<span style="color:#8b94a7;font-size:12px">'
                    '暂无数据</span>')
        cells = []
        for ts, sc in points:
            grade, color = _score_grade(sc)
            pct = int(round(sc))
            cells.append(
                '<span class="bar" title="%s · 得分 %.0f（%s）" '
                'style="background:linear-gradient(90deg,'
                '%s %d%%,%s26 %d%%)">%.0f</span>'
                % (time.strftime("%m-%d %H:%M",
                                 _cst(ts)),
                   sc, grade, color, pct, color, pct, sc))
        return "".join(cells)

    def _mib_val(v):
        return ("%.0f MiB/s" % v) if v >= 100 else (
            ("%.1f MiB/s" % v) if v >= 10 else ("%.2f MiB/s" % v))

    heat_html = (
        '<div class="heatrow"><div class="hl">客户端下载</div>'
        '<div class="hc">%s</div></div>'
        '<div class="heatrow"><div class="hl">客户端上传</div>'
        '<div class="hc">%s</div></div>'
        '<div class="heatrow"><div class="hl">服务器下载</div>'
        '<div class="hc">%s</div></div>'
        '<div class="heatrow"><div class="hl">服务器上传</div>'
        '<div class="hc">%s</div></div>'
        '<div class="heatrow"><div class="hl">客户端得分</div>'
        '<div class="hc">%s</div></div>'
        '<div class="heatrow"><div class="hl">服务器得分</div>'
        '<div class="hc">%s</div></div>'
        % (_bar_strip(_mib(_series("client", "down", limit=20)),
                      "#4f7cff", _mib_short, _mib_val),
           _bar_strip(_mib(_series("client", "up", limit=20)),
                      "#38b2a8", _mib_short, _mib_val),
           _bar_strip(_mib(_series("server", "down", limit=20)),
                      "#e8a13a", _mib_short, _mib_val),
           _bar_strip(_mib(_series("server", "up", limit=20)),
                      "#b06ab3", _mib_short, _mib_val),
           _score_strip(_score_history("client")),
           _score_strip(_score_history("server"))))
    size_lines = []

    def _size_line(label, run):
        if not run:
            return
        try:
            sizes = json.loads(run["detail"] or "{}").get("sizes") or {}
        except Exception:
            sizes = {}
        if not sizes:
            return
        parts = " → ".join(
            "%s %s" % (_esc(k if k.endswith("MiB")
                            else k.replace("MB", "MiB")),
                       _mib_short(_to_mibs(v)))
            for k, v in sizes.items() if k not in ("1MB", "1MiB"))
        if not parts:
            return
        size_lines.append(
            '<tr><td style="color:#8b94a7;white-space:nowrap;'
            'width:30%%">%s</td><td>%s MiB/s</td></tr>'
            % (_esc(label), parts))

    _size_line("客户端下载", c_down)
    _size_line("客户端上传", c_up)
    for target in ("cloudflare", "hetzner-ash"):
        for proto in ("ipv6", "ipv4"):
            d = _latest_t("server", "down", target, proto)
            if d:
                _size_line("服务器下载 %s/%s" % (target, proto), d)
    if s_up:
        _size_line("服务器上传 cloudflare/%s" % s_up["proto"], s_up)
    sizes_html = (
        '<div class="tblwrap"><table>%s</table></div>'
        % ("".join(size_lines) if size_lines else
           '<tr><td style="color:#8b94a7">暂无阶梯数据，'
           '下一次测速后生成</td></tr>'))
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
    # ---- plain-text export of the last 7 days (one-click copy)
    _lines = ["测速数据（截至 %s）"
              % time.strftime("%m-%d %H:%M", _cst(time.time()))]
    for _t, _sc, _pt in (("客户端得分", c_score, c_parts),
                         ("服务器得分", s_score, s_parts)):
        if _sc is not None:
            _lines.append("%s %.0f（%s）" % (
                _t, _sc, " · ".join(
                    "%s %.0f" % ({"down": "下载", "up": "上传",
                                  "lat": "延迟"}[k], v)
                    for k, v in _pt.items())))
    _lines.append("[近 7 天统计] 项目 次数 平均 中位 最小 最大")
    for _label, _actor, _metric, _spd in (
            ("客户端下载(MiB/s)", "client", "down", True),
            ("客户端上传(MiB/s)", "client", "up", True),
            ("客户端延迟(ms)", "client", "ping", False),
            ("服务器下载(MiB/s)", "server", "down", True),
            ("服务器上传(MiB/s)", "server", "up", True),
            ("服务器延迟(ms)", "server", "ping", False)):
        _a = _agg(_actor, _metric, transform=(
            _srv_rtt if (_actor, _metric) == ("server", "ping")
            else None))
        if _a:
            _f = (lambda v: _mib_short(_to_mibs(v))) if _spd else (
                lambda v: "%.0f" % v)
            _lines.append("%s %d %s %s %s %s" % (
                _label, _a["n"], _f(_a["avg"]), _f(_a["p50"]),
                _f(_a["min"]), _f(_a["max"])))
    _lines.append("[最近明细]")
    for _r in _recent_runs(15):
        _val = _fmt_ms(_srv_rtt(_r)) if (
            _r["metric"] == "ping" and _r["actor"] == "server") else (
            _fmt_ms(_r["value"]) if _r["metric"] == "ping" else
            _fmt_speed(_r["value"]))
        _tgt = (_r["target"] or "").split(":")[0] \
            if _r["actor"] == "client" else (_r["target"] or "")
        _lines.append("%s %s %s %s %s %s %s" % (
            time.strftime("%m-%d %H:%M", _cst(_r["ts"])),
            "客户端" if _r["actor"] == "client" else "服务器",
            {"ping": "延迟", "down": "下载", "up": "上传"}[_r["metric"]],
            _r["proto"] or "", _tgt, _val, _fmt_bytes(_r["bytes"])))
    copy_json = json.dumps("\n".join(_lines), ensure_ascii=False).replace(
        "</", "<\\/")
    body = (
        '<div class="topbar"><h1>测速</h1>'
        '<span class="msg">客户端 ↔ 本服务器 · 服务器 ↔ 公网</span></div>'
        '<div class="panel"><h2>链路得分（指导日常使用）</h2>'
        '<div class="cards">%s</div></div>'
        '<div class="panel"><h2>客户端测速（浏览器到这台服务器）</h2>'
        '<div class="cards">%s</div>'
        '<div style="margin-top:12px"><button id="cgo">开始测速</button>'
        '<span class="msg" id="cmsg"></span></div>'
        '<div class="progress" style="margin-top:10px"><i id="cbar"></i></div>'
        '<p style="color:#8b94a7;font-size:12.5px">'
        'h3 = UDP · h2/1.1 = TCP；debi 入口测 IPv6，debi4 测 IPv4。'
        '档位「%s」，一次最多约 %s。</p></div>'
        '<div class="panel"><h2>服务器自测（这台 VPS 到公网）</h2>'
        '<div class="tblwrap"><table><tr><th>目标</th><th>协议</th><th>延迟 · ms</th>'
        '<th>速度 · MiB/s</th><th>时间</th></tr>%s</table></div>'
        '<div style="margin-top:10px">'
        '<button class="ghost" id="sgo">立即自测</button>'
        '<span class="msg" id="smsg">%s</span></div>'
        '<p style="color:#8b94a7;font-size:12.5px">'
        'Cloudflare / Hetzner 两目标，v6、v4 分开测；'
        '定时自测用省流档。</p></div>'
        '<details class="panel"><summary>阶梯明细（最近一次）</summary>%s</details>'
        '<div class="panel"><h2>趋势</h2>%s</div>'
        '<div class="panel"><h2>历史数据条（一格一次测速，仿 Excel 数据条）</h2>%s'
        '<p style="color:#8b94a7;font-size:12.5px">'
        '色块长度 = 数值占比：满格 = 该行最快；得分行满格 = 100 分。</p></div>'
        '<div class="panel"><h2>近 7 天统计</h2>'
        '<div class="tblwrap"><table><tr><th>项目</th><th>次数</th><th>平均</th><th>中位</th>'
        '<th>最小</th><th>最大</th></tr>%s</table></div>'
        '<div style="margin-top:10px">'
        '<button class="ghost" onclick="copyData()">复制近 7 天数据</button>'
        '<span class="msg" id="copymsg"></span></div></div>'
        '<div class="panel"><h2>设置</h2>'
        '<form id="cfg"><div style="display:flex;gap:10px;flex-wrap:wrap;'
        'align-items:center">'
        '<label>测速档位 <select id="profile">%s</select></label>'
        '<label>服务器定时自测 <select id="sched">%s</select></label>'
        '<button type="submit">保存</button>'
        '<span class="msg" id="cfgmsg"></span></div></form>'
        '<p style="color:#8b94a7;font-size:12.5px">阶梯 '
        '4 → 16 → 64 MiB（省流档至 16），相邻档差 &lt;10%% 收敛停测；'
        '历史保留 %d 天。</p></div>'
        '<div class="panel"><h2>最近记录</h2>'
        '<div class="tblwrap"><table><tr><th>时间</th><th>来源</th><th>项目</th><th>协议</th>'
        '<th>目标</th><th>结果</th><th>流量</th></tr>%s</table></div></div>'
        '%s<script>var COPY_TEXT=%s;</script>'
        '<script>%s</script>'
        % (score_cards, cards, _esc(prof["label"]),
           _fmt_bytes(sum(prof["steps"]) * 2),
           "".join(s_rows) or '<tr><td colspan="5">还没有自测记录</td></tr>',
           _esc(_last_test_text()),
           sizes_html, chart, heat_html,
           "".join(aggs) or '<tr><td colspan="6">暂无数据</td></tr>',
           prof_opts, sched_opts, RETENTION_DAYS,
           "".join(recent) or '<tr><td colspan="7">暂无记录</td></tr>',
           modals_html, copy_json, CLIENT_JS))
    return _page("测速", body, base_href=base_href)


CLIENT_JS = r"""
const $=id=>document.getElementById(id);
async function jpost(u,b){const r=await fetch(u,{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});
return r.json();}
function setBar(p){$("cbar").style.width=(p*100)+"%";}
function copyData(){
  const t=window.COPY_TEXT||"";
  const done=()=>{$("copymsg").textContent="已复制";};
  const fb=()=>{
    const ta=document.createElement("textarea");
    ta.value=t;ta.style.position="fixed";ta.style.opacity="0";
    document.body.appendChild(ta);ta.select();
    try{document.execCommand("copy");done();}
    catch(e){$("copymsg").textContent="复制失败";}
    ta.remove();};
  if(navigator.clipboard&&navigator.clipboard.writeText)
    navigator.clipboard.writeText(t).then(done).catch(fb);
  else fb();
}
function openModal(k){var m=document.getElementById("modal-"+k);
  if(m)m.classList.add("open");}
function closeModal(){document.querySelectorAll(".modal.open")
  .forEach(m=>m.classList.remove("open"));}
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
async function downStep(n,onprog){
  const t0=performance.now();let got=0,firstT=0,firstB=0;
  const r=await fetch("api/dl?bytes="+n,{cache:"no-store"});
  const rd=r.body.getReader();
  while(true){
    const {done,value}=await rd.read();
    if(done)break;
    got+=value.length;
    const el=(performance.now()-t0)/1000;
    if(!firstT&&el>0.3){firstT=el;firstB=got;}
    if(onprog)onprog(got/n);
    if(el>10){try{await rd.cancel();}catch(e){}break;}
  }
  const total=(performance.now()-t0)/1000;
  const secs=total-(firstT||0),bytes=got-(firstB||0);
  return {mbps:(secs>0.2?bytes*8/secs:got*8/total)/1e6,bytes:got};
}
async function downTest(steps){
  const sizes={};let spent=0,prev=0,rate=0;
  for(let i=0;i<steps.length;i++){
    const n=steps[i];
    const r=await downStep(n,f=>{
      setBar(0.15+0.45*((i+f)/steps.length));});
    rate=r.mbps;spent+=r.bytes;
    sizes[(n/1048576)+"MiB"]=Math.round(rate*10)/10;
    if(prev>0&&rate>0&&Math.abs(rate-prev)/Math.max(rate,prev)<0.10)
      break;
    prev=rate;
  }
  return {mbps:rate,bytes:spent,sizes:sizes};
}
function upStep(n){
  return new Promise((res,rej)=>{
    const buf=new Uint8Array(1048576);
    if(window.crypto)crypto.getRandomValues(buf.subarray(0,65536));
    const parts=[];let left=n;
    while(left>0){const c=Math.min(left,buf.length);
      parts.push(buf.subarray(0,c));left-=c;}
    const t0=performance.now();
    const x=new XMLHttpRequest();
    x.open("POST","api/up");
    x.onload=()=>{const secs=(performance.now()-t0)/1000;
      res({mbps:n*8/secs/1e6,bytes:n});};
    x.onerror=()=>rej(new Error("上传中断"));
    x.send(new Blob(parts));
  });
}
async function upTest(steps){
  const sizes={};let spent=0,prev=0,rate=0;
  for(let i=0;i<steps.length;i++){
    const n=steps[i];
    const r=await upStep(n);
    rate=r.mbps;spent+=r.bytes;
    sizes[(n/1048576)+"MiB"]=Math.round(rate*10)/10;
    setBar(0.6+0.3*((i+1)/steps.length));
    if(prev>0&&rate>0&&Math.abs(rate-prev)/Math.max(rate,prev)<0.10)
      break;
    prev=rate;
  }
  return {mbps:rate,bytes:spent,sizes:sizes};
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
    msg("测下载…");const d=await downTest(cfg.steps);setBar(0.6);
    msg("测上传…");const u=await upTest(cfg.steps);setBar(0.92);
    const proto=protoUsed();
    await jpost("api/client-result",{ping:p,down:d,up:u,proto:proto,
      family:window._fam||"unknown",host:location.host});
    setBar(1);
    const mibs=x=>(x*0.11921).toFixed(2);
    msg("完成：延迟 "+p.avg.toFixed(0)+" ms · 下载 "+mibs(d.mbps)
      +" MiB/s（"+proto+"） · 上传 "+mibs(u.mbps)
      +" MiB/s。刷新页面看得分、趋势与统计。");
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
        n = max(0, min(n, PROFILES["full"]["steps"][-1]))
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
        if length > PROFILES["full"]["steps"][-1]:
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
                      "caps": {"c_down": prof["steps"][-1],
                               "c_up": prof["steps"][-1]},
                      "steps": prof["steps"],
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
                     {"family": family,
                      "sizes": down.get("sizes") or {}})
        if up.get("mbps"):
            _add_run("client", "up", proto, host,
                     float(up["mbps"]), int(up.get("bytes") or 0),
                     {"family": family,
                      "sizes": up.get("sizes") or {}})
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
