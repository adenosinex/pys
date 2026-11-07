# -*- coding: utf-8 -*-
"""野兽素材库 (beastlib): MrBeast 文案材料库。

一条记录 = 一篇 BV 摘要 (md) + 配对原文 (txt)。身份与排序:
  - 序号: 按 BV 号排序确定性分配 (#001 起), 重导入不变;
  - YouTube video_id 为规范主键字段: 本批 front-matter 全为 null,
    只允许用「用户自己的 YouTube 下载清单」(seed/match_source.csv)
    按英文标题匹配回填 —— 确定匹配才落库, 疑似只进报告待裁决,
    未匹配留空。BV 号只是来源标识, 不冒充主键;
  - title_zh (中文名) 是标准名: 列表/详情/搜索都以它为准。
无项目内验证 (面板 /web 门负责)。数据落平台数据目录 (DATA_DIR 解析)。
"""
import csv
import html as _html
import json
import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict

from flask import Flask, abort, g, redirect, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(BASE_DIR, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(BASE_DIR, ".runtime")


DATA_DIR = _data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "beastlib.db")
SEED_DIR = os.path.join(BASE_DIR, "seed")

app = Flask(__name__)

# ---------------------------------------------------------------- fm / md

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


def parse_front_matter(text):
    """Return (fm dict, body). Values kept as-is; null stays None."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}, text
    raw, body = m.group(1), text[m.end():]
    fm = {}
    if yaml is not None:
        try:
            data = yaml.safe_load(raw)
            if isinstance(data, dict):
                fm = data
        except Exception:
            fm = {}
    if not fm:
        for line in raw.split("\n"):
            mm = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
            if not mm:
                continue
            k, v = mm.group(1), mm.group(2).strip()
            if v in ("", "null", "~"):
                fm[k] = None
            elif len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                fm[k] = v[1:-1]
            else:
                fm[k] = v
    return fm, body


def extract_keywords(body):
    m = re.search(r"^##\s*检索关键词\s*\n(.*?)(?=^##\s|\Z)", body, re.S | re.M)
    if not m:
        return ""
    for line in m.group(1).split("\n"):
        line = line.strip()
        if line:
            return line
    return ""


def _inline(s):
    s = _html.escape(s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


def render_md(body):
    """Minimal safe renderer for this corpus' regular md structure."""
    out, para, lst = [], [], None

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_list():
        nonlocal lst
        if lst is not None:
            out.append("</" + lst + ">")
            lst = None

    for raw in body.split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush_para()
            flush_list()
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush_para()
            flush_list()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>" + _inline(m.group(2)) + f"</h{lvl}>")
            continue
        m = re.match(r"^[-*]\s+(.*)$", s)
        if m:
            flush_para()
            if lst != "ul":
                flush_list()
                out.append("<ul>")
                lst = "ul"
            out.append("<li>" + _inline(m.group(1)) + "</li>")
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", s)
        if m:
            flush_para()
            if lst != "ol":
                flush_list()
                out.append("<ol>")
                lst = "ol"
            out.append("<li>" + _inline(m.group(1)) + "</li>")
            continue
        flush_list()
        para.append(s)
    flush_para()
    flush_list()
    return "\n".join(out)


# ---------------------------------------------------------------- matcher
# 与离线调校版完全一致的算法 (v5): Dice 词元相似 + 数字短语合并 +
# >=100 数字冲突门 + 大额数字锚点等值通道。只降级不提升的人工复核表:
DEMOTE = {
    "BV12z7wzLEX1": "算法按同题材词元误配至碰撞视频; 摘要实为 1 美元到 1 亿美元逐车测评, 清单内另有 $100,000,000 Car Doors 共享大额锚点, 不唯一, 降级待裁决",
    "BV16U411S7cX": "仅大额锚点命中 (dice 0.50), 核心动作 hide 与候选 fight 不一致, 降级待裁决",
}

_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
             "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
             "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
             "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
             "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
             "hundred": 100, "thousand": 1000, "million": 1000000,
             "billion": 1000000000}
