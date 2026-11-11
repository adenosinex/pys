"""Page for 直连管理 (directmgr).

Design per 《平台 UI 设计逻辑》: compact tool UI, 68rem measure,
follows the OS light/dark setting (new-page hard requirement),
delete action demoted, sync status always visible (failures are
never silent).

Frontend normalization (user ruling 2026-11-11): pasted/typed text
is tidied into a bare domain INSIDE the input box, visibly, before
submit; the user can still edit the tidied text and what is in the
box is what gets submitted. Timing: normalize on paste and on blur
-- never per keystroke, so the caret is never yanked mid-typing.
Rules (fixed): strip scheme (http/https), strip path/query/hash,
strip port, strip trailing dot, lowercase, trim whitespace. A www.
prefix is KEPT as given (www.example.com covers only www + its
subdomains; example.com covers everything) -- the hint line under
the input says so. The backend accepts only the canonical form
and rejects anything else without fixing it.
"""

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>直连管理</title>
<style>
:root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--line:#e2e7f0;--ink:#1d2433;--mut:#68707f;--acc:#2f6fd0;--bad:#b3372f;--ok:#2c7a3f}
@media (prefers-color-scheme:dark){:root{--bg:#14181f;--card:#1d232e;--line:#333d4f;--ink:#e6eaf2;--mut:#98a2b5;--acc:#6ea8ec;--bad:#e08a84;--ok:#7fc98f}}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:68rem;margin:0 auto;padding:18px 14px 60px}
h1{font-size:20px;margin:6px 0 4px}
.sub{color:var(--mut);font-size:13px;margin-bottom:16px;line-height:1.6}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=text]{flex:1;min-width:200px;padding:9px 10px;border:1px solid var(--line);border-radius:9px;font-size:14px;background:var(--card);color:var(--ink)}
button{padding:8px 14px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:13px;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
button.danger{color:var(--bad)}
button:disabled{opacity:.55;cursor:default}
.hint{color:var(--mut);font-size:12px;margin-top:8px;line-height:1.6}
.msg{font-size:13px;color:var(--ok);min-height:18px}
.err{color:var(--bad);font-size:13px;line-height:1.6}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:600}
.tag{font-size:11px;border:1px solid var(--line);border-radius:20px;padding:1px 8px;color:var(--mut)}
.dom{font-weight:600;overflow-wrap:anywhere}
.note{color:var(--mut)}
.tscroll{overflow-x:auto}
</style>
</head>
<body>
<div class="wrap">
  <h1>直连管理</h1>
  <div class="sub">名单里的每个域名（含全部子域名）在自营订阅里走直连（DIRECT）。增删后三份订阅文件自动同步；客户端刷新订阅后生效。</div>

  <div class="card">
    <div class="row">
      <input id="f-domain" type="text" placeholder="dedirock.com" autocomplete="off" spellcheck="false">
      <input id="f-note" type="text" placeholder="备注（可选）" maxlength="80" style="flex:0 1 220px;min-width:140px">
      <button id="f-add" class="primary" type="button">添加</button>
    </div>
    <div class="hint">粘贴任意链接自动整理为纯域名；填 dedirock.com 覆盖全部子域名（粘贴带 www 的会保留 www 本体，只覆盖 www 及其子域）。</div>
    <div class="row" style="margin-top:8px"><span class="msg" id="f-msg"></span></div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div class="dom">同步状态</div>
      <button id="f-resync" type="button">重新同步</button>
    </div>
    <div id="sync-state" class="hint">加载中…</div>
    <div id="sync-err" class="err"></div>
    <div class="hint" id="tpl-note"></div>
  </div>

  <div class="card">
    <div class="tscroll"><table>
      <thead><tr><th>域名</th><th>覆盖</th><th>添加时间</th><th>备注</th><th></th></tr></thead>
      <tbody id="list"></tbody>
    </table></div>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);

