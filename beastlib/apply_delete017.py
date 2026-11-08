#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二阶段处置 1/2 (用户 2026-11-08 拍板):
剔除旧 #017 BV16o1PBfEhv《能带走多少现金就拿多少》。
该条为合集: 由基座内两支视频首尾连播剪成
(tYHTVjfShOA《Anything You Can Carry, I'll Pay For》2019-04-20 +
 npDey6_9YRs 同题第二集 2019-09-06), 无单一基座身份;
用户在 A/B 选项中选定「剔除 #017(合集, 无单一身份)」。
删前整行已由主会话导出到 beastlib-work/
deleted_record_017_compilation_20261108.json 保底;
本脚本再把处置留痕写进 meta 'phase2_disposition_report'。
运行前须已做全量备份 beastlib.db.bak-renumber-20261108 (脚本内断言)。
盒上以 apphub 身份在项目代码目录运行 (无参数)。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402

BACKUP_NAME = "beastlib.db.bak-renumber-20261108"
TARGET_SEQ = 17
TARGET_BV = "BV16o1PBfEhv"
COMPONENTS = [
    {"video_id": "tYHTVjfShOA",
     "title": "Anything You Can Carry, I'll Pay For Challenge",
     "upload_date": "2019-04-20"},
    {"video_id": "npDey6_9YRs",
     "title": "Anything You Can Carry, I'll Pay For Challenge (part 2)",
     "upload_date": "2019-09-06"},
]


def main():
    db_path = beast.DB_PATH
    bak = os.path.join(os.path.dirname(db_path), BACKUP_NAME)
    assert os.path.exists(bak), "backup missing: " + bak
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    total0 = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert total0 == 746, "unexpected total before: %d" % total0
    row = db.execute(
        "SELECT * FROM records WHERE seq=? AND source_bv=?",
        (TARGET_SEQ, TARGET_BV)).fetchone()
    assert row is not None, "target record not found"
    assert row["video_id"] is None, "target unexpectedly has video_id"
    assert row["match_method"] == "compilation", \
        "target match_method=%r" % row["match_method"]
    sus = db.execute(
        "SELECT COUNT(*) FROM suspects WHERE source_bv=?",
        (TARGET_BV,)).fetchone()[0]
    report = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": "delete_compilation",
        "decided_by": "user option 2026-11-08: delete #017 (compilation)",
        "record": {"id": row["id"], "seq": row["seq"],
                   "source_bv": row["source_bv"],
                   "title_zh": row["title_zh"],
                   "title_original": row["title_original"],
                   "match_tier": row["match_tier"],
                   "match_method": row["match_method"],
                   "md_chars": len(row["md_body"] or ""),
                   "txt_chars": len(row["txt_text"] or "")},
        "class": "compilation_removed",
        "components_in_base": COMPONENTS,
        "suspects_rows_removed": sus,
        "export_file": "beastlib-work/"
                       "deleted_record_017_compilation_20261108.json",
    }
    with db:
        db.execute("DELETE FROM suspects WHERE source_bv=?", (TARGET_BV,))
        cur = db.execute(
            "DELETE FROM records WHERE seq=? AND source_bv=?",
            (TARGET_SEQ, TARGET_BV))
        assert cur.rowcount == 1, "deleted %d rows" % cur.rowcount
        db.execute(
            "INSERT INTO meta(key, value) VALUES('phase2_disposition_report', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(report, ensure_ascii=False),))
        db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
    total1 = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    tiers = dict(db.execute(
        "SELECT match_tier, COUNT(*) FROM records"
        " GROUP BY match_tier").fetchall())
    print("total: %d -> %d" % (total0, total1))
    print("tiers:", tiers)
    assert total1 == 745 and tiers.get("matched") == 745 \
        and tiers.get("no_original", 0) == 0
    print("OK: #017 removed; disposition stored in meta"
          " 'phase2_disposition_report'")


if __name__ == "__main__":
    main()
