#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资源库批量上传脚本。

用法：
    python3 vidlib_upload.py [路径] [--tags 标签] [--source 来源]
    [--library-id 库ID] [--server 地址] [--token 令牌] [--dry-run]

不带路径时，运行后提示输入一个文件或文件夹路径；文件夹会递归
扫描其中的视频并逐个上传。第一次运行会问服务器地址和上传令牌
（令牌在资源库「管理 → 视频」页的「批量上传令牌」里），之后自动
记住，存在脚本旁边的 vidlib_upload_config.json 里。

只用 Python 标准库，不用装任何依赖。断点续传：中途退出（Ctrl+C）
再跑，会接着传，不重传已完成的分片；已存在的文件秒传跳过。
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

CHUNK = 8 * 1024 * 1024
FULL_HASH = 32 * 1024 * 1024
SAMPLE = 1024 * 1024
PARALLEL_CHUNKS = 3
CHUNK_TRIES = 3
CAPACITY_RETRIES = 5
CAPACITY_WAIT = 60

VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "m4v", "avi", "ts", "flv",
              "wmv", "mpg", "mpeg", "3gp", "rmvb"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "vidlib_upload_config.json")
STATE_FILE = os.path.join(SCRIPT_DIR, "vidlib_upload_state.json")

DEFAULT_SERVER = "https://debi.su7.dpdns.org"


class UploadError(Exception):
    pass


class SessionLost(UploadError):
    pass


class CapacityError(UploadError):
    pass


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % int(n)
        n /= 1024.0
    return "%d B" % int(n)


