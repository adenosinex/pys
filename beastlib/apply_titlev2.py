#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标题 v2 裁定应用 (用户 2026-11-08 批准): 纯标题/元数据四路信号
(汉字数字归一化签名 + 播放量先验 + 概念定向检索 + 转录密度校准)
对 59 条问题记录的裁定落库:
  - 29 条提议匹配 -> video_id + match_tier='matched' + match_method='title_v2',
    yt_* 从 yt_base 回填 (与 apply_rematch/apply_content 同惯例);
  - 12 条无 YouTube 原版 (B 站独家/合集/幕后, 标题自证) ->
    match_tier='no_original', match_method='no_original', video_id 留空;
  - 硬约束: zajUgQLviwk 只配 #070; _c2e1NcI3Sc 不在本段配给任何人。
裁定全文见 title_match_v2_verdicts.md; 结果写入 meta 'titlev2_report'。
盒上以 apphub 身份在项目代码目录运行 (无参数)。运行前须已备份 DB。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402

# (seq, source_bv, video_id, 置信)
MATCHES = [
    (3, "BV12z7wzLEX1", "KrLj6nc516A", "高"),
    (9, "BV14YWxzFEFf", "hxwpkM5w3Cc", "中高"),
    (14, "BV15fJk6aE79", "3OFj6l2tQ9s", "中高"),
    (15, "BV16U411S7cX", "00NgUctWoLQ", "中高"),
    (20, "BV1784y137sT", "YLt73w6criQ", "中高"),
    (34, "BV1DYNmeWENh", "NDsO1LT_0lw", "高"),
    (43, "BV1KGk1YDEtA", "gs8qfL9PNac", "高"),
    (45, "BV1KM4m1f7AN", "gHzuabZUd6c", "高"),
    (49, "BV1LUzaB7E3W", "pAnGwRiQ4-4", "中"),
    (51, "BV1M1421X7tQ", "l-nMKJ5J3Uc", "高"),
    (52, "BV1Mntd65Eci", "9RhWXPcKBI8", "高"),
    (59, "BV1QMUgBqEke", "Ah_uuTwGOYU", "中"),
    (60, "BV1R61dBaED3", "3RmOvxilbPM", "中高"),
    (65, "BV1U2BKBfEKJ", "ZFoNBxpXen4", "高"),
    (67, "BV1UuCTBrEw7", "E6E22XQPhhg", "高"),
    (70, "BV1WAXZYrEMb", "zajUgQLviwk", "中"),
    (79, "BV1Yp421o7NB", "A-vX1AGBGsc", "中"),
    (81, "BV1ZQHVz6EMk", "C680oxL__ck", "高"),
    (84, "BV1Zw411H7Ym", "7dYTw-jAYkY", "高"),
    (89, "BV1bMhV6PEtE", "Qtl8lJwbd4g", "中高"),
    (92, "BV1cyZaYjEnM", "AA-VpIj8F5Y", "中高"),
    (96, "BV1fM4m1X7ek", "erLbbextvlY", "高"),
    (97, "BV1ft421j7nn", "1WEAJ-DFkHE", "高"),
    (98, "BV1ggFPeXEYB", "sF5LYGgKbUA", "中高"),
    (101, "BV1hrKmzHEER", "3ryID_SwU5E", "中"),
    (106, "BV1nW421w7LC", "iogcY_4xGjo", "高"),
    (114, "BV1twabz4ENd", "Wdjh81uH6FU", "高"),
    (118, "BV1v6tHzkE4M", "0e3GPea1Tyg", "高"),
    (121, "BV1ww411V7w5", "K_CbgLpvH9E", "中高"),
]

# (seq, source_bv)
NO_ORIGINAL = [
    (6, "BV13Chw66EAa"), (11, "BV15H4y1Y7Sm"), (25, "BV18c3g6kE4k"),
    (28, "BV19EEizDEc4"), (36, "BV1EP411h7de"), (55, "BV1Nw411E7MW"),
    (69, "BV1ViVBzJERh"), (72, "BV1WpME6UEzt"), (78, "BV1YXMwzPE5t"),
    (85, "BV1aJ7rzaE9C"), (110, "BV1qdY76eE46"), (123, "BV1yt5Tz7EMG"),
]


