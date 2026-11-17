"""Daily records, ported from the user's myweb blueprint
(1daily_extension). Uses one SQLite table (universal_records) in
.runtime/universal_stats.db, exactly like the original shared
db/universal_stats.db. Logic preserved; pytz became zoneinfo and
Flask responses became webutil calls. (The 4fuel_extension moved to
the standalone fuel light app in 2026-11; its rows now live in
PocketBase.)"""
import csv
import io
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from webutil import json_response, read_json, parse_multipart, csv_response

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

DB_FILE = os.path.join(_platform_data_dir(BASE), "universal_stats.db")
TZ = ZoneInfo("Asia/Shanghai")

_last_add_time = ""


def _conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    with closing(_conn()) as conn, conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS universal_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                record_time TEXT NOT NULL,
                val1 REAL,
                val2 REAL,
                remark TEXT
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cat_time ON universal_records(category, record_time)')


# ---------------------------------------------------------------- daily

def _daily_add(project, addtime, count=None, remark_extra=""):
    global _last_add_time
    if _last_add_time == addtime:
        return
    with closing(_conn()) as conn, conn:
        r = conn.execute(
            "SELECT record_time FROM universal_records "
            "WHERE category='daily_stats' AND remark LIKE '%auto%' "
            "ORDER BY record_time DESC LIMIT 1"
        ).fetchone()
        if r:
            last_ts = datetime.strptime(r['record_time'], '%Y-%m-%d %H:%M:%S').timestamp()
            curr_ts = datetime.strptime(addtime, '%Y-%m-%d %H:%M:%S').timestamp()
            if (curr_ts - last_ts) < 10:
                return
        final_remark = f"{project} | {remark_extra}" if remark_extra else project
        conn.execute(
            "INSERT INTO universal_records (category, record_time, val1, remark) VALUES (?, ?, ?, ?)",
            ('daily_stats', addtime, count, final_remark)
        )
        _last_add_time = addtime


def _daily_stats(descending=True):
    direction = 'DESC' if descending else 'ASC'
    with closing(_conn()) as conn, conn:
        rows = conn.execute(
            "SELECT id, record_time, val1 as count, remark "
            f"FROM universal_records WHERE category='daily_stats' ORDER BY record_time {direction}, id {direction}"
        ).fetchall()
    stats = []
    for row in rows:
        time_str = row['record_time']
        count_val = int(row['count']) if row['count'] is not None else ""
        full_remark = row['remark'] or ""
        if " | " in full_remark:
            project, actual_remark = full_remark.split(" | ", 1)
        else:
            project, actual_remark = full_remark, ""
        stats.append({
            "id": row['id'],
            "_time": time_str,
            "date": time_str[:10],
            "time": time_str[11:],
            "project": project,
            "count": count_val,
            "remark": actual_remark
        })
    return stats


def daily_main(environ, start_response):
    if environ["REQUEST_METHOD"] == "POST":
        data = read_json(environ) or {}
        time_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
        _daily_add(
            project=data.get('project', '默认项目'),
            addtime=time_str,
            count=data.get('count'),
            remark_extra=data.get('remark', '')
        )
        return json_response(start_response, {'status': 'ok'})
    return json_response(start_response, _daily_stats())


def daily_delete(environ, start_response, record_id):
    with closing(_conn()) as conn, conn:
        cursor = conn.execute(
            "DELETE FROM universal_records WHERE id=? AND category='daily_stats'",
            (record_id,))
        success = cursor.rowcount > 0
    return json_response(start_response, {'status': 'ok', 'deleted': success})


def daily_export(environ, start_response):
    stats = _daily_stats(descending=False)
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['Time', 'Project', 'Count', 'Remark'])
    for item in stats:
        cw.writerow([item['_time'], item['project'], item['count'], item['remark']])
    return csv_response(start_response, '﻿' + si.getvalue(), "daily_stats.csv")


def _parse_daily_csv_row(row):
    def value(*keys):
        return next((row[key] for key in keys if key in row), None)

    timestamp = value('Time', 'time', '时间')
    project = value('Project', 'project', '项目')
    count = value('Count', 'count', '次数')
    remark = value('Remark', 'remark', '备注') or ''
    if not timestamp or not project:
        return None
    normalized = timestamp.replace('/', '-').replace('T', ' ').strip()
    if ':' not in normalized:
        normalized += ' 00:00:00'
    elif len(normalized.split(':')) == 2:
        normalized += ':00'
    normalized = datetime.strptime(normalized, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
    count_num = float(count) if count and count.strip() else None
    return ('daily_stats', normalized, count_num, f'{project} | {remark}' if remark else project)


def daily_import(environ, start_response):
    _fields, files = parse_multipart(environ)
    if 'file' not in files:
        return json_response(start_response, {'error': 'No file'}, 400)
    blob = files['file'][1]
    try:
        raw_content = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            raw_content = blob.decode("gbk")
        except UnicodeDecodeError:
            return json_response(start_response, {'error': '文件编码错误'}, 400)
    first_line = raw_content.split('\n')[0]
    delimiter = '\t' if '\t' in first_line else ','
    csv_input = csv.DictReader(io.StringIO(raw_content), delimiter=delimiter)
    records = []
    for row in csv_input:
        try:
            record = _parse_daily_csv_row(row)
            if record is not None:
                records.append(record)
        except (ValueError, TypeError):
            pass
    if records:
        with closing(_conn()) as conn, conn:
            conn.executemany(
                "INSERT INTO universal_records (category, record_time, val1, remark) VALUES (?, ?, ?, ?)",
                records)
    return json_response(start_response, {'status': 'ok', 'imported_rows': len(records)})
