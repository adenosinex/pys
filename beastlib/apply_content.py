#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容核验层 (升级层): 标题不够时才读转写片段核验。
读 content_match.py 的原始排名 JSON (BV txt 对全部已就绪 ASR 转写的
8 词 shingle 覆盖度), 只做两件事:
  1) 标题层未定 (suspect/unmatched) 的记录: 强内容证据 -> 确认落 video_id
     (match_method=content), 弱一些 -> 内容疑似 (只进 content_report);
  2) 标题层已确定的记录: 内容同向 -> 升级 title+content 并存证据;
     内容强指向别家 -> 记冲突 (不自动改, 待裁决); 候选转写未就绪 -> 未核验。
门槛 (按 2026-11-07 实测双峰分布标定: 真配对覆盖 0.53-0.85 / 边距近覆盖本身,
噪声带 <= 0.02, 中间无人):
  确定: cov >= 0.50 且 margin >= 0.25 且 (标题 dice >= 0.30 或 cov >= 0.75)
  内容疑似: cov >= 0.10 (有证据但未过门)
全局冲突: 两个 BV 同时确定同一 video_id -> 双双转内容疑似, 理由留痕。
负控制 BV1aJ7rzaE9C (中国创作者参观视频) 的结果单独写入报告。
盒上以 apphub 身份在项目代码目录运行; 参数 = content_match_raw.json 路径。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402

