"""记录台 — the records hub for the App Hub.

One project hosting the user's record-keeping features ported from
myweb: daily stats, fuel log, history snapshots (+ tracker boards),
audio memos, snippets, and the /api2 universal store several pages
are built on (product / supplies / exercise). Each feature keeps its
original API shapes and its own SQLite file under .runtime/, so the
databases from the original project can be dropped in (via seed/ or
directly into .runtime/) and pick up where they left off. Access is
gated by the hub's unified login.
"""
import json
import os
import re
import shutil
import threading

import auth
import mod_audiom
import mod_health
import mod_history
import mod_manuals
import mod_snippets
import mod_stats
import mod_store
from health_page import HEALTH_PAGE
from pages import PAGES
from webutil import json_response, serve_file

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

# ---------------------------------------------------------- credentials
# Write-only API-key store in the platform data directory. Keys are
# used internally only: the API reports whether a key is configured
# and where it came from, never its value. An environment variable
# always wins over the in-app store.

CRED_FILE = os.path.join(RUNTIME, "credentials.json")
CRED_KEYS = ("SILICONFLOW_API_KEY", "DB_API_KEY")


def _cred_values():
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        values = data.get("values", {}) if isinstance(data, dict) else {}
        return {k: str(v) for k, v in values.items() if isinstance(v, str)}
    except Exception:
        return {}


def _api_key(name):
    env = os.getenv(name, "").strip()
    if env:
        return env
    return _cred_values().get(name, "").strip()


def _cred_status():
    values = _cred_values()
    out = {}
    for name in CRED_KEYS:
        if os.getenv(name, "").strip():
            out[name] = {"configured": True, "source": "env"}
        elif values.get(name, "").strip():
            out[name] = {"configured": True, "source": "app"}
        else:
            out[name] = {"configured": False, "source": None}
    return out


def _cred_save(updates):
    values = _cred_values()
    for name, val in updates.items():
        if name not in CRED_KEYS:
            return False, "不支持的凭据：" + name
        val = str(val or "").strip()
        if val:
            values[name] = val
        else:
            values.pop(name, None)
    try:
        os.makedirs(os.path.dirname(CRED_FILE), exist_ok=True)
        tmp = CRED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"values": values}, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CRED_FILE)
    except OSError:
        return False, "凭据保存失败。"
    return True, ""


def handle_credentials(environ, start_response, method):
    if method == "GET":
        return json_response(start_response, {"ok": True, "keys": _cred_status()})
    from webutil import read_json
    data = read_json(environ)
    if not isinstance(data, dict):
        return json_response(start_response, {"ok": False, "error": "请求体无效。"}, code=400)
    ok, err = _cred_save(data)
    if not ok:
        return json_response(start_response, {"ok": False, "error": err}, code=400)
    # Take effect immediately in the modules that captured keys at import.
    try:
        mod_history.VL_CONFIG["api_key"] = _api_key("SILICONFLOW_API_KEY")
    except Exception:
        pass
    try:
        mod_audiom.STT_CONFIG["api_key"] = _api_key("DB_API_KEY")
        mod_audiom.NLP_CONFIG["api_key"] = _api_key("SILICONFLOW_API_KEY")
    except Exception:
        pass
    return json_response(start_response, {"ok": True, "keys": _cred_status()})

SEED = os.path.join(BASE, "seed")
STATIC_DIR = os.path.join(BASE, "static")
COOKIE_NAME = "records_session"

_account, _account_from_env = auth.load_account()


def _client_ip(environ):
    fwd = environ.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return environ.get("REMOTE_ADDR", "") or "unknown"

_account_lock = threading.Lock()


