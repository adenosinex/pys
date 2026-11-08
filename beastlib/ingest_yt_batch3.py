#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YouTube 字幕批 3 入库: 补差批次 — 用户基于补差包 (171 srt) 新产的 md+srt, 与待补清单精确全覆盖。
本批特点 (与第一批的差异):
- md front-matter 的 video_id 为 null (源 srt 无元数据头), 记录键从文件名取:
  文件名形如 <video_id> - <标题>.md, 首 token 即 video_id;
- 两个来源目录合并打包, 含 .workbuddy 流水线 md 2 个 (处理日志/规范), 排除;
- fm 的 published_at/source 等为 null 属正常, 保持 null 不猜填;
  统计字段 (底座标题/播放/时长) 与发布时间从 yt_base 回填。
规则 (沿用第一批先例并按用户简报适配):
- 只认 video_id 精确挂接元数据底座, 不做模糊匹配;
- video_id 已有记录: BV 记录 -> 并入第二来源 (yt_md_body/yt_srt_text);
  yt 记录且已有本批来源 -> 幂等跳过 (正文不同则跳过并列入 diff 清单, 不覆盖);
- video_id 在底座内且无记录 -> 新建 origin='yt', 序号在现最大值后连续编,
  定序: 底座 upload_date 升序, 同日按 video_id 字典序;
- video_id 不在底座内 -> 不入库, 单独列清单报告 (本批不走 base_pending);
- 无 md 的 srt 不建记录; 占位条目 (fm transcript_status=partial 且正文有
  源字幕缺陷标注) 照常入库, 在报告中单独计数并列清单;
