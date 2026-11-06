# -*- coding: utf-8 -*-
"""nameclean_client - 文件名清洗客户端（执行层）

哑巴执行器：不保存规则、不做判断。流程：
  扫描目录 -> 把文件名清单上传到「文件名清洗台」(App Hub) -> 等待你在
  网页上审查新词块 -> 拉取「涉及本批」的规则子集 -> 本地预览 -> 确认改名。
核心规则库在服务端先行过滤：已判定过的词块不会再让你审第二遍。

用法：
  直接双击运行（或无参数运行）进入交互模式：按提示输入令牌、把文件夹
  拖进来即可，全程有提示，结束会停住等你看完再关。
  也可以把文件夹直接拖到本脚本图标上启动。
  命令行：python3 nameclean_client.py --path /待清洗目录
  python3 nameclean_client.py --path /目录 --recursive      # 含子目录
  python3 nameclean_client.py --path /目录 --dry-run        # 只预览不改名
  python3 nameclean_client.py --undo nameclean-log-3.json   # 按日志回滚
  python3 nameclean_client.py --path /目录 --export-names names.txt  # 只导出清单
  python3 nameclean_client.py --path /目录 --rules rules.txt         # 手动规则模式

配置：脚本同目录 nameclean_client.json
  {"server": "https://debi.su7.dpdns.org/web/nameclean", "token": "你在网页设置里填的令牌"}
也可以用环境变量（优先于配置文件）：NAMECLEAN_TOKEN、NAMECLEAN_SERVER。
客户端可从清洗台网页右上角「下载客户端」直接下载最新版。
分段/套用算法与服务端保持一致（SEG_VERSION = 1）。
"""
CLIENT_VERSION = "1.2.0"
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

SEG_VERSION = 1
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts",
              ".rmvb", ".rm", ".webm", ".mpg", ".mpeg", ".m4v", ".3gp"}

# ------- segmentation (shared spec with the server, SEG_VERSION = 1) ------
SEP_CHARS = set("-_ \t|~·•・*+&@#%=,，;；'\"")
BRACKET_CHARS = set("[]()（）《》〈〉{}<>「」『』") | {chr(12304), chr(12305)}
DOMAIN_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9\-]{1,30}\."
    r"(?:com|net|org|cc|xyz|top|vip|cn|io|me|tv|co|info|site|club|online)"
    r"(?:\.[a-z]{2,3})?")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
EPISODE_RE = re.compile(
    r"(?i)^(s\d{1,2}e\d{1,3}|e\d{1,3}|ep\d{1,3}|cd\d|dvd\d|"
    r"第[0-9一二三四五六七八九十百零〇两]+[集期话話回讲講])$")
DIGITS_RE = re.compile(r"^\d+$")


def split_ext(name):
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        if stem and 0 < len(ext) <= 5 and ext.isalnum():
            return stem, "." + ext
    return name, ""


def segments(name):
    stem, _ext = split_ext(name)
    stem = DOMAIN_RE.sub("\x00", stem)
    out, cur = [], []
    for ch in stem:
        if ch in SEP_CHARS or ch in BRACKET_CHARS or ch == "." or ch == "\x00":
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [t.strip() for t in out if t.strip()]


def is_protected(token):
    return bool(YEAR_RE.match(token) or EPISODE_RE.match(token)
                or DIGITS_RE.match(token))


# ---------------- apply engine ---------------------------------------------

