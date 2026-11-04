"""Snippets, ported from the user's myweb 6snippets blueprint.
Storage: .runtime/snippets.db (same schema as the original)."""
import json
import os
import sqlite3

from webutil import json_response, read_json

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

DB_PATH = os.path.join(_platform_data_dir(BASE), "snippets.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS snippets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_snippets(environ, start_response):
    conn = _conn()
    rows = conn.execute('SELECT id, title, created_at FROM snippets ORDER BY created_at DESC').fetchall()
    conn.close()
    return json_response(start_response, {"snippets": [dict(s) for s in rows]})


def get_snippet(environ, start_response, snippet_id):
    conn = _conn()
    row = conn.execute('SELECT id, title, content FROM snippets WHERE id = ?', (snippet_id,)).fetchone()
    conn.close()
    if row is None:
        return json_response(start_response, {"error": "记录不存在"}, 404)
    return json_response(start_response, dict(row))


def save_snippet(environ, start_response):
    data = read_json(environ) or {}
    snippet_id = data.get('id')
    title = str(data.get('title', '')).strip()
    content = str(data.get('content', ''))
    if not title:
        return json_response(start_response, {"error": "标题不能为空"}, 400)
    conn = _conn()
    try:
        if snippet_id:
            conn.execute('UPDATE snippets SET title = ?, content = ? WHERE id = ?',
                         (title, content, snippet_id))
        else:
            conn.execute('INSERT INTO snippets (title, content) VALUES (?, ?)',
                         (title, content))
        conn.commit()
        return json_response(start_response, {"message": "保存成功"})
    except Exception as e:
        return json_response(start_response, {"error": str(e)}, 500)
    finally:
        conn.close()


def delete_snippet(environ, start_response, snippet_id):
    conn = _conn()
    try:
        cursor = conn.execute('DELETE FROM snippets WHERE id = ?', (snippet_id,))
        conn.commit()
        if cursor.rowcount == 0:
            return json_response(start_response, {"error": "记录不存在"}, 404)
        return json_response(start_response, {"message": "删除成功"})
    except Exception as e:
        return json_response(start_response, {"error": str(e)}, 500)
    finally:
        conn.close()


def _download(start_response, payload, filename):
    body = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
    start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                              ("Content-Disposition", 'attachment; filename="%s"' % filename),
                              ("Content-Length", str(len(body)))])
    return [body]


def export_snippets(environ, start_response):
    conn = _conn()
    rows = conn.execute("SELECT title, content, created_at FROM snippets ORDER BY created_at").fetchall()
    conn.close()
    return _download(start_response,
                     {"format": "snippets-backup", "version": 1,
                      "items": [dict(r) for r in rows]},
                     "snippets-backup.json")


def import_snippets(environ, start_response):
    data = read_json(environ)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return json_response(start_response, {"ok": False, "error": "备份格式不对。"}, 400)
    conn = _conn()
    existing = {(r[0], r[1]) for r in conn.execute("SELECT title, content FROM snippets").fetchall()}
    added = skipped = 0
    for it in items:
        if not isinstance(it, dict):
            skipped += 1
            continue
        title = str(it.get("title") or "").strip()
        content = str(it.get("content") or "")
        if not title or not content or (title, content) in existing:
            skipped += 1
            continue
        created = it.get("created_at")
        if isinstance(created, str) and len(created) <= 32:
            conn.execute("INSERT INTO snippets (title, content, created_at) VALUES (?, ?, ?)",
                         (title, content, created))
        else:
            conn.execute("INSERT INTO snippets (title, content) VALUES (?, ?)", (title, content))
        existing.add((title, content))
        added += 1
    conn.commit()
    conn.close()
    return json_response(start_response, {"ok": True, "added": added, "skipped": skipped})
