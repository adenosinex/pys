"""Export page for u3磁力获取 (/export).

Task sentence (UI canon P1): open this page to export a batch of
deduplicated magnet links for a keyword -- search, then copy 100 at
a time, marking each batch processed so later searches skip it. The
first screen must let the user search and see batch states.

Pipeline (user spec 2026-11-13): keyword search (latest-N cap
and/or Beijing-date range) -> keyword blacklist filter (server
side, stored in export_blacklist.json, maintained on this page) ->
title dedup with the method of the /site/p1791363651 tool (the
dedup functions below are that page's code with the same defaults,
run in the browser over titles + size_bytes) -> size-ascending
batches of 100 (smallest first, each batch header shows its total
size); the summary shows the working set's earliest time. Confirming
a batch, or copying the next batch, records its
infohashes as processed; later export searches exclude them in SQL.
Search results are held on the page only, never stored.

UI canon mapping: one task per page (P1); batch header is the
judgment layer, titles live one disclosure down (P3); batch rows
carry batch number + count + status + two actions (P4); batches
are the convergence means, no pagination (P6); status uses badges,
color only for state (P7); four empty states with filter echo
(P8); 44px targets, full-width main button on mobile (P10); fetch
+ dedup report real elapsed time with a visible progress state
(P12); follows the OS light/dark setting (new-page hard rule).

The HTML is a RAW string: the dedup JavaScript carries regex
escapes (\\u, \\d, \\p) that Python must not interpret.
"""