def main():
    assert len(MATCHES) == 29 and len(NO_ORIGINAL) == 12
    vids = [m[2] for m in MATCHES]
    assert len(set(vids)) == len(vids), "裁定表内 video_id 重复"
    assert "_c2e1NcI3Sc" not in vids, "硬约束: _c2e1NcI3Sc 不在本段分配"
    assert [m[0] for m in MATCHES if m[2] == "zajUgQLviwk"] == [70], \
        "硬约束: zajUgQLviwk 只配 #070"
    db = sqlite3.connect(beast.DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(beast.SCHEMA)
    beast._migrate(db)
    base_by_id = {r["video_id"]: r for r in db.execute(
        "SELECT * FROM yt_base WHERE error IS NULL")}
    used = {r["video_id"]: r["seq"] for r in db.execute(
        "SELECT seq, video_id FROM records WHERE video_id IS NOT NULL")}
    for seq, bv, vid, _cf in MATCHES:
        assert vid in base_by_id, "底座无此视频: %s" % vid
        assert vid not in used, "video_id 已被 #%s 占用: %s" % (used.get(vid), vid)
    before = dict(db.execute(
        "SELECT match_tier, COUNT(*) FROM records GROUP BY 1").fetchall())
    applied, noorig = [], []
    with db:
        for seq, bv, vid, conf in MATCHES:
            row = db.execute(
                "SELECT seq, match_tier FROM records WHERE source_bv=?",
                (bv,)).fetchone()
            assert row and row["seq"] == seq, "记录对不上: %s" % bv
            assert row["match_tier"] in ("suspect", "unmatched"), \
                "#%d 当前 tier=%s, 不在问题集" % (seq, row["match_tier"])
            b = base_by_id[vid]
            yt_title = b["title"]
            yt_views = str(b["view_count"]) if b["view_count"] is not None else None
            yt_dur = str(b["duration"]) if b["duration"] is not None else None
            db.execute(
                "UPDATE records SET video_id=?, match_tier='matched',"
                " match_method='title_v2', yt_title=?, yt_view_count=?,"
                " yt_duration_sec=? WHERE source_bv=?",
                (vid, yt_title, yt_views, yt_dur, bv))
            db.execute("DELETE FROM suspects WHERE source_bv=?", (bv,))
            applied.append({"seq": seq, "bv": bv, "video_id": vid,
                            "confidence": conf, "yt_title": yt_title,
                            "from_tier": row["match_tier"]})
        for seq, bv in NO_ORIGINAL:
            row = db.execute(
                "SELECT seq, match_tier FROM records WHERE source_bv=?",
                (bv,)).fetchone()
            assert row and row["seq"] == seq, "记录对不上: %s" % bv
            assert row["match_tier"] in ("suspect", "unmatched")
            db.execute(
                "UPDATE records SET video_id=NULL, match_tier='no_original',"
                " match_method='no_original', yt_title=NULL,"
                " yt_view_count=NULL, yt_duration_sec=NULL, content_cov=NULL,"
                " content_shared=NULL WHERE source_bv=?", (bv,))
            db.execute("DELETE FROM suspects WHERE source_bv=?", (bv,))
            noorig.append({"seq": seq, "bv": bv,
                           "from_tier": row["match_tier"]})
        db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
        after = dict(db.execute(
            "SELECT match_tier, COUNT(*) FROM records GROUP BY 1").fetchall())
        report = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "title_match_v2_verdicts.md (用户 2026-11-08 批准应用)",
            "before": before, "after": after,
            "matched": applied, "no_original": noorig,
            "notes": [
                "match_method='title_v2' 保留溯源; 证据 (签名/密度/互证) 见裁定表",
                "#092 匹配成立但转录本身为残片 (transcript_status=partial), 与匹配无关",
                "zajUgQLviwk 按裁定只配 #070; _c2e1NcI3Sc 本段未分配 (留待内容仲裁)",
            ]}
        db.execute("INSERT OR REPLACE INTO meta VALUES ('titlev2_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    print(json.dumps({"before": before, "after": after,
                      "applied": len(applied), "no_original": len(noorig)},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