def file_digest(path):
    """与服务器 _file_digest / 网页 digestOf 完全一致的去重键。

    32MB 以内整文件 SHA-256；更大时取 SHA-256(大小十进制 +
    首 1MB + 中间 1MB + 末尾 1MB)，与服务器存的键一致，
    已存在的文件才能秒传命中。
    """
    size = os.path.getsize(path)
    if size <= FULL_HASH:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                block = fh.read(CHUNK)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        for off in (0, (size - SAMPLE) // 2, size - SAMPLE):
            fh.seek(off)
            h.update(fh.read(SAMPLE))
    return h.hexdigest()


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json_atomic(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception as exc:
        print("警告：写 %s 失败：%s" % (path, exc))


class Client:
    def __init__(self, server, token):
        self.base = server.rstrip("/") + "/vidlib-api/up"
        self.token = token

    def _request(self, method, url, data=None, timeout=30,
                 content_type=None):
        headers = {"X-Upload-Token": self.token,
                   "User-Agent": "vidlib-upload-script/1.0"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            msg = ""
            try:
                msg = json.loads(body).get("message") or ""
            except Exception:
                pass
            if exc.code == 404:
                raise UploadError(
                    "服务器拒绝（404）：上传令牌不对，或服务器尚未开通"
                    "脚本上传。请核对令牌。")
            raise UploadError("HTTP %d：%s" % (exc.code, msg or body[:200]))
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            text = str(reason)
            if "CERTIFICATE_VERIFY_FAILED" in text:
                raise UploadError(
                    "TLS 证书验证失败。macOS 上请先运行 Python 自带的 "
                    "Install Certificates.command，再重试。")
            raise UploadError("网络错误：%s" % text)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise UploadError("服务器返回异常：%s" % raw[:100])

    def post_json(self, path, payload, timeout=30):
        return self._request(
            "POST", self.base + path,
            data=json.dumps(payload).encode("utf-8"), timeout=timeout,
            content_type="application/json")

    def check(self, digest):
        return self.post_json("/check", {"digest": digest})

    def register(self, digest, name, tags, source, relpath, library_id):
        payload = {"digest": digest, "name": name, "tags": tags,
                   "source": source, "relpath": relpath}
        if library_id is not None:
            payload["library_id"] = library_id
        return self.post_json("/register", payload)

    def init_upload(self, name, size, title, tags, source, relpath,
                    library_id):
        payload = {"name": name, "size": size, "title": title,
                   "tags": tags, "source": source, "relpath": relpath}
        if library_id is not None:
            payload["library_id"] = library_id
        return self.post_json("/init", payload)

    def put_chunk(self, uid, offset, data):
        url = "%s/%s/chunk?offset=%d" % (self.base, uid, offset)
        return self._request("PUT", url, data=data, timeout=300,
                             content_type="application/octet-stream")

    def complete(self, uid):
        return self.post_json("/%s/complete" % uid, {}, timeout=600)


class Progress:
    def __init__(self, total_files, total_bytes):
        self.lock = threading.Lock()
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.done_bytes = 0
        self.file_index = 0
        self.file_name = ""
        self.file_size = 0
        self.file_done = 0

    def start_file(self, index, name, size, already):
        with self.lock:
            self.file_index = index
            self.file_name = name
            self.file_size = size
            self.file_done = already
        self._render()

    def add(self, n):
        with self.lock:
            self.file_done += n
            self.done_bytes += n
        self._render()

    def _render(self):
        pct = (100.0 * self.file_done / self.file_size
               if self.file_size else 100.0)
        bar_len = 24
        filled = int(bar_len * pct / 100.0)
        bar = "#" * filled + "-" * (bar_len - filled)
        line = ("[%d/%d] %s [%s] %d%% %s/%s"
                % (self.file_index, self.total_files, self.file_name,
                   bar, int(pct), fmt_size(self.file_done),
                   fmt_size(self.file_size)))
        sys.stdout.write("\r" + line[:120])
        sys.stdout.flush()

    def finish_line(self, text):
        sys.stdout.write("\r" + " " * 120 + "\r")
        print(text)
        sys.stdout.flush()


def scan_videos(path):
    """返回 [(绝对路径, 相对路径)]，相对路径供目录标签使用。"""
    path = os.path.abspath(path)
    found = []
    if os.path.isfile(path):
        if os.path.getsize(path) > 0:
            found.append((path, os.path.basename(path)))
        return found
    if not os.path.isdir(path):
        raise UploadError("路径不存在：%s" % path)
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            if name.startswith("."):
                continue
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in VIDEO_EXTS:
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) <= 0:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(full, path).replace(os.sep, "/")
            found.append((full, rel))
    found.sort(key=lambda item: item[1].lower())
    return found


def upload_file(client, path, relpath, tags, source, library_id,
                progress, index, state):
    """上传一个文件。返回结果字符串。"""
    st = os.stat(path)
    size = st.st_size
    name = os.path.basename(path)
    title = os.path.splitext(name)[0]
    digest = file_digest(path)
    # 文件在算哈希期间被改过就放弃，避免传出一半新一半旧。
    st2 = os.stat(path)
    if st2.st_size != size or st2.st_mtime_ns != st.st_mtime_ns:
        raise UploadError("文件在处理期间被修改，跳过。")

    try:
        res = client.check(digest)
    except UploadError:
        res = None
    if res and res.get("ok") and res.get("exists"):
        try:
            reg = client.register(digest, name, tags, source, relpath,
                                  library_id)
        except UploadError:
            reg = None
        if reg and reg.get("ok"):
            return "已存在，秒传跳过"

    key = os.path.abspath(path)
    saved = state.get(key) or {}
    uid = None
    done = set()
    if (saved.get("size") == size
            and saved.get("mtime_ns") == st.st_mtime_ns
            and saved.get("digest") == digest
            and saved.get("uid")):
        uid = saved["uid"]
        done = set(saved.get("done") or [])
    n_chunks = -(-size // CHUNK)

    def do_init():
        for attempt in range(CAPACITY_RETRIES + 1):
            res = client.init_upload(name, size, title, tags, source,
                                     relpath, library_id)
            if res.get("ok"):
                return res["uid"]
            msg = res.get("message") or ""
            if "预算不足" in msg or "磁盘剩余" in msg:
                if attempt < CAPACITY_RETRIES:
                    print("暂存区已满，等待归档腾出空间（%d 秒后重试）…"
                          % CAPACITY_WAIT)
                    time.sleep(CAPACITY_WAIT)
                    continue
                raise CapacityError(msg)
            raise UploadError(msg or "初始化失败")
        raise CapacityError("暂存区预算不足")

    if uid is None:
        uid = do_init()
        done = set()
    state[key] = {"size": size, "mtime_ns": st.st_mtime_ns,
                  "digest": digest, "uid": uid, "done": sorted(done)}
    save_json_atomic(STATE_FILE, state)

    already = sum(min(CHUNK, size - i * CHUNK) for i in done)
    progress.start_file(index, name, size, already)
    missing = [i for i in range(n_chunks) if i not in done]
    stop = threading.Event()
    failure = []

    def send_one(idx):
        if stop.is_set():
            return
        off = idx * CHUNK
        want = min(CHUNK, size - off)
        with open(path, "rb") as fh:
            fh.seek(off)
            data = fh.read(want)
        if len(data) != want:
            raise UploadError("文件在上传期间变小了：%s" % name)
        last_err = None
        for _ in range(CHUNK_TRIES):
            if stop.is_set():
                return
            try:
                res = client.put_chunk(uid, off, data)
            except UploadError as exc:
                last_err = exc
                time.sleep(1)
                continue
            if res.get("ok"):
                with progress.lock:
                    done.add(idx)
                    state[key]["done"] = sorted(done)
                save_json_atomic(STATE_FILE, state)
                progress.add(len(data))
                return
            msg = res.get("message") or ""
            if "会话" in msg:
                raise SessionLost(msg)
            last_err = UploadError(msg or "分片被拒绝")
            time.sleep(1)
        raise last_err or UploadError("分片上传失败")

    def run_chunks(idxs):
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=PARALLEL_CHUNKS) as pool:
            futs = [pool.submit(send_one, i) for i in idxs]
            for fut in concurrent.futures.as_completed(futs):
                try:
                    fut.result()
                except SessionLost:
                    stop.set()
                    raise
                except Exception as exc:
                    if not failure:
                        failure.append(exc)
                    stop.set()
        if failure:
            raise failure[0]

    try:
        run_chunks(missing)
    except SessionLost:
        # 服务器会话没了（重启/过期）：重新初始化，全量重传一次。
        uid = do_init()
        done = set()
        state[key] = {"size": size, "mtime_ns": st.st_mtime_ns,
                      "digest": digest, "uid": uid, "done": []}
        save_json_atomic(STATE_FILE, state)
        progress.start_file(index, name, size, 0)
        stop.clear()
        failure.clear()
        run_chunks(list(range(n_chunks)))

    try:
        res = client.complete(uid)
    except UploadError:
        # 响应丢失时用哈希确认是否已入库（与网页端一致）。
        try:
            chk = client.check(digest)
        except UploadError:
            chk = None
        if chk and chk.get("ok") and chk.get("exists"):
            res = {"ok": True, "duplicate": False, "recovered": True}
        else:
            raise
    if not res.get("ok"):
        msg = res.get("message") or ""
        raise UploadError(msg or "入库失败")
    state.pop(key, None)
    save_json_atomic(STATE_FILE, state)
    if res.get("duplicate"):
        return "内容已存在，已新增条目（不重复占空间）"
    if res.get("recovered"):
        return "已入库（响应丢失后经校验确认）"
    return "已入库，等待归档"


def ask_path():
    while True:
        raw = input("请输入文件或文件夹路径：").strip().strip("\"").strip("'")
        raw = os.path.expanduser(raw)
        if raw and (os.path.isfile(raw) or os.path.isdir(raw)):
            return raw
        print("路径不存在，请重新输入。")


def load_config(cli_server, cli_token):
    cfg = load_json(CONFIG_FILE)
    server = (cli_server or os.environ.get("VIDLIB_SERVER")
              or cfg.get("server") or "")
    token = (cli_token or os.environ.get("VIDLIB_TOKEN")
             or cfg.get("token") or "")
    changed = False
    if not server:
        entered = input("服务器地址 [%s]：" % DEFAULT_SERVER).strip()
        server = entered or DEFAULT_SERVER
        changed = True
    if not token:
        token = input(
            "请输入上传令牌（资源库 管理 → 视频 页的「批量上传令牌」）："
        ).strip()
        if not token:
            raise UploadError("没有令牌，无法上传。")
        changed = True
    if changed and not cli_server and not cli_token:
        save_json_atomic(CONFIG_FILE, {"server": server, "token": token})
        print("服务器与令牌已记住，存在脚本旁边的配置文件里。")
    return server, token


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="资源库批量上传：输入路径，上传其中全部视频。")
    parser.add_argument("path", nargs="?",
                        help="文件或文件夹路径；不带则运行后提示输入")
    parser.add_argument("--tags", default="", help="标签，逗号分隔")
    parser.add_argument("--source", default="", help="来源")
    parser.add_argument("--library-id", type=int, default=None,
                        help="视频库 ID；不带则进默认库")
    parser.add_argument("--server", default="",
                        help="服务器地址，默认记住的值")
    parser.add_argument("--token", default="", help="上传令牌")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出要传的文件，不上传")
    args = parser.parse_args(argv)

    try:
        path = args.path or ask_path()
        files = scan_videos(path)
        if not files:
            print("没有找到视频文件。")
            return 0
        total = sum(os.path.getsize(p) for p, _ in files)
        print("找到 %d 个视频，共 %s。" % (len(files), fmt_size(total)))
        if args.dry_run:
            for _, rel in files:
                print("  " + rel)
            return 0
        server, token = load_config(args.server, args.token)
        client = Client(server, token)
        state = load_json(STATE_FILE)
        progress = Progress(len(files), total)
        ok = 0
        failed = []
        for index, (full, rel) in enumerate(files, 1):
            name = os.path.basename(full)
            try:
                result = upload_file(client, full, rel, args.tags,
                                     args.source, args.library_id,
                                     progress, index, state)
                progress.finish_line("[%d/%d] %s：%s"
                                     % (index, len(files), name, result))
                ok += 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                progress.finish_line("[%d/%d] %s：失败：%s"
                                     % (index, len(files), name, exc))
                failed.append((name, str(exc)))
        print("完成：成功 %d 个，失败 %d 个。" % (ok, len(failed)))
        for name, err in failed:
            print("  失败：%s（%s）" % (name, err))
        return 0 if not failed else 1
    except KeyboardInterrupt:
        print("\n已中断，进度已保存；再跑一次会断点续传。")
        return 130
    except UploadError as exc:
        print("错误：%s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
