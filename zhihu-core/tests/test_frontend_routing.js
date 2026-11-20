'use strict';
// Frontend routing harness for the Bilibili transcription wiring.
//
// Loads the real static/app.js in a vm context with a stub DOM and
// asserts on classifyLink / extractLinks / TEXT_URL_RE / extractContent
// routing / renderVideoResult / the lazy direct-link flow. No deps.
//
// Run from the project root: node tests/test_frontend_routing.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('node:assert/strict');

const BILI_URL = 'https://www.bilibili.com/video/BV1VeHQ6tEaS/';
const BV = 'BV1VeHQ6tEaS';

function makeElement(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    _attrs: {},
    _handlers: {},
    _classSet: new Set(),
    hidden: false,
    disabled: false,
    open: false,
    textContent: '',
    value: '',
    checked: false,
    className: '',
    src: '',
    href: '',
    download: '',
    type: '',
    addEventListener(type, fn) {
      if (!this._handlers[type]) this._handlers[type] = [];
      this._handlers[type].push(fn);
    },
    removeEventListener() {},
    setAttribute(key, value) { this._attrs[key] = String(value); },
    getAttribute(key) {
      return Object.prototype.hasOwnProperty.call(this._attrs, key)
        ? this._attrs[key] : null;
    },
    removeAttribute(key) { delete this._attrs[key]; },
    append() { this.children.push(...arguments); },
    appendChild(node) { this.children.push(node); return node; },
    replaceChildren() { this.children = Array.from(arguments); },
    focus() {},
    select() {},
    click() {},
    load() {},
    remove() {},
    closest() { return null; },
    __fire(type, event) {
      const list = this._handlers[type] || [];
      return list.map((fn) => fn(Object.assign(
        {preventDefault() {}, target: this, button: 0}, event || {})));
    },
  };
  el.classList = {
    add() {
      for (const c of arguments) el._classSet.add(c);
    },
    remove() {
      for (const c of arguments) el._classSet.delete(c);
    },
    toggle(c, force) {
      const on = force !== undefined ? force : !el._classSet.has(c);
      if (on) el._classSet.add(c);
      else el._classSet.delete(c);
      return on;
    },
    contains(c) { return el._classSet.has(c); },
  };
  return el;
}

const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, makeElement());
  return elements.get(id);
}

function makeStorage() {
  const m = new Map();
  return {
    getItem(k) { return m.has(k) ? m.get(k) : null; },
    setItem(k, v) { m.set(k, String(v)); },
    removeItem(k) { m.delete(k); },
  };
}

const fetchCalls = [];
let fetchImpl = async () => ({
  ok: true, status: 200, json: async () => ({}),
});
const sandboxFetch = async (pathArg, options) => {
  fetchCalls.push([pathArg, options]);
  return fetchImpl(pathArg, options);
};

const uiCalls = {classify: [], extract: [], open: 0, accountChanged: []};
let classifyImpl = () => null;
let extractImpl = async () => {
  throw new Error('siteSettingsUI.extract not stubbed');
};
const siteSettingsUI = {
  classify(parsed) {
    uiCalls.classify.push(parsed);
    return classifyImpl(parsed);
  },
  async extract() {
    const args = Array.from(arguments);
    uiCalls.extract.push(args);
    return extractImpl(...args);
  },
  open() { uiCalls.open += 1; },
  accountChanged(u) { uiCalls.accountChanged.push(u); },
};

const documentStub = {
  getElementById: (id) => el(id),
  createElement: (tag) => makeElement(tag),
  createRange: () => ({selectNodeContents() {}}),
  execCommand: () => false,
  activeElement: null,
  body: makeElement('body'),
  _handlers: {},
  addEventListener(type, fn) {
    if (!this._handlers[type]) this._handlers[type] = [];
    this._handlers[type].push(fn);
  },
};

const sandbox = {
  document: documentStub,
  navigator: {
    clipboard: {readText: async () => '', writeText: async () => {}},
  },
  localStorage: makeStorage(),
  sessionStorage: makeStorage(),
  fetch: sandboxFetch,
  siteSettingsUI,
  getSelection: () => ({
    toString: () => '',
    removeAllRanges() {},
    addRange() {},
  }),
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  URL,
  URLSearchParams,
  TextEncoder,
  Blob,
  AbortController,
  performance,
};
sandbox.window = sandbox;
vm.createContext(sandbox);