def _init_storage():
    os.makedirs(RUNTIME, exist_ok=True)
    for name in ("universal_stats.db", "universal_datasmall.db", "snippets.db",
                 "vocal_data.db", "universal_data.db"):
        target = os.path.join(RUNTIME, name)
        source = os.path.join(SEED, name)
        if not os.path.exists(target) and os.path.exists(source):
            shutil.copyfile(source, target)
    for name in ("history", "assert-cloud", "audio_uploads", "raw_responses"):
        target = os.path.join(RUNTIME, name)
        source = os.path.join(SEED, name)
        if not os.path.exists(target) and os.path.isdir(source):
            shutil.copytree(source, target)
    mod_stats.init_db()
    mod_snippets.init_db()
    mod_history.init_db()
    mod_manuals.init_db()
    mod_audiom.init_db()
    mod_store.init_db()
    mod_health.init_db()


_init_storage()

# ---------------------------------------------------------------- auth


def _cookie_path(environ):
    prefix = environ.get("HTTP_X_FORWARDED_PREFIX") or ""
    return (prefix + "/") if prefix else "/"


def _session_cookie(environ, token, max_age):
    return ("Set-Cookie", "%s=%s; Path=%s; HttpOnly; SameSite=Strict; Max-Age=%d"
            % (COOKIE_NAME, token, _cookie_path(environ), max_age))


def _current_account():
    with _account_lock:
        return _account


def _auth_state(environ):
    """Identity verification lives at the platform layer (the App Hub
    gate in front of /web/<slug>/). This app performs no verification
    itself: any request that reaches it has either passed the
    platform gate or arrived on the local port, which is trusted."""
    account = _current_account()
    return True, "platform", (account.username if account else "local")

def _authed(environ):
    return _auth_state(environ)[0]


def _json_body(environ):
    from webutil import read_json
    return read_json(environ)


