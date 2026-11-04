"""Health-record single-page app (served at /health-record).

Mobile-first implementation of the user's health-record design:
today timeline, 3-step quick-record sheet, calendar history, light
insights, display codes + stealth mode for sensitive categories,
offline create-outbox, JSON/CSV export and dedupe import.
"""

HEALTH_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>健康记录</title>
<link rel="manifest" href="health-manifest.json">
<meta name="theme-color" content="#0f1116">
<style>
  :root { color-scheme: dark; --bg:#0f1116; --panel:#161a22; --line:#232a37; --mut:#8b93a7; --acc:#2f6fd0; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; background:var(--bg); color:#e6e8ee; font-family: system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width: 640px; margin: 0 auto; padding: 14px 14px 96px; }
  header.top { display:flex; align-items:center; gap:8px; padding: 6px 2px 10px; }
  header.top h1 { font-size: 18px; margin: 0; flex: 0 0 auto; }
  #sync-dot { width:10px; height:10px; border-radius:50%; background:#43a06a; flex:0 0 auto; }
  #sync-dot.pending { background:#e8a13a; }
  #sync-label { font-size:12px; color:var(--mut); flex:1; }
  .datebar { display:flex; align-items:center; gap:10px; margin: 2px 0 12px; }
  .datebar button, .chipbtn { background:var(--panel); border:1px solid var(--line); color:#e6e8ee; border-radius:8px; padding:6px 12px; font-size:13px; cursor:pointer; }
  .datebar .cur { font-size:16px; font-weight:600; flex:1; text-align:center; }
  .chips3 { display:grid; grid-template-columns: repeat(3, 1fr); gap:10px; margin-bottom:14px; }
  .sumcard { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:10px 12px; }
  .sumcard .k { font-size:12px; color:var(--mut); }
  .sumcard .v { font-size:20px; font-weight:600; margin-top:2px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin-bottom:12px; }
  .card.warn { border-color:#6a5320; }
  .card h3 { margin:0 0 6px; font-size:14px; }
  .card p { margin:4px 0; font-size:13px; color:var(--mut); line-height:1.6; }
  .card .go { color:var(--acc); font-size:13px; cursor:pointer; }
  .grouplabel { font-size:12px; color:var(--mut); margin:14px 2px 8px; }
  .rec { display:flex; gap:10px; align-items:flex-start; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:10px 12px; margin-bottom:8px; cursor:pointer; }
  .dot { width:10px; height:10px; border-radius:50%; margin-top:6px; flex:0 0 auto; }
  .rec .main { flex:1; min-width:0; }
  .rec .title { font-size:14px; font-weight:600; }
  .rec .val { font-size:13px; color:#c9d2e0; margin-left:6px; font-weight:400; }
  .rec .meta { font-size:12px; color:var(--mut); margin-top:3px; line-height:1.55; word-break:break-word; }
  .rec .time { font-size:12px; color:var(--mut); flex:0 0 auto; }
  .tag { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:1px 8px; font-size:11px; color:var(--mut); margin:2px 4px 0 0; }
  nav.bottom { position:fixed; left:0; right:0; bottom:0; background:rgba(15,17,22,.96); border-top:1px solid var(--line); display:flex; align-items:stretch; z-index:40; padding-bottom: env(safe-area-inset-bottom); }
  nav.bottom .inner { max-width:640px; margin:0 auto; display:flex; width:100%; }
  nav.bottom button { flex:1; background:none; border:0; color:var(--mut); font-size:12px; padding:10px 0 12px; cursor:pointer; }
  nav.bottom button.on { color:#fff; font-weight:600; }
  nav.bottom button.add { flex:0 0 64px; }
  nav.bottom button.add span { display:block; width:44px; height:44px; line-height:42px; margin:2px auto 0; border-radius:50%; background:var(--acc); color:#fff; font-size:26px; }
  .sheet-wrap { position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:60; display:none; }
  .sheet-wrap.open { display:block; }
  .sheet { position:absolute; left:0; right:0; bottom:0; max-width:640px; margin:0 auto; background:#171b24; border-radius:16px 16px 0 0; padding:16px 16px calc(18px + env(safe-area-inset-bottom)); max-height:88vh; overflow-y:auto; }
  .sheet h3 { margin:0 0 10px; font-size:16px; }
  .steplabel { font-size:12px; color:var(--mut); margin:14px 0 8px; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; }
  .chip { border:1px solid var(--line); background:var(--panel); color:#e6e8ee; border-radius:999px; padding:7px 13px; font-size:13px; cursor:pointer; }
  .chip.on { background:var(--acc); border-color:var(--acc); color:#fff; }
  .chip.bristol { border-radius:10px; padding:8px 10px; text-align:center; min-width:64px; }
  .chip.bristol small { display:block; font-size:11px; color:var(--mut); margin-top:2px; }
  .chip.bristol.on small { color:#dce8ff; }
  input[type=text], input[type=number], input[type=datetime-local], input[type=file], textarea, select {
    width:100%; padding:10px 12px; border-radius:10px; border:1px solid var(--line); background:#0f1116; color:#e6e8ee; font-size:14px; }
  .row2 { display:flex; gap:8px; } .row2 > * { flex:1; }
  .btn { display:inline-block; border:0; border-radius:10px; padding:11px 18px; font-size:14px; cursor:pointer; background:var(--acc); color:#fff; }
  .btn.ghost { background:transparent; border:1px solid var(--line); color:#e6e8ee; }
  .btn.danger { background:#5a2323; }
  .btn.block { width:100%; margin-top:16px; }
  .cal { display:grid; grid-template-columns: repeat(7, 1fr); gap:4px; }
  .cal .dow { font-size:11px; color:var(--mut); text-align:center; padding:4px 0; }
  .cal .day { text-align:center; padding:7px 0 5px; border-radius:10px; cursor:pointer; border:1px solid transparent; }
  .cal .day.sel { border-color:var(--acc); }
  .cal .day .n { font-size:13px; }
  .cal .dots { height:6px; margin-top:3px; }
  .cal .dots i { display:inline-block; width:5px; height:5px; border-radius:50%; margin:0 1px; }
  .trendrow { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--mut); margin:5px 0; }
  .trendrow .bar { flex:1; height:10px; background:#0f1116; border-radius:5px; overflow:hidden; }
  .trendrow .bar i { display:block; height:100%; background:var(--acc); }
  .kv { display:flex; align-items:center; gap:10px; margin:10px 0; }
  .kv label { flex:0 0 74px; font-size:13px; color:var(--mut); }
  .kv input { flex:1; }
  .switch { position:relative; width:44px; height:24px; flex:0 0 auto; }
  .switch input { opacity:0; width:100%; height:100%; position:absolute; margin:0; cursor:pointer; }
  .switch i { position:absolute; inset:0; background:#2a3242; border-radius:999px; }
  .switch i:after { content:""; position:absolute; width:18px; height:18px; border-radius:50%; background:#fff; top:3px; left:3px; transition:.15s; }
  .switch input:checked + i { background:var(--acc); }
  .switch input:checked + i:after { left:23px; }
  .hidden { display:none !important; }
  .mut { color:var(--mut); font-size:12px; line-height:1.6; }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>健康记录</h1><span id="sync-dot"></span><span id="sync-label">已同步</span>
    <a href="./" style="font-size:12px;color:var(--mut);text-decoration:none">记录台</a>
  </header>

  <section id="view-today">
    <div class="datebar">
      <button id="d-prev">‹ 前一天</button>
      <div class="cur" id="d-title"></div>
      <button id="d-next">后一天 ›</button>
    </div>
    <div class="chips3" id="summary"></div>
    <div id="suggests"></div>
    <div class="card" id="morning-card">
      <h3>晨间打卡</h3>
      <p>一次填完睡眠、心情、精力，不用记三次。</p>
      <span class="go" id="morning-go">开始打卡 ›</span>
    </div>
    <div class="card">
      <h3>最近常用</h3>
      <div class="chips" id="recent"></div>
    </div>
    <div id="timeline"></div>
  </section>

  <section id="view-history" class="hidden">
    <div class="datebar">
      <button id="m-prev">‹ 上月</button>
      <div class="cur" id="m-title"></div>
      <button id="m-next">下月 ›</button>
    </div>
    <div class="chips" id="hist-filter" style="margin-bottom:10px"></div>
    <div class="card"><div class="cal" id="calendar"></div></div>
    <div id="hist-day"></div>
  </section>

  <section id="view-insights" class="hidden">
    <div id="insights-body"></div>
  </section>

  <section id="view-me" class="hidden">
    <div class="card">
      <h3>隐私</h3>
      <div class="kv"><label style="flex:1">隐蔽模式（敏感项只显示代号）</label>
        <span class="switch"><input type="checkbox" id="me-stealth"><i></i></span></div>
      <p class="mut">敏感类别在所有列表里都只显示代号（见下方映射）。开启隐蔽模式后再进一步：敏感记录的值、标签和备注也隐藏，只剩时间和代号，点开详情需再次确认。</p>
    </div>
    <div class="card">
      <h3>代号映射</h3>
      <p class="mut">敏感类别在时间线、筛选、快速记录和洞察里显示的代号，可改成任何词。底层存的仍是真实类别，统计和导出不受影响。</p>
      <div id="me-codes"></div>
      <button class="btn" id="me-codes-save">保存代号</button>
    </div>
    <div class="card">
      <h3>预测建议开关</h3>
      <p class="mut">规律稳定的类别会在今日页给一张建议卡，只在应用内显示，永不推送。</p>
      <div id="me-predict"></div>
      <button class="btn" id="me-predict-save">保存开关</button>
    </div>
    <div class="card">
      <h3>导出备份</h3>
      <p class="mut">JSON 为全量备份（可完整恢复，含代号映射与预设项）；CSV 适合表格分析。离线也能导出。</p>
      <div class="row2" style="margin:8px 0">
        <select id="exp-format"><option value="json">JSON 全量备份</option><option value="csv">CSV 表格</option></select>
        <select id="exp-names"><option value="real">真实类别名</option><option value="code">代号</option></select>
      </div>
      <button class="btn block" id="me-export">导出</button>
    </div>
    <div class="card">
      <h3>导入恢复</h3>
      <p class="mut">选择之前导出的 JSON 备份。先预览（新增/更新/冲突条数），确认后才写入；按记录编号去重，重复导入同一文件新增为零，已删除的记录不会复活。</p>
      <input type="file" id="imp-file" accept=".json,application/json">
      <div id="imp-preview" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <h3>关于</h3>
      <p class="mut">数据只保存在这台服务器的记录台数据目录中，不接入任何云端识别或分析。相关性洞察只讲相关、注明样本量，不做医疗判断。</p>
    </div>
  </section>
</div>

<nav class="bottom"><div class="inner">
  <button data-view="today" class="on">今日</button>
  <button data-view="history">历史</button>
  <button class="add" id="btn-add"><span>＋</span></button>
  <button data-view="insights">洞察</button>
  <button data-view="me">我的</button>
</div></nav>

<div class="sheet-wrap" id="sheet-wrap">
  <div class="sheet" id="sheet"></div>
</div>

<script>
"use strict";
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const API = "api/health";
let CATS = {}, PRESETS = {}, FOOD_ATTRS = [], CTX_TAGS = [];
let TAG_POOLS = {}, CAT_COUNTS = {}, ITEM_COUNTS = {}, CAT_ORDER = [];
const DEFAULT_ORDER = ["stool", "food", "sleep", "symptom", "exercise", "medication", "intimacy", "state"];
const SENSITIVE_DEFAULT_ITEMS = { stool: "排便", intimacy: "性生活" };
let SETTINGS = { codes: {}, predict: {} };
let viewDate = todayStr();
let histMonth = todayStr().slice(0, 7), histDate = todayStr(), histFilter = "";
let sheetState = null;

function pad(n) { return String(n).padStart(2, "0"); }
function todayStr(d) { d = d || new Date(); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
function fmtTime(ts) { const d = new Date(ts * 1000); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
function stealth() { return localStorage.getItem("health_stealth") === "1"; }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

async function api(path, opts) {
  const r = await fetch(API + path, Object.assign({ cache: "no-store" }, opts || {}));
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}
function apiPost(path, body) { return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }

/* ---------------- outbox (offline creates) ---------------- */
function outbox() { try { return JSON.parse(localStorage.getItem("health_outbox") || "[]"); } catch (e) { return []; } }
function setOutbox(q) { localStorage.setItem("health_outbox", JSON.stringify(q)); syncLabel(); }
function syncLabel() {
  const n = outbox().length;
  $("#sync-dot").className = n ? "pending" : "";
  $("#sync-dot").id = "sync-dot";
  $("#sync-label").textContent = n ? ("离线已存本地 · 待同步 " + n + " 条") : "已同步";
}
async function flushOutbox() {
  const q = outbox(); if (!q.length) return;
  const rest = [];
  for (const item of q) {
    try { await apiPost("/records", item); } catch (e) { rest.push(item); }
  }
  setOutbox(rest);
  if (rest.length !== q.length) loadToday();
}
function uuid() { return Array.from(crypto.getRandomValues(new Uint8Array(16))).map(b => b.toString(16).padStart(2, "0")).join(""); }

/* ---------------- display helpers ---------------- */
function isSensitive(rec) { return CATS[rec.category] && CATS[rec.category].sensitive; }
function codeOf(cat) { return SETTINGS.codes[cat] || (CATS[cat] || {}).code || cat; }
function catLabel(cat) {
  return (CATS[cat] && CATS[cat].sensitive) ? codeOf(cat) : (CATS[cat] || {}).name || cat;
}
function itemLabel(cat, item) {
  if (CATS[cat] && CATS[cat].sensitive && SENSITIVE_DEFAULT_ITEMS[cat] === item) return codeOf(cat);
  return item;
}
function dispTitle(rec) { return itemLabel(rec.category, rec.item); }
function computeCatOrder() {
  CAT_ORDER = Object.keys(CATS).sort((a, b) =>
    ((CAT_COUNTS[b] || 0) - (CAT_COUNTS[a] || 0)) ||
    (DEFAULT_ORDER.indexOf(a) - DEFAULT_ORDER.indexOf(b)));
}
function sortedItems(cat) {
  const counts = ITEM_COUNTS[cat] || {};
  return (PRESETS[cat] || []).slice().sort((a, b) => (counts[b] || 0) - (counts[a] || 0));
}
function valueText(rec) {
  const v = rec.value; if (!v) return "";
  if (v.type === "bool") return v.v ? "已记录" : "";
  if (v.type === "enum") {
    if (rec.category === "stool") {
      const desc = { 1: "偏干·硬块", 2: "偏干", 3: "略干", 4: "正常·顺滑", 5: "正常·软", 6: "偏稀", 7: "稀水" };
      return v.v + " 型 · " + (desc[v.v] || "");
    }
    return "热量" + v.v;
  }
  if (v.type === "measure") return v.v + (v.unit || "");
  if (v.type === "count") return v.v + " 次";
  if (v.type === "scale") return v.v + " 分";
  return "";
}
function recRow(rec) {
  const st = stealth() && isSensitive(rec);
  const color = st ? "#5a6376" : (CATS[rec.category] || {}).color || "#888";
  const tags = st ? "" : (rec.tags || []).map(t => '<span class="tag">' + esc(t) + "</span>").join("");
  const note = st ? "" : esc(rec.note || "");
  const meta = [tags, note].filter(Boolean).join(" ");
  return '<div class="rec" data-id="' + rec.id + '"><span class="dot" style="background:' + color + '"></span>' +
    '<div class="main"><span class="title">' + esc(dispTitle(rec)) + '</span>' +
    (st ? "" : '<span class="val">' + esc(valueText(rec)) + "</span>") +
    (meta ? '<div class="meta">' + meta + "</div>" : "") + "</div>" +
    '<span class="time">' + fmtTime(rec.ts) + "</span></div>";
}
function bindRows(root, records) {
  $$(".rec", root).forEach(el => el.addEventListener("click", () => {
    const rec = records.find(r => r.id === el.dataset.id);
    if (!rec) return;
    if (stealth() && isSensitive(rec) && !confirm("这是敏感记录，确认查看详情？")) return;
    openSheet({ mode: "edit", rec });
  }));
}
function groupLabel(ts) { const h = new Date(ts * 1000).getHours(); return h < 12 ? "上午" : (h < 18 ? "下午" : "晚上"); }

/* ---------------- today ---------------- */
async function loadToday() {
  $("#d-title").textContent = viewDate === todayStr() ? "今天 · " + viewDate : viewDate;
  let d;
  try { d = await api("/day?date=" + viewDate); } catch (e) { $("#timeline").innerHTML = '<p class="mut">读取失败：' + esc(e.message) + "</p>"; return; }
  const recs = d.records || [];
  const s = d.summary || {};
  const cell = (k, v, unit) => '<div class="sumcard"><div class="k">' + k + '</div><div class="v">' + (v == null ? "—" : v) + (v == null ? "" : '<small style="font-size:12px;color:var(--mut)"> ' + unit + "</small>") + "</div></div>";
  $("#summary").innerHTML = cell("心情", s["心情"], "分") + cell("精力", s["精力"], "分") + cell("睡眠", s["睡眠"], "小时");
  $("#morning-card").style.display = viewDate === todayStr() ? "" : "none";
  try {
    const p = await api("/presets");
    CAT_COUNTS = p.cat_counts || CAT_COUNTS; ITEM_COUNTS = p.item_counts || ITEM_COUNTS;
    TAG_POOLS = p.tag_pools || TAG_POOLS; computeCatOrder();
  } catch (e) {}
  const groups = { "上午": [], "下午": [], "晚上": [] };
  recs.slice().sort((a, b) => b.ts - a.ts).forEach(r => groups[groupLabel(r.ts)].push(r));
  $("#timeline").innerHTML = ["晚上", "下午", "上午"].filter(g => groups[g].length)
    .map(g => '<div class="grouplabel">' + g + "</div>" + groups[g].map(recRow).join("")).join("") ||
    '<p class="mut" style="text-align:center;margin-top:26px">这一天还没有记录，点底部 ＋ 记一条。</p>';
  bindRows($("#timeline"), recs);
  const counts = {};
  recs.forEach(r => { counts[r.category + "|" + r.item] = (counts[r.category + "|" + r.item] || 0) + 1; });
  try {
    const all = await api("/month?ym=" + viewDate.slice(0, 7));
  } catch (e) {}
  renderRecent(recs);
  if (viewDate === todayStr()) loadSuggests(); else $("#suggests").innerHTML = "";
}
function renderRecent(recs) {
  const seen = [], keys = new Set();
  recs.slice().sort((a, b) => b.ts - a.ts).forEach(r => {
    const k = r.category + "|" + r.item;
    if (!keys.has(k)) { keys.add(k); seen.push(r); }
  });
  $("#recent").innerHTML = seen.slice(0, 8).map(r =>
    '<button class="chip" data-cat="' + r.category + '" data-item="' + esc(r.item) + '">' + esc(dispTitle(r)) + "</button>").join("") ||
    '<span class="mut">暂无</span>';
  $$("#recent .chip").forEach(c => c.addEventListener("click", () =>
    openSheet({ mode: "create", category: c.dataset.cat, item: c.dataset.item, date: viewDate })));
}
async function loadSuggests() {
  try {
    const d = await api("/suggest");
    $("#suggests").innerHTML = (d.suggestions || []).map(sg => {
      const meta = CATS[sg.category] || {};
      const name = meta.sensitive ? codeOf(sg.category) : sg.item;
      return '<div class="card warn"><h3>可能要记：' + esc(name) + "</h3><p>平时在 " + sg.window +
        " 之间，共 " + sg.count + " 次，今天还没记。</p>" +
        '<span class="go" data-cat="' + sg.category + '" data-item="' + esc(sg.item) + '">记一条 ›</span></div>';
    }).join("");
    $$("#suggests .go").forEach(g => g.addEventListener("click", () =>
      openSheet({ mode: "create", category: g.dataset.cat, item: g.dataset.item, date: todayStr() })));
  } catch (e) { $("#suggests").innerHTML = ""; }
}

/* ---------------- quick record sheet ---------------- */
const VALUE_CONF = {
  food: { kind: "enum", label: "热量档（可选）", options: ["低", "中", "高"] },
  stool: { kind: "bristol", label: "布里斯托分型" },
  intimacy: { kind: "bool", label: "" },
  state: { kind: "scale", label: "程度（1 很少 – 5 很强）" },
  sleep: { kind: "measure", label: "时长", unit: "小时", step: 0.5 },
  exercise: { kind: "measure", label: "时长", unit: "分钟", step: 5 },
  medication: { kind: "bool", label: "" },
  symptom: { kind: "scale", label: "程度（1 轻微 – 5 严重）" }
};
function defaultMeal() { const h = new Date().getHours(); return h < 10 ? "早餐" : h < 15 ? "午餐" : h < 21 ? "晚餐" : "夜宵"; }

function openSheet(opts) {
  opts = opts || {};
  const rec = opts.rec || null;
  sheetState = {
    mode: opts.mode || "create",
    rec: rec,
    note: rec ? (rec.note || "") : "",
    id: rec ? rec.id : uuid(),
    category: rec ? rec.category : (opts.category || CAT_ORDER[0] || "state"),
    item: rec ? rec.item : (opts.item || ""),
    value: rec ? rec.value : null,
    tags: rec ? (rec.tags || []).slice() : [],
    date: rec ? rec.date : (opts.date || viewDate),
    morning: !!opts.morning,
    mSleep: "", mMood: null, mEnergy: null
  };
  if (sheetState.category === "food" && !sheetState.item) sheetState.item = defaultMeal();
  renderSheet();
  $("#sheet-wrap").classList.add("open");
}
function closeSheet() { $("#sheet-wrap").classList.remove("open"); sheetState = null; }

function renderSheet() {
  const st = sheetState, el = $("#sheet");
  if (st.morning) { renderMorning(el); return; }
  const catChips = CAT_ORDER.map(c =>
    '<button class="chip' + (st.category === c ? " on" : "") + '" data-act="cat" data-v="' + c + '">' +
    esc(catLabel(c)) + "</button>").join("");
  const items = sortedItems(st.category);
  const itemChips = items.map(i =>
    '<button class="chip' + (st.item === i ? " on" : "") + '" data-act="item" data-v="' + esc(i) + '">' + esc(itemLabel(st.category, i)) + "</button>").join("");
  const conf = VALUE_CONF[st.category];
  let valueHtml = "";
  if (conf.kind === "scale") {
    valueHtml = '<div class="steplabel">' + conf.label + '</div><div class="chips">' +
      [1, 2, 3, 4, 5].map(n => '<button class="chip' + (st.value && st.value.v === n ? " on" : "") + '" data-act="val" data-v="' + n + '">' + n + "</button>").join("") + "</div>";
  } else if (conf.kind === "bristol") {
    const desc = { 1: "偏干·硬块", 2: "偏干", 3: "略干", 4: "正常·顺滑", 5: "正常·软", 6: "偏稀", 7: "稀水" };
    valueHtml = '<div class="steplabel">' + conf.label + '</div><div class="chips">' +
      [1, 2, 3, 4, 5, 6, 7].map(n => '<button class="chip bristol' + (st.value && st.value.v === n ? " on" : "") + '" data-act="val" data-v="' + n + '">' + n + " 型<small>" + desc[n] + "</small></button>").join("") + "</div>";
  } else if (conf.kind === "enum") {
    valueHtml = '<div class="steplabel">' + conf.label + '</div><div class="chips">' +
      conf.options.map(o => '<button class="chip' + (st.value && st.value.v === o ? " on" : "") + '" data-act="val" data-v="' + o + '">' + o + "</button>").join("") + "</div>";
  } else if (conf.kind === "measure") {
    valueHtml = '<div class="steplabel">' + conf.label + "（" + conf.unit + '）</div>' +
      '<input type="number" id="sh-num" step="' + conf.step + '" min="0" placeholder="例如 ' + (st.category === "sleep" ? "6.5" : "30") + '" value="' + (st.value ? st.value.v : "") + '">';
  }
  const tagPool = (TAG_POOLS[st.category] || CTX_TAGS).slice();
  st.tags.forEach(t => { if (!tagPool.includes(t)) tagPool.push(t); });
  const tagChips = tagPool.map(t =>
    '<button class="chip' + (st.tags.includes(t) ? " on" : "") + '" data-act="tag" data-v="' + esc(t) + '">' + esc(t) + "</button>").join("");
  const when = new Date((st.recTs || Date.now() / 1000) * 1000);
  const local = st.date + "T" + (st.timeStr || pad(new Date().getHours()) + ":" + pad(new Date().getMinutes()));
  el.innerHTML =
    "<h3>" + (st.mode === "edit" ? "记录详情" : "快速记录") + "</h3>" +
    '<div class="steplabel">类别</div><div class="chips">' + catChips + "</div>" +
    '<div class="steplabel">项目</div><div class="chips">' + itemChips + "</div>" +
    '<div class="row2" style="margin-top:8px"><input type="text" id="sh-custom-item" placeholder="或自建项目名"><button class="btn ghost" data-act="custom-item">使用</button></div>' +
    valueHtml +
    '<div class="steplabel">标签（可多选）</div><div class="chips">' + tagChips + "</div>" +
    '<div class="row2" style="margin-top:8px"><input type="text" id="sh-custom-tag" placeholder="或自建标签"><button class="btn ghost" data-act="custom-tag">添加</button></div>' +
    '<div class="steplabel">备注（可选）</div><input type="text" id="sh-note" placeholder="一句话即可" value="' + esc(st.note || "") + '">' +
    '<div class="steplabel">时间</div><input type="datetime-local" id="sh-when" value="' + (st.whenLocal || local) + '">' +
    '<button class="btn block" data-act="save">' + (st.mode === "edit" ? "保存修改" : "保存记录") + "</button>" +
    (st.mode === "edit" ? '<button class="btn danger block" data-act="delete">删除这条</button>' : "") +
    '<button class="btn ghost block" data-act="close">取消</button>';
  if (st.mode === "edit" && st.rec) {
    $("#sh-note").value = st.rec.note || "";
    const d = new Date(st.rec.ts * 1000);
    $("#sh-when").value = st.rec.date + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  $$("#sheet [data-act]").forEach(b => b.addEventListener("click", () => sheetAction(b.dataset.act, b.dataset.v)));
}
function renderMorning(el) {
  const st = sheetState;
  const scale = (key, cur) => [1, 2, 3, 4, 5].map(n =>
    '<button class="chip' + (cur === n ? " on" : "") + '" data-m="' + key + '" data-v="' + n + '">' + n + "</button>").join("");
  el.innerHTML = "<h3>晨间打卡</h3>" +
    '<div class="steplabel">昨晚睡眠（小时）</div><input type="number" id="m-sleep" step="0.5" min="0" placeholder="6.5" value="' + st.mSleep + '">' +
    '<div class="steplabel">心情（1–5）</div><div class="chips">' + scale("mMood", st.mMood) + "</div>" +
    '<div class="steplabel">精力（1–5）</div><div class="chips">' + scale("mEnergy", st.mEnergy) + "</div>" +
    '<button class="btn block" data-act="morning-save">保存打卡</button>' +
    '<button class="btn ghost block" data-act="close">取消</button>';
  $$("#sheet [data-m]").forEach(b => b.addEventListener("click", () => {
    sheetState[b.dataset.m] = Number(b.dataset.v); renderSheet();
  }));
  $$("#sheet [data-act]").forEach(b => b.addEventListener("click", () => sheetAction(b.dataset.act, b.dataset.v)));
}
async function sheetAction(act, v) {
  const st = sheetState; if (!st) return;
  if (act === "close") { closeSheet(); return; }
  if (act === "cat") { st.category = v; if (!PRESETS[v].includes(st.item)) st.item = sortedItems(v)[0] || ""; st.value = null; st.tags = []; renderSheet(); return; }
  if (act === "item") { st.item = v; renderSheet(); return; }
  if (act === "custom-tag") {
    const t = $("#sh-custom-tag").value.trim();
    if (t && !st.tags.includes(t)) st.tags.push(t);
    renderSheet(); return;
  }
  if (act === "custom-item") {
    const t = $("#sh-custom-item").value.trim();
    if (t) { st.item = t; renderSheet(); }
    return;
  }
  if (act === "val") {
    const conf = VALUE_CONF[st.category];
    if (conf.kind === "scale") st.value = { type: "scale", v: Number(v) };
    else if (conf.kind === "bristol") st.value = { type: "enum", v: Number(v) };
    else if (conf.kind === "enum") st.value = st.value && st.value.v === v ? null : { type: "enum", v };
    renderSheet(); return;
  }
  if (act === "tag") {
    const i = st.tags.indexOf(v);
    if (i >= 0) st.tags.splice(i, 1); else st.tags.push(v);
    renderSheet(); return;
  }
  if (act === "delete") {
    if (!confirm("删除这条记录？（保留删除标记，导入旧备份不会复活）")) return;
    try { await api("/records/" + st.id, { method: "DELETE" }); } catch (e) { alert("删除失败：" + e.message); return; }
    closeSheet(); loadToday(); return;
  }
  if (act === "morning-save") {
    const sleep = parseFloat($("#m-sleep").value);
    const items = [];
    if (!isNaN(sleep) && sleep > 0) items.push({ category: "sleep", item: "睡眠", value: { type: "measure", v: sleep, unit: "小时" } });
    if (st.mMood) items.push({ category: "state", item: "心情", value: { type: "scale", v: st.mMood } });
    if (st.mEnergy) items.push({ category: "state", item: "精力", value: { type: "scale", v: st.mEnergy } });
    if (!items.length) { alert("至少填一项"); return; }
    const now = Math.floor(Date.now() / 1000);
    for (const it of items) {
      const payload = Object.assign({ id: uuid(), ts: now, date: todayStr(), tags: [], note: "" }, it);
      try { await apiPost("/records", payload); }
      catch (e) { const q = outbox(); q.push(payload); setOutbox(q); }
    }
    closeSheet(); loadToday(); return;
  }
  if (act === "save") {
    const conf = VALUE_CONF[st.category];
    let value = st.value;
    if (conf.kind === "measure") {
      const n = parseFloat(($("#sh-num") || {}).value);
      value = (!isNaN(n) && n > 0) ? { type: "measure", v: n, unit: conf.unit } : null;
    } else if (conf.kind === "bool") value = { type: "bool", v: true };
    const whenVal = $("#sh-when").value;
    if (!whenVal) { alert("请选择时间"); return; }
    const dt = new Date(whenVal);
    const payload = {
      category: st.category, item: st.item || (sortedItems(st.category)[0] || ""),
      value, tags: st.tags, note: $("#sh-note").value.trim(),
      ts: Math.floor(dt.getTime() / 1000), date: todayStr(dt)
    };
    if (!payload.item) { alert("请先选择或填写项目名（例如药名）"); return; }
    if (st.mode === "edit") {
      try { await apiPost("/records/" + st.id, payload); }
      catch (e) { alert("保存失败：" + e.message); return; }
    } else {
      payload.id = st.id;
      try { await apiPost("/records", payload); }
      catch (e) { const q = outbox(); q.push(payload); setOutbox(q); }
    }
    closeSheet();
    if (payload.date === viewDate || st.mode === "edit") loadToday();
    return;
  }
}
$("#sheet-wrap").addEventListener("click", e => { if (e.target.id === "sheet-wrap") closeSheet(); });
$("#btn-add").addEventListener("click", () => openSheet({ mode: "create", date: viewDate === todayStr() ? todayStr() : viewDate }));
$("#morning-go").addEventListener("click", () => openSheet({ morning: true }));
$("#d-prev").addEventListener("click", () => { const d = new Date(viewDate); d.setDate(d.getDate() - 1); viewDate = todayStr(d); loadToday(); });
$("#d-next").addEventListener("click", () => { const d = new Date(viewDate); d.setDate(d.getDate() + 1); viewDate = todayStr(d); loadToday(); });

/* ---------------- history ---------------- */
async function loadHistory() {
  $("#m-title").textContent = histMonth;
  const chips = ['<button class="chip' + (histFilter === "" ? " on" : "") + '" data-f="">全部</button>']
    .concat(CAT_ORDER.map(c => '<button class="chip' + (histFilter === c ? " on" : "") + '" data-f="' + c + '">' +
      esc(catLabel(c)) + "</button>"));
  $("#hist-filter").innerHTML = chips.join("");
  $$("#hist-filter .chip").forEach(ch => ch.addEventListener("click", () => { histFilter = ch.dataset.f; loadHistory(); }));
  let d;
  try { d = await api("/month?ym=" + histMonth); } catch (e) { return; }
  const days = d.days || {};
  const [y, m] = histMonth.split("-").map(Number);
  const first = new Date(y, m - 1, 1), nDays = new Date(y, m, 0).getDate();
  let html = ["日", "一", "二", "三", "四", "五", "六"].map(x => '<div class="dow">' + x + "</div>").join("");
  for (let i = 0; i < first.getDay(); i++) html += "<div></div>";
  for (let day = 1; day <= nDays; day++) {
    const ds = histMonth + "-" + pad(day);
    const cats = days[ds] || [];
    const dots = cats.map(c => {
      const stl = stealth() && CATS[c] && CATS[c].sensitive;
      return '<i style="background:' + (stl ? "#5a6376" : (CATS[c] || {}).color || "#888") + '"></i>';
    }).join("");
    html += '<div class="day' + (ds === histDate ? " sel" : "") + '" data-d="' + ds + '"><div class="n">' + day + '</div><div class="dots">' + dots + "</div></div>";
  }
  $("#calendar").innerHTML = html;
  $$("#calendar .day").forEach(el => el.addEventListener("click", () => { histDate = el.dataset.d; loadHistory(); }));
  try {
    const dd = await api("/day?date=" + histDate);
    let recs = (dd.records || []).slice().sort((a, b) => b.ts - a.ts);
    if (histFilter) recs = recs.filter(r => r.category === histFilter);
    $("#hist-day").innerHTML = '<div class="grouplabel">' + histDate + "</div>" +
      (recs.map(recRow).join("") || '<p class="mut">当天没有记录。</p>');
    bindRows($("#hist-day"), dd.records || []);
  } catch (e) { $("#hist-day").innerHTML = ""; }
}
$("#m-prev").addEventListener("click", () => { const d = new Date(histMonth + "-01"); d.setMonth(d.getMonth() - 1); histMonth = todayStr(d).slice(0, 7); loadHistory(); });
$("#m-next").addEventListener("click", () => { const d = new Date(histMonth + "-01"); d.setMonth(d.getMonth() + 1); histMonth = todayStr(d).slice(0, 7); loadHistory(); });

/* ---------------- insights ---------------- */
async function loadInsights() {
  let d;
  try { d = await api("/insights"); } catch (e) { $("#insights-body").innerHTML = '<p class="mut">读取失败</p>'; return; }
  if (d.days_needed > 0) {
    $("#insights-body").innerHTML = '<div class="card"><h3>规律还在积累</h3><p>已记录 ' + d.days_recorded +
      " 天，还需要 " + d.days_needed + " 天（满 14 天）才开始算趋势与相关性。先坚持记，数据够了这里自然长出来。</p></div>";
    return;
  }
  const bars = (rows, max, unit) => rows.map(r =>
    '<div class="trendrow"><span style="flex:0 0 74px">' + r.date.slice(5) + '</span><span class="bar"><i style="width:' +
    (r.v == null ? 0 : Math.min(100, r.v / max * 100)) + '%"></i></span><span style="flex:0 0 52px;text-align:right">' +
    (r.v == null ? "—" : r.v + unit) + "</span></div>").join("");
  let html = '<div class="card"><h3>近 14 天趋势</h3><p>睡眠时长</p>' + bars(d.trends.sleep, 10, "h") +
    "<p>精力评分</p>" + bars(d.trends.energy, 5, "分") + "</div>";
  if (d.bristol && d.bristol.total) {
    html += '<div class="card"><h3>' + esc(codeOf("stool")) + ' 分布</h3><p>共 ' + d.bristol.total + " 次，约每周 " + d.bristol.per_week +
      " 次；3–4 型（正常区间）占 " + Math.round((d.bristol.share_mid || 0) * 100) + "%。</p></div>";
  }
  (d.correlations || []).forEach(c => {
    html += '<div class="card warn"><h3>相关性提示</h3><p>「' + esc(c.tag) + "」之后的次日，" + esc(c.outcome) +
      " 的比例为 " + Math.round(c.rate * 100) + "%，高于平时的 " + Math.round(c.base * 100) +
      "%（样本 " + c.n + " 天）。这是相关性，不是因果结论。</p></div>";
  });
  if (!(d.correlations || []).length) html += '<div class="card"><h3>相关性</h3><p>暂时没有达到阈值的相关模式。</p></div>';
  $("#insights-body").innerHTML = html;
}

/* ---------------- me ---------------- */
function renderMe() {
  $("#me-stealth").checked = stealth();
  $("#me-codes").innerHTML = CAT_ORDER.map(c =>
    '<div class="kv"><label>' + esc(CATS[c].name) + '</label><input type="text" id="code-' + c + '" value="' +
    esc(SETTINGS.codes[c] || CATS[c].code) + '" maxlength="12"></div>').join("");
  $("#me-predict").innerHTML = CAT_ORDER.map(c =>
    '<div class="kv"><label style="flex:1">' + esc(catLabel(c)) + '</label><span class="switch"><input type="checkbox" id="pred-' + c + '"' +
    (SETTINGS.predict[c] ? " checked" : "") + "><i></i></span></div>").join("");
}
$("#me-stealth").addEventListener("change", e => {
  localStorage.setItem("health_stealth", e.target.checked ? "1" : "0");
  loadToday();
});
$("#me-codes-save").addEventListener("click", async () => {
  const codes = {}; Object.keys(CATS).forEach(c => codes[c] = $("#code-" + c).value.trim());
  try { const d = await apiPost("/settings", { codes }); SETTINGS = d; alert("代号已保存"); loadToday(); }
  catch (e) { alert("保存失败：" + e.message); }
});
$("#me-predict-save").addEventListener("click", async () => {
  const predict = {}; Object.keys(CATS).forEach(c => predict[c] = $("#pred-" + c).checked);
  try { const d = await apiPost("/settings", { predict }); SETTINGS = d; alert("已保存"); }
  catch (e) { alert("保存失败：" + e.message); }
});
$("#me-export").addEventListener("click", () => {
  const f = $("#exp-format").value, n = $("#exp-names").value;
  const a = document.createElement("a");
  a.href = API + "/export?format=" + f + "&names=" + n;
  a.download = "";
  document.body.appendChild(a); a.click(); a.remove();
});
let importPayload = null;
$("#imp-file").addEventListener("change", async e => {
  const file = e.target.files[0]; if (!file) return;
  try {
    importPayload = JSON.parse(await file.text());
    const d = await apiPost("/import/preview", { payload: importPayload });
    const c = d.counts;
    let html = "<p>预览：新增 " + c.added + " 条 · 更新 " + c.updated + " 条 · 跳过重复 " + c.skipped + " 条 · 冲突 " + c.conflicts + " 条。</p>";
    if (c.conflicts) html += '<div class="row2"><select id="imp-tie"><option value="local">冲突时保留本地</option><option value="import">冲突时保留导入文件</option></select></div>';
    html += '<button class="btn block" id="imp-apply">确认导入</button>';
    $("#imp-preview").innerHTML = html;
    $("#imp-apply").addEventListener("click", async () => {
      const tie = $("#imp-tie") ? $("#imp-tie").value : "local";
      const dd = await apiPost("/import/apply", { payload: importPayload, tie });
      $("#imp-preview").innerHTML = "<p>导入完成：新增 " + dd.counts.added + " · 更新 " + dd.counts.updated + " · 跳过 " + dd.counts.skipped + "。</p>";
      importPayload = null; loadToday();
    });
  } catch (err) { $("#imp-preview").innerHTML = '<p class="mut">导入失败：' + esc(err.message) + "</p>"; }
});

/* ---------------- nav + init ---------------- */
$$("nav.bottom button[data-view]").forEach(b => b.addEventListener("click", () => {
  $$("nav.bottom button[data-view]").forEach(x => x.classList.toggle("on", x === b));
  ["today", "history", "insights", "me"].forEach(v =>
    $("#view-" + v).classList.toggle("hidden", v !== b.dataset.view));
  if (b.dataset.view === "history") loadHistory();
  if (b.dataset.view === "insights") loadInsights();
  if (b.dataset.view === "me") renderMe();
}));
async function init() {
  try {
    const p = await api("/presets");
    CATS = p.categories || {}; PRESETS = p.presets || {};
    FOOD_ATTRS = p.food_attrs || []; CTX_TAGS = p.context_tags || [];
    TAG_POOLS = p.tag_pools || {}; CAT_COUNTS = p.cat_counts || {}; ITEM_COUNTS = p.item_counts || {};
    computeCatOrder();
    const s = await api("/settings");
    SETTINGS = { codes: s.codes || {}, predict: s.predict || {} };
  } catch (e) { /* shell still renders; loads below will retry */ }
  syncLabel();
  loadToday();
  flushOutbox();
  if ("serviceWorker" in navigator) {
    try { navigator.serviceWorker.register("health-sw.js"); } catch (e) {}
  }
}
window.addEventListener("online", flushOutbox);
init();
</script>
</body>
</html>
"""
