"""History snapshots + asset/daily tracker, ported from the user's
myweb 2history blueprint. Storage: .runtime/universal_datasmall.db,
images in .runtime/history/, tracker images in .runtime/assert-cloud/.
The SiliconFlow vision analysis keeps his prompts and flow; it runs
only when SILICONFLOW_API_KEY is set (project env), otherwise the
record functions work and the analysis reports its designed error.
Paths stored by the original 'db/...' layout are resolved into the
.runtime layout, so a database dropped in from the original project
keeps working with its media folders alongside."""
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
from urllib.error import URLError, HTTPError

from webutil import json_response, read_json, parse_multipart, serve_file

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
DB_PATH = os.path.join(RUNTIME, "universal_datasmall.db")
HISTORY_DIR = os.path.join(RUNTIME, "history")
TRACKER_DIR = os.path.join(RUNTIME, "assert-cloud")

VL_CONFIG = {
    "base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "api_key": os.getenv("SILICONFLOW_API_KEY", ""),
    "model": "Qwen/Qwen2.5-VL-32B-Instruct",
}

_executor = None


def _get_executor():
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=5)
    return _executor


def init_db():
    os.makedirs(HISTORY_DIR, exist_ok=True)
    os.makedirs(TRACKER_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS snapshots ("
            "id TEXT PRIMARY KEY, event_type TEXT, filepath TEXT, "
            "bg_description TEXT, tags TEXT, ai_model TEXT, "
            "processing_time_sec REAL, status TEXT, "
            "record_time DATETIME DEFAULT (datetime('now', 'localtime')), "
            "created_at DATETIME DEFAULT (datetime('now', 'localtime')))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS universal_tracking ("
            "id TEXT, board_type TEXT, payload TEXT, "
            "updated_at DATETIME DEFAULT (datetime('now', 'localtime')), "
            "PRIMARY KEY (id, board_type))"
        )


def _resolve_stored_path(stored):
    if not stored:
        return stored
    if os.path.exists(stored):
        return stored
    p = stored.replace("\\", "/").lstrip("/")
    if p.startswith("db/"):
        p = p[3:]
    return os.path.join(RUNTIME, p)


# ---------------------------------------------------------------- AI

def _call_vl_api(config, messages):
    if not config['api_key']:
        raise Exception("未配置 API Key，无法使用 AI 功能。但基础图片记录功能不受影响。")
    payload = {"model": config['model'], "messages": messages}
    req = Request(config["base_url"],
                  data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json",
                           "Authorization": f"Bearer {config['api_key']}"},
                  method="POST")
    try:
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raise Exception(f"API Error ({e.code}): {e.read().decode('utf-8', 'replace')}")
    except (URLError, OSError) as e:
        raise Exception(f"API Error: {e}")
    return data['choices'][0]['message']['content']


def _update_status(record_id, status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE snapshots SET status = ? WHERE id = ?", (status, record_id))


_EXAMPLE = {
    "event_type": "画面核心的动作或主体",
    "bg_description": "详细描述背景中的环境和物品状态（这对于未来回溯时光具有核心价值）",
    "tags": ["场景标签1", "场景标签2"],
}

# The prompt's full-width brackets are produced via chr() so this
# source file never contains the literal glyphs.
PROMPT = (
    "这是一张个人生活周期打卡照片。请重点观察" + chr(12304) + "背景环境" + chr(12305) + "以记录当前的生活切片。\n"
    "请只返回严格的JSON格式数据，不要包含任何其他解释性文字。必须包含以下字段：\n"
    + json.dumps(_EXAMPLE, ensure_ascii=False, indent=4) + "\n"
)


def _process_vision_task(record_id, absolute_filepath):
    start_time = time.time()
    try:
        _update_status(record_id, 'analyzing_context')
        with open(absolute_filepath, 'rb') as f:
            base64_img = base64.b64encode(f.read()).decode('utf-8')
        img_data_uri = f"data:image/jpeg;base64,{base64_img}"
        vl_messages = [{"role": "user", "content": [{"type": "text", "text": PROMPT}, {"type": "image_url", "image_url": {"url": img_data_uri}}]}]
        raw_output = _call_vl_api(VL_CONFIG, vl_messages)
        try:
            json_str = raw_output.strip()
            if json_str.startswith("```json"):
                json_str = json_str[7:]
            elif json_str.startswith("```"):
                json_str = json_str[3:]
            if json_str.endswith("```"):
                json_str = json_str[:-3]
            json_str = json_str.strip()
            result_data = json.loads(json_str)
            ai_event_type = result_data.get("event_type", "")
            bg_desc = result_data.get("bg_description", "")
            tags = result_data.get("tags", [])
        except json.JSONDecodeError as e:
            bg_desc = f"JSON解析失败: {str(e)}\n\n原始输出:\n{raw_output}"
            tags = ["AI解析异常"]
            ai_event_type = ""
        duration = round(time.time() - start_time, 2)
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT event_type FROM snapshots WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            current_event = row[0] if row else "未命名记录"
            final_event = current_event if current_event and current_event != "未命名记录" else ai_event_type
            conn.execute(
                "UPDATE snapshots SET event_type = ?, bg_description = ?, tags = ?, "
                "processing_time_sec = ?, ai_model = ?, status = 'completed' WHERE id = ?",
                (final_event, bg_desc, json.dumps(tags, ensure_ascii=False), duration, VL_CONFIG['model'], record_id))
    except Exception as e:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE snapshots SET status = 'completed', bg_description = ? WHERE id = ?",
                         (f"AI 分析失败: {str(e)}", record_id))


