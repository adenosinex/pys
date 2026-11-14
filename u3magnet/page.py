"""Page for u3磁力获取 (u3magnet).

UI canon mapping (《平台 UI 设计逻辑》, 2026-11-11; pointer in the
platform map, doc at panel 经验/app-hub/UI设计逻辑.md):
- Task first (P1): the search box is the first screen; one task
  per page; the usage manual is deleted, not hidden (P9).
- Row budget (P4): each result shows title + date + size + category
  (4 info points); the magnet action group is the single next step.
- No table: results are cards, so the phone never scrolls sideways
  and the magnet action is always on screen (P10).
- Touch targets >= 44px; search button full-width on mobile (P10).
- Pagination 15/page (P6: 10-15); filter is the convergence means,
  pagination is the fallback.
- Exclusion controls live behind a disclosure whose summary always
  shows the live state (P3); destructive Clear stays inside (P10).
- Empty states echo the query/filters and offer a clear-filters
  action (P8); search has busy state + loading feedback (self-check).
- Follows the OS light/dark setting (new-page hard requirement).
"""

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>u3 磁力获取</title>
<style>
:root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--line:#e2e7f0;--ink:#1d2433;--mut:#68707f;--acc:#2f6fd0;--bad:#b3372f;--ok:#2c7a3f}
@media (prefers-color-scheme:dark){:root{--bg:#14181f;--card:#1d232e;--line:#333d4f;--ink:#e6eaf2;--mut:#98a2b5;--acc:#6ea8ec;--bad:#e08a84;--ok:#7fc98f}}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:68rem;margin:0 auto;padding:18px 14px 60px}
h1{font-size:20px;margin:0}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:6px 0 14px}
.navlink{display:inline-flex;align-items:center;min-height:44px;color:var(--acc);font-size:14px;text-decoration:none}
.navlinks{display:flex;gap:14px;align-items:center}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.searchrow{display:flex;gap:8px;flex-wrap:wrap}
.txt{flex:1;min-width:200px;min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font-size:15px;background:var(--card);color:var(--ink)}
.sel{min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;font-size:14px;background:var(--card);color:var(--ink)}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:10px 16px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:14px;cursor:pointer;text-decoration:none;white-space:nowrap}
.btn-primary{background:var(--acc);border-color:var(--acc);color:#fff}
.btn-danger{color:var(--bad)}
.btn:disabled{opacity:.55;cursor:default}
.hint{color:var(--mut);font-size:12px;margin-top:10px;line-height:1.6}
.msg{font-size:13px;color:var(--ok);min-height:18px}
.err{color:var(--bad);font-size:13px;line-height:1.6}
.res{padding:12px 2px;border-bottom:1px solid var(--line)}
.res:last-child{border-bottom:none}
.res-title{overflow-wrap:anywhere;line-height:1.5;font-size:15px;font-weight:600}
.res-meta{display:flex;gap:10px;flex-wrap:wrap;align-items:center;color:var(--mut);font-size:12px;margin-top:4px}
.res-actions{display:flex;gap:8px;margin-top:10px}
.tag{font-size:11px;border:1px solid var(--line);border-radius:20px;padding:1px 8px;color:var(--mut);white-space:nowrap}
.pager{display:flex;gap:8px;align-items:center;justify-content:center;margin-top:12px}
.pager-info{color:var(--mut);font-size:13px}
.empty{color:var(--mut);font-size:14px;padding:18px 4px;line-height:1.7}
.empty-err{color:var(--bad)}
summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;gap:8px;font-weight:600;min-height:44px}
summary::-webkit-details-marker{display:none}
.x-body{margin-top:6px}
.x-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
@media (max-width:640px){
.searchrow{flex-direction:column}
.txt{width:100%}
.sel{width:100%}
.btn-search{width:100%}
.act{flex:1}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar"><h1>u3 磁力获取</h1><span class="navlinks"><a class="navlink" href="stats">统计</a><a class="navlink" href="export">导出</a></span></div>
  <div class="card">
    <div class="searchrow">
      <input id="f-q" class="txt" type="text" placeholder="输入关键词，如 fc2 4897458" autocomplete="off" spellcheck="false">
      <select id="f-cat" class="sel"><option value="">全部类型</option></select>
      <button id="f-search" class="btn btn-primary btn-search" type="button">搜索</button>
    </div>
    <div class="hint" id="f-count"></div>
    <div id="list"></div>
    <div id="empty" class="empty" style="display:none"></div>
    <div class="pager" id="pager" style="display:none">
      <button id="p-prev" class="btn" type="button">上一页</button>
      <span class="pager-info" id="p-info"></span>
      <button id="p-next" class="btn" type="button">下一页</button>
    </div>
  </div>
  <details class="card" id="x-box">
    <summary><span>排除筛选</span><span class="tag" id="x-state">加载中…</span></summary>
    <div class="x-body">
      <div class="hint">上传磁力列表文件（.txt，一行一条：整条 magnet、裸 infohash 都认），命中的条目不会出现在搜索结果里。上传是追加，可随时清空。</div>
      <div class="x-row">
        <input id="x-file" type="file" accept=".txt,text/plain">
        <button id="x-upload" class="btn btn-primary" type="button">上传并追加</button>
        <button id="x-toggle" class="btn" type="button">停用排除</button>
        <button id="x-clear" class="btn btn-danger" type="button">清空排除集</button>
      </div>
      <div class="hint" id="x-last"></div>
      <div class="msg" id="x-msg"></div>
    </div>
  </details>
</div>
<script>
const $ = (id) => document.getElementById(id);
let curPage = 1, curTotal = 0, catsLoaded = false, seq = 0;

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
async function search(page) {
  const my = ++seq;
  const done = busyBtn($("f-search"));
  curPage = page || 1;
  const q = $("f-q").value.trim();
  const cat = $("f-cat").value;
  $("f-count").textContent = "搜索中…";
  let d;
  try {
    d = await api("api/search?q=" + encodeURIComponent(q) +
      "&cat=" + encodeURIComponent(cat) + "&page=" + curPage);
  } catch (e) {
    if (my === seq) {
      $("f-count").textContent = "";
      $("list").innerHTML = "";
      $("pager").style.display = "none";
      showEmptyText("搜索失败（网络或服务异常），请点搜索重试。", true);
    }
    done();
    return;
  }
  done();
  if (my !== seq) return;
  if (!d.ok) {
    $("f-count").textContent = "";
    $("list").innerHTML = "";
    $("pager").style.display = "none";
    showEmptyText(d.message || "搜索失败，请重试。", true);
    return;
  }
  curTotal = d.total;
  if (!catsLoaded) {
    const sel = $("f-cat");
    for (const c of (d.categories || [])) {
      const o = document.createElement("option");
      o.value = c; o.textContent = c; sel.appendChild(o);
    }
    catsLoaded = true;
  }
  $("f-count").textContent = "命中 " + d.total.toLocaleString() +
    " 条（库内共 " + d.db_total.toLocaleString() + " 条）" +
    (d.exclude && d.exclude.enabled && d.exclude.count
      ? " · 已排除 " + d.exclude.count.toLocaleString() + " 条" : "");
  const box = $("list");
  box.innerHTML = "";
  for (const it of (d.items || [])) {
    const card = document.createElement("div");
    card.className = "res";
    const t = document.createElement("div");
    t.className = "res-title";
    t.textContent = it.title;
    const meta = document.createElement("div");
    meta.className = "res-meta";
    const tm = document.createElement("span");
    tm.textContent = fmtTime(it.published_at);
    const sz = document.createElement("span");
    sz.textContent = it.size_text || "";
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = it.category || "";
    meta.appendChild(tm);
    meta.appendChild(sz);
    meta.appendChild(tag);
    const acts = document.createElement("div");
    acts.className = "res-actions";
    const a = document.createElement("a");
    a.className = "btn act";
    a.href = it.magnet;
    a.textContent = "磁力链接";
    a.title = "magnet:?xt=urn:btih:" + it.infohash;
    const cp = document.createElement("button");
    cp.type = "button";
    cp.className = "btn act";
    cp.textContent = "复制磁力";
    cp.onclick = async () => {
      try { await navigator.clipboard.writeText(it.magnet); cp.textContent = "已复制"; }
      catch (e) { cp.textContent = "复制失败"; }
      setTimeout(() => { cp.textContent = "复制磁力"; }, 1500);
    };
    acts.appendChild(a);
    acts.appendChild(cp);
    card.appendChild(t);
    card.appendChild(meta);
    card.appendChild(acts);
    box.appendChild(card);
  }
  const pages = Math.max(1, Math.ceil(d.total / d.page_size));
  if (!d.total) {
    const wrap = document.createElement("div");
    const s = document.createElement("span");
    if (q || cat) {
      s.textContent = "没有命中" +
        (q ? "「" + q + "」" : "") +
        (cat ? "（类型：" + cat + "）" : "") +
        " 的条目。换个关键词、清掉类型过滤，或检查排除集是否把它们排除了。";
      const clr = document.createElement("button");
      clr.type = "button";
      clr.className = "btn";
      clr.style.marginLeft = "10px";
      clr.textContent = "清除筛选";
      clr.onclick = () => {
        $("f-q").value = "";
        $("f-cat").value = "";
        search(1);
      };
      wrap.appendChild(s);
      wrap.appendChild(clr);
    } else {
      s.textContent = "库里暂时没有数据（爬取可能尚未完成）。";
      wrap.appendChild(s);
    }
    showEmpty(wrap, false);
  } else {
    $("empty").style.display = "none";
  }
  $("pager").style.display = d.total ? "flex" : "none";
  $("p-info").textContent = "第 " + curPage + " / " + pages + " 页";
  $("p-prev").disabled = curPage <= 1;
  $("p-next").disabled = curPage >= pages;
  renderExclude(d.exclude);
}
function renderExclude(x) {
  if (!x) return;
  $("x-state").textContent = x.enabled
    ? ("已启用 · 排除 " + x.count.toLocaleString() + " 条")
    : ("已停用 · 排除集 " + x.count.toLocaleString() + " 条（不生效）");
  $("x-toggle").textContent = x.enabled ? "停用排除" : "启用排除";
  const lu = x.last_upload;
  $("x-last").textContent = lu
    ? ("最近上传：" + lu.filename + " · " + lu.at + " · 共 " + lu.lines +
       " 行，识别 " + lu.recognized + " 行，新增 " + lu.added + " 条" +
       (lu.bad ? "，" + lu.bad + " 行没认出来" : ""))
    : "还没有上传过排除文件。";
}
$("f-search").onclick = (e) => { search(1); };
$("f-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); search(1); }
});
$("f-cat").addEventListener("change", () => search(1));
$("p-prev").onclick = () => search(curPage - 1);
$("p-next").onclick = () => search(curPage + 1);
$("x-upload").onclick = async (e) => {
  const f = $("x-file").files[0];
  const msg = $("x-msg");
  if (!f) { msg.textContent = "先选一个 .txt 文件。"; return; }
  const done = busyBtn(e.currentTarget);
  const fd = new FormData();
  fd.append("file", f);
  const d = await api("api/exclude/upload", "POST", fd, true);
  done();
  msg.textContent = d.message || (d.ok ? "已上传。" : "上传失败。");
  if (d.ok) { $("x-file").value = ""; renderExclude(d); search(curPage); }
};
$("x-clear").onclick = async (e) => {
  if (!confirm("清空全部排除记录？之后这些条目会重新出现在搜索结果里。")) return;
  const done = busyBtn(e.currentTarget);
  const d = await api("api/exclude/clear", "POST", {});
  done();
  $("x-msg").textContent = d.message || "";
  if (d.ok) { renderExclude(d); search(curPage); }
};
$("x-toggle").onclick = async (e) => {
  const done = busyBtn(e.currentTarget);
  const d = await api("api/exclude/toggle", "POST", {});
  done();
  $("x-msg").textContent = d.message || "";
  if (d.ok) { renderExclude(d); search(curPage); }
};
search(1);
api("api/exclude/state").then((d) => { if (d.ok) renderExclude(d); });
</script>
</body>
</html>
"""
