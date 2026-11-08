#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube 字幕批入库: 用户基于导出字幕新产的 md+srt (front-matter 已带 video_id)。
规则 (用户定):
- 只认 video_id 精确挂接元数据底座, 不做任何模糊匹配/标题猜配;
- video_id 已有记录 (BV 批已匹配上的) -> 这篇 md + 配对 srt 作同一条记录的
  第二套来源文档并入 (yt_md_body/yt_srt_text), 不新建重复记录;
- video_id 全新 -> 建新记录 (origin='yt'), 序号在现有最大值后连续编;
  定序口径: 发布日期升序 (底座 upload_date 优先, 其次 fm published_at,
  缺失排最后), 同日按 video_id 字典序;
- 无 md 的 srt 不建记录; _ 开头的规范文件与 .py/.txt 流水线文件不入库
  (报告列明);
- 底座暂缺的 id: 记录先用 front-matter 字段立住, base_pending=1 并在报告
  列清单计数;
- 幂等: 按 video_id + 来源种类判重, 重跑只刷新不重复建; FTS 重建纳入
  新 md 与配对 srt 文本; meta 写 yt_batch_report。
盒上以 apphub 身份在项目代码目录运行; 参数 = zip 路径。
"""
import json
import os
import sqlite3
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402


def main():
    zpath = (sys.argv[1] if len(sys.argv) > 1
             else "/tmp/mrbeast-subtitles-20261107.zip")
    z = zipfile.ZipFile(zpath)
    mds, srts, skipped = {}, {}, []
    for n in z.namelist():
        base = n.rsplit("/", 1)[-1]
        if not base:
            continue
        low = base.lower()
        stem = base.rsplit(".", 1)[0]
        if low.endswith(".md"):
            text = z.read(n).decode("utf-8", "replace")
            fm0, _ = beast.parse_front_matter(text)
            if fm0.get("video_id"):
                mds[stem] = (base, text)
            else:
                skipped.append({"file": base,
                                "desc": "无 video_id 的规范/说明文件, 非视频文档, 未入库"})
        elif low.endswith(".srt"):
            srts[stem] = (base, z.read(n).decode("utf-8", "replace"))
        elif low.endswith((".py", ".txt")):
            skipped.append({"file": base, "desc": "流水线脚本/清单文件, 未入库"})
    srt_only = sorted(set(srts) - set(mds))
    db = sqlite3.connect(beast.DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(beast.SCHEMA)
    beast._migrate(db)
    base_ids, base_dates = set(), {}
    if beast._has_base(db):
        for r in db.execute("SELECT video_id, upload_date FROM yt_base"
                            " WHERE error IS NULL"):
            base_ids.add(r["video_id"])
            base_dates[r["video_id"]] = r["upload_date"]
    existing = {r["video_id"]: dict(r) for r in db.execute(
        "SELECT * FROM records WHERE video_id IS NOT NULL")}
    max_seq = db.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM records").fetchone()[0]
    new_items, merged, already = [], 0, 0
    pending, covered = [], 0
    with db:
        for stem in sorted(mds):
            fname, text = mds[stem]
            fm, body = beast.parse_front_matter(text)
            vid = fm.get("video_id") or stem.split(" - ")[0]
            srt_name, srt_text = srts.get(stem, (None, None))
            if vid in base_ids:
                covered += 1
            else:
                pending.append(vid)
            ex = existing.get(vid)
            if ex is not None:
                if ex["origin"] == "yt":
                    db.execute(
                        "UPDATE records SET md_body=?, txt_text=?, has_txt=?,"
                        " keywords=?, title_zh=?, title_original=?,"
                        " source_file=?, transcript_status=?, base_pending=?"
                        " WHERE id=?",
                        (body, srt_text, 1 if srt_text is not None else 0,
                         beast.extract_keywords(body),
                         fm.get("title_zh") or ex["title_zh"],
                         beast._s(fm.get("title_original")),
                         beast._s(fm.get("source_file")),
                         beast._s(fm.get("transcript_status")),
                         0 if vid in base_ids else 1, ex["id"]))
                    already += 1
                elif ex.get("yt_md_body"):
                    already += 1
                else:
                    db.execute(
                        "UPDATE records SET yt_md_body=?, yt_srt_text=?,"
                        " yt_doc_name=? WHERE id=?",
                        (body, srt_text, srt_name, ex["id"]))
                    merged += 1
            else:
                new_items.append((vid, fm, body, srt_name, srt_text))
                existing[vid] = {"origin": "yt", "yt_md_body": body}

        def sortkey(item):
            vid, fm = item[0], item[1]
            d = base_dates.get(vid) or fm.get("published_at") or "99999999"
            return (str(d), vid)

        new_items.sort(key=sortkey)
        seq = max_seq
        for vid, fm, body, srt_name, srt_text in new_items:
            seq += 1
            db.execute(
                """INSERT INTO records (seq, source_bv, video_id, title_zh,
                   title_original, source_file, published_at, source,
                   source_date, source_index, declared_words,
                   transcript_status, keywords, md_body, txt_text, has_txt,
                   match_tier, match_method, origin, base_pending)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (seq, None, vid, fm.get("title_zh") or vid,
                 beast._s(fm.get("title_original")),
                 beast._s(fm.get("source_file")),
                 beast._s(fm.get("published_at")),
                 beast._s(fm.get("source")), beast._s(fm.get("source_date")),
                 beast._s(fm.get("source_index")),
                 beast._s(fm.get("declared_words")),
                 beast._s(fm.get("transcript_status")),
                 beast.extract_keywords(body), body, srt_text,
                 1 if srt_text is not None else 0,
                 "matched", "direct", "yt",
                 0 if vid in base_ids else 1))
        db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
        report = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "total_md": len(mds), "new": len(new_items),
                  "merged": merged, "already": already,
                  "base_covered": covered, "base_pending": pending,
                  "srt_only": len(srt_only), "skipped_files": skipped,
                  "first_new_seq": (max_seq + 1) if new_items else None}
        db.execute("INSERT OR REPLACE INTO meta VALUES ('yt_batch_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                      for k, v in report.items() if k != "skipped_files"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