// Frontend normalization (fixed rules; see module docstring).
// Timing: paste + blur only -- never per keystroke.
function normalizeDomain(raw) {
  let s = String(raw == null ? "" : raw).trim();
  if (!s) return "";
  const sp = s.indexOf("://");
  if (sp > -1) s = s.slice(sp + 3);                      // scheme
  s = s.split("/")[0].split("?")[0].split("#")[0];       // path/query/hash
  s = s.trim();
  // strip port (host:port); IPv6 literals are not valid input here
  const ci = s.lastIndexOf(":");
  if (ci > -1 && s.slice(ci + 1) !== "" && !isNaN(Number(s.slice(ci + 1)))) s = s.slice(0, ci);
  while (s.endsWith(".")) s = s.slice(0, -1);            // trailing dot(s)
  return s.toLowerCase();
}
const inp = $("f-domain");
inp.addEventListener("paste", () => {
  setTimeout(() => { inp.value = normalizeDomain(inp.value); }, 0);
});
inp.addEventListener("blur", () => { inp.value = normalizeDomain(inp.value); });
inp.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("f-add").click(); }
});

async function api(path, method, payload) {
  const opts = {method: method || "GET", headers: {}};
  if (payload) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(payload); }
  const r = await fetch(path, opts);
  return r.json();
}
function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, (c) =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function busyBtn(b) {
  if (!b || b.disabled) return () => {};
  const t = b.textContent; b.disabled = true; b.textContent = "进行中…";
  return () => { b.disabled = false; b.textContent = t; };
}
function fmtTs(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", {hour12: false});
}
function render(d) {
  if (d.message) $("f-msg").textContent = d.message;
  const st = d.sync || {};
  let line = st.last_sync_at
    ? ("最近同步：" + fmtTs(st.last_sync_at) + (st.ok ? " · 成功" : " · 失败"))
    : "尚无同步记录（首次增删时自动同步）。";
  if (d.drift && d.drift.length) line += " 差异：" + d.drift.join("；");
  $("sync-state").textContent = line;
  $("sync-err").textContent = st.error ? ("同步错误：" + st.error) : "";
  $("tpl-note").textContent = d.template_note || "";
  const tb = $("list");
  tb.innerHTML = "";
  for (const it of (d.domains || [])) {
    const tr = document.createElement("tr");
    tr.innerHTML = "<td class='dom'></td><td>含全部子域名</td><td></td><td class='note'></td><td style='text-align:right'></td>";
    tr.children[0].textContent = it.domain;
    if (it.builtin) {
      const tag = document.createElement("span");
      tag.className = "tag"; tag.textContent = "内置";
      tr.children[0].appendChild(document.createTextNode(" "));
      tr.children[0].appendChild(tag);
    }
    tr.children[2].textContent = it.added_at || "—";
    tr.children[3].textContent = it.note || "";
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "danger"; btn.textContent = "删除";
    btn.onclick = async (e) => {
      const warn = it.builtin
        ? ("删除内置域名 " + it.domain + " 后，自有服务将改走代理节点，可能变慢或不可达，确定删除？")
        : ("删除 " + it.domain + " 的直连？");
      if (!confirm(warn)) return;
      const done = busyBtn(e.currentTarget);
      const r = await api("api/delete", "POST", {domain: it.domain});
      done();
      if (!r.ok && r.error !== "not_found") { $("f-msg").textContent = "失败：" + (r.message || ""); return; }
      render(r.ok ? r : await api("api/list"));
    };
    tr.children[4].appendChild(btn);
    tb.appendChild(tr);
  }
}
async function load() {
  const d = await api("api/list");
  if (d.ok) render(d);
}
$("f-add").onclick = async (e) => {
  inp.value = normalizeDomain(inp.value);
  const domain = inp.value;
  const msg = $("f-msg");
  if (!domain) { msg.textContent = "先填一个域名。"; return; }
  const done = busyBtn(e.currentTarget);
  const d = await api("api/add", "POST", {domain: domain, note: $("f-note").value});
  done();
  if (!d.ok) { msg.textContent = "失败：" + (d.message || ""); return; }
  inp.value = ""; $("f-note").value = "";
  render(d);
};
$("f-resync").onclick = async (e) => {
  const done = busyBtn(e.currentTarget);
  const d = await api("api/resync", "POST", {});
  done();
  if (d.ok) render(d);
};
load();
</script>
</body>
</html>
"""