_SCALES = {100, 1000, 1000000, 1000000000}
_STOP = set("""i a an the to in on for of and or my me you your it its this that
with from into at by be is was are win wins won keep keeps kept survive survives
surviving survived trapped compete competes competing challenge tries try
trying lives lived live living get gets got make makes go goes went inside
outside every day days hour hours""".split())
_SYN = {"lbs": "lb", "pounds": "lb", "pound": "lb", "people": "person",
        "kids": "kid", "children": "kid", "creators": "creator",
        "youtubers": "creator", "strangers": "stranger", "cubes": "cube",
        "built": "build", "building": "build", "aged": "age", "ocean": "sea",
        "contestants": "contestant", "participants": "participant",
        "youtuber": "creator", "youtube": "yt"}


def _stem(t):
    if len(t) > 5 and t.endswith("ing"):
        t = t[:-3]
        if len(t) > 2 and t[-1] == t[-2]:
            t = t[:-1]
    elif len(t) > 4 and t.endswith("ed"):
        t = t[:-2]
        if len(t) > 2 and t[-1] == t[-2]:
            t = t[:-1]
    elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return t


def _combine(nums):
    out, result, buf = [], 0, 0
    for v in nums:
        if v in _SCALES and v >= 1000:
            result += (buf or 1) * v
            buf = 0
        elif v == 100:
            buf = (buf or 1) * 100
        else:
            buf += v
    if result or buf:
        out.append(str(result + buf))
    return out


def _tokens(s):
    s = (s or "").lower().replace(",", "").replace("$", " $ ")
    s = re.sub(r"half a million", " 500000 ", s)
    s = re.sub(r"[^a-z0-9$ ]+", " ", s)
    raw = []
    for t in s.split():
        if t == "$":
            continue
        if t in _NUMWORDS:
            raw.append(("n", _NUMWORDS[t]))
            continue
        if t in _STOP:
            continue
        t = _SYN.get(t, t)
        t = _stem(t)
        t = _SYN.get(t, t)
        if t in _STOP or not t:
            continue
        if t.isdigit():
            raw.append(("n", int(t)))
        else:
            raw.append(("w", t))
    out, run = [], []

    def flush_run():
        nonlocal run
        if not run:
            return
        if len(run) > 1 and not any(v in _SCALES for v in run):
            out.extend(str(v) for v in run)
        else:
            out.extend(_combine(run))
        run = []

    for kind, v in raw:
        if kind == "n":
            run.append(v)
        else:
            flush_run()
            out.append(v)
    flush_run()
    return out


def _bignums(tok_set):
    return sorted(t for t in tok_set if t.isdigit() and int(t) >= 100)


def match_records(records, cand_rows):
    """records: [{bv, to}]; cand_rows: csv dicts. Returns (results, conflicts)."""
    cand, index = [], defaultdict(list)
    for i, r in enumerate(cand_rows):
        toks = _tokens(r.get("title"))
        cand.append({"video_id": r.get("video_id"), "title": r.get("title"),
                     "views": r.get("view_count", ""), "dur": r.get("duration_sec", ""),
                     "set": set(toks)})
        for t in set(toks):
            index[t].append(i)
    results = []
    for rec in records:
        tset = set(_tokens(rec["to"]))
        mybig = _bignums(tset)
        pool = Counter()
        for t in tset:
            for i in index.get(t, []):
                pool[i] += 1
        scored = []
        for i in pool:
            c = cand[i]
            inter = len(tset & c["set"])
            if inter == 0:
                continue
            dice = 2 * inter / (len(tset) + len(c["set"])) if (len(tset) + len(c["set"])) else 0
            cbig = _bignums(c["set"])
            num_conflict = bool(mybig and cbig and not (set(mybig) & set(cbig)))
            anchor_eq = bool(mybig) and mybig == cbig
            scored.append({"i": i, "dice": dice, "inter": inter,
                           "num_conflict": num_conflict, "anchor_eq": anchor_eq})
        scored.sort(key=lambda x: -x["dice"])
        best = scored[0] if scored else None
        second = scored[1] if len(scored) > 1 else None
        tier, chosen, rule = "unmatched", None, ""
        if best and best["inter"] >= 3 and not best["num_conflict"] and len(cand[best["i"]]["set"]) >= 3:
            margin = best["dice"] - (second["dice"] if second else 0)
            anchors = [s for s in scored if s["anchor_eq"] and s["inter"] >= 3 and s["dice"] >= 0.40]
            if best["dice"] >= 0.55 and margin >= 0.08:
                tier, chosen, rule = "matched", best, "dice"
            elif best["dice"] >= 0.54 and margin >= 0.20:
                tier, chosen, rule = "matched", best, "dice-margin"
            elif len(anchors) == 1 and anchors[0] is best:
                tier, chosen, rule = "matched", best, "anchor"
            elif len(anchors) == 1:
                a = anchors[0]
                if a["dice"] >= 0.40:
                    tier, chosen, rule = "matched", a, "anchor-override"
        if tier == "unmatched" and best and best["dice"] >= 0.34:
            tier = "suspect"

        def fmt(s):
            return {"video_id": cand[s["i"]]["video_id"], "title": cand[s["i"]]["title"],
                    "dice": round(s["dice"], 3), "anchor_eq": s["anchor_eq"]} if s else None

        results.append({"bv": rec["bv"], "to": rec["to"], "tier": tier, "rule": rule,
                        "best": fmt(chosen) if chosen else fmt(best),
                        "top": [fmt(s) for s in scored[:3]]})
    chosen_ids = Counter(r["best"]["video_id"] for r in results
                         if r["tier"] == "matched" and r["best"])
    conflicts = {k: v for k, v in chosen_ids.items() if v > 1}
    for r in results:
        if r["tier"] == "matched" and r["best"] and r["best"]["video_id"] in conflicts:
            r["tier"] = "suspect"
            r["conflict"] = True
    for r in results:
        if r["tier"] == "matched" and r["bv"] in DEMOTE:
            r["tier"] = "suspect"
            r["demoted"] = DEMOTE[r["bv"]]
    return results, conflicts