EXPORT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>导出磁力 · u3 磁力获取</title>
<style>
:root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--line:#e2e7f0;--ink:#1d2433;--mut:#68707f;--acc:#2f6fd0;--bad:#b3372f;--ok:#2c7a3f}
@media (prefers-color-scheme:dark){:root{--bg:#14181f;--card:#1d232e;--line:#333d4f;--ink:#e6eaf2;--mut:#98a2b5;--acc:#6ea8ec;--bad:#e08a84;--ok:#7fc98f}}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:68rem;margin:0 auto;padding:18px 14px 60px}
h1{font-size:20px;margin:0}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:6px 0 14px}
.navlinks{display:flex;gap:14px;align-items:center}
.navlink{display:inline-flex;align-items:center;min-height:44px;color:var(--acc);font-size:14px;text-decoration:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.searchrow{display:flex;gap:8px;flex-wrap:wrap}
.txt{flex:1;min-width:200px;min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font-size:15px;background:var(--card);color:var(--ink)}
.num{width:110px;min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font-size:14px;background:var(--card);color:var(--ink)}
.date{width:150px;min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font-size:14px;background:var(--card);color:var(--ink)}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:10px 16px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:14px;cursor:pointer;text-decoration:none;white-space:nowrap}
.btn-primary{background:var(--acc);border-color:var(--acc);color:#fff}
.btn:disabled{opacity:.55;cursor:default}
.hint{color:var(--mut);font-size:12px;margin-top:10px;line-height:1.6}
.msg{font-size:13px;color:var(--ok);min-height:18px}
.err{color:var(--bad);font-size:13px;line-height:1.6}
.tag{font-size:11px;border:1px solid var(--line);border-radius:20px;padding:1px 8px;color:var(--mut);white-space:nowrap}
.badge{font-size:12px;font-weight:600;color:var(--mut);background:var(--line);border-radius:20px;padding:2px 10px;white-space:nowrap}
.badge-done{color:var(--ok)}
.prog{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:10px}
.progbar{height:100%;width:40%;background:var(--acc);border-radius:3px;animation:progslide 1s linear infinite}
@keyframes progslide{from{transform:translateX(-100%)}to{transform:translateX(280%)}}
.prog-time{color:var(--mut);font-size:12px;margin-top:6px;font-variant-numeric:tabular-nums}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-top:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.stat-k{font-size:12px;color:var(--mut);margin-bottom:4px}
.stat-v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.empty{color:var(--mut);font-size:14px;padding:18px 4px;line-height:1.7}
.empty-err{color:var(--bad)}
.batch{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-top:12px}
.batch-head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.batch-title{font-weight:600;font-size:15px}
.batch-actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.batch-items{margin-top:6px}
.bitem{padding:8px 2px;border-bottom:1px solid var(--line);overflow-wrap:anywhere;line-height:1.5;font-size:14px}
.bitem-meta{color:var(--mut);font-size:12px;margin-top:2px}
.grp{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-top:10px}
.grp-head{display:flex;gap:8px;align-items:baseline}
.grp-rep{font-weight:600;overflow-wrap:anywhere}
.grp-cnt{margin-left:auto;flex-shrink:0;font-size:12px;color:var(--mut);white-space:nowrap}
.grp-member{font-size:12px;color:var(--mut);padding:2px 0 2px 10px;overflow-wrap:anywhere;border-left:2px solid var(--line);margin-top:4px}
.bl-text{width:100%;min-height:120px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font-size:14px;background:var(--card);color:var(--ink);font-family:inherit;resize:vertical;margin-top:10px}
.x-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;gap:8px;font-weight:600;min-height:44px}
summary::-webkit-details-marker{display:none}
@media (max-width:640px){
.searchrow{flex-direction:column}
.txt{width:100%}
.num{width:100%}
.date{width:100%}
.btn-search{width:100%}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar"><h1>导出磁力</h1><span class="navlinks"><a class="navlink" href="./">搜索</a><a class="navlink" href="stats">统计</a></span></div>
  <div class="card">
    <div class="searchrow">
      <input id="f-q" class="txt" type="text" placeholder="输入关键词，如 fc2 4897458" autocomplete="off" spellcheck="false">
      <input id="f-limit" class="num" type="number" min="1" max="5000" value="500" title="最新数量上限">
      <input id="f-from" class="date" type="date" title="开始日期">
      <input id="f-to" class="date" type="date" title="结束日期">
      <button id="f-go" class="btn btn-primary btn-search" type="button">搜索并处理</button>
    </div>
    <div class="hint">最新数量默认 500（上限 5000），日期范围按北京时间。流程：搜索 → 黑名单剔除 → 标题去重 → 按大小升序、每批 100 条复制（最小的在第一批）。确认一批、或复制下一批时，该批自动记为已处理，后续搜索不再出现。</div>
    <div class="hint" id="proc-line"></div>
    <div id="prog" class="prog" style="display:none"><div class="progbar"></div></div>
    <div id="prog-time" class="prog-time"></div>
    <div id="summary" class="stats" style="display:none">
      <div class="stat"><div class="stat-k">命中</div><div class="stat-v" id="s-total">0</div></div>
      <div class="stat"><div class="stat-k">取回（最新）</div><div class="stat-v" id="s-fetched">0</div></div>
      <div class="stat"><div class="stat-k">黑名单剔除</div><div class="stat-v" id="s-black">0</div></div>
      <div class="stat"><div class="stat-k">去重后</div><div class="stat-v" id="s-kept">0</div></div>
    </div>
    <div class="hint" id="sum-line"></div>
    <div id="empty" class="empty">输入关键词，点「搜索并处理」开始。结果先过关键词黑名单、再按标题去重，然后按每批 100 条复制处理。</div>
    <div id="batches"></div>
    <div class="msg" id="batch-msg"></div>
  </div>
  <details class="card" id="dup-box" style="display:none">
    <summary><span>重复组明细</span><span class="tag" id="dup-state"></span></summary>
    <div id="grouplist"></div>
  </details>
  <details class="card" id="bl-box">
    <summary><span>关键词黑名单</span><span class="tag" id="bl-state">加载中…</span></summary>
    <div class="hint">标题包含任一关键词即剔除（不区分大小写），先于去重生效。每行一个关键词，保存后下次搜索生效。</div>
    <textarea id="bl-text" class="bl-text" placeholder="每行一个关键词"></textarea>
    <div class="x-row">
      <button id="bl-save" class="btn btn-primary" type="button">保存黑名单</button>
    </div>
    <div class="msg" id="bl-msg"></div>
  </details>
</div>
<script>
/* ============================================================
 * 标题去重：与 /site/p1791363651（视频标题去重工具 · 大小接近版）
 * 相同的方法与默认值。函数原文照搬，只改输入来源：条目直接
 * 取自搜索接口（标题 + size_bytes），不再解析文本行。
 * ============================================================ */

function toHalfWidth(s) {
  return String(s)
    .replace(/[\uFF01-\uFF5E]/g, c => String.fromCharCode(c.charCodeAt(0) - 0xFEE0))
    .replace(/\u3000/g, ' ');
}

function makeBigrams(str) {
  const set = new Set();
  if (!str) return set;
  if (str.length === 1) { set.add(str); return set; }
  for (let i = 0; i < str.length - 1; i++) set.add(str.slice(i, i + 2));
  return set;
}

function jaccard(A, B) {
  if (A.size === 0 || B.size === 0) return 0;
  const small = A.size <= B.size ? A : B;
  const big   = A.size <= B.size ? B : A;
  let inter = 0;
  for (const x of small) if (big.has(x)) inter++;
  const union = A.size + B.size - inter;
  return union === 0 ? 0 : inter / union;
}

const DATE_PREFIX_PATTERNS = [
  /^[\s\p{P}\p{S}]*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*(最新|更新|发布|整理|版)?\s*/u,
  /^[\s\p{P}\p{S}]*\d{4}\s*年\s*\d{1,2}\s*月\s*(最新|更新|发布|整理|版)?\s*/u,
  /^[\s\p{P}\p{S}]*\d{4}\s*年\s*(最新|更新|发布|整理|版)?\s*/u,
  /^[\s\p{P}\p{S}]*\d{4}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}\s*(最新|更新)?\s*/u,
  /^[\s\p{P}\p{S}]*\d{4}\s*[-./]\s*\d{1,2}\s*(最新|更新)?\s*/u,
  /^[\s\p{P}\p{S}]*\d{1,2}\s*月\s*\d{1,2}\s*日\s*(最新|更新)?\s*/u,
  /^[\s\p{P}\p{S}]*\d{1,2}\s*月\s*(最新|更新|发布|整理)\s*/u,
  /^[\s\p{P}\p{S}]*(最新|最近|今日|近期|今日更新|今日最新|刚刚)\s*/u,
];

function stripDatePrefixes(t) {
  let cur = t;
  for (let round = 0; round < 4; round++) {
    const before = cur;
    for (const p of DATE_PREFIX_PATTERNS) cur = cur.replace(p, '');
    if (cur === before) break;
  }
  return cur;
}

function normalizeTitle(raw, opt) {
  let t = toHalfWidth(raw).toLowerCase();
  if (opt.stripDate) t = stripDatePrefixes(t);

  if (opt.leadMode === 'aggressive') {
    t = t.replace(/^[\s\p{P}\p{S}\d]+/u, '');
  } else if (opt.leadMode === 'conservative') {
    t = t.replace(/^[\s\p{P}\p{S}]*(?:\d+[\s\p{P}\p{S}]+)+/u, '');
  }

  t = t.replace(/\s+/g, '');
  return t;
}

function numberKey(norm) {
  const m = norm.match(/\d+/g);
  if (!m) return '';
  return m.map(x => String(parseInt(x, 10))).join(',');
}

function sizeRelation(a, b, opt) {
  if (!a.hasSize || !b.hasSize) return 'unknown';
  const maxV = Math.max(a.size, b.size, 1e-9);
  const diff = Math.abs(a.size - b.size) / maxV;
  if (diff <= opt.sizeClosePct / 100) return 'close';
  if (diff >= opt.sizeHardPct  / 100) return 'hard';
  return 'mid';
}

function isDuplicate(a, b, opt) {
  if (opt.useNumKey && a.nk !== b.nk) return false;

  const la = a.norm.length, lb = b.norm.length;
  if (la === 0 || lb === 0) return la === lb;

  const short = la <= lb ? a.norm : b.norm;
  const long  = la <= lb ? b.norm : a.norm;

  const rel = sizeRelation(a, b, opt);

  if (opt.sizeGate && rel === 'hard' && short !== long) return false;

  let threshold  = opt.threshold;
  let containMin = opt.minContainLen;

  if (rel === 'close') {
    threshold  *= opt.boostClose;
    containMin  = Math.max(2, containMin - 1);
  }

  if (opt.useContain && short.length >= containMin) {
    if (long.indexOf(short) !== -1) return true;
  }

  if (short.length / long.length < opt.minLenRatio) return false;

  return jaccard(a.grams, b.grams) >= threshold;
}

class UnionFind {
  constructor(n) {
    this.parent = new Int32Array(n);
    for (let i = 0; i < n; i++) this.parent[i] = i;
  }
  find(x) {
    let root = x;
    while (this.parent[root] !== root) root = this.parent[root];
    while (this.parent[x] !== root) {
      const next = this.parent[x];
      this.parent[x] = root;
      x = next;
    }
    return root;
  }
  union(a, b) {
    const ra = this.find(a), rb = this.find(b);
    if (ra !== rb) this.parent[rb] = ra;
  }
}

function buildCandidatePairs(entries) {
  const n = entries.length;
  const pairs = [];

  if (n <= 1000) {
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) pairs.push(i, j);
    }
    return pairs;
  }

  const index = new Map();
  for (let i = 0; i < n; i++) {
    for (const g of entries[i].grams) {
      let arr = index.get(g);
      if (!arr) index.set(g, arr = []);
      arr.push(i);
    }
  }

  const commonLimit = Math.max(50, n * 0.5);

  for (let i = 0; i < n; i++) {
    const cnt = new Map();
    for (const g of entries[i].grams) {
      const arr = index.get(g);
      if (!arr || arr.length > commonLimit) continue;
      for (let k = 0; k < arr.length; k++) {
        const j = arr[k];
        if (j <= i) continue;
        cnt.set(j, (cnt.get(j) || 0) + 1);
      }
    }
    for (const [j, c] of cnt) {
      if (c >= 2) pairs.push(i, j);
    }
  }

  const shortIds = [];
  for (let i = 0; i < n; i++) {
    if (entries[i].norm.length <= 4) shortIds.push(i);
  }
  if (shortIds.length <= 800) {
    for (let a = 0; a < shortIds.length; a++) {
      for (let b = a + 1; b < shortIds.length; b++) {
        pairs.push(shortIds[a], shortIds[b]);
      }
    }
  }

  return pairs;
}