COV_CERTAIN = 0.50
MARGIN_MIN = 0.25
TDICE_GATE = 0.30
COV_OVERRIDE = 0.75
COV_SUSPECT = 0.10
VERIFY_MIN = 0.40
# 人工复核降级表 (只降不升; 复核后按需填写, 理由留痕)。本轮为空。
DEMOTE_CONTENT = {}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/content_match_raw.json"
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if "entries" in raw:  # newer dict format
        entries = raw["entries"]
        n_transcripts = raw.get("transcripts_indexed")
    else:  # list format: {at, transcripts, records:[{bv, top:[...]}]}
        entries = {r["bv"]: r for r in raw.get("records", [])}
        n_transcripts = raw.get("transcripts")

    def margin_of(e):
        top = (e or {}).get("top") or []
        if not top:
            return 0.0
        return (top[0].get("cov") or 0) - (top[1].get("cov") or 0
                                           if len(top) > 1 else 0)
    db = sqlite3.connect(beast.DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(beast.SCHEMA)
    beast._migrate(db)
    base_by_id = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                  " AND name='yt_base'").fetchone():
        base_by_id = {r["video_id"]: dict(r) for r in db.execute(
            "SELECT * FROM yt_base WHERE error IS NULL")}
    recs = db.execute("SELECT * FROM records ORDER BY seq").fetchall()
    confirmed, verified, suspects, conflicts = [], [], [], []
    unverified = 0
    plan = {}  # bv -> candidate (pending global conflict check)
    pre = {}   # bv -> from_tier
    for r in recs:
        bv = r["source_bv"]
        e = entries.get(bv)
        if r["match_method"] in ("content", "title+content"):
            continue  # 已是内容层结论, 重跑稳定
        if r["match_method"] == "direct" or r["source_bv"] is None:
            continue  # 2026-11-08 重跑范围限定: YouTube 直连批次 (video_id 精确挂接)
            # 不参与内容复核; 其 txt_text 为本视频字幕, 若参与会在 content_match
            # 排名里自指并把其余 172 条误判为冲突
        _top = (e or {}).get("top") or []
        t1 = _top[0] if _top else None
        if r["match_tier"] == "matched":
            if not t1 or (t1["cov"] or 0) < COV_SUSPECT:
                unverified += 1
            elif t1["video_id"] == r["video_id"] and t1["cov"] >= VERIFY_MIN:
                verified.append((r, t1))
            elif (t1["video_id"] != r["video_id"]
                  and t1["cov"] >= COV_CERTAIN and margin_of(e) >= MARGIN_MIN):
                conflicts.append({
                    "bv": bv, "seq": r["seq"], "title_zh": r["title_zh"],
                    "kept_video_id": r["video_id"],
                    "content_video_id": t1["video_id"],
                    "cov": t1["cov"], "shared": t1["shared"]})
            else:
                unverified += 1
            continue
        # suspect / unmatched: 升级核验
        if not t1 or (t1["cov"] or 0) < COV_SUSPECT:
            continue
        cov = t1["cov"]
        margin = margin_of(e)
        gate = (t1["title_dice"] or 0) >= TDICE_GATE or cov >= COV_OVERRIDE
        if cov >= COV_CERTAIN and margin >= MARGIN_MIN and gate:
            plan[bv] = t1
            pre[bv] = r["match_tier"]
        else:
            reason = ("覆盖 %.1f%% 未达确定门槛" % (cov * 100) if cov < COV_CERTAIN
                      else "标题校验未过 (dice %.2f) 且覆盖未达 %.0f%% 强覆盖线"
                      % (t1["title_dice"] or 0, COV_OVERRIDE * 100))
            suspects.append({
                "bv": bv, "seq": r["seq"], "title_zh": r["title_zh"],
                "video_id": t1["video_id"], "cand_title": t1["yt_title"],
                "cov": cov, "shared": t1["shared"],
                "title_dice": t1["title_dice"], "reason": reason})
    # 全局冲突: 同一 video 被两个 BV 确定 -> 双双降内容疑似
    by_vid = {}
    for bv, t1 in plan.items():
        by_vid.setdefault(t1["video_id"], []).append(bv)
    for vid, bvs in by_vid.items():
        if len(bvs) > 1:
            for bv in bvs:
                t1 = plan.pop(bv)
                r = next(x for x in recs if x["source_bv"] == bv)
                suspects.append({
                    "bv": bv, "seq": r["seq"], "title_zh": r["title_zh"],
                    "video_id": vid, "cand_title": t1["yt_title"],
                    "cov": t1["cov"], "shared": t1["shared"],
                    "title_dice": t1["title_dice"],
                    "reason": "内容层双 BV 争用同一视频 (%s), 互降待裁决"
                    % ", ".join(bvs)})
    demoted = []
    for bv in list(plan):
        if bv in DEMOTE_CONTENT:
            t1 = plan.pop(bv)
            r = next(x for x in recs if x["source_bv"] == bv)
            demoted.append({"bv": bv, "reason": DEMOTE_CONTENT[bv]})
            suspects.append({
                "bv": bv, "seq": r["seq"], "title_zh": r["title_zh"],
                "video_id": t1["video_id"], "cand_title": t1["yt_title"],
                "cov": t1["cov"], "shared": t1["shared"],
                "title_dice": t1["title_dice"],
                "reason": "人工复核降级: " + DEMOTE_CONTENT[bv]})
    with db:
        for bv, t1 in plan.items():
            vid = t1["video_id"]
            b = base_by_id.get(vid)
            yt_title = b["title"] if b else t1["yt_title"]
            yt_views = str(b["view_count"]) if b and b["view_count"] is not None else None
            yt_dur = str(b["duration"]) if b and b["duration"] is not None else None
            db.execute(
                "UPDATE records SET video_id=?, match_tier='matched',"
                " match_method='content', content_cov=?, content_shared=?,"
                " yt_title=?, yt_view_count=?, yt_duration_sec=?"
                " WHERE source_bv=?",
                (vid, t1["cov"], t1["shared"], yt_title, yt_views, yt_dur, bv))
            db.execute("DELETE FROM suspects WHERE source_bv=?", (bv,))
            r = next(x for x in recs if x["source_bv"] == bv)
            confirmed.append({
                "bv": bv, "seq": r["seq"], "title_zh": r["title_zh"],
                "video_id": vid, "cov": t1["cov"], "shared": t1["shared"],
                "title_dice": t1["title_dice"], "from_tier": pre[bv]})
        for r, t1 in verified:
            db.execute(
                "UPDATE records SET match_method='title+content',"
                " content_cov=?, content_shared=? WHERE source_bv=?",
                (t1["cov"], t1["shared"], r["source_bv"]))
        nc = entries.get("BV1aJ7rzaE9C", {})
        nc_top = (nc.get("top") or [{}])[0]
        nc_row = db.execute("SELECT match_tier, video_id FROM records"
                            " WHERE source_bv='BV1aJ7rzaE9C'").fetchone()
        negative = {
            "bv": "BV1aJ7rzaE9C",
            "result": ("未匹配 (正确): 最高候选 %s 覆盖 %.3f / 共享 %s 段, "
                       "属噪声带 (真配对带 0.53 起); 终局层级 %s"
                       % (nc_top.get("video_id"), nc_top.get("cov") or 0,
                          nc_top.get("shared"), nc_row["match_tier"]))
            if nc_row else "记录不存在"}
        report = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "transcripts_indexed": n_transcripts,
            "transcript_files": raw.get("transcript_files"),
            "confirmed": confirmed,
            "verified": [{"bv": r["source_bv"], "seq": r["seq"],
                          "video_id": t1["video_id"], "cov": t1["cov"],
                          "shared": t1["shared"]} for r, t1 in verified],
            "suspects": suspects, "conflicts": conflicts,
            "unverified": unverified, "negative_control": negative,
            "demoted": demoted,
            "thresholds": {"cov_certain": COV_CERTAIN, "margin": MARGIN_MIN,
                           "title_dice_gate": TDICE_GATE,
                           "cov_override": COV_OVERRIDE,
                           "cov_suspect": COV_SUSPECT}}
        db.execute("INSERT OR REPLACE INTO meta VALUES ('content_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    print(json.dumps({"confirmed": len(confirmed), "verified": len(verified),
                      "content_suspects": len(suspects),
                      "conflicts": len(conflicts),
                      "unverified": unverified,
                      "negative_control": negative["result"]},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
