# -*- coding: utf-8 -*-
"""野兽素材库 (beastlib): MrBeast 文案材料库。

数据分层 (元数据先行):
  - 底座层 yt_base: 用户 YouTube 清单 706 个 video_id 的元数据 (原始标题/
    播放/点赞/评论/发布日期/时长), fetch_base.py 用 yt-dlp 实抓、
    ingest_base.py 入表; 统计展示一律以底座为准。
  - 文本层 records: 一条记录 = 一篇 BV 摘要 (md) + 配对原文 (txt), 是用户
    特意筛选先行下载的精选批 (与 706 补全集是两个批次, 不是从中切出的子集)。
身份与排序:
  - 序号: 按 BV 号排序确定性分配 (#001 起), 重导入不变; 底座记录无序号,
    以 video_id 为身份。
  - YouTube video_id 为规范主键字段: 本批 front-matter 全为 null, 只允许
    匹配回填 —— 先标题匹配 (对底座原始标题), 标题不够的升级内容核验
    (BV txt 与候选 ASR 转写的片段重合); 确定才落库, 疑似只进报告待裁决,
    未匹配留空, 只降不升、降级留理由。内容匹配 (match_method 为
    content / title+content) 强于标题层: 标题法重导入时保留不覆盖。
    BV 号只是来源标识, 不冒充主键;
  - title_zh (中文名) 是标准名: 列表/详情/搜索都以它为准。
无项目内验证 (面板 /web 门负责)。数据落平台数据目录 (DATA_DIR 解析)。
"""
import csv
import hashlib
import html as _html
import time
import json
import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict

