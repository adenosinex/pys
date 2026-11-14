# -*- coding: utf-8 -*-
"""hike - 徒步预测 (App Hub port of myweb tech/1hike, 2026-11-14).

Route projects hold reference tracks (parsed to lat,lon,dist,time CSVs).
The page matches the hiker's current GPS position (or pasted coordinates)
against the references, then projects remaining time from the pace ratio
between elapsed time and the reference's time at the same distance.

Port notes (vs the myweb original):
- Storage resolves via the platform data dir (DATA_DIR env, else ./data
  symlink, else .runtime); the blueprint is served at the app root, so the
  page calls the API with a relative 'api' prefix (works behind /web/).
- No pandas: /api/predict reads CSVs with the stdlib csv module and the
  predictor works on plain lists (identical matching semantics).
- Uploads accept GPX (original parser, untouched) plus KML/KMZ: a KML
  Track's when/coord pairs are matched in document order (a timestamp with
  no following coordinate, e.g. OpenTracks segment markers, is skipped),
  then written through the same CSV writer as GPX.
- The one-shot CSV regeneration helper from the original is dropped: it
  was commented out upstream and is a footgun on restart.
- No auth in this project: identity verification lives only in the panel
  platform layer (App Hub auth placement rule).
"""
import base64
import csv
import io
import math
import os
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

from flask import Flask, jsonify, render_template, request, send_file


def _resolve_data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    link = os.path.join(here, "data")
    if os.path.islink(link) or os.path.isdir(link):
        return link
    return os.path.join(here, ".runtime")


BASE_DIR = _resolve_data_dir()
PROJECTS_BASE_DIR = os.path.join(BASE_DIR, "projects")
DB_FILE = os.path.join(BASE_DIR, "hike_projects.db")

for path in [BASE_DIR, PROJECTS_BASE_DIR]:
    os.makedirs(path, exist_ok=True)

app = Flask(__name__)
application = app