const src = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
vm.runInContext(src, sandbox, {filename: 'app.js'});
vm.runInContext([
  'globalThis.__t = {',
  '  classifyLink, extractLinks, TEXT_URL_RE, extractContent,',
  '  extractVideo, renderVideoResult,',
  '  get flow() { return flow; },',
  '  get videoResult() { return videoResult; },',
  '  getCurrentResult() { return currentResult; },',
  '  setCurrentResult(v) { currentResult = v; },',
  '  setUser(u) { activeUsername = u; },',
  '  resetFlow() {',
  '    flow.links = []; flow.selected = ""; flow.busy = false;',
  '    flow.reading = false; flow.copying = false; flow.phase = "idle";',
  '    flow.controller = null; currentResult = null;',
  '  },',
  '};',
].join('\n'), sandbox);
const T = sandbox.__t;

function reset() {
  fetchCalls.length = 0;
  uiCalls.classify.length = 0;
  uiCalls.extract.length = 0;
  uiCalls.open = 0;
  classifyImpl = () => null;
  extractImpl = async () => {
    throw new Error('siteSettingsUI.extract not stubbed');
  };
  fetchImpl = async () => ({
    ok: true, status: 200, json: async () => ({}),
  });
  for (const element of elements.values()) {
    element.textContent = '';
    element.value = '';
    element.hidden = false;
    element.disabled = false;
    element.checked = false;
    element.children = [];
    element._attrs = {};
    element._classSet.clear();
    element.src = '';
    element.href = '';
  }
  el('format').value = 'text';
  T.resetFlow();
  T.setUser('tester');
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

// ---- TEXT_URL_RE ----

test('regex matches bare bilibili hosts', () => {
  reset();
  const text = '看 www.bilibili.com/video/' + BV + '/ 和 bilibili.com/video/'
    + BV + '/ 以及 b23.tv/abc123';
  const found = Array.from(text.matchAll(T.TEXT_URL_RE), (m) => m[0]);
  assert.deepEqual(found, [
    'www.bilibili.com/video/' + BV + '/',
    'bilibili.com/video/' + BV + '/',
    'b23.tv/abc123',
  ]);
});

test('regex matches full bilibili urls', () => {
  reset();
  const text = '链接：https://m.bilibili.com/video/' + BV + '/ 结束';
  const found = Array.from(text.matchAll(T.TEXT_URL_RE), (m) => m[0]);
  assert.deepEqual(found, ['https://m.bilibili.com/video/' + BV + '/']);
});

// ---- classifyLink ----

test('classifyLink: bilibili hosts take the pipeline kind, no media', () => {
  reset();
  for (const host of ['bilibili.com', 'www.bilibili.com',
    'm.bilibili.com', 'b23.tv']) {
    const link = T.classifyLink('https://' + host + '/video/' + BV + '/');
    assert.ok(link, host);
    assert.equal(link.kind, 'B站视频');
    assert.ok(!('media' in link), host + ' must not carry media:video');
    assert.equal(link.url, 'https://' + host + '/video/' + BV + '/');
  }
});

test('classifyLink: douyin keeps media video', () => {
  reset();
  const link = T.classifyLink('https://www.douyin.com/video/123');
  assert.ok(link);
  assert.equal(link.media, 'video');
  assert.equal(link.kind, '抖音视频');
});

test('classifyLink: rejects ports, credentials, unknown hosts', () => {
  reset();
  assert.equal(
    T.classifyLink('https://www.bilibili.com:8080/video/' + BV + '/'), null);
  assert.equal(
    T.classifyLink('https://user@www.bilibili.com/video/' + BV + '/'), null);
  assert.equal(
    T.classifyLink('https://evilbilibili.com/video/' + BV + '/'), null);
  assert.equal(
    T.classifyLink('https://api.bilibili.com/x/web-interface/view'), null);
  assert.equal(T.classifyLink('https://example.com/video/' + BV + '/'), null);
});

test('classifyLink: case-insensitive host, trailing punctuation stripped', () => {
  reset();
  const link = T.classifyLink(
    'HTTPS://WWW.BILIBILI.COM/video/' + BV + '/.');
  assert.ok(link);
  // Scheme/host case is preserved in the returned url; only the
  // trailing dot is stripped.
  assert.equal(link.url, 'HTTPS://WWW.BILIBILI.COM/video/' + BV + '/');
});

test('classifyLink: zhihu regression', () => {
  reset();
  const link = T.classifyLink('https://www.zhihu.com/question/123/answer/456');
  assert.ok(link);
  assert.equal(link.kind, '回答');
  assert.equal(T.classifyLink('https://www.zhihu.com/'), null);
});

test('classifyLink: routed rule wins over builtin', () => {
  reset();
  classifyImpl = () => ({url: BILI_URL, kind: '自定义', tail: 'x'});
  const link = T.classifyLink(BILI_URL);
  assert.equal(link.kind, '自定义');
});

// ---- extractLinks ----

test('extractLinks: mixed text, kinds, dedup', () => {
  reset();
  const text = BILI_URL + ' ' + BILI_URL
    + ' https://www.douyin.com/video/123'
    + ' https://www.zhihu.com/question/123';
  const links = T.extractLinks(text);
  assert.equal(links.length, 3);
  assert.equal(links[0].kind, 'B站视频');
  assert.ok(!('media' in links[0]));
  assert.equal(links[1].media, 'video');
  assert.equal(links[2].kind, '问题');
});

test('extractLinks: bare api/evil hosts excluded', () => {
  reset();
  // Length checks: the arrays come from the vm realm, so deepEqual
  // would fail on prototype identity.
  assert.equal(
    T.extractLinks('x api.bilibili.com/x/web-interface/view y').length, 0);
  assert.equal(
    T.extractLinks('x https://evilbilibili.com/video/' + BV + '/ y').length,
    0);
});

test('extractLinks: bare BV alone is not a link', () => {
  reset();
  assert.equal(T.extractLinks(BV).length, 0);
  assert.equal(T.extractLinks('看看 ' + BV + ' 这个').length, 0);
});

// ---- extractContent routing ----

test('extractContent: bilibili goes to siteSettingsUI.extract', async () => {
  reset();
  el('url').value = BILI_URL;
  extractImpl = async () => ({
    content: 'hello text', kind: 'bilibili', title: 'T', author: 'A',
    published: '', format: 'text', url: BILI_URL,
  });
  const ok = await T.extractContent();
  assert.equal(ok, true);
  assert.equal(uiCalls.extract.length, 1);
  assert.equal(uiCalls.extract[0][0], BILI_URL);
  assert.equal(uiCalls.extract[0][1], 'text');
  assert.deepEqual(fetchCalls, []);
  assert.equal(el('fetch-direct-link').hidden, false);
  assert.equal(el('content').textContent, 'hello text');
});

test('extractContent: non-bilibili result keeps direct-link hidden', async () => {
  reset();
  el('url').value = 'https://www.zhihu.com/question/123';
  extractImpl = async () => ({
    content: 'hello', kind: 'proxy', title: 'T', author: '',
    published: '', format: 'text', url: 'https://www.zhihu.com/question/123',
  });
  const ok = await T.extractContent();
  assert.equal(ok, true);
  assert.equal(el('fetch-direct-link').hidden, true);
});

test('extractContent: bare BV synthesizes the standard url', async () => {
  reset();
  el('url').value = '  ' + BV + '  ';
  extractImpl = async () => ({
    content: 'hello', kind: 'bilibili', title: 'T', author: '',
    published: '', format: 'text', url: BILI_URL,
  });
  const ok = await T.extractContent();
  assert.equal(ok, true);
  assert.equal(uiCalls.extract.length, 1);
  assert.equal(uiCalls.extract[0][0], BILI_URL);
  assert.equal(T.flow.selected, BILI_URL);
});

test('extractContent: bare BV inside share text synthesizes too', async () => {
  reset();
  el('url').value = '分享一个视频 ' + BV + ' 很好看';
  extractImpl = async () => ({
    content: 'hello', kind: 'bilibili', title: 'T', author: '',
    published: '', format: 'text', url: BILI_URL,
  });
  const ok = await T.extractContent();
  assert.equal(ok, true);
  assert.equal(uiCalls.extract[0][0], BILI_URL);
});

test('extractContent: no link and no BV warns and extracts nothing', async () => {
  reset();
  el('url').value = 'nothing useful here';
  const ok = await T.extractContent();
  assert.equal(ok, false);
  assert.equal(uiCalls.extract.length, 0);
  assert.deepEqual(fetchCalls, []);
  assert.match(el('extract-message').textContent, /没有识别到支持的链接/);
});

test('extractContent: douyin still takes the video parse path', async () => {
  reset();
  el('url').value = 'https://www.douyin.com/video/123';
  fetchImpl = async () => ({
    ok: true, status: 200,
    json: async () => ({
      title: 'D', platform_name: '抖音', quality: 'q',
      play_url: 'https://cdn.example.com/v.mp4',
      referer: 'https://www.douyin.com/video/123',
      filename: 'douyin_123.mp4', audio_only: false,
    }),
  });
  const ok = await T.extractContent();
  assert.equal(ok, true);
  assert.equal(uiCalls.extract.length, 0);
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0][0], '/api/video/parse');
  assert.equal(
    JSON.parse(fetchCalls[0][1].body).text,
    'https://www.douyin.com/video/123');
  assert.equal(el('video-panel').hidden, false);
});

