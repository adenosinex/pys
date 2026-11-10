'use strict';
const $ = (id) => document.getElementById(id);
let currentResult = null;
let activeUsername = null;
let cookieState = null;
let cookieBusy = false;
let cookieTimer = null;
let cookieGeneration = 0;

async function api(path, body, signal) {
  const options = {credentials: 'same-origin', cache: 'no-store', signal};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json'};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`服务暂时不可用（${response.status}），请稍后重试。`); }
  if (!response.ok) {
    const error = new Error(data.error || '请求失败，请重试。');
    error.status = response.status;
    error.code = data.code;
    throw error;
  }
  return data;
}

function showAccount(username) {
  // No in-app verification: entry is gated by the platform layer, so
  // the workspace is shown directly.
  activeUsername = username || 'platform';
  $('workspace').hidden = false;
  window.siteSettingsUI.accountChanged(activeUsername);
  loadCookieState();
  loadBiliCookieState();
  loadMacCodeState();
}

function cookieMessage(message, kind = '') {
  $('cookie-message').textContent = message;
  $('cookie-message').className = 'message' + (kind ? ' ' + kind : '');
}

function cookieButtons() {
  $('save-cookie').disabled = cookieBusy;
  $('test-cookie').disabled = cookieBusy || (!cookieState?.configured && !$('cookie-input').value.trim());
  $('remove-cookie').disabled = cookieBusy || !cookieState?.configured;
}

function renderCookieState(data) {
  cookieState = data;
  const validation = data.validation || {};
  $('cookie-badge').textContent = !data.configured ? '未配置'
    : validation.valid ? '已保存 · 验证通过' : validation.state === 'unchecked' ? '已保存 · 待测试' : '已保存 · 测试未通过';
  $('cookie-badge').dataset.state = validation.valid ? 'valid' : data.configured ? 'warning' : 'unchecked';
  cookieButtons();
}

function validationMessage(validation, prefix) {
  const size = validation.content_length ? `（${validation.content_length} 字符）` : '';
  const checked = validation.checked_at ? ` 上次测试：${new Date(validation.checked_at).toLocaleString()}` : '';
  return prefix + validation.message + size + checked;
}

function cookieFailure(error) {
  cookieMessage(error.message, 'warning');
}

function syncCookieLink(inputId, linkId, storageKey, persist = true) {
  const value = $(inputId).value.trim();
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || !(url.hostname === 'zhihu.com' || url.hostname.endsWith('.zhihu.com')) || url.username || url.password) throw new Error();
    $(linkId).href = url.href;
    $(linkId).removeAttribute('aria-disabled');
    if (persist) { try { localStorage.setItem(storageKey, value); } catch {} }
  } catch {
    $(linkId).removeAttribute('href');
    $(linkId).setAttribute('aria-disabled', 'true');
  }
}

async function loadCookieState() {
  try {
    const data = await api('/api/cookie');
    if (!activeUsername) return;
    renderCookieState(data);
    if (!$('cookie-input').value.trim()) $('cookie-panel').open = !data.configured;
    let customTest = null;
    try { customTest = localStorage.getItem('zhihu_cookie_test_url'); } catch {}
    if (!customTest && data.test_url) $('cookie-test-url').value = data.test_url;
    syncCookieLink('cookie-test-url', 'cookie-test-link', 'zhihu_cookie_test_url', false);
    if (data.configured) {
      cookieMessage(validationMessage(data.validation, ''), data.validation.valid ? 'success' : 'warning');
      const checked = Date.parse(data.validation.checked_at || '');
      if (!Number.isFinite(checked) || Date.now() - checked > 15 * 60 * 1000) await testCookie(false);
    }
  } catch (error) { cookieFailure(error); }
}

async function testCookie(preview = false) {
  if (cookieBusy || !activeUsername) return;
  const candidate = $('cookie-input').value.trim();
  if (preview && !candidate) return;
  const generation = ++cookieGeneration;
  cookieBusy = true;
  cookieButtons();
  cookieMessage(preview ? '正在自动验证输入的 Cookie…' : '正在使用已保存的 Cookie 测试内容访问…', 'pending');
  try {
    const payload = {test_url: $('cookie-test-url').value.trim()};
    if (preview) payload.cookie = candidate;
    const data = await api('/api/cookie/test', payload);
    if (generation !== cookieGeneration || !activeUsername) return;
    if (!preview && cookieState) renderCookieState({...cookieState, validation: data.validation});
    cookieMessage(validationMessage(data.validation, preview ? '输入尚未保存。' : ''), data.validation.valid ? 'success' : 'warning');
  } catch (error) { if (generation === cookieGeneration) cookieFailure(error); }
  finally { cookieBusy = false; cookieButtons(); }
}

function queueCookiePreview() {
  clearTimeout(cookieTimer);
  const candidate = $('cookie-input').value.trim().replace(/^cookie:\s*/i, '');
  if (!candidate || !/(?:^|;\s*)d_c0=\s*\S+/.test(candidate) || !/(?:^|;\s*)z_c0=\s*\S+/.test(candidate)) return;
  cookieTimer = setTimeout(() => { if (cookieBusy) queueCookiePreview(); else testCookie(true); }, 1200);
}

