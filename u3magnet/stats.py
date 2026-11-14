"""Stats page for u3磁力获取 (u3magnet).

UI canon mapping (《平台 UI 设计逻辑》, 2026-11-11):
- Task first (P1): the page answers "how does the library grow over
  time" — summary totals first, then the calendar / period lists.
- One API call returns the full daily series; week/month views are
  rollups of the same series, so all three views always agree.
- Calendar cells carry day + volume only (P4); the detail strip
  shows the exact count / volume / cumulative for the tapped day.
- Filters (keyword + category) are the convergence means (P6);
  week/month lists page at 12 (P6: 10-15).
- Empty states echo the filters and offer a clear-filters action
  (P8); loading + busy state on the stats button (self-check).
- Touch targets >= 44px (P10); follows the OS light/dark setting.
- Stats measure the whole corpus: the exclusion set hides search
  results only and does not change these numbers (stated on page).
- Volumes use 1024-based units, matching size_text (5.32GB in the
  corpus text equals 5712306503 bytes).
- Settled data first (P11, 2026-11-13): the unfiltered payload is
  served from a snapshot file in the data dir (30-minute TTL, the
  increment cycle); a stale snapshot is served instantly and refreshed
  in a background thread, the page re-fetches silently; every response
  reports computed_at + compute_ms and the page shows both.
- Visible waiting (P12, 2026-11-13): a live query slower than 300ms
  reveals an indeterminate progress bar plus a ticking elapsed timer
  (0.1s steps); the final time lands in the meta line. The silent
  refresh never shows the bar.
"""

