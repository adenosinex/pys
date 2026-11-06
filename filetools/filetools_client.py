#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件整理台客户端 (filetools client) — stdlib only.

Flow (server-driven, paths first):
  1. Scan a folder, upload ONLY relative paths (no stat — fast on big
     trees).
  2. Wait while the user picks a function + parameters on the web page.
  3. If that function declares it needs extra fields (size / mtime), the
     server asks for exactly those; this client stats just them and
     uploads them.
  4. The server returns an operation list (from -> to, relative). This
     client previews it, executes the moves locally (never overwrites:
     " (2)", " (3)" suffixes), writes an undo log, and reports back.

Usage:
  python3 filetools_client.py                    # wizard: asks folder only
  python3 filetools_client.py --path DIR         # same, non-interactive
  python3 filetools_client.py --path DIR --yes   # execute without prompt
  python3 filetools_client.py --path DIR --dry-run
  python3 filetools_client.py --undo LOG.json    # roll back a run
  python3 filetools_client.py --version

Config: filetools_client.json next to this script, or env vars
FILETOOLS_TOKEN / FILETOOLS_SERVER / FILETOOLS_API (env wins).
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

CLIENT_VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "filetools_client.json")
DEFAULT_SERVER = "https://debi.su7.dpdns.org/web/filetools/"
API_PREFIX = "/ft-api"
TOKEN_HEADER = "X-Filetools-Token"
LOG_PREFIX = "filetools-log-"
POLL_TIMEOUT = 15 * 60


# ---------------- config ------------------------------------------------------

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception:
            cfg = {}
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("警告: 配置保存失败:", e)


def derive_api(server):
    p = urllib.parse.urlsplit(server)
    return "%s://%s%s" % (p.scheme, p.netloc, API_PREFIX)


def resolve(cli_token, cli_server):
    cfg = load_config()
    token = cli_token or os.environ.get("FILETOOLS_TOKEN") \
        or cfg.get("token") or ""
    server = cli_server or os.environ.get("FILETOOLS_SERVER") \
        or cfg.get("server") or DEFAULT_SERVER
    api = os.environ.get("FILETOOLS_API") or cfg.get("api") \
        or derive_api(server)
    if not os.path.exists(CONFIG_PATH):
        save_config({"server": server, "token": token, "api": ""})
    return cfg, token, server, api


# ---------------- http ---------------------------------------------------------

def http(api, method, path, token, payload=None, timeout=30):
    url = api.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header(TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"ok": False, "error": str(e)}


def must(status, obj, what):
    if status != 200 or not obj.get("ok"):
        raise RuntimeError("%s失败: %s" % (what, obj.get("error") or status))
    return obj


# ---------------- scan / fields -------------------------------------------------

