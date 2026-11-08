#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""身份裁定第一阶段处置 (用户 2026-11-08 定稿):
  基准只用 YouTube 主频道基座 (yt_base) 数据; 无基座身份者剔除素材库。
  1. 剔除 12 条:
     - B 组 3 条: 已识别出 YouTube 姊妹频道身份, 但不在主频道基座,
       按基准口径剔除; 导出 JSON 单独成节并附 YouTube 身份便于恢复。
     - C 组 9 条: B站专属 / 中国创作者自制内容, 无 YouTube 对应, 剔除。
  2. 旧 #036 赋予基座身份 vBpQ1SlfVtU (主频道 World's Largest
     Dominoes Vs Store, 2023-09-02, yt_base 在册 excluded=0):
     tier 改 matched, match_method 记 identity_verdict, 统计字段
     从 yt_base 回填。此前识别趟给 #036 的三个伪成分 ID
     (7vsF9DFPmbQ / EzS7kbLShro / BzYgowhSaKQ) 经全库清查未写入
     任何记录字段或 meta, 在本报告中正式注明作废。
  3. 旧 #017 (合集) 暂不动; 重编号与页面日期展示本趟不做。
删除前整行导出 JSON (含 suspects 行) 到 EXPORT_PATH;
运行前须已做全量备份 beastlib.db.bak-identity-20261108 (脚本内断言)。
盒上以 apphub 身份在项目代码目录运行 (无参数)。
结果写入 meta 'identity_verdict_report'。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402

BACKUP_NAME = "beastlib.db.bak-identity-20261108"
EXPORT_PATH = "/tmp/deleted_records_identity_20261108.json"

# (seq, source_bv, YouTube 身份) — B 组: 姊妹频道, 不在基座
B_GROUP = [
    (6, "BV13Chw66EAa", {"video_id": "dGCdYtk9fHQ", "channel": "MrBeast 2"}),
    (42, "BV1KGKV6PEe3", {"video_id": "Q49kJeG6nr8",
                          "channel": "Beast Philanthropy",
                          "upload_date": "2024-02-25"}),
    (72, "BV1WpME6UEzt", {"video_id": "Ut41eePtfBk", "channel": "MrBeast 2"}),
]
# (seq, source_bv) — C 组: B站专属 / 中国创作者自制
C_GROUP = [
    (11, "BV15H4y1Y7Sm"),
    (25, "BV18c3g6kE4k"),
    (28, "BV19EEizDEc4"),
    (55, "BV1Nw411E7MW"),
    (69, "BV1ViVBzJERh"),
    (78, "BV1YXMwzPE5t"),
    (85, "BV1aJ7rzaE9C"),
    (110, "BV1qdY76eE46"),
    (123, "BV1yt5Tz7EMG"),
]
B_REASON = ("已识别 YouTube 姊妹频道身份, 但不在主频道基座内; "
            "用户 2026-11-08 基准裁定: 基座外一律剔除")
C_REASON = ("B站专属 / 中国创作者自制内容, 无 YouTube 对应; "
            "用户 2026-11-08 基准裁定: 剔除")

ASSIGN_SEQ, ASSIGN_BV, ASSIGN_VID = 36, "BV1EP411h7de", "vBpQ1SlfVtU"
KEEP_SEQ, KEEP_BV = 17, "BV16o1PBfEhv"
FAKE_IDS = ["7vsF9DFPmbQ", "EzS7kbLShro", "BzYgowhSaKQ"]


def prison_set(db):
    out = set()
    for _label, r in beast.search_records(db, "监狱"):
        out.add((r["seq"], r["source_bv"]))
    return out


