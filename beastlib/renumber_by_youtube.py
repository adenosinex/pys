#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全库统一重编号: 按 YouTube 基座发布日期编号 (用户 2026-11-08 定稿)。

规则: 以 yt_base.upload_date 升序 (最早 = #001); 同日以 video_id
字典序 tiebreak; 同一 video_id 被两条记录共指 (双来源共存) 时日期与
video_id 都相同, 再以旧 seq 升序作最终 tiebreak 保持确定性。
旧 -> 新映射完整存入 meta 'seq_remap_20261108'
({at, rule, count, mapping: {old_seq: new_seq}})。
结构前提 (已核清): suspects 按 source_bv 建键、records_fts 按
records.id 挂接, 均不引用 seq; 列表 ORDER BY seq, 详情 /r/<seq>
要求新序号 1..N 连续 —— 本脚本写后自检连续性。
前置断言: 每条记录必须有 video_id 且在 yt_base 有 upload_date,
否则整趟中止不写库。将来导入新批次后重跑本脚本即恢复全局日期序。
盒上以 apphub 身份在项目代码目录运行 (无参数)。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402

META_KEY = "seq_remap_20261108"
RULE = ("yt_base.upload_date ASC, video_id ASC,"
        " old seq ASC (dup video_id final tiebreak)")


def main():
    db = sqlite3.connect(beast.DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT r.id AS rid, r.seq AS old_seq, r.video_id AS vid,"
        " b.upload_date AS ud"
        " FROM records r LEFT JOIN yt_base b ON b.video_id = r.video_id"
    ).fetchall()
    bad = [r for r in rows if not r["vid"] or not r["ud"]]
    assert not bad, "records missing base identity/date: %s" % [
        (r["rid"], r["old_seq"], r["vid"]) for r in bad]
    rows.sort(key=lambda r: (r["ud"], r["vid"], r["old_seq"]))
    mapping = {}
    plan = []
    for i, r in enumerate(rows, 1):
        mapping[str(r["old_seq"])] = i
        plan.append((i, r["rid"]))
    payload = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule": RULE,
        "count": len(plan),
        "mapping": mapping,
    }
    with db:
        for new_seq, rid in plan:
            db.execute("UPDATE records SET seq=? WHERE id=?",
                       (new_seq, rid))
        db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (META_KEY, json.dumps(payload, ensure_ascii=False)))
    seqs = [r[0] for r in db.execute("SELECT seq FROM records ORDER BY seq")]
    assert seqs == list(range(1, len(seqs) + 1)), "seq not contiguous"
    first = db.execute(
        "SELECT r.seq, r.title_zh, b.upload_date FROM records r"
        " JOIN yt_base b ON b.video_id = r.video_id"
        " ORDER BY r.seq LIMIT 1").fetchone()
    last = db.execute(
        "SELECT r.seq, r.title_zh, b.upload_date FROM records r"
        " JOIN yt_base b ON b.video_id = r.video_id"
        " ORDER BY r.seq DESC LIMIT 1").fetchone()
    print("renumbered %d records; seq 1..%d contiguous"
          % (len(seqs), len(seqs)))
    print("new #001:", tuple(first))
    print("new last:", tuple(last))
    print("mapping stored in meta '%s'" % META_KEY)


if __name__ == "__main__":
    main()