from flask import Flask, abort, g, redirect, request
from urllib.parse import urlencode

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
  source_bv TEXT UNIQUE,
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
  yt_duration_sec TEXT,
  match_method TEXT,
  content_cov REAL,
  content_shared INTEGER,
  origin TEXT NOT NULL DEFAULT 'bv',
  yt_md_body TEXT,
  yt_srt_text TEXT,
  yt_doc_name TEXT,
  base_pending INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS suspects (
  source_bv TEXT NOT NULL,
  cand_video_id TEXT,
  cand_title TEXT,
  dice REAL,
  PRIMARY KEY (source_bv, cand_video_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS yt_base (
  video_id TEXT PRIMARY KEY,
  title TEXT,
  view_count INTEGER,
  like_count INTEGER,
  comment_count INTEGER,
  upload_date TEXT,
  duration INTEGER,
  fetched_at TEXT,
  source TEXT,
  error TEXT,
  transcript_ready INTEGER NOT NULL DEFAULT 0,
  tab TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
  title_zh, title_original, keywords, md_body, txt_text, yt_md_body, yt_srt_text,
  content='records', content_rowid='id', tokenize='trigram');
"""

RECORDS_V2_DEF = """
CREATE TABLE records_v2 (
  id INTEGER PRIMARY KEY,
  seq INTEGER NOT NULL,
  source_bv TEXT UNIQUE,
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
  yt_duration_sec TEXT,
  match_method TEXT,
  content_cov REAL,
  content_shared INTEGER,
  origin TEXT NOT NULL DEFAULT 'bv',
  yt_md_body TEXT,
  yt_srt_text TEXT,
  yt_doc_name TEXT,
  base_pending INTEGER NOT NULL DEFAULT 0
);
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
    _migrate(db)
    # 内容层结论强于标题层: 已有内容匹配的 BV 在标题法重导入时保留原结论
    preserved = {}
    for bv0, vid0, mm0, ct0, cv0, cs0, t0, v0, d0 in db.execute(
            "SELECT source_bv, video_id, match_method, yt_title, yt_view_count,"
            " yt_duration_sec, title_zh FROM records"
            " WHERE video_id IS NOT NULL AND match_method IN ('content','title+content','title_v2')"):
        preserved[bv0] = {"video_id": vid0, "match_method": mm0,
                          "yt_title": ct0, "views": cv0, "dur": cs0,
                          "title_zh": t0}
    matched = suspect = unmatched = 0
    matched_list, suspect_list, unmatched_list, demoted_list = [], [], [], []
    with db:
        db.execute("DELETE FROM suspects")
        for i, rec in enumerate(sorted(recs, key=lambda x: x["bv"])):
            seq = i + 1
            res = by_bv[rec["bv"]]
            tier = res["tier"]
            fm = rec["fm"]
            pres = preserved.get(rec["bv"])
            if pres:
                tier = "matched"
            video_id = yt_title = yt_views = yt_dur = None
            if tier == "matched":
                matched += 1
                if pres:
                    video_id = pres["video_id"]
                    yt_title = pres["yt_title"]
                    yt_views = pres["views"]
                    yt_dur = pres["dur"]
                    matched_list.append({"bv": rec["bv"], "seq": seq,
                                         "title_zh": fm.get("title_zh"),
                                         "video_id": video_id,
                                         "yt_title": yt_title,
                                         "views": yt_views, "dice": None,
                                         "rule": pres["match_method"]})
                else:
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
        "title_source": "用户下载清单 (seed/match_source.csv)",
        "matched_rate": round(matched / len(recs) * 100, 1) if recs else 0,
        "conflicts": conflicts,
        "demoted": demoted_list,
        "orphans": orphans,
        "matched_list": matched_list,
        "suspect_list": suspect_list,
        "unmatched_list": unmatched_list,
        "notes": [
            "video_id 原始数据全为 null (%d/%d); 确定匹配 %d 条已回填" % (len(recs), len(recs), matched),
            "本报告为标题层结果 (来源见报告页标题层段落); 元数据底座 (yt-dlp 实抓) 的标题层由 apply_rematch 生成, 内容核验层由 apply_content 生成, 均在本页上部汇总",
            "下载清单 (still_to_download.csv, 706 行) 不含 published_at 字段; 发布时间/点赞/评论只来自底座实抓",
            "疑似匹配不落库, 在本报告并排列出候选供裁决; 未匹配 video_id 留空",
            "BV 号为来源标识 (source_bv / source_file), 不作主键; 序号按 BV 排序稳定分配; 内容匹配 (content/title+content) 在标题法重导入时保留不覆盖",
        ],
    }
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('import_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    return report


def _migrate(db):
    """v1: 匹配证据列; v2: records 重建 (source_bv 可空 + origin/第二套文档列
    + FTS 扩列纳入 YouTube 来源文档)。幂等, 按列存在性判断。"""
    cols = {r[1] for r in db.execute("PRAGMA table_info(records)")}
    if not cols:
        return  # fresh DB: SCHEMA 已是 v2
    for name, decl in (("match_method", "TEXT"), ("content_cov", "REAL"),
                       ("content_shared", "INTEGER")):
        if name not in cols:
            db.execute(f"ALTER TABLE records ADD COLUMN {name} {decl}")
            cols.add(name)
    if "origin" not in cols:
        keep = ["id", "seq", "source_bv", "video_id", "title_zh",
                "title_original", "source_file", "published_at", "source",
                "source_date", "source_index", "declared_words",
                "transcript_status", "keywords", "md_body", "txt_text",
                "has_txt", "match_tier", "yt_title", "yt_view_count",
                "yt_duration_sec", "match_method", "content_cov",
                "content_shared"]
        collist = ", ".join(keep)
        db.execute("DROP TABLE IF EXISTS records_fts")
        db.execute("DROP TABLE IF EXISTS records_v2")
        db.execute(RECORDS_V2_DEF)
        db.execute(f"INSERT INTO records_v2 ({collist}) "
                   f"SELECT {collist} FROM records")
        db.execute("DROP TABLE records")
        db.execute("ALTER TABLE records_v2 RENAME TO records")
        db.execute("""CREATE VIRTUAL TABLE records_fts USING fts5(
          title_zh, title_original, keywords, md_body, txt_text, yt_md_body,
          yt_srt_text, content='records', content_rowid='id',
          tokenize='trigram')""")
        db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
    db.execute("UPDATE records SET match_method='title' "
               "WHERE video_id IS NOT NULL AND match_method IS NULL")
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                  " AND name='yt_base'").fetchone():
        bcols = {r[1] for r in db.execute("PRAGMA table_info(yt_base)")}
        if "tab" not in bcols:
            db.execute("ALTER TABLE yt_base ADD COLUMN tab TEXT")
        if "excluded" not in bcols:
            db.execute("ALTER TABLE yt_base ADD COLUMN excluded"
                       " INTEGER NOT NULL DEFAULT 0")
    db.commit()


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
        _migrate(db)
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
.links { font-size: 12.5px; margin-top: 5px; color: #6b6b70; }
.links a { color: #0a64d8; text-decoration: none; }
.dl { display: flex; gap: 8px; margin: 10px 0 2px; flex-wrap: wrap; }
.dl a { font-size: 13px; padding: 5px 12px; border: 1px solid #c9c9cd; border-radius: 99px;
  color: #1c1c1e; text-decoration: none; background: #fff; }
.dl a:hover { border-color: #0a64d8; color: #0a64d8; }
.b-src { background: #eef0ff; color: #3a3fb0; }
.b-tr { background: #e8f6ee; color: #14662e; }
.b-notr { background: #f0f0ee; color: #6b6b70; }
.b-nomd { background: #f0f0ee; color: #6b6b70; }
.b-err { background: #fde8e8; color: #a11; }
.tgl { font-size: 13.5px; margin: 2px 0 12px; }
.tgl a { color: #0a64d8; text-decoration: none; }
.tgl .on { font-weight: 700; color: #1c1c1e; }
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
    keys = r.keys()
    origin = r["origin"] if "origin" in keys else "bv"
    if origin == "yt":
        b.append("<span class=\"b b-src\">YouTube 批</span>")
    elif "yt_md_body" in keys and r["yt_md_body"]:
        b.append("<span class=\"b b-src\">双来源</span>")
    else:
        b.append("<span class=\"b b-src\">BV 批</span>")
    return "<div class=\"badges\">" + "".join(b) + "</div>"


def _row(r, hit_label=None, views=None, pub_date=None, rank=None):
    links = ""
    if r["video_id"]:
        links += (f"<a href=\"https://www.youtube.com/watch?v={r['video_id']}\" "
                  "target=\"_blank\" rel=\"noopener\">YouTube ↗</a> · ")
    links += (f"<a href=\"https://www.bilibili.com/video/{r['source_bv']}\" "
              "target=\"_blank\" rel=\"noopener\">Bilibili ↗</a>")
    vh = _views_html(views, rank)
    if vh:
        links += " · " + vh
    if pub_date:
        links += f" · <span>发布 {pub_date}</span>"
    return (
        "<div class=\"row\">"
        f"<a class=\"seq\" href=\"{U('/r/%d' % r['seq'])}\">#{r['seq']:03d}</a>"
        "<div class=\"main\">"
        f"<a class=\"t\" href=\"{U('/r/%d' % r['seq'])}\">{_html.escape(r['title_zh'])}</a>"
        f"<div class=\"sub\">{_html.escape(r['title_original'] or '')}</div>"
        f"<div class=\"kw\">{_html.escape(r['keywords'] or '')}</div>"
        f"<div class=\"links\">{links}</div>"
        "</div>" + _badges(r, hit_label) + "</div>")


PER_PAGE = 30


_REC_SORTS = ("seq", "views", "text", "random")


def _sort_args(valid, default_sort, asc_by_default):
    sort = request.args.get("sort") or default_sort
    if sort not in valid:
        sort = default_sort
    d = request.args.get("dir") or ""
    if d not in ("asc", "desc"):
        d = "asc" if sort in asc_by_default else "desc"
    seed = (request.args.get("seed") or "").strip()
    return sort, d, seed


def _qs(**kw):
    pairs = [(k, v) for k, v in kw.items() if v not in (None, "")]
    return ("?" + urlencode(pairs)) if pairs else ""


def _rand_key(seed, ident):
    return hashlib.md5(("%s:%s" % (seed, ident)).encode("utf-8")).hexdigest()


def _sortbar(view, sort, d, seed, q=""):
    if view == "rec":
        opts = [("seq", "序号", "asc"), ("views", "播放量", "desc"),
                ("text", "文本长度", "desc"), ("random", "随机", "")]
        vkw = {}
    else:
        opts = [("views", "播放量", "desc"), ("date", "发布时间", "desc"),
                ("random", "随机", "")]
        vkw = {"view": "base"}
    items = []
    if sort is None:
        items.append("<span class=\"on\">相关度</span>")
    for key, label, dd in opts:
        if key == sort:
            items.append(f"<span class=\"on\">{label}</span>")
        else:
            kw = dict(vkw)
            if q:
                kw["q"] = q
            kw["sort"] = key
            if dd:
                kw["dir"] = dd
            if key == "random":
                kw["seed"] = str(int(time.time()))
            items.append(f"<a href=\"{U('/' + _qs(**kw))}\">{label}</a>")
    if sort == "random":
        kw = dict(vkw)
        if q:
            kw["q"] = q
        kw.update(sort="random", seed=str(int(time.time())))
        items.append(f"<a href=\"{U('/' + _qs(**kw))}\">换一批</a>")
    elif sort:
        nd = "asc" if d == "desc" else "desc"
        kw = dict(vkw)
        if q:
            kw["q"] = q
        kw.update(sort=sort, dir=nd)
        cur = "降序" if d == "desc" else "升序"
        oth = "升序" if nd == "asc" else "降序"
        items.append(f"<span class=\"on\">{cur}</span> "
                     f"<a href=\"{U('/' + _qs(**kw))}\">{oth}</a>")
    return "<div class=\"tgl\">排序：" + " · ".join(items) + "</div>"


def _sort_hits(hits, sort, d, seed, bmap):
    deco = []
    for label, r in hits:
        v = _num(_disp_views(r, bmap))
        tl = len(r["txt_text"] or "") + len(r["yt_srt_text"] or "")
        deco.append((label, r, v, tl))
    if sort == "views":
        if d == "desc":
            deco.sort(key=lambda x: (x[2] is not None, x[2] or 0),
                      reverse=True)
        else:
            deco.sort(key=lambda x: (x[2] is None, x[2] or 0))
    elif sort == "text":
        deco.sort(key=lambda x: x[3], reverse=(d == "desc"))
    elif sort == "random":
        deco.sort(key=lambda x: _rand_key(seed, x[1]["id"]))
    else:
        deco.sort(key=lambda x: x[1]["seq"], reverse=(d == "desc"))
    return [(label, r) for label, r, _, _ in deco]


@app.route("/")
def index():
    db = get_db()
    if (request.args.get("view") or "") == "base":
        return _base_page(db)
    q = (request.args.get("q") or "").strip()
    sort, d, seed = _sort_args(_REC_SORTS, "seq", {"seq"})
    explicit_sort = "sort" in request.args
    total = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    n_matched = db.execute(
        "SELECT COUNT(*) FROM records WHERE video_id IS NOT NULL").fetchone()[0]
    n_txt = db.execute("SELECT COUNT(*) FROM records WHERE has_txt=1").fetchone()[0]
    head = ("<h1 class=\"pt\">野兽素材库</h1>"
            f"<div class=\"stats\">共 {total} 条 · 有原文 {n_txt} 条 · "
            f"YouTube 已匹配 {n_matched} 条 · "
            f"<a href=\"{U('/report')}\">入库报告</a> · "
            f"<a href=\"{U('/?view=base')}\">全部底座</a></div>"
            f"<form class=\"search\" method=\"get\" action=\"{U('/')}\">"
            f"<input name=\"q\" value=\"{_html.escape(q)}\" "
            "placeholder=\"搜索中文名 / 原名 / 关键词 / 正文 / 原文\">"
            "<button type=\"submit\">搜索</button></form>")
    ranks = _view_ranks(db)
    if q:
        hits = search_records(db, q)
        body = head + f"<div class=\"stats\">搜索「{_html.escape(q)}」命中 {len(hits)} 条 · <a href=\"{U('/')}\">清除</a></div>"
        if hits:
            bmap = _base_map(db, [r["video_id"] for _, r in hits])
            if explicit_sort:
                hits = _sort_hits(hits, sort, d, seed, bmap)
            body += _sortbar("rec", sort if explicit_sort else None, d, seed, q)
            body += "".join(
                _row(r, label, _disp_views(r, bmap), _disp_date(r, bmap),
                     ranks.get(r["video_id"]))
                for label, r in hits)
        else:
            body += "<div class=\"empty\">没有命中。</div>"
        return page("搜索 · 野兽素材库", body)
    try:
        p = max(1, int(request.args.get("p", "1")))
    except ValueError:
        p = 1
    light = db.execute(
        "SELECT id, seq, video_id, yt_view_count, "
        "LENGTH(COALESCE(txt_text, '')) + LENGTH(COALESCE(yt_srt_text, '')) AS tl "
        "FROM records").fetchall()
    bmap_all = _base_map(db, [r["video_id"] for r in light])

    def _lv(lr):
        b = bmap_all.get(lr["video_id"]) if lr["video_id"] else None
        if b is not None and not b["error"] and b["view_count"] is not None:
            return b["view_count"]
        return _num(lr["yt_view_count"])

    items = [(lr["id"], lr["seq"], _lv(lr), lr["tl"]) for lr in light]
    if sort == "views":
        if d == "desc":
            items.sort(key=lambda x: (x[2] is not None, x[2] or 0),
                       reverse=True)
        else:
            items.sort(key=lambda x: (x[2] is None, x[2] or 0))
    elif sort == "text":
        items.sort(key=lambda x: x[3], reverse=(d == "desc"))
    elif sort == "random":
        items.sort(key=lambda x: _rand_key(seed, x[0]))
    else:
        items.sort(key=lambda x: x[1], reverse=(d == "desc"))
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    p = min(p, pages)
    page_ids = [x[0] for x in items[(p - 1) * PER_PAGE: p * PER_PAGE]]
    rows = []
    if page_ids:
        got = db.execute(
            "SELECT * FROM records WHERE id IN (%s)"
            % ",".join("?" * len(page_ids)), page_ids).fetchall()
        by_id = {r["id"]: r for r in got}
        rows = [by_id[i] for i in page_ids]
    bmap = _base_map(db, [r["video_id"] for r in rows])
    body = head + _sortbar("rec", sort, d, seed)
    body += "".join(
        _row(r, views=_disp_views(r, bmap), pub_date=_disp_date(r, bmap),
             rank=ranks.get(r["video_id"]))
        for r in rows)

    def pq(n):
        return U("/" + _qs(p=n, sort=sort, dir=d,
                           seed=seed if sort == "random" else None))

    prev = (f"<a href=\"{pq(p - 1)}\">上一页</a>" if p > 1
            else "<span class=\"off\">上一页</span>")
    nxt = (f"<a href=\"{pq(p + 1)}\">下一页</a>" if p < pages
           else "<span class=\"off\">下一页</span>")
    body += (f"<div class=\"pager\">{prev}<span>第 {p} / {pages} 页</span>{nxt}</div>")
    return page("野兽素材库", body)

def _brow(b, rank=None):
    badges = []
    if b["error"]:
        badges.append("<span class=\"b b-err\">抓取失败</span>")
    if "excluded" in b.keys() and b["excluded"]:
        badges.append("<span class=\"b b-err\">已排除（超长）</span>")
    if b["rseq"] is not None:
        badges.append(f"<span class=\"b b-yt\">有摘要 #{b['rseq']:03d}</span>")
    else:
        badges.append("<span class=\"b b-nomd\">无摘要</span>")
    badges.append("<span class=\"b b-tr\">转写就绪</span>" if b["transcript_ready"]
                  else "<span class=\"b b-notr\">未转写</span>")
    if "tab" in b.keys() and b["tab"] == "shorts":
        badges.append("<span class=\"b b-src\">Shorts</span>")
    title = b["title"] or b["video_id"]
    if b["rseq"] is not None:
        tlink = (f"<a class=\"t\" href=\"{U('/r/%d' % b['rseq'])}\">"
                 f"{_html.escape(title)}</a>")
    else:
        tlink = (f"<a class=\"t\" href=\"https://www.youtube.com/watch?v={b['video_id']}\" "
                 f"target=\"_blank\" rel=\"noopener\">{_html.escape(title)}</a>")
    links = (f"<a href=\"https://www.youtube.com/watch?v={b['video_id']}\" "
             "target=\"_blank\" rel=\"noopener\">YouTube ↗</a>")
    if b["rseq"] is not None:
        links += f" · <a href=\"{U('/r/%d' % b['rseq'])}\">看摘要 →</a>"
    vh = _views_html(b["view_count"], rank)
    if vh:
        links += " · " + vh
    sub = (f"{b['video_id']} · 发布 {_fmt_date(b['upload_date'])} · "
           f"时长 {_fmt_dur(b['duration'])}")
    if b["error"]:
        sub += f" · {_html.escape(b['error'])}"
    return ("<div class=\"row\"><div class=\"seq\">底座</div><div class=\"main\">"
            + tlink + f"<div class=\"sub\">{_html.escape(sub)}</div>"
            f"<div class=\"links\">{links}</div></div>"
            "<div class=\"badges\">" + "".join(badges) + "</div></div>")


def _base_page(db):
    back = f"<div class=\"back\"><a href=\"{U('/')}\">← 素材记录</a></div>"
    if not _has_base(db):
        return page("元数据底座 · 野兽素材库",
                    back + "<div class=\"empty\">底座尚未入库。</div>")
    sort, d, seed = _sort_args(("views", "date", "random"), "views", set())
    bcols = {r[1] for r in db.execute("PRAGMA table_info(yt_base)")}
    has_ex = "excluded" in bcols
    exw = " AND excluded=0" if has_ex else ""
    total_all = db.execute("SELECT COUNT(*) FROM yt_base").fetchone()[0]
    excl_n = (db.execute(
        "SELECT COUNT(*) FROM yt_base WHERE excluded=1").fetchone()[0]
        if has_ex else 0)
    total = total_all - excl_n
    ok = db.execute(
        "SELECT COUNT(*) FROM yt_base WHERE error IS NULL"
        + exw).fetchone()[0]
    tr = db.execute(
        "SELECT COUNT(*) FROM yt_base WHERE transcript_ready=1"
        + exw).fetchone()[0]
    n_md = db.execute(
        "SELECT COUNT(DISTINCT video_id) FROM records "
        "WHERE video_id IS NOT NULL").fetchone()[0]
    head = (back + "<h1 class=\"pt\">元数据底座</h1>"
            f"<div class=\"stats\">共 {total} 条 · 抓取成功 {ok} · 失败 {total - ok} · "
            f"转写就绪 {tr} · 已挂摘要 {n_md} 条"
            + (f" · 已排除 {excl_n} 条超长视频（不计入底座，列表中标「已排除」）"
               if excl_n else "")
            + "</div>"
            f"<div class=\"tgl\"><a href=\"{U('/')}\">素材记录（摘要）</a> · "
            "<span class=\"on\">全部底座</span></div>"
            "<div class=\"stats\">底座为清单 706 个 video_id 的 yt-dlp 实抓元数据；"
            "有摘要的挂素材记录序号，无摘要的只有 video_id 身份"
            "（底座不用 # 序号）。名次为底座内部按播放量降序的排名。</div>")
    try:
        p = max(1, int(request.args.get("p", "1")))
    except ValueError:
        p = 1
    pages = max(1, (total_all + PER_PAGE - 1) // PER_PAGE)
    p = min(p, pages)
    ranks = _view_ranks(db)
    if sort == "random":
        ids = [r[0] for r in db.execute("SELECT video_id FROM yt_base")]
        ids.sort(key=lambda vid: _rand_key(seed, vid))
        page_ids = ids[(p - 1) * PER_PAGE: p * PER_PAGE]
        rows = []
        if page_ids:
            got = db.execute(
                "SELECT b.*, r.seq AS rseq FROM yt_base b "
                "LEFT JOIN records r ON r.video_id = b.video_id "
                "WHERE b.video_id IN (%s)" % ",".join("?" * len(page_ids)),
                page_ids).fetchall()
            by_id = {b["video_id"]: b for b in got}
            rows = [by_id[i] for i in page_ids]
    else:
        if sort == "date":
            ordsql = ("(b.upload_date IS NULL OR b.upload_date = ''), "
                      "b.upload_date %s, b.video_id"
                      % ("DESC" if d == "desc" else "ASC"))
        else:
            ordsql = ("(b.view_count IS NULL), b.view_count %s, b.video_id"
                      % ("DESC" if d == "desc" else "ASC"))
        rows = db.execute(
            "SELECT b.*, r.seq AS rseq FROM yt_base b "
            "LEFT JOIN records r ON r.video_id = b.video_id "
            "ORDER BY " + ordsql + " LIMIT ? OFFSET ?",
            (PER_PAGE, (p - 1) * PER_PAGE)).fetchall()
    body = head + _sortbar("base", sort, d, seed)
    body += "".join(_brow(b, ranks.get(b["video_id"])) for b in rows)

    def pq(n):
        return U("/" + _qs(view="base", p=n, sort=sort, dir=d,
                           seed=seed if sort == "random" else None))

    prev = (f"<a href=\"{pq(p - 1)}\">上一页</a>" if p > 1
            else "<span class=\"off\">上一页</span>")
    nxt = (f"<a href=\"{pq(p + 1)}\">下一页</a>" if p < pages
           else "<span class=\"off\">下一页</span>")
    body += f"<div class=\"pager\">{prev}<span>第 {p} / {pages} 页</span>{nxt}</div>"
    return page("元数据底座 · 野兽素材库", body)

def _num(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip()
    return int(s) if s.isdigit() else None


def _fmt_views(v):
    """数字带单位: >=1亿 -> X.XX亿 (尾零可省), >=1万 -> X.X万, 否则千分位原数。"""
    n = _num(v)
    if n is None:
        return "\u2014"
    if n >= 100000000:
        s = f"{n / 100000000:.2f}".rstrip("0").rstrip(".")
        return s + "\u4ebf"
    if n >= 10000:
        s = f"{n / 10000:.1f}".rstrip("0").rstrip(".")
        if s == "10000":
            s = f"{n / 100000000:.2f}".rstrip("0").rstrip(".")
            return s + "\u4ebf"
        return s + "\u4e07"
    return f"{n:,}"


def _views_html(v, rank=None):
    """列表行播放量片段: 单位格式 + 原值 title + 可选底座名次。"""
    n = _num(v)
    if n is None:
        return ""
    s = f"<span title=\"{n:,}\">播放 {_fmt_views(n)}</span>"
    if rank:
        s += f" · <span>第 {rank} 名</span>"
    return s


def _view_ranks(db):
    """底座 (excluded=0) 按播放量降序的内部名次: {video_id: rank}。"""
    if not _has_base(db):
        return {}
    cols = {r[1] for r in db.execute("PRAGMA table_info(yt_base)")}
    exw = " WHERE excluded=0" if "excluded" in cols else ""
    rows = db.execute(
        "SELECT video_id FROM yt_base" + exw +
        " ORDER BY (view_count IS NULL), view_count DESC, video_id").fetchall()
    return {r[0]: i + 1 for i, r in enumerate(rows)}


def _fmt_dur(sec):
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "—"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_date(d):
    d = str(d or "")
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d or "—"


def _has_base(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='yt_base'"
    ).fetchone() is not None


def _base_map(db, vids):
    vids = list(dict.fromkeys(v for v in vids if v))
    if not vids or not _has_base(db):
        return {}
    rows = db.execute(
        "SELECT * FROM yt_base WHERE video_id IN (%s)"
        % ",".join("?" * len(vids)), vids).fetchall()
    return {r["video_id"]: r for r in rows}


def _disp_views(r, bmap):
    """展示播放量: 底座 (yt-dlp 实抓) 优先, 缺失时回落清单回填值。"""
    b = bmap.get(r["video_id"]) if r["video_id"] else None
    if b is not None and not b["error"] and b["view_count"] is not None:
        return b["view_count"]
    return r["yt_view_count"]


def _disp_date(r, bmap):
    """展示发布日期: 一律取底座 upload_date 并格式化。"""
    b = bmap.get(r["video_id"]) if r["video_id"] else None
    if b is not None and not b["error"] and b["upload_date"]:
        return _fmt_date(b["upload_date"])
    return None


@app.route("/r/<int:seq>")
def detail(seq):
    db = get_db()
    r = db.execute("SELECT * FROM records WHERE seq=?", (seq,)).fetchone()
    if r is None:
        abort(404)
    p = (seq - 1) // PER_PAGE + 1
    base = (_base_map(db, [r["video_id"]]).get(r["video_id"])
            if r["video_id"] else None)
    pub_date = (_fmt_date(base["upload_date"])
                if base is not None and not base["error"] else None)
    fm_items = [
        ("序号", f"#{r['seq']:03d}"), ("来源 BV", r["source_bv"]),
        ("来源批次", "YouTube 字幕批" if r["origin"] == "yt" else "BV 精选批"),
        ("YouTube ID", r["video_id"]),
        ("发布时间", pub_date or r["published_at"]),
        ("来源", r["source"]), ("来源日期", r["source_date"]),
        ("来源文件", r["source_file"]), ("来源序号", r["source_index"]),
        ("标注字数", r["declared_words"]), ("转写状态", r["transcript_status"]),
    ]
    keys = set(r.keys())
    if r["video_id"]:
        fm_items += [("清单标题", r["yt_title"])]
        method = r["match_method"] if "match_method" in keys else None
        label = {"title": "标题匹配", "content": "内容核验",
                 "title+content": "标题匹配 + 内容核验",
                 "title_v2": "标题匹配 v2（多信号裁定）",
                 "direct": "批次直供（video_id 自带）"}.get(
                     method or "title", method or "标题匹配")
        fm_items.append(("匹配方式", label))
        if "content_cov" in keys and r["content_cov"] is not None:
            fm_items.append(("内容覆盖", "%.1f%%" % (r["content_cov"] * 100)))
        if "content_shared" in keys and r["content_shared"]:
            fm_items.append(("共享片段", "%d 段" % r["content_shared"]))
    fm = "<dl class=\"fm\">" + "".join(
        f"<div><dt>{k}：</dt><dd>{_html.escape(str(v)) if v else '—'}</dd></div>"
        for k, v in fm_items) + "</dl>"
    dl = "<div class=\"dl\">"
    if r["video_id"]:
        dl += (f"<a href=\"https://www.youtube.com/watch?v={r['video_id']}\" "
               "target=\"_blank\" rel=\"noopener\">YouTube ↗</a>")
    dl += (f"<a href=\"https://www.bilibili.com/video/{r['source_bv']}\" "
           "target=\"_blank\" rel=\"noopener\">Bilibili 源 ↗</a></div>")
    stats = ""
    if base is not None and not base["error"]:
        _rk = _view_ranks(db).get(r["video_id"]) if r["video_id"] else None
        _vv = _fmt_views(base["view_count"])
        if _rk:
            _vv += f" · 第 {_rk} 名"
        items = [("播放量", _vv),
                 ("点赞", _fmt_views(base["like_count"])),
                 ("评论", _fmt_views(base["comment_count"])),
                 ("时长", _fmt_dur(base["duration"])),
                 ("发布日期", _fmt_date(base["upload_date"])),
                 ("底座标题", base["title"])]
        if "tab" in base.keys() and base["tab"]:
            items.append(("类型", "Shorts" if base["tab"] == "shorts" else "长视频"))
        stats = ("<h3>统计 · 元数据底座</h3><dl class=\"fm\">" + "".join(
            f"<div><dt>{k}：</dt><dd>{_html.escape(str(v)) if v else '—'}</dd></div>"
            for k, v in items) + "</dl>"
            f"<p class=\"note\">统计来自元数据底座（yt-dlp 实抓，数据日期 "
            f"{_html.escape(base['fetched_at'] or '')}）；与清单回填值冲突时以底座为准。</p>")
    elif r["video_id"]:
        items = [("清单播放量", _fmt_views(r["yt_view_count"])),
                 ("清单时长", _fmt_dur(r["yt_duration_sec"]))]
        stats = ("<h3>统计 · 下载清单</h3><dl class=\"fm\">" + "".join(
            f"<div><dt>{k}：</dt><dd>{_html.escape(str(v)) if v else '—'}</dd></div>"
            for k, v in items) + "</dl>"
            "<p class=\"note\">底座暂无此条，统计为下载清单回填值。</p>")
    body = (f"<div class=\"back\"><a href=\"{U('/?p=%d' % p)}\">← 返回列表</a></div>"
            "<article>"
            f"<h1>{_html.escape(r['title_zh'])}</h1>"
            f"<div class=\"orig\">{_html.escape(r['title_original'] or '')}</div>"
            + fm + dl + stats + render_md(r["md_body"]) + "</article>")
    if r["has_txt"] and r["txt_text"] is not None:
        summ = (f"配对原文（SRT · {_html.escape(r['source_file'] or '')}）"
                if r["origin"] == "yt"
                else f"配对原文（{_html.escape(r['source_file'] or '')}）")
        body += (f"<details class=\"txt\"><summary>{summ}</summary>"
                 f"<pre>{_html.escape(r['txt_text'])}</pre></details>")
    else:
        body += "<details class=\"txt\"><summary>配对原文</summary><p>无配对原文。</p></details>"
    if "yt_md_body" in keys and r["yt_md_body"]:
        body += ("<article style=\"margin-top:14px\">"
                 "<div class=\"orig\">第二套来源文档 · YouTube SRT 字幕批</div>"
                 + render_md(r["yt_md_body"]) + "</article>")
        if r["yt_srt_text"]:
            body += (f"<details class=\"txt\"><summary>YouTube 来源原文"
                     f"（{_html.escape(r['yt_doc_name'] or '')}）</summary>"
                     f"<pre>{_html.escape(r['yt_srt_text'])}</pre></details>")
    return page(r["title_zh"] + " · 野兽素材库", body)


def _report_extra(db):
    h = []
    tiers = dict(db.execute(
        "SELECT match_tier, COUNT(*) FROM records GROUP BY match_tier").fetchall())
    methods = dict(db.execute(
        "SELECT COALESCE(match_method, '-'), COUNT(*) FROM records "
        "WHERE video_id IS NOT NULL GROUP BY 1").fetchall())
    h.append("<h2>最终匹配汇总（实时）</h2>")
    h.append(
        f"<div class=\"stats\">确定 {tiers.get('matched', 0)} 条"
        f"（标题 {methods.get('title', 0)} · 标题+内容核验 {methods.get('title+content', 0)}"
        f" · 内容核验 {methods.get('content', 0)} · 标题v2 {methods.get('title_v2', 0)}）· "
        f"疑似 {tiers.get('suspect', 0)} 条 · 未匹配 {tiers.get('unmatched', 0)} 条 · "
        f"无原版 {tiers.get('no_original', 0)} 条</div>")
    no_rows = db.execute(
        "SELECT seq, source_bv, match_method FROM records"
        " WHERE match_tier='no_original' ORDER BY seq").fetchall()
    if no_rows:
        tag = {"no_original": "B站独家", "compilation": "合集",
               "off_base": "底座外"}
        items = "、".join(
            "#%03d（%s）" % (x["seq"], tag.get(
                x["match_method"] or "no_original",
                x["match_method"] or "B站独家")) for x in no_rows)
        h.append(f"<div class=\"stats\">无原版明细：{items}</div>")
    if _has_base(db):
        bcols = {r[1] for r in db.execute("PRAGMA table_info(yt_base)")}
        has_ex = "excluded" in bcols
        exw = " AND excluded=0" if has_ex else ""
        total = db.execute(
            "SELECT COUNT(*) FROM yt_base WHERE 1=1" + exw).fetchone()[0]
        excl_n = (db.execute(
            "SELECT COUNT(*) FROM yt_base WHERE excluded=1").fetchone()[0]
            if has_ex else 0)
        ok = db.execute(
            "SELECT COUNT(*) FROM yt_base WHERE error IS NULL"
            + exw).fetchone()[0]
        tr = db.execute(
            "SELECT COUNT(*) FROM yt_base WHERE transcript_ready=1"
            + exw).fetchone()[0]
        n_md = db.execute(
            "SELECT COUNT(DISTINCT video_id) FROM records "
            "WHERE video_id IS NOT NULL").fetchone()[0]
        brow = db.execute(
            "SELECT value FROM meta WHERE key='base_report'").fetchone()
        bat = ""
        if brow:
            try:
                bat = " · 入库时间 " + json.loads(brow["value"]).get("at", "")
            except Exception:
                bat = ""
        h.append("<h2>元数据底座（yt-dlp 实抓）</h2>")
        h.append(f"<div class=\"stats\">共 {total} 条 · 抓取成功 {ok} · 失败 {total - ok} · "
                 f"转写就绪 {tr} · 已挂摘要 {n_md}"
                 + (f" · 已排除 {excl_n} 条超长视频（用户定稿排除，不计入）"
                    if excl_n else "")
                 + f"{bat}</div>")
        if brow:
            try:
                sc = json.loads(brow["value"]).get("scope") or {}
            except Exception:
                sc = {}
            if sc:
                h.append(
                    f"<div class=\"stats\">底座范围三方对账（频道枚举 ∪ 清单）："
                    f"频道长视频 {sc.get('channel_videos_tab')} 条（清单含 "
                    f"{sc.get('videos_in_list')}、缺 {sc.get('videos_not_list')}）· "
                    f"频道 Shorts {sc.get('channel_shorts_tab')} 条（清单含 "
                    f"{sc.get('shorts_in_list')}、缺 {sc.get('shorts_not_list')}）· "
                    f"清单 {sc.get('list_total')} 条（多出 {sc.get('list_not_channel')} 条）· "
                    f"两页交集 {sc.get('tab_overlap')} · 底座共 {sc.get('base_total')} 条；"
                    "Shorts 在底座里以 tab=shorts 标记，与长视频分开</div>")
        fails = db.execute(
            "SELECT video_id, error FROM yt_base WHERE error IS NOT NULL "
            "ORDER BY video_id").fetchall()
        if fails:
            h.append("<div class=\"scroll\"><table><tr><th>video_id</th><th>失败原因</th></tr>")
            for f in fails:
                h.append(f"<tr><td>{_html.escape(f['video_id'])}</td>"
                         f"<td>{_html.escape(f['error'] or '')}</td></tr>")
            h.append("</table></div>")
    crow = db.execute(
        "SELECT value FROM meta WHERE key='content_report'").fetchone()
    if crow:
        try:
            cr = json.loads(crow["value"])
        except Exception:
            cr = None
        if cr:
            h.append("<h2>内容核验（升级层：标题不够时读转写片段核验）</h2>")
            h.append(
                f"<div class=\"stats\">核验时间 {cr.get('at', '')} · 索引转写 "
                f"{cr.get('transcripts_indexed', 0)} 条 · 内容确认救回 "
                f"{len(cr.get('confirmed', []))} 条 · 标题匹配经内容确认 "
                f"{len(cr.get('verified', []))} 条 · 内容疑似 "
                f"{len(cr.get('suspects', []))} 条 · 冲突 "
                f"{len(cr.get('conflicts', []))} 条 · 未核验 "
                f"{cr.get('unverified', 0)} 条（候选转写未就绪）</div>")
            if cr.get("confirmed"):
                h.append("<div class=\"scroll\"><table><tr><th>序号</th><th>BV</th>"
                         "<th>中文名</th><th>YouTube ID</th><th>覆盖</th>"
                         "<th>共享片段</th><th>标题 dice</th><th>原层级</th></tr>")
                for m in cr["confirmed"]:
                    h.append("<tr><td class=\"mono\">#%03d</td><td>%s</td><td>%s</td>"
                             "<td>%s</td><td>%.1f%%</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                                 m["seq"], _html.escape(m["bv"]),
                                 _html.escape(m["title_zh"] or ""),
                                 _html.escape(m["video_id"] or ""),
                                 (m["cov"] or 0) * 100, m["shared"],
                                 m["title_dice"], _html.escape(m["from_tier"] or "")))
                h.append("</table></div>")
            for s in cr.get("suspects", []):
                h.append(f"<p><strong>#{s['seq']:03d} {_html.escape(s['bv'])}</strong> "
                         f"{_html.escape(s['title_zh'] or '')}<br>"
                         f"<span class=\"cand\">内容候选：{_html.escape(s['cand_title'] or '')} "
                         f"[{_html.escape(s['video_id'] or '')}] · 覆盖 "
                         f"{(s['cov'] or 0) * 100:.1f}% · 共享片段 {s['shared']} · "
                         f"标题 dice {s['title_dice']}</span><br>"
                         f"<span class=\"note\">{_html.escape(s['reason'] or '')}</span></p>")
            for c in cr.get("conflicts", []):
                h.append(f"<p><strong>#{c['seq']:03d} {_html.escape(c['bv'])}</strong> "
                         f"{_html.escape(c['title_zh'] or '')}：标题层保留 "
                         f"{_html.escape(c['kept_video_id'] or '')}，内容最强指向 "
                         f"{_html.escape(c['content_video_id'] or '')}"
                         f"（覆盖 {(c['cov'] or 0) * 100:.1f}%，共享 {c['shared']} 段）"
                         "——不自动改，待裁决。</p>")
            if cr.get("negative_control"):
                nc = cr["negative_control"]
                h.append(f"<p>负控制 {_html.escape(nc.get('bv', ''))}："
                         f"{_html.escape(nc.get('result', ''))}</p>")
            for d in cr.get("demoted", []):
                h.append(f"<p>{_html.escape(d['bv'])}：{_html.escape(d['reason'])}</p>")
    yrow = db.execute(
        "SELECT value FROM meta WHERE key='yt_batch_report'").fetchone()
    if yrow:
        try:
            yb = json.loads(yrow["value"])
        except Exception:
            yb = None
        if yb:
            h.append("<h2>YouTube 字幕批入库（video_id 精确挂接，不做模糊匹配）</h2>")
            h.append(
                f"<div class=\"stats\">入库时间 {yb.get('at', '')} · 视频 md "
                f"{yb.get('total_md', 0)} 篇 · 新建 {yb.get('new', 0)} 条"
                f"（序号自 #125 起：按发布日期升序、video_id 字典序定序）· "
                f"并入已有 {yb.get('merged', 0)} 条（第二套来源文档）· "
                f"重跑已在库 {yb.get('already', 0)} 条 · 底座覆盖 "
                f"{yb.get('base_covered', 0)}/{yb.get('total_md', 0)} · "
                f"待底座补齐 {len(yb.get('base_pending', []))} 条 · "
                f"无 md 的 srt {yb.get('srt_only', 0)} 个未建记录</div>")
            if yb.get("base_pending"):
                h.append("<div class=\"scroll\"><p>待底座补齐 video_id："
                         + " · ".join(_html.escape(v)
                                      for v in yb["base_pending"])
                         + "</p></div>")
            for s in yb.get("skipped_files", []):
                h.append(f"<p><strong>{_html.escape(s['file'])}</strong>："
                         f"{_html.escape(s['desc'])}</p>")
    return "".join(h)


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
         f"配对原文 {rep['paired']} 条 · 仅摘要 {len(rep['md_only'])} 条</div>"]
    h.append(_report_extra(db))
    h += ["<h2>标题匹配（标题层）</h2>",
          f"<div class=\"stats\">来源：{_html.escape(rep.get('title_source') or '用户下载清单')}</div>",
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
                     ("—" if m["dice"] is None else m["dice"]),
                     _html.escape(m["rule"])))
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
