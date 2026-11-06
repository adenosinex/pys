"""MedAtlas 医学图谱 — personal medical knowledge learning app for the App Hub.

Single-file stdlib WSGI app. Knowledge nodes, directed typed relations,
personal notes, reference sources, learning status and simple review
scheduling live in one SQLite file inside the platform data directory
(DATA_DIR env -> ./data symlink -> .runtime fallback), so the panel's
daily backup covers everything. This app contains no identity
verification: the App Hub platform gate in front of /web/medatlas/
performs it centrally, and direct access on the local port is trusted.

Design notes (from the project brief, adapted):
- Verification status is a first-class field: sourced / pending / personal.
  Nothing AI- or hand-written is silently presented as verified.
- Relations are directed and typed from a fixed vocabulary; reverse
  listings never invert the wording, so causality is never flipped.
- Drug price / expiry facts are dated sub-records with their own source,
  never a single invented figure on the generic drug.
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ------------------------------------------------------------------ config

def _data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    link = Path(__file__).resolve().parent / "data"
    if link.exists():
        return link
    rt = Path(__file__).resolve().parent / ".runtime"
    rt.mkdir(parents=True, exist_ok=True)
    return rt


DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "medatlas.db"
SCHEMA_VERSION = 1
EXPORT_FORMAT = "medatlas-export-v1"

CATEGORIES = ["医学基础", "人体结构与生理", "疾病与病理", "药理与药物",
              "症状与鉴别思路", "医学检查与指标"]
NODE_TYPES = {
    "concept": "医学基础概念",
    "disease": "疾病",
    "drug": "药物",
    "system": "结构与生理",
    "symptom": "症状",
    "exam": "检查与指标",
}
# Fixed relation vocabulary: key -> label shown on the source side.
# Reverse listings reuse the same label with an incoming arrow, so the
# direction of the statement is never flipped.
REL_TYPES = {
    "involves": "涉及",
    "belongs": "属于",
    "participates": "参与",
    "manifests_as": "可表现为",
    "causes": "可能导致",
    "increases_risk": "增加风险",
    "acts_on": "作用于",
    "indicated_for": "用于",
    "metabolized_in": "主要代谢于",
    "differential": "需与之鉴别",
    "examined_by": "可通过检查",
}
STATUSES = {"unlearned": "未学习", "learning": "学习中",
            "review": "待复习", "mastered": "已掌握"}
VERIFY = {"sourced": "有来源", "pending": "待核验", "personal": "个人整理"}
DRUG_FIELDS = [
    ("drug_class", "药物类别"), ("mechanism", "作用机制"),
    ("indications", "适应证"), ("dosage_forms", "常见剂型与规格"),
    ("adverse", "常见不良反应"), ("contraindications", "禁忌与重要注意事项"),
    ("interactions", "药物相互作用"), ("special_populations", "特殊人群注意事项"),
    ("overdose_risk", "过量使用的风险"), ("longterm_risk", "长期或反复使用的风险"),
    ("red_flags", "需要就医的警示情况"),
]

# ------------------------------------------------------------------ db

_tls = threading.local()
_init_lock = threading.Lock()
_init_done = False


def db():
    # One connection per thread: the panel launcher serves with several
    # threads, and a sqlite3 connection may only be used in its own thread.
    global _init_done
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        with _init_lock:
            if not _init_done:
                _init_db(conn)
                _init_done = True
        _tls.conn = conn
    return conn


def _init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS nodes (
        id TEXT PRIMARY KEY, type TEXT NOT NULL, category TEXT NOT NULL,
        title TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]',
        summary TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
        fields TEXT NOT NULL DEFAULT '{}', tags TEXT NOT NULL DEFAULT '[]',
        difficulty INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'unlearned',
        verify TEXT NOT NULL DEFAULT 'pending', favorite INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0, next_review_at REAL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS relations (
        id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
        rel_type TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
        verify TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
        UNIQUE(source_id, target_id, rel_type));
    CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, publisher TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '', pub_date TEXT NOT NULL DEFAULT '',
        accessed_at TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS node_sources (
        node_id TEXT NOT NULL, source_id TEXT NOT NULL,
        PRIMARY KEY (node_id, source_id));
    CREATE TABLE IF NOT EXISTS notes (
        id TEXT PRIMARY KEY, node_id TEXT NOT NULL, content TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY, node_id TEXT NOT NULL, reviewed_at REAL NOT NULL,
        result TEXT NOT NULL, next_at REAL);
    CREATE TABLE IF NOT EXISTS questions (
        id TEXT PRIMARY KEY, qtype TEXT NOT NULL DEFAULT 'single', question TEXT NOT NULL,
        options TEXT NOT NULL DEFAULT '[]', answer TEXT NOT NULL DEFAULT '',
        explanation TEXT NOT NULL DEFAULT '', node_ids TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL);
    """)
    conn.commit()
    row = conn.execute("SELECT v FROM meta WHERE k='seeded'").fetchone()
    if not row:
        _seed(conn)
        conn.execute("INSERT INTO meta (k, v) VALUES ('seeded', '1')")
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.commit()


def _nid():
    return uuid.uuid4().hex[:16]


def _now():
    return time.time()


# ------------------------------------------------------------------ seed

