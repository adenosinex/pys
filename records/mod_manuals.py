"""Smart manuals (AI OCR + tagging), ported from the user's myweb
3aiocr blueprint. Storage: manuals.db + manuals_img/ in the platform
data directory. The two-stage pipeline (vision OCR, then NLP tagging)
keeps his models and prompts and runs on the shared SiliconFlow key
(project env first, then the in-app credentials store); without a key
the library functions work and processing reports the missing key.
Import/export follows the snapshots pattern: one JSON backup with
base64 images, de-duplicated by record id on import."""
import base64
import json
import os
import re
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.request import Request, urlopen

from webutil import json_response, parse_multipart, read_json, serve_file

BASE = os.path.dirname(os.path.abspath(__file__))


def _platform_data_dir(base):
    """Persistent data directory: the platform's DATA_DIR env var when
    provided, else the ./data symlink the panel creates inside the
    project folder (pointing at the central data directory), else
    ./.runtime for standalone local runs."""
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(base, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(base, ".runtime")


RUNTIME = _platform_data_dir(BASE)
DB_PATH = os.path.join(RUNTIME, "manuals.db")
IMG_DIR = os.path.join(RUNTIME, "manuals_img")
CRED_FILE = os.path.join(RUNTIME, "credentials.json")


def _api_key(name="SILICONFLOW_API_KEY"):
    env = os.getenv(name, "").strip()
    if env:
        return env
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        values = data.get("values", {}) if isinstance(data, dict) else {}
        return str(values.get(name, "") or "").strip()
    except Exception:
        return ""


OCR_CONFIG = {
    "base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "model": "Qwen/Qwen3-VL-30B-A3B-Instruct",
}
NLP_CONFIG = {
    "base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "model": "deepseek-ai/DeepSeek-V4-Flash",
}

_executor = None


def _get_executor():
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=5)
    return _executor