def apply_rules(filename, rules):
    """Return (new_name, hits). hits: pattern -> count for this file."""
    keep = set(rules.get("keep") or [])
    blacklist = set(rules.get("blacklist") or [])
    replace = dict(rules.get("replace") or [])
    substrs = list(rules.get("substr") or [])
    _stem, ext = split_ext(filename)
    hits = {}
    out = []
    for tok in segments(filename):
        if tok in keep:
            out.append(tok)
            continue
        if tok in replace:
            hits[tok] = hits.get(tok, 0) + 1
            if replace[tok]:
                out.append(replace[tok])
            continue
        if tok in blacklist and not is_protected(tok):
            hits[tok] = hits.get(tok, 0) + 1
            continue
        t = tok
        for s in substrs:
            if s and s in t:
                hits[s] = hits.get(s, 0) + 1
                t = t.replace(s, "")
        t = t.strip()
        if t:
            out.append(t)
        elif tok:
            hits[tok] = hits.get(tok, 0) + 1
    if not out:
        return filename, {}
    new = ".".join(out) + ext
    if len(new) - len(ext) < 2 or new == filename:
        return filename, {}
    return new, hits


def parse_rules_text(text):
    rules = {"blacklist": [], "replace": [], "keep": [], "substr": []}
    section = None
    mapping = {"# 黑名单": "blacklist", "# 替换": "replace",
               "# 保留": "keep", "# 子串删除": "substr"}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in mapping:
            section = mapping[line]
            continue
        if line.startswith("#"):
            section = None
            continue
        if section == "replace":
            if "=>" in line:
                a, b = line.split("=>", 1)
                rules["replace"].append([a.strip(), b.strip()])
        elif section:
            rules[section].append(line)
    return rules


# ---------------- http ------------------------------------------------------

class Client:
    def __init__(self, server, token):
        self.server = server.rstrip("/")
        self.token = token

    def call(self, method, path, payload=None):
        url = self.server + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Clean-Token", self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8"))
            except Exception:
                detail = {}
            raise RuntimeError(detail.get("error") or ("HTTP %d" % e.code))
        except urllib.error.URLError as e:
            raise RuntimeError("连不上服务器：%s" % e.reason)


# ---------------- scan / rename ----------------------------------------------

def scan(path, recursive, all_files):
    found = []
    if recursive:
        for root, _dirs, files in os.walk(path):
            for f in files:
                found.append(os.path.join(root, f))
    else:
        for e in os.scandir(path):
            if e.is_file():
                found.append(e.path)
    out = []
    for p in found:
        if all_files or os.path.splitext(p)[1].lower() in VIDEO_EXTS:
            out.append(p)
    return sorted(out)


def plan_renames(paths, rules):
    """Return (plan, total_hits). plan: [(path, new_path)] only for changes."""
    plan = []
    total_hits = {}
    taken = set()
    for p in paths:
        name = os.path.basename(p)
        new_name, hits = apply_rules(name, rules)
        for k, v in hits.items():
            total_hits[k] = total_hits.get(k, 0) + v
        if new_name == name:
            continue
        target = os.path.join(os.path.dirname(p), new_name)
        stem, ext = split_ext(new_name)
        n = 2
        cand = target
        while cand in taken or (os.path.exists(cand) and cand != p):
            cand = os.path.join(os.path.dirname(p), "%s (%d)%s" % (stem, n, ext))
            n += 1
        taken.add(cand)
        plan.append((p, cand))
    return plan, total_hits


def do_undo(log_path):
    with open(log_path, encoding="utf-8") as f:
        log = json.load(f)
    ok = miss = 0
    for item in reversed(log.get("renames") or []):
        src = item["new"]
        dst = item["old"]
        if os.path.exists(src) and not os.path.exists(dst):
            os.rename(src, dst)
            ok += 1
        else:
            miss += 1
            print("跳过（文件不存在或原名已被占用）：", src)
    print("回滚完成：恢复 %d 个，跳过 %d 个" % (ok, miss))
    return 0


# ---------------- main ---------------------------------------------------------