$('cookie-input').addEventListener('input', () => {
  cookieGeneration += 1;
  cookieButtons();
  cookieMessage($('cookie-input').value.trim() ? '输入含 d_c0 和 z_c0 的完整 Cookie 后会自动预验证；点击保存后才会替换已保存的值。' : '');
  queueCookiePreview();
});

$('cookie-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (cookieBusy) return;
  const candidate = $('cookie-input').value.trim();
  if (!candidate) { cookieMessage('请先粘贴完整的知乎 Cookie。', 'warning'); return; }
  clearTimeout(cookieTimer);
  const generation = ++cookieGeneration;
  cookieBusy = true;
  cookieButtons();
  cookieMessage('正在保存并测试 Cookie…', 'pending');
  try {
    const data = await api('/api/cookie', {cookie: candidate, test_url: $('cookie-test-url').value.trim()});
    if (generation !== cookieGeneration || !activeUsername) return;
    $('cookie-input').value = '';
    renderCookieState(data);
    cookieMessage(validationMessage(data.validation, data.validation.valid ? '已保存。' : 'Cookie 已保存，测试暂未通过。'), data.validation.valid ? 'success' : 'warning');
  } catch (error) { if (generation === cookieGeneration) cookieFailure(error); }
  finally { cookieBusy = false; cookieButtons(); }
});

$('test-cookie').addEventListener('click', () => { clearTimeout(cookieTimer); testCookie(Boolean($('cookie-input').value.trim())); });
$('remove-cookie').addEventListener('click', async () => {
  if (cookieBusy) return;
  clearTimeout(cookieTimer);
  const generation = ++cookieGeneration;
  cookieBusy = true;
  cookieButtons();
  try {
    const data = await api('/api/cookie/remove', {});
    if (generation !== cookieGeneration || !activeUsername) return;
    $('cookie-input').value = '';
    renderCookieState(data);
    cookieMessage('已移除当前保存的知乎 Cookie。');
  } catch (error) { if (generation === cookieGeneration) cookieFailure(error); }
  finally { cookieBusy = false; cookieButtons(); }
});

for (const [input, link, storage] of [['cookie-help-url', 'cookie-help-link', 'zhihu_cookie_help_url'], ['cookie-test-url', 'cookie-test-link', 'zhihu_cookie_test_url']]) {
  try { const saved = localStorage.getItem(storage); if (saved) $(input).value = saved; } catch {}
  syncCookieLink(input, link, storage, false);
  $(input).addEventListener('input', () => {
    syncCookieLink(input, link, storage);
    if (input === 'cookie-test-url') { cookieGeneration += 1; queueCookiePreview(); }
  });
}

// ---- Bilibili cookie: unlocks direct subtitle fetching ----
// Mirrors the Zhihu cookie flow: typing a complete cookie triggers
// a debounced preview test, saving stores + tests against the test
// video, and a stale saved validation re-tests itself on load.
let biliCookieState = null;
let biliCookieBusy = false;
let biliCookieTimer = null;
let biliCookieGeneration = 0;

function biliCookieMessage(message, kind = '') {
  $('bili-cookie-message').textContent = message;
  $('bili-cookie-message').className = 'message' + (kind ? ' ' + kind : '');
}

function biliCookieButtons() {
  $('save-bili-cookie').disabled = biliCookieBusy;
  $('test-bili-cookie').disabled = biliCookieBusy || (!biliCookieState?.bili_configured && !$('bili-cookie-input').value.trim());
  $('remove-bili-cookie').disabled = biliCookieBusy || !biliCookieState?.bili_configured;
}

function renderBiliCookieState(data) {
  biliCookieState = data;
  const validation = data.validation || {};
  $('bili-cookie-badge').textContent = !data.bili_configured ? '未配置'
    : validation.valid ? '已保存 · 验证通过' : validation.state === 'unchecked' ? '已保存 · 待测试' : '已保存 · 测试未通过';
  $('bili-cookie-badge').dataset.state = validation.valid ? 'valid' : data.bili_configured ? 'warning' : 'unchecked';
  biliCookieButtons();
}

function biliValidationMessage(validation, prefix) {
  const checked = validation.checked_at ? ` 上次测试：${new Date(validation.checked_at).toLocaleString()}` : '';
  return prefix + (validation.message || '') + checked;
}

function syncBiliTestLink() {
  const value = $('bili-test-url').value.trim();
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || !(url.hostname === 'bilibili.com' || url.hostname.endsWith('.bilibili.com')) || url.username || url.password) throw new Error();
    $('bili-test-link').href = url.href;
    $('bili-test-link').removeAttribute('aria-disabled');
  } catch {
    $('bili-test-link').removeAttribute('href');
    $('bili-test-link').setAttribute('aria-disabled', 'true');
  }
}