const DEDUP_OPT = {
  threshold: 0.75,
  minLenRatio: 0.5,
  minContainLen: 3,
  leadMode: "conservative",
  repMode: "first",
  stripDate: true,
  useContain: true,
  useNumKey: true,
  sizeClosePct: 10,
  sizeHardPct: 50,
  boostClose: 0.8,
  sizeGate: true
};

function dedupeItems(items) {
  const opt = DEDUP_OPT;
  const entries = [];
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    const norm = normalizeTitle(it.title, opt);
    entries.push({
      idx: i,
      title: it.title,
      norm: norm,
      nk: opt.useNumKey ? numberKey(norm) : "",
      size: it.size_bytes || 0,
      hasSize: (it.size_bytes || 0) > 0,
      grams: null,
      item: it
    });
  }
  for (let i = 0; i < entries.length; i++) {
    entries[i].grams = makeBigrams(entries[i].norm);
  }
  const uf = new UnionFind(entries.length);
  const pairs = buildCandidatePairs(entries);
  for (let p = 0; p < pairs.length; p += 2) {
    const i = pairs[p], j = pairs[p + 1];
    if (isDuplicate(entries[i], entries[j], opt)) uf.union(i, j);
  }
  const groupMap = new Map();
  for (let i = 0; i < entries.length; i++) {
    const root = uf.find(i);
    let g = groupMap.get(root);
    if (!g) groupMap.set(root, g = []);
    g.push(entries[i]);
  }
  const groups = Array.from(groupMap.values());
  groups.sort((a, b) => a[0].idx - b[0].idx);
  const kept = [];
  const dupGroups = [];
  for (const g of groups) {
    kept.push(g[0].item);
    if (g.length > 1) dupGroups.push(g);
  }
  return {kept: kept, dupGroups: dupGroups};
}