def load_config(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def _ask(prompt, default=""):
    try:
        v = input(prompt).strip().strip('"').strip("'")
    except EOFError:
        v = ""
    return v or default


def interactive_main():
    """Double-click / no-args mode: wizard prompts, then the normal flow."""
    print("=" * 52)
    print(" 文件名清洗客户端 v%s（交互模式）" % CLIENT_VERSION)
    print(" 扫描目录 → 上传清单 → 你在网页上审查 → 自动改名")
    print("=" * 52)
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "nameclean_client.json")
    cfg = load_config(cfg_path) or {}
    server = (os.environ.get("NAMECLEAN_SERVER") or cfg.get("server")
              or "https://debi.su7.dpdns.org/web/nameclean")
    token = os.environ.get("NAMECLEAN_TOKEN") or cfg.get("token") or ""
    changed = False
    if not token:
        token = _ask("请输入清洗台令牌（网页「设置」页里保存的那个）: ")
        if not token:
            print("没有令牌无法连接，退出。")
            return 2
        cfg["token"] = token
        changed = True
    s2 = _ask("服务器地址（直接回车 = %s）: " % server, server)
    if s2 != server:
        server = s2
        cfg["server"] = server
        changed = True
    elif not cfg.get("server"):
        cfg["server"] = server
        changed = True
    if changed:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("配置已保存，下次双击不用再输：", cfg_path)
    path = _ask("把要清洗的文件夹拖到这里，或输入路径后回车: ")
    if not path or not os.path.isdir(path):
        print("路径无效：", path)
        return 2
    argv = ["--path", path, "--server", server, "--token", token]
    if _ask("包含子文件夹？[y/N] ").lower() == "y":
        argv.append("--recursive")
    print()
    return main(argv)


def main(argv=None):
    ap = argparse.ArgumentParser(description="文件名清洗客户端")
    ap.add_argument("path_arg", nargs="?", help="目标目录（可把文件夹拖到脚本图标上）")
    ap.add_argument("--path", help="要扫描的目录")
    ap.add_argument("--server", help="清洗台地址（覆盖配置）")
    ap.add_argument("--token", help="客户端令牌（覆盖配置）")
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "nameclean_client.json"))
    ap.add_argument("--recursive", action="store_true", help="递归子目录")
    ap.add_argument("--all", action="store_true", help="处理所有文件（默认只视频）")
    ap.add_argument("--yes", action="store_true", help="预览后不询问直接改名")
    ap.add_argument("--dry-run", action="store_true", help="只预览不改名")
    ap.add_argument("--interval", type=int, default=5, help="轮询间隔秒")
    ap.add_argument("--export-names", help="只导出文件名清单到该文件")
    ap.add_argument("--rules", help="手动模式：用本地规则文本文件执行")
    ap.add_argument("--undo", help="按改名日志回滚")
    ap.add_argument("--version", action="version",
                    version="nameclean_client " + CLIENT_VERSION)
    args = ap.parse_args(argv)

    if args.undo:
        return do_undo(args.undo)
    if not args.path and args.path_arg:
        args.path = args.path_arg
    if not args.path:
        ap.error("需要 --path")
    if not os.path.isdir(args.path):
        print("目录不存在：", args.path)
        return 2

    paths = scan(args.path, args.recursive, args.all)
    print("扫描到 %d 个文件" % len(paths))
    if not paths:
        return 0
    names = [os.path.basename(p) for p in paths]

    if args.export_names:
        with open(args.export_names, "w", encoding="utf-8") as f:
            f.write("\n".join(names) + "\n")
        print("文件名清单已写到", args.export_names)
        return 0

    if args.rules:
        with open(args.rules, encoding="utf-8") as f:
            rules = parse_rules_text(f.read())
        print("手动规则：黑名单 %d · 替换 %d · 保留 %d · 子串 %d" % (
            len(rules["blacklist"]), len(rules["replace"]),
            len(rules["keep"]), len(rules["substr"])))
        return execute(args, paths, rules, batch_id=None, client=None)

    cfg = load_config(args.config) or {}
    server = args.server or os.environ.get("NAMECLEAN_SERVER") or cfg.get("server") or ""
    token = args.token or os.environ.get("NAMECLEAN_TOKEN") or cfg.get("token") or ""
    if not server or not token:
        if not os.path.exists(args.config):
            with open(args.config, "w", encoding="utf-8") as f:
                json.dump({"server": "https://debi.su7.dpdns.org/web/nameclean",
                           "token": ""}, f, ensure_ascii=False, indent=2)
        print("先在配置里填 server 和 token：", args.config)
        print("token 是你在「文件名清洗台」设置页里保存的那个令牌；"
              "也可以用环境变量 NAMECLEAN_TOKEN 提供。")
        return 2

    client = Client(server, token)
    print("上传文件名清单到清洗台…")
    try:
        r = client.call("POST", "/api/client/batches",
                        {"name": os.path.basename(os.path.abspath(args.path)),
                         "filenames": names})
    except RuntimeError as e:
        print("上传失败：", e)
        return 1
    batch_id = r["batch_id"]
    print("批次 #%d 已建立：%d 个文件，%d 个候选词块" % (
        batch_id, r["file_count"], r["candidate_count"]))
    if r["status"] != "confirmed":
        print("请到网页审查待确认关键词：%s/#batch=%d" % (server, batch_id))
        print("（核心库已判定过的词块不会再出现，只审新词。审完点「完成审查 · 入库」）")
        last_pending = None
        try:
            while True:
                time.sleep(args.interval)
                st = client.call("GET", "/api/client/batches/%d" % batch_id)
                if st["status"] == "confirmed":
                    break
                if st["pending"] != last_pending:
                    last_pending = st["pending"]
                    print("  待确认：%d 个…" % last_pending)
        except KeyboardInterrupt:
            print("\n已停止等待。批次 #%d 还在服务器上，审完后可用 --rules 手动模式，"
                  "或重新运行（会新建批次，核心库会过滤已审词块）。" % batch_id)
            return 130
    print("批次已确认，拉取本批涉及的规则…")
    rules = client.call("GET", "/api/client/batches/%d/rules" % batch_id)
    print("本批规则：黑名单 %d · 替换 %d · 保留 %d · 子串 %d" % (
        len(rules["blacklist"]), len(rules["replace"]),
        len(rules["keep"]), len(rules["substr"])))
    return execute(args, paths, rules, batch_id=batch_id, client=client)