async function loadBiliCookieState() {
  try {
    const data = await api('/api/bili-cookie');
    if (!activeUsername) return;
    renderBiliCookieState(data);
    if (data.test_url) $('bili-test-url').value = data.test_url;
    syncBiliTestLink();
    if (data.bili_configured) {
      biliCookieMessage(biliValidationMessage(data.validation || {}, ''), data.validation?.valid ? 'success' : 'warning');
      const checked = Date.parse(data.validation?.checked_at || '');
      if (!Number.isFinite(checked) || Date.now() - checked > 15 * 60 * 1000) await testBiliCookie(false);
    }
  } catch (error) { biliCookieMessage(error.message, 'warning'); }
}

async function testBiliCookie(preview = false) {
  if (biliCookieBusy || !activeUsername) return;
  const candidate = $('bili-cookie-input').value.trim();
  if (preview && !candidate) return;
  const generation = ++biliCookieGeneration;
  biliCookieBusy = true;
  biliCookieButtons();
  biliCookieMessage(preview ? '正在自动验证输入的 Cookie…' : '正在使用已保存的 Cookie 测试登录与字幕…', 'pending');
  try {
    const payload = {test_url: $('bili-test-url').value.trim()};
    if (preview) payload.cookie = candidate;
    const data = await api('/api/bili-cookie/test', payload);
    if (generation !== biliCookieGeneration || !activeUsername) return;
    if (!preview && biliCookieState) renderBiliCookieState({...biliCookieState, validation: data.validation});
    biliCookieMessage(biliValidationMessage(data.validation, preview ? '输入尚未保存。' : ''), data.validation.valid ? 'success' : 'warning');
  } catch (error) { if (generation === biliCookieGeneration) biliCookieMessage(error.message, 'warning'); }
  finally { biliCookieBusy = false; biliCookieButtons(); }
}

function queueBiliCookiePreview() {
  clearTimeout(biliCookieTimer);
  const candidate = $('bili-cookie-input').value.trim().replace(/^cookie:\s*/i, '');
  if (!candidate || !/(?:^|;\s*)SESSDATA=\s*\S+/.test(candidate)) return;
  biliCookieTimer = setTimeout(() => { if (biliCookieBusy) queueBiliCookiePreview(); else testBiliCookie(true); }, 1200);
}

$('bili-cookie-input').addEventListener('input', () => {
  biliCookieGeneration += 1;
  biliCookieButtons();
  biliCookieMessage($('bili-cookie-input').value.trim() ? '输入含 SESSDATA 的完整 Cookie 后会自动预验证；点击保存后才会替换已保存的值。' : '');
  queueBiliCookiePreview();
});

$('bili-test-url').addEventListener('input', () => { syncBiliTestLink(); biliCookieGeneration += 1; queueBiliCookiePreview(); });

$('bili-cookie-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (biliCookieBusy) return;
  const candidate = $('bili-cookie-input').value.trim();
  if (!candidate) { biliCookieMessage('请先粘贴完整的 Bilibili Cookie。', 'warning'); return; }
  clearTimeout(biliCookieTimer);
  const generation = ++biliCookieGeneration;
  biliCookieBusy = true;
  biliCookieButtons();
  biliCookieMessage('正在保存并测试 Cookie…', 'pending');
  try {
    const data = await api('/api/bili-cookie', {cookie: candidate, test_url: $('bili-test-url').value.trim()});
    if (generation !== biliCookieGeneration || !activeUsername) return;
    $('bili-cookie-input').value = '';
    renderBiliCookieState(data);
    biliCookieMessage(biliValidationMessage(data.validation, data.validation.valid ? '已保存。' : 'Cookie 已保存，测试暂未通过。'), data.validation.valid ? 'success' : 'warning');
  } catch (error) { if (generation === biliCookieGeneration) biliCookieMessage(error.message, 'warning'); }
  finally { biliCookieBusy = false; biliCookieButtons(); }
});

$('test-bili-cookie').addEventListener('click', () => { clearTimeout(biliCookieTimer); testBiliCookie(Boolean($('bili-cookie-input').value.trim())); });
$('remove-bili-cookie').addEventListener('click', async () => {
  if (biliCookieBusy) return;
  clearTimeout(biliCookieTimer);
  const generation = ++biliCookieGeneration;
  biliCookieBusy = true;
  biliCookieButtons();
  try {
    const data = await api('/api/bili-cookie/remove', {});
    if (generation !== biliCookieGeneration || !activeUsername) return;
    $('bili-cookie-input').value = '';
    renderBiliCookieState(data);
    biliCookieMessage('已移除当前保存的 Bilibili Cookie。');
  } catch (error) { if (generation === biliCookieGeneration) biliCookieMessage(error.message, 'warning'); }
  finally { biliCookieBusy = false; biliCookieButtons(); }
});

// Mac transcription access code (write-only): the value is stored
// server-side for the YouTube ASR fallback and never read back; the
// panel only ever learns whether one is configured.
let macCodeState = null;
let macCodeBusy = false;