# ---------------------------------------------------------------- db

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY,
  seq INTEGER NOT NULL,
  source_bv TEXT NOT NULL UNIQUE,
  video_id TEXT,
  title_zh TEXT NOT NULL,
  title_original TEXT,
  source_file TEXT,
  published_at TEXT,
  source TEXT,
  source_date TEXT,
  source_index TEXT,
  declared_words TEXT,
  transcript_status TEXT,
  keywords TEXT,
  md_body TEXT NOT NULL,
  txt_text TEXT,
  has_txt INTEGER NOT NULL DEFAULT 0,
  match_tier TEXT NOT NULL DEFAULT 'unmatched',
  yt_title TEXT,
  yt_view_count TEXT,
  yt_duration_sec TEXT
);
CREATE TABLE IF NOT EXISTS suspects (
  source_bv TEXT NOT NULL,
  cand_video_id TEXT,
  cand_title TEXT,
  dice REAL,
  PRIMARY KEY (source_bv, cand_video_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
  title_zh, title_original, keywords, md_body, txt_text,
  content='records', content_rowid='id', tokenize='trigram');
"""


def _s(v):
    if v is None:
        return None
    return str(v)


def run_import():
    files = sorted(f for f in os.listdir(SEED_DIR)
                   if re.fullmatch(r"BV[0-9A-Za-z]+\.md", f))
    recs = []
    for f in files:
        bv = f[:-3]
        with open(os.path.join(SEED_DIR, f), encoding="utf-8") as fh:
            text = fh.read()
        fm, body = parse_front_matter(text)
        sf = fm.get("source_file") or (bv + ".txt")
        txt = None
        for cand_name in (sf, bv + ".txt"):
            p = os.path.join(SEED_DIR, cand_name)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    txt = fh.read()
                break
        recs.append({"bv": bv, "fm": fm, "body": body, "txt": txt,
                     "keywords": extract_keywords(body)})
    rows = []
    csv_path = os.path.join(SEED_DIR, "match_source.csv")
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    results, conflicts = match_records(
        [{"bv": r["bv"], "to": r["fm"].get("title_original") or ""} for r in recs], rows)
    by_bv = {r["bv"]: r for r in results}
    csv_by_id = {r["video_id"]: r for r in rows}

    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    matched = suspect = unmatched = 0
    matched_list, suspect_list, unmatched_list, demoted_list = [], [], [], []
    with db:
        db.execute("DELETE FROM suspects")
        for i, rec in enumerate(sorted(recs, key=lambda x: x["bv"])):
            seq = i + 1
            res = by_bv[rec["bv"]]
            tier = res["tier"]
            fm = rec["fm"]
            video_id = yt_title = yt_views = yt_dur = None
            if tier == "matched":
                matched += 1
                video_id = res["best"]["video_id"]
                crow = csv_by_id.get(video_id, {})
                yt_title = crow.get("title")
                yt_views = crow.get("view_count") or None
                yt_dur = crow.get("duration_sec") or None
                matched_list.append({"bv": rec["bv"], "seq": seq,
                                     "title_zh": fm.get("title_zh"),
                                     "video_id": video_id, "yt_title": yt_title,
                                     "views": yt_views, "dice": res["best"]["dice"],
                                     "rule": res["rule"]})
            elif tier == "suspect":
                suspect += 1
                suspect_list.append({"bv": rec["bv"], "seq": seq,
                                     "title_zh": fm.get("title_zh"),
                                     "title_original": fm.get("title_original"),
                                     "candidates": res["top"],
                                     "note": res.get("demoted") or ("双 BV 争用同一视频, 已互降" if res.get("conflict") else "")})
                for c in res["top"]:
                    if c:
                        db.execute("INSERT OR REPLACE INTO suspects VALUES (?,?,?,?)",
                                   (rec["bv"], c["video_id"], c["title"], c["dice"]))
            else:
                unmatched += 1
                unmatched_list.append({"bv": rec["bv"], "seq": seq,
                                       "title_zh": fm.get("title_zh")})
            if res.get("demoted"):
                demoted_list.append({"bv": rec["bv"], "reason": res["demoted"]})
            db.execute(
                """INSERT INTO records (seq, source_bv, video_id, title_zh,
                   title_original, source_file, published_at, source,
                   source_date, source_index, declared_words,
                   transcript_status, keywords, md_body, txt_text, has_txt,
                   match_tier, yt_title, yt_view_count, yt_duration_sec)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_bv) DO UPDATE SET
                   seq=excluded.seq, video_id=excluded.video_id,
                   title_zh=excluded.title_zh,
                   title_original=excluded.title_original,
                   source_file=excluded.source_file,
                   published_at=excluded.published_at, source=excluded.source,
                   source_date=excluded.source_date,
                   source_index=excluded.source_index,
                   declared_words=excluded.declared_words,
                   transcript_status=excluded.transcript_status,
                   keywords=excluded.keywords, md_body=excluded.md_body,
                   txt_text=excluded.txt_text, has_txt=excluded.has_txt,
                   match_tier=excluded.match_tier, yt_title=excluded.yt_title,
                   yt_view_count=excluded.yt_view_count,
                   yt_duration_sec=excluded.yt_duration_sec""",
                (seq, rec["bv"], video_id, fm.get("title_zh") or rec["bv"],
                 _s(fm.get("title_original")), _s(fm.get("source_file")),
                 _s(fm.get("published_at")), _s(fm.get("source")),
                 _s(fm.get("source_date")), _s(fm.get("source_index")),
                 _s(fm.get("declared_words")), _s(fm.get("transcript_status")),
                 rec["keywords"], rec["body"], rec["txt"],
                 1 if rec["txt"] is not None else 0, tier,
                 yt_title, yt_views, yt_dur))
        db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")

    bv_set = {r["bv"] for r in recs}
    orphans = []
    for f in sorted(os.listdir(SEED_DIR)):
        if re.fullmatch(r"BV[0-9A-Za-z]+\.(md|txt)", f) or f == "match_source.csv":
            continue
        p = os.path.join(SEED_DIR, f)
        with open(p, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        lines = [l for l in content.split("\n") if l.strip()]
        if f == "bilibili_bv.txt":
            ids = {l.split("\t")[0] for l in lines}
            desc = ("BV 号与中文题的来源清单 (下载索引): 共 %d 行, BV 集合与语料 %s; "
                    "非视频原文, 未建记录" % (len(lines), "一致" if ids == bv_set else "不一致"))
        elif f == "downloaded.log":
            desc = ("来源下载日志 (BV+中文题+时间戳): 共 %d 条; 仅作报备材料, "
                    "未用于回填任何字段" % len(lines))
        else:
            desc = "未识别的附带文件, 未建记录"
        orphans.append({"file": f, "desc": desc})

    import time
    report = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(recs),
        "paired": sum(1 for r in recs if r["txt"] is not None),
        "md_only": [r["bv"] for r in recs if r["txt"] is None],
        "matched": matched, "suspect": suspect, "unmatched": unmatched,
        "matched_rate": round(matched / len(recs) * 100, 1) if recs else 0,
        "conflicts": conflicts,
        "demoted": demoted_list,
        "orphans": orphans,
        "matched_list": matched_list,
        "suspect_list": suspect_list,
        "unmatched_list": unmatched_list,
        "notes": [
            "video_id 原始数据全为 null (%d/%d); 确定匹配 %d 条已按下载清单回填" % (len(recs), len(recs), matched),
            "下载清单 (still_to_download.csv, 706 行) 不含 published_at 字段, 故发布时间未回填; 回填的清单字段仅 view_count / duration_sec",
            "疑似匹配不落库, 在本报告并排列出候选供裁决; 未匹配 video_id 留空",
            "BV 号为来源标识 (source_bv / source_file), 不作主键; 序号按 BV 排序稳定分配",
        ],
    }
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('import_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    return report


_init_lock = threading.Lock()
_inited = False


def ensure_init():
    global _inited
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        db = sqlite3.connect(DB_PATH)
        db.executescript(SCHEMA)
        n = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        db.close()
        if n == 0:
            run_import()
        _inited = True


@app.before_request
def _before():
    ensure_init()
    g.base = request.headers.get("X-Forwarded-Prefix") or ""


def U(path):
    return (g.base or "") + path

# ---------------------------------------------------------------- search

def _fts_ids(db, term):
    t = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff ]", " ", term).strip()
    if len(t) < 3:
        return set()
    q = '"' + t + '"'
    try:
        return {r[0] for r in db.execute(
            "SELECT rowid FROM records_fts WHERE records_fts MATCH ?", (q,))}
    except sqlite3.Error:
        return set()


def _like_ids(db, term, fields):
    pat = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    cols = " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in fields)
    return {r[0] for r in db.execute(
        f"SELECT id FROM records WHERE {cols}", [pat] * len(fields))}


def search_records(db, q):
    q = q.strip()
    terms = [t for t in re.split(r"\s+", q) if t]
    if not terms:
        return []
    # Phrase priority: a pasted quote / multi-char phrase that hits
    # verbatim wins outright over the bag-of-words AND below.
    cand = set()
    if len(q) >= 3 and len(terms) > 1:
        cand = _fts_ids(db, q)
        cand |= _like_ids(db, q, ["md_body", "txt_text"])
    if not cand:
        cand = None
    per_term = []
    for t in terms:
        ids = _fts_ids(db, t)
        ids |= _like_ids(db, t, ["title_zh", "title_original", "keywords"])
        if len(t) < 3:
            ids |= _like_ids(db, t, ["md_body", "txt_text"])
        per_term.append(ids)
    if cand is None:
        cand = set.intersection(*per_term) if per_term else set()
    if not cand:
        return []
    rows = db.execute(
        "SELECT * FROM records WHERE id IN (%s)" % ",".join("?" * len(cand)),
        list(cand)).fetchall()
    out = []
    for r in rows:
        best_rank, best_label = 3, "原文"
        for t in terms:
            if t in (r["title_zh"] or "") or t in (r["title_original"] or ""):
                best_rank, best_label = 0, "标题"
                break
            if t in (r["keywords"] or "") and best_rank > 1:
                best_rank, best_label = 1, "关键词"
            elif t in (r["md_body"] or "") and best_rank > 2:
                best_rank, best_label = 2, "摘要"
        out.append((best_rank, r["seq"], best_label, r))
    out.sort(key=lambda x: (x[0], x[1]))
    return [(label, r) for _, _, label, r in out]


# ---------------------------------------------------------------- pages

CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #f6f6f4; color: #1c1c1e; }
.wrap { max-width: 880px; margin: 0 auto; padding: 20px 16px 48px; }
h1.pt { font-size: 22px; margin: 4px 0 2px; }
.stats { color: #6b6b70; font-size: 13px; margin-bottom: 14px; }
.stats a { color: #0a64d8; text-decoration: none; }
form.search { display: flex; gap: 8px; margin: 12px 0 16px; }
form.search input { flex: 1; padding: 9px 12px; font-size: 15px; border: 1px solid #c9c9cd;
  border-radius: 8px; }
form.search button { padding: 9px 18px; font-size: 15px; border: 0; border-radius: 8px;
  background: #1c1c1e; color: #fff; cursor: pointer; }
.row { display: flex; gap: 12px; background: #fff; border: 1px solid #e4e4e1;
  border-radius: 10px; padding: 12px 14px; margin-bottom: 8px; align-items: flex-start; }
.row .seq { font-variant-numeric: tabular-nums; color: #8a8a8e; font-size: 13px;
  padding-top: 2px; min-width: 40px; }
.row .main { flex: 1; min-width: 0; }
.row a.t { color: #1c1c1e; text-decoration: none; font-size: 16px; font-weight: 600; }
.row a.t:hover { color: #0a64d8; }
.row .sub { color: #6b6b70; font-size: 13px; margin-top: 2px; word-break: break-word; }
.row .kw { color: #8a8a8e; font-size: 12px; margin-top: 4px; }
.badges { display: flex; flex-direction: column; gap: 4px; align-items: flex-end; }
.b { font-size: 11px; padding: 2px 8px; border-radius: 99px; white-space: nowrap; }
.b-txt { background: #e5f4e3; color: #1d6b1d; }
.b-md { background: #fdf0d9; color: #8a5a00; }
.b-yt { background: #e2ecfd; color: #0a4fb0; }
.b-hit { background: #f3e8fd; color: #6a1fb0; }
.pager { display: flex; gap: 14px; align-items: center; margin: 16px 0; font-size: 14px; }
.pager a { color: #0a64d8; text-decoration: none; }
.pager .off { color: #b9b9bd; }
.empty { background: #fff; border: 1px dashed #c9c9cd; border-radius: 10px;
  padding: 28px; text-align: center; color: #6b6b70; }
article { background: #fff; border: 1px solid #e4e4e1; border-radius: 10px;
  padding: 20px 22px; }
article h1 { font-size: 21px; margin: 0 0 4px; }
article h2 { font-size: 17px; margin: 22px 0 6px; padding-top: 12px;
  border-top: 1px solid #eee; }
article h3 { font-size: 15px; margin: 16px 0 4px; }
article p, article li { font-size: 14.5px; line-height: 1.75; }
article ul, article ol { padding-left: 22px; margin: 6px 0; }
.orig { color: #6b6b70; font-size: 14px; margin-bottom: 10px; word-break: break-word; }
dl.fm { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 6px 18px; background: #fafaf8; border: 1px solid #eee; border-radius: 8px;
  padding: 12px 14px; margin: 12px 0 4px; }
dl.fm div { font-size: 13px; }
dl.fm dt { color: #8a8a8e; display: inline; }
dl.fm dd { display: inline; margin: 0; font-weight: 600; word-break: break-all; }
.note { font-size: 12.5px; color: #8a5a00; margin: 6px 0 0; }
details.txt { margin-top: 18px; background: #fff; border: 1px solid #e4e4e1;
  border-radius: 10px; padding: 12px 16px; }
details.txt summary { cursor: pointer; font-weight: 600; font-size: 14.5px; }
details.txt pre { white-space: pre-wrap; word-break: break-word; font-size: 13px;
  line-height: 1.7; max-height: 60vh; overflow: auto; }
.back { font-size: 14px; margin-bottom: 12px; }
.back a { color: #0a64d8; text-decoration: none; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border-bottom: 1px solid #eee; padding: 6px 8px; text-align: left;
  vertical-align: top; }
th { color: #6b6b70; font-weight: 600; }
.scroll { max-height: 46vh; overflow: auto; border: 1px solid #e4e4e1;
  border-radius: 8px; background: #fff; margin: 8px 0 18px; }
section.rep h2 { font-size: 17px; margin: 20px 0 4px; }
.cand { color: #444; }
.mono { font-variant-numeric: tabular-nums; }
button.reimp { padding: 8px 16px; border: 0; border-radius: 8px; background: #1c1c1e;
  color: #fff; cursor: pointer; font-size: 14px; }
"""