// ---- renderVideoResult ----

test('renderVideoResult: fills the video panel', () => {
  reset();
  T.renderVideoResult({
    title: 'T', id: BV, platform_name: 'B站', quality: '最低画质',
    play_url: 'https://cdn.example.com/v.mp4',
    referer: BILI_URL, filename: 'bilibili_' + BV + '.mp4',
    audio_only: false,
  });
  assert.equal(el('video-title').textContent, 'T');
  assert.equal(el('video-platform').textContent, 'B站 · 最低画质');
  assert.equal(el('video-player').src, 'https://cdn.example.com/v.mp4');
  assert.match(el('video-download').href, /^\/api\/video\/stream\?/);
  assert.equal(el('video-download').textContent, '下载视频');
  assert.equal(el('video-open').href, 'https://cdn.example.com/v.mp4');
  assert.equal(el('video-result').hidden, false);
  assert.equal(el('video-panel').hidden, false);
});

test('renderVideoResult: audio-only label', () => {
  reset();
  T.renderVideoResult({
    title: 'T', id: BV, platform_name: 'B站', quality: 'q',
    play_url: 'https://cdn.example.com/a.m4a', referer: BILI_URL,
    filename: 'a.m4a', audio_only: true,
  });
  assert.equal(el('video-download').textContent, '下载音频');
});