def main():
    db_path = beast.DB_PATH
    bak = os.path.join(os.path.dirname(db_path), BACKUP_NAME)
    assert os.path.exists(bak), "缺少开工全量备份: %s" % bak
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.executescript(beast.SCHEMA)
    beast._migrate(db)

    total_before = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    tiers_before = dict(db.execute(
        "SELECT match_tier, COUNT(*) FROM records GROUP BY 1").fetchall())
    assert total_before == 758, "开工总数 %d != 758" % total_before
    assert tiers_before == {"matched": 744, "no_original": 14}, tiers_before

    # ---- 伪成分 ID 全库清查 (只读断言)
    fake_sweep = {}
    for vid in FAKE_IDS:
        n_vid = db.execute("SELECT COUNT(*) FROM records WHERE video_id=?",
                           (vid,)).fetchone()[0]
        n_meta = db.execute("SELECT COUNT(*) FROM meta WHERE value LIKE ?",
                            ("%" + vid + "%",)).fetchone()[0]
        n_txt = db.execute(
            "SELECT COUNT(*) FROM records WHERE md_body LIKE ?"
            " OR txt_text LIKE ? OR yt_md_body LIKE ? OR yt_srt_text LIKE ?",
            ("%" + vid + "%",) * 4).fetchone()[0]
        fake_sweep[vid] = {"records_video_id": n_vid, "meta": n_meta,
                           "records_text": n_txt}
        assert n_vid == 0 and n_meta == 0 and n_txt == 0, \
            "伪成分 ID %s 意外出现在库中: %s" % (vid, fake_sweep[vid])

    # ---- 删除目标核对 (只读)
    targets = []
    for seq, bv, yt in B_GROUP:
        targets.append((seq, bv, "B", B_REASON, yt))
    for seq, bv in C_GROUP:
        targets.append((seq, bv, "C", C_REASON, None))
    for seq, bv, grp, reason, yt in targets:
        r = db.execute("SELECT seq, match_tier, video_id FROM records"
                       " WHERE source_bv=?", (bv,)).fetchone()
        assert r and r["seq"] == seq, "待删记录对不上: %s" % bv
        assert r["match_tier"] == "no_original", \
            "#%d tier=%s, 不在 no_original 集" % (seq, r["match_tier"])
        assert r["video_id"] is None, "#%d 已有 video_id, 不应删除" % seq

    # ---- #036 赋值前提核对 (只读)
    r36 = db.execute("SELECT seq, match_tier, video_id FROM records"
                     " WHERE source_bv=?", (ASSIGN_BV,)).fetchone()
    assert r36 and r36["seq"] == ASSIGN_SEQ \
        and r36["match_tier"] == "no_original" and r36["video_id"] is None
    base = db.execute("SELECT * FROM yt_base WHERE video_id=?",
                      (ASSIGN_VID,)).fetchone()
    assert base and base["excluded"] == 0 and base["error"] is None, \
        "目标不在可用基座: %s" % ASSIGN_VID
    holder = db.execute("SELECT seq FROM records WHERE video_id=?",
                        (ASSIGN_VID,)).fetchone()
    assert holder is None, "目标已被 #%s 占用" % holder["seq"]

    # ---- #017 暂不动: 快照留待事后逐字核对
    keep_before = dict(db.execute("SELECT * FROM records WHERE source_bv=?",
                                  (KEEP_BV,)).fetchone())
    assert keep_before["seq"] == KEEP_SEQ

    prison_before = prison_set(db)

    # ---- 删除前整行导出 (先落盘, 后动库)
    export = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "note": "beastlib 身份裁定第一阶段剔除记录整行导出 (可恢复)",
              "backup": BACKUP_NAME, "count": len(targets),
              "groups": {"B_youtube_sister_off_base": [],
                         "C_bilibili_exclusive": []},
              "records": []}
    for seq, bv, grp, reason, yt in targets:
        r = db.execute("SELECT * FROM records WHERE source_bv=?",
                       (bv,)).fetchone()
        susp = [dict(x) for x in db.execute(
            "SELECT * FROM suspects WHERE source_bv=?", (bv,))]
        item = {"seq": seq, "source_bv": bv, "group": grp,
                "delete_reason": reason, "youtube_identity": yt,
                "record": dict(r), "suspects": susp}
        export["records"].append(item)
        key = "B_youtube_sister_off_base" if grp == "B" \
            else "C_bilibili_exclusive"
        export["groups"][key].append(item)
    with open(EXPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(export, fh, ensure_ascii=False, indent=1)
    assert os.path.getsize(EXPORT_PATH) > 1000, "导出文件异常小"

    deleted, assigned = [], None
    with db:
        # ---- 1. #036 赋予基座身份
        db.execute(
            "UPDATE records SET video_id=?, match_tier='matched',"
            " match_method='identity_verdict', yt_title=?,"
            " yt_view_count=?, yt_duration_sec=?, published_at=?"
            " WHERE source_bv=?",
            (ASSIGN_VID, base["title"],
             str(base["view_count"]) if base["view_count"] is not None
             else None,
             str(base["duration"]) if base["duration"] is not None
             else None,
             base["upload_date"], ASSIGN_BV))
        db.execute("DELETE FROM suspects WHERE source_bv=?", (ASSIGN_BV,))
        assigned = {"seq": ASSIGN_SEQ, "source_bv": ASSIGN_BV,
                    "video_id": ASSIGN_VID, "yt_title": base["title"],
                    "upload_date": base["upload_date"]}
        # ---- 2. 删除 12 条 (连同 suspects 候选行)
        for seq, bv, grp, reason, yt in targets:
            db.execute("DELETE FROM suspects WHERE source_bv=?", (bv,))
            cur = db.execute("DELETE FROM records WHERE source_bv=?", (bv,))
            assert cur.rowcount == 1, "删除行数异常: %s" % bv
            deleted.append({"seq": seq, "bv": bv, "group": grp,
                            "youtube_identity": yt})
        db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
        # ---- 3. 事务内验收
        total_after = db.execute(
            "SELECT COUNT(*) FROM records").fetchone()[0]
        tiers_after = dict(db.execute(
            "SELECT match_tier, COUNT(*) FROM records"
            " GROUP BY 1").fetchall())
        assert total_after == 746, "收尾总数 %d != 746" % total_after
        assert tiers_after == {"matched": 745, "no_original": 1}, tiers_after
        sole = db.execute("SELECT seq, source_bv FROM records"
                          " WHERE match_tier='no_original'").fetchone()
        assert sole["seq"] == KEEP_SEQ and sole["source_bv"] == KEEP_BV
        keep_after = dict(db.execute(
            "SELECT * FROM records WHERE source_bv=?",
            (KEEP_BV,)).fetchone())
        assert keep_after == keep_before, "#017 被意外改动"
        r36a = db.execute("SELECT video_id, match_tier, match_method"
                          " FROM records WHERE source_bv=?",
                          (ASSIGN_BV,)).fetchone()
        assert r36a["video_id"] == ASSIGN_VID \
            and r36a["match_tier"] == "matched" \
            and r36a["match_method"] == "identity_verdict"
        prison_after = prison_set(db)
        report = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "用户 2026-11-08 身份裁定第一阶段: 基准只用 YouTube"
                      " 主频道基座; B 组 (姊妹频道/基座外) 与 C 组"
                      " (B站专属) 共 12 条剔除; #036 赋予基座身份;"
                      " #017 合集暂缓; 重编号与日期展示不在本趟",
            "backup": BACKUP_NAME, "export": EXPORT_PATH,
            "total_before": total_before, "total_after": total_after,
            "tiers_before": tiers_before, "tiers_after": tiers_after,
            "deleted": deleted, "assigned": assigned,
            "kept_pending": {"seq": KEEP_SEQ, "source_bv": KEEP_BV,
                             "note": "合集, 处置待用户拍板, 本趟未动"},
            "fake_component_ids_voided": {
                "ids": FAKE_IDS, "sweep": fake_sweep,
                "note": "识别趟给 #036 的三个伪成分 ID 经全库清查"
                        "未写入任何记录字段或 meta, 无可清除;"
                        "随 #036 改判单片定案, 该成分指认正式作废"},
            "prison_search": {
                "before": sorted(prison_before),
                "after": sorted(prison_after),
                "removed_by_deletion": sorted(prison_before - prison_after),
                "added": sorted(prison_after - prison_before)},
        }
        db.execute("INSERT OR REPLACE INTO meta VALUES"
                   " ('identity_verdict_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    print(json.dumps({"total_before": total_before,
                      "total_after": total_after,
                      "tiers_before": tiers_before,
                      "tiers_after": tiers_after,
                      "deleted": len(deleted), "assigned": assigned,
                      "prison_before": sorted(prison_before),
                      "prison_after": sorted(prison_after),
                      "export": EXPORT_PATH},
                     ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