def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gpx_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                original_name TEXT NOT NULL,
                gpx_path TEXT NOT NULL,
                csv_path TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects (id)
            )
        """)


init_db()


# ================= 工具函数 =================
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _parse_iso_time(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _gpx_points(gpx_path):
    it = ET.iterparse(gpx_path)
    for _, el in it:
        el.tag = el.tag.split("}", 1)[1] if "}" in el.tag else el.tag
    root = it.root
    points = []
    for trkpt in root.iter("trkpt"):
        lat = float(trkpt.get("lat"))
        lon = float(trkpt.get("lon"))
        time_el = trkpt.find("time")
        if time_el is None or not time_el.text:
            continue
        dt = _parse_iso_time(time_el.text)
        if dt is None:
            continue
        ele_el = trkpt.find("ele")
        ele = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
        points.append((lat, lon, dt, ele))
    return points


def _kml_track_points(kml_bytes):
    root = _strip_ns(ET.fromstring(kml_bytes))
    points = []
    for track in root.iter("Track"):
        pending_time = None
        for child in track:
            if child.tag == "when":
                pending_time = _parse_iso_time((child.text or "").strip())
            elif child.tag == "coord":
                parts = (child.text or "").strip().split()
                if len(parts) < 2 or pending_time is None:
                    continue
                try:
                    lon = float(parts[0])
                    lat = float(parts[1])
                    ele = float(parts[2]) if len(parts) > 2 else 0.0
                except ValueError:
                    continue
                points.append((lat, lon, pending_time, ele))
                pending_time = None
    return points


def _kmz_points(kmz_path):
    with zipfile.ZipFile(kmz_path) as zf:
        names = zf.namelist()
        if len(names) > 2000:
            raise ValueError("ZIP entries too many")
        chosen = None
        for name in names:
            if name.lower().endswith(".kml") and not name.endswith("/"):
                if name.lower() == "doc.kml":
                    chosen = name
                    break
                if chosen is None:
                    chosen = name
        if chosen is None:
            raise ValueError("no KML inside KMZ")
        info = zf.getinfo(chosen)
        if info.file_size > 70 * 1024 * 1024:
            raise ValueError("KML too large")
        return _kml_track_points(zf.read(chosen))


def _write_csv(points, csv_path):
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["lat", "lon", "distance_km", "time_hours", "elevation_m", "timestamp"])
        total_dist = 0.0
        start_time = points[0][2]
        writer.writerow([points[0][0], points[0][1], 0.0, 0.0, points[0][3], int(start_time.timestamp())])
        for i in range(1, len(points)):
            lat1, lon1, t1, _ = points[i - 1]
            lat2, lon2, t2, ele2 = points[i]
            dist = haversine(lat1, lon1, lat2, lon2)
            total_dist += dist
            time_diff = (t2 - start_time).total_seconds() / 3600.0
            writer.writerow([lat2, lon2, round(total_dist, 4), round(time_diff, 4), round(ele2, 1), int(t2.timestamp())])


def parse_track_to_csv(src_path, csv_path):
    """Parse a GPX/KML/KMZ track file into the reference CSV. Returns bool."""
    try:
        lower = src_path.lower()
        if lower.endswith(".kmz"):
            points = _kmz_points(src_path)
        elif lower.endswith(".kml"):
            with open(src_path, "rb") as f:
                points = _kml_track_points(f.read())
        else:
            points = _gpx_points(src_path)
        if not points:
            return False
        _write_csv(points, csv_path)
        return True
    except Exception as e:
        print(f"track parse error: {e}")
        return False


def parse_gpx_to_csv(gpx_path, csv_path):
    return parse_track_to_csv(gpx_path, csv_path)


def predict_remaining_time(records, current_distance, current_time):
    """records: [{name, rows: [(distance_km, time_hours), ...]}]. Same matching
    semantics as the original pandas version: nearest distance within 0.5 km,
    reference time > 0.01 h, pick the record whose reference time at that
    distance is closest to the hiker's elapsed time."""
    best_match = None
    min_time_diff = float("inf")
    ref_current_time = 0.0
    for record in records:
        rows = record["rows"]
        if not rows or max(d for d, _ in rows) < current_distance:
            continue
        idx = min(range(len(rows)), key=lambda i: abs(rows[i][0] - current_distance))
        closest_distance, gpx_time_at_dist = rows[idx]
        if abs(closest_distance - current_distance) > 0.5:
            continue
        if gpx_time_at_dist <= 0.01:
            continue
        time_diff = abs(current_time - gpx_time_at_dist)
        if time_diff < min_time_diff:
            min_time_diff = time_diff
            best_match = record
            ref_current_time = gpx_time_at_dist
    if best_match is None:
        return 0.0, None
    ref_total_time = best_match["rows"][-1][1]
    ref_remaining_time = ref_total_time - ref_current_time
    pace_ratio = current_time / ref_current_time
    return ref_remaining_time * pace_ratio, best_match["name"]


# ================= 路由接口 =================
@app.route("/")
def index():
    return render_template("hike_index.html")


@app.route("/api/projects", methods=["GET", "POST"])
def handle_projects():
    if request.method == "POST":
        name = request.json.get("name")
        if not name:
            return jsonify({"error": "名称不能为空"}), 400
        try:
            with get_db() as conn:
                cursor = conn.execute("INSERT INTO projects (name) VALUES (?)", (name,))
                return jsonify({"id": cursor.lastrowid, "name": name})
        except sqlite3.IntegrityError:
            return jsonify({"error": "项目已存在"}), 400
    with get_db() as conn:
        projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return jsonify([dict(p) for p in projects])


@app.route("/api/projects/<int:project_id>/records", methods=["GET"])
def get_project_records(project_id):
    with get_db() as conn:
        records = conn.execute(
            "SELECT id, original_name, gpx_path FROM gpx_records WHERE project_id = ? ORDER BY id DESC",
            (project_id,),
        ).fetchall()
    result = []
    for r in records:
        r_dict = dict(r)
        size_str = "未知大小"
        if os.path.exists(r_dict["gpx_path"]):
            size_bytes = os.path.getsize(r_dict["gpx_path"])
            if size_bytes < 1024 * 1024:
                size_str = f"{size_bytes / 1024:.1f} KB"
            else:
                size_str = f"{size_bytes / (1024 * 1024):.2f} MB"
        r_dict["size_formatted"] = size_str
        del r_dict["gpx_path"]
        result.append(r_dict)
    return jsonify(result)