- FTS 重建; meta 写 yt_batch3_report。
盒上以 apphub 身份在项目代码目录运行; 参数 = zip 路径 [--dry-run]。
"""
import json
import os
import re
import sqlite3
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402

VID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
DEFECT_MARKERS = ["没有可用", "无内容可确认", "正文没有任何文字",
                  "正文全部为空", "几乎为空", "字幕缺失", "仅依据文件名",
                  "字幕不可用", "无可核实"]


def defect_hit(body):
    for m in DEFECT_MARKERS:
        if m in body:
            return m
    return None


def collect(z):
    mds, srts, skipped, collisions = {}, {}, [], []
    for n in z.namelist():
        if n.endswith("/"):
            continue
        base = n.rsplit("/", 1)[-1]
        low = base.lower()
        stem = base.rsplit(".", 1)[0]
        if "/.workbuddy/" in n or n.startswith(".workbuddy/"):
            skipped.append({"file": n, "desc": "workbuddy 流水线文件, 未入库"})
            continue
        if low.endswith(".md"):
            if stem in mds:
                collisions.append(base)
                continue
            mds[stem] = (base, z.read(n).decode("utf-8", "replace"))
        elif low.endswith(".srt"):
            if stem in srts:
                collisions.append(base)
                continue
            srts[stem] = (base, z.read(n).decode("utf-8", "replace"))
        else:
            skipped.append({"file": n, "desc": "非 md/srt 文件, 未入库"})
    return mds, srts, skipped, collisions


def build_plan(db, mds, srts):
    base = {}
    for r in db.execute(
            "SELECT video_id, title, view_count, duration, upload_date,"
            " error, excluded FROM yt_base"):
        base[r["video_id"]] = dict(r)
    existing = {r["video_id"]: dict(r) for r in db.execute(
        "SELECT * FROM records WHERE video_id IS NOT NULL")}
    plan = {"new": [], "merge": [], "already": [], "diff": [],
            "not_in_base": [], "excluded_hit": [], "bad_vid": [],
            "placeholders": [], "md_no_pair": [], "partial_fm": 0}
    for stem in sorted(mds):
        fname, text = mds[stem]
        fm, body = beast.parse_front_matter(text)
        if fm.get("transcript_status") == "partial":
            plan["partial_fm"] += 1
        vid = stem.split(" - ")[0]
        if not VID_RE.match(vid):
            plan["bad_vid"].append(fname)
            continue
        if fm.get("video_id") and fm["video_id"] != vid:
            plan["diff"].append({"vid": vid, "why": "fm video_id 与文件名不一致"})
            continue
        srt_name, srt_text = srts.get(stem, (None, None))
        if srt_text is None:
            plan["md_no_pair"].append(vid)
        if fm.get("transcript_status") == "partial":
            hit = defect_hit(body)
            if hit:
                plan["placeholders"].append(
                    {"vid": vid, "marker": hit,
                     "srt_len": len(srt_text) if srt_text else 0})
        brow = base.get(vid)
        if brow is None or brow["error"] is not None:
            plan["not_in_base"].append(vid)
            continue
        if brow["excluded"]:
            plan["excluded_hit"].append(vid)
            continue
        ex = existing.get(vid)
        item = {"vid": vid, "fm": fm, "body": body, "stem": stem,
                "srt_name": srt_name, "srt_text": srt_text, "base": brow}
        if ex is None:
            plan["new"].append(item)
        elif ex["origin"] == "yt":
            if ex["md_body"] == body:
                plan["already"].append(vid)
            else:
                plan["diff"].append(
                    {"vid": vid, "why": "yt 记录已有不同正文, 未覆盖"})
        elif ex.get("yt_md_body"):
            if ex["yt_md_body"] == body:
                plan["already"].append(vid)
            else:
                plan["diff"].append(
                    {"vid": vid, "why": "BV 记录已有不同 yt 正文, 未覆盖"})
        else:
            item["ex_id"] = ex["id"]
            item["ex_seq"] = ex["seq"]
            plan["merge"].append(item)
    plan["new"].sort(key=lambda it: (str(it["base"]["upload_date"]
                                          or "99999999"), it["vid"]))
    return plan


def summarize(plan, mds, srts, skipped, collisions):
    srt_only = sorted(set(srts) - set(mds))
    return {
        "total_md": len(mds), "total_srt": len(srts),
        "new": len(plan["new"]), "merge": len(plan["merge"]),
        "already": len(plan["already"]), "diff": plan["diff"],
        "not_in_base": plan["not_in_base"],
        "excluded_hit": plan["excluded_hit"], "bad_vid": plan["bad_vid"],
        "placeholders": plan["placeholders"],
        "md_no_pair": plan["md_no_pair"], "srt_only": len(srt_only),
        "skipped_files": skipped, "collisions": collisions,
        "merge_seqs": [it["ex_seq"] for it in plan["merge"]],
        "partial_fm": plan["partial_fm"],
        "new_first": (plan["new"][0]["vid"] if plan["new"] else None),
        "new_last": (plan["new"][-1]["vid"] if plan["new"] else None),
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    zpath = args[0] if args else "/tmp/batch3.zip"
    z = zipfile.ZipFile(zpath)
    mds, srts, skipped, collisions = collect(z)
    if dry:
        db = sqlite3.connect("file:%s?mode=ro" % beast.DB_PATH, uri=True)
    else:
        db = sqlite3.connect(beast.DB_PATH)
    db.row_factory = sqlite3.Row
    if not dry:
        db.executescript(beast.SCHEMA)
        beast._migrate(db)
    plan = build_plan(db, mds, srts)
    summary = summarize(plan, mds, srts, skipped, collisions)
    if dry:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        db.close()
        return
    max_seq = db.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM records").fetchone()[0]
    seq = max_seq
    new_seqs = []
    with db:
        for it in plan["merge"]:
            db.execute(
                "UPDATE records SET yt_md_body=?, yt_srt_text=?,"
                " yt_doc_name=? WHERE id=?",
                (it["body"], it["srt_text"], it["srt_name"], it["ex_id"]))
        for it in plan["new"]:
            seq += 1
            new_seqs.append(seq)
            fm, brow = it["fm"], it["base"]
            db.execute(
                """INSERT INTO records (seq, source_bv, video_id, title_zh,
                   title_original, source_file, published_at, source,
                   source_date, source_index, declared_words,
                   transcript_status, keywords, md_body, txt_text, has_txt,
                   match_tier, match_method, origin, base_pending,
                   yt_title, yt_view_count, yt_duration_sec)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (seq, None, it["vid"], fm.get("title_zh") or it["vid"],
                 beast._s(fm.get("title_original")),
                 beast._s(fm.get("source_file")),
                 beast._s(brow["upload_date"]),
                 beast._s(fm.get("source")), beast._s(fm.get("source_date")),
                 beast._s(fm.get("source_index")),
                 beast._s(fm.get("declared_words")),
                 beast._s(fm.get("transcript_status")),
                 beast.extract_keywords(it["body"]), it["body"],
                 it["srt_text"],
                 1 if it["srt_text"] is not None else 0,
                 "matched", "direct", "yt", 0,
                 brow["title"],
                 None if brow["view_count"] is None
                 else str(brow["view_count"]),
                 None if brow["duration"] is None
                 else str(brow["duration"])))
        db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
        report = dict(summary)
        report["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        report["first_new_seq"] = (max_seq + 1) if plan["new"] else None
        report["last_new_seq"] = seq if plan["new"] else None
        report["new_seqs"] = new_seqs
        db.execute("INSERT OR REPLACE INTO meta VALUES ('yt_batch3_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                      for k, v in report.items()
                      if k not in ("skipped_files", "new_seqs")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