def execute(args, paths, rules, batch_id, client):
    plan, total_hits = plan_renames(paths, rules)
    if not plan:
        print("没有文件名需要修改。")
        return 0
    print("\n将改名 %d 个：" % len(plan))
    for old, new in plan:
        print("  %s\n -> %s" % (os.path.basename(old), os.path.basename(new)))
    if args.dry_run:
        print("（dry-run，未执行）")
        return 0
    if not args.yes:
        ans = input("\n确认改名？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消，没有改动任何文件。")
            return 0
    done = []
    for old, new in plan:
        os.rename(old, new)
        done.append({"old": old, "new": new})
    log = {"batch_id": batch_id, "ts": time.time(), "dir": args.path,
           "renames": done}
    log_name = "nameclean-log-%s.json" % (batch_id if batch_id else "manual")
    log_path = os.path.join(args.path, log_name)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print("已改名 %d 个。日志：%s（回滚： --undo %s）" % (len(done), log_path, log_name))
    if client is not None and batch_id:
        try:
            client.call("POST", "/api/client/batches/%d/report" % batch_id,
                        {"hits": total_hits})
            print("命中统计已回传。")
        except RuntimeError:
            pass
    return 0


def _pause():
    try:
        input("\n按回车键退出…")
    except EOFError:
        pass


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # double-click / bare launch: interactive wizard, never flash-close
        try:
            _code = interactive_main()
        except Exception as _e:
            print("出错了：", _e)
            _code = 1
        _pause()
        sys.exit(_code)
    _code = main()
    if len(sys.argv) == 2 and os.path.isdir(sys.argv[1]):
        _pause()  # launched by dragging a folder onto the script icon
    sys.exit(_code)