/* ============================================================
 * 页面逻辑
 * ============================================================ */

const $ = (id) => document.getElementById(id);
const BATCH = 100;
let seq = 0;
let batches = [];
let batchDone = {};
let processedTotal = 0;
let timerId = null;
let timerT0 = 0;
let progTimer = null;

async function api(path, method, payload, isForm) {
  const opts = {method: method || "GET", headers: {}};
  if (payload && isForm) { opts.body = payload; }
  else if (payload) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(payload); }
  const r = await fetch(path, opts);
  return r.json();
}
function busyBtn(b) {
  if (!b || b.disabled) return () => {};
  const t = b.textContent; b.disabled = true; b.textContent = "进行中…";
  return () => { b.disabled = false; b.textContent = t; };
}
function fmtTime(v) {
  if (!v) return "";
  return String(v).replace("T", " ").slice(0, 16);
}
function fmtBytes(n) {
  if (!n || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let u = 0;
  let v = n;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  return (u === 0 ? String(Math.round(v)) : v.toFixed(1)) + " " + units[u];
}
function progStart() {
  timerT0 = performance.now();
  $("prog").style.display = "none";
  $("prog-time").textContent = "";
  clearTimeout(progTimer);
  progTimer = setTimeout(() => { $("prog").style.display = "block"; }, 300);
  clearInterval(timerId);
  timerId = setInterval(() => {
    $("prog-time").textContent = "已用 " + ((performance.now() - timerT0) / 1000).toFixed(1) + " 秒";
  }, 100);
}
function progStop() {
  clearTimeout(progTimer);
  clearInterval(timerId);
  $("prog").style.display = "none";
  $("prog-time").textContent = "";
  return (performance.now() - timerT0) / 1000;
}
function renderProcessedLine() {
  $("proc-line").textContent = "已处理磁力：" + processedTotal.toLocaleString() + " 条（后续搜索自动剔除）。";
}
function showEmpty(node, isErr) {
  const el = $("empty");
  el.style.display = "block";
  el.className = isErr ? "empty empty-err" : "empty";
  el.innerHTML = "";
  el.appendChild(node);
}
function showEmptyText(text, isErr) {
  const s = document.createElement("span");
  s.textContent = text;
  showEmpty(s, isErr);
}
function clearFilters() {
  $("f-q").value = "";
  $("f-from").value = "";
  $("f-to").value = "";
  runSearch();
}

async function runSearch() {
  const my = ++seq;
  const done = busyBtn($("f-go"));
  const q = $("f-q").value.trim();
  const limit = $("f-limit").value;
  const from = $("f-from").value;
  const to = $("f-to").value;
  $("batch-msg").textContent = "";
  progStart();
  let d;
  try {
    d = await api("api/export/search?q=" + encodeURIComponent(q) +
      "&limit=" + encodeURIComponent(limit) +
      "&from=" + encodeURIComponent(from) +
      "&to=" + encodeURIComponent(to));
  } catch (e) {
    progStop();
    done();
    if (my === seq) showEmptyText("搜索失败（网络或服务异常），请点「搜索并处理」重试。", true);
    return;
  }
  if (my !== seq) { progStop(); done(); return; }
  if (!d.ok) {
    progStop();
    done();
    showEmptyText(d.message || "搜索失败，请重试。", true);
    return;
  }
  await new Promise((r) => setTimeout(r, 0));
  const res = dedupeItems(d.items || []);
  res.kept.sort((a, b) => (a.size_bytes || 0) - (b.size_bytes || 0));
  res.dupGroups.sort((a, b) => (a[0].item.size_bytes || 0) - (b[0].item.size_bytes || 0));
  const secs = progStop();
  done();
  if (my !== seq) return;
  processedTotal = d.processed_total;
  renderProcessedLine();
  renderSummary(d, res, secs, q, from, to);
  renderBatches(res.kept);
  renderGroups(res.dupGroups);
}

function renderSummary(d, res, secs, q, from, to) {
  $("summary").style.display = "grid";
  $("s-total").textContent = d.total.toLocaleString();
  $("s-fetched").textContent = d.fetched.toLocaleString();
  $("s-black").textContent = d.blacklist_removed.toLocaleString();
  $("s-kept").textContent = res.kept.length.toLocaleString();
  let line = "用时 " + secs.toFixed(1) + " 秒（服务端查询 " + d.compute_ms + " 毫秒）";
  if (d.total > d.fetched) line += " · 命中多于取回，仅取最新 " + d.fetched.toLocaleString() + " 条";
  if (d.blacklist_count) line += " · 黑名单 " + d.blacklist_count + " 个关键词";
  let earliest = "";
  for (const it of res.kept) {
    if (it.published_at && (!earliest || it.published_at < earliest)) earliest = it.published_at;
  }
  if (earliest) line += " · 数据起始 " + fmtTime(earliest);
  $("sum-line").textContent = line;
  if (!d.total) {
    const wrap = document.createElement("div");
    const s = document.createElement("span");
    s.textContent = "没有命中" +
      (q ? "「" + q + "」" : "") +
      (from || to ? "（" + (from || "…") + " 至 " + (to || "…") + "）" : "") +
      " 的条目（已处理的不再出现）。换个关键词、放宽日期范围，或检查黑名单。";
    const clr = document.createElement("button");
    clr.type = "button";
    clr.className = "btn";
    clr.style.marginLeft = "10px";
    clr.textContent = "清除筛选";
    clr.onclick = clearFilters;
    wrap.appendChild(s);
    wrap.appendChild(clr);
    showEmpty(wrap, false);
  } else if (!res.kept.length) {
    showEmptyText("取回的 " + d.fetched + " 条全部被黑名单剔除。减少黑名单关键词，或换个关键词再搜。", false);
  } else {
    $("empty").style.display = "none";
  }
}

function updateBatchUI(k) {
  const st = $("b-st-" + k);
  const ok = $("b-ok-" + k);
  if (!st || !ok) return;
  if (batchDone[k]) {
    st.textContent = "已处理";
    st.className = "badge badge-done";
    ok.disabled = true;
  } else {
    st.textContent = "未处理";
    st.className = "badge";
    ok.disabled = false;
  }
}

async function markBatch(k) {
  if (batchDone[k]) return;
  batchDone[k] = true;
  updateBatchUI(k);
  const hashes = batches[k].map((it) => it.infohash);
  try {
    const d = await api("api/export/processed/add", "POST", {infohashes: hashes});
    if (d.ok) {
      processedTotal = d.total;
      renderProcessedLine();
    } else {
      batchDone[k] = false;
      updateBatchUI(k);
      $("batch-msg").textContent = "标记失败：" + (d.message || "请重试。");
    }
  } catch (e) {
    batchDone[k] = false;
    updateBatchUI(k);
    $("batch-msg").textContent = "标记失败（网络异常），请重试。";
  }
}

async function copyBatch(k) {
  const text = batches[k].map((it) => it.magnet).join("\n");
  const btn = $("b-cp-" + k);
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "已复制";
  } catch (e) {
    btn.textContent = "复制失败";
  }
  setTimeout(() => { btn.textContent = "复制本批"; }, 1500);
  for (let j = 0; j < k; j++) {
    if (!batchDone[j]) markBatch(j);
  }
}