def page(title, body):
    return ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{_html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><div class=\"wrap\">{body}</div></body></html>")


def _badges(r, hit_label=None):
    b = []
    if hit_label:
        b.append(f"<span class=\"b b-hit\">命中·{_html.escape(hit_label)}</span>")
    b.append("<span class=\"b b-txt\">有原文</span>" if r["has_txt"]
             else "<span class=\"b b-md\">仅摘要</span>")
    if r["video_id"]:
        b.append(f"<span class=\"b b-yt\" title=\"{_html.escape(r['video_id'])}\">YouTube 已匹配</span>")
    return "<div class=\"badges\">" + "".join(b) + "</div>"


def _row(r, hit_label=None):
    return (
        "<div class=\"row\">"
        f"<a class=\"seq\" href=\"{U('/r/%d' % r['seq'])}\">#{r['seq']:03d}</a>"
        "<div class=\"main\">"
        f"<a class=\"t\" href=\"{U('/r/%d' % r['seq'])}\">{_html.escape(r['title_zh'])}</a>"
        f"<div class=\"sub\">{_html.escape(r['title_original'] or '')}</div>"
        f"<div class=\"kw\">{_html.escape(r['keywords'] or '')}</div>"
        "</div>" + _badges(r, hit_label) + "</div>")


PER_PAGE = 30


