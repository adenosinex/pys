"""Page template ported from the user's myweb chat blueprint
(共享剪贴板): markup, styles and behaviour preserved; the API endpoint
is derived from the page URL so the page works under the App Hub
prefix, and an auth gate (panel SSO or this project's own account)
was added in front of the chat logic."""

PAGE_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>共享剪贴板</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto,
                 "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #0f1116;
    color: #e6e8ee;
  }
  .wrap {
    height: 100%;
    max-width: 780px;
    margin: 0 auto;
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  header { display: flex; align-items: center; gap: 10px; }
  h1 { font-size: 17px; margin: 0; font-weight: 600; }
  #status {
    font-size: 12px; padding: 2px 9px; border-radius: 99px;
    background: #1e2430; color: #8b93a7; transition: .2s;
  }
  #status.on  { background: #12301f; color: #5ee08a; }
  #status.off { background: #3a1d1d; color: #ff8b8b; }

  /* ---------------- 消息区 ---------------- */
  #messages {
    flex: 1;
    overflow-y: auto;
    background: #161a22;
    border: 1px solid #232a37;
    border-radius: 12px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .msg {
    position: relative;
    background: #1e2430;
    border-radius: 10px;
    padding: 7px 11px;
    max-width: 80%;
    align-self: flex-start;
    word-break: break-word;
    white-space: pre-wrap;
    cursor: pointer;                 /* 点击可复制 */
    transition: background .15s, transform .08s;
    animation: pop .15s ease-out;
  }
  .msg:hover  { background: #262e3c; }
  .msg:active { transform: scale(.985); }
  .msg.me { background: #1d3a55; align-self: flex-end; }
  .msg.me:hover { background: #24486a; }

  /* 复制成功徽标 */
  .msg.copied::after {
    content: "已复制";
    position: absolute;
    top: -9px; right: 8px;
    font-size: 10px;
    line-height: 1.5;
    padding: 0 7px;
    border-radius: 99px;
    background: #2f6fd0;
    color: #fff;
    white-space: nowrap;
    box-shadow: 0 2px 6px rgba(0,0,0,.35);
    animation: pop .15s ease-out;
  }
  .msg.copied { background: #24486a; }

  .meta { font-size: 11px; color: #8b93a7; margin-bottom: 2px; }
  .empty { color: #5d6577; font-size: 13px; margin: auto; }
  @keyframes pop { from { opacity: 0; transform: translateY(4px); } }

  /* ---------------- 快捷按钮 ---------------- */
  .quick { display: flex; flex-wrap: wrap; gap: 6px; }
  .quick button {
    background: #1e2430;
    color: #c3cad9;
    border: 1px solid #2b3444;
    border-radius: 99px;
    padding: 5px 12px;
    font: inherit;
    font-size: 12px;
    line-height: 1.6;
    cursor: pointer;
    transition: .15s;
    -webkit-tap-highlight-color: transparent;
  }
  .quick button:hover  { background: #26303f; border-color: #3d6ea8; color: #fff; }
  .quick button:active { transform: scale(.96); }
  .quick button:disabled { opacity: .45; cursor: default; }
  .quick button.waiting {
    border-color: #d09a2f;
    color: #ffd479;
    animation: breathe 1.2s ease-in-out infinite;
  }
  @keyframes breathe { 50% { opacity: .55; } }

  /* ---------------- 输入区 ---------------- */
  form { display: flex; gap: 8px; }
  input {
    background: #161a22; border: 1px solid #232a37; color: inherit;
    padding: 10px 12px; border-radius: 10px; font: inherit; outline: none;
  }
  input:focus { border-color: #3d6ea8; }
  #name { width: 92px; flex: none; }
  #text { flex: 1; min-width: 0; }
  #text.pasted { animation: pasteFlash .5s ease-out; }
  @keyframes pasteFlash {
    0%   { background: #24486a; border-color: #3d6ea8; }
    100% { background: #161a22; border-color: #232a37; }
  }
  button[type="submit"] {
    background: #2f6fd0; color: #fff; border: 0; border-radius: 10px;
    padding: 0 18px; font: inherit; cursor: pointer;
  }
  button[type="submit"]:hover { background: #3a80e8; }
  button[type="submit"]:disabled { opacity: .5; cursor: default; }

  /* ---------------- 浮动提示 ---------------- */
  #toast {
    position: fixed;
    left: 50%;
    bottom: 92px;
    transform: translateX(-50%) translateY(10px);
    background: #232a37;
    color: #e6e8ee;
    border: 1px solid #2f3a4c;
    font-size: 13px;
    line-height: 1.5;
    padding: 8px 16px;
    border-radius: 99px;
    max-width: 88vw;
    text-align: center;
    opacity: 0;
    pointer-events: none;
    transition: opacity .2s, transform .2s;
    z-index: 50;
    box-shadow: 0 6px 20px rgba(0,0,0,.4);
  }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

  /* ---------------- 登录遮罩（App Hub 统一验证） ---------------- */
  #authGate {
    position: fixed; inset: 0; z-index: 100;
    background: rgba(10, 12, 16, .92);
    display: none; align-items: center; justify-content: center;
  }
  #authGate.show { display: flex; }
  #authGate .card {
    width: min(92vw, 340px);
    background: #161a22; border: 1px solid #232a37; border-radius: 14px;
    padding: 22px 20px; display: flex; flex-direction: column; gap: 10px;
  }
  #authGate h2 { margin: 0 0 4px; font-size: 16px; }
  #authGate p { margin: 0; font-size: 12px; color: #8b93a7; }
  #authGate input {
    background: #0f1116; border: 1px solid #232a37; color: inherit;
    padding: 10px 12px; border-radius: 10px; font: inherit; outline: none; width: 100%;
  }
  #authGate input:focus { border-color: #3d6ea8; }
  #authGate button {
    background: #2f6fd0; color: #fff; border: 0; border-radius: 10px;
    padding: 10px; font: inherit; cursor: pointer;
  }
  #authGate .err { color: #ff8b8b; font-size: 12px; min-height: 15px; }
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

<div id="authGate">
  <div class="card">
    <h2>共享剪贴板 · 验证</h2>
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
<div class="wrap">
  <header>
    <h1>共享剪贴板</h1>
    <span id="status">连接中…</span>
  </header>

  <div id="messages"><div class="empty">还没有消息，来说第一句吧 ~</div></div>

  <!-- 快捷按钮：点击后读取剪贴板 → 粘贴 → 自动发送 -->
  <div class="quick" id="quick"></div>

  <form id="form" autocomplete="off">
    <input id="name" maxlength="20" placeholder="昵称">
    <input id="text" maxlength="1000"
           placeholder="说点什么…（点击上方气泡可复制消息）" required>
    <button id="send" type="submit">发送</button>
  </form>
</div>

<div id="toast"></div>

<script>
(function () {
  "use strict";

  const BASE   = location.pathname.endsWith("/") ? location.pathname : location.pathname + "/";
  const API    = BASE + "api/messages";
  const QUICK  = ["📋 发送剪贴板内容"];

  const box      = document.getElementById("messages");
  const form     = document.getElementById("form");
  const nameEl   = document.getElementById("name");
  const textEl   = document.getElementById("text");
  const sendEl   = document.getElementById("send");
  const status   = document.getElementById("status");
  const quickBox = document.getElementById("quick");
  const toastEl  = document.getElementById("toast");

  const POLL_MS = 1000;      // 轮询间隔（毫秒）
  const COPY_HL = 1100;      // “已复制”徽标显示时长（毫秒）

  let lastId      = 0;       // 已收到的最大消息 id
  let myName      = localStorage.getItem("chat_name") || "";
  let inFlight    = false;   // 拉取中标记
  let sending     = false;   // 发送中标记
  let pendingPaste = false;  // 等待用户手动粘贴（剪贴板读取失败时的兜底）
  let timer       = null;
  let toastTimer  = null;

  nameEl.value = myName;

  /* ------------------------------------------------------------------ */
  /* 工具函数                                                           */
  /* ------------------------------------------------------------------ */

  function setStatus(ok) {
    status.classList.toggle("on", ok);
    status.classList.toggle("off", !ok);
    status.textContent = ok ? "已连接" : "连接断开，重试中…";
  }

  function fmtTime(ts) {
    try { return new Date(ts * 1000).toLocaleTimeString(); }
    catch (e) { return ""; }
  }

  function isNearBottom() {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  }

  function scrollToBottom() {
    box.scrollTop = box.scrollHeight;
  }

  function toast(msg, ms) {
    ms = ms || 2200;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove("show");
    }, ms);
  }

  function flashPaste() {
    textEl.classList.remove("pasted");
    void textEl.offsetWidth;              // 强制重排以重启动画
    textEl.classList.add("pasted");
    setTimeout(function () { textEl.classList.remove("pasted"); }, 500);
  }

  /* ------------------------------------------------------------------ */
  /* 渲染消息                                                           */
  /* ------------------------------------------------------------------ */

  function appendMessage(m) {
    const empty = box.querySelector(".empty");
    if (empty) empty.remove();

    const el = document.createElement("div");
    el.className = "msg" + (myName && m.name === myName ? " me" : "");
    el.dataset.raw = m.text;            // 复制时取这份原始文本
    el.title = "点击复制这条消息";

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = m.name + " · " + fmtTime(m.ts);

    const body = document.createElement("div");
    body.textContent = m.text;          // textContent 天然防 XSS

    el.appendChild(meta);
    el.appendChild(body);
    box.appendChild(el);
  }

  /* ------------------------------------------------------------------ */
  /* 点击消息 → 复制到剪贴板                                            */
  /* ------------------------------------------------------------------ */

  async function copyText(text) {
    // 优先使用异步剪贴板 API（需要 HTTPS 或 localhost）
    if (navigator.clipboard && navigator.clipboard.writeText
        && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    // 退化方案：临时 textarea + execCommand
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    if (!ok) throw new Error("copy failed");
    return true;
  }

  function flashCopied(el) {
    el.classList.add("copied");
    setTimeout(function () { el.classList.remove("copied"); }, COPY_HL);
  }

  box.addEventListener("click", async function (e) {
    const el = e.target.closest(".msg");
    if (!el) return;

    // 用户正在划词选择时，不触发整条复制
    const sel = window.getSelection();
    if (sel && sel.toString().trim()) return;

    const raw = el.dataset.raw || el.textContent || "";
    if (!raw.trim()) return;

    try {
      await copyText(raw);
      flashCopied(el);
    } catch (err) {
      // 复制失败时把内容塞进输入框，方便手动 Ctrl+C
      textEl.value = raw;
      textEl.focus();
      textEl.select();
      setStatus(false);
      toast("复制失败，已填入输入框，可手动复制");
    }
  });

  /* ------------------------------------------------------------------ */
  /* 拉取增量消息                                                       */
  /* ------------------------------------------------------------------ */

  async function poll() {
    if (!window.__clipAuthed) return;
    if (inFlight) return;
    inFlight = true;
    try {
      const res = await fetch(API + "?since=" + lastId, { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);

      const data = await res.json();
      const list = data.messages || [];

      if (list.length) {
        const stick = isNearBottom();       // 只有本来就在底部才自动滚动
        for (const m of list) appendMessage(m);
        if (stick) scrollToBottom();
      }
      if (typeof data.last_id === "number" && data.last_id > lastId) {
        lastId = data.last_id;
      }
      setStatus(true);
    } catch (err) {
      setStatus(false);
    } finally {
      inFlight = false;
      clearTimeout(timer);
      timer = setTimeout(poll, POLL_MS);
    }
  }

  /* ------------------------------------------------------------------ */
  /* 发送消息                                                           */
  /* ------------------------------------------------------------------ */

  async function sendMessage(text) {
    text = (text || "").trim();
    if (!text || sending) return false;

    myName = nameEl.value.trim() || "匿名";
    localStorage.setItem("chat_name", myName);

    sending = true;
    sendEl.disabled = true;
    setQuickDisabled(true);

    let ok = false;
    try {
      const res = await fetch(API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: myName, text: text })
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      ok = true;
      setStatus(true);
    } catch (err) {
      setStatus(false);
    } finally {
      sending = false;
      sendEl.disabled = false;
      setQuickDisabled(false);
      poll();               // 立刻补拉一次，减少等待感
    }
    return ok;
  }

  // 表单提交
  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const text = textEl.value.trim();
    if (!text) return;

    textEl.value = "";
    const ok = await sendMessage(text);
    if (!ok) {
      textEl.value = text;   // 失败把内容还回去
      toast("发送失败，请检查网络后重试");
    }
  });

  /* ------------------------------------------------------------------ */
  /* 快捷按钮：读取剪贴板 → 粘贴 → 自动发送                              */
  /* ------------------------------------------------------------------ */

  const quickBtns = [];

  function setQuickDisabled(disabled) {
    for (const b of quickBtns) {
      if (disabled) b.disabled = true;
      if (!disabled) b.disabled = false;
    }
  }

  function setQuickWaiting(waiting) {
    for (const b of quickBtns) {
      b.classList.toggle("waiting", waiting);
    }
  }

  // 读取系统剪贴板文本。浏览器要求在安全上下文 + 用户手势下调用。
  async function readClipboardText() {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      throw new Error("Clipboard API 不可用");
    }
    if (!window.isSecureContext) {
      throw new Error("非安全上下文（需 HTTPS 或 localhost）");
    }
    return await navigator.clipboard.readText();
  }

  // 把文本「粘贴」到输入框并发送
  async function pasteAndSend(text) {
    text = (text || "").trim();
    if (!text) {
      toast("剪贴板是空的，没有可发送的内容");
      return false;
    }

    // 1) 自动“粘贴”到输入框，闪烁一下作为视觉反馈
    textEl.value = text;
    flashPaste();

    // 2) 自动发送；成功后清空输入框，失败则保留内容便于手动重试
    const ok = await sendMessage(text);
    if (ok) {
      textEl.value = "";
      pendingPaste = false;
      setQuickWaiting(false);
    } else {
      toast("发送失败，内容已保留在输入框，可手动重试");
    }
    return ok;
  }

  async function quickSendFromClipboard() {
    if (sending) return;

    let text = "";
    try {
      text = await readClipboardText();
    } catch (err) {
      // 兜底：无法直接读取剪贴板 → 引导用户手动粘贴，粘贴后自动发送
      pendingPaste = true;
      setQuickWaiting(true);
      textEl.value = "";
      textEl.focus();
      toast("浏览器不允许直接读取剪贴板，请按 Ctrl/⌘+V 粘贴，将自动发送", 4000);
      return;
    }

    await pasteAndSend(text);
  }

  // 手动粘贴兜底：捕获 paste 事件后直接发送
  textEl.addEventListener("paste", function (e) {
    if (!pendingPaste) return;

    const cd = e.clipboardData || window.clipboardData;
    if (!cd) return;

    const t = cd.getData("text");
    if (!t || !t.trim()) return;

    e.preventDefault();          // 阻止默认插入，改用统一流程
    pendingPaste = false;
    pasteAndSend(t);
  });

  // 用户主动输入 / 按 Esc 时取消等待状态
  textEl.addEventListener("input", function () {
    if (pendingPaste && textEl.value.trim()) {
      pendingPaste = false;
      setQuickWaiting(false);
    }
  });
  textEl.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && pendingPaste) {
      pendingPaste = false;
      setQuickWaiting(false);
      toast("已取消", 1200);
    }
  });

  // 创建快捷按钮
  QUICK.forEach(function (label) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.title = "点击读取剪贴板并自动发送";
    b.addEventListener("click", quickSendFromClipboard);
    quickBox.appendChild(b);
    quickBtns.push(b);
  });

  /* ------------------------------------------------------------------ */
  /* 其他                                                               */
  /* ------------------------------------------------------------------ */

  // 页面重新可见时立刻同步一次（后台标签页被节流后补拉）
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });

  window.__startChat = function () {
    window.__clipAuthed = true;
    textEl.focus();
    poll();
  };
})();
</script>


</body>
</html>
'''