function macMessage(message, kind = '') {
  $('mac-message').textContent = message;
  $('mac-message').className = 'message' + (kind ? ' ' + kind : '');
}

function renderMacCodeState(data) {
  macCodeState = data;
  $('mac-badge').textContent = data.configured ? '已保存' : '未配置';
  $('mac-badge').dataset.state = data.configured ? 'valid' : 'unchecked';
  $('save-mac').disabled = macCodeBusy;
  $('remove-mac').disabled = macCodeBusy || !data.configured;
}

async function loadMacCodeState() {
  try {
    const data = await api('/api/mac-code');
    if (!activeUsername) return;
    renderMacCodeState(data);
    macMessage(data.configured ? '已保存访问码，YouTube 无字幕时会自动用 Mac 转写。'
      : '尚未配置：YouTube 视频没有字幕时将无法转写。');
  } catch (error) { macMessage(error.message, 'warning'); }
}

$('mac-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (macCodeBusy || !activeUsername) return;
  const value = $('mac-input').value.trim();
  if (!value) { macMessage('请先粘贴访问码。', 'warning'); return; }
  macCodeBusy = true;
  renderMacCodeState(macCodeState || {configured: false});
  try {
    const data = await api('/api/mac-code', {access_code: value});
    $('mac-input').value = '';
    renderMacCodeState(data);
    macMessage('访问码已保存到服务器。', 'success');
  } catch (error) { macMessage(error.message, 'warning'); }
  finally { macCodeBusy = false; renderMacCodeState(macCodeState || {configured: false}); }
});

$('remove-mac').addEventListener('click', async () => {
  if (macCodeBusy || !activeUsername) return;
  macCodeBusy = true;
  renderMacCodeState(macCodeState || {configured: false});
  try {
    const data = await api('/api/mac-code/remove', {});
    renderMacCodeState(data);
    macMessage('已删除保存的访问码。');
  } catch (error) { macMessage(error.message, 'warning'); }
  finally { macCodeBusy = false; renderMacCodeState(macCodeState || {configured: false}); }
});

// Live progress for the built-in YouTube pipeline. settings.js polls
// the task and forwards snapshots here; null hides the block again.
let tpTimer = null;
let tpAnchor = 0;

function tpFormatElapsed(sec) {
  const total = Math.max(0, Math.floor(sec));
  return String(Math.floor(total / 60)).padStart(2, '0') + ':'
    + String(total % 60).padStart(2, '0');
}

function ytTaskProgress(task) {
  const block = $('task-progress');
  if (!task) {
    block.hidden = true;
    if (tpTimer) { clearInterval(tpTimer); tpTimer = null; }
    return;
  }
  block.hidden = false;
  const label = task.stage_label || '正在处理…';
  $('tp-stage').textContent = label + (task.position > 1 ? `（排队第 ${task.position} 位）` : '');
  $('tp-detail').textContent = task.detail || '';
  const bar = $('tp-bar');
  if (typeof task.overall_percent === 'number') bar.value = task.overall_percent;
  else bar.removeAttribute('value');
  $('status').textContent = label;
  tpAnchor = Date.now() - (task.elapsed_sec || 0) * 1000;
  $('tp-elapsed').textContent = tpFormatElapsed((Date.now() - tpAnchor) / 1000);
  if (!tpTimer) {
    tpTimer = setInterval(() => {
      $('tp-elapsed').textContent = tpFormatElapsed((Date.now() - tpAnchor) / 1000);
    }, 500);
  }
}
window.onYtTaskProgress = ytTaskProgress;

// Clipboard workflow follows the supplied page: read/extract, then click/copy.
const flow = {links: [], selected: '', busy: false, reading: false, copying: false,
  phase: 'idle', controller: null, generation: 0, lastCopiedText: ''};
const zen = {on: false};
let linkTimer = null;
let toastTimer = null;

