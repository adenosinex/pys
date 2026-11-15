#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把当前目录下所有 txt / md 文件内容批量上传到笔记接口，
上传成功后移动到 ./archive-YYYY-MM-DD/ 目录。
直接运行即可：python import_notes.py
"""

import os
import time
import json
import shutil
from pathlib import Path
from datetime import datetime

import requests


API_URL = "https://debi.zin7.de/web/docsnew/docapi/document/save"
# 直达码获取顺序：环境变量 DOCSNEW_DIRECT_CODE > 用户目录下的保存文件 > 提示输入（输入后保存）。
# 码本身绝不写进本文件。换码：删保存文件，或改环境变量。
CODE_ENV_VAR = "DOCSNEW_DIRECT_CODE"
CODE_FILE = Path.home() / ".docsnew_direct_code"
BASE_DIR = Path(r'\\240-2\md')
TIMEOUT = 30
SUPPORTED_EXTS = (".txt", ".md")   # 支持的文件类型


# 面板生成的直达码只含 URL 安全字符；其它字符一定是复制带入的杂质，
# 其中控制字符会让服务器在 HTTP 层直接拒收请求（400），必须先拦下。
_CODE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")

# 复制来的码首尾可能带入空白、BOM 或控制符。合法直达码只含 URL 安全
# 字符，首尾这些杂质一律去掉，中间的仍由 _code_problem 报错。
_PASTE_EDGE_CHARS = "".join(chr(c) for c in range(0x00, 0x20)) + "\x7f\ufeff "


def _code_problem(code):
    """返回首个非法字符的说明，合法返回 None。"""
    for i, ch in enumerate(code):
        if ch not in _CODE_CHARS:
            msg = f"第 {i + 1} 位含有无效字符 U+{ord(ch):04X}"
            if ch == "=":
                msg += "（请只粘贴等号后面的值，不要带 hub_direct= 前缀）"
            return msg
    return None


def get_direct_code() -> str:
    """取全局直达码：环境变量 > 保存文件 > 提示输入（输入后保存到文件）。取不到返回 ""。"""
    code = os.environ.get(CODE_ENV_VAR, "").strip()
    if code:
        problem = _code_problem(code)
        if problem:
            print(f"环境变量 {CODE_ENV_VAR} 里的直达码{problem}，请重新复制后重试。")
            return ""
        return code
    try:
        code = CODE_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        code = ""
    if code:
        problem = _code_problem(code)
        if problem:
            print(f"保存文件 {CODE_FILE} 里的直达码{problem}，请删除该文件后重新运行。")
            return ""
        return code
    while True:
        try:
            code = input("请输入全局直达码（只输一次，将保存到用户目录自动读取；输入可见）：").strip(_PASTE_EDGE_CHARS)
        except (EOFError, KeyboardInterrupt):
            return ""
        if not code:
            return ""
        problem = _code_problem(code)
        if problem:
            print(f"输入的直达码{problem}（可能是复制时带入的不可见字符），请重新复制后再次输入。")
            continue
        try:
            CODE_FILE.write_text(code, encoding="utf-8")
            try:
                os.chmod(CODE_FILE, 0o600)
            except OSError:
                pass
        except OSError as e:
            print(f"  [!] 直达码保存失败: {e}（本次继续使用，下次需重新输入）")
        return code


def get_archive_dir() -> Path:
    """当日归档目录 ./archive-YYYY-MM-DD/"""
    day = datetime.now().strftime("%Y-%m-%d")
    target = BASE_DIR / f"archive-{day}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def move_to_archive(src: Path, archive_dir: Path) -> Path:
    """移动到归档目录，同名时自动加后缀。"""
    dst = archive_dir / src.name
    if dst.exists():
        stem, suffix = src.stem, src.suffix
        i = 1
        while True:
            candidate = archive_dir / f"{stem}_{i}{suffix}"
            if not candidate.exists():
                dst = candidate
                break
            i += 1
    shutil.move(str(src), str(dst))
    return dst


def get_all_file_times(path: Path) -> list:
    """
    提取文件所有可用的时间戳（秒）。
    包括：
      - st_mtime   修改时间
      - st_ctime   Windows=创建时间 / Unix=inode 变更时间
      - st_atime   访问时间
      - st_birthtime 部分平台支持的真实创建时间（macOS / 某些 Windows 环境）
    返回值仅包含 >0 的整数时间戳。
    """
    times = []
    try:
        st = path.stat()
    except OSError:
        return times

    for attr in ("st_mtime", "st_ctime", "st_atime", "st_birthtime"):
        v = getattr(st, attr, None)
        if v is not None and v > 0:
            times.append(int(v))

    return times


def get_created_time(path: Path) -> int:
    """
    取文件各类时间中最早的一个，作为笔记创建时间。
    返回 Unix 时间戳（秒），例如 1787620710。
    若所有时间都取不到，退化为当前时间。
    """
    times = get_all_file_times(path)
    if not times:
        # 极端情况（文件被删除/无权限）退化为当前时间
        return int(time.time())
    return min(times)


def collect_files() -> list[Path]:
    """收集目录下所有 .txt / .md 文件（不区分大小写），按文件名排序。"""
    files = [
        p for p in BASE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    ]
    return sorted(files, key=lambda p: p.name.lower())


def upload(content: str, created_time: int, session: requests.Session, code: str):
    """
    上传一份内容到接口，返回 (成功?, 说明)
    参考网页请求：POST JSON {id, content, created_time}
    """
    doc_id = f"note_{int(time.time() * 1000)}"
    payload = {
        "id": doc_id,
        "content": content,
        "created_time": created_time,   # 文件最早时间 -> 笔记创建时间
    }

    try:
        resp = session.post(
            API_URL,
            json=payload,
            headers={
                "X-Hub-Direct": code,
                "accept": "*/*",
                "content-type": "application/json",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        return False, f"请求异常: {e}"

    if resp.status_code in (301, 302, 303, 307, 308):
        return False, "直达码未被接受（服务器重定向到验证页）：请确认用的是当前全局直达码；换码请删除保存文件或修改环境变量"

    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"

    # 校验返回数据
    try:
        data = resp.json()
    except Exception:
        return False, f"返回非 JSON: {resp.text[:200]}"

    # 期望返回结构里包含 meta（网页端保存后也是用 meta 同步的）
    meta = data.get("meta") if isinstance(data, dict) else None
    if not meta:
        return False, f"响应缺少 meta 字段: {resp.text[:200]}"

    returned_id = meta.get("id")
    if returned_id != doc_id:
        return False, f"返回 id 不一致: 期望 {doc_id}, 得到 {returned_id}"

    return True, (
        f"id={doc_id}, created_time={created_time}, "
        f"update_time={meta.get('update_time')}"
    )


def main():
    code = get_direct_code()
    if not code:
        print("未提供全局直达码，无法上传。")
        return

    files = collect_files()

    if not files:
        print("当前目录没有 .txt / .md 文件")
        return

    archive_dir = get_archive_dir()
    print(f"归档目录: {archive_dir}")
    print(f"待处理: {len(files)} 个文件\n")

    session = requests.Session()
    ok, fail = 0, 0

    for path in files:
        print(f"[{path.name}]")

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                content = path.read_text(encoding="gbk")
            except Exception as e:
                print(f"  [x] 读取失败: {e}\n")
                fail += 1
                continue
        except Exception as e:
            print(f"  [x] 读取失败: {e}\n")
            fail += 1
            continue

        if not content.strip():
            print("  [!] 空文件，跳过\n")
            fail += 1
            continue

        # 文件各类时间中最早的一个 -> 笔记创建时间
        created_time = get_created_time(path)
        readable = datetime.fromtimestamp(created_time).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  [i] 创建时间(最早): {created_time} ({readable})")

        success, msg = upload(content, created_time, session, code)

        if success:
            print(f"  [√] 上传成功: {msg}")
            try:
                dst = move_to_archive(path, archive_dir)
                print(f"  -> 已归档: {dst.relative_to(BASE_DIR)}")
            except Exception as e:
                print(f"  [!] 归档失败: {e}")
            ok += 1
        else:
            print(f"  [x] 上传失败: {msg}")
            fail += 1
            if msg.startswith("直达码未被接受"):
                print("  [!] 直达码有问题，停止继续尝试；修正后重新运行即可，未归档的文件都在。")
                break

        print()

    print("=" * 50)
    print(f"完成: 成功 {ok} / 失败 {fail}")


if __name__ == "__main__":
    main()
