"""Audio memos, ported from the user's myweb 7audiom blueprint.
Recording storage and the Volcengine streaming-ASR + SiliconFlow NLP
pipeline keep his protocol code verbatim; the websocket-client
library is vendored into this project (the hub env does not ship it),
requests became urllib, and storage moved to .runtime (vocal_data.db,
audio_uploads/, raw_responses/). Without DB_API_KEY /
SILICONFLOW_API_KEY in the project env, uploads and playback work
and transcription lands in his designed failure states."""
import gzip
import json
import math
import os
import sqlite3
import struct
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import websocket  # vendored websocket-client

from webutil import json_response, read_json, parse_multipart, serve_file

BASE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(BASE, ".runtime")
DB_PATH = os.path.join(RUNTIME, "vocal_data.db")
UPLOAD_FOLDER = os.path.join(RUNTIME, "audio_uploads")
RAW_FOLDER = os.path.join(RUNTIME, "raw_responses")

STT_CONFIG = {
    "api_key": os.getenv("DB_API_KEY", "your-volcengine-api-key"),
    "resource_id": "volc.seedasr.sauc.duration",
    "ws_url": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream",
}

NLP_CONFIG = {
    "base_url": "https://api.siliconflow.cn/v1/chat/completions",
    "api_key": os.getenv("SILICONFLOW_API_KEY"),
    "model": "deepseek-ai/DeepSeek-V4-Flash",
}

_executor = None


def _get_executor():
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=5)
    return _executor