// ---- lazy direct-link flow ----

test('lazy link: click resolves through parse and renders the panel', async () => {
  reset();
  T.setCurrentResult({url: BILI_URL, content: 'x'});
  fetchImpl = async () => ({
    ok: true, status: 200,
    json: async () => ({
      title: 'T', platform_name: 'B站', quality: 'q',
      play_url: 'https://cdn.example.com/v.mp4', referer: BILI_URL,
      filename: 'bilibili_' + BV + '.mp4', audio_only: false,
    }),
  });
  await Promise.all(el('fetch-direct-link').__fire('click'));
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0][0], '/api/video/parse');
  assert.equal(JSON.parse(fetchCalls[0][1].body).text, BILI_URL);
  assert.equal(el('video-player').src, 'https://cdn.example.com/v.mp4');
  assert.equal(el('video-result').hidden, false);
  assert.equal(el('fetch-direct-link').disabled, false);
});

test('lazy link: subtitle-only parse shows the no-link message', async () => {
  reset();
  T.setCurrentResult({url: BILI_URL, content: 'x'});
  fetchImpl = async () => ({
    ok: true, status: 200,
    json: async () => ({
      title: 'T', platform_name: 'B站', kind: 'subtitle',
      subtitle_text: 'SUB', subtitle_lang: '中文',
    }),
  });
  await Promise.all(el('fetch-direct-link').__fire('click'));
  assert.equal(el('video-result').hidden, true);
  assert.match(el('video-message').textContent, /已有字幕/);
});

test('lazy link: click with no result does nothing', async () => {
  reset();
  T.setCurrentResult(null);
  await Promise.all(el('fetch-direct-link').__fire('click'));
  assert.deepEqual(fetchCalls, []);
});

(async () => {
  let failures = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log('ok - ' + name);
    } catch (error) {
      failures += 1;
      console.log('FAIL - ' + name);
      console.log(error && error.stack ? error.stack : String(error));
    }
  }
  console.log(tests.length - failures + '/' + tests.length + ' passed');
  process.exitCode = failures ? 1 : 0;
})();