function toast(message, duration = 2200) {
  $('toast').textContent = message;
  $('toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.remove('show'), duration);
}

function extractMessage(message = '', kind = '') {
  $('extract-message').textContent = message;
  $('extract-message').className = 'message extract-message' + (kind ? ' ' + kind : '');
}

function controls() {
  const busy = flow.busy || flow.reading || flow.copying;
  $('extract-button').disabled = busy;
  $('pasteBtn').disabled = busy;
  $('oneClick').disabled = busy;
  $('copy').disabled = !currentResult || busy;
  $('download').disabled = !currentResult || busy;
  $('cancel-extract').hidden = !flow.busy;
  $('oneClick').classList.toggle('ready', flow.phase === 'ready');
  $('oneClick').textContent = flow.reading ? '读取剪贴板…' : flow.busy ? '正在提取…'
    : flow.copying ? '正在复制…' : flow.phase === 'ready' ? '📋 再次点击复制文本' : '⚡ 一键获取';
}

function disarmCopy() {
  $('copyHint').hidden = true;
  if (flow.phase === 'ready') flow.phase = 'idle';
  controls();
}

function armCopy() {
  if (!currentResult?.content) return;
  flow.phase = 'ready';
  $('copyHint').hidden = false;
  $('status').textContent = '再次点击复制正文';
  controls();
}

const TEXT_URL_RE = /https?:\/\/[^\s"'<>《》「」『』【】（）()[\]{}，,。；;、！!…]+|(?:(?:zhuanlan\.|www\.|m\.)?zhihu\.com|(?:www\.|m\.|music\.)?youtube\.com|youtu\.be)\/[^\s"'<>《》「」『』【】（）()[\]{}，,。；;、！!…]+/gi;

function classifyLink(raw) {
  let value = raw.replace(/[.,;:!?。，；：！？、)\]}>》」』"'…]+$/g, '');
  if (!/^https?:\/\//i.test(value)) value = 'https://' + value;
  let parsed;
  try { parsed = new URL(value); } catch { return null; }
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.port) return null;
  const path = parsed.pathname.replace(/\/+$/, '');
  const host = parsed.hostname.toLowerCase();
  const routed = window.siteSettingsUI.classify(parsed);
  if (routed) return routed;
  if (['douyin.com', 'www.douyin.com', 'v.douyin.com', 'iesdouyin.com', 'www.iesdouyin.com'].includes(host)) {
    return {url: value, kind: '抖音视频', tail: host + path, media: 'video'};
  }
  if (['bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv'].includes(host)) {
    return {url: value, kind: 'B站视频', tail: host + path, media: 'video'};
  }
  let match;
  if (host === 'zhuanlan.zhihu.com' && (match = /^\/p\/([0-9]{1,30})$/.exec(path))) {
    return {url: 'https://zhuanlan.zhihu.com' + path, kind: '专栏', tail: 'p/' + match[1]};
  }
  if (!['zhihu.com', 'www.zhihu.com', 'm.zhihu.com'].includes(host)) return null;
  if ((match = /^\/question\/([0-9]{1,30})\/answer\/([0-9]{1,30})$/.exec(path))) {
    return {url: 'https://www.zhihu.com' + path, kind: '回答', tail: 'answer/' + match[2]};
  }
  if ((match = /^\/answer\/([0-9]{1,30})$/.exec(path))) {
    return {url: 'https://www.zhihu.com' + path, kind: '回答', tail: 'answer/' + match[1]};
  }
  if ((match = /^\/question\/([0-9]{1,30})$/.exec(path))) {
    return {url: 'https://www.zhihu.com' + path, kind: '问题', tail: 'question/' + match[1]};
  }
  return null;
}

function extractLinks(text) {
  const found = [], seen = new Set();
  for (const match of String(text).slice(0, 65536).matchAll(TEXT_URL_RE)) {
    if (match.index > 0 && /[A-Za-z0-9_.@-]/.test(text[match.index - 1])) continue;
    const link = classifyLink(match[0]);
    if (!link || seen.has(link.url)) continue;
    seen.add(link.url);
    found.push(link);
  }
  return found;
}

function renderLinks() {
  flow.links = extractLinks($('url').value);
  if (!flow.links.some(link => link.url === flow.selected)) flow.selected = flow.links[0]?.url || '';
  $('linkChips').replaceChildren();
  $('links').hidden = flow.links.length === 0;
  $('linkCount').textContent = flow.links.length === 1 ? '识别到 1 个可用链接'
    : `识别到 ${flow.links.length} 个可用链接，点击可切换：`;
  for (const [index, link] of flow.links.entries()) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chip' + (link.url === flow.selected ? ' on' : '');
    button.dataset.index = String(index);
    button.setAttribute('aria-pressed', String(link.url === flow.selected));
    const kind = document.createElement('span');
    kind.className = 'kind';
    kind.textContent = link.kind;
    const tail = document.createElement('span');
    tail.className = 'tail';
    tail.textContent = link.tail;
    button.append(kind, tail);
    $('linkChips').appendChild(button);
  }
}

function cancelExtraction(quiet = false) {
  flow.generation += 1;
  ytTaskProgress(null);
  flow.controller?.abort();
  flow.controller = null;
  flow.busy = false;
  flow.reading = false;
  flow.phase = 'idle';
  $('copyHint').hidden = true;
  controls();
  if (!quiet) {
    $('status').textContent = '已取消';
    extractMessage('已取消当前页面的请求。');
  }
}

function resetExtraction({quiet = false} = {}) {
  cancelExtraction(true);
  clearTimeout(linkTimer);
  currentResult = null;
  flow.selected = '';
  flow.lastCopiedText = '';
  $('url').value = '';
  $('content').textContent = '';
  $('result-title').textContent = '';
  $('result-meta').textContent = '';
  $('result-source').hidden = true;
  $('result-source').removeAttribute('href');
  $('result').hidden = true;
  $('video-panel').hidden = true;
  $('video-result').hidden = true;
  $('video-player').removeAttribute('src');
  $('video-player').load();
  videoResult = null;
  $('copy').textContent = '一键复制';
  $('copy').classList.remove('done');
  renderLinks();
  controls();
  extractMessage();
  $('status').textContent = '就绪';
  if (!quiet) $('url').focus();
}