@app.route("/")
def index():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    total = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    n_matched = db.execute(
        "SELECT COUNT(*) FROM records WHERE video_id IS NOT NULL").fetchone()[0]
    n_txt = db.execute("SELECT COUNT(*) FROM records WHERE has_txt=1").fetchone()[0]
    head = ("<h1 class=\"pt\">野兽素材库</h1>"
            f"<div class=\"stats\">共 {total} 条 · 有原文 {n_txt} 条 · "
            f"YouTube 已匹配 {n_matched} 条 · "
            f"<a href=\"{U('/report')}\">入库报告</a></div>"
            f"<form class=\"search\" method=\"get\" action=\"{U('/')}\">"
            f"<input name=\"q\" value=\"{_html.escape(q)}\" "
            "placeholder=\"搜索中文名 / 原名 / 关键词 / 正文 / 原文\">"
            "<button type=\"submit\">搜索</button></form>")
    if q:
        hits = search_records(db, q)
        body = head + f"<div class=\"stats\">搜索「{_html.escape(q)}」命中 {len(hits)} 条 · <a href=\"{U('/')}\">清除</a></div>"
        if hits:
            body += "".join(_row(r, label) for label, r in hits)
        else:
            body += "<div class=\"empty\">没有命中。</div>"
        return page("搜索 · 野兽素材库", body)
    try:
        p = max(1, int(request.args.get("p", "1")))
    except ValueError:
        p = 1
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    p = min(p, pages)
    rows = db.execute("SELECT * FROM records ORDER BY seq LIMIT ? OFFSET ?",
                      (PER_PAGE, (p - 1) * PER_PAGE)).fetchall()
    body = head + "".join(_row(r) for r in rows)
    prev = (f"<a href=\"{U('/?p=%d' % (p - 1))}\">上一页</a>" if p > 1
            else "<span class=\"off\">上一页</span>")
    nxt = (f"<a href=\"{U('/?p=%d' % (p + 1))}\">下一页</a>" if p < pages
           else "<span class=\"off\">下一页</span>")
    body += (f"<div class=\"pager\">{prev}<span>第 {p} / {pages} 页</span>{nxt}</div>")
    return page("野兽素材库", body)


