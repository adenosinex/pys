"""The /api2 universal store, ported verbatim in behaviour from the
user's myweb core (app.py): append-only JSON collections plus an
expiring key-value store, backed by .runtime/universal_data.db.
Several record pages (product, supplies, exercise) are built on it."""
import json
import os
import sqlite3
import time

from webutil import json_response, read_json

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, ".runtime", "universal_data.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS store (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                payload TEXT NOT NULL,
                create_time DATETIME DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS kv_store (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL,
                expire_at REAL
            )
        ''')


def collection_main(environ, start_response, collection):
    if environ["REQUEST_METHOD"] == "POST":
        data = read_json(environ)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('INSERT INTO store (collection, payload) VALUES (?, ?)',
                         (collection, json.dumps(data, ensure_ascii=False)))
        return json_response(start_response, {"status": "success"})
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT payload, create_time FROM store WHERE collection=? ORDER BY id DESC LIMIT 50',
                       (collection,))
        rows = cursor.fetchall()
    result = []
    for row in rows:
        item = json.loads(row[0])
        if isinstance(item, dict):
            item['_time'] = row[1]
        result.append(item)
    return json_response(start_response, result)


def kv_main(environ, start_response, key):
    if environ["REQUEST_METHOD"] == "POST":
        data = read_json(environ) or {}
        payload = json.dumps(data.get('payload', {}), ensure_ascii=False)
        expire_at = data.get('expire_at')
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('INSERT OR REPLACE INTO kv_store (k, v, expire_at) VALUES (?, ?, ?)',
                         (key, payload, expire_at))
        return json_response(start_response, {"status": "success"})
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT v, expire_at FROM kv_store WHERE k=?', (key,))
        row = cursor.fetchone()
        if row:
            v, expire_at = row
            if expire_at and time.time() > expire_at:
                conn.execute('DELETE FROM kv_store WHERE k=?', (key,))
                conn.commit()
                return json_response(start_response, {"error": "提取码已过期，数据已永久销毁"}, 404)
            return json_response(start_response, json.loads(v))
        return json_response(start_response, {"error": "提取码不存在或已被销毁"}, 404)