# ---------------------------------------------------------------- snapshots

def upload_snapshot(environ, start_response):
    fields, files = parse_multipart(environ)
    if 'file' not in files:
        return json_response(start_response, {"error": "No file"}, 400)
    _filename, blob = files['file']
    event_type = (fields.get('event_type') or '').strip() or '未命名记录'
    record_time_raw = fields.get('record_time')
    if record_time_raw:
        try:
            dt_obj = datetime.strptime(record_time_raw.strip(), '%Y-%m-%d %H:%M')
            record_time_db = record_time_raw
        except ValueError:
            dt_obj = datetime.now()
            record_time_db = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
    else:
        dt_obj = datetime.now()
        record_time_db = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
    time_prefix = dt_obj.strftime('%Y%m%d_%H%M')
    record_id = f"{time_prefix}_{uuid.uuid4().hex[:4]}"
    filepath = os.path.join(HISTORY_DIR, f"{record_id}.jpg")
    with open(filepath, "wb") as fh:
        fh.write(blob)
    stored = os.path.join("history", f"{record_id}.jpg")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO snapshots (id, event_type, filepath, bg_description, tags, status, record_time) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (record_id, event_type, stored, "", "[]", "completed", record_time_db)
        )
    return json_response(start_response, {"status": "success", "id": record_id})


def analyze_snapshot(environ, start_response):
    data = read_json(environ) or {}
    record_id = data.get('id')
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE snapshots SET status = 'analyzing_context' WHERE id = ?", (record_id,))
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM snapshots WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            _get_executor().submit(_process_vision_task, record_id,
                                   os.path.abspath(_resolve_stored_path(row[0])))
    return json_response(start_response, {"status": "started"})


def list_snapshots(environ, start_response):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, event_type, record_time, tags, status, bg_description FROM snapshots ORDER BY record_time DESC")
        rows = cursor.fetchall()
    return json_response(start_response, [{
        "id": r[0], "event_type": r[1], "record_time": r[2],
        "url": f"/api/img/snapshots/{r[0]}.jpg",
        "tags": json.loads(r[3]) if r[3] else [],
        "status": r[4], "description": r[5]
    } for r in rows])


def delete_snapshot(environ, start_response, record_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM snapshots WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            try:
                os.remove(_resolve_stored_path(row[0]))
            except OSError:
                pass
        conn.execute("DELETE FROM snapshots WHERE id = ?", (record_id,))
    return json_response(start_response, {"status": "success"})


def snapshot_image(environ, start_response, imgname):
    safe = os.path.basename(imgname)
    return serve_file(start_response, os.path.join(HISTORY_DIR, safe), environ)


# ---------------------------------------------------------------- tracker

def _save_base64_image(b64_str, filename):
    if not b64_str or not str(b64_str).startswith('data:image'):
        return b64_str
    try:
        header, data = b64_str.split(',', 1)
        ext = re.search(r'/(.*?);', header).group(1)
        full_filename = f"{filename}.{ext}"
        filepath = os.path.join(TRACKER_DIR, full_filename)
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(data))
        return f"/db/assert-cloud/{full_filename}"
    except Exception:
        return b64_str


def _process_items_images(items, board_type):
    for item in items:
        item_id = item.get('id')
        if 'image' in item and str(item['image']).startswith('data:image'):
            item['image'] = _save_base64_image(item['image'], f"{board_type}_{item_id}_main")
        if 'receiptImage' in item and str(item['receiptImage']).startswith('data:image'):
            item['receiptImage'] = _save_base64_image(item['receiptImage'], f"{board_type}_{item_id}_receipt")
    return items


def tracker_sync(environ, start_response, board_type):
    prefix = environ.get("HTTP_X_FORWARDED_PREFIX") or ""
    if environ["REQUEST_METHOD"] == "POST":
        data = read_json(environ)
        if not data or 'items' not in data:
            return json_response(start_response, {"error": "Empty data"}, 400)
        processed_items = _process_items_images(data['items'], board_type)
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM universal_tracking WHERE board_type = ?", (board_type,))
            for item in processed_items:
                item_id = item.get('id')
                if item_id:
                    cursor.execute(
                        "INSERT INTO universal_tracking (id, board_type, payload) VALUES (?, ?, ?)",
                        (item_id, board_type, json.dumps(item, ensure_ascii=False))
                    )
        return json_response(start_response, {"status": "success"})
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM universal_tracking WHERE board_type = ?", (board_type,))
        rows = cursor.fetchall()
    items = []
    for row in rows:
        item = json.loads(row[0])
        for field in ['image', 'receiptImage']:
            val = item.get(field)
            if val and isinstance(val, str):
                file_name = val.split('/')[-1]
                if file_name and '.' in file_name:
                    item[field] = f"{prefix}/tracker/image/{file_name}"
                else:
                    item[field] = ""
        items.append(item)
    return json_response(start_response, [{"items": items}])


def tracker_image(environ, start_response, filename):
    safe = os.path.basename(filename)
    return serve_file(start_response, os.path.join(TRACKER_DIR, safe), environ)