def _fmt_views(v):
    if v and str(v).isdigit():
        return f"{int(v):,}"
    return v or "—"


@app.route("/r/<int:seq>")
def detail(seq):
    db = get_db()
    r = db.execute("SELECT * FROM records WHERE seq=?", (seq,)).fetchone()
    if r is None:
        abort(404)
    p = (seq - 1) // PER_PAGE + 1
    fm_items = [
        ("序号", f"#{r['seq']:03d}"), ("来源 BV", r["source_bv"]),
        ("YouTube ID", r["video_id"]), ("发布时间", r["published_at"]),
        ("来源", r["source"]), ("来源日期", r["source_date"]),
        ("来源文件", r["source_file"]), ("来源序号", r["source_index"]),
        ("标注字数", r["declared_words"]), ("转写状态", r["transcript_status"]),
    ]
    if r["video_id"]:
        fm_items += [("清单标题", r["yt_title"]),
                     ("清单播放量", _fmt_views(r["yt_view_count"])),
                     ("清单时长(秒)", r["yt_duration_sec"])]
    fm = "<dl class=\"fm\">" + "".join(
        f"<div><dt>{k}：</dt><dd>{_html.escape(str(v)) if v else '—'}</dd></div>"
        for k, v in fm_items) + "</dl>"
    note = ("<p class=\"note\">YouTube 字段由用户下载清单按英文标题匹配回填（确定匹配）；"
            "front-matter 原值未改。</p>" if r["video_id"] else "")
    body = (f"<div class=\"back\"><a href=\"{U('/?p=%d' % p)}\">← 返回列表</a></div>"
            "<article>"
            f"<h1>{_html.escape(r['title_zh'])}</h1>"
            f"<div class=\"orig\">{_html.escape(r['title_original'] or '')}</div>"
            + fm + note + render_md(r["md_body"]) + "</article>")
    if r["has_txt"] and r["txt_text"] is not None:
        body += (f"<details class=\"txt\"><summary>配对原文（{_html.escape(r['source_file'] or '')}）</summary>"
                 f"<pre>{_html.escape(r['txt_text'])}</pre></details>")
    else:
        body += "<details class=\"txt\"><summary>配对原文</summary><p>无配对原文。</p></details>"
    return page(r["title_zh"] + " · 野兽素材库", body)


