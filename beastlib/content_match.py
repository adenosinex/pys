#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容匹配: BV 配对 txt 与 YouTube ASR 转写 (.txt) 的词级 shingle 重合。

原理: 同一视频的两份英文转写长片段重合近乎唯一, 远强于改写标题。
规范化 (小写, 只取 [a-z0-9]+ 词元) 后取 8 词 shingle, 对全部已就绪
转写建倒排索引; 每篇 BV txt 计算与各候选的共享 shingle 覆盖度
(共享数 / BV shingle 总数) 并排名; 标题 dice 作二次校验。

本脚本只读, 输出原始排名 JSON + 覆盖分布, 不改数据库。
落库由 apply_content.py 按校准后的门槛执行。

用法 (盒上):
  python3 content_match.py [--out ~/beastlib-base/content_match_raw.json]
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, "/srv/apphub/code/python/beastlib")
import app as beast_app  # noqa: E402  (reuse _tokens for title dice)

DB = "/srv/apphub/data/projects/beastlib/beastlib.db"
SHINGLE = 8
WORD_RE = re.compile(r"[a-z0-9]+")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MIN_SHINGLES = 30


def words(text):
    return WORD_RE.findall((text or "").lower())


def shingle_set(toks):
    if len(toks) < SHINGLE:
        return set()
    return {" ".join(toks[i:i + SHINGLE]) for i in range(len(toks) - SHINGLE + 1)}


def title_dice(a, b):
    sa, sb = set(beast_app._tokens(a or "")), set(beast_app._tokens(b or ""))
    if not sa or not sb:
        return 0.0
    return 2.0 * len(sa & sb) / (len(sa) + len(sb))


def main():
    ap = argparse.ArgumentParser()
    home = os.path.expanduser("~")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--audio-dir", default=os.path.join(home, "mrbeast/audio"))
    ap.add_argument("--csv", default=os.path.join(home, "mrbeast/still_to_download.csv"))
    ap.add_argument("--out", default=os.path.join(home, "beastlib-base/content_match_raw.json"))
    args = ap.parse_args()

    yt_title = {}
    with open(args.csv, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            yt_title[r["video_id"]] = r.get("title") or ""

    # --- build shingle index over ready transcripts
    index = defaultdict(set)
    yt_shingles = {}
    files = sorted(f for f in os.listdir(args.audio_dir) if f.endswith(".txt"))
    for f in files:
        vid = f[:-4].split(" - ")[0]
        if not ID_RE.match(vid) or vid in yt_shingles:
            continue
        with open(os.path.join(args.audio_dir, f), encoding="utf-8", errors="replace") as fh:
            sh = shingle_set(words(fh.read()))
        yt_shingles[vid] = sh
        for s in sh:
            index[s].add(vid)
    print("transcripts indexed: %d (files %d)" % (len(yt_shingles), len(files)), flush=True)

    db = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    db.row_factory = sqlite3.Row
    recs = db.execute(
        "SELECT seq, source_bv, title_zh, title_original, txt_text, video_id, match_tier "
        "FROM records ORDER BY seq").fetchall()
    db.close()

    out_recs = []
    for r in recs:
        toks = words(r["txt_text"])
        sh = shingle_set(toks)
        entry = {"bv": r["source_bv"], "seq": r["seq"], "title_zh": r["title_zh"],
                 "title_original": r["title_original"],
                 "existing_video_id": r["video_id"], "existing_tier": r["match_tier"],
                 "n_words": len(toks), "n_shingles": len(sh)}
        if len(sh) < MIN_SHINGLES:
            entry["status"] = "insufficient"
            out_recs.append(entry)
            continue
        cnt = Counter()
        for s in sh:
            for vid in index.get(s, ()):  # shared shingle -> candidate
                cnt[vid] += 1
        ranked = []
        for vid, shared in cnt.most_common(3):
            ranked.append({
                "video_id": vid, "shared": shared,
                "cov": round(shared / len(sh), 4),
                "yt_cov": round(shared / len(yt_shingles[vid]), 4) if yt_shingles[vid] else 0,
                "title_dice": round(title_dice(r["title_original"], yt_title.get(vid, "")), 3),
                "yt_title": yt_title.get(vid, ""),
            })
        entry["status"] = "ranked"
        entry["top"] = ranked
        out_recs.append(entry)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "transcripts": len(yt_shingles), "records": out_recs},
                  fh, ensure_ascii=False, indent=1)
    print("wrote", args.out)

    # --- distribution printout for threshold calibration
    print("\nbv seq | cov1 cov2 margin tdice1 | existing | top1")
    for e in out_recs:
        if e["status"] != "ranked" or not e["top"]:
            print("%s #%03d INSUFFICIENT shingles=%d words=%d existing=%s" % (
                e["bv"], e["seq"], e["n_shingles"], e["n_words"], e["existing_video_id"]))
            continue
        t = e["top"]
        cov1 = t[0]["cov"]
        cov2 = t[1]["cov"] if len(t) > 1 else 0.0
        print("%s #%03d | %.3f %.3f %.3f %.3f | %s | %s %s" % (
            e["bv"], e["seq"], cov1, cov2, cov1 - cov2, t[0]["title_dice"],
            e["existing_video_id"], t[0]["video_id"],
            "SAME" if t[0]["video_id"] == e["existing_video_id"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
