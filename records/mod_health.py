"""Personal health records for the records hub.

Implements the user's health-record design: structured quick records
across fixed categories (food, stool, intimacy, state, sleep,
exercise, medication, symptom) with preset items, typed values,
context tags, display codes for sensitive categories, regularity
suggestions, light correlation insights, and JSON/CSV export plus
idempotent import (uuid-keyed, tombstone-aware).

Data lives in health.db inside the platform data directory. Nothing
is sent to any external service.
"""
import csv
import io
import json
import os
import sqlite3
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta

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
DB_PATH = os.path.join(RUNTIME, "health.db")

from webutil import json_response, read_json

_lock = threading.Lock()

CATEGORIES = {
    "food": {"name": "食物", "code": "食", "color": "#e8a13a", "sensitive": False},
    "stool": {"name": "大便", "code": "BM", "color": "#a9805a", "sensitive": True},
    "intimacy": {"name": "性生活", "code": "INT", "color": "#b06ab3", "sensitive": True},
    "sleep": {"name": "睡眠", "code": "眠", "color": "#5b6ee1", "sensitive": False},
    "exercise": {"name": "运动", "code": "动", "color": "#43a06a", "sensitive": False},
    "note": {"name": "备注", "code": "记", "color": "#7d8590", "sensitive": False},
}

DEFAULT_PRESETS = {
    "food": ["食物"],
    "stool": ["排便"],
    "intimacy": ["性生活"],
    "sleep": ["睡眠"],
    "note": ["备注"],
    "exercise": ["步行", "跑步", "骑行", "游泳", "健身", "拉伸", "瑜伽"],
}

FOOD_ATTRS = ["高脂", "高糖", "高盐", "高纤维", "高蛋白", "油炸", "烧烤",
              "辣", "奶制品", "麸质", "酒精", "咖啡因", "大份", "空腹", "吃得快"]
CONTEXT_TAGS = ["熬夜", "压力大", "出差", "饮酒后", "运动后", "生病", "假期"]

# Tags are per-category: each kind of record has its own relevant set.
TAG_POOLS = {
    "food": FOOD_ATTRS,
    "stool": ["腹痛", "腹胀", "很急", "没排干净", "疼痛", "熬夜", "吃辣后", "饮酒后"],
    "intimacy": ["放松", "疲劳", "压力大", "假期"],
    "sleep": ["熬夜", "午睡后", "早起", "失眠", "多梦", "喝咖啡", "饮酒后", "运动后", "起床即散", "持续到上午", "大半天都在"],
    "exercise": ["晨练", "夜跑", "空腹", "饭后", "大强度", "拉伸到位"],
}

VALUE_TYPES = ("count", "scale", "enum", "measure", "bool")


