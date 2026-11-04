'use strict';

window.siteSettingsUI = (() => {
  const el = (id) => document.getElementById(id);
  let username = null;
  let generation = 0;
  let routes = [];
  let busy = false;
  let writable = false;
  let dirty = false;

  function message(text = '', kind = '') {
    el('settings-message').textContent = text;
    el('settings-message').className = 'message' + (kind ? ' ' + kind : '');
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function button(text, action) {
    const control = node('button', 'btn btn-ghost btn-sm', text);
    control.type = 'button';
    control.addEventListener('click', action);
    return control;
  }

  function input(parent, name, label, value = '', {type = 'text', hint = '', placeholder = '', choices} = {}) {
    const wrapper = node('label', type === 'checkbox' ? 'settings-check' : 'settings-field');
    const control = node(choices ? 'select' : type === 'textarea' ? 'textarea' : 'input');
    control.dataset.field = name;
    if (control.tagName === 'INPUT') control.type = type;
    if (choices) {
      for (const [key, text] of choices) {
        const option = node('option', '', text);
        option.value = key;
        control.append(option);
      }
    }
    if (type === 'checkbox') control.checked = Boolean(value);
    else control.value = value;
    if (placeholder) control.placeholder = placeholder;
    control.autocomplete = 'off';
    if (type === 'textarea') { control.rows = 3; control.spellcheck = false; }
    if (type === 'password') { control.maxLength = 4096; control.spellcheck = false; }
    if (['name', 'hosts', 'endpoint', 'url', 'url_parameter', 'response_field'].includes(name)) {
      control.maxLength = name === 'hosts' ? 5000 : ['endpoint', 'url'].includes(name) ? 2048 : name === 'name' ? 60 : 100;
    }
    if (type === 'number') { control.min = '5'; control.max = '300'; control.step = '1'; }
    if (type === 'checkbox') wrapper.append(control, node('span', '', label));
    else wrapper.append(node('span', '', label), control);
    if (hint) wrapper.append(node('span', 'hint', hint));
    parent.append(wrapper);
    return control;
  }

  function field(editor, name) { return editor.querySelector(`[data-field="${name}"]`); }

  function shortcutEditor(link = {}) {
    const editor = node('div', 'shortcut-editor');
    editor.dataset.id = link.id || crypto.randomUUID();
    input(editor, 'name', '名称', link.name || '', {placeholder: '例如：YouTube'});
    input(editor, 'url', '跳转网址', link.url || '', {type: 'url', placeholder: 'https://www.youtube.com/'});
    editor.append(button('删除', () => { editor.remove(); markDirty(); }));
    el('shortcut-editors').append(editor);
    return editor;
  }

  function routeEditor(route = {}) {
    const editor = node('details', 'route-editor');
    editor.dataset.id = route.id || crypto.randomUUID();
    editor.open = !route.id;
    const summary = node('summary');
    const label = node('span', '', route.name || '新网站规则');
    const badge = node('span', 'hint', route.enabled === false ? '未启用' : '已启用');
    summary.append(label, badge, node('span', 'chev', '▾'));
    const body = node('div', 'route-body');
    const grid = node('div', 'settings-grid');
    const hosts = input(grid, 'hosts', '识别的网站域名', (route.hosts || []).join(', '), {
      placeholder: 'youtube.com, *.youtube.com, youtu.be', hint: '逗号或空格分隔；*.example.com 只匹配子域名。'});
    const browser = (route.transport || 'browser') === 'browser';
    const endpoint = input(grid, 'endpoint', '局域网 IP:端口', browser ? (route.endpoint || '').replace(/\/extract$/, '') : route.endpoint || '', {
      placeholder: '192.168.1.10:8765'});
    body.append(grid);
    const enabled = input(body, 'enabled', '启用这条规则', route.enabled !== false, {type: 'checkbox'});
    enabled.addEventListener('change', () => { badge.textContent = enabled.checked ? '已启用' : '未启用'; });
    const advanced = node('details', 'route-security');
    advanced.append(node('summary', '', '更多选项与其他接口适配'));
    const options = node('div', 'settings-grid');
    const name = input(options, 'name', '显示名称（可留空）', route.name || '', {placeholder: '自动使用网站名称'});
    const transport = input(options, 'transport', '请求位置', route.transport || 'browser', {
      choices: [['browser', '浏览器直连局域网（默认）'], ['server', '云端转发到公网 HTTPS 服务']]});
    const timeout = input(options, 'timeout', '最长等待秒数', route.timeout || (browser ? 120 : 40), {type: 'number'});
    advanced.append(options);
    const server = node('div', 'settings-grid server-options');
    input(server, 'method', '请求方式', route.method || 'POST', {
      choices: [['POST', 'POST · JSON 请求体'], ['GET', 'GET · 查询参数']]});
    input(server, 'url_parameter', '链接参数名', route.url_parameter || 'url', {placeholder: 'url'});
    input(server, 'response_field', 'JSON 返回文本字段（可留空）', route.response_field || '', {
      placeholder: '例如 data.text', hint: '留空时自动识别 content、text 或 transcript；纯文本直接展示。'});
    advanced.append(server);
    function updateMode() {
      const local = transport.value === 'browser';
      server.hidden = local;
      endpoint.type = local ? 'text' : 'url';
      endpoint.placeholder = local ? '192.168.1.10:8765' : 'https://processor.example.com/extract';
      endpoint.closest('label').querySelector('span').textContent = local ? '局域网 IP:端口' : '公网 HTTPS 完整接口';
      timeout.max = local ? '300' : '60';
      if (!local && Number(timeout.value) > 60) timeout.value = '40';
    }
    function updateLabel() { label.textContent = name.value.trim() || hosts.value.split(/[\s,，;；]+/)[0] || '新网站规则'; }
    name.addEventListener('input', updateLabel);
    hosts.addEventListener('input', updateLabel);
    transport.addEventListener('change', updateMode);
    updateMode();
    body.append(advanced);
    const actions = node('div', 'settings-actions');
    actions.append(button('上移', () => { if (editor.previousElementSibling) editor.previousElementSibling.before(editor); markDirty(); }),
      button('下移', () => { if (editor.nextElementSibling) editor.nextElementSibling.after(editor); markDirty(); }),
      button('删除规则', () => { editor.remove(); markDirty(); }));
    body.append(actions);
    editor.append(summary, body);
    el('route-editors').append(editor);
    return editor;
  }

  function renderShortcuts(links) {
    el('shortcut-links').replaceChildren();
    for (const link of links) {
      let parsed;
      try { parsed = new URL(link.url); } catch { continue; }
      if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) continue;
      const anchor = node('a', 'shortcut-link', link.name + ' ↗');
      anchor.href = parsed.href;
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      el('shortcut-links').append(anchor);
    }
  }

  function markDirty() {
    dirty = true;
    el('settings-badge').textContent = '有未保存修改';
    message('修改后请点击“保存链接与规则”，随后其他浏览器也可使用。', 'pending');
  }

  function controls() { el('settings-fields').disabled = busy || !writable; }

  function render(data) {
    routes = data.routes || [];
    el('shortcut-editors').replaceChildren();
    el('route-editors').replaceChildren();
    for (const link of data.shortcuts || []) shortcutEditor(link);
    for (const route of routes) routeEditor(route);
    renderShortcuts(data.shortcuts || []);
    writable = data.storage_configured;
    dirty = false;
    el('settings-badge').textContent = `${(data.shortcuts || []).length} 个链接 · ${routes.filter(route => route.enabled).length} 条启用规则`;
    el('network-hint').textContent = '浏览器需能访问节点所在局域网。Chrome 142+ 首次连接会请求本地网络访问许可；节点同时需要允许本网站的 CORS 请求。';
    if (!writable) message('在线设置存储尚未初始化，请先完成部署配置。', 'warning');
    renderLinks();
    const current = JSON.parse(document.querySelector('meta[name="routing-origins"]').content);
    if (JSON.stringify(current) !== JSON.stringify(data.browser_origins || [])) {
      try { sessionStorage.setItem('routing-input', el('url').value); } catch {}
      message('已保存，正在刷新页面以应用局域网连接权限…', 'pending');
      location.reload();
    }
  }

  function failure(error) {
    if (error.status === 401) {
      showAccount(null);
      el('login-message').textContent = '登录已过期，请重新登录。';
    } else { message(error.message, 'warning'); }
  }

  async function load() {
    if (!username || busy) return;
    const request = ++generation;
    busy = true;
    controls();
    message('正在读取链接与分流设置…', 'pending');
    try {
      const data = await api('/api/settings');
      if (request !== generation || !username) return;
      render(data);
      if (writable) message();
    } catch (error) { if (request === generation) failure(error); }
    finally { if (request === generation) { busy = false; controls(); } }
  }

  function accountChanged(user) {
    username = user;
    generation += 1;
    busy = false;
    writable = false;
    dirty = false;
    routes = [];
    el('shortcut-editors').replaceChildren();
    el('route-editors').replaceChildren();
    renderShortcuts([]);
    message();
    controls();
    if (user) load();
  }

  function classify(parsed) {
    const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
    const rule = routeFor(host);
    const youtube = ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be', 'music.youtube.com'].includes(host);
    if (!rule && !youtube) return null;
    parsed.hash = '';
    return {url: parsed.href, kind: rule?.name || 'YouTube', tail: (host + parsed.pathname).slice(0, 100)};
  }

  function routeFor(host) {
    return routes.find(item => item.enabled && item.hosts.some(pattern => pattern.startsWith('*.')
      ? host.endsWith(pattern.slice(1)) && host !== pattern.slice(2) : host === pattern));
  }

  async function boundedText(response) {
    const limit = 2 * 1024 * 1024;
    if (!response.body) {
      const data = await response.arrayBuffer();
      if (data.byteLength > limit) throw new Error('节点返回内容超过 2 MiB。');
      return new TextDecoder().decode(data);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let size = 0;
    const chunks = [];
    try {
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > limit) { await reader.cancel(); throw new Error('节点返回内容超过 2 MiB。'); }
        chunks.push(decoder.decode(value, {stream: true}));
      }
      chunks.push(decoder.decode());
      return chunks.join('');
    } finally { reader.releaseLock(); }
  }

  async function extract(url, format, options, signal) {
    const parsed = new URL(url);
    const rule = routeFor(parsed.hostname.toLowerCase().replace(/\.$/, ''));
    if (!rule || rule.transport === 'server') return api('/api/extract', {url, format, ...options}, signal);
    if (url.length > 8000) throw new Error('链接过长，请使用原始网址或短链接。');
    const session = await api('/api/session', undefined, signal);
    if (!session.authenticated) {
      const error = new Error('登录已过期，请重新登录。');
      error.status = 401;
      error.code = 'unauthorized';
      throw error;
    }
    const controller = new AbortController();
    const abort = () => controller.abort();
    let timedOut = false;
    if (signal?.aborted) controller.abort();
    signal?.addEventListener('abort', abort, {once: true});
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, (rule.timeout || 120) * 1000);
    try {
      const response = await fetch(rule.endpoint, {method: 'POST', mode: 'cors', credentials: 'omit',
        redirect: 'error', cache: 'no-store', targetAddressSpace: 'local', signal: controller.signal,
        headers: {'Content-Type': 'application/json', 'Accept': 'text/plain, application/json'}, body: JSON.stringify({url})});
      const raw = await boundedText(response);
      const type = (response.headers.get('Content-Type') || '').toLowerCase();
      let text = raw;
      let metadata = {};
      if (type.includes('json')) {
        try { metadata = JSON.parse(raw); } catch { throw new Error('节点返回了无效 JSON。'); }
        if (typeof metadata === 'string') metadata = {text: metadata};
        if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) throw new Error('节点 JSON 需要包含 text 或 content 文本字段。');
        if (!response.ok || metadata.ok === false || metadata.success === false) {
          throw new Error(typeof metadata.error === 'string' ? metadata.error.slice(0, 400) : `节点处理失败（${response.status}）。`);
        }
        text = [metadata.content, metadata.text, metadata.transcript, metadata.data?.text,
          metadata.data?.content].find(value => typeof value === 'string') || '';
      }
      if (!response.ok) throw new Error(`节点返回 HTTP ${response.status}。${raw.slice(0, 300)}`);
      if (type && !type.includes('json') && !type.includes('text/plain') && !type.includes('text/markdown')) {
        throw new Error('节点应返回纯文本，请按接口规范配置 /extract。');
      }
      if (!text.trim()) throw new Error('节点返回了空文本。');
      const doc = {url, kind: 'proxy', title: String(metadata.title || rule.name || parsed.hostname),
        author: String(metadata.author || ''), published: '', source: rule.name, text, markdown: text,
        extra: {routing_rule: rule.id}};
      return {ok: true, ...doc, format, content: format === 'json' ? JSON.stringify(doc, null, 2) : text};
    } catch (error) {
      if (timedOut) throw new Error('局域网节点处理超时，请稍后再试，或在更多选项中延长等待时间。');
      if (error.name === 'TypeError') throw new Error('无法访问局域网节点。请确认设备在同一网络、IP 和端口正确，并允许浏览器访问本地网络；节点需支持本网站的 CORS 请求。');
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }

  el('open-settings').addEventListener('click', () => {
    el('settings-panel').open = true;
    el('settings-panel').scrollIntoView({block: 'start'});
  });
  el('add-shortcut').addEventListener('click', () => { shortcutEditor(); markDirty(); });
  el('add-route').addEventListener('click', () => { routeEditor({enabled: false}); markDirty(); });
  el('youtube-preset').addEventListener('click', () => {
    routeEditor({name: 'YouTube 字幕', hosts: ['youtube.com', '*.youtube.com', 'youtu.be'],
      transport: 'browser', method: 'POST', url_parameter: 'url', timeout: 120, enabled: false});
    markDirty();
    message('已添加 YouTube 预设。填写局域网 IP:端口，勾选启用并保存即可；节点按统一 /extract 接口返回文本。', 'pending');
  });
  el('settings-fields').addEventListener('input', markDirty);
  el('reload-settings').addEventListener('click', load);
  el('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!username || busy || !writable) return;
    let payload;
    try {
      payload = {
        shortcuts: [...el('shortcut-editors').children].map(editor => ({id: editor.dataset.id,
          name: field(editor, 'name').value.trim(), url: field(editor, 'url').value.trim()})),
        routes: [...el('route-editors').children].map(editor => {
          return {id: editor.dataset.id, name: field(editor, 'name').value.trim(),
            hosts: field(editor, 'hosts').value.split(/[\s,，;；]+/).filter(Boolean),
            endpoint: field(editor, 'endpoint').value.trim(), transport: field(editor, 'transport').value,
            method: field(editor, 'method').value,
            url_parameter: field(editor, 'url_parameter').value.trim(),
            response_field: field(editor, 'response_field').value.trim(), timeout: Number(field(editor, 'timeout').value),
            enabled: field(editor, 'enabled').checked};
        })
      };
    } catch { message('设置格式无效，请检查网站和节点地址。', 'warning'); return; }
    const request = ++generation;
    busy = true;
    controls();
    message('正在保存链接与分流规则…', 'pending');
    try {
      const data = await api('/api/settings', payload);
      if (request !== generation || !username) return;
      render(data);
      message('已保存，链接和分流规则已立即生效。', 'success');
    } catch (error) { if (request === generation) failure(error); }
    finally { if (request === generation) { busy = false; controls(); } }
  });
  window.addEventListener('beforeunload', (event) => {
    if (username && dirty) { event.preventDefault(); event.returnValue = ''; }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && username && !dirty && !busy) load();
  });
  return {accountChanged, classify, extract, open: () => el('open-settings').click()};
})();