function renderBatches(kept) {
  batches = [];
  batchDone = {};
  for (let i = 0; i < kept.length; i += BATCH) {
    batches.push(kept.slice(i, i + BATCH));
  }
  const box = $("batches");
  box.innerHTML = "";
  batches.forEach((items, k) => {
    const wrap = document.createElement("div");
    wrap.className = "batch";
    const head = document.createElement("div");
    head.className = "batch-head";
    const title = document.createElement("span");
    title.className = "batch-title";
    let totalBytes = 0;
    for (const it of items) totalBytes += (it.size_bytes || 0);
    title.textContent = "第 " + (k + 1) + " 批 · " + items.length + " 条 · 共 " + fmtBytes(totalBytes);
    const st = document.createElement("span");
    st.className = "badge";
    st.id = "b-st-" + k;
    st.textContent = "未处理";
    head.appendChild(title);
    head.appendChild(st);
    const acts = document.createElement("div");
    acts.className = "batch-actions";
    const cp = document.createElement("button");
    cp.type = "button";
    cp.className = "btn btn-primary";
    cp.id = "b-cp-" + k;
    cp.textContent = "复制本批";
    cp.onclick = () => copyBatch(k);
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "btn";
    ok.id = "b-ok-" + k;
    ok.textContent = "确认已处理";
    ok.onclick = () => markBatch(k);
    acts.appendChild(cp);
    acts.appendChild(ok);
    const det = document.createElement("details");
    det.className = "batch-detail";
    const sum = document.createElement("summary");
    const sumL = document.createElement("span");
    sumL.textContent = "查看本批标题";
    const sumR = document.createElement("span");
    sumR.className = "tag";
    sumR.textContent = items.length + " 条";
    sum.appendChild(sumL);
    sum.appendChild(sumR);
    det.appendChild(sum);
    const list = document.createElement("div");
    list.className = "batch-items";
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "bitem";
      row.textContent = it.title;
      const meta = document.createElement("div");
      meta.className = "bitem-meta";
      meta.textContent = fmtTime(it.published_at) + (it.size_text ? " · " + it.size_text : "");
      row.appendChild(meta);
      list.appendChild(row);
    }
    det.appendChild(list);
    wrap.appendChild(head);
    wrap.appendChild(acts);
    wrap.appendChild(det);
    box.appendChild(wrap);
  });
}