def _db():
    os.makedirs(RUNTIME, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS health_records(
                id TEXT PRIMARY KEY, ts INTEGER NOT NULL, date TEXT NOT NULL,
                category TEXT NOT NULL, item TEXT NOT NULL,
                value_json TEXT NOT NULL DEFAULT 'null',
                tags_json TEXT NOT NULL DEFAULT '[]',
                note TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_health_date ON health_records(date);
            CREATE TABLE IF NOT EXISTS health_presets(
                category TEXT NOT NULL, item TEXT NOT NULL,
                PRIMARY KEY(category, item));
            CREATE TABLE IF NOT EXISTS health_settings(
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        for cat, items in DEFAULT_PRESETS.items():
            for item in items:
                conn.execute(
                    "INSERT OR IGNORE INTO health_presets(category, item) VALUES (?, ?)",
                    (cat, item))


def _row_out(r):
    return {"id": r["id"], "ts": r["ts"], "date": r["date"],
            "category": r["category"], "item": r["item"],
            "value": json.loads(r["value_json"] or "null"),
            "tags": json.loads(r["tags_json"] or "[]"),
            "note": r["note"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "deleted": bool(r["deleted"])}


def _settings():
    with _db() as conn:
        rows = dict(conn.execute("SELECT key, value FROM health_settings").fetchall())
    codes = dict(json.loads(rows["codes"])) if "codes" in rows else {}
    predict = dict(json.loads(rows["predict"])) if "predict" in rows else {}
    merged_codes = {cat: codes.get(cat, meta["code"]) for cat, meta in CATEGORIES.items()}
    merged_predict = {cat: bool(predict.get(cat, True)) for cat in CATEGORIES}
    return {"codes": merged_codes, "predict": merged_predict}


def _save_settings(data):
    cur = _settings()
    if isinstance(data.get("codes"), dict):
        for cat, code in data["codes"].items():
            if cat in CATEGORIES and isinstance(code, str) and 0 < len(code.strip()) <= 12:
                cur["codes"][cat] = code.strip()
    if isinstance(data.get("predict"), dict):
        for cat, flag in data["predict"].items():
            if cat in CATEGORIES:
                cur["predict"][cat] = bool(flag)
    with _lock, _db() as conn:
        conn.execute("INSERT OR REPLACE INTO health_settings(key, value) VALUES ('codes', ?)",
                     (json.dumps(cur["codes"], ensure_ascii=False),))
        conn.execute("INSERT OR REPLACE INTO health_settings(key, value) VALUES ('predict', ?)",
                     (json.dumps(cur["predict"], ensure_ascii=False),))
    return cur


def _validate_record(data, partial=False):
    """Returns (clean dict, error). With partial=True only given fields."""
    out = {}
    if not partial or "category" in data:
        cat = str(data.get("category") or "")
        if cat not in CATEGORIES:
            return None, "类别无效。"
        out["category"] = cat
    if not partial or "item" in data:
        item = str(data.get("item") or "").strip()
        if not (1 <= len(item) <= 40):
            return None, "项目名称需为 1–40 个字符。"
        out["item"] = item
    if not partial or "ts" in data:
        try:
            ts = int(data.get("ts") or 0)
        except (TypeError, ValueError):
            return None, "时间无效。"
        if ts <= 0:
            return None, "时间无效。"
        out["ts"] = ts
    if not partial or "date" in data:
        date = str(data.get("date") or "")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return None, "日期无效。"
        out["date"] = date
    if "value" in data:
        v = data.get("value")
        if v is not None:
            if not isinstance(v, dict) or v.get("type") not in VALUE_TYPES:
                return None, "值类型无效。"
            out["value"] = v
        else:
            out["value"] = None
    if "tags" in data:
        tags = data.get("tags")
        if not isinstance(tags, list) or len(tags) > 16:
            return None, "标签无效。"
        clean = []
        for t in tags:
            t = str(t).strip()
            if 1 <= len(t) <= 20 and t not in clean:
                clean.append(t)
        out["tags"] = clean
    if "note" in data:
        note = str(data.get("note") or "")
        if len(note) > 500:
            return None, "备注过长。"
        out["note"] = note
    return out, None


# ---------------------------------------------------------------- handlers

def _day(environ, start_response):
    from urllib.parse import parse_qs
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    date = (qs.get("date") or [""])[0]
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return json_response(start_response, {"ok": False, "error": "日期无效。"}, 400)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM health_records WHERE date = ? AND deleted = 0 ORDER BY ts",
            (date,)).fetchall()
    records = [_row_out(r) for r in rows]
    summary = {}
    for r in records:
        v = r["value"] or {}
        if r["category"] == "state" and r["item"] in ("心情", "精力") and v.get("type") == "scale":
            summary[r["item"]] = v.get("v")
        if r["category"] == "state" and r["item"] == "心情备注" and r["note"]:
            summary["心情备注"] = r["note"]
        if r["category"] == "note" and r["note"]:
            summary["备注"] = r["note"]
        if r["category"] == "sleep":
            for t in r["tags"]:
                if t.startswith("残留困意·"):
                    summary["残留困意"] = t[len("残留困意·"):]
        if r["category"] == "sleep" and r["item"] == "残留困意" and v.get("type") == "enum":
            summary["残留困意"] = v.get("v")
        if r["category"] == "sleep" and v.get("type") == "measure":
            summary["睡眠"] = v.get("v")
        if r["category"] == "sleep" and r["item"] == "睡眠" and v.get("type") == "enum":
            summary["睡眠长短"] = v.get("v")
    return json_response(start_response,
                         {"ok": True, "date": date, "records": records, "summary": summary})


def _month(environ, start_response):
    from urllib.parse import parse_qs
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    ym = (qs.get("ym") or [""])[0]
    try:
        datetime.strptime(ym, "%Y-%m")
    except ValueError:
        return json_response(start_response, {"ok": False, "error": "月份无效。"}, 400)
    with _db() as conn:
        rows = conn.execute(
            "SELECT date, category FROM health_records WHERE date LIKE ? AND deleted = 0",
            (ym + "-%",)).fetchall()
    days = {}
    for r in rows:
        days.setdefault(r["date"], [])
        if r["category"] not in days[r["date"]]:
            days[r["date"]].append(r["category"])
    return json_response(start_response, {"ok": True, "days": days})


def _create(environ, start_response):
    data = read_json(environ)
    if not isinstance(data, dict):
        return json_response(start_response, {"ok": False, "error": "请求体无效。"}, 400)
    clean, err = _validate_record(data)
    if err:
        return json_response(start_response, {"ok": False, "error": err}, 400)
    now = int(time.time())
    rid = str(data.get("id") or "") or uuid.uuid4().hex
    with _lock, _db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO health_records"
            "(id, ts, date, category, item, value_json, tags_json, note, created_at, updated_at, deleted)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (rid, clean["ts"], clean["date"], clean["category"], clean["item"],
             json.dumps(clean.get("value"), ensure_ascii=False),
             json.dumps(clean.get("tags", []), ensure_ascii=False),
             clean.get("note", ""), now, now))
        conn.execute("INSERT OR IGNORE INTO health_presets(category, item) VALUES (?, ?)",
                     (clean["category"], clean["item"]))
        row = conn.execute("SELECT * FROM health_records WHERE id = ?", (rid,)).fetchone()
    return json_response(start_response,
                         {"ok": True, "record": _row_out(row), "created": cur.rowcount > 0})


def _update(environ, start_response, rid):
    data = read_json(environ)
    if not isinstance(data, dict):
        return json_response(start_response, {"ok": False, "error": "请求体无效。"}, 400)
    clean, err = _validate_record(data, partial=True)
    if err:
        return json_response(start_response, {"ok": False, "error": err}, 400)
    sets, args = [], []
    for key in ("ts", "date", "category", "item", "note"):
        if key in clean:
            sets.append(key + " = ?")
            args.append(clean[key])
    if "value" in clean:
        sets.append("value_json = ?")
        args.append(json.dumps(clean["value"], ensure_ascii=False))
    if "tags" in clean:
        sets.append("tags_json = ?")
        args.append(json.dumps(clean["tags"], ensure_ascii=False))
    if not sets:
        return json_response(start_response, {"ok": False, "error": "没有可更新的字段。"}, 400)
    sets.append("updated_at = ?")
    args.append(int(time.time()))
    args.append(rid)
    with _lock, _db() as conn:
        cur = conn.execute(
            "UPDATE health_records SET " + ", ".join(sets) + " WHERE id = ? AND deleted = 0", args)
        if cur.rowcount == 0:
            return json_response(start_response, {"ok": False, "error": "记录不存在。"}, 404)
        row = conn.execute("SELECT * FROM health_records WHERE id = ?", (rid,)).fetchone()
    return json_response(start_response, {"ok": True, "record": _row_out(row)})


def _delete(start_response, rid):
    with _lock, _db() as conn:
        cur = conn.execute(
            "UPDATE health_records SET deleted = 1, updated_at = ? WHERE id = ? AND deleted = 0",
            (int(time.time()), rid))
    if cur.rowcount == 0:
        return json_response(start_response, {"ok": False, "error": "记录不存在。"}, 404)
    return json_response(start_response, {"ok": True})


def _presets(environ, start_response, method):
    if method == "GET":
        with _db() as conn:
            rows = conn.execute("SELECT category, item FROM health_presets").fetchall()
            cat_rows = conn.execute(
                "SELECT category, COUNT(*) FROM health_records WHERE deleted = 0"
                " GROUP BY category").fetchall()
            item_rows = conn.execute(
                "SELECT category, item, COUNT(*) FROM health_records WHERE deleted = 0"
                " GROUP BY category, item").fetchall()
        out = {cat: [] for cat in CATEGORIES}
        for r in rows:
            out.setdefault(r["category"], []).append(r["item"])
        item_counts = {}
        for r in item_rows:
            item_counts.setdefault(r[0], {})[r[1]] = r[2]
        return json_response(start_response, {"ok": True, "presets": out,
                                              "food_attrs": FOOD_ATTRS,
                                              "context_tags": CONTEXT_TAGS,
                                              "tag_pools": TAG_POOLS,
                                              "cat_counts": {r[0]: r[1] for r in cat_rows},
                                              "item_counts": item_counts,
                                              "categories": CATEGORIES})
    data = read_json(environ)
    cat = str((data or {}).get("category") or "")
    item = str((data or {}).get("item") or "").strip()
    if cat not in CATEGORIES or not (1 <= len(item) <= 40):
        return json_response(start_response, {"ok": False, "error": "预设项无效。"}, 400)
    with _lock, _db() as conn:
        conn.execute("INSERT OR IGNORE INTO health_presets(category, item) VALUES (?, ?)",
                     (cat, item))
    return json_response(start_response, {"ok": True})


def _settings_api(environ, start_response, method):
    if method == "GET":
        return json_response(start_response, {"ok": True, **_settings()})
    data = read_json(environ)
    if not isinstance(data, dict):
        return json_response(start_response, {"ok": False, "error": "请求体无效。"}, 400)
    return json_response(start_response, {"ok": True, **_save_settings(data)})


def _suggest(start_response):
    settings = _settings()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    weekend = now.weekday() >= 5
    with _db() as conn:
        rows = conn.execute(
            "SELECT category, item, ts, date FROM health_records WHERE deleted = 0").fetchall()
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    out = []
    for cat, recs in by_cat.items():
        if not settings["predict"].get(cat, True):
            continue
        if any(r["date"] == today for r in recs):
            continue
        same_kind = [r for r in recs
                     if (datetime.strptime(r["date"], "%Y-%m-%d").weekday() >= 5) == weekend]
        pool = same_kind if len(same_kind) >= 5 else recs
        if len(pool) < 5:
            continue
        minutes = sorted((r["ts"] % 86400 + 8 * 3600) % 86400 // 60 for r in pool)
        median = statistics.median(minutes)
        within = sum(1 for m in minutes if abs(m - median) <= 60)
        if within / len(minutes) < 0.7:
            continue
        items = {}
        for r in pool:
            items[r["item"]] = items.get(r["item"], 0) + 1
        top_item = max(items, key=items.get)
        lo, hi = int(median) - 30, int(median) + 30
        fmt = lambda m: "%02d:%02d" % ((m // 60) % 24, m % 60)
        out.append({"category": cat, "item": top_item, "count": len(pool),
                    "window": fmt(lo) + "–" + fmt(hi), "median_min": int(median)})
    out.sort(key=lambda x: x["median_min"])
    return json_response(start_response, {"ok": True, "suggestions": out})


def _insights(start_response):
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM health_records WHERE deleted = 0 ORDER BY ts").fetchall()
    recs = [_row_out(r) for r in rows]
    days = sorted({r["date"] for r in recs})
    payload = {"ok": True, "days_recorded": len(days), "days_needed": max(0, 14 - len(days))}
    if len(days) < 14:
        return json_response(start_response, payload)
    by_date = {}
    for r in recs:
        by_date.setdefault(r["date"], []).append(r)

    def day_value(date, cat, item, vtype):
        for r in by_date.get(date, []):
            v = r["value"] or {}
            if r["category"] == cat and r["item"] == item and v.get("type") == vtype:
                return v.get("v")
        return None

    recent = days[-14:]
    payload["trends"] = {
        "sleep": [{"date": d, "v": day_value(d, "sleep", "睡眠", "measure")} for d in recent],
        "energy": [{"date": d, "v": day_value(d, "state", "精力", "scale")} for d in recent],
    }
    stool = [r for r in recs if r["category"] == "stool"]
    mid = [r for r in stool if (r["value"] or {}).get("v") in (3, 4)]
    payload["bristol"] = {"total": len(stool),
                          "share_mid": round(len(mid) / len(stool), 3) if stool else None,
                          "per_week": round(len(stool) / max(1, len(days)) * 7, 1)}
    # correlations: food attribute (or context tag) on day D vs outcome on D+1
    outcomes = {
        "腹胀明显": lambda d: (day_value(d, "state", "腹胀", "scale") or 0) >= 3,
        "精力偏低": lambda d: (day_value(d, "state", "精力", "scale") or 99) <= 2,
        "睡眠不足": lambda d: (day_value(d, "sleep", "睡眠", "measure") or 99) < 6,
    }
    exposures = {}
    for r in recs:
        if r["category"] == "food":
            for t in r["tags"]:
                exposures.setdefault(t, set()).add(r["date"])
        for t in r["tags"]:
            if t in CONTEXT_TAGS:
                exposures.setdefault(t, set()).add(r["date"])
    cards = []
    all_dates = set(days)
    for tag, exp_dates in exposures.items():
        if len(exp_dates) < 5:
            continue
        nxt = {(datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
               for d in exp_dates}
        rest = {d for d in all_dates if d not in nxt and d > min(days)}
        for label, fn in outcomes.items():
            hit = sum(1 for d in nxt if d in by_date and fn(d))
            base_hit = sum(1 for d in rest if d in by_date and fn(d))
            if len(nxt & all_dates) < 5 or len(rest) < 5:
                continue
            rate = hit / len(nxt & all_dates)
            base = base_hit / len(rest)
            if rate - base >= 0.2:
                cards.append({"tag": tag, "outcome": label,
                              "rate": round(rate, 2), "base": round(base, 2),
                              "n": len(nxt & all_dates)})
    payload["correlations"] = cards[:6]
    return json_response(start_response, payload)


def _value_display(value, names_mode, cat_name):
    if not value:
        return ""
    t, v = value.get("type"), value.get("v")
    if t == "bool":
        return "是" if v else "否"
    if t == "enum" and isinstance(v, int):
        return "布里斯托 %d 型" % v if cat_name == "stool" else str(v)
    if t == "measure":
        return "%s%s" % (v, value.get("unit", ""))
    if t == "count":
        return "%s次" % v
    if t == "scale":
        return "%s分" % v
    return str(v)


def _export(environ, start_response):
    from urllib.parse import parse_qs
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    fmt = (qs.get("format") or ["json"])[0]
    names = (qs.get("names") or ["real"])[0]
    settings = _settings()
    with _db() as conn:
        rows = conn.execute("SELECT * FROM health_records ORDER BY ts").fetchall()
        preset_rows = conn.execute("SELECT category, item FROM health_presets").fetchall()
    recs = [_row_out(r) for r in rows]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "time", "category", "item", "value", "tags", "note",
                    "id", "updated_at", "deleted"])
        for r in recs:
            meta = CATEGORIES[r["category"]]
            cat = meta["name"] if names == "real" else settings["codes"][r["category"]]
            w.writerow([r["date"],
                        datetime.fromtimestamp(r["ts"]).strftime("%H:%M"),
                        cat, r["item"], _value_display(r["value"], names, r["category"]),
                        ";".join(r["tags"]), r["note"], r["id"], r["updated_at"],
                        1 if r["deleted"] else 0])
        body = buf.getvalue().encode("utf-8-sig")
        fname = "health-records-%s.csv" % datetime.now().strftime("%Y%m%d")
        start_response("200 OK", [("Content-Type", "text/csv; charset=utf-8"),
                                  ("Content-Disposition", 'attachment; filename="%s"' % fname),
                                  ("Content-Length", str(len(body)))])
        return [body]
    presets = {}
    for r in preset_rows:
        presets.setdefault(r["category"], []).append(r["item"])
    payload = {"format": "health-backup", "version": 1,
               "exported_at": int(time.time()), "codes": settings["codes"],
               "presets": presets, "records": recs}
    body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
    fname = "health-backup-%s.json" % datetime.now().strftime("%Y%m%d")
    start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                              ("Content-Disposition", 'attachment; filename="%s"' % fname),
                              ("Content-Length", str(len(body)))])
    return [body]


def _import_counts(payload, tie):
    if not isinstance(payload, dict) or payload.get("format") != "health-backup":
        return None, "文件格式不对，需要健康记录的 JSON 备份。"
    incoming = payload.get("records")
    if not isinstance(incoming, list) or len(incoming) > 10000:
        return None, "备份内容无效。"
    counts = {"added": 0, "updated": 0, "skipped": 0, "conflicts": 0}
    plan = []
    with _db() as conn:
        local = {r["id"]: r for r in conn.execute("SELECT * FROM health_records").fetchall()}
    for rec in incoming:
        if not isinstance(rec, dict) or not rec.get("id"):
            counts["skipped"] += 1
            continue
        if rec.get("category") in ("state", "symptom", "medication"):
            rec = dict(rec)
            old_item = str(rec.get("item") or "").strip()
            old_note = str(rec.get("note") or "").strip()
            rec["category"] = "note"
            rec["item"] = "备注"
            rec["value"] = None
            rec["tags"] = []
            rec["note"] = (old_item + "：" + old_note) if old_item and old_note else (old_note or old_item)
        clean, err = _validate_record(rec)
        if err:
            counts["skipped"] += 1
            continue
        rid = str(rec["id"])
        upd = int(rec.get("updated_at") or 0)
        if rid not in local:
            counts["added"] += 1
            plan.append(("insert", rec))
        elif upd > local[rid]["updated_at"]:
            counts["updated"] += 1
            plan.append(("replace", rec))
        elif upd < local[rid]["updated_at"]:
            counts["skipped"] += 1
        else:
            counts["conflicts"] += 1
            if tie == "import":
                plan.append(("replace", rec))
    return (counts, plan), None


def _import(environ, start_response, apply):
    data = read_json(environ)
    if not isinstance(data, dict):
        return json_response(start_response, {"ok": False, "error": "请求体无效。"}, 400)
    tie = data.get("tie") if data.get("tie") in ("local", "import") else "local"
    result, err = _import_counts(data.get("payload"), tie)
    if err:
        return json_response(start_response, {"ok": False, "error": err}, 400)
    counts, plan = result
    if apply:
        with _lock, _db() as conn:
            for _op, rec in plan:
                clean, _ = _validate_record(rec)
                conn.execute(
                    "INSERT OR REPLACE INTO health_records"
                    "(id, ts, date, category, item, value_json, tags_json, note,"
                    " created_at, updated_at, deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(rec["id"]), clean["ts"], clean["date"], clean["category"],
                     clean["item"], json.dumps(clean.get("value"), ensure_ascii=False),
                     json.dumps(clean.get("tags", []), ensure_ascii=False),
                     clean.get("note", ""), int(rec.get("created_at") or rec["updated_at"] or 0),
                     int(rec.get("updated_at") or 0), 1 if rec.get("deleted") else 0))
                conn.execute("INSERT OR IGNORE INTO health_presets(category, item) VALUES (?, ?)",
                             (clean["category"], clean["item"]))
    return json_response(start_response, {"ok": True, "counts": counts, "applied": apply})


def api(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "")
    sub = path[len("/api/health"):]
    if sub == "/day" and method == "GET":
        return _day(environ, start_response)
    if sub == "/month" and method == "GET":
        return _month(environ, start_response)
    if sub == "/records" and method == "POST":
        return _create(environ, start_response)
    if sub.startswith("/records/") and method == "POST":
        return _update(environ, start_response, sub[len("/records/"):])
    if sub.startswith("/records/") and method == "DELETE":
        return _delete(start_response, sub[len("/records/"):])
    if sub == "/presets" and method in ("GET", "POST"):
        return _presets(environ, start_response, method)
    if sub == "/settings" and method in ("GET", "POST"):
        return _settings_api(environ, start_response, method)
    if sub == "/suggest" and method == "GET":
        return _suggest(start_response)
    if sub == "/insights" and method == "GET":
        return _insights(start_response)
    if sub == "/export" and method == "GET":
        return _export(environ, start_response)
    if sub == "/import/preview" and method == "POST":
        return _import(environ, start_response, apply=False)
    if sub == "/import/apply" and method == "POST":
        return _import(environ, start_response, apply=True)
    return json_response(start_response, {"ok": False, "error": "接口不存在。"}, 404)
