"""Panel page: the user's myweb dnsm.html, adapted for the
manual mapping manager (see build_dnsmap.py)."""

PAGE_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DNS Control Panel</title>
<style>
  :root {
    font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    color-scheme: light;
    --bg: #f5f7fb; --surface: #fff; --text: #172033; --muted: #64748b;
    --line: #e5eaf2; --primary: #3165e7; --primary-hover: #2454cf;
    --green: #0f9d75; --red: #df5252; --gray: #c5cedb;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); }
  .container { max-width: 1540px; padding: 32px 24px 64px; margin: auto; }
  header { display: flex; justify-content: space-between; align-items: flex-end; gap: 18px; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 25px; letter-spacing: -.5px; }
  .sub { margin: 8px 0 0; color: var(--muted); font-size: 14px; }
  .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  button { border: 0; border-radius: 9px; cursor: pointer; transition: background .16s, opacity .16s; font: inherit; }
  button:focus-visible, input:focus-visible { outline: 3px solid #9db7ff; outline-offset: 2px; }
  button:disabled { cursor: not-allowed; opacity: .5; }
  .button { padding: 10px 17px; font-size: 14px; font-weight: 650; }
  .button.primary { background: var(--primary); color: #fff; }
  .button.primary:hover:not(:disabled) { background: var(--primary-hover); }
  .button.light { color: #29405f; background: #e7ecf6; }
  .button.light:hover:not(:disabled) { background: #dbe3f0; }
  .stats { display: flex; flex-wrap: wrap; gap: 12px; margin: 24px 0 15px; }
  .stat { background: var(--surface); padding: 14px 19px; border: 1px solid var(--line);
          border-radius: 11px; min-width: 165px; }
  .stat strong { display: block; font-size: 23px; font-variant-numeric: tabular-nums; }
  .stat span { color: var(--muted); font-size: 13px; }
  .table-card { background: var(--surface); border: 1px solid var(--line);
                border-radius: 13px; overflow: hidden; box-shadow: 0 3px 18px #1a325005; }
  .table-scroll { overflow-x: auto; }
  table { width: 100%; min-width: 1170px; border-collapse: collapse; text-align: left; }
  th { background: #f8fafd; color: #52647d; font-size: 12px; font-weight: 700;
       white-space: nowrap; letter-spacing: .25px; }
  td { font-size: 13px; }
  th, td { padding: 17px 14px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  tbody tr:last-child td { border-bottom: 0; }
  tbody tr:hover { background: #fafcff; }
  .host-name { font-weight: 750; font-size: 14px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; font-size: 12px; }
  .wrap { overflow-wrap: anywhere; }
  .muted { color: var(--muted); }
  .address { display: flex; align-items: center; gap: 9px; min-width: 0; }
  .address .content { min-width: 0; overflow-wrap: anywhere; }
  .detail { margin: 5px 0 0 21px; color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
  .light { display: inline-block; width: 10px; height: 10px; border-radius: 50%; flex: 0 0 10px; background: var(--gray); }
  .light.ok { background: var(--green); box-shadow: 0 0 0 3px #e5f8f0; }
  .light.err { background: var(--red); box-shadow: 0 0 0 3px #fff0f0; }
  .light.pending { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .4; } }
  .actions { display: flex; gap: 7px; align-items: center; }
  .actions input { width: 205px; max-width: 205px; height: 34px; padding: 0 9px;
                   border: 1px solid #d4dce8; background: white; border-radius: 7px;
                   font: 12px ui-monospace, SFMono-Regular, Consolas, monospace; }
  .small-btn { padding: 8px 10px; white-space: nowrap; font-size: 12px; font-weight: 650; }
  .small-btn.auto { background: #e1f6ee; color: #087b59; }
  .small-btn.auto:hover:not(:disabled) { background: #d0efe3; }
  .small-btn.update { background: #e5edff; color: #2758c1; }
  .small-btn.update:hover:not(:disabled) { background: #d5e2ff; }
  .empty { text-align: center; color: var(--muted); padding: 55px 16px; }
  .footer { display: flex; gap: 16px; flex-wrap: wrap; justify-content: space-between;
            margin-top: 15px; color: var(--muted); font-size: 12px; }
  .legend { display: flex; gap: 15px; align-items: center; flex-wrap: wrap; }
  .legend span { display: inline-flex; gap: 7px; align-items: center; }
  #notice { position: fixed; right: 22px; bottom: 24px; padding: 14px 18px; border-radius: 10px;
            max-width: min(470px, calc(100vw - 44px)); background: #22334e; color: white;
            box-shadow: 0 10px 35px #11182730; line-height: 1.5; font-size: 13px;
            visibility: hidden; opacity: 0; transition: opacity .2s; z-index: 10; white-space: pre-line; }
  #notice.show { visibility: visible; opacity: 1; }
  #notice.bad { background: #a32938; }
  @media(max-width: 700px) {
    .container { padding: 20px 12px 35px; } h1 { font-size: 21px; }
    .toolbar { width: 100%; } .toolbar .button { flex: 1; }
    .stat { flex: 1; min-width: 95px; padding: 12px; }
  }
</style>

<style>
  #authGate{position:fixed;inset:0;z-index:9999;background:rgba(10,12,16,.93);display:none;align-items:center;justify-content:center;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
  #authGate.show{display:flex}
  #authGate .card{width:min(92vw,360px);background:#161a22;border:1px solid #232a37;border-radius:14px;padding:22px 20px;display:flex;flex-direction:column;gap:10px;color:#e6e8ee}
  #authGate h2{margin:0 0 4px;font-size:16px}
  #authGate p{margin:0;font-size:12px;color:#8b93a7;line-height:1.5}
  #authGate .hint-row{font-size:14px}
  #authGate .hint-row b{color:#ffd479;font-size:16px}
  #authGate input{background:#0f1116;border:1px solid #232a37;color:inherit;padding:10px 12px;border-radius:10px;font:inherit;outline:none;width:100%;box-sizing:border-box}
  #authGate input:focus{border-color:#3d6ea8}
  #authGate .btn-row{display:flex;flex-wrap:wrap;gap:8px}
  #authGate .word-btn{flex:1 1 30%;background:#0f1116;color:#e6e8ee;border:1px solid #2b3444;border-radius:10px;padding:9px 4px;font:inherit;cursor:pointer}
  #authGate .word-btn.sel{border-color:#2f6fd0;background:#1b2a44;color:#fff}
  #authGate #chGo{background:#2f6fd0;color:#fff;border:0;border-radius:10px;padding:10px;font:inherit;cursor:pointer}
  #authGate .err{color:#ff8b8b;font-size:12px;min-height:15px}
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>DNS Control Panel</h1>
      <p class="sub">域名与 IP 映射管理 · 公网 DNS 指向核对 · hosts 临时指向导出（映射只保存在本机记录，不会推送到任何 DNS 服务商）</p>
    </div>
    <div class="toolbar">
      <button id="addHostBtn" type="button" class="button primary">添加主机</button>
      <button id="exportHosts" type="button" class="button light">导出 hosts</button>
      <button id="refresh" type="button" class="button light">刷新状态</button>
      <button id="hostedBtn" type="button" class="button light">托管设置</button>
    </div>
  </header>
  <section class="stats" aria-label="状态概览">
    <div class="stat"><strong id="countHosts">—</strong><span>主机总数</span></div>
    <div class="stat"><strong id="countOnline">—</strong><span>IPv6 已记录</span></div>
    <div class="stat"><strong id="countDns">—</strong><span>IPv4 / IPv6 DNS 均正确</span></div>
  </section>
  <div class="table-card">
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>主机</th><th>IPv4（局域网）</th><th>IPv4 域名（DNS）</th>
            <th>IPv6 域名（DNS）</th><th>IPv6（记录）</th><th>最后上报 / 更新</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="tb"><tr><td colspan="7" class="empty">正在加载主机配置…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div id="addPanel" class="table-card" style="display:none; padding:14px 16px; margin-bottom:14px;">
    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
      <input id="addName" placeholder="主机名（如 raspi）" autocomplete="off">
      <input id="addLan" placeholder="局域网 IPv4（如 192.168.31.50）" autocomplete="off">
      <input id="addV6" placeholder="公网 IPv6（可空）" size="36" autocomplete="off">
      <input id="addNote" placeholder="备注（可空）" autocomplete="off">
      <button id="addSave" type="button" class="button primary">保存</button>
      <button id="addCancel" type="button" class="button light">取消</button>
    </div>
    <p class="sub" style="margin:8px 0 0;">域名按约定生成：主机名.su7.dpdns.org 记录 IPv6，主机名4.su7.dpdns.org 记录 IPv4。</p>
  </div>
  <div id="exportPanel" class="table-card" style="display:none; padding:14px 16px; margin-bottom:14px;">
    <textarea id="hostsText" rows="10" style="width:100%; box-sizing:border-box;" readonly></textarea>
    <div style="margin-top:8px; display:flex; gap:8px;">
      <button id="copyHosts" type="button" class="button primary">复制</button>
      <button id="closeExport" type="button" class="button light">关闭</button>
    </div>
    <p class="sub" style="margin:8px 0 0;">把以上内容粘贴到某台机器的 hosts 文件，即可在那台机器上临时指向，不改动 DNS。</p>
  </div>
  <div id="hostedPanel" class="table-card" style="display:none; padding:14px 16px; margin-bottom:14px;">
    <h3 style="margin:2px 0 8px;">zin7.de 托管分册（Cloudflare 控制面）</h3>
    <p class="sub" style="margin:0 0 10px;">提供商令牌只保存在服务器、只写不回显；智能体经 /dm-api 令牌通道仅可新增 / 修正 zin7.de 的 A / AAAA 记录，删除与 NS 变更不开放。</p>
    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:10px;">
      <input id="cfToken" type="password" placeholder="粘贴新的 CF API Token（留空不改）" size="44" autocomplete="off">
      <button id="cfSave" type="button" class="button primary">保存令牌</button>
      <button id="cfClear" type="button" class="button light">清除令牌</button>
      <span id="credState" class="sub">凭据状态加载中…</span>
    </div>
    <div id="hostedList" class="sub" style="margin-bottom:10px;">托管记录加载中…</div>
    <div id="auditList" class="sub">审计加载中…</div>
  </div>
  <div class="footer">
    <div class="legend">
      <span><i class="light ok" aria-hidden="true"></i> 正常 / 指向正确</span>
      <span><i class="light err" aria-hidden="true"></i> 异常 / 不匹配</span>
      <span><i class="light" aria-hidden="true"></i> 未检测 / 无记录</span>
    </div>
    <span id="lastChecked">尚未完成检测</span>
  </div>
</div>
<div id="notice" role="status" aria-live="polite"></div>
<script>
(() => {
  'use strict';
  const tb = document.getElementById('tb');
  const refreshButton = document.getElementById('refresh');
  const rows = new Map();
  let hosts = {};
  let refreshing = false;
  let refreshAgain = false;
  let working = 0;
  let notifyTimer;
  let token = '';
  try { token = sessionStorage.getItem('dns_panel_token') || ''; } catch (_) {}

  const elem = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };

  function notify(message, bad = false) {
    const box = document.getElementById('notice');
    box.textContent = message;
    box.className = bad ? 'show bad' : 'show';
    clearTimeout(notifyTimer);
    notifyTimer = setTimeout(() => box.className = '', bad ? 6500 : 4200);
  }

  async function api(url, { method = 'GET', data = null, timeout = 20000 } = {}) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);
      try {
        const headers = { Accept: 'application/json' };
        if (data !== null) headers['Content-Type'] = 'application/json';
        const response = await fetch((window.API_BASE || '') + url, {
          method, headers, credentials: 'same-origin', cache: 'no-store',
          body: data === null ? undefined : JSON.stringify(data), signal: controller.signal,
        });
        const payload = await response.json().catch(() => ({}));
        
        if (response.status === 401) throw new Error('登录已过期，请刷新页面后重新登录');
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        return payload;
      } catch (error) {
        if (error.name === 'AbortError') throw new Error('请求超时，请稍后重试');
        throw error;
      } finally {
        clearTimeout(timer);
      }
    }
    throw new Error('认证失败');
  }

  function light(title) {
    const node = elem('span', 'light pending');
    node.title = `${title}：检测中`;
    node.setAttribute('role', 'img');
    node.setAttribute('aria-label', node.title);
    return node;
  }

  function setLight(node, value, title) {
    const state = value === null || value === undefined ? '未检测' : (value ? '正常' : '异常');
    node.className = 'light' + (value === null || value === undefined ? '' : value ? ' ok' : ' err');
    node.title = `${title}：${state}`;
    node.setAttribute('aria-label', node.title);
  }

  function addressCell(row, address, title, extra = '') {
    const td = elem('td');
    const box = elem('div', 'address');
    const dot = light(title);
    const content = elem('span', 'content mono wrap', address || '—');
    box.append(dot, content);
    td.append(box);
    if (extra) td.append(elem('div', 'detail', extra));
    row.append(td);
    return { dot, content, td };
  }

  function timeText(unix) {
    if (!unix || !Number.isFinite(Number(unix))) return '—';
    return new Date(Number(unix) * 1000).toLocaleString('zh-CN', { hour12: false });
  }

  function buildRow(name, host, priorValue = '') {
    const tr = elem('tr');
    const nameCell = elem('td');
    nameCell.append(elem('strong', 'host-name', name));
    tr.append(nameCell);
    const v4 = addressCell(tr, host.lan_ipv4, 'IPv4 Ping');
    const a = addressCell(tr, host.ipv4dns, 'IPv4 DNS 指向正确');
    const aaaa = addressCell(tr, host.ipv6dns, 'IPv6 DNS 指向正确');
    const v6 = addressCell(tr, host.last_ipv6 || '未记录', 'IPv6 Ping');
    const timeCell = elem('td', 'muted', timeText(host.last_seen));
    tr.append(timeCell);
    const actionCell = elem('td');
    const actions = elem('div', 'actions');
    const input = elem('input');
    input.type = 'text';
    input.placeholder = '输入公网 IPv6';
    input.value = priorValue;
    input.autocomplete = 'off';
    input.setAttribute('aria-label', `${name} 的 IPv6`);
    autoBtn.type = 'button';
    const updateBtn = elem('button', 'small-btn update', '更新');
    updateBtn.type = 'button';
    updateBtn.addEventListener('click', () => runOne(name, host.lan_ipv4, updateBtn, false, input));
    const delBtn = elem('button', 'small-btn', '删除');
    delBtn.type = 'button';
    delBtn.addEventListener('click', () => removeHost(name));
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); updateBtn.click(); }
    });
    actions.append(input, updateBtn, delBtn);
    actionCell.append(actions);
    tr.append(actionCell);
    return { tr, v4, a, aaaa, v6, timeCell, input, updateBtn,
             lan: host.lan_ipv4, aName: host.ipv4dns, aaaaName: host.ipv6dns };
  }

  function showHosts(data) {
    const entries = Object.entries(data);
    // 配置未变时直接复用现有 DOM，避免自动刷新打断正在输入的 IPv6。
    const unchanged = entries.length === rows.size && entries.every(([name, host]) => {
      const row = rows.get(name);
      return row && row.lan === host.lan_ipv4 && row.aName === host.ipv4dns
        && row.aaaaName === host.ipv6dns;
    });
    if (unchanged) return;
    const drafts = new Map([...rows].map(([name, row]) => [name, row.input.value]));
    rows.clear();
    const frag = document.createDocumentFragment();
    for (const [name, host] of Object.entries(data)) {
      const row = buildRow(name, host, drafts.get(name) || '');
      rows.set(name, row);
      frag.append(row.tr);
    }
    if (!rows.size) {
      const tr = elem('tr');
      const td = elem('td', 'empty', '暂无主机');
      td.colSpan = 7;
      tr.append(td); frag.append(tr);
    }
    tb.replaceChildren(frag);
    document.getElementById('countHosts').textContent = rows.size;
    document.getElementById('countOnline').textContent = '—';
    document.getElementById('countDns').textContent = '—';
  }

  function showStatus(statuses) {
    let online = 0, dnsBoth = 0;
    for (const [name, row] of rows) {
      const h = statuses[name];
      if (!h) {
        [row.v4, row.a, row.aaaa, row.v6].forEach(c => setLight(c.dot, null, '未收到状态'));
        continue;
      }
      setLight(row.v4.dot, h.ipv4_ok, '局域网连通性（云端不可测）');
      setLight(row.a.dot, h.dns_ok, 'IPv4 DNS 指向正确');
      setLight(row.aaaa.dot, h.ipv6_dns_ok, 'IPv6 DNS 指向正确');
      setLight(row.v6.dot, h.ipv6_ok, 'IPv6 可达性（云端不可测）');
      row.v6.content.textContent = h.last_ipv6 || '未记录';
      row.timeCell.textContent = timeText(h.last_seen);
      row.a.dot.title += `；当前解析：${(h.ipv4_resolved || []).join(', ') || '无'}`;
      row.aaaa.dot.title += `；当前解析：${(h.ipv6_resolved || []).join(', ') || '无'}`;
      if (h.last_ipv6) online++;
      if (h.dns_ok === true && h.ipv6_dns_ok === true) dnsBoth++;
    }
    document.getElementById('countOnline').textContent = online;
    document.getElementById('countDns').textContent = dnsBoth;
    document.getElementById('lastChecked').textContent =
      `检测时间：${new Date().toLocaleTimeString('zh-CN', { hour12: false })} · 每 60 秒自动刷新`;
  }

  async function load() {
    if (refreshing) { refreshAgain = true; return; }
    refreshing = true;
    refreshButton.disabled = true;
    refreshButton.textContent = '检测中…';
    try {
      const config = await api('/dns/api/hosts');
      hosts = config;
      showHosts(hosts); // 先展示配置，不阻塞于 Ping / DNS
      try {
        const statuses = await api('/dns/api/status', { timeout: 30000 });
        showStatus(statuses); // 仅更新状态，保留输入内容
      } catch (error) {
        for (const row of rows.values()) {
          [row.v4, row.a, row.aaaa, row.v6].forEach(c => setLight(c.dot, null, '检测失败'));
        }
        notify('主机已加载，但状态检测失败：' + error.message, true);
      }
    } catch (error) {
      notify('加载失败：' + error.message, true);
      if (!rows.size) {
        const tr = elem('tr'); const td = elem('td', 'empty', '加载失败，请点击刷新重试');
        td.colSpan = 7; tr.append(td); tb.replaceChildren(tr);
      }
    } finally {
      refreshing = false;
      refreshButton.disabled = false;
      refreshButton.textContent = '刷新状态';
      if (refreshAgain) { refreshAgain = false; void load(); }
    }
  }

  async function runOne(name, lan, button, automatic, input) {
    const ipv6 = input.value.trim();
    if (!ipv6) { notify('请先输入 IPv6 地址', true); input.focus(); return; }
    button.disabled = true;
    const label = button.textContent;
    button.textContent = '处理中…';
    working++;
    try {
      const url = '/dns/manual_update';
      const payload = { lan_ipv4: lan, ipv6 };
      const result = await api(url, { method: 'POST', data: payload, timeout: 40000 });
      notify(`${name} 更新成功：${result.ipv6}`);
      input.value = '';
      await load();
    } catch (error) {
      notify(`${name} 更新失败：${error.message}`, true);
    } finally {
      working--;
      button.textContent = label;
      button.disabled = false;
    }
  }



  async function removeHost(name) {
    if (!window.confirm('确定删除主机 ' + name + '？（只删除本页的映射记录，不影响 DNS 本身）')) return;
    try {
      await api('/dns/api/hosts/' + encodeURIComponent(name), { method: 'DELETE' });
      notify(name + ' 已删除');
      await load();
    } catch (error) { notify('删除失败：' + error.message, true); }
  }

  const addPanel = document.getElementById('addPanel');
  document.getElementById('addHostBtn').addEventListener('click', () => {
    addPanel.style.display = addPanel.style.display === 'none' ? '' : 'none';
  });
  document.getElementById('addCancel').addEventListener('click', () => { addPanel.style.display = 'none'; });
  document.getElementById('addSave').addEventListener('click', async () => {
    const payload = {
      name: document.getElementById('addName').value.trim(),
      lan_ipv4: document.getElementById('addLan').value.trim(),
      ipv6: document.getElementById('addV6').value.trim(),
      note: document.getElementById('addNote').value.trim(),
    };
    try {
      await api('/dns/api/hosts', { method: 'POST', data: payload });
      notify('主机已添加：' + payload.name);
      addPanel.style.display = 'none';
      await load();
    } catch (error) { notify('添加失败：' + error.message, true); }
  });

  const exportPanel = document.getElementById('exportPanel');
  document.getElementById('exportHosts').addEventListener('click', async () => {
    try {
      const data = await api('/dns/api/export');
      document.getElementById('hostsText').value = data.content;
      exportPanel.style.display = '';
    } catch (error) { notify('导出失败：' + error.message, true); }
  });
  document.getElementById('closeExport').addEventListener('click', () => { exportPanel.style.display = 'none'; });
  document.getElementById('copyHosts').addEventListener('click', async () => {
    const ta = document.getElementById('hostsText');
    ta.select();
    try { await navigator.clipboard.writeText(ta.value); notify('hosts 内容已复制'); }
    catch (_) { document.execCommand('copy'); notify('hosts 内容已复制'); }
  });


  const hostedPanel = document.getElementById('hostedPanel');
  async function loadHosted() {
    try {
      const cred = await api('/dns/api/credentials');
      document.getElementById('credState').textContent =
        'CF 令牌：' + (cred.cf_token_configured ? '已配置' : '未配置') +
        ' · 通道令牌：' + (cred.dm_token_configured ? '已配置' : '未配置') +
        ' · zone：' + cred.zone;
      const hosted = await api('/dns/api/hosted');
      const hl = document.getElementById('hostedList');
      if (!hosted.records.length) { hl.textContent = '托管记录：暂无'; }
      else {
        hl.innerHTML = '托管记录（' + hosted.zone + '）：<br>' + hosted.records.map(r =>
          r.name + ' ' + r.type + ' → ' + r.value + '（实测 ' +
          (r.resolved && r.resolved.length ? r.resolved.join(', ') : '无解析') +
          ' · ' + (r.ok ? '一致' : '未生效/不一致') + '）').join('<br>');
      }
      const aud = await api('/dns/api/audit');
      const al = document.getElementById('auditList');
      const rows = (aud.audit || []).slice(-8).reverse();
      al.innerHTML = rows.length
        ? '最近审计：<br>' + rows.map(a => a.ts + ' ' + a.action + ' ' + a.name + ' ' + a.type +
            ' ' + (a.old || '∅') + ' → ' + (a.new || '∅') + ' · ' + a.result).join('<br>')
        : '最近审计：暂无';
    } catch (error) { notify('托管信息加载失败：' + error.message, true); }
  }
  document.getElementById('hostedBtn').addEventListener('click', () => {
    const show = hostedPanel.style.display === 'none';
    hostedPanel.style.display = show ? '' : 'none';
    if (show) void loadHosted();
  });
  document.getElementById('cfSave').addEventListener('click', async () => {
    const v = document.getElementById('cfToken').value.trim();
    if (!v) { notify('请先粘贴令牌', true); return; }
    try {
      await api('/dns/api/credentials', { method: 'POST', data: { cf_api_token: v } });
      document.getElementById('cfToken').value = '';
      notify('CF 令牌已保存（不回显）');
      await loadHosted();
    } catch (error) { notify('保存失败：' + error.message, true); }
  });
  document.getElementById('cfClear').addEventListener('click', async () => {
    if (!window.confirm('清除已保存的 CF 令牌？清除后自动通道将停止工作。')) return;
    try {
      await api('/dns/api/credentials', { method: 'POST', data: { cf_api_token: '' } });
      notify('CF 令牌已清除');
      await loadHosted();
    } catch (error) { notify('清除失败：' + error.message, true); }
  });

  refreshButton.addEventListener('click', load);
  void load();
  setInterval(() => { if (!refreshing && !working && !document.hidden) void load(); }, 60000);
})();
</script>

<div id="authGate">
  <div class="card">
    <h2>域名指向 · 验证</h2>
    <p>弱服务独立验证：看提示词，想起同一行的三个词，输入中间的词，再点中最后一个词。有控制台会话时刷新即可直接进入，不必过这一关。</p>
    <div class="hint-row">提示：<b id="chHint">…</b></div>
    <input id="chInput" placeholder="输入中间的词" maxlength="64" autocomplete="off">
    <div class="btn-row" id="chButtons"></div>
    <div class="err" id="authErr"></div>
    <button id="chGo" type="button">进入</button>
  </div>
</div>
<script>
(function(){
  "use strict";
  var BASE = window.API_BASE || location.pathname.replace(/\/[^/]*$/, "");
  var cid = null, selToken = null;
  function loadChallenge(){
    document.getElementById("authErr").textContent = "";
    document.getElementById("chInput").value = "";
    selToken = null;
    fetch(BASE + "/api/challenge", {cache:"no-store"})
      .then(function(r){ return r.json(); })
      .then(function(d){
        cid = d.challenge_id;
        document.getElementById("chHint").textContent = d.hint;
        var box = document.getElementById("chButtons");
        box.innerHTML = "";
        (d.buttons || []).forEach(function(b){
          var btn = document.createElement("button");
          btn.type = "button"; btn.className = "word-btn"; btn.textContent = b.label;
          btn.addEventListener("click", function(){
            selToken = b.token;
            Array.prototype.forEach.call(box.children, function(c){ c.classList.remove("sel"); });
            btn.classList.add("sel");
          });
          box.appendChild(btn);
        });
      })
      .catch(function(){ document.getElementById("authErr").textContent = "挑战加载失败，请刷新重试"; });
  }
  function showGate(){
    document.getElementById("authGate").classList.add("show");
    loadChallenge();
  }
  fetch(BASE + "/api/session", {cache:"no-store"})
    .then(function(r){ return r.json(); })
    .then(function(s){ if(!s.authenticated) showGate(); })
    .catch(function(){ showGate(); });
  document.getElementById("chGo").addEventListener("click", function(){
    if(!cid){ return; }
    if(!selToken){ document.getElementById("authErr").textContent = "请先点中正确的词按钮"; return; }
    fetch(BASE + "/api/challenge", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({challenge_id: cid, token: selToken,
                            input: document.getElementById("chInput").value})
    }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
      .then(function(res){
        if(res.ok){ location.reload(); }
        else{
          document.getElementById("authErr").textContent = (res.d && res.d.message) || "验证失败，请重试";
          loadChallenge();
        }
      })
      .catch(function(){ document.getElementById("authErr").textContent = "网络错误，请重试"; });
  });
  document.getElementById("chInput").addEventListener("keydown", function(e){
    if(e.key === "Enter"){ e.preventDefault(); document.getElementById("chGo").click(); }
  });
})();
</script>
</body>
</html>
'''