def _seed(conn):
    t = _now()
    _ctr = [0]

    def next_seed_id():
        _ctr[0] += 1
        return "seed%012d" % _ctr[0]

    src_msddx = next_seed_id()
    src_label = next_seed_id()
    conn.execute(
        "INSERT INTO sources (id, title, publisher, url, pub_date, accessed_at, notes, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (src_msddx, "默沙东诊疗手册（大众版）", "默沙东", "", "", "", "公开医学教育资料，条目内容以该手册对应主题为准。", t))
    conn.execute(
        "INSERT INTO sources (id, title, publisher, url, pub_date, accessed_at, notes, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (src_label, "药品说明书（国家药品监督管理局核准）", "国家药品监督管理局", "", "", "",
         "药物条目的机制、不良反应、禁忌等以所用产品说明书为准；不同厂家说明书细节可能略有差异。", t))

    def node(type_, cat, title, aliases, summary, body, tags, verify="pending", fields=None, diff=1):
        nid = next_seed_id()
        conn.execute(
            "INSERT INTO nodes (id, type, category, title, aliases, summary, body, fields, tags,"
            " difficulty, status, verify, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'unlearned', ?, ?, ?)",
            (nid, type_, cat, title, json.dumps(aliases, ensure_ascii=False), summary, body,
             json.dumps(fields or {}, ensure_ascii=False), json.dumps(tags, ensure_ascii=False),
             diff, verify, t, t))
        return nid

    fever = node("concept", "医学基础", "发热", ["发烧"],
                 "体温调节中枢调定点上移引起的体温升高，是许多疾病的共同表现而非疾病本身。",
                 "发热是下丘脑体温调节中枢在致热原作用下调定点上移的结果。理解发热要区分「体温升高」与「过热」：前者是调定点改变，后者是散热障碍。发热本身是症状线索，退热的目标是舒适而非把体温降到正常。",
                 ["症状", "基础概念"])
    inflam = node("concept", "医学基础", "炎症", ["发炎"],
                  "机体对损伤或感染的防御性反应，典型表现为红、肿、热、痛和功能障碍。",
                  "炎症是血管和免疫系统对刺激的程序化反应：血管扩张、通透性增加、白细胞渗出。急性炎症多由感染或创伤引起；慢性炎症可持续数月以上，与多种慢性病相关。炎症是防御机制，但过度或持续的炎症本身也会损伤组织。",
                  ["基础概念", "病理"])
    infect = node("concept", "医学基础", "感染", [],
                  "病原体侵入机体并繁殖，可引起局部或全身反应；感染不等于一定发病。",
                  "感染的结果取决于病原体的毒力、数量与宿主免疫状态。感染可以无症状、可以自愈，也可以进展为全身性反应。抗菌药物只对细菌等特定病原体有效，对病毒感染无效——这是区分「感染」与「细菌感染」的关键。",
                  ["基础概念", "病理"])
    thermo = node("system", "人体结构与生理", "体温调节", [],
                  "下丘脑通过产热与散热的平衡维持核心体温相对稳定。",
                  "体温调节中枢位于下丘脑：冷时通过寒战、血管收缩增加产热并减少散热；热时通过出汗、血管扩张散热。调定点被致热原上移时，机体按新调定点升温，于是出现畏寒、寒战等发热起始表现。",
                  ["生理"])
    liver = node("system", "人体结构与生理", "肝脏", [],
                   "人体最大的实质性脏器，承担代谢、合成、解毒与胆汁分泌等功能。",
                   "肝脏参与糖、脂肪、蛋白质代谢，合成白蛋白和凝血因子，灭活与转化许多药物和毒物（药物代谢的主要场所），并生成胆汁。肝功能受损时这些环节可同时出现问题，因此肝损伤的表现是系统性的。",
                   ["生理", "器官"])
    metab = node("concept", "药理与药物", "药物代谢", ["生物转化"],
                 "机体对药物的化学转化过程，主要在肝脏进行，决定药物作用持续时间与蓄积风险。",
                 "药物代谢通常使药物失活并便于排泄，少数药物经代谢转化为活性或毒性产物。代谢能力受年龄、肝功能、遗传和合并用药影响；代谢被抑制或诱导是药物相互作用的主要来源之一。",
                 ["药理", "基础概念"])
    liverinj = node("disease", "疾病与病理", "肝损伤", ["药物性肝损伤"],
                    "各种原因导致的肝细胞损伤或肝功能异常，药物是常见原因之一。",
                    "肝损伤可由病毒、酒精、药物、自身免疫等多种原因引起。药物性肝损伤与剂量、个体易感性有关，多数停药后可恢复，但严重时可进展。出现明显黄疸、乏力加重、尿色深等表现时应及时就医评估。",
                    ["疾病", "风险"], verify="pending")
    uri = node("disease", "疾病与病理", "急性上呼吸道感染", ["感冒", "普通感冒"],
               "鼻、咽、喉等上呼吸道的急性感染，多数由病毒引起，常可自愈。",
               "普通感冒以鼻塞、流涕、喷嚏、咽痛、低热为主要表现，病程多为一周左右自限。治疗以缓解症状为主；抗菌药物对病毒性感冒无效。症状加重、高热持续或出现呼吸困难时需要就医评估，以排除其他疾病。",
               ["疾病", "感染"])
    pain = node("symptom", "症状与鉴别思路", "疼痛", [],
                "组织损伤或潜在损伤相关的不愉快感觉，是就医最常见的原因之一。",
                "记录疼痛时应关注部位、性质、持续时间、诱发与缓解因素。疼痛是症状而非诊断：同样的部位疼痛可能来自完全不同的机制，止痛处理不能替代对原因的评估。突发剧烈疼痛、伴随意识或循环异常的疼痛属于警示情况。",
                ["症状"])
    apap = node("drug", "药理与药物", "对乙酰氨基酚", ["扑热息痛", "Paracetamol"],
                "常用的解热镇痛药，无明显抗炎作用；过量可造成肝损伤，是需要重点理解风险边界的药物。",
                "对乙酰氨基酚通过中枢机制退热和缓解轻中度疼痛。治疗剂量下安全性较好，但过量（包括同时服用多种含此成分的复方制剂导致的叠加过量）可产生毒性代谢产物，造成肝损伤。阅读复方感冒药成分表、避免重复用药是关键。",
                ["药物", "解热镇痛"], verify="sourced", fields={
                    "drug_class": "解热镇痛药（非甾体抗炎药之外的解热镇痛成分）",
                    "mechanism": "主要通过中枢神经系统抑制前列腺素合成而退热、镇痛；外周抗炎作用弱。",
                    "indications": "发热；轻至中度疼痛（如头痛、牙痛、肌肉痛）。",
                    "dosage_forms": "片剂、胶囊、颗粒、口服液、栓剂等多种规格；具体规格以所购产品说明书为准。",
                    "adverse": "治疗剂量下较少；偶见皮疹等过敏反应。",
                    "contraindications": "对本品过敏者禁用；严重肝功能不全者慎用/禁用（以说明书为准）。饮酒者、肝病患者风险更高。",
                    "interactions": "多种复方感冒药含本成分，合用易叠加过量；与酒精合用增加肝损伤风险。",
                    "special_populations": "儿童按体重计算剂量（以说明书为准）；老年人及肝功能异常者需谨慎。",
                    "overdose_risk": "过量可致严重肝损伤甚至肝衰竭，早期症状可能不明显；怀疑过量应立即就医，不要等待症状出现。",
                    "longterm_risk": "长期大剂量使用增加肝损伤风险；不要超过说明书的最大日剂量与疗程。",
                    "red_flags": "服药后出现黄疸、尿色加深、恶心呕吐加重、右上腹痛，或怀疑过量时，立即就医。",
                    "price_records": [], "expiry_records": []})
    ibu = node("drug", "药理与药物", "布洛芬", ["Ibuprofen"],
               "非甾体抗炎药，兼具解热、镇痛和抗炎作用；主要风险在胃肠道和肾脏。",
               "布洛芬通过抑制环氧化酶减少前列腺素合成。与对乙酰氨基酚相比，它有抗炎作用，但对胃黏膜的刺激和肾脏影响更突出，宜餐后服用。脱水、心肾功能不全者使用时风险增加。",
               ["药物", "解热镇痛", "NSAID"], verify="sourced", fields={
                    "drug_class": "非甾体抗炎药（NSAID）",
                    "mechanism": "抑制环氧化酶（COX），减少前列腺素合成，从而解热、镇痛、抗炎。",
                    "indications": "发热；轻至中度疼痛；炎症性疼痛（如关节痛）。",
                    "dosage_forms": "片剂、缓释胶囊、混悬液等；具体规格以所购产品说明书为准。",
                    "adverse": "胃部不适、恶心；长期或大剂量可致胃黏膜损伤、溃疡甚至出血；可能影响肾功能。",
                    "contraindications": "活动性消化性溃疡/出血、严重心肾功能不全、对 NSAID 过敏者禁用（以说明书为准）；哮喘患者慎用。",
                    "interactions": "与其他 NSAID、抗凝药、酒精合用增加出血风险；与某些降压药合用可能削弱降压效果。",
                    "special_populations": "儿童有专用剂型与剂量（以说明书为准）；老年人胃肠道和肾脏风险更高。",
                    "overdose_risk": "过量可致胃肠道出血、肾损伤、意识障碍；怀疑过量立即就医。",
                    "longterm_risk": "长期使用增加胃肠出血、心血管事件和肾损伤风险；用最低有效剂量、最短疗程。",
                    "red_flags": "黑便、呕血、明显胃痛、尿量明显减少或水肿时，立即停药并就医。",
                    "price_records": [], "expiry_records": []})
    for nid_, sid in ((apap, src_label), (ibu, src_label), (fever, src_msddx), (uri, src_msddx)):
        conn.execute("INSERT OR IGNORE INTO node_sources (node_id, source_id) VALUES (?,?)", (nid_, sid))

    def rel(a, b, typ, note="", verify="pending"):
        conn.execute(
            "INSERT OR IGNORE INTO relations (id, source_id, target_id, rel_type, note, verify, created_at)"
            " VALUES (?,?,?,?,?,?,?)", (next_seed_id(), a, b, typ, note, verify, t))

    rel(fever, thermo, "involves", "发热的本质是调定点上移")
    rel(inflam, fever, "manifests_as", "炎症常伴发热")
    rel(infect, inflam, "causes", "感染是炎症的常见原因")
    rel(uri, fever, "manifests_as", "可伴低热")
    rel(apap, fever, "indicated_for", "退热")
    rel(apap, pain, "indicated_for", "缓解轻中度疼痛")
    rel(apap, liverinj, "causes", "过量使用时", "sourced")
    rel(apap, liver, "metabolized_in", "主要经肝脏代谢，毒性代谢产物与肝损伤相关")
    rel(liver, metab, "participates", "肝脏是药物代谢的主要场所")
    rel(ibu, inflam, "acts_on", "抑制前列腺素合成，减轻炎症")
    rel(ibu, pain, "indicated_for", "镇痛")


# ------------------------------------------------------------------ store