@app.route("/report")
def report():
    db = get_db()
    row = db.execute("SELECT value FROM meta WHERE key='import_report'").fetchone()
    if row is None:
        return page("入库报告 · 野兽素材库", "<p>尚未导入。</p>")
    rep = json.loads(row["value"])
    h = ["<div class=\"back\"><a href=\"%s\">← 返回列表</a></div>" % U("/"),
         "<section class=\"rep\">",
         "<h1 class=\"pt\">入库报告</h1>",
         f"<div class=\"stats\">导入时间 {rep['at']} · 共 {rep['total']} 条 · "
         f"配对原文 {rep['paired']} 条 · 仅摘要 {len(rep['md_only'])} 条</div>",
         "<h2>YouTube 标题匹配（来源：用户下载清单）</h2>",
         f"<div class=\"stats\">确定匹配 {rep['matched']} 条（{rep['matched_rate']}%）· "
         f"疑似 {rep['suspect']} 条（不落库，待裁决）· 未匹配 {rep['unmatched']} 条（video_id 留空）</div>"]
    h.append("<div class=\"scroll\"><table><tr><th>序号</th><th>BV</th><th>中文名</th>"
             "<th>YouTube ID</th><th>清单标题</th><th>播放量</th><th>dice</th><th>通道</th></tr>")
    for m in rep["matched_list"]:
        h.append("<tr><td class=\"mono\">#%03d</td><td>%s</td><td>%s</td><td>%s</td>"
                 "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                     m["seq"], _html.escape(m["bv"]),
                     _html.escape(m["title_zh"] or ""),
                     _html.escape(m["video_id"] or ""),
                     _html.escape(m["yt_title"] or ""),
                     _html.escape(_fmt_views(m["views"])),
                     m["dice"], _html.escape(m["rule"])))
    h.append("</table></div>")
    h.append("<h2>疑似匹配（并排列出，供裁决）</h2><div class=\"scroll\">")
    for s in rep["suspect_list"]:
        h.append(f"<p><strong>#{s['seq']:03d} {_html.escape(s['bv'])}</strong> "
                 f"{_html.escape(s['title_zh'] or '')}<br>"
                 f"<span class=\"cand\">md 原名：{_html.escape(s['title_original'] or '')}</span>")
        if s.get("note"):
            h.append(f"<br><span class=\"note\">{_html.escape(s['note'])}</span>")
        for c in s["candidates"]:
            if c:
                h.append(f"<br><span class=\"cand\">候选：{_html.escape(c['title'])} "
                         f"[{_html.escape(c['video_id'])}] · dice {c['dice']}</span>")
        h.append("</p>")
    h.append("</div>")
    if rep["demoted"]:
        h.append("<h2>人工复核降级（只降不升）</h2>")
        for d in rep["demoted"]:
            h.append(f"<p>{_html.escape(d['bv'])}：{_html.escape(d['reason'])}</p>")
    h.append("<h2>未匹配</h2><div class=\"scroll\"><p>")
    h.append(" · ".join(f"#{u['seq']:03d} {_html.escape(u['title_zh'] or u['bv'])}"
                         for u in rep["unmatched_list"]))
    h.append("</p></div>")
    h.append("<h2>附带文件（孤儿结论）</h2>")
    for o in rep["orphans"]:
        h.append(f"<p><strong>{_html.escape(o['file'])}</strong>：{_html.escape(o['desc'])}</p>")
    h.append("<h2>口径说明</h2>")
    for n in rep["notes"]:
        h.append(f"<p>{_html.escape(n)}</p>")
    h.append(f"<form method=\"post\" action=\"{U('/import')}\">"
             "<button class=\"reimp\" type=\"submit\">重新导入（幂等）</button></form>")
    h.append("</section>")
    return page("入库报告 · 野兽素材库", "".join(h))


@app.route("/import", methods=["POST"])
def do_import():
    run_import()
    return redirect(U("/report"))


@app.route("/api/session")
def api_session():
    return {"authenticated": True}