async function extractContent({copyOnNextClick = false} = {}) {
  if (!activeUsername || flow.busy || flow.reading || flow.copying) return false;
  renderLinks();
  const selectedLink = flow.links.find(link => link.url === flow.selected);
  if (selectedLink?.media === 'video'
      || (!flow.selected && /BV[0-9A-Za-z]{10}/.test($('url').value))) {
    return extractVideo({copyOnNextClick});
  }
  if (!flow.selected) {
    extractMessage('没有识别到支持的链接。请粘贴知乎、YouTube、抖音、B站或已配置网站的完整链接。', 'warning');
    $('url').focus();
    return false;
  }
  disarmCopy();
  const generation = ++flow.generation;
  const controller = new AbortController();
  flow.controller = controller;
  flow.busy = true;
  flow.phase = 'extracting';
  currentResult = null;
  $('result').hidden = true;
  $('content').textContent = '';
  $('status').textContent = '正在提取…';
  extractMessage('正在读取页面，请稍候…', 'pending');
  controls();
  try {
    const startedAt = performance.now();
    const data = await window.siteSettingsUI.extract(flow.selected, $('format').value,
      {keep_images: $('keep-images').checked}, controller.signal);
    if (generation !== flow.generation || !activeUsername) return false;
    if (typeof data.content !== 'string' || !data.content.trim()) throw new Error('请求成功，但没有获取到正文。');
    currentResult = data;
    $('result-title').textContent = data.title || '提取结果';
    $('result-meta').textContent = [data.author, data.published].filter(Boolean).join(' · ') || '—';
    { const elapsedMs = Math.round(performance.now() - startedAt); const kb = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B'; const parts = ['用时 ' + (elapsedMs >= 1000 ? (elapsedMs / 1000).toFixed(1) + ' 秒' : elapsedMs + ' 毫秒')]; if (typeof data.stats?.fetched_bytes === 'number') parts.push('拉取 ' + kb(data.stats.fetched_bytes)); if (data.origin) parts.push('来源 ' + data.origin); const outBytes = new TextEncoder().encode(data.content).length; parts.push('输出 ' + kb(outBytes) + '（' + data.content.length.toLocaleString() + ' 字）'); $('result-meta').textContent += ' · ' + parts.join(' · '); }
    $('result-source').href = flow.selected;
    $('result-source').hidden = false;
    $('content').textContent = data.content;
    $('result').hidden = false;
    $('copy').textContent = '一键复制';
    $('copy').classList.remove('done');
    flow.phase = 'idle';
    if (copyOnNextClick || zen.on) {
      armCopy();
      extractMessage('✓ 正文已提取。再次点击「一键获取」或页面空白处即可复制。', 'success');
      toast('已提取，再点一次复制正文', 2600);
    } else {
      $('status').textContent = '提取完成 ✓';
      extractMessage('提取成功', 'success');
    }
    return true;
  } catch (error) {
    if (generation !== flow.generation || error.name === 'AbortError') return false;
    flow.phase = 'idle';
    $('status').textContent = '提取未完成';
    extractMessage(error.code === 'cookie_not_configured'
      ? '请在页面下方展开“知乎 Cookie”，粘贴并保存后再提取。' : error.message, 'warning');
    if (error.code === 'cookie_not_configured') $('cookie-panel').open = true;
    if (error.code === 'route_not_configured') window.siteSettingsUI.open();
    return false;
  } finally {
    if (generation === flow.generation) {
      flow.controller = null;
      flow.busy = false;
      controls();
    }
  }
}

async function readClipboardText() {
  if (!navigator.clipboard?.readText) throw new Error('当前浏览器不支持读取剪贴板，请手动粘贴后点击“提取文本”。');
  let text;
  try { text = String(await navigator.clipboard.readText() || '').trim(); }
  catch { throw new Error('无法读取剪贴板，请允许此网站的剪贴板权限，或手动粘贴后点击“提取文本”。'); }
  if (!text) throw new Error('剪贴板里没有文本，请先复制链接。');
  return text.slice(0, 65536);
}

async function extractVideo({copyOnNextClick = false} = {}) {
  disarmCopy();
  const generation = ++flow.generation;
  flow.busy = true;
  flow.phase = 'extracting';
  currentResult = null;
  $('result').hidden = true;
  $('video-panel').hidden = true;
  $('video-result').hidden = true;
  $('status').textContent = '正在解析…';
  extractMessage('正在解析视频链接，请稍候…', 'pending');
  controls();
  const startedAt = performance.now();
  try {
    const data = await api('/api/video/parse', {text: $('url').value.trim()});
    if (generation !== flow.generation || !activeUsername) return false;
    videoResult = data;
    if (data.subtitle_text) {
      currentResult = {title: data.title || '字幕', author: data.platform_name || '',
        published: '', content: data.subtitle_text, format: 'text'};
      $('result-title').textContent = data.title || '字幕原文';
      const elapsedMs = Math.round(performance.now() - startedAt);
      const parts = ['用时 ' + (elapsedMs >= 1000 ? (elapsedMs / 1000).toFixed(1) + ' 秒' : elapsedMs + ' 毫秒'),
        '字幕 ' + data.subtitle_text.length.toLocaleString() + ' 字'];
      $('result-meta').textContent = [data.platform_name + ' · 字幕', data.subtitle_lang].filter(Boolean).join(' · ')
        + ' · ' + parts.join(' · ');
      const src = data.page_url || data.referer;
      if (src) { $('result-source').href = src; $('result-source').hidden = false; }
      $('content').textContent = data.subtitle_text;
      $('result').hidden = false;
      $('copy').textContent = '一键复制';
      $('copy').classList.remove('done');
      flow.phase = 'idle';
      if (copyOnNextClick || zen.on) {
        armCopy();
        extractMessage('✓ 字幕已提取。再次点击「一键获取」或页面空白处即可复制。', 'success');
        toast('已提取字幕，再点一次复制', 2600);
      } else {
        $('status').textContent = '提取完成 ✓';
        extractMessage('字幕提取成功', 'success');
      }
      return true;
    }
    $('video-title').textContent = data.title || data.id;
    $('video-platform').textContent = data.platform_name + ' · ' + data.quality;
    videoUsingProxy = false;
    $('video-player').src = data.play_url;
    $('video-download').href = videoStreamUrl(data, true);
    $('video-download').textContent = data.audio_only ? '下载音频' : '下载视频';
    $('video-open').href = data.play_url;
    $('video-status').textContent = '';
    $('video-message').textContent = '';
    $('video-result').hidden = false;
    $('video-panel').hidden = false;
    flow.phase = 'idle';
    $('status').textContent = '解析完成 ✓';
    extractMessage(data.audio_only ? '该视频没有音画合成流，已改为只取音频（保证有声音）。' : '视频解析成功', 'success');
    return true;
  } catch (error) {
    if (generation !== flow.generation) return false;
    flow.phase = 'idle';
    $('status').textContent = '解析未完成';
    extractMessage(error.message, 'warning');
    return false;
  } finally {
    if (generation === flow.generation) {
      flow.busy = false;
      controls();
    }
  }
}

async function fromClipboard({extractImmediately = true} = {}) {
  if (!activeUsername || flow.busy || flow.reading || flow.copying) return;
  const generation = flow.generation;
  flow.reading = true;
  $('status').textContent = '读取剪贴板…';
  controls();
  try {
    const text = await readClipboardText();
    if (generation !== flow.generation || !activeUsername) return;
    if (extractImmediately && flow.lastCopiedText && text === flow.lastCopiedText) {
      $('status').textContent = '请复制下一条链接';
      toast('正文已复制，请先复制下一条链接');
      return;
    }
    disarmCopy();
    $('url').value = text;
    renderLinks();
    if (!flow.links.length) {
      extractMessage('已读取剪贴板，但没有识别到支持的链接。请添加对应的网站规则。', 'warning');
      $('status').textContent = '没有可用链接';
      return;
    }
    flow.reading = false;
    controls();
    if (extractImmediately) {
      await extractContent({copyOnNextClick: true});
    } else {
      extractMessage(`已粘贴，识别到 ${flow.links.length} 个可用链接。`, 'success');
      $('status').textContent = '已识别链接';
    }
  } catch (error) {
    if (generation !== flow.generation) return;
    $('status').textContent = '请允许剪贴板权限或手动粘贴';
    extractMessage(error.message, 'warning');
    toast('请允许剪贴板权限，或手动粘贴');
  } finally {
    if (generation === flow.generation) {
      flow.reading = false;
      controls();
    }
  }
}

function fallbackCopy(text) {
  const previous = document.activeElement;
  const field = document.createElement('textarea');
  field.className = 'clipboard-fallback';
  field.value = text;
  field.setAttribute('readonly', '');
  document.body.appendChild(field);
  field.select();
  let copied = false;
  try { copied = document.execCommand('copy'); } catch {}
  field.remove();
  try { previous?.focus({preventScroll: true}); } catch {}
  return copied;
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch {}
  }
  return fallbackCopy(text);
}