function renderGroups(dupGroups) {
  const box = $("dup-box");
  if (!dupGroups) { box.style.display = "none"; return; }
  box.style.display = "block";
  $("dup-state").textContent = "共 " + dupGroups.length + " 组";
  const list = $("grouplist");
  list.innerHTML = "";
  if (!dupGroups.length) {
    const tip = document.createElement("div");
    tip.className = "empty";
    tip.textContent = "没有发现重复标题。";
    list.appendChild(tip);
    return;
  }
  const shown = dupGroups.slice(0, 50);
  shown.forEach((g, gi) => {
    const card = document.createElement("div");
    card.className = "grp";
    const head = document.createElement("div");
    head.className = "grp-head";
    const rep = document.createElement("span");
    rep.className = "grp-rep";
    rep.textContent = g[0].title;
    const cnt = document.createElement("span");
    cnt.className = "grp-cnt";
    cnt.textContent = g.length + " 条";
    head.appendChild(rep);
    head.appendChild(cnt);
    card.appendChild(head);
    g.slice(0, 5).forEach((m) => {
      const row = document.createElement("div");
      row.className = "grp-member";
      row.textContent = (m === g[0] ? "★ " : "· ") + m.title +
        (m.item.size_text ? "（" + m.item.size_text + "）" : "");
      card.appendChild(row);
    });
    if (g.length > 5) {
      const more = document.createElement("div");
      more.className = "grp-member";
      more.textContent = "… 还有 " + (g.length - 5) + " 条";
      card.appendChild(more);
    }
    list.appendChild(card);
  });
  if (dupGroups.length > 50) {
    const tip = document.createElement("div");
    tip.className = "empty";
    tip.textContent = "仅显示前 50 组，共 " + dupGroups.length + " 组。";
    list.appendChild(tip);
  }
}

async function loadBlacklist() {
  try {
    const d = await api("api/export/blacklist");
    if (!d.ok) return;
    $("bl-text").value = (d.keywords || []).join("\n");
    $("bl-state").textContent = "共 " + (d.keywords || []).length + " 个关键词";
    processedTotal = d.processed_total;
    renderProcessedLine();
  } catch (e) {
    $("bl-state").textContent = "加载失败";
  }
}

$("f-go").onclick = () => runSearch();
$("f-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runSearch(); }
});
$("bl-save").onclick = async (e) => {
  const done = busyBtn(e.currentTarget);
  const kws = $("bl-text").value.split(/\r?\n/);
  let d;
  try {
    d = await api("api/export/blacklist", "POST", {keywords: kws});
  } catch (err) {
    d = {ok: false, message: "保存失败（网络异常）。"};
  }
  done();
  $("bl-msg").textContent = d.message || (d.ok ? "已保存。" : "保存失败。");
  if (d.ok) {
    $("bl-text").value = (d.keywords || []).join("\n");
    $("bl-state").textContent = "共 " + (d.keywords || []).length + " 个关键词";
  }
};
loadBlacklist();
</script>
</body>
</html>
"""