def init_db():
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(RAW_FOLDER, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS vocal_records (
                id TEXT PRIMARY KEY,
                filename TEXT,
                filepath TEXT,
                stt_text TEXT,
                sync_data TEXT,
                title_with_tags TEXT,
                duration_sec REAL,
                stt_model TEXT,
                nlp_model TEXT,
                stt_time_sec REAL,
                nlp_time_sec REAL,
                processing_time_sec REAL,
                status TEXT,
                created_at DATETIME DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        try:
            conn.execute("ALTER TABLE vocal_records ADD COLUMN duration_sec REAL")
        except sqlite3.Error:
            pass


def _resolve_stored_path(stored):
    if not stored:
        return stored
    if os.path.exists(stored):
        return stored
    p = stored.replace("\\", "/").lstrip("/")
    if p.startswith("db/"):
        p = p[3:]
    return os.path.join(RUNTIME, p)


def _update_status(record_id, status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE vocal_records SET status = ? WHERE id = ?", (status, record_id))


def _fallback_title(record_id, suffix_tag=None):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT created_at, duration_sec FROM vocal_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            created_at, duration = row
            dur_str = f"[{int(duration)}s]" if duration else ""
            base_title = f"记录_{created_at.replace('-', '').replace(':', '').replace(' ', '_')} {dur_str}"
            return f"{base_title} #{suffix_tag}" if suffix_tag else base_title
    return f"未知记录 #{suffix_tag}" if suffix_tag else "未知记录"


# ---------------------------------------------------------------- STT

def _pack_full_client_request(req_json):
    payload = gzip.compress(json.dumps(req_json).encode('utf-8'))
    header = b'\x11\x10\x11\x00'
    size = struct.pack('>I', len(payload))
    return header + size + payload


def _pack_audio_request(audio_chunk, is_last=False):
    payload = gzip.compress(audio_chunk)
    header = b'\x11\x22\x01\x00' if is_last else b'\x11\x20\x01\x00'
    size = struct.pack('>I', len(payload))
    return header + size + payload


def _process_stt_task(record_id, absolute_filepath):
    start_time = time.time()
    ws = None
    try:
        _update_status(record_id, 'processing_stt')
        task_id = str(uuid.uuid4())
        file_ext = os.path.splitext(absolute_filepath)[1].lower().strip('.')
        if file_ext in ['webm', 'ogg']:
            audio_format, codec = 'ogg', 'opus'
        elif file_ext in ['wav']:
            audio_format, codec = 'wav', 'raw'
        else:
            audio_format, codec = 'mp3', 'raw'
        headers = {
            "X-Api-Key": STT_CONFIG["api_key"],
            "X-Api-Resource-Id": STT_CONFIG["resource_id"],
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1",
        }
        ws = websocket.create_connection(STT_CONFIG["ws_url"], header=headers, timeout=15)
        req_json = {
            "user": {"uid": "vocalmind_user"},
            "audio": {
                "format": audio_format,
                "codec": codec,
                "rate": 16000,
                "bits": 16,
                "channel": 1,
                "language": "zh-CN",
            },
            "request": {
                "model_name": "bigmodel",
                "show_utterances": True,
                "enable_punc": True,
                "enable_itn": True,
            },
        }
        ws.send_binary(_pack_full_client_request(req_json))
        chunk_size = 8192
        with open(absolute_filepath, 'rb') as f:
            chunk = f.read(chunk_size)
            while chunk:
                next_chunk = f.read(chunk_size)
                is_last = not bool(next_chunk)
                ws.send_binary(_pack_audio_request(chunk, is_last=is_last))
                chunk = next_chunk
                time.sleep(0.01)
        raw_response_data = None
        while True:
            resp = ws.recv()
            if not resp or len(resp) < 4:
                continue
            header_size = (resp[0] & 0x0F) * 4
            msg_type = (resp[1] & 0xF0) >> 4
            msg_flags = resp[1] & 0x0F
            msg_comp = resp[2] & 0x0F
            offset = header_size
            if msg_type == 0x0F:
                err_code = struct.unpack('>I', resp[offset:offset + 4])[0]
                err_size = struct.unpack('>I', resp[offset + 4:offset + 8])[0]
                err_msg = resp[offset + 8:offset + 8 + err_size].decode('utf-8')
                raise Exception(f"流式服务器错误 [{err_code}]: {err_msg}")
            if msg_type == 0x09:
                if msg_flags in [0x01, 0x03]:
                    offset += 4
                payload_size = struct.unpack('>I', resp[offset:offset + 4])[0]
                offset += 4
                payload_compressed = resp[offset:offset + payload_size]
                if msg_comp == 0x01:
                    payload_json = gzip.decompress(payload_compressed).decode('utf-8')
                else:
                    payload_json = payload_compressed.decode('utf-8')
                parsed_res = json.loads(payload_json)
                if "result" in parsed_res:
                    raw_response_data = parsed_res
                if msg_flags in [0x02, 0x03]:
                    break
        if ws:
            ws.close()
        if not raw_response_data:
            raise Exception("未获取到有效的语音识别结果。")
        raw_filepath = os.path.join(RAW_FOLDER, f"{record_id}_raw.json")
        with open(raw_filepath, 'w', encoding='utf-8') as f:
            json.dump(raw_response_data, f, ensure_ascii=False, indent=2)
        result_dict = raw_response_data.get('result', {})
        full_text = result_dict.get('text', '')
        utterances = result_dict.get('utterances', [])
        sync_segments = [{"start": u.get("start_time", 0) / 1000.0, "end": u.get("end_time", 0) / 1000.0, "text": u.get("text", "")} for u in utterances]
        stt_duration = round(time.time() - start_time, 2)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                UPDATE vocal_records
                SET stt_text = ?, sync_data = ?, stt_time_sec = ?, stt_model = ?, status = 'pending_nlp'
                WHERE id = ?
            ''', (full_text, json.dumps(sync_segments, ensure_ascii=False), stt_duration, STT_CONFIG['resource_id'], record_id))
        _get_executor().submit(_process_nlp_task, record_id)
    except Exception as e:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        print(f"[STT 任务异常] Record ID: {record_id}")
        traceback.print_exc()
        fallback_title = _fallback_title(record_id, "识别异常")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE vocal_records SET status = 'stt_failed', title_with_tags = ?, stt_text = ? WHERE id = ?",
                         (fallback_title, f"STT Error: {str(e)}", record_id))


def _process_nlp_task(record_id):
    start_time = time.time()
    try:
        _update_status(record_id, 'processing_nlp')
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT stt_text, stt_time_sec FROM vocal_records WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if not row or not row[0] or "STT Error" in row[0]:
                raise Exception("无可用文本，已跳过 NLP 分析。")
            stt_text = row[0]
            stt_duration = float(row[1] or 0)
        messages = [
            {"role": "system", "content": "你是一个摘要助手。严格按格式输出：生成10字以内的核心标题，以及2-4个核心关键词。格式必须为：标题 #关键词1 #关键词2"},
            {"role": "user", "content": f"文本内容：\n{stt_text[:3000]}"},
        ]
        payload = {"model": NLP_CONFIG['model'], "messages": messages}
        req = Request(NLP_CONFIG['base_url'],
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json",
                               "Authorization": f"Bearer {NLP_CONFIG['api_key']}"},
                      method="POST")
        try:
            with urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            raise Exception(f"NLP API Error: {e.read().decode('utf-8', 'replace')}")
        except (URLError, OSError) as e:
            raise Exception(f"NLP API Error: {e}")
        title_with_tags = data['choices'][0]['message']['content'].strip()
        nlp_duration = round(time.time() - start_time, 2)
        total_duration = round(stt_duration + nlp_duration, 2)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                UPDATE vocal_records
                SET title_with_tags = ?, nlp_time_sec = ?, processing_time_sec = ?, status = 'completed', nlp_model = ?
                WHERE id = ?
            ''', (title_with_tags, nlp_duration, total_duration, NLP_CONFIG['model'], record_id))
    except Exception as e:
        print(f"[NLP 任务异常] Record ID: {record_id}")
        traceback.print_exc()
        fallback_title = _fallback_title(record_id, "摘要异常")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE vocal_records SET status = 'nlp_failed', title_with_tags = ? WHERE id = ?", (fallback_title, record_id))


# ---------------------------------------------------------------- handlers

def upload_audio(environ, start_response):
    fields, files = parse_multipart(environ)
    if 'file' not in files:
        return json_response(start_response, {"error": "No file"}, 400)
    up_name, blob = files['file']
    original_filename = fields.get('original_filename') or up_name or "recording.webm"
    try:
        duration_sec = float(fields.get('duration', 0))
        if math.isinf(duration_sec) or math.isnan(duration_sec) or duration_sec < 0:
            duration_sec = 0.0
    except (ValueError, TypeError):
        duration_sec = 0.0
    record_id = str(uuid.uuid4())
    ext = os.path.splitext(original_filename)[1]
    if not ext:
        ext = '.webm'
    filepath = os.path.join(UPLOAD_FOLDER, f"{record_id}{ext}")
    with open(filepath, "wb") as fh:
        fh.write(blob)
    stored = os.path.join("audio_uploads", f"{record_id}{ext}")
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    dur_str = f"[{int(duration_sec)}s]" if duration_sec > 0 else ""
    default_title = f"记录_{created_at.replace('-', '').replace(':', '').replace(' ', '_')} {dur_str} #未转录"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO vocal_records (id, filename, filepath, duration_sec, stt_text, sync_data, title_with_tags, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (record_id, original_filename, stored, duration_sec, "", "[]", default_title, "uploaded", created_at))
    return json_response(start_response, {"status": "success", "id": record_id})


def start_processing(environ, start_response):
    data = read_json(environ) or {}
    ids = data.get('ids', [])
    with sqlite3.connect(DB_PATH) as conn:
        for record_id in ids:
            conn.execute("UPDATE vocal_records SET status = 'pending_stt' WHERE id = ?", (record_id,))
            cursor = conn.cursor()
            cursor.execute("SELECT filepath FROM vocal_records WHERE id = ?", (record_id,))
            row = cursor.fetchone()
            if row:
                _get_executor().submit(_process_stt_task, record_id,
                                       os.path.abspath(_resolve_stored_path(row[0])))
    return json_response(start_response, {"status": "started"})


def retry_processing(environ, start_response):
    data = read_json(environ) or {}
    record_id = data.get('id')
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath, status FROM vocal_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if not row:
            return json_response(start_response, {"error": "Not found"}, 404)
        filepath, status = row
        if status == 'nlp_failed':
            conn.execute("UPDATE vocal_records SET status = 'pending_nlp' WHERE id = ?", (record_id,))
            _get_executor().submit(_process_nlp_task, record_id)
        else:
            conn.execute("UPDATE vocal_records SET status = 'pending_stt', stt_text = '' WHERE id = ?", (record_id,))
            _get_executor().submit(_process_stt_task, record_id,
                                   os.path.abspath(_resolve_stored_path(filepath)))
    return json_response(start_response, {"status": "queued_retry"})


def get_audio(environ, start_response, record_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM vocal_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            path = _resolve_stored_path(row[0])
            if Path(path).is_file():
                return serve_file(start_response, path, environ)
    return json_response(start_response, {"error": "Audio not found"}, 404)


def delete_record(environ, start_response, record_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM vocal_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if row:
            try:
                os.remove(_resolve_stored_path(row[0]))
            except OSError:
                pass
            raw_path = os.path.join(RAW_FOLDER, f"{record_id}_raw.json")
            if os.path.exists(raw_path):
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
        conn.execute("DELETE FROM vocal_records WHERE id = ?", (record_id,))
    return json_response(start_response, {"status": "success"})


def get_records(environ, start_response):
    prefix = environ.get("HTTP_X_FORWARDED_PREFIX") or ""
    query = "SELECT id, filename, filepath, title_with_tags, status, stt_text, sync_data, created_at FROM vocal_records ORDER BY created_at DESC"
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
    return json_response(start_response, [{
        "id": r[0],
        "filename": r[1] or '未命名录音',
        "audio_url": f"{prefix}/audiom/api/vocal/audio/{r[0]}",
        "title_with_tags": r[3] or '',
        "status": r[4],
        "stt_text_preview": (r[5][:60] + '...') if r[5] else '',
        "sync_data": json.loads(r[6]) if r[6] else [],
        "created_at": r[7],
    } for r in rows if r[4] != 'uploaded'])