@app.route("/api/download/<int:record_id>", methods=["GET"])
def download_gpx(record_id):
    with get_db() as conn:
        record = conn.execute(
            "SELECT gpx_path, original_name FROM gpx_records WHERE id = ?", (record_id,)
        ).fetchone()
    if record and os.path.exists(record["gpx_path"]):
        return send_file(record["gpx_path"], as_attachment=True, download_name=record["original_name"])
    return jsonify({"error": "文件不存在"}), 404


@app.route("/api/upload", methods=["POST"])
def upload_gpx():
    data = request.json
    project_id = data.get("project_id")
    filename = data.get("filename")
    file_base64 = data.get("file_base64")
    if not all([project_id, filename, file_base64]):
        return jsonify({"error": "参数不全"}), 400
    if "," in file_base64:
        file_base64 = file_base64.split(",")[1]
    project_dir = os.path.join(PROJECTS_BASE_DIR, str(project_id))
    os.makedirs(project_dir, exist_ok=True)
    timestamp = int(datetime.now().timestamp())
    safe_name = filename.replace(" ", "_")
    gpx_path = os.path.join(project_dir, f"{timestamp}_{safe_name}")
    csv_path = os.path.join(project_dir, f"{timestamp}_{safe_name}.csv")
    try:
        with open(gpx_path, "wb") as f:
            f.write(base64.b64decode(file_base64))
    except Exception as e:
        return jsonify({"error": f"写入失败: {str(e)}"}), 500
    if parse_track_to_csv(gpx_path, csv_path):
        with get_db() as conn:
            conn.execute(
                "INSERT INTO gpx_records (project_id, original_name, gpx_path, csv_path) VALUES (?, ?, ?, ?)",
                (project_id, filename, gpx_path, csv_path),
            )
        return jsonify({"message": "OK"})
    else:
        if os.path.exists(gpx_path):
            os.remove(gpx_path)
        if os.path.exists(csv_path):
            os.remove(csv_path)
        return jsonify({"error": "格式不正确"}), 500


@app.route("/api/csv/<int:record_id>", methods=["GET"])
def get_csv_file(record_id):
    with get_db() as conn:
        record = conn.execute("SELECT csv_path FROM gpx_records WHERE id = ?", (record_id,)).fetchone()
    if record and os.path.exists(record["csv_path"]):
        return send_file(record["csv_path"], mimetype="text/csv")
    return jsonify({"error": "CSV 文件不存在"}), 404


@app.route("/api/predict", methods=["POST"])
def predict_time():
    data = request.json
    project_id = data.get("project_id")
    current_dist = float(data.get("current_distance", 0))
    current_time = float(data.get("current_time", 0))
    if current_dist <= 0 or current_time <= 0:
        return jsonify({"error": "数据需大于0"}), 400
    with get_db() as conn:
        records = conn.execute(
            "SELECT original_name, csv_path FROM gpx_records WHERE project_id = ?", (project_id,)
        ).fetchall()
    if not records:
        return jsonify({"error": "该项目暂无参考轨迹"}), 400
    loaded = []
    for row in records:
        if os.path.exists(row["csv_path"]):
            rows = []
            with open(row["csv_path"], newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    try:
                        rows.append((float(r["distance_km"]), float(r["time_hours"])))
                    except (KeyError, ValueError):
                        continue
            loaded.append({"name": row["original_name"], "rows": rows})
    predicted_rem, matched_name = predict_remaining_time(loaded, current_dist, current_time)
    if predicted_rem == 0.0:
        return jsonify({"error": "当前距离超出历史轨迹总长，或起点偏差过大"}), 400
    return jsonify(
        {
            "predicted_remaining_hours": round(predicted_rem, 2),
            "total_estimated_hours": round(current_time + predicted_rem, 2),
            "reference_gpx": matched_name,
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=18099)