def scan_paths(root):
    """Relative paths of all files under root. Names only — no stat."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.startswith(LOG_PREFIX) and fn.endswith(".json"):
                continue  # never move our own undo logs
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            out.append(rel.replace(os.sep, "/"))
    out.sort()
    return out


def collect_fields(root, paths, needs):
    """Stat only the requested fields. Returns {path: {size/mtime}}."""
    out = {}
    for p in paths:
        full = os.path.join(root, *p.split("/"))
        try:
            st = os.stat(full)
        except OSError:
            continue
        vals = {}
        if "size" in needs:
            vals["size"] = st.st_size
        if "mtime" in needs:
            vals["mtime"] = int(st.st_mtime)
        if vals:
            out[p] = vals
    return out


# ---------------- execute --------------------------------------------------------

def _suffixed(path):
    """a/b.txt -> a/b (2).txt, a/b (3).txt ... first free variant."""
    if not os.path.exists(path):
        return path
    d, b = os.path.split(path)
    if "." in b[1:]:
        stem, ext = b.rsplit(".", 1)
        ext = "." + ext
    else:
        stem, ext = b, ""
    n = 2
    while True:
        cand = os.path.join(d, "%s (%d)%s" % (stem, n, ext))
        if not os.path.exists(cand):
            return cand
        n += 1


def execute_plan(root, plan):
    done, skipped, errors = [], 0, 0
    for op in plan:
        src = os.path.join(root, *op["from"].split("/"))
        dst = os.path.join(root, *op["to"].split("/"))
        if not os.path.isfile(src):
            skipped += 1
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            final = _suffixed(dst)
            shutil.move(src, final)
            done.append({"from": op["from"],
                         "to": os.path.relpath(final, root)
                         .replace(os.sep, "/")})
        except Exception as e:
            errors += 1
            print("  失败:", op["from"], "->", op["to"], e)
    return done, skipped, errors


def do_undo(logfile):
    with open(logfile, encoding="utf-8") as f:
        log = json.load(f)
    root = log["root"]
    n = 0
    for op in reversed(log["ops"]):
        cur = os.path.join(root, *op["to"].split("/"))
        back = os.path.join(root, *op["from"].split("/"))
        if not os.path.exists(cur):
            print("  跳过(不存在):", op["to"])
            continue
        os.makedirs(os.path.dirname(back), exist_ok=True)
        final = _suffixed(back)
        shutil.move(cur, final)
        n += 1
    print("已回滚 %d 个文件。" % n)


# ---------------- main flow -------------------------------------------------------

def run(root, token, api, yes=False, dry_run=False, interval=3):
    root = os.path.abspath(root)
    print("扫描中(只读路径)…")
    paths = scan_paths(root)
    print("共 %d 个文件。上传路径列表…" % len(paths))
    st, obj = http(api, "POST", "/api/client/batches", token,
                   {"name": os.path.basename(root) or root, "paths": paths})
    must(st, obj, "创建批次")
    bid = obj["batch_id"]
    print("批次 #%d 已创建。等待在网页上选择功能…(网页: 我的 App Hub / 文件整理台)" % bid)
    deadline = time.time() + POLL_TIMEOUT
    fields_sent = False
    plan = None
    while time.time() < deadline:
        st, obj = http(api, "GET", "/api/client/batches/%d" % bid, token)
        if st == 200 and obj.get("ok"):
            status = obj["status"]
            if status == "fields" and not fields_sent:
                needs = obj.get("needs") or []
                print("所选功能需要字段 %s，补读中…" % needs)
                fields = collect_fields(root, paths, needs)
                keys = list(fields.items())
                for i in range(0, len(keys), 2000):
                    chunk = dict(keys[i:i + 2000])
                    st2, obj2 = http(api, "POST",
                                     "/api/client/batches/%d/fields" % bid,
                                     token, {"fields": chunk}, timeout=60)
                    must(st2, obj2, "上传字段")
                fields_sent = True
                print("字段已上传，等待生成清单…")
            elif status == "ready":
                plan = obj.get("plan") or []
                break
            elif status == "done":
                print("该批次已执行过。")
                return 0
        time.sleep(interval)
    if plan is None:
        print("等待超时(15 分钟)，批次 #%d 仍在服务器上，可稍后重试。" % bid)
        return 1
    print("收到操作清单: %d 条。" % len(plan))
    for op in plan[:20]:
        print("  %s -> %s" % (op["from"], op["to"]))
    if len(plan) > 20:
        print("  …(其余 %d 条略)" % (len(plan) - 20))
    if dry_run:
        print("dry-run 模式，未执行。")
        return 0
    if not yes:
        ans = input("执行以上移动？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消(清单仍在服务器上)。")
            return 0
    done, skipped, errors = execute_plan(root, plan)
    log = {"batch": bid, "root": root, "time": time.time(), "ops": done}
    logfile = os.path.join(root, "%s%d.json" % (LOG_PREFIX, bid))
    with open(logfile, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print("完成: 移动 %d，跳过 %d，失败 %d。回滚日志: %s"
          % (len(done), skipped, errors, logfile))
    st, obj = http(api, "POST", "/api/client/batches/%d/report" % bid, token,
                   {"moved": len(done), "skipped": skipped, "errors": errors})
    if st != 200:
        print("提示: 结果回传失败(不影响已执行的操作)。")
    return 0


def wizard(cli_token=None, cli_server=None):
    print("=== 文件整理台客户端 v%s ===" % CLIENT_VERSION)
    cfg, token, server, api = resolve(cli_token, cli_server)
    if os.environ.get("FILETOOLS_TOKEN"):
        print("令牌来源: 环境变量 FILETOOLS_TOKEN")
    elif cfg.get("token"):
        print("令牌来源: 配置文件")
    if not token:
        token = input("请输入客户端令牌(网页设置里保存的那个): ").strip()
        if not token:
            print("没有令牌无法继续。")
            return 1
        cfg["token"] = token
        if not cfg.get("server"):
            cfg["server"] = server
        save_config(cfg)
        print("令牌已保存到配置文件，下次不用再输。")
    root = input("把文件夹拖到这里(或输入路径)，回车开始: ").strip().strip('"').strip("'")
    if not root or not os.path.isdir(root):
        print("路径无效:", root)
        return 1
    return run(root, token, api, yes=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="文件整理台客户端")
    ap.add_argument("path", nargs="?", help="目标文件夹(也可拖放到脚本图标上)")
    ap.add_argument("--path", dest="path_opt")
    ap.add_argument("--token")
    ap.add_argument("--server")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--undo", metavar="LOGFILE")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)
    if args.version:
        print(CLIENT_VERSION)
        return 0
    if args.undo:
        do_undo(args.undo)
        return 0
    root = args.path_opt or args.path
    interactive = root is None
    try:
        if interactive:
            return wizard(args.token, args.server)
        cfg, token, server, api = resolve(args.token, args.server)
        if not token:
            print("缺少令牌: 用 --token、环境变量 FILETOOLS_TOKEN 或配置文件提供。")
            return 1
        # --path keeps the preview->confirm step unless --yes; a positional
        # path (drag onto the icon / wizard) auto-executes after the
        # preview — the undo log is the safety net.
        auto = args.yes if args.path_opt else True
        return run(root, token, api, yes=auto, dry_run=args.dry_run)
    finally:
        if interactive or args.path:
            try:
                input("按回车键退出…")
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
