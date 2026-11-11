"""Page for DoH 解析 (dohrelay).

Design per the platform UI canon: compact tool UI, 68rem measure,
follows the OS light/dark setting (new-page hard requirement).
First screen answers: what are my two entry addresses, is the
ranking data fresh, is the Shock stack healthy. Credential values
(the DoH path segment, the probe key) are rendered ONLY here,
behind the platform login -- never in chat, reports or the map.
"""

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DoH 解析</title>
<style>
:root{color-scheme:light dark;--bg:#f4f6fa;--card:#fff;--line:#e2e7f0;--ink:#1d2433;--mut:#68707f;--acc:#2f6fd0;--bad:#b3372f;--ok:#2c7a3f;--code:#eef1f6}
@media (prefers-color-scheme:dark){:root{--bg:#14181f;--card:#1d232e;--line:#333d4f;--ink:#e6eaf2;--mut:#98a2b5;--acc:#6ea8ec;--bad:#e08a84;--ok:#7fc98f;--code:#262e3d}}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
.wrap{max-width:68rem;margin:0 auto;padding:18px 14px 60px}
h1{font-size:20px;margin:6px 0 4px}
.sub{color:var(--mut);font-size:13px;margin-bottom:16px;line-height:1.6}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{padding:8px 14px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:13px;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
button.warn{color:var(--bad)}
button:disabled{opacity:.55;cursor:default}
.hint{color:var(--mut);font-size:12px;margin-top:8px;line-height:1.6}
.msg{font-size:13px;color:var(--ok);min-height:18px}
.err{color:var(--bad);font-size:13px;line-height:1.6}
.addr{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;background:var(--code);border-radius:8px;padding:8px 10px;overflow-wrap:anywhere;flex:1;min-width:220px}
.lab{font-weight:600;font-size:13px;margin-bottom:6px}
.tag{font-size:11px;border:1px solid var(--line);border-radius:20px;padding:1px 8px;color:var(--mut)}
.stale{color:var(--bad);font-weight:600}
</style>
</head>
<body>
<div class="wrap">
  <h1>DoH 解析</h1>
  <div class="sub">Shock 是唯一对外解析入口（取真 + 按国内探针实测排序）；广州国内机只做延迟探针、不提供服务。国内域名在 Shock 侧走国内上游分流。</div>

  <div class="card">
    <div class="lab">DoH 地址（浏览器 / FlClash 填这个）</div>
    <div class="row"><span class="addr" id="doh-url">加载中…</span><button id="cp-doh" type="button">复制</button></div>
    <div class="lab" style="margin-top:12px">DoT 主机名（手机「私人 DNS」填这个）</div>
    <div class="row"><span class="addr" id="dot-host">dot.zin7.de</span><button id="cp-dot" type="button">复制</button></div>
    <div class="hint">DoT 端口 853。私人 DNS 只认主机名、不认 IP，所以入口必须落在境外域名上。</div>
    <div class="row" style="margin-top:8px"><span class="msg" id="f-msg"></span></div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div class="lab" style="margin:0">服务状态</div>
      <button id="f-refresh" type="button">刷新</button>
    </div>
    <div id="svc-state" class="hint">加载中…</div>
    <div id="probe-state" class="hint"></div>
  </div>

  <div class="card">
    <div class="lab">国内探针</div>
    <div class="hint" style="margin-top:0">探针包（离线全量，Ubuntu 24.04，一条命令安装）：装在广州机上，只出站推送实测，不开任何入站端口。包内 config 已含当前凭据；重置凭据后需重新下载。</div>
    <div class="row" style="margin-top:8px">
      <a href="probe-package"><button type="button" class="primary">下载探针包</button></a>
      <span class="hint" id="pkg-note"></span>
    </div>
  </div>

  <div class="card">
    <div class="lab">凭据重置</div>
    <div class="hint" style="margin-top:0">重置后旧 DoH 地址立即作废（Shock 侧换新路径段），已填好的设备需改填新地址；探针包也要重新下载。探针密钥独立，不受影响。</div>
    <div class="row" style="margin-top:8px"><button id="f-rotate" class="warn" type="button">重置 DoH 凭据</button></div>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
async function api(path, method, payload) {
  const opts = {method: method || "GET", headers: {}};
  if (payload) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(payload); }
  const r = await fetch(path, opts);
  return r.json();
}
function copyOf(el, btn) {
  const t = el.textContent;
  const done = () => { const o = btn.textContent; btn.textContent = "已复制"; setTimeout(() => btn.textContent = o, 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(done, done);
  else done();
}
function fmtAge(sec) {
  if (sec == null) return "无数据";
  if (sec < 90) return sec + " 秒前";
  if (sec < 5400) return Math.round(sec / 60) + " 分钟前";
  return (sec / 3600).toFixed(1) + " 小时前";
}
function render(d) {
  if (d.message) $("f-msg").textContent = d.message;
  $("doh-url").textContent = d.doh_url || "—";
  $("dot-host").textContent = d.dot_host || "—";
  const h = d.health || {};
  let svc = "Shock 解析栈：" + (h.ok ? "正常" : "异常");
  if (h.agh && h.agh.version) svc += "（AGH " + h.agh.version + "）";
  if (h.queries && h.queries.total != null) svc += " · 累计查询 " + h.queries.total;
  if (h.error) svc += " · " + h.error;
  $("svc-state").textContent = svc;
  const p = d.probe || {};
  let line = "探针数据：";
  if (!p.domains) line += "尚无推送（探针未部署或未到首轮）。";
  else line += "最近 " + fmtAge(p.age_sec) + " · 覆盖 " + p.domains + " 个域名";
  if (p.stale) line += " · <span class='stale'>已陈旧（超 1 小时），当前按原始顺序下发</span>";
  line += " · 排序处理 " + ((d.gate_stats || {}).ranked || 0) + " 次";
  if ((d.gate_stats || {}).probe_unreach) line += " · 已剔除不可达 " + d.gate_stats.probe_unreach;
  $("probe-state").innerHTML = line;
  $("pkg-note").textContent = d.pkg_note || "";
}
async function load() {
  const d = await api("api/state");
  if (d.ok) render(d); else $("svc-state").textContent = "加载失败：" + (d.message || "");
}
$("cp-doh").onclick = (e) => copyOf($("doh-url"), e.currentTarget);
$("cp-dot").onclick = (e) => copyOf($("dot-host"), e.currentTarget);
$("f-refresh").onclick = async (e) => { const b = e.currentTarget; b.disabled = true; await load(); b.disabled = false; };
$("f-rotate").onclick = async (e) => {
  if (!confirm("重置 DoH 凭据？旧地址立即作废，设备需改填新地址，探针包需重新下载。")) return;
  const b = e.currentTarget; b.disabled = true; b.textContent = "进行中…";
  const d = await api("api/rotate", "POST", {});
  b.disabled = false; b.textContent = "重置 DoH 凭据";
  if (!d.ok) { $("f-msg").textContent = "失败：" + (d.message || ""); return; }
  render(d);
};
load();
</script>
</body>
</html>
"""
