#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标题层重跑: 候选改为 yt_base 的原始标题 (yt-dlp 实抓, 比清单标题权威),
复用 app.match_records 同一算法; 时长/播放量随底座一并作回填值。
- 内容层结论 (match_method = content / title+content) 完全不动。
- 标题层结果全量重写: records 的 video_id/match_tier/yt_* (yt_* 从底座取),
  suspects 表重写, meta import_report 重写 (title_source = 元数据底座)。
- 打印与重跑前的差异清单 (升/降/换配)。
盒上以 apphub 身份在项目代码目录运行 (无参数)。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402


def main():
    db = sqlite3.connect(beast.DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(beast.SCHEMA)
    beast._migrate(db)
    base_rows = db.execute(
        "SELECT * FROM yt_base WHERE error IS NULL").fetchall()
    if not base_rows:
        print("yt_base 为空, 先跑 ingest_base")
        return
    cand_rows = [{"video_id": r["video_id"], "title": r["title"],
                  "view_count": str(r["view_count"] or ""),
                  "duration_sec": str(r["duration"] or "")}
                 for r in base_rows]
    base_by_id = {r["video_id"]: r for r in base_rows}
    recs = db.execute("SELECT * FROM records ORDER BY seq").fetchall()
    old = {r["source_bv"]: dict(r) for r in recs}
    results, conflicts = beast.match_records(
        [{"bv": r["source_bv"], "to": r["title_original"] or ""}
         for r in recs], cand_rows)
    by_bv = {r["bv"]: r for r in results}
    matched_list, suspect_list, unmatched_list, demoted_list = [], [], [], []
    changes = []
    n_matched = n_suspect = n_unmatched = kept_content = 0
    with db:
        db.execute("DELETE FROM suspects")
        for r in recs:
            bv = r["source_bv"]
            o = old[bv]
            if o.get("match_method") in ("content", "title+content", "title_v2") and o.get("video_id"):
                kept_content += 1
                n_matched += 1
                matched_list.append({
                    "bv": bv, "seq": r["seq"], "title_zh": r["title_zh"],
                    "video_id": o["video_id"], "yt_title": o["yt_title"],
                    "views": o["yt_view_count"], "dice": None,
                    "rule": o["match_method"]})
                continue
            res = by_bv[bv]
            tier = res["tier"]
            new_vid = None
            if res.get("demoted"):
                demoted_list.append({"bv": bv, "reason": res["demoted"]})
            if tier == "matched":
                n_matched += 1
                new_vid = res["best"]["video_id"]
                b = base_by_id.get(new_vid)
                yt_title = b["title"] if b else None
                yt_views = str(b["view_count"]) if b and b["view_count"] is not None else None
                yt_dur = str(b["duration"]) if b and b["duration"] is not None else None
                db.execute(
                    "UPDATE records SET video_id=?, match_tier='matched',"
                    " match_method='title', yt_title=?, yt_view_count=?,"
                    " yt_duration_sec=? WHERE source_bv=?",
                    (new_vid, yt_title, yt_views, yt_dur, bv))
                matched_list.append({
                    "bv": bv, "seq": r["seq"], "title_zh": r["title_zh"],
                    "video_id": new_vid, "yt_title": yt_title,
                    "views": yt_views, "dice": res["best"]["dice"],
                    "rule": res["rule"]})
            elif tier == "suspect":
                n_suspect += 1
                db.execute(
                    "UPDATE records SET video_id=NULL, match_tier='suspect',"
                    " match_method=NULL, yt_title=NULL, yt_view_count=NULL,"
                    " yt_duration_sec=NULL WHERE source_bv=?", (bv,))
                suspect_list.append({
                    "bv": bv, "seq": r["seq"], "title_zh": r["title_zh"],
                    "title_original": r["title_original"],
                    "candidates": res["top"],
                    "note": res.get("demoted") or (
                        "双 BV 争用同一视频, 已互降" if res.get("conflict") else "")})
                for c in res["top"]:
                    if c:
                        db.execute(
                            "INSERT OR REPLACE INTO suspects VALUES (?,?,?,?)",
                            (bv, c["video_id"], c["title"], c["dice"]))
            else:
                n_unmatched += 1
                db.execute(
                    "UPDATE records SET video_id=NULL, match_tier='unmatched',"
                    " match_method=NULL, yt_title=NULL, yt_view_count=NULL,"
                    " yt_duration_sec=NULL WHERE source_bv=?", (bv,))
                unmatched_list.append({"bv": bv, "seq": r["seq"],
                                       "title_zh": r["title_zh"]})
            if o["match_tier"] != tier or o["video_id"] != new_vid:
                changes.append({
                    "bv": bv, "seq": r["seq"],
                    "from": f"{o['match_tier']}/{o['video_id']}",
                    "to": f"{tier}/{new_vid}"})
        row = db.execute(
            "SELECT value FROM meta WHERE key='import_report'").fetchone()
        old_rep = json.loads(row[0]) if row else {}
        total = len(recs)
        new_rep = dict(old_rep)
        new_rep.update({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": total,
            "matched": n_matched, "suspect": n_suspect,
            "unmatched": n_unmatched,
            "matched_rate": round(n_matched / total * 100, 1) if total else 0,
            "title_source": "元数据底座原始标题 (yt-dlp 实抓, %d 条成功)" % len(base_rows),
            "conflicts": conflicts, "demoted": demoted_list,
            "matched_list": matched_list, "suspect_list": suspect_list,
            "unmatched_list": unmatched_list,
            "notes": [
                "标题层候选 = 元数据底座的原始标题 (yt-dlp 实抓); 与清单标题不一致时以底座为准",
                "统计字段 (播放/点赞/评论/发布日期/时长) 一律以底座为准; 清单仅有播放量/时长",
                "内容层结论 (content / title+content) 强于标题层, 本重跑保留不覆盖 (%d 条)" % kept_content,
                "疑似不落库, 报告并排供裁决; 未匹配 video_id 留空; 只降不升, 人工降级理由见复核段",
            ]})
        db.execute("INSERT OR REPLACE INTO meta VALUES ('import_report', ?)",
                   (json.dumps(new_rep, ensure_ascii=False),))
    db.close()
    print(json.dumps({"matched": n_matched, "suspect": n_suspect,
                      "unmatched": n_unmatched, "kept_content": kept_content,
                      "changes": changes}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