def handle_setup(environ, start_response):
    global _account
    with _account_lock:
        if _account is not None:
            return json_response(start_response,
                                 {"ok": False, "error": "setup_done", "message": "账户已设置，设置入口已关闭。"}, 409)
        data = _json_body(environ) or {}
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not (1 <= len(username) <= 128):
            return json_response(start_response,
                                 {"ok": False, "error": "bad_username", "message": "账户名需为 1–128 个字符。"}, 400)
        if len(password) < 10:
            return json_response(start_response,
                                 {"ok": False, "error": "short_password", "message": "密码至少 10 位。"}, 400)
        _account = auth.store_account(username, password)
        account = _account
    return json_response(start_response, {"ok": True, "username": account.username},
                         extra_headers=[_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_login(environ, start_response):
    account = _current_account()
    if account is None:
        return json_response(start_response,
                             {"ok": False, "error": "not_configured", "message": "尚未设置账户。"}, 503)
    data = _json_body(environ) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if username != account.username or not account.check_password(password):
        return json_response(start_response,
                             {"ok": False, "error": "bad_credentials", "message": "账户名或密码不正确。"}, 401)
    return json_response(start_response, {"ok": True, "username": account.username},
                         extra_headers=[_session_cookie(environ, account.make_token(), auth.SESSION_TTL)])


def handle_session(environ, start_response):
    account = _current_account()
    authed, via, username = _auth_state(environ)
    return json_response(start_response, {
        "ok": True,
        "setup_required": False,
        "authenticated": authed,
        "via": via,
        "username": username if authed else None,
    })

# ---------------------------------------------------------------- pages


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>记录台</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font-family: system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; background:#0f1116; color:#e6e8ee; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 28px 16px 40px; }
  h1 { font-size: 20px; margin: 0 0 6px; }
  p.sub { color:#8b93a7; font-size: 13px; margin: 0 0 22px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; }
  a.card { display:block; background:#161a22; border:1px solid #232a37; border-radius:12px; padding:16px 14px; color:inherit; text-decoration:none; transition:.15s; }
  a.card:hover { background:#1e2430; border-color:#3d6ea8; }
  a.card .t { font-size:15px; font-weight:600; margin-bottom:4px; }
  a.card .d { font-size:12px; color:#8b93a7; line-height:1.5; }
</style>
</head>
<body>
<div class="wrap">
  <h1>记录台</h1>
  <p class="sub">由 myweb 迁来的记录类功能，数据各自独立存储，互不干扰。</p>
  <div class="grid">
    <a class="card" href="daily"><div class="t">日常记录</div><div class="d">每日计数打卡，可导入导出 CSV</div></a>
    <a class="card" href="fuel"><div class="t">加油记录</div><div class="d">油量与里程，自动算油耗</div></a>
    <a class="card" href="history"><div class="t">历史快照</div><div class="d">生活切片照片，可选 AI 背景分析</div></a>
    <a class="card" href="audiom"><div class="t">录音备忘</div><div class="d">录音转文字与摘要（需转写接口）</div></a>
    <a class="card" href="manuals"><div class="t">智能说明书</div><div class="d">说明书照片 AI 识别与标签（需 SiliconFlow 密钥）</div></a>
    <a class="card" href="snippets"><div class="t">片段库</div><div class="d">代码与文本片段</div></a>
    <a class="card" href="product"><div class="t">产品追踪</div><div class="d">通用追踪看板</div></a>
    <a class="card" href="supplies"><div class="t">日常物资</div><div class="d">物资清单与状态</div></a>
    <a class="card" href="assert"><div class="t">资产看板</div><div class="d">资产条目与凭证图</div></a>
    <a class="card" href="cloud"><div class="t">云端日常</div><div class="d">日常数据同步页</div></a>
    <a class="card" href="exercise"><div class="t">运动记录</div><div class="d">健身配置与记录</div></a>
    <a class="card" href="health-record"><div class="t">健康记录</div><div class="d">食物、睡眠、状态与敏感项的私人记录</div></a>
  </div>

  <section style="margin-top:26px;background:#161a22;border:1px solid #232a37;border-radius:12px;padding:16px 14px">
    <div style="font-size:15px;font-weight:600;margin-bottom:4px">API 凭据</div>
    <p style="font-size:12px;color:#8b93a7;margin:0 0 12px;line-height:1.6">历史快照的 AI 分析、录音转写与摘要用的接口密钥。只保存在服务器内部使用，页面永不回显已保存的值；保存空值可清除，环境变量优先。</p>
    <label style="display:block;font-size:12px;color:#8b93a7;margin:10px 0 4px">SiliconFlow API Key <span id="cred-sf-status"></span></label>
    <div style="display:flex;gap:8px">
      <input id="cred-sf" type="password" autocomplete="off" placeholder="sk-…" style="flex:1;box-sizing:border-box;padding:9px 10px;border-radius:8px;border:1px solid #2a3242;background:#0f1116;color:#e6e8ee;font-size:14px">
      <button id="cred-sf-save" type="button" style="padding:9px 14px;border:0;border-radius:8px;background:#2f6fd0;color:#fff;font-size:13px;cursor:pointer">保存</button>
      <button id="cred-sf-clear" type="button" style="padding:9px 12px;border:1px solid #2a3242;border-radius:8px;background:transparent;color:#e6e8ee;font-size:13px;cursor:pointer">清除</button>
    </div>
    <label style="display:block;font-size:12px;color:#8b93a7;margin:12px 0 4px">火山引擎 DB_API_KEY（语音转写） <span id="cred-db-status"></span></label>
    <div style="display:flex;gap:8px">
      <input id="cred-db" type="password" autocomplete="off" style="flex:1;box-sizing:border-box;padding:9px 10px;border-radius:8px;border:1px solid #2a3242;background:#0f1116;color:#e6e8ee;font-size:14px">
      <button id="cred-db-save" type="button" style="padding:9px 14px;border:0;border-radius:8px;background:#2f6fd0;color:#fff;font-size:13px;cursor:pointer">保存</button>
      <button id="cred-db-clear" type="button" style="padding:9px 12px;border:1px solid #2a3242;border-radius:8px;background:transparent;color:#e6e8ee;font-size:13px;cursor:pointer">清除</button>
    </div>
    <div id="cred-msg" style="font-size:12px;color:#7fc97f;margin-top:10px;min-height:16px"></div>
  </section>
</div>
<script>
(function () {
  function load() {
    fetch("api/credentials", {cache: "no-store"}).then(function (r) { return r.json(); }).then(function (d) {
      var k = d.keys || {};
      function t(id, key) {
        var el = document.getElementById(id); if (!el) return;
        var s = k[key] || {};
        el.textContent = s.configured ? ("· 已配置（" + (s.source === "env" ? "环境变量" : "页面保存") + "）") : "· 未配置";
      }
      t("cred-sf-status", "SILICONFLOW_API_KEY"); t("cred-db-status", "DB_API_KEY");
    }).catch(function () {});
  }
  function save(key, inputId, clear) {
    var v = clear ? "" : document.getElementById(inputId).value.trim();
    if (!clear && !v) { document.getElementById("cred-msg").textContent = "请先粘贴 Key"; return; }
    var body = {}; body[key] = v;
    fetch("api/credentials", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(function (r) { return r.json(); }).then(function (d) {
        document.getElementById("cred-msg").textContent = d.ok ? (clear ? "已清除" : "已保存，立即生效") : (d.error || "保存失败");
        document.getElementById(inputId).value = "";
        load();
      }).catch(function () { document.getElementById("cred-msg").textContent = "保存失败"; });
  }
  document.getElementById("cred-sf-save").addEventListener("click", function () { save("SILICONFLOW_API_KEY", "cred-sf", false); });
  document.getElementById("cred-sf-clear").addEventListener("click", function () { save("SILICONFLOW_API_KEY", "cred-sf", true); });
  document.getElementById("cred-db-save").addEventListener("click", function () { save("DB_API_KEY", "cred-db", false); });
  document.getElementById("cred-db-clear").addEventListener("click", function () { save("DB_API_KEY", "cred-db", true); });
  load();
})();
</script>
</body>
</html>
"""


DATA_WIDGETS = {
    "cloud": {"mode": "tracker", "title": "云端日常 · 数据进出口",
              "note": "导出为 JSON 备份；导入会整体替换当前看板数据，请先导出留底。",
              "api": "tracker_data/sync/daily", "board": "daily"},
    "snippets": {"mode": "api", "title": "片段库 · 数据进出口",
                 "note": "完整备份（标题+内容）。导入按标题+内容去重，不覆盖已有片段。",
                 "export_path": "api/snippets/export", "import_path": "api/snippets/import",
                 "filename": "snippets-backup.json"},
    "history": {"mode": "api", "title": "历史快照 · 数据进出口",
                "note": "完整备份（含照片文件，体积可能较大）。导入按编号去重，不覆盖已有快照。",
                "export_path": "api/snapshots/export", "import_path": "api/snapshots/import",
                "filename": "snapshots-backup.json"},
    "audiom": {"mode": "api", "title": "录音备忘 · 数据进出口",
               "note": "完整备份（含音频与转写文本，体积可能较大）。导入按编号去重，不覆盖已有录音。",
               "export_path": "audiom/api/vocal/export", "import_path": "audiom/api/vocal/import",
               "filename": "vocal-backup.json"},
    "exercise": {"mode": "kv", "title": "运动记录 · 配置进出口",
                 "note": "导出/导入运动配置（gym_cfg）。导入会覆盖当前配置。",
                 "kv_key": "gym_cfg", "filename": "gym-cfg-backup.json"},
    "assert": {"mode": "tracker", "title": "资产看板 · 数据进出口",
               "note": "导出为 JSON 备份；导入会整体替换当前看板数据，请先导出留底。",
               "api": "tracker_data/sync/assets", "board": "assets"},
    "product": {"mode": "store", "title": "产品追踪 · 数据进出口",
                "note": "导出全部快照历史（通用存储）。导入会逐条追加快照，不去重，请先导出留底。",
                "collection": "data_tracking", "filename": "data-tracking-backup.json"},
    "supplies": {"mode": "store", "title": "日常物资 · 数据进出口",
                 "note": "导出全部快照历史（通用存储）。导入会逐条追加快照，不去重，请先导出留底。",
                 "collection": "daily_supplies", "filename": "supplies-backup.json"},
    "manuals": {"mode": "api", "title": "智能说明书 · 数据进出口",
                "note": "完整备份（含图片文件，体积可能较大）。导入按编号去重，不覆盖已有说明书。",
                "export_path": "api/manuals/export", "import_path": "api/manuals/import",
                "filename": "manuals-backup.json"},
}

DATA_WIDGET = """
<div id="diw" style="position:fixed;right:12px;bottom:12px;z-index:9998;font-family:system-ui,-apple-system,'PingFang SC',sans-serif">
  <button id="diw-btn" type="button" style="padding:8px 13px;border-radius:999px;border:1px solid #3a4356;background:#1b2130;color:#dfe5ef;font-size:13px;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.35)">数据进出口</button>
  <div id="diw-panel" hidden style="position:absolute;right:0;bottom:46px;width:290px;background:#171b24;border:1px solid #2a3242;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.5);padding:14px;color:#e6e8ee">
    <div id="diw-title" style="font-weight:600;font-size:14px"></div>
    <p id="diw-note" style="font-size:12px;color:#8b93a7;margin:6px 0 10px;line-height:1.55"></p>
    <button id="diw-export" type="button" style="width:100%;padding:9px;border:0;border-radius:8px;background:#2f6fd0;color:#fff;font-size:13px;cursor:pointer">导出备份</button>
    <div style="display:flex;gap:8px;margin-top:10px">
      <input id="diw-file" type="file" accept=".json,application/json" style="flex:1;font-size:12px;color:#8b93a7">
    </div>
    <button id="diw-import" type="button" style="width:100%;padding:9px;margin-top:8px;border:1px solid #2a3242;border-radius:8px;background:transparent;color:#e6e8ee;font-size:13px;cursor:pointer">导入所选文件</button>
    <div id="diw-msg" style="font-size:12px;color:#7fc97f;margin-top:8px;min-height:16px"></div>
  </div>
</div>
<script>
(function () {
  var CFG = __CFG__;
  var root = location.pathname.replace(/\\/[^/]*$/, "");
  function $(id) { return document.getElementById(id); }
  function msg(t) { $("diw-msg").textContent = t; }
  function download(name, blob) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 400);
  }
  function dlJson(name, obj) { download(name, new Blob([JSON.stringify(obj, null, 1)], {type: "application/json"})); }
  async function jget(u) { var r = await fetch(u, {cache: "no-store"}); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }
  async function jpost(u, body) {
    var r = await fetch(u, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    var d = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  $("diw-title").textContent = CFG.title;
  $("diw-note").textContent = CFG.note;
  $("diw-btn").addEventListener("click", function () { var p = $("diw-panel"); p.hidden = !p.hidden; });
  $("diw-export").addEventListener("click", async function () {
    try {
      if (CFG.mode === "tracker") {
        var d = await jget(root + "/" + CFG.api);
        var items = Array.isArray(d) ? ((d[0] || {}).items || []) : (d.items || []);
        dlJson(CFG.board + "-backup.json", {format: "tracker-backup", board: CFG.board, exported_at: Math.floor(Date.now() / 1000), items: items});
        msg("已导出 " + items.length + " 条。");
      } else if (CFG.mode === "kv") {
        var v = await jget(root + "/api2/kv/" + CFG.kv_key);
        dlJson(CFG.filename, {format: "kv-backup", key: CFG.kv_key, payload: v});
        msg("配置已导出。");
      } else if (CFG.mode === "store") {
        var rows = await jget(root + "/api2/" + CFG.collection);
        dlJson(CFG.filename, {format: "store-backup", collection: CFG.collection, exported_at: Math.floor(Date.now() / 1000), items: rows});
        msg("已导出 " + rows.length + " 条快照。");
      } else {
        var r = await fetch(root + "/" + CFG.export_path, {cache: "no-store"});
        if (!r.ok) throw new Error("HTTP " + r.status);
        download(CFG.filename, await r.blob());
        msg("备份已导出。");
      }
    } catch (e) { msg("导出失败：" + e.message); }
  });
  $("diw-import").addEventListener("click", async function () {
    var f = $("diw-file").files[0];
    if (!f) { msg("请先选择备份文件"); return; }
    try {
      var parsed = JSON.parse(await f.text());
      if (CFG.mode === "tracker") {
        var items = parsed.items || (Array.isArray(parsed) ? ((parsed[0] || {}).items) : null);
        if (!Array.isArray(items)) throw new Error("备份里没有 items");
        if (!confirm("导入将整体替换当前看板数据，确定继续？")) return;
        await jpost(root + "/" + CFG.api, {items: items});
        msg("导入完成（整体替换 " + items.length + " 条），刷新页面查看。");
      } else if (CFG.mode === "kv") {
        var payload = ("payload" in parsed) ? parsed.payload : parsed;
        if (!confirm("导入将覆盖当前配置，确定继续？")) return;
        await jpost(root + "/api2/kv/" + CFG.kv_key, {payload: payload});
        msg("配置已导入，刷新页面查看。");
      } else if (CFG.mode === "store") {
        var sitems = Array.isArray(parsed) ? parsed : (parsed.items || []);
        if (!sitems.length) throw new Error("备份里没有快照");
        if (!confirm("导入将逐条追加 " + sitems.length + " 条快照（不去重），确定继续？")) return;
        for (var i = 0; i < sitems.length; i++) {
          var row = Object.assign({}, sitems[i]);
          delete row._time;
          await jpost(root + "/api2/" + CFG.collection, row);
        }
        msg("导入完成：追加 " + sitems.length + " 条快照，刷新页面查看。");
      } else {
        var d = await jpost(root + "/" + CFG.import_path, parsed);
        msg("导入完成：新增 " + (d.added || 0) + "，跳过 " + (d.skipped || 0) + "，刷新页面查看。");
      }
    } catch (e) { msg("导入失败：" + e.message); }
  });
})();
</script>
"""


HEALTH_SW = """
const CACHE = "health-shell-v1";
self.addEventListener("install", (e) => { self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method === "GET" && url.pathname.endsWith("/health-record")) {
    e.respondWith(
      caches.open(CACHE).then((c) =>
        fetch(e.request).then((r) => { c.put(e.request, r.clone()); return r; })
          .catch(() => c.match(e.request))
      )
    );
  }
});
"""


def _page_response(start_response, html):
    body = html.encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                              ("Content-Length", str(len(body)))])
    return [body]

# ---------------------------------------------------------------- router


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"

    if path == "/health" and method == "GET":
        return json_response(start_response, {"ok": True})
    if path == "/api/session" and method == "GET":
        return handle_session(environ, start_response)
    if path in ("/api/setup", "/api/login") and method == "POST":
        return json_response(start_response,
                         {"ok": False, "error": "not_found", "message": "接口不存在。"}, 404)
    if path == "/api/logout" and method == "POST":
        return json_response(start_response, {"ok": True},
                             extra_headers=[_session_cookie(environ, "", 0)])

    if path == "/" and method == "GET":
        return _page_response(start_response, INDEX_HTML)
    if path == "/health-record" and method == "GET":
        return _page_response(start_response, HEALTH_PAGE)
    if path == "/health-manifest.json" and method == "GET":
        return json_response(start_response, {
            "name": "健康记录", "short_name": "健康记录",
            "display": "standalone", "background_color": "#0f1116",
            "theme_color": "#0f1116", "start_url": "health-record",
            "scope": ".", "icons": []})
    if path == "/health-sw.js" and method == "GET":
        body = HEALTH_SW.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/javascript; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]
    if method == "GET" and path[1:] in PAGES:
        html = PAGES[path[1:]]
        cfg = DATA_WIDGETS.get(path[1:])
        if cfg:
            widget = DATA_WIDGET.replace("__CFG__", json.dumps(cfg, ensure_ascii=False))
            html = (html.replace("</body>", widget + "</body>", 1)
                    if "</body>" in html else html + widget)
        return _page_response(start_response, html)
    if method == "GET" and path.startswith("/static/"):
        rel = path[len("/static/"):]
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if full.startswith(STATIC_DIR):
            return serve_file(start_response, full, environ)
        return json_response(start_response, {"error": "not found"}, 404)

    if method == "OPTIONS" and path.startswith("/audiom/"):
        start_response("204 No Content", [("Content-Length", "0")])
        return [b""]

    if not _authed(environ):
        return json_response(start_response,
                             {"ok": False, "error": "unauthorized", "message": "请先登录。"}, 401)

    # /api2 universal store (kv before collections)
    m = re.fullmatch(r"/api2/kv/([^/]+)", path)
    if m and method in ("GET", "POST"):
        from urllib.parse import unquote
        return mod_store.kv_main(environ, start_response, unquote(m.group(1)))
    m = re.fullmatch(r"/api2/([^/]+)", path)
    if m and method in ("GET", "POST"):
        from urllib.parse import unquote
        return mod_store.collection_main(environ, start_response, unquote(m.group(1)))

    # daily + fuel
    if path == "/api/credentials" and method in ("GET", "POST"):
        return handle_credentials(environ, start_response, method)
    if path.startswith("/api/health/"):
        return mod_health.api(environ, start_response)
    if path == "/api/daily_stats" and method in ("GET", "POST"):
        return mod_stats.daily_main(environ, start_response)
    if path == "/api/daily_stats/export" and method == "GET":
        return mod_stats.daily_export(environ, start_response)
    if path == "/api/daily_stats/import" and method == "POST":
        return mod_stats.daily_import(environ, start_response)
    m = re.fullmatch(r"/api/daily_stats/(\d+)", path)
    if m and method == "DELETE":
        return mod_stats.daily_delete(environ, start_response, int(m.group(1)))
    if path == "/api/fuel_stats" and method in ("GET", "POST"):
        return mod_stats.fuel_main(environ, start_response)
    if path == "/api/fuel_stats/export" and method == "GET":
        return mod_stats.fuel_export(environ, start_response)
    if path == "/api/fuel_stats/import" and method == "POST":
        return mod_stats.fuel_import(environ, start_response)
    m = re.fullmatch(r"/api/fuel_stats/(\d+)", path)
    if m and method == "DELETE":
        return mod_stats.fuel_delete(environ, start_response, int(m.group(1)))

    # snippets
    if path == "/api/snippets/export" and method == "GET":
        return mod_snippets.export_snippets(environ, start_response)
    if path == "/api/snippets/import" and method == "POST":
        return mod_snippets.import_snippets(environ, start_response)
    if path == "/api/snippets/list" and method == "GET":
        return mod_snippets.list_snippets(environ, start_response)
    if path == "/api/snippets/save" and method == "POST":
        return mod_snippets.save_snippet(environ, start_response)
    m = re.fullmatch(r"/api/snippets/raw/(\d+)", path)
    if m and method == "GET":
        return mod_snippets.get_snippet(environ, start_response, int(m.group(1)))
    m = re.fullmatch(r"/api/snippets/delete/(\d+)", path)
    if m and method == "DELETE":
        return mod_snippets.delete_snippet(environ, start_response, int(m.group(1)))

    # history snapshots + tracker
    if path == "/api/snapshots/export" and method == "GET":
        return mod_history.export_snapshots(environ, start_response)
    if path == "/api/snapshots/import" and method == "POST":
        return mod_history.import_snapshots(environ, start_response)
    if path == "/api/snapshots" and method == "GET":
        return mod_history.list_snapshots(environ, start_response)
    if path == "/api/snapshots/upload" and method == "POST":
        return mod_history.upload_snapshot(environ, start_response)
    if path == "/api/snapshots/analyze" and method == "POST":
        return mod_history.analyze_snapshot(environ, start_response)
    m = re.fullmatch(r"/api/snapshots/([^/]+)", path)
    if m and method == "DELETE":
        from urllib.parse import unquote
        return mod_history.delete_snapshot(environ, start_response, unquote(m.group(1)))
    m = re.fullmatch(r"/api/img/snapshots/([^/]+)", path)
    if m and method == "GET":
        from urllib.parse import unquote
        return mod_history.snapshot_image(environ, start_response, unquote(m.group(1)))
    if path == "/tracker_data/sync/daily" and method in ("GET", "POST"):
        return mod_history.tracker_sync(environ, start_response, "daily_data")
    if path == "/tracker_data/sync/assets" and method in ("GET", "POST"):
        return mod_history.tracker_sync(environ, start_response, "assets_data")
    m = re.fullmatch(r"/tracker/image/([^/]+)", path)
    if m and method == "GET":
        from urllib.parse import unquote
        return mod_history.tracker_image(environ, start_response, unquote(m.group(1)))

    # audio memos
    if path == "/audiom/api/vocal/export" and method == "GET":
        return mod_audiom.export_vocal(environ, start_response)
    if path == "/audiom/api/vocal/import" and method == "POST":
        return mod_audiom.import_vocal(environ, start_response)
    if path == "/audiom/api/vocal" and method == "GET":
        return mod_audiom.get_records(environ, start_response)
    if path == "/audiom/api/vocal/upload" and method == "POST":
        return mod_audiom.upload_audio(environ, start_response)
    if path == "/audiom/api/vocal/start" and method == "POST":
        return mod_audiom.start_processing(environ, start_response)
    if path == "/audiom/api/vocal/retry" and method == "POST":
        return mod_audiom.retry_processing(environ, start_response)
    m = re.fullmatch(r"/audiom/api/vocal/audio/([^/]+)", path)
    if m and method == "GET":
        from urllib.parse import unquote
        return mod_audiom.get_audio(environ, start_response, unquote(m.group(1)))
    m = re.fullmatch(r"/audiom/api/vocal/([^/]+)", path)
    if m and method == "DELETE":
        from urllib.parse import unquote
        return mod_audiom.delete_record(environ, start_response, unquote(m.group(1)))

    # smart manuals (AI OCR)
    if path == "/api/manuals" and method == "GET":
        return mod_manuals.manuals_list(environ, start_response)
    if path == "/api/manuals/upload" and method == "POST":
        return mod_manuals.manuals_upload(environ, start_response)
    if path == "/api/manuals/start" and method == "POST":
        return mod_manuals.manuals_start(environ, start_response)
    if path == "/api/manuals/retry" and method == "POST":
        return mod_manuals.manuals_retry(environ, start_response)
    if path == "/api/manuals/reanalyze" and method == "POST":
        return mod_manuals.manuals_reanalyze(environ, start_response)
    if path == "/api/manuals/export" and method == "GET":
        return mod_manuals.manuals_export(environ, start_response)
    if path == "/api/manuals/import" and method == "POST":
        return mod_manuals.manuals_import(environ, start_response)
    m = re.fullmatch(r"/api/manuals/img/([^/]+)", path)
    if m and method == "GET":
        from urllib.parse import unquote
        return mod_manuals.manuals_img(environ, start_response, unquote(m.group(1)))
    m = re.fullmatch(r"/api/manuals/([^/]+)", path)
    if m and method == "DELETE":
        from urllib.parse import unquote
        return mod_manuals.manuals_delete(environ, start_response, unquote(m.group(1)))

    return json_response(start_response,
                         {"ok": False, "error": "not_found", "message": "接口不存在。"}, 404)


app = application