def _loads(text, default):
    try:
        v = json.loads(text or "")
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def node_dict(row, with_extra=False):
    d = {
        "id": row["id"], "type": row["type"], "category": row["category"],
        "title": row["title"], "aliases": _loads(row["aliases"], []),
        "summary": row["summary"], "body": row["body"],
        "fields": _loads(row["fields"], {}), "tags": _loads(row["tags"], []),
        "difficulty": row["difficulty"], "status": row["status"],
        "verify": row["verify"], "favorite": bool(row["favorite"]),
        "archived": bool(row["archived"]), "next_review_at": row["next_review_at"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
    if with_extra:
        conn = db()
        d["sources"] = [dict(r) for r in conn.execute(
            "SELECT s.* FROM sources s JOIN node_sources ns ON ns.source_id = s.id"
            " WHERE ns.node_id = ? ORDER BY s.created_at", (row["id"],))]
        d["relations_out"] = _relations_of(row["id"], "out")
        d["relations_in"] = _relations_of(row["id"], "in")
        d["notes"] = [dict(r) | {"tags": _loads(r["tags"], [])} for r in conn.execute(
            "SELECT * FROM notes WHERE node_id = ? ORDER BY created_at", (row["id"],))]
    return d


def _relations_of(node_id, direction):
    conn = db()
    if direction == "out":
        rows = conn.execute(
            "SELECT r.*, n.title AS other_title FROM relations r"
            " JOIN nodes n ON n.id = r.target_id WHERE r.source_id = ? ORDER BY r.created_at",
            (node_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT r.*, n.title AS other_title FROM relations r"
            " JOIN nodes n ON n.id = r.source_id WHERE r.target_id = ? ORDER BY r.created_at",
            (node_id,)).fetchall()
    return [dict(r) for r in rows]


def list_nodes(q="", category="", tag="", status="", archived=0, limit=300):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM nodes WHERE archived = ? ORDER BY updated_at DESC", (archived,)).fetchall()
    out = []
    ql = q.strip().lower()
    for r in rows:
        d = node_dict(r)
        if category and d["category"] != category:
            continue
        if status and d["status"] != status:
            continue
        if tag and tag not in d["tags"]:
            continue
        if ql:
            hay = " ".join([d["title"], " ".join(d["aliases"]), d["summary"],
                            " ".join(d["tags"]), d["category"]]).lower()
            if ql not in hay:
                continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def get_node(node_id):
    row = db().execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return node_dict(row, with_extra=True) if row else None


def _clean_str(v, maxlen):
    return str(v or "").strip()[:maxlen]


def _clean_list(v, maxlen=200, maxitems=30):
    if not isinstance(v, list):
        return []
    return [str(x).strip()[:maxlen] for x in v if str(x).strip()][:maxitems]


def save_node(data, node_id=None):
    title = _clean_str(data.get("title"), 120)
    if not title:
        return None, "标题不能为空"
    type_ = data.get("type") if data.get("type") in NODE_TYPES else "concept"
    category = data.get("category") if data.get("category") in CATEGORIES else "医学基础"
    verify = data.get("verify") if data.get("verify") in VERIFY else "pending"
    status = data.get("status") if data.get("status") in STATUSES else "unlearned"
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    fields = {str(k)[:40]: (v if isinstance(v, list) else str(v)[:4000])
              for k, v in fields.items()}
    try:
        difficulty = max(1, min(5, int(data.get("difficulty") or 1)))
    except Exception:
        difficulty = 1
    conn = db()
    t = _now()
    if node_id:
        row = conn.execute("SELECT id FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            return None, "知识不存在"
        conn.execute(
            "UPDATE nodes SET type=?, category=?, title=?, aliases=?, summary=?, body=?, fields=?,"
            " tags=?, difficulty=?, status=?, verify=?, favorite=?, updated_at=? WHERE id=?",
            (type_, category, title, json.dumps(_clean_list(data.get("aliases"), 60), ensure_ascii=False),
             _clean_str(data.get("summary"), 600), _clean_str(data.get("body"), 20000),
             json.dumps(fields, ensure_ascii=False),
             json.dumps(_clean_list(data.get("tags"), 40), ensure_ascii=False),
             difficulty, status, verify, 1 if data.get("favorite") else 0, t, node_id))
        nid = node_id
    else:
        nid = _nid()
        conn.execute(
            "INSERT INTO nodes (id, type, category, title, aliases, summary, body, fields, tags,"
            " difficulty, status, verify, favorite, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (nid, type_, category, title, json.dumps(_clean_list(data.get("aliases"), 60), ensure_ascii=False),
             _clean_str(data.get("summary"), 600), _clean_str(data.get("body"), 20000),
             json.dumps(fields, ensure_ascii=False),
             json.dumps(_clean_list(data.get("tags"), 40), ensure_ascii=False),
             difficulty, status, verify, 1 if data.get("favorite") else 0, t, t))
    src_ids = data.get("source_ids")
    if isinstance(src_ids, list):
        conn.execute("DELETE FROM node_sources WHERE node_id = ?", (nid,))
        for sid in src_ids[:20]:
            if conn.execute("SELECT id FROM sources WHERE id = ?", (str(sid),)).fetchone():
                conn.execute("INSERT OR IGNORE INTO node_sources (node_id, source_id) VALUES (?,?)",
                             (nid, str(sid)))
    conn.commit()
    return get_node(nid), ""


def export_data():
    conn = db()
    nodes = [node_dict(r) for r in conn.execute("SELECT * FROM nodes ORDER BY created_at")]
    return {
        "format": EXPORT_FORMAT, "version": SCHEMA_VERSION, "exported_at": _now(),
        "nodes": nodes,
        "relations": [dict(r) for r in conn.execute("SELECT * FROM relations ORDER BY created_at")],
        "sources": [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY created_at")],
        "node_sources": [dict(r) for r in conn.execute("SELECT * FROM node_sources")],
        "notes": [dict(r) | {"tags": _loads(r["tags"], [])} for r in
                  conn.execute("SELECT * FROM notes ORDER BY created_at")],
        "reviews": [dict(r) for r in conn.execute("SELECT * FROM reviews ORDER BY reviewed_at")],
    }


def validate_import(data):
    if not isinstance(data, dict):
        return "备份文件格式不对（应为 JSON 对象）"
    if data.get("format") != EXPORT_FORMAT:
        return "不是 MedAtlas 备份文件"
    if int(data.get("version") or 0) > SCHEMA_VERSION:
        return "备份版本高于当前应用版本，请先升级应用"
    for key in ("nodes", "relations", "sources", "notes"):
        if not isinstance(data.get(key), list):
            return "备份缺少 " + key + " 数据"
    for n in data["nodes"]:
        if not isinstance(n, dict) or not n.get("id") or not n.get("title"):
            return "备份中有知识条目缺少 id 或标题"
    return ""


def import_preview(data):
    conn = db()
    have_nodes = {r[0] for r in conn.execute("SELECT id FROM nodes")}
    have_rel = {r[0] for r in conn.execute("SELECT id FROM relations")}
    have_src = {r[0] for r in conn.execute("SELECT id FROM sources")}
    have_notes = {r[0] for r in conn.execute("SELECT id FROM notes")}
    def split(items, have):
        new = sum(1 for x in items if x.get("id") not in have)
        return {"new": new, "overwrite": len(items) - new, "total": len(items)}
    return {"nodes": split(data["nodes"], have_nodes),
            "relations": split(data["relations"], have_rel),
            "sources": split(data["sources"], have_src),
            "notes": split(data["notes"], have_notes),
            "reviews_total": len(data.get("reviews") or [])}


def apply_import(data):
    """Merge by id (upsert). Never deletes existing rows."""
    err = validate_import(data)
    if err:
        return err
    conn = db()
    try:
        conn.execute("BEGIN")
        for s in data["sources"]:
            conn.execute(
                "INSERT INTO sources (id, title, publisher, url, pub_date, accessed_at, notes, created_at)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
                " publisher=excluded.publisher, url=excluded.url, pub_date=excluded.pub_date,"
                " accessed_at=excluded.accessed_at, notes=excluded.notes",
                (s["id"], _clean_str(s.get("title"), 200), _clean_str(s.get("publisher"), 120),
                 _clean_str(s.get("url"), 1000), _clean_str(s.get("pub_date"), 40),
                 _clean_str(s.get("accessed_at"), 40), _clean_str(s.get("notes"), 1000),
                 float(s.get("created_at") or _now())))
        for n in data["nodes"]:
            d, _e = _node_upsert(conn, n)
            if _e:
                conn.rollback()
                return "导入失败：" + _e
        for r in data["relations"]:
            if r.get("rel_type") not in REL_TYPES:
                continue
            if not (conn.execute("SELECT id FROM nodes WHERE id=?", (r.get("source_id"),)).fetchone()
                    and conn.execute("SELECT id FROM nodes WHERE id=?", (r.get("target_id"),)).fetchone()):
                continue
            conn.execute(
                "INSERT INTO relations (id, source_id, target_id, rel_type, note, verify, created_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET rel_type=excluded.rel_type,"
                " note=excluded.note, verify=excluded.verify",
                (r["id"], r["source_id"], r["target_id"], r["rel_type"],
                 _clean_str(r.get("note"), 500), r.get("verify") if r.get("verify") in VERIFY else "pending",
                 float(r.get("created_at") or _now())))
        for ns in data.get("node_sources") or []:
            conn.execute("INSERT OR IGNORE INTO node_sources (node_id, source_id) VALUES (?,?)",
                         (ns.get("node_id"), ns.get("source_id")))
        for nt in data["notes"]:
            conn.execute(
                "INSERT INTO notes (id, node_id, content, tags, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,"
                " tags=excluded.tags, updated_at=excluded.updated_at",
                (nt["id"], nt.get("node_id"), _clean_str(nt.get("content"), 10000),
                 json.dumps(_clean_list(nt.get("tags"), 40), ensure_ascii=False),
                 float(nt.get("created_at") or _now()), float(nt.get("updated_at") or _now())))
        for rv in data.get("reviews") or []:
            conn.execute(
                "INSERT OR IGNORE INTO reviews (id, node_id, reviewed_at, result, next_at)"
                " VALUES (?,?,?,?,?)",
                (rv.get("id") or _nid(), rv.get("node_id"), float(rv.get("reviewed_at") or _now()),
                 _clean_str(rv.get("result"), 20), rv.get("next_at")))
        conn.commit()
    except Exception as exc:  # rollback keeps existing data intact
        conn.rollback()
        return "导入失败：" + str(exc)[:200]
    return ""


def _node_upsert(conn, n):
    nid = str(n.get("id"))[:32]
    title = _clean_str(n.get("title"), 120)
    if not title:
        return None, "有知识条目缺少标题"
    type_ = n.get("type") if n.get("type") in NODE_TYPES else "concept"
    category = n.get("category") if n.get("category") in CATEGORIES else "医学基础"
    fields = n.get("fields") if isinstance(n.get("fields"), dict) else {}
    conn.execute(
        "INSERT INTO nodes (id, type, category, title, aliases, summary, body, fields, tags, difficulty,"
        " status, verify, favorite, archived, next_review_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET type=excluded.type, category=excluded.category,"
        " title=excluded.title, aliases=excluded.aliases, summary=excluded.summary, body=excluded.body,"
        " fields=excluded.fields, tags=excluded.tags, difficulty=excluded.difficulty,"
        " status=excluded.status, verify=excluded.verify, favorite=excluded.favorite,"
        " archived=excluded.archived, next_review_at=excluded.next_review_at, updated_at=excluded.updated_at",
        (nid, type_, category, title,
         json.dumps(_clean_list(n.get("aliases"), 60), ensure_ascii=False),
         _clean_str(n.get("summary"), 600), _clean_str(n.get("body"), 20000),
         json.dumps(fields, ensure_ascii=False),
         json.dumps(_clean_list(n.get("tags"), 40), ensure_ascii=False),
         max(1, min(5, int(n.get("difficulty") or 1))) if str(n.get("difficulty") or "").isdigit() else 1,
         n.get("status") if n.get("status") in STATUSES else "unlearned",
         n.get("verify") if n.get("verify") in VERIFY else "pending",
         1 if n.get("favorite") else 0, 1 if n.get("archived") else 0, n.get("next_review_at"),
         float(n.get("created_at") or _now()), float(n.get("updated_at") or _now())))
    return nid, ""


def home_data():
    conn = db()
    now = _now()
    counts = {k: 0 for k in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) AS c FROM nodes WHERE archived = 0 GROUP BY status"):
        counts[r["status"]] = r["c"]
    total = conn.execute("SELECT COUNT(*) AS c FROM nodes WHERE archived = 0").fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE archived = 0 AND verify = 'pending'").fetchone()["c"]
    due = [node_dict(r) for r in conn.execute(
        "SELECT * FROM nodes WHERE archived = 0 AND next_review_at IS NOT NULL AND next_review_at <= ?"
        " ORDER BY next_review_at LIMIT 10", (now,))]
    cont = [node_dict(r) for r in conn.execute(
        "SELECT * FROM nodes WHERE archived = 0 AND status IN ('learning', 'review')"
        " ORDER BY updated_at DESC LIMIT 5")]
    recent = [node_dict(r) for r in conn.execute(
        "SELECT * FROM nodes WHERE archived = 0 ORDER BY updated_at DESC LIMIT 6")]
    tags = set()
    for r in conn.execute("SELECT tags FROM nodes WHERE archived = 0"):
        tags.update(_loads(r["tags"], []))
    return {"counts": counts, "total": total, "pending_verify": pending,
            "due": due, "continue": cont, "recent": recent, "tags": sorted(tags)}


# ------------------------------------------------------------------ page

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>医学图谱 MedAtlas</title>
<style>
:root{--bg:#f6f8f7;--card:#fff;--ink:#1f2937;--mut:#6b7280;--line:#e5e7eb;
--acc:#0f766e;--acc2:#0d9488;--warn:#b45309;--bad:#b91c1c;--pers:#7c3aed}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
a{color:var(--acc);text-decoration:none}
header{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid var(--line);
display:flex;gap:10px;align-items:center;padding:10px 14px;flex-wrap:wrap}
header .logo{font-weight:700;font-size:18px;color:var(--acc);white-space:nowrap}
header input[type=search]{flex:1;min-width:140px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:15px}
.btn{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:14px;cursor:pointer;min-height:38px}
.btn.ghost{background:#fff;color:var(--acc);border:1px solid var(--acc)}
.btn.gray{background:#eef2f1;color:var(--ink);border:1px solid var(--line)}
.btn.danger{background:#fff;color:var(--bad);border:1px solid var(--bad)}
.btn.sm{padding:4px 10px;min-height:30px;font-size:13px}
nav.tabs{display:flex;gap:6px;background:#fff;border-bottom:1px solid var(--line);padding:6px 14px;overflow-x:auto}
nav.tabs button{border:0;background:transparent;padding:8px 12px;font-size:15px;cursor:pointer;border-radius:8px;color:var(--mut);white-space:nowrap}
nav.tabs button.on{background:#e6f4f1;color:var(--acc);font-weight:600}
main{max-width:1060px;margin:0 auto;padding:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.kcard{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px;cursor:pointer}
.kcard:hover{border-color:var(--acc2)}
.kcard h3{margin:0 0 6px;font-size:16px}
.kcard p{margin:0;color:var(--mut);font-size:13px;line-height:1.5;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.badge{display:inline-block;font-size:11px;padding:2px 7px;border-radius:99px;background:#eef2f1;color:var(--mut);margin:2px 4px 2px 0}
.badge.t{background:#e0f2f1;color:var(--acc)}
.badge.src{background:#dcfce7;color:#166534}
.badge.pen{background:#fef3c7;color:var(--warn)}
.badge.per{background:#ede9fe;color:var(--pers)}
.badge.st-learning{background:#dbeafe;color:#1d4ed8}
.badge.st-review{background:#ffedd5;color:#c2410c}
.badge.st-mastered{background:#dcfce7;color:#166534}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.chip{border:1px solid var(--line);background:#fff;border-radius:99px;padding:6px 12px;font-size:13px;cursor:pointer;min-height:34px}
.chip.on{background:var(--acc);color:#fff;border-color:var(--acc)}
h2.sec{font-size:17px;margin:18px 0 8px}
label.fl{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
input.in,select.in,textarea.in{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:8px;font-size:15px;background:#fff}
textarea.in{min-height:90px;line-height:1.6}
.rel-row{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px dashed var(--line);flex-wrap:wrap}
.rel-row .lbl{background:#e6f4f1;color:var(--acc);border-radius:6px;padding:2px 8px;font-size:12px}
.rel-row .note{color:var(--mut);font-size:12px}
.note-card{border-left:3px solid var(--pers);background:#faf7ff;border-radius:8px;padding:10px 12px;margin-bottom:8px}
.src-card{border-left:3px solid #16a34a;background:#f7fdf8;border-radius:8px;padding:10px 12px;margin-bottom:8px;font-size:14px}
.body-text{line-height:1.85;font-size:15.5px;white-space:pre-wrap}
.kv{display:grid;grid-template-columns:110px 1fr;gap:6px 12px;font-size:14.5px;line-height:1.7}
.kv dt{color:var(--mut)}
.kv dd{margin:0;white-space:pre-wrap}
.stat{display:inline-block;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 14px;margin:0 8px 8px 0;font-size:14px}
.stat b{font-size:18px;color:var(--acc)}
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#111827;color:#fff;
padding:10px 18px;border-radius:99px;font-size:14px;opacity:0;pointer-events:none;transition:opacity .25s;z-index:50;max-width:88vw;text-align:center}
#toast.show{opacity:.94}
.empty{color:var(--mut);text-align:center;padding:28px 0;font-size:14px}
.row-flex{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
select.in.wa,input.in.wa{width:auto}
.small{font-size:12.5px;color:var(--mut)}
@media (max-width:640px){.kv{grid-template-columns:92px 1fr}main{padding:10px}}
</style>
</head>
<body>
<header>
  <span class="logo">医学图谱</span>
  <input type="search" id="global-q" placeholder="搜索知识、别名、标签…" aria-label="全局搜索">
  <button class="btn" id="btn-add">＋ 新增知识</button>
</header>
<nav class="tabs" id="tabs">
  <button data-view="home" class="on">首页</button>
  <button data-view="library">知识库</button>
  <button data-view="manage">我的管理</button>
</nav>
<main id="app"></main>
<div id="toast"></div>
<script>
const API = "api/";
const CATS = ["医学基础","人体结构与生理","疾病与病理","药理与药物","症状与鉴别思路","医学检查与指标"];
const TYPES = {concept:"医学基础概念",disease:"疾病",drug:"药物",system:"结构与生理",symptom:"症状",exam:"检查与指标"};
const RELS = {involves:"涉及",belongs:"属于",participates:"参与",manifests_as:"可表现为",causes:"可能导致",increases_risk:"增加风险",acts_on:"作用于",indicated_for:"用于",metabolized_in:"主要代谢于",differential:"需与之鉴别",examined_by:"可通过检查"};
const STS = {unlearned:"未学习",learning:"学习中",review:"待复习",mastered:"已掌握"};
const VER = {sourced:"有来源",pending:"待核验",personal:"个人整理"};
const DRUG_FIELDS = [["drug_class","药物类别"],["mechanism","作用机制"],["indications","适应证"],["dosage_forms","常见剂型与规格"],["adverse","常见不良反应"],["contraindications","禁忌与重要注意事项"],["interactions","药物相互作用"],["special_populations","特殊人群注意事项"],["overdose_risk","过量使用的风险"],["longterm_risk","长期或反复使用的风险"],["red_flags","需要就医的警示情况"]];
let state = {view:"home", q:"", category:"", tag:"", status:"", nodeId:null, sources:[]};
const $ = s => document.querySelector(s);
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function toast(msg){const t=$("#toast");t.textContent=msg;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),2400)}
async function api(path, opts){
  const r = await fetch(API + path, opts);
  const d = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(d.error || ("请求失败 " + r.status));
  return d;
}
function badges(n){
  let h = '<span class="badge t">' + esc(TYPES[n.type]||n.type) + '</span>'
        + '<span class="badge">' + esc(n.category) + '</span>'
        + '<span class="badge st-' + n.status + '">' + (STS[n.status]||n.status) + '</span>';
  h += n.verify === "sourced" ? '<span class="badge src">有来源</span>'
     : n.verify === "personal" ? '<span class="badge per">个人整理</span>'
     : '<span class="badge pen">待核验</span>';
  if(n.favorite) h += '<span class="badge">★ 收藏</span>';
  return h;
}
function kcard(n){
  return '<div class="kcard" data-open="' + n.id + '"><h3>' + esc(n.title) + '</h3><p>' + esc(n.summary) + '</p><div>' + badges(n) + '</div></div>';
}
function bindOpen(root){
  root.querySelectorAll("[data-open]").forEach(el => el.addEventListener("click", () => openNode(el.dataset.open)));
}
function go(view){state.view = view; document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.view === view)); render();}
function openNode(id){state.nodeId = id; state.view = "detail"; document.querySelectorAll("#tabs button").forEach(b => b.classList.remove("on")); render(); window.scrollTo(0,0);}

async function render(){
  const app = $("#app");
  if(state.view === "home") return renderHome(app);
  if(state.view === "library") return renderLibrary(app);
  if(state.view === "detail") return renderDetail(app);
  if(state.view === "edit") return renderEdit(app, state.editId || null);
  if(state.view === "manage") return renderManage(app);
}

async function renderHome(app){
  const d = await api("home");
  let h = "";
  h += '<div class="card"><h2 style="margin:4px 0 10px">今日学习</h2>';
  h += '<span class="stat">知识总数 <b>' + d.total + '</b></span>'
     + '<span class="stat">已掌握 <b>' + (d.counts.mastered||0) + '</b></span>'
     + '<span class="stat">学习中 <b>' + (d.counts.learning||0) + '</b></span>'
     + '<span class="stat">待复习 <b>' + (d.counts.review||0) + '</b></span>'
     + '<span class="stat">待核验 <b>' + d.pending_verify + '</b></span>';
  h += '<div class="row-flex" style="margin-top:6px"><button class="btn" onclick="go(\'library\')">进入知识库</button>'
     + '<button class="btn ghost" id="home-add">＋ 新增知识</button></div></div>';
  if(d.due.length){
    h += '<h2 class="sec">今日待复习（' + d.due.length + '）</h2><div class="grid">'
       + d.due.map(kcard).join("") + '</div>';
  }
  if(d.continue.length){
    h += '<h2 class="sec">继续学习</h2><div class="grid">' + d.continue.map(kcard).join("") + '</div>';
  }
  h += '<h2 class="sec">最近更新</h2><div class="grid">' + d.recent.map(kcard).join("") + '</div>';
  if(!d.total) h += '<div class="empty">知识库是空的，点「新增知识」添加第一条。</div>';
  app.innerHTML = h;
  bindOpen(app);
  const b = $("#home-add"); if(b) b.addEventListener("click", () => startEdit(null));
}

async function renderLibrary(app){
  const params = new URLSearchParams();
  if(state.q) params.set("q", state.q);
  if(state.category) params.set("category", state.category);
  if(state.tag) params.set("tag", state.tag);
  if(state.status) params.set("status", state.status);
  const [list, home] = await Promise.all([api("nodes?" + params.toString()), api("home")]);
  let h = '<div class="chips"><button class="chip' + (state.category ? "" : " on") + '" data-cat="">全部</button>'
    + CATS.map(c => '<button class="chip' + (state.category === c ? " on" : "") + '" data-cat="' + esc(c) + '">' + esc(c) + '</button>').join("") + '</div>';
  h += '<div class="row-flex"><input class="in wa" id="lib-q" type="search" placeholder="搜索标题、别名、摘要、标签" value="' + esc(state.q) + '" style="flex:1;min-width:160px">'
     + '<select class="in wa" id="lib-status"><option value="">全部状态</option>'
     + Object.keys(STS).map(k => '<option value="' + k + '"' + (state.status === k ? " selected" : "") + '>' + STS[k] + '</option>').join("") + '</select>'
     + '<select class="in wa" id="lib-tag"><option value="">全部标签</option>'
     + home.tags.map(t => '<option' + (state.tag === t ? " selected" : "") + '>' + esc(t) + '</option>').join("") + '</select></div>';
  h += '<p class="small">共 ' + list.nodes.length + ' 条</p>';
  h += list.nodes.length ? '<div class="grid">' + list.nodes.map(kcard).join("") + '</div>'
                         : '<div class="empty">没有找到符合条件的知识。可以换个关键词，或新增一条。</div>';
  app.innerHTML = h;
  bindOpen(app);
  app.querySelectorAll("[data-cat]").forEach(b => b.addEventListener("click", () => {state.category = b.dataset.cat; render();}));
  $("#lib-status").addEventListener("change", e => {state.status = e.target.value; render();});
  $("#lib-tag").addEventListener("change", e => {state.tag = e.target.value; render();});
  let timer;
  $("#lib-q").addEventListener("input", e => {clearTimeout(timer); timer = setTimeout(() => {state.q = e.target.value; render();}, 350);});
}

function relRowOut(r){
  return '<div class="rel-row"><span>本页</span><span class="lbl">' + esc(RELS[r.rel_type]||r.rel_type) + '</span>'
    + '<a href="#" data-open="' + r.target_id + '">' + esc(r.other_title) + '</a>'
    + (r.note ? '<span class="note">' + esc(r.note) + '</span>' : '')
    + '<button class="btn gray sm" data-delrel="' + r.id + '">删除</button></div>';
}
function relRowIn(r){
  return '<div class="rel-row"><a href="#" data-open="' + r.source_id + '">' + esc(r.other_title) + '</a>'
    + '<span class="lbl">' + esc(RELS[r.rel_type]||r.rel_type) + '</span><span>本页</span>'
    + (r.note ? '<span class="note">' + esc(r.note) + '</span>' : '')
    + '<span class="note">（指向本页，方向以原文为准）</span>'
    + '<button class="btn gray sm" data-delrel="' + r.id + '">删除</button></div>';
}

async function renderDetail(app){
  const n = await api("nodes/" + state.nodeId);
  state.sources = (await api("sources")).sources;
  const f = n.fields || {};
  let h = '<div class="card"><div class="row-flex" style="justify-content:space-between">'
    + '<h2 style="margin:2px 0">' + esc(n.title) + '</h2></div>';
  if(n.aliases.length) h += '<p class="small">别名：' + n.aliases.map(esc).join("、") + '</p>';
  h += '<div>' + badges(n) + n.tags.map(t => '<span class="badge">#' + esc(t) + '</span>').join("") + '</div>';
  h += '<p class="body-text" style="font-size:16px">' + esc(n.summary) + '</p>';
  if(n.body) h += '<div class="body-text">' + esc(n.body) + '</div>';
  h += '<div class="row-flex" style="margin-top:12px">'
     + '<label class="small">学习状态 <select class="in wa" id="d-status">'
     + Object.keys(STS).map(k => '<option value="' + k + '"' + (n.status === k ? " selected" : "") + '>' + STS[k] + '</option>').join("") + '</select></label>'
     + '<button class="btn ghost sm" id="d-fav">' + (n.favorite ? "取消收藏" : "★ 收藏") + '</button>'
     + '<button class="btn ghost sm" id="d-edit">编辑</button>'
     + '<button class="btn gray sm" id="d-archive">' + (n.archived ? "取消归档" : "归档") + '</button>'
     + '<button class="btn danger sm" id="d-delete">删除</button></div>';
  h += '<p class="small" style="margin-top:8px">复习自评：'
     + '<button class="btn gray sm" data-rev="again">忘了（1 天后）</button> '
     + '<button class="btn gray sm" data-rev="fuzzy">模糊（2 天后）</button> '
     + '<button class="btn gray sm" data-rev="good">记得（7 天后）</button>'
     + (n.next_review_at ? '　下次复习：' + new Date(n.next_review_at * 1000).toLocaleDateString() : '') + '</p></div>';

  if(n.type === "drug"){
    h += '<div class="card"><h3 style="margin:4px 0 10px">药物档案</h3><dl class="kv">';
    for(const [k, label] of DRUG_FIELDS){ if(f[k]) h += '<dt>' + label + '</dt><dd>' + esc(f[k]) + '</dd>'; }
    h += '</dl>';
    const prs = Array.isArray(f.price_records) ? f.price_records : [];
    const exs = Array.isArray(f.expiry_records) ? f.expiry_records : [];
    if(prs.length){ h += '<h4>价格记录（带来源与日期）</h4>' + prs.map(p => '<p class="small">' + esc([p.region,p.spec,p.pack,p.price,p.currency,p.source,p.date].filter(Boolean).join(" · ")) + '</p>').join(""); }
    if(exs.length){ h += '<h4>有效期信息（具体产品）</h4>' + exs.map(p => '<p class="small">' + esc([p.product,p.expiry,p.storage,p.after_open,p.source].filter(Boolean).join(" · ")) + '</p>').join(""); }
    if(!prs.length && !exs.length) h += '<p class="small">暂无价格 / 有效期记录（这类信息必须带来源和日期，不填统一数值）。</p>';
    h += '</div>';
  } else {
    const extra = Object.keys(f).filter(k => !["price_records","expiry_records"].includes(k) && f[k]);
    if(extra.length) h += '<div class="card"><h3 style="margin:4px 0 10px">补充信息</h3><dl class="kv">'
      + extra.map(k => '<dt>' + esc(k) + '</dt><dd>' + esc(f[k]) + '</dd>').join("") + '</dl></div>';
  }

  h += '<div class="card"><h3 style="margin:4px 0 6px">知识关联</h3>';
  h += n.relations_out.length ? n.relations_out.map(relRowOut).join("") : '<p class="small">暂无向外关联。</p>';
  h += n.relations_in.length ? '<h4 style="margin:12px 0 4px">指向本页</h4>' + n.relations_in.map(relRowIn).join("") : '';
  h += '<div class="row-flex" style="margin-top:12px"><input class="in wa" id="rel-q" type="search" placeholder="搜索目标知识" style="flex:1;min-width:150px">'
     + '<select class="in wa" id="rel-type">' + Object.keys(RELS).map(k => '<option value="' + k + '">' + RELS[k] + '</option>').join("") + '</select>'
     + '<input class="in wa" id="rel-note" placeholder="备注（可选）" style="flex:1;min-width:120px"></div>'
     + '<div id="rel-results" style="margin-top:8px"></div></div>';

  h += '<div class="card"><h3 style="margin:4px 0 8px">参考来源</h3>';
  h += n.sources.length ? n.sources.map(s => '<div class="src-card"><b>' + esc(s.title) + '</b>'
      + (s.publisher ? ' · ' + esc(s.publisher) : '') + (s.url ? ' · <a href="' + esc(s.url) + '" target="_blank" rel="noopener">链接</a>' : '')
      + (s.notes ? '<div class="small">' + esc(s.notes) + '</div>' : '') + '</div>').join("")
    : '<p class="small">暂无来源。内容未经来源核对时请保持「待核验」状态。</p>';
  h += '</div>';

  h += '<div class="card"><h3 style="margin:4px 0 8px">个人笔记（与参考内容区分）</h3>';
  h += n.notes.length ? n.notes.map(nt => '<div class="note-card">' + esc(nt.content)
      + '<div class="row-flex" style="margin-top:6px"><span class="small">' + new Date(nt.created_at * 1000).toLocaleString() + '</span>'
      + '<button class="btn gray sm" data-delnote="' + nt.id + '">删除</button></div></div>').join("")
    : '<p class="small">还没有笔记。</p>';
  h += '<textarea class="in" id="note-text" placeholder="写下你的理解、疑问或记忆要点…"></textarea>'
     + '<div class="row-flex" style="margin-top:8px"><input class="in wa" id="note-tags" placeholder="标签（逗号分隔，可选）">'
     + '<button class="btn sm" id="note-add">添加笔记</button></div></div>';

  app.innerHTML = h;
  bindOpen(app);
  $("#d-status").addEventListener("change", async e => { await api("nodes/" + n.id + "/status", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({status: e.target.value})}); toast("状态已更新"); render(); });
  $("#d-fav").addEventListener("click", async () => { await api("nodes/" + n.id, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(fullNode(n, {favorite: !n.favorite}))}); render(); });
  $("#d-edit").addEventListener("click", () => startEdit(n.id));
  $("#d-archive").addEventListener("click", async () => { await api("nodes/" + n.id + (n.archived ? "/unarchive" : "/archive"), {method:"POST"}); toast(n.archived ? "已取消归档" : "已归档"); go("library"); });
  $("#d-delete").addEventListener("click", async () => { if(confirm("确定永久删除这条知识？其关联和笔记也会删除。此操作不可恢复（建议优先用归档）。")){ await api("nodes/" + n.id + "/delete", {method:"POST"}); toast("已删除"); go("library"); } });
  app.querySelectorAll("[data-rev]").forEach(b => b.addEventListener("click", async () => { const r = await api("nodes/" + n.id + "/review", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({result: b.dataset.rev})}); toast("已安排下次复习：" + new Date(r.next_review_at * 1000).toLocaleDateString()); render(); }));
  app.querySelectorAll("[data-delrel]").forEach(b => b.addEventListener("click", async () => { if(confirm("删除这条关联？")){ await api("relations/" + b.dataset.delrel + "/delete", {method:"POST"}); render(); } }));
  app.querySelectorAll("[data-delnote]").forEach(b => b.addEventListener("click", async () => { if(confirm("删除这条笔记？")){ await api("notes/" + b.dataset.delnote + "/delete", {method:"POST"}); render(); } }));
  $("#note-add").addEventListener("click", async () => {
    const text = $("#note-text").value.trim(); if(!text){ toast("笔记内容不能为空"); return; }
    await api("notes", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({node_id: n.id, content: text, tags: $("#note-tags").value.split(/[,，]/).map(s => s.trim()).filter(Boolean)})});
    toast("笔记已保存"); render();
  });
  let rtimer;
  $("#rel-q").addEventListener("input", e => {
    clearTimeout(rtimer);
    rtimer = setTimeout(async () => {
      const q = e.target.value.trim(); const box = $("#rel-results");
      if(!q){ box.innerHTML = ""; return; }
      const res = await api("nodes?q=" + encodeURIComponent(q));
      box.innerHTML = res.nodes.filter(x => x.id !== n.id).slice(0, 8).map(x =>
        '<div class="rel-row"><span>' + esc(x.title) + '</span><span class="badge">' + esc(TYPES[x.type]||"") + '</span>'
        + '<button class="btn sm" data-addrel="' + x.id + '">建立关联</button></div>').join("") || '<p class="small">没有匹配的知识。</p>';
      box.querySelectorAll("[data-addrel]").forEach(b => b.addEventListener("click", async () => {
        try{
          await api("relations", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({source_id: n.id, target_id: b.dataset.addrel, rel_type: $("#rel-type").value, note: $("#rel-note").value.trim()})});
          toast("关联已建立"); render();
        }catch(err){ toast(err.message); }
      }));
    }, 300);
  });
}

function fullNode(n, patch){
  return Object.assign({}, n, patch || {});
}

function startEdit(id){ state.editId = id; state.view = "edit"; document.querySelectorAll("#tabs button").forEach(b => b.classList.remove("on")); render(); window.scrollTo(0,0); }

async function renderEdit(app, id){
  const n = id ? await api("nodes/" + id) : {type:"concept", category:"医学基础", title:"", aliases:[], summary:"", body:"", fields:{}, tags:[], difficulty:1, status:"unlearned", verify:"pending", favorite:false, sources:[]};
  state.sources = (await api("sources")).sources;
  const f = n.fields || {};
  const selSrc = new Set((n.sources || []).map(s => s.id));
  let h = '<div class="card"><h2 style="margin:4px 0">' + (id ? "编辑知识" : "新增知识") + '</h2>';
  h += '<label class="fl">类型</label><select class="in" id="e-type">' + Object.keys(TYPES).map(k => '<option value="' + k + '"' + (n.type === k ? " selected" : "") + '>' + TYPES[k] + '</option>').join("") + '</select>';
  h += '<label class="fl">分类</label><select class="in" id="e-cat">' + CATS.map(c => '<option' + (n.category === c ? " selected" : "") + '>' + esc(c) + '</option>').join("") + '</select>';
  h += '<label class="fl">标题 *</label><input class="in" id="e-title" value="' + esc(n.title) + '">';
  h += '<label class="fl">别名（逗号分隔）</label><input class="in" id="e-alias" value="' + esc(n.aliases.join(", ")) + '">';
  h += '<label class="fl">摘要 *</label><textarea class="in" id="e-summary" style="min-height:60px">' + esc(n.summary) + '</textarea>';
  h += '<label class="fl">正文</label><textarea class="in" id="e-body" style="min-height:130px">' + esc(n.body) + '</textarea>';
  h += '<div id="e-drug"' + (n.type === "drug" ? "" : ' style="display:none"') + '><h3 style="margin:14px 0 4px">药物档案字段</h3>';
  for(const [k, label] of DRUG_FIELDS) h += '<label class="fl">' + label + '</label><textarea class="in drugf" data-k="' + k + '" style="min-height:44px">' + esc(f[k] || "") + '</textarea>';
  h += '<label class="fl">价格记录（每行一条：地区 | 规格 | 包装 | 价格 | 币种 | 来源 | 日期）</label><textarea class="in" id="e-prices" style="min-height:52px">'
     + esc((f.price_records || []).map(p => [p.region,p.spec,p.pack,p.price,p.currency,p.source,p.date].join(" | ")).join("\n")) + '</textarea>';
  h += '<label class="fl">有效期记录（每行一条：具体产品 | 有效期 | 储存要求 | 开封后要求 | 来源）</label><textarea class="in" id="e-expiries" style="min-height:52px">'
     + esc((f.expiry_records || []).map(p => [p.product,p.expiry,p.storage,p.after_open,p.source].join(" | ")).join("\n")) + '</textarea></div>';
  h += '<label class="fl">标签（逗号分隔）</label><input class="in" id="e-tags" value="' + esc(n.tags.join(", ")) + '">';
  h += '<div class="row-flex"><label class="fl" style="margin:0">难度 <select class="in wa" id="e-diff">' + [1,2,3,4,5].map(d => '<option' + (n.difficulty === d ? " selected" : "") + '>' + d + '</option>').join("") + '</select></label>'
     + '<label class="fl" style="margin:0">核验状态 <select class="in wa" id="e-verify">' + Object.keys(VER).map(k => '<option value="' + k + '"' + (n.verify === k ? " selected" : "") + '>' + VER[k] + '</option>').join("") + '</select></label>'
     + '<label class="fl" style="margin:0">学习状态 <select class="in wa" id="e-status">' + Object.keys(STS).map(k => '<option value="' + k + '"' + (n.status === k ? " selected" : "") + '>' + STS[k] + '</option>').join("") + '</select></label></div>';
  h += '<label class="fl">参考来源（勾选关联）</label><div>' + (state.sources.length ? state.sources.map(s =>
      '<label style="display:block;font-size:14px;margin:4px 0"><input type="checkbox" class="e-src" value="' + s.id + '"' + (selSrc.has(s.id) ? " checked" : "") + '> ' + esc(s.title) + (s.publisher ? '（' + esc(s.publisher) + '）' : '') + '</label>').join("")
    : '<span class="small">暂无来源，可先在「我的管理」里添加。</span>') + '</div>';
  h += '<div class="row-flex" style="margin-top:16px"><button class="btn" id="e-save">保存</button>'
     + '<button class="btn gray" id="e-cancel">取消</button><span class="small">保存后进入详情页预览效果。</span></div></div>';
  app.innerHTML = h;
  $("#e-type").addEventListener("change", e => { $("#e-drug").style.display = e.target.value === "drug" ? "" : "none"; });
  $("#e-cancel").addEventListener("click", () => { if(id) openNode(id); else go("library"); });
  $("#e-save").addEventListener("click", async () => {
    const title = $("#e-title").value.trim(), summary = $("#e-summary").value.trim();
    if(!title){ toast("标题不能为空"); return; }
    if(!summary){ toast("摘要不能为空"); return; }
    const fields = {};
    if($("#e-type").value === "drug"){
      app.querySelectorAll(".drugf").forEach(t => { if(t.value.trim()) fields[t.dataset.k] = t.value.trim(); });
      fields.price_records = $("#e-prices").value.split("\n").map(l => l.trim()).filter(Boolean).map(l => { const p = l.split("|").map(s => s.trim()); return {region:p[0]||"", spec:p[1]||"", pack:p[2]||"", price:p[3]||"", currency:p[4]||"", source:p[5]||"", date:p[6]||""}; });
      fields.expiry_records = $("#e-expiries").value.split("\n").map(l => l.trim()).filter(Boolean).map(l => { const p = l.split("|").map(s => s.trim()); return {product:p[0]||"", expiry:p[1]||"", storage:p[2]||"", after_open:p[3]||"", source:p[4]||""}; });
    } else {
      Object.keys(f || {}).forEach(k => { fields[k] = f[k]; });
    }
    const payload = {type: $("#e-type").value, category: $("#e-cat").value, title, summary,
      aliases: $("#e-alias").value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      body: $("#e-body").value, fields,
      tags: $("#e-tags").value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      difficulty: parseInt($("#e-diff").value, 10), verify: $("#e-verify").value, status: $("#e-status").value,
      favorite: n.favorite || false,
      source_ids: Array.from(app.querySelectorAll(".e-src:checked")).map(c => c.value)};
    try{
      const saved = await api(id ? "nodes/" + id : "nodes", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
      toast("已保存"); openNode(saved.node.id);
    }catch(err){ toast(err.message); }
  });
}

async function renderManage(app){
  const [pend, arch, srcs] = await Promise.all([
    api("nodes?verify=pending"), api("nodes?archived=1"), api("sources")]);
  let h = '<div class="card"><h3 style="margin:4px 0 8px">数据备份与恢复</h3>'
    + '<p class="small">导出为完整 JSON（知识、关联、来源、笔记、学习记录）。导入采用按 id 合并：新增或覆盖同 id 条目，绝不清空现有数据，导入前先显示范围。</p>'
    + '<div class="row-flex"><a class="btn" href="api/export" download>导出备份</a>'
    + '<label class="btn ghost" style="display:inline-flex;align-items:center;cursor:pointer">导入备份<input type="file" id="m-file" accept=".json,application/json" style="display:none"></label></div>'
    + '<div id="m-import"></div></div>';
  h += '<div class="card"><h3 style="margin:4px 0 8px">参考来源管理</h3><div id="src-list">'
    + (srcs.sources.length ? srcs.sources.map(s => '<div class="src-card"><b>' + esc(s.title) + '</b>' + (s.publisher ? ' · ' + esc(s.publisher) : '') + (s.notes ? '<div class="small">' + esc(s.notes) + '</div>' : '') + '</div>').join("") : '<p class="small">暂无来源。</p>')
    + '</div><label class="fl">来源标题 *</label><input class="in" id="s-title">'
    + '<label class="fl">发布方</label><input class="in" id="s-pub">'
    + '<label class="fl">链接（可空，不要填写不存在的链接）</label><input class="in" id="s-url">'
    + '<label class="fl">备注</label><input class="in" id="s-notes">'
    + '<div style="margin-top:10px"><button class="btn sm" id="s-add">添加来源</button></div></div>';
  h += '<h2 class="sec">待核验内容（' + pend.nodes.length + '）</h2>'
    + (pend.nodes.length ? '<div class="grid">' + pend.nodes.map(kcard).join("") + '</div>' : '<p class="small">没有待核验内容。</p>');
  h += '<h2 class="sec">已归档（' + arch.nodes.length + '）</h2>'
    + (arch.nodes.length ? '<div class="grid">' + arch.nodes.map(kcard).join("") + '</div>' : '<p class="small">没有归档内容（在详情页可归档/取消归档）。</p>');
  app.innerHTML = h;
  bindOpen(app);
  $("#s-add").addEventListener("click", async () => {
    const title = $("#s-title").value.trim(); if(!title){ toast("来源标题不能为空"); return; }
    await api("sources", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({title, publisher: $("#s-pub").value.trim(), url: $("#s-url").value.trim(), notes: $("#s-notes").value.trim()})});
    toast("来源已添加"); render();
  });
  $("#m-file").addEventListener("change", async e => {
    const file = e.target.files[0]; if(!file) return;
    let data;
    try{ data = JSON.parse(await file.text()); }catch(err){ toast("文件不是合法 JSON"); return; }
    const box = $("#m-import");
    try{
      const pv = await api("import/preview", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(data)});
      const c = pv.counts;
      box.innerHTML = '<div class="card" style="margin-top:10px"><b>导入预览</b><p class="small">'
        + '知识：新增 ' + c.nodes.new + '，覆盖 ' + c.nodes.overwrite + '；'
        + '关联：新增 ' + c.relations.new + '，覆盖 ' + c.relations.overwrite + '；'
        + '来源：新增 ' + c.sources.new + '，覆盖 ' + c.sources.overwrite + '；'
        + '笔记：新增 ' + c.notes.new + '，覆盖 ' + c.notes.overwrite + '。现有其他数据不受影响。</p>'
        + '<button class="btn sm" id="m-apply">确认导入</button></div>';
      $("#m-apply").addEventListener("click", async () => {
        await api("import/apply", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(data)});
        toast("导入成功"); go("home");
      });
    }catch(err){ box.innerHTML = '<p class="small" style="color:var(--bad)">' + esc(err.message) + '</p>'; }
  });
}

document.querySelectorAll("#tabs button").forEach(b => b.addEventListener("click", () => go(b.dataset.view)));
$("#btn-add").addEventListener("click", () => startEdit(null));
let gtimer;
$("#global-q").addEventListener("input", e => { clearTimeout(gtimer); gtimer = setTimeout(() => { state.q = e.target.value; go("library"); }, 300); });
render().catch(err => { $("#app").innerHTML = '<div class="empty">加载失败：' + esc(err.message) + '</div>'; });
</script>
</body>
</html>
"""


# ------------------------------------------------------------------ http

def _json(start_response, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def _read_json(environ, limit=8 * 1024 * 1024):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > limit:
        return None
    raw = environ["wsgi.input"].read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


REVIEW_RULES = {"again": (1, "review"), "fuzzy": (2, "review"), "good": (7, "mastered")}


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = (environ.get("PATH_INFO") or "/").rstrip("/") or "/"
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    conn = db()

    def q1(name, default=""):
        v = qs.get(name)
        return v[0] if v else default

    try:
        if path == "/" and method == "GET":
            body = PAGE.encode("utf-8")
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                      ("Content-Length", str(len(body)))])
            return [body]

        if path == "/api/session":
            return _json(start_response, "200 OK", {"authenticated": True, "via": "platform"})

        if path == "/api/home" and method == "GET":
            return _json(start_response, "200 OK", home_data())

        if path == "/api/nodes" and method == "GET":
            nodes = list_nodes(q=q1("q"), category=q1("category"), tag=q1("tag"),
                               status=q1("status"),
                               archived=1 if q1("archived") == "1" else 0)
            verify = q1("verify")
            if verify:
                nodes = [n for n in nodes if n["verify"] == verify]
            return _json(start_response, "200 OK", {"nodes": nodes})

        if path == "/api/nodes" and method == "POST":
            data = _read_json(environ)
            if data is None:
                return _json(start_response, "400 Bad Request", {"error": "请求体不是合法 JSON"})
            node, err = save_node(data)
            if err:
                return _json(start_response, "400 Bad Request", {"error": err})
            return _json(start_response, "200 OK", {"node": node})

        if path == "/api/sources" and method == "GET":
            rows = conn.execute("SELECT * FROM sources ORDER BY created_at").fetchall()
            return _json(start_response, "200 OK", {"sources": [dict(r) for r in rows]})

        if path == "/api/sources" and method == "POST":
            data = _read_json(environ) or {}
            title = _clean_str(data.get("title"), 200)
            if not title:
                return _json(start_response, "400 Bad Request", {"error": "来源标题不能为空"})
            sid = _nid()
            conn.execute(
                "INSERT INTO sources (id, title, publisher, url, pub_date, accessed_at, notes, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (sid, title, _clean_str(data.get("publisher"), 120), _clean_str(data.get("url"), 1000),
                 _clean_str(data.get("pub_date"), 40), _clean_str(data.get("accessed_at"), 40),
                 _clean_str(data.get("notes"), 1000), _now()))
            conn.commit()
            return _json(start_response, "200 OK", {"id": sid})

        if path == "/api/relations" and method == "POST":
            data = _read_json(environ) or {}
            src, tgt, typ = data.get("source_id"), data.get("target_id"), data.get("rel_type")
            if typ not in REL_TYPES:
                return _json(start_response, "400 Bad Request", {"error": "关联类型无效"})
            if not src or not tgt or src == tgt:
                return _json(start_response, "400 Bad Request", {"error": "关联双方无效"})
            for nid_ in (src, tgt):
                if not conn.execute("SELECT id FROM nodes WHERE id = ?", (nid_,)).fetchone():
                    return _json(start_response, "400 Bad Request", {"error": "关联的知识不存在"})
            rid = _nid()
            try:
                conn.execute(
                    "INSERT INTO relations (id, source_id, target_id, rel_type, note, verify, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (rid, src, tgt, typ, _clean_str(data.get("note"), 500),
                     data.get("verify") if data.get("verify") in VERIFY else "pending", _now()))
                conn.commit()
            except sqlite3.IntegrityError:
                return _json(start_response, "400 Bad Request", {"error": "这条关联已经存在"})
            return _json(start_response, "200 OK", {"id": rid})

        if path == "/api/notes" and method == "POST":
            data = _read_json(environ) or {}
            content = _clean_str(data.get("content"), 10000)
            if not content:
                return _json(start_response, "400 Bad Request", {"error": "笔记内容不能为空"})
            if not conn.execute("SELECT id FROM nodes WHERE id = ?", (data.get("node_id"),)).fetchone():
                return _json(start_response, "400 Bad Request", {"error": "笔记所属知识不存在"})
            nid_ = _nid()
            t = _now()
            conn.execute(
                "INSERT INTO notes (id, node_id, content, tags, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (nid_, data["node_id"], content,
                 json.dumps(_clean_list(data.get("tags"), 40), ensure_ascii=False), t, t))
            conn.commit()
            return _json(start_response, "200 OK", {"id": nid_})

        if path == "/api/export" and method == "GET":
            body = json.dumps(export_data(), ensure_ascii=False, indent=1).encode("utf-8")
            fname = "medatlas-backup-" + time.strftime("%Y%m%d-%H%M%S") + ".json"
            start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                                      ("Content-Disposition", "attachment; filename=" + fname),
                                      ("Content-Length", str(len(body)))])
            return [body]

        if path == "/api/import/preview" and method == "POST":
            data = _read_json(environ)
            err = validate_import(data) if data is not None else "请求体不是合法 JSON"
            if err:
                return _json(start_response, "400 Bad Request", {"error": err})
            return _json(start_response, "200 OK", {"counts": import_preview(data)})

        if path == "/api/import/apply" and method == "POST":
            data = _read_json(environ)
            if data is None:
                return _json(start_response, "400 Bad Request", {"error": "请求体不是合法 JSON"})
            err = apply_import(data)
            if err:
                return _json(start_response, "400 Bad Request", {"error": err})
            return _json(start_response, "200 OK", {"ok": True})

        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "nodes":
            node_id = parts[2]
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if not row:
                return _json(start_response, "404 Not Found", {"error": "知识不存在"})
            if len(parts) == 3 and method == "GET":
                return _json(start_response, "200 OK", get_node(node_id))
            if len(parts) == 3 and method == "POST":
                data = _read_json(environ)
                if data is None:
                    return _json(start_response, "400 Bad Request", {"error": "请求体不是合法 JSON"})
                node, err = save_node(data, node_id=node_id)
                if err:
                    return _json(start_response, "400 Bad Request", {"error": err})
                return _json(start_response, "200 OK", {"node": node})
            action = parts[3] if len(parts) > 3 else ""
            if action == "archive":
                conn.execute("UPDATE nodes SET archived = 1, updated_at = ? WHERE id = ?", (_now(), node_id))
            elif action == "unarchive":
                conn.execute("UPDATE nodes SET archived = 0, updated_at = ? WHERE id = ?", (_now(), node_id))
            elif action == "delete":
                conn.execute("DELETE FROM relations WHERE source_id = ? OR target_id = ?", (node_id, node_id))
                conn.execute("DELETE FROM notes WHERE node_id = ?", (node_id,))
                conn.execute("DELETE FROM node_sources WHERE node_id = ?", (node_id,))
                conn.execute("DELETE FROM reviews WHERE node_id = ?", (node_id,))
                conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            elif action == "status":
                data = _read_json(environ) or {}
                st = data.get("status")
                if st not in STATUSES:
                    return _json(start_response, "400 Bad Request", {"error": "状态无效"})
                if st == "review" and not row["next_review_at"]:
                    conn.execute("UPDATE nodes SET status = ?, next_review_at = ?, updated_at = ? WHERE id = ?",
                                 (st, _now(), _now(), node_id))
                else:
                    conn.execute("UPDATE nodes SET status = ?, updated_at = ? WHERE id = ?",
                                 (st, _now(), node_id))
            elif action == "review":
                data = _read_json(environ) or {}
                rule = REVIEW_RULES.get(data.get("result"))
                if not rule:
                    return _json(start_response, "400 Bad Request", {"error": "复习结果无效"})
                days, st = rule
                nxt = _now() + days * 86400
                conn.execute("INSERT INTO reviews (id, node_id, reviewed_at, result, next_at) VALUES (?,?,?,?,?)",
                             (_nid(), node_id, _now(), data["result"], nxt))
                conn.execute("UPDATE nodes SET status = ?, next_review_at = ?, updated_at = ? WHERE id = ?",
                             (st, nxt, _now(), node_id))
                conn.commit()
                return _json(start_response, "200 OK", {"next_review_at": nxt, "status": st})
            else:
                return _json(start_response, "404 Not Found", {"error": "未知操作"})
            conn.commit()
            return _json(start_response, "200 OK", {"ok": True})

        if len(parts) == 4 and parts[0] == "api" and parts[1] == "relations" and parts[3] == "delete":
            conn.execute("DELETE FROM relations WHERE id = ?", (parts[2],))
            conn.commit()
            return _json(start_response, "200 OK", {"ok": True})

        if len(parts) == 4 and parts[0] == "api" and parts[1] == "notes" and parts[3] == "delete":
            conn.execute("DELETE FROM notes WHERE id = ?", (parts[2],))
            conn.commit()
            return _json(start_response, "200 OK", {"ok": True})

        return _json(start_response, "404 Not Found", {"error": "not found"})
    except Exception as exc:
        return _json(start_response, "500 Internal Server Error", {"error": str(exc)[:200]})


app = application
