#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元数据底座抓取 (手动触发, 可重跑): 对 [清单 706 ∪ 频道枚举新增] 的每个
video_id 用 yt-dlp --dump-json 抓 title/view/like/comment/upload_date/duration,
逐条追加 JSONL (断点续跑: 已 ok 的跳过; 失败默认跳过, --retry-failed 重试)。

底座范围 = MrBeast 主频道全量上传 (yt-dlp --flat-playlist 枚举 videos+shorts)
∪ 用户清单 still_to_download.csv。频道新增 id 放 --extra-ids 文件 (一行一个)。
盒上 muse 身份手动运行 (无定时):
  cd ~/beastlib-base && python3 fetch_base.py --workers 3 --extra-ids extra_ids.txt
凭证: ~/mrbeast/cookies.txt 只复制成工作副本使用 (每 worker 一份), 不读值、
不改原件。v2: 子进程独立会话 + 超时整组击杀 + 输出走临时文件, 防管道楔死。
"""
import argparse
import csv
import glob
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

YT = "/srv/apphub/code/python/yt-audio/.runtime/bin/yt-dlp"
DENO = "/srv/apphub/code/python/yt-audio/.runtime/bin/deno"
CSV_PATH = os.path.expanduser("~/mrbeast/still_to_download.csv")
AUDIO_DIR = os.path.expanduser("~/mrbeast/audio")
COOKIES_SRC = os.path.expanduser("~/mrbeast/cookies.txt")
OUT_DIR = os.path.expanduser("~/beastlib-base")
OUT_PATH = os.path.join(OUT_DIR, "yt_base_fetch.jsonl")
PER_CALL_TIMEOUT = 90


def transcript_ids():
    ids = set()
    for p in glob.glob(os.path.join(AUDIO_DIR, "*.txt")):
        stem = os.path.basename(p)[:-4]
        vid = stem.split(" - ")[0]
        if len(vid) == 11:
            ids.add(vid)
    return ids


def fetch_one(video_id, cookie_copy, ready):
    base = {"video_id": video_id, "source": "yt-dlp",
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "transcript_ready": 1 if video_id in ready else 0}
    cmd = [YT, "-j", "--skip-download", "--no-playlist", "--no-warnings",
           "--socket-timeout", "20", "--extractor-retries", "1",
           "--js-runtimes", "deno:" + DENO]
    if cookie_copy:
        cmd += ["--cookies", cookie_copy]
    cmd.append("https://www.youtube.com/watch?v=" + video_id)
    out_f = tempfile.TemporaryFile()
    err_f = tempfile.TemporaryFile()
    try:
        p = subprocess.Popen(cmd, stdout=out_f, stderr=err_f,
                             start_new_session=True)
        try:
            p.wait(timeout=PER_CALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                p.wait(timeout=10)
            except Exception:
                pass
            base.update({"ok": False, "error": "TIMEOUT"})
            return base
        out_f.seek(0)
        raw = out_f.read().decode("utf-8", "replace")
        if p.returncode != 0 or not raw.strip():
            err_f.seek(0)
            tail = err_f.read().decode("utf-8", "replace").strip().split("\n")
            base.update({"ok": False,
                         "error": (tail[-1] if tail and tail[-1]
                                   else "rc=%d" % p.returncode)[:300]})
            return base
        try:
            d = json.loads(raw)
        except Exception:
            base.update({"ok": False, "error": "PARSE_FAIL"})
            return base
        base.update({"ok": True, "title": d.get("title"),
                     "view_count": d.get("view_count"),
                     "like_count": d.get("like_count"),
                     "comment_count": d.get("comment_count"),
                     "upload_date": d.get("upload_date"),
                     "duration": d.get("duration")})
        return base
    except Exception as e:  # never let one id kill the run
        base.update({"ok": False, "error": ("EXC " + str(e))[:300]})
        return base
    finally:
        out_f.close()
        err_f.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.2)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--extra-ids", default="")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    ids = []
    seen = set()
    with open(CSV_PATH, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            v = row["video_id"]
            if v not in seen:
                seen.add(v)
                ids.append(v)
    if args.extra_ids:
        with open(args.extra_ids, encoding="utf-8") as fh:
            for line in fh:
                v = line.strip()
                if v and v not in seen:
                    seen.add(v)
                    ids.append(v)
    done_ok, done_any = set(), set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                done_any.add(d["video_id"])
                if d.get("ok"):
                    done_ok.add(d["video_id"])
    skip = done_ok if args.retry_failed else done_any
    todo = [v for v in ids if v not in skip]
    if args.limit:
        todo = todo[:args.limit]
    ready = transcript_ids()
    print("total=%d already_ok=%d todo=%d transcripts=%d"
          % (len(ids), len(done_ok), len(todo), len(ready)), flush=True)
    cookies = []
    if os.path.exists(COOKIES_SRC):
        for w in range(max(1, args.workers)):
            cp = os.path.join(OUT_DIR, "cookies_work_%d.txt" % w)
            shutil.copyfile(COOKIES_SRC, cp)
            cookies.append(cp)
        print("cookies: copies=%d" % len(cookies), flush=True)
    ok = fail = 0
    lock = __import__("threading").Lock()

    def work(idx_vid):
        idx, vid = idx_vid
        ck = cookies[idx % len(cookies)] if cookies else None
        time.sleep(random.uniform(0.8, 2.0) * (args.sleep / 1.2))
        return fetch_one(vid, ck, ready)

    with open(OUT_PATH, "a", encoding="utf-8") as out:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {ex.submit(work, (i, v)): v for i, v in enumerate(todo)}
            for fut in as_completed(futs):
                rec = fut.result()
                with lock:
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                if rec["ok"]:
                    ok += 1
                else:
                    fail += 1
                if (ok + fail) % 25 == 0:
                    print("progress %d/%d ok=%d fail=%d last=%s"
                          % (ok + fail, len(todo), ok, fail, rec["video_id"]),
                          flush=True)
    print("DONE ok=%d fail=%d" % (ok, fail), flush=True)


if __name__ == "__main__":
    main()