def _call_api(config, messages):
    key = _api_key()
    if not key:
        raise Exception("未配置 SILICONFLOW_API_KEY（在记录台首页的 API 凭据区保存）")
    payload = json.dumps({"model": config["model"], "messages": messages},
                         ensure_ascii=False).encode("utf-8")
    req = Request(config["base_url"], data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
    })
    with urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def init_db():
    os.makedirs(IMG_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manuals (
                id TEXT PRIMARY KEY,
                filename TEXT,
                filepath TEXT,
                ocr_text TEXT,
                tags TEXT,
                ocr_model TEXT,
                nlp_model TEXT,
                processing_time_sec REAL,
                status TEXT,
                created_at TEXT,
                ocr_time_sec REAL,
                nlp_time_sec REAL,
                original_hash TEXT
            )
        """)


def _set_status(record_id, status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE manuals SET status = ? WHERE id = ?", (status, record_id))


def _img_path(record_id):
    return os.path.join(IMG_DIR, record_id + ".jpg")


# ------------------------------------------------------------ pipeline

def process_ocr_task(record_id, absolute_filepath):
    start = time.time()
    try:
        _set_status(record_id, "processing_ocr")
        with open(absolute_filepath, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("utf-8")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "请提取图中所有文字，严禁遗漏。使用Markdown格式呈现表格和标题，保持原有阅读顺序。"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ],
        }]
        out = _call_api(OCR_CONFIG, messages)
        ocr_text = out if isinstance(out, str) else str(out)
        dur = round(time.time() - start, 2)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE manuals SET ocr_text = ?, ocr_time_sec = ?, ocr_model = ?,"
                " status = 'pending_nlp' WHERE id = ?",
                (ocr_text, dur, OCR_CONFIG["model"], record_id))
        _get_executor().submit(process_nlp_task, record_id)
    except Exception as exc:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE manuals SET status = 'ocr_failed', ocr_text = ? WHERE id = ?",
                         ("OCR Error: " + str(exc), record_id))


def process_nlp_task(record_id):
    start = time.time()
    try:
        _set_status(record_id, "processing_nlp")
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT ocr_text, ocr_time_sec FROM manuals WHERE id = ?",
                (record_id,)).fetchone()
        if not row or not row[0] or row[0].startswith("OCR Error"):
            raise Exception("未找到有效的 OCR 提取文本")
        ocr_text = row[0]
        ocr_dur = float(row[1] or 0)
        messages = [{
            "role": "user",
            "content": "请提取2-5个凝练标签（如物品类、品牌、场景）。返回纯JSON数组，不要代码块。\n文本："
                       + ocr_text[:2500],
        }]
        raw = _call_api(NLP_CONFIG, messages).strip()
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        tags = json.loads(m.group(0)) if m else ["未分类"]
        dur = round(time.time() - start, 2)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE manuals SET tags = ?, nlp_time_sec = ?, processing_time_sec = ?,"
                " status = 'completed', nlp_model = ? WHERE id = ?",
                (json.dumps(tags, ensure_ascii=False), dur, round(ocr_dur + dur, 2),
                 NLP_CONFIG["model"], record_id))
    except Exception:
        _set_status(record_id, "nlp_failed")


# ------------------------------------------------------------ handlers

def manuals_list(environ, start_response):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, filename, tags, status, processing_time_sec, ocr_time_sec,"
            " nlp_time_sec, ocr_text FROM manuals"
            " WHERE status != 'uploaded' ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        try:
            tags = json.loads(r[2]) if r[2] else []
        except Exception:
            tags = []
        out.append({
            "id": r[0], "filename": r[1] or "未命名文件",
            "url": "api/manuals/img/%s.jpg" % r[0],
            "tags": tags, "status": r[3],
            "metrics": {"total_sec": r[4], "ocr_sec": r[5], "nlp_sec": r[6]},
            "ocr_text": r[7],
        })
    return json_response(start_response, out)


def manuals_upload(environ, start_response):
    fields, files = parse_multipart(environ)
    if "file" not in files:
        return json_response(start_response, {"error": "No file"}, 400)
    filename, blob = files["file"]
    original_hash = (fields.get("original_hash") or [None])[0] \
        if isinstance(fields.get("original_hash"), list) else fields.get("original_hash")
    original_name = fields.get("original_filename") or filename or "未命名文件"
    if isinstance(original_name, list):
        original_name = original_name[0]
    if original_hash:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT id FROM manuals WHERE original_hash = ?",
                               (original_hash,)).fetchone()
        if row:
            return json_response(start_response,
                                 {"status": "duplicate", "message": "原文件已存在，跳过"})
    record_id = str(uuid.uuid4())
    with open(_img_path(record_id), "wb") as fh:
        fh.write(blob)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO manuals (id, filename, filepath, original_hash, ocr_text,"
            " tags, status, created_at) VALUES (?, ?, ?, ?, '', '[]', 'uploaded', ?)",
            (record_id, original_name, _img_path(record_id), original_hash, now))
    return json_response(start_response, {"status": "success", "id": record_id})


def manuals_start(environ, start_response):
    data = read_json(environ) or {}
    ids = data.get("ids") or []
    with sqlite3.connect(DB_PATH) as conn:
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT id, filepath FROM manuals WHERE id IN (%s)"
            % ",".join("?" * len(ids)), ids).fetchall()} if ids else {}
        for rid in ids:
            conn.execute("UPDATE manuals SET status = 'pending_ocr' WHERE id = ?", (rid,))
    for rid in ids:
        if rid in rows and rows[rid]:
            _get_executor().submit(process_ocr_task, rid, os.path.abspath(rows[rid]))
    return json_response(start_response, {"status": "started"})


def manuals_retry(environ, start_response):
    data = read_json(environ) or {}
    rid = data.get("id")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT filepath FROM manuals WHERE id = ?", (rid,)).fetchone()
        if not row:
            return json_response(start_response, {"error": "Not found"}, 404)
        conn.execute("UPDATE manuals SET status = 'pending_ocr', ocr_text = '',"
                     " tags = '[]' WHERE id = ?", (rid,))
    _get_executor().submit(process_ocr_task, rid, os.path.abspath(row[0]))
    return json_response(start_response, {"status": "queued_full_retry"})


def manuals_reanalyze(environ, start_response):
    data = read_json(environ) or {}
    rid = data.get("id")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT ocr_text FROM manuals WHERE id = ?", (rid,)).fetchone()
        if not row:
            return json_response(start_response, {"error": "Not found"}, 404)
        if not row[0] or row[0].startswith("OCR Error"):
            return json_response(start_response,
                                 {"error": "没有找到可用的OCR文本，请使用全量重试"}, 400)
        conn.execute("UPDATE manuals SET status = 'pending_nlp', tags = '[]' WHERE id = ?",
                     (rid,))
    _get_executor().submit(process_nlp_task, rid)
    return json_response(start_response, {"status": "queued_nlp_only"})


def manuals_delete(environ, start_response, record_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT filepath FROM manuals WHERE id = ?",
                           (record_id,)).fetchone()
        conn.execute("DELETE FROM manuals WHERE id = ?", (record_id,))
    if row and row[0]:
        try:
            os.remove(row[0])
        except OSError:
            pass
    return json_response(start_response, {"status": "success"})


def manuals_img(environ, start_response, imgname):
    safe = os.path.basename(imgname)
    return serve_file(start_response, os.path.join(IMG_DIR, safe), environ)


# ------------------------------------------------------- backup/restore

def _download_json(start_response, payload, filename):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                              ("Content-Disposition", 'attachment; filename="%s"' % filename),
                              ("Content-Length", str(len(body)))])
    return [body]


def manuals_export(environ, start_response):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM manuals ORDER BY created_at").fetchall()]
    items = []
    for row in rows:
        item = {k: row[k] for k in ("id", "filename", "ocr_text", "tags", "status",
                                    "ocr_model", "nlp_model", "processing_time_sec",
                                    "ocr_time_sec", "nlp_time_sec", "original_hash",
                                    "created_at")}
        item["image_base64"] = None
        item["image_ext"] = ".jpg"
        path = _img_path(row["id"])
        if os.path.exists(path):
            with open(path, "rb") as fh:
                item["image_base64"] = base64.b64encode(fh.read()).decode("ascii")
        items.append(item)
    return _download_json(start_response,
                          {"format": "manuals-backup", "version": 1, "items": items},
                          "manuals-backup.json")


def manuals_import(environ, start_response):
    data = read_json(environ)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return json_response(start_response, {"ok": False, "error": "备份格式不对。"}, 400)
    added = skipped = 0
    with sqlite3.connect(DB_PATH) as conn:
        existing = {r[0] for r in conn.execute("SELECT id FROM manuals").fetchall()}
        for it in items:
            if not isinstance(it, dict) or not it.get("id"):
                skipped += 1
                continue
            rid = str(it["id"])
            if rid in existing:
                skipped += 1
                continue
            blob = it.get("image_base64")
            if blob:
                try:
                    raw = base64.b64decode(blob)
                except Exception:
                    raw = b""
                if raw:
                    os.makedirs(IMG_DIR, exist_ok=True)
                    with open(_img_path(rid), "wb") as fh:
                        fh.write(raw)
            tags = it.get("tags")
            if not isinstance(tags, str):
                tags = json.dumps(tags or [], ensure_ascii=False)
            metrics_ok = True
            conn.execute(
                "INSERT INTO manuals (id, filename, filepath, ocr_text, tags, ocr_model,"
                " nlp_model, processing_time_sec, status, created_at, ocr_time_sec,"
                " nlp_time_sec, original_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, it.get("filename") or "未命名文件", _img_path(rid),
                 it.get("ocr_text") or "", tags, it.get("ocr_model"), it.get("nlp_model"),
                 it.get("processing_time_sec"), it.get("status") or "completed",
                 it.get("created_at"), it.get("ocr_time_sec"), it.get("nlp_time_sec"),
                 it.get("original_hash")))
            existing.add(rid)
            added += 1
    return json_response(start_response, {"ok": True, "added": added, "skipped": skipped})