async function copyResult() {
  if (!activeUsername || !currentResult?.content || flow.busy || flow.reading || flow.copying) return;
  const text = currentResult.content;
  const generation = flow.generation;
  flow.copying = true;
  controls();
  try {
    const copied = await copyText(text);
    if (generation !== flow.generation || !activeUsername) return;
    if (!copied) {
      const range = document.createRange();
      range.selectNodeContents($('content'));
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
      extractMessage('自动复制未成功，已选中正文，请按 Ctrl / ⌘ + C 复制。', 'warning');
      $('status').textContent = '请手动复制';
      return;
    }
    flow.lastCopiedText = text.trim().slice(0, 65536);
    flow.phase = 'copied';
    $('copyHint').hidden = true;
    $('copy').textContent = '已复制 ✓';
    $('copy').classList.add('done');
    $('status').textContent = '已复制到剪贴板 ✓';
    extractMessage('✓ 已复制正文。复制下一条链接后，再点「一键获取」或在无感模式下点击页面空白处即可继续。', 'success');
    toast('✓ 已复制正文到剪贴板');
  } finally {
    flow.copying = false;
    controls();
  }
}

function setZenOn(on, silent = false) {
  zen.on = Boolean(on);
  $('zenMode').checked = zen.on;
  $('zenSwitch').classList.toggle('on', zen.on);
  try { localStorage.setItem('zen_mode', zen.on ? '1' : '0'); } catch {}
  if (!silent) {
    toast(zen.on ? '无感模式已开启' : '无感模式已关闭');
    extractMessage(zen.on ? '无感模式：复制链接后点击页面空白处提取，完成后再点一次复制。' : '', zen.on ? 'success' : '');
  }
  if (zen.on && currentResult?.content && !flow.busy && flow.phase !== 'copied') armCopy();
}

