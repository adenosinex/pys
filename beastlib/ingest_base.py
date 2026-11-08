#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 fetch_base.py 的 JSONL 抓取结果入 yt_base 表 (元数据底座) + 写 base_report。

盒上以 apphub 身份在项目代码目录运行:
  /srv/apphub/envs/python-shared/bin/python ingest_base.py /tmp/yt_base_fetch.jsonl
可重跑: 按 video_id upsert (重跑 = 刷新)。失败行也入表 (error 记原因)。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/yt_base_fetch.jsonl"
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    db = sqlite3.connect(beast.DB_PATH)
    db.executescript(beast.SCHEMA)
    beast._migrate(db)
    tab_map = {}
    if len(sys.argv) > 3 and os.path.exists(sys.argv[3]):
        with open(sys.argv[3], encoding="utf-8") as fh:
            tab_map = json.load(fh)
    ok = fail = tr = 0
    existing = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                  " AND name='yt_base'").fetchone():
        existing = {r[0]: r for r in db.execute(
            "SELECT video_id, title, view_count, like_count, comment_count,"
            " upload_date, duration FROM yt_base")}
    with db:
        for r in rows:
            err = None if r.get("ok") else (r.get("error") or "unknown")
            if err:
                fail += 1
            else:
                ok += 1
            if r.get("transcript_ready"):
                tr += 1
            ex = existing.get(r["video_id"])
            if ex and err is None:
                # 轻量法的 null = 未测, 不冲掉已有实测值 (如 comment_count)
                vals = [r.get("title"), r.get("view_count"),
                        r.get("like_count"), r.get("comment_count"),
                        r.get("upload_date"), r.get("duration")]
                merged = [v if v is not None else ex[i + 1]
                          for i, v in enumerate(vals)]
                (r["title"], r["view_count"], r["like_count"],
                 r["comment_count"], r["upload_date"],
                 r["duration"]) = merged
            db.execute(
                """INSERT INTO yt_base (video_id, title, view_count, like_count,
                   comment_count, upload_date, duration, fetched_at, source,
                   error, transcript_ready, tab)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(video_id) DO UPDATE SET
                   title=excluded.title, view_count=excluded.view_count,
                   like_count=excluded.like_count,
                   comment_count=excluded.comment_count,
                   upload_date=excluded.upload_date,
                   duration=excluded.duration, fetched_at=excluded.fetched_at,
                   source=excluded.source, error=excluded.error,
                   transcript_ready=excluded.transcript_ready,
                   tab=excluded.tab""",
                (r["video_id"], r.get("title"), r.get("view_count"),
                 r.get("like_count"), r.get("comment_count"),
                 r.get("upload_date"), r.get("duration"), r.get("fetched_at"),
                 "yt-dlp --dump-json", err,
                 1 if r.get("transcript_ready") else 0,
                 tab_map.get(r["video_id"])))
        # 按表内终局计数 (同一 id 的旧失败行会被后来的成功行覆盖, 不重复计)
        total_t = db.execute("SELECT COUNT(*) FROM yt_base").fetchone()[0]
        ok_t = db.execute(
            "SELECT COUNT(*) FROM yt_base WHERE error IS NULL").fetchone()[0]
        tr_t = db.execute(
            "SELECT COUNT(*) FROM yt_base WHERE transcript_ready=1"
        ).fetchone()[0]
        report = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "total": total_t, "ok": ok_t, "failed": total_t - ok_t,
                  "transcript_ready": tr_t}
        if len(sys.argv) > 2 and os.path.exists(sys.argv[2]):
            with open(sys.argv[2], encoding="utf-8") as fh:
                report["scope"] = json.load(fh)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('base_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
