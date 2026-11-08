#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元数据底座轻量抓取 (手动触发, 可重跑): 串行 curl 取 watch 页
ytInitialPlayerResponse, 解析 microformat + videoDetails。
字段: title / view_count / like_count (精确, 必达) + upload_date / duration
(顺带); comment_count 页面 initial data 无可靠字段 -> 恒为 null 并在运行
摘要里明示 (不拿别的数据凑)。播放器/formats 完全不碰, CPU 近零。
与 fetch_base.py 共用同一 JSONL (已 ok 的跳过, 全量法记录是超集不重抓)。
盒上 muse 身份手动运行:
  cd ~/beastlib-base && python3 fetch_light.py --extra-ids extra_ids.txt
"""
import argparse
import csv
import glob
import json
import os
import random
import shutil
import subprocess
import sys
import time

CSV_PATH = os.path.expanduser("~/mrbeast/still_to_download.csv")
AUDIO_DIR = os.path.expanduser("~/mrbeast/audio")
COOKIES_SRC = os.path.expanduser("~/mrbeast/cookies.txt")
OUT_DIR = os.path.expanduser("~/beastlib-base")
OUT_PATH = os.path.join(OUT_DIR, "yt_base_fetch.jsonl")
COOKIE_COPY = os.path.join(OUT_DIR, "cookies_light.txt")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def transcript_ids():
    ids = set()
    for p in glob.glob(os.path.join(AUDIO_DIR, "*.txt")):
        vid = os.path.basename(p)[:-4].split(" - ")[0]
        if len(vid) == 11:
            ids.add(vid)
    return ids


def extract_player_response(html):
    """Locate ytInitialPlayerResponse JSON (string-aware brace scan)."""
    key = "ytInitialPlayerResponse"
    i = html.find(key)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(j, len(html)):
        c = html[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[j:k + 1])
                except Exception:
                    return None
    return None


def fetch_one(video_id, ready):
    rec = {"video_id": video_id, "source": "watch-page-initial-data",
           "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "transcript_ready": 1 if video_id in ready else 0}
    url = "https://www.youtube.com/watch?v=" + video_id
    try:
        p = subprocess.run(
            ["curl", "-s", "--max-time", "30", "-A", UA,
             "-H", "Accept-Language: en-US",
             "--cookie", COOKIE_COPY, url],
            capture_output=True, timeout=40)
        html = p.stdout.decode("utf-8", "replace")
    except Exception as e:
        rec.update({"ok": False, "error": ("CURL " + str(e))[:200]})
        return rec
    d = extract_player_response(html)
    if not d:
        rec.update({"ok": False, "error": "NO_PLAYER_RESPONSE"})
        return rec
    status = (d.get("playabilityStatus") or {}).get("status")
    vd = d.get("videoDetails") or {}
    mf = ((d.get("microformat") or {}).get("playerMicroformatRenderer") or {})

    def as_int(v):
        try:
            return int(str(v).replace(",", ""))
        except Exception:
            return None

    title = vd.get("title") or (mf.get("title") or {}).get("simpleText")
    views = as_int(vd.get("viewCount") or mf.get("viewCount"))
    likes = as_int(mf.get("likeCount"))
    dur = as_int(vd.get("lengthSeconds") or mf.get("lengthSeconds"))
    upload = mf.get("uploadDate") or mf.get("publishDate") or ""
    upload = upload[:10].replace("-", "") if upload else None
    if status != "OK":
        rec.update({"ok": False,
                    "error": ("PLAYABILITY_" + str(status))[:200]})
        return rec
    if not title or views is None or likes is None:
        rec.update({"ok": False, "error": "MISSING_MUST_FIELDS"})
        return rec
    rec.update({"ok": True, "title": title, "view_count": views,
                "like_count": likes, "comment_count": None,
                "upload_date": upload, "duration": dur})
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--extra-ids", default="")
    ap.add_argument("--ids", default="")  # explicit comma list (tests)
    ap.add_argument("--force", action="store_true",
                    help="test only: ignore already-ok skip")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    if not os.path.exists(COOKIE_COPY):
        shutil.copyfile(COOKIES_SRC, COOKIE_COPY)
    ids, seen = [], set()
    if args.ids:
        for v in args.ids.split(","):
            v = v.strip()
            if v:
                seen.add(v)
                ids.append(v)
    else:
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
    done_ok = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("ok"):
                    done_ok.add(d["video_id"])
    todo = ids if args.force else [v for v in ids if v not in done_ok]
    if args.limit:
        todo = todo[:args.limit]
    ready = transcript_ids()
    print("total=%d already_ok=%d todo=%d transcripts=%d"
          % (len(ids), len(done_ok), len(todo), len(ready)), flush=True)
    ok = fail = 0
    with open(OUT_PATH, "a", encoding="utf-8") as out:
        for i, vid in enumerate(todo):
            rec = fetch_one(vid, ready)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            if rec["ok"]:
                ok += 1
            else:
                fail += 1
            if (i + 1) % 25 == 0:
                print("progress %d/%d ok=%d fail=%d last=%s"
                      % (i + 1, len(todo), ok, fail, vid), flush=True)
            time.sleep(random.uniform(0.7, 1.3) * args.sleep)
    print("DONE ok=%d fail=%d (comment_count: light method has no reliable"
          " field -> null)" % (ok, fail), flush=True)


if __name__ == "__main__":
    main()