function isInteractive(element) {
  return Boolean(element?.closest('button, a, input, textarea, select, summary, label, details, [contenteditable], #copyHint, #toast'));
}

$('extract-form').addEventListener('submit', event => { event.preventDefault(); extractContent(); });
$('pasteBtn').addEventListener('click', () => fromClipboard({extractImmediately: false}));
$('oneClick').addEventListener('click', () => flow.phase === 'ready' && currentResult ? copyResult() : fromClipboard());
$('copy').addEventListener('click', copyResult);
$('cancel-extract').addEventListener('click', () => cancelExtraction());
$('cancel-copy').addEventListener('click', () => { disarmCopy(); $('status').textContent = '正文已就绪'; });
$('reset').addEventListener('click', () => resetExtraction());
$('zenMode').addEventListener('change', event => setZenOn(event.target.checked));

$('url').addEventListener('input', () => {
  if (flow.busy || flow.reading) cancelExtraction(true);
  disarmCopy();
  clearTimeout(linkTimer);
  linkTimer = setTimeout(renderLinks, 140);
});
$('url').addEventListener('focus', () => { if ($('url').value) $('url').select(); });
$('url').addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault();
    extractContent();
  }
});
$('linkChips').addEventListener('click', event => {
  const button = event.target.closest('button[data-index]');
  if (!button || !flow.links[Number(button.dataset.index)]) return;
  if (flow.busy || flow.reading) cancelExtraction(true);
  disarmCopy();
  flow.selected = flow.links[Number(button.dataset.index)].url;
  renderLinks();
});
for (const id of ['format', 'keep-images']) $(id).addEventListener('change', () => {
  if (flow.busy || flow.reading) cancelExtraction(true);
  disarmCopy();
});

$('download').addEventListener('click', () => {
  if (!currentResult) return;
  const extension = {markdown: 'md', text: 'txt', json: 'json'}[currentResult.format];
  const file = new Blob([currentResult.content], {type: 'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(file);
  const link = document.createElement('a');
  link.href = url;
  link.download = (currentResult.title || 'zhihu').replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').slice(0, 100) + '.' + extension;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

document.addEventListener('click', event => {
  if (!activeUsername || event.defaultPrevented || event.button !== 0 || isInteractive(event.target)) return;
  if (flow.busy || flow.reading || flow.copying || String(window.getSelection() || '').trim()) return;
  if (flow.phase === 'ready' && currentResult) copyResult();
  else if (zen.on) fromClipboard();
});

try { setZenOn(localStorage.getItem('zen_mode') === '1', true); } catch { setZenOn(false, true); }
try {
  const restored = sessionStorage.getItem('routing-input');
  if (restored) { $('url').value = restored; renderLinks(); }
  sessionStorage.removeItem('routing-input');
} catch {}
controls();
showAccount('platform');

// ---- short-video parsing (Douyin / Bilibili, lowest quality) ----
// Logic ported from jiuhunwl/short_videos:
// https://github.com/jiuhunwl/short_videos
let videoResult = null;
let videoUsingProxy = false;

function videoStreamUrl(data, download) {
  const params = new URLSearchParams({u: data.play_url, ref: data.referer,
    name: data.filename});
  if (download) params.set('dl', '1');
  return '/api/video/stream?' + params.toString();
}

$('video-copy').addEventListener('click', async () => {
  if (!videoResult) return;
  try {
    await navigator.clipboard.writeText(videoResult.play_url);
    $('video-status').textContent = '直链已复制';
  } catch {
    $('video-message').textContent = '复制失败，请用「打开直链」手动复制地址。';
  }
});

$('video-player').addEventListener('error', () => {
  if (!videoResult) return;
  if (!videoUsingProxy) {
    videoUsingProxy = true;
    $('video-player').src = videoStreamUrl(videoResult, false);
    $('video-status').textContent = '直连被 CDN 拒绝，已切换服务器中转播放';
  } else {
    $('video-message').textContent = '播放失败：直链可能已过期，请重新解析。';
  }
});