STATS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>磁力统计</title>
<style>
:root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--line:#e2e7f0;--ink:#1d2433;--mut:#68707f;--acc:#2f6fd0;--bad:#b3372f;--ok:#2c7a3f}
@media (prefers-color-scheme:dark){:root{--bg:#14181f;--card:#1d232e;--line:#333d4f;--ink:#e6eaf2;--mut:#98a2b5;--acc:#6ea8ec;--bad:#e08a84;--ok:#7fc98f}}
*{box-sizing:border-box}
.progress{height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin:0 0 10px}
.progress i{display:block;height:100%;width:40%;background:var(--acc);border-radius:2px;animation:pslide 1.1s ease-in-out infinite}
@keyframes pslide{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
body{margin:0;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:68rem;margin:0 auto;padding:18px 14px 60px}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:6px 0 14px}
h1{font-size:20px;margin:0}
.navlink{display:inline-flex;align-items:center;min-height:44px;color:var(--acc);font-size:14px;text-decoration:none}
.navlinks{display:flex;gap:14px;align-items:center}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.searchrow{display:flex;gap:8px;flex-wrap:wrap}
.txt{flex:1;min-width:200px;min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font-size:15px;background:var(--card);color:var(--ink)}
.sel{min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font-size:14px;background:var(--card);color:var(--ink)}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:10px 16px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:14px;cursor:pointer;text-decoration:none;white-space:nowrap}
.btn-primary{background:var(--acc);border-color:var(--acc);color:#fff}
.btn:disabled{opacity:.55;cursor:default}
.hint{color:var(--mut);font-size:12px;margin-top:10px;line-height:1.6}
.sum{font-size:16px;font-weight:600;line-height:1.6}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tab{flex:1}
.tab-on{background:var(--acc);border-color:var(--acc);color:#fff}
.calnav{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.cal-title{flex:1;text-align:center;font-weight:600;font-size:15px}
.calgrid{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.wd{text-align:center;color:var(--mut);font-size:12px;padding:4px 0}
.cell{min-height:52px;padding:4px 2px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px}
.cell-num{font-size:13px;font-weight:600}
.cell-vol{font-size:11px;color:var(--mut)}
.cell-blank{border:1px solid transparent}
.cell-future{opacity:.4;cursor:default}
.cell-sel{outline:2px solid var(--acc);outline-offset:-2px}
.lv1{background:color-mix(in srgb,var(--acc) 12%,transparent)}
.lv2{background:color-mix(in srgb,var(--acc) 26%,transparent)}
.lv3{background:color-mix(in srgb,var(--acc) 45%,transparent)}
.detail{margin-top:12px;font-size:13px;line-height:1.7}
.prow{padding:12px 2px;border-bottom:1px solid var(--line)}
.prow:last-child{border-bottom:none}
.prow-top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.prow-range{font-size:15px;font-weight:600}
.prow-count{font-size:13px;color:var(--mut);white-space:nowrap}
.prow-meta{font-size:12px;color:var(--mut);margin-top:4px}
.bar{height:6px;background:var(--line);border-radius:3px;margin-top:8px;overflow:hidden}
.bar-fill{height:100%;background:var(--acc)}
.yr{font-size:13px;font-weight:600;color:var(--mut);margin:14px 0 2px}
.pager{display:flex;gap:8px;align-items:center;justify-content:center;margin-top:12px}
.pager-info{color:var(--mut);font-size:13px}
.empty{color:var(--mut);font-size:14px;padding:18px 4px;line-height:1.7}
.empty-err{color:var(--bad)}
@media (max-width:640px){
.searchrow{flex-direction:column}
.txt{width:100%}
.sel{width:100%}
.btn-search{width:100%}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar"><h1>磁力统计</h1><span class="navlinks"><a class="navlink" href="./">搜索</a><a class="navlink" href="export">导出</a></span></div>
  <div class="card">
    <div class="progress" id="s-progress" style="display:none" aria-hidden="true"><i></i></div>
    <div class="sum" id="s-sum">加载中…</div>
    <div class="hint" id="s-undated" style="display:none"></div>
    <div class="hint" id="s-meta"></div>
    <div class="hint">统计口径：全库数据，排除筛选不影响统计。日期为内容发布日（北京时间）。</div>
  </div>
  <div class="card">
    <div class="searchrow">
      <input id="f-q" class="txt" type="text" placeholder="关键词筛选，如 fc2 4897458" autocomplete="off" spellcheck="false">
      <select id="f-cat" class="sel"><option value="">全部类型</option></select>
      <button id="f-go" class="btn btn-primary btn-search" type="button">统计</button>
    </div>
  </div>
  <div class="card">
    <div class="tabs">
      <button id="t-day" class="btn tab tab-on" type="button">日</button>
      <button id="t-week" class="btn tab" type="button">周</button>
      <button id="t-month" class="btn tab" type="button">月</button>
    </div>
    <div id="v-day">
      <div class="calnav">
        <button id="c-prev" class="btn" type="button">← 上月</button>
        <span class="cal-title" id="c-title"></span>
        <button id="c-next" class="btn" type="button">下月 →</button>
        <button id="c-today" class="btn" type="button">本月</button>
      </div>
      <div class="calgrid" id="c-grid"></div>
      <div class="detail" id="c-detail"></div>
    </div>
    <div id="v-week" style="display:none">
      <div id="w-list"></div>
      <div class="pager" id="w-pager" style="display:none">
        <button id="w-prev" class="btn" type="button">上一页</button>
        <span class="pager-info" id="w-info"></span>
        <button id="w-next" class="btn" type="button">下一页</button>
      </div>
    </div>
    <div id="v-month" style="display:none">
      <div id="m-list"></div>
      <div class="pager" id="m-pager" style="display:none">
        <button id="m-prev" class="btn" type="button">上一页</button>
        <span class="pager-info" id="m-info"></span>
        <button id="m-next" class="btn" type="button">下一页</button>
      </div>
    </div>
    <div id="empty" class="empty" style="display:none"></div>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
let series = [];
let dayMap = new Map();
let totals = null;
let catsLoaded = false;
let curView = "day";
let curMonth = null;
let selDay = null;
let weekPage = 1;
let monthPage = 1;
let seq = 0;
let refreshTimer = null;
const PAGE = 12;

function pad(n) { return (n < 10 ? "0" : "") + n; }
function dayKey(y, m, d) { return y + "-" + pad(m) + "-" + pad(d); }
function parseDay(s) {
  const p = s.split("-");
  return {y: parseInt(p[0], 10), m: parseInt(p[1], 10), d: parseInt(p[2], 10)};
}
function todayKey() {
  const t = new Date();
  return dayKey(t.getFullYear(), t.getMonth() + 1, t.getDate());
}
function addDays(key, n) {
  const p = parseDay(key);
  const t = new Date(p.y, p.m - 1, p.d + n);
  return dayKey(t.getFullYear(), t.getMonth() + 1, t.getDate());
}
function mondayOf(key) {
  const p = parseDay(key);
  const t = new Date(p.y, p.m - 1, p.d);
  const dow = (t.getDay() + 6) % 7;
  return addDays(key, -dow);
}
function fmtBytes(b) {
  if (b == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let v = b, u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  let s;
  if (u === 0) s = String(Math.round(v));
  else if (v >= 100) s = String(Math.round(v));
  else if (v >= 10) s = v.toFixed(1);
  else s = v.toFixed(2);
  return s + " " + units[u];
}
function fmtBytesShort(b) {
  if (!b) return "";
  const units = ["B", "K", "M", "G", "T", "P"];
  let v = b, u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  let s;
  if (v >= 100) s = String(Math.round(v));
  else if (v >= 10) s = String(Math.round(v * 10) / 10);
  else s = String(Math.round(v * 100) / 100);
  return s + units[u];
}
function withCumulative(days) {
  let cn = 0, cb = 0;
  return days.map(function (it) {
    cn += it.n; cb += it.b;
    return {d: it.d, n: it.n, b: it.b, cumN: cn, cumB: cb};
  });
}
function rollupWeeks(days) {
  const weeks = [];
  let cur = null;
  for (const it of days) {
    const mon = mondayOf(it.d);
    if (!cur || cur.start !== mon) {
      cur = {start: mon, end: addDays(mon, 6), n: 0, b: 0, cumN: 0, cumB: 0};
      weeks.push(cur);
    }
    cur.n += it.n; cur.b += it.b;
    cur.cumN = it.cumN; cur.cumB = it.cumB;
  }
  return weeks;
}
function rollupMonths(days) {
  const months = [];
  let cur = null;
  for (const it of days) {
    const ym = it.d.slice(0, 7);
    if (!cur || cur.ym !== ym) {
      const p = parseDay(it.d);
      cur = {ym: ym, y: p.y, m: p.m, n: 0, b: 0, cumN: 0, cumB: 0};
      months.push(cur);
    }
    cur.n += it.n; cur.b += it.b;
    cur.cumN = it.cumN; cur.cumB = it.cumB;
  }
  return months;
}
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
function fmtRange(a, b) {
  return a.slice(5) + " ~ " + b.slice(5);
}
function fmtSnapshotTime(epoch) {
  const t = new Date(epoch * 1000);
  const now = new Date();
  const hm = pad(t.getHours()) + ":" + pad(t.getMinutes());
  if (t.getFullYear() === now.getFullYear() &&
      t.getMonth() === now.getMonth() && t.getDate() === now.getDate()) {
    return hm;
  }
  return pad(t.getMonth() + 1) + "-" + pad(t.getDate()) + " " + hm;
}
function renderMeta(updating) {
  const t = totals;
  if (!t || t.computed_at == null) { $("s-meta").textContent = ""; return; }
  let s = "数据截至 " + fmtSnapshotTime(t.computed_at) +
    " · 查询用时 " + (t.compute_ms / 1000).toFixed(2) + " 秒";
  if (updating) s += " · 正在更新…";
  $("s-meta").textContent = s;
}
function maybeSilentRefresh(d, q, cat, my) {
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
  if (!d.stale) return;
  renderMeta(true);
  let tries = 0;
  const tick = async () => {
    refreshTimer = null;
    if (my !== seq) return;
    tries++;
    let nd;
    try {
      nd = await api("api/stats/daily?q=" + encodeURIComponent(q) +
        "&cat=" + encodeURIComponent(cat));
    } catch (e) { return; }
    if (my !== seq || !nd.ok) return;
    if (nd.computed_at === d.computed_at && nd.stale && tries < 5) {
      refreshTimer = setTimeout(tick, 6000);
      return;
    }
    totals = nd;
    series = withCumulative(nd.days || []);
    dayMap = new Map();
    for (const it of series) dayMap.set(it.d, it);
    renderSummary();
    renderMeta(false);
    if (series.length) renderView();
  };
  refreshTimer = setTimeout(tick, 6000);
}
function setViewsVisible(v) {
  $("v-day").style.display = v && curView === "day" ? "block" : "none";
  $("v-week").style.display = v && curView === "week" ? "block" : "none";
  $("v-month").style.display = v && curView === "month" ? "block" : "none";
}
function showEmpty(node, isErr) {
  setViewsVisible(false);
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
function showEmptyState() {
  const q = $("f-q").value.trim();
  const cat = $("f-cat").value;
  const wrap = document.createElement("div");
  const s = document.createElement("span");
  if (totals && totals.total > 0) {
    s.textContent = "筛选到的 " + totals.total.toLocaleString() + " 条都没有发布日期，无法按日历显示。";
    wrap.appendChild(s);
  } else {
    s.textContent = "没有命中" +
      (q ? "「" + q + "」" : "") +
      (cat ? "（类型：" + cat + "）" : "") +
      " 的条目。";
    const clr = document.createElement("button");
    clr.type = "button";
    clr.className = "btn";
    clr.style.marginLeft = "10px";
    clr.textContent = "清除筛选";
    clr.onclick = () => {
      $("f-q").value = "";
      $("f-cat").value = "";
      loadStats();
    };
    wrap.appendChild(s);
    wrap.appendChild(clr);
  }
  showEmpty(wrap, false);
}
function showError(text) {
  $("s-sum").textContent = "";
  showEmptyText(text, true);
}
function renderSummary() {
  const t = totals;
  let s = "共 " + t.total.toLocaleString() + " 条 · 总体积 " + fmtBytes(t.total_bytes);
  if (t.min_date && t.max_date) s += " · " + t.min_date + " ~ " + t.max_date;
  $("s-sum").textContent = s;
  $("s-undated").style.display = t.undated_count ? "block" : "none";
  if (t.undated_count) {
    $("s-undated").textContent = "其中 " + t.undated_count.toLocaleString() +
      " 条日期不明（体积 " + fmtBytes(t.undated_bytes) + "），已计入总数，不进日历。";
  }
}
async function loadStats() {
  const my = ++seq;
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
  const done = busyBtn($("f-go"));
  const q = $("f-q").value.trim();
  const cat = $("f-cat").value;
  $("s-sum").textContent = "统计中…";
  const t0 = Date.now();
  let elapsedTimer = null;
  const showProgress = () => {
    if (my !== seq) return;
    $("s-progress").style.display = "block";
    elapsedTimer = setInterval(() => {
      if (my !== seq) return;
      $("s-sum").textContent = "统计中… " +
        ((Date.now() - t0) / 1000).toFixed(1) + " 秒";
    }, 100);
  };
  const progressTimer = setTimeout(showProgress, 300);
  const hideProgress = () => {
    clearTimeout(progressTimer);
    if (elapsedTimer) clearInterval(elapsedTimer);
    if (my === seq) $("s-progress").style.display = "none";
  };
  let d;
  try {
    d = await api("api/stats/daily?q=" + encodeURIComponent(q) +
      "&cat=" + encodeURIComponent(cat));
  } catch (e) {
    hideProgress();
    done();
    if (my === seq) showError("统计失败（网络或服务异常），请点统计重试。");
    return;
  }
  done();
  if (my !== seq) { hideProgress(); return; }
  hideProgress();
  if (!d.ok) { showError(d.message || "统计失败，请重试。"); return; }
  totals = d;
  series = withCumulative(d.days || []);
  dayMap = new Map();
  for (const it of series) dayMap.set(it.d, it);
  if (!catsLoaded) {
    const sel = $("f-cat");
    for (const c of (d.categories || [])) {
      const o = document.createElement("option");
      o.value = c; o.textContent = c; sel.appendChild(o);
    }
    catsLoaded = true;
  }
  renderSummary();
  renderMeta(false);
  maybeSilentRefresh(d, q, cat, my);
  weekPage = 1; monthPage = 1;
  if (!series.length) { showEmptyState(); return; }
  $("empty").style.display = "none";
  if (!curMonth) curMonth = (d.max_date || todayKey()).slice(0, 7);
  selDay = null;
  renderView();
}
function renderView() {
  setViewsVisible(true);
  if (curView === "day") renderDay();
  else if (curView === "week") renderWeek();
  else renderMonth();
}
function setView(v) {
  if (!series.length) return;
  curView = v;
  $("t-day").className = "btn tab" + (v === "day" ? " tab-on" : "");
  $("t-week").className = "btn tab" + (v === "week" ? " tab-on" : "");
  $("t-month").className = "btn tab" + (v === "month" ? " tab-on" : "");
  renderView();
}
function renderDay() {
  const p = parseDay(curMonth + "-01");
  $("c-title").textContent = p.y + "年" + p.m + "月";
  const first = new Date(p.y, p.m - 1, 1);
  const leadBlanks = (first.getDay() + 6) % 7;
  const daysInMonth = new Date(p.y, p.m, 0).getDate();
  const today = todayKey();
  let mx = 0;
  for (const it of series) {
    if (it.d.slice(0, 7) === curMonth && it.b > mx) mx = it.b;
  }
  const grid = $("c-grid");
  grid.innerHTML = "";
  const wds = ["一", "二", "三", "四", "五", "六", "日"];
  for (const w of wds) {
    const h = document.createElement("div");
    h.className = "wd";
    h.textContent = w;
    grid.appendChild(h);
  }
  for (let i = 0; i < leadBlanks; i++) {
    const b = document.createElement("div");
    b.className = "cell-blank";
    grid.appendChild(b);
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const key = dayKey(p.y, p.m, d);
    const it = dayMap.get(key);
    const b = document.createElement("button");
    b.type = "button";
    let cls = "cell";
    if (key > today) cls += " cell-future";
    else if (it && mx > 0) {
      const r = it.b / mx;
      if (r > 0.66) cls += " lv3";
      else if (r > 0.33) cls += " lv2";
      else if (r > 0) cls += " lv1";
    }
    if (selDay === key) cls += " cell-sel";
    b.className = cls;
    b.disabled = key > today;
    const num = document.createElement("span");
    num.className = "cell-num";
    num.textContent = String(d);
    const vol = document.createElement("span");
    vol.className = "cell-vol";
    vol.textContent = it ? fmtBytesShort(it.b) : "";
    b.appendChild(num);
    b.appendChild(vol);
    b.onclick = (function (k) { return function () { selDay = k; renderDay(); }; })(key);
    grid.appendChild(b);
  }
  const minMonth = totals.min_date ? totals.min_date.slice(0, 7) : curMonth;
  const maxMonth = today.slice(0, 7);
  $("c-prev").disabled = curMonth <= minMonth;
  $("c-next").disabled = curMonth >= maxMonth;
  renderDayDetail();
}
function renderDayDetail() {
  const el = $("c-detail");
  if (selDay) {
    const it = dayMap.get(selDay);
    if (it) {
      el.textContent = selDay + "：新增 " + it.n.toLocaleString() + " 条 · 体积 " +
        fmtBytes(it.b) + " · 累计 " + it.cumN.toLocaleString() + " 条 / " + fmtBytes(it.cumB);
    } else {
      let prev = null;
      for (let i = series.length - 1; i >= 0; i--) {
        if (series[i].d < selDay) { prev = series[i]; break; }
      }
      el.textContent = selDay + "：没有新增。" +
        (prev ? "累计 " + prev.cumN.toLocaleString() + " 条 / " + fmtBytes(prev.cumB) + "。" : "");
    }
    return;
  }
  let n = 0, b = 0, cumN = 0, cumB = 0;
  for (const it of series) {
    if (it.d.slice(0, 7) === curMonth) { n += it.n; b += it.b; cumN = it.cumN; cumB = it.cumB; }
  }
  el.textContent = "本月新增 " + n.toLocaleString() + " 条 · 体积 " + fmtBytes(b) +
    (n ? " · 累计 " + cumN.toLocaleString() + " 条 / " + fmtBytes(cumB) : "");
}
function shiftMonth(delta) {
  const p = parseDay(curMonth + "-01");
  const t = new Date(p.y, p.m - 1 + delta, 1);
  curMonth = dayKey(t.getFullYear(), t.getMonth() + 1, 1).slice(0, 7);
  selDay = null;
  renderDay();
}
function periodRow(label, n, b, cumN, cumB, mx) {
  const row = document.createElement("div");
  row.className = "prow";
  const top = document.createElement("div");
  top.className = "prow-top";
  const lb = document.createElement("span");
  lb.className = "prow-range";
  lb.textContent = label;
  const ct = document.createElement("span");
  ct.className = "prow-count";
  ct.textContent = "新增 " + n.toLocaleString() + " 条";
  top.appendChild(lb);
  top.appendChild(ct);
  const meta = document.createElement("div");
  meta.className = "prow-meta";
  meta.textContent = "体积 " + fmtBytes(b) + " · 累计 " + cumN.toLocaleString() + " 条 / " + fmtBytes(cumB);
  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("div");
  fill.className = "bar-fill";
  fill.style.width = (mx > 0 ? Math.max(2, Math.round(b / mx * 100)) : 0) + "%";
  bar.appendChild(fill);
  row.appendChild(top);
  row.appendChild(meta);
  row.appendChild(bar);
  return row;
}
function renderWeek() {
  const weeks = rollupWeeks(series).slice().reverse();
  const pages = Math.max(1, Math.ceil(weeks.length / PAGE));
  if (weekPage > pages) weekPage = pages;
  const slice = weeks.slice((weekPage - 1) * PAGE, weekPage * PAGE);
  let mx = 0;
  for (const w of weeks) if (w.b > mx) mx = w.b;
  const box = $("w-list");
  box.innerHTML = "";
  for (const w of slice) {
    box.appendChild(periodRow(fmtRange(w.start, w.end), w.n, w.b, w.cumN, w.cumB, mx));
  }
  $("w-pager").style.display = weeks.length ? "flex" : "none";
  $("w-info").textContent = "第 " + weekPage + " / " + pages + " 页";
  $("w-prev").disabled = weekPage <= 1;
  $("w-next").disabled = weekPage >= pages;
}
function renderMonth() {
  const months = rollupMonths(series).slice().reverse();
  const pages = Math.max(1, Math.ceil(months.length / PAGE));
  if (monthPage > pages) monthPage = pages;
  const slice = months.slice((monthPage - 1) * PAGE, monthPage * PAGE);
  let mx = 0;
  for (const m of months) if (m.b > mx) mx = m.b;
  const box = $("m-list");
  box.innerHTML = "";
  let lastYear = null;
  for (const m of slice) {
    if (m.y !== lastYear) {
      const h = document.createElement("div");
      h.className = "yr";
      h.textContent = m.y + " 年";
      box.appendChild(h);
      lastYear = m.y;
    }
    box.appendChild(periodRow(m.y + "年" + m.m + "月", m.n, m.b, m.cumN, m.cumB, mx));
  }
  $("m-pager").style.display = months.length ? "flex" : "none";
  $("m-info").textContent = "第 " + monthPage + " / " + pages + " 页";
  $("m-prev").disabled = monthPage <= 1;
  $("m-next").disabled = monthPage >= pages;
}
$("f-go").onclick = function () { loadStats(); };
$("f-q").addEventListener("keydown", function (e) {
  if (e.key === "Enter") { e.preventDefault(); loadStats(); }
});
$("f-cat").addEventListener("change", function () { loadStats(); });
$("t-day").onclick = function () { setView("day"); };
$("t-week").onclick = function () { setView("week"); };
$("t-month").onclick = function () { setView("month"); };
$("c-prev").onclick = function () { shiftMonth(-1); };
$("c-next").onclick = function () { shiftMonth(1); };
$("c-today").onclick = function () { curMonth = todayKey().slice(0, 7); selDay = null; renderDay(); };
$("w-prev").onclick = function () { weekPage--; renderWeek(); };
$("w-next").onclick = function () { weekPage++; renderWeek(); };
$("m-prev").onclick = function () { monthPage--; renderMonth(); };
$("m-next").onclick = function () { monthPage++; renderMonth(); };
loadStats();
</script>
</body>
</html>
"""
