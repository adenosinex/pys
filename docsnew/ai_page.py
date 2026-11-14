# -*- coding: utf-8 -*-
"""AI 控制台页面：独立页面，不依赖 Vue/Element，轻量直达。

任务句：打开这页，我要批量给笔记生成摘要并分类标题，所以首屏
必须让我看到 AI 是否就绪、有多少篇待处理、以及一键开始。
"""
AI_CONSOLE_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>AI 控制台 · 知识笔记</title>
<style>
:root {
  --bg: #f4f6f9; --card: #fff; --text: #1c2330; --muted: #6b7484;
  --line: #dfe4ec; --accent: #2f6fd0; --accent-t: #fff;
  --ok: #2c7a3f; --ok-bg: #e6f4ea; --warn: #9a6700; --warn-bg: #fef3d9;
  --bad: #b3261e; --bad-bg: #fdecea; --chip: #eef1f6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151c; --card: #1b1f2a; --text: #e8eaf0; --muted: #9aa3b5;
    --line: #2c3242; --accent: #5b93e6; --accent-t: #0b1220;
    --ok: #5fbf77; --ok-bg: #17301d; --warn: #e0a83c; --warn-bg: #3a2c10;
    --bad: #f28b82; --bad-bg: #3d1a18; --chip: #252b3a;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
.wrap { max-width: 880px; margin: 0 auto; padding: 16px 14px 40px; }
.topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.topbar h1 { font-size: 20px; margin: 0; }
.topbar a { color: var(--accent); text-decoration: none; font-size: 14px; }
.status { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.badge { display: inline-block; padding: 5px 10px; border-radius: 999px;
  background: var(--chip); font-size: 13px; }
.badge.ok { background: var(--ok-bg); color: var(--ok); }
.badge.warn { background: var(--warn-bg); color: var(--warn); }
.badge.bad { background: var(--bad-bg); color: var(--bad); }
.alert { background: var(--warn-bg); color: var(--warn); border-radius: 10px;
  padding: 10px 12px; font-size: 14px; margin-bottom: 12px; }
.alert button { margin-left: 8px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 14px; margin-bottom: 12px; }
.card h2 { font-size: 16px; margin: 0 0 4px; }
.card .desc { color: var(--muted); font-size: 13px; margin: 0 0 10px; }
.opts { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center;
  font-size: 14px; margin-bottom: 10px; }
.opts label { display: flex; align-items: center; gap: 6px; min-height: 44px; }
.opts input[type="number"], .opts select, .opts input[type="text"] {
  padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--card); color: var(--text); font-size: 14px; min-height: 44px; }
.opts input[type="checkbox"] { width: 20px; height: 20px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
button { min-height: 44px; padding: 8px 18px; border-radius: 8px; font-size: 14px;
  cursor: pointer; border: 1px solid var(--line); background: var(--card); color: var(--text); }
button.primary { background: var(--accent); color: var(--accent-t); border: 0; }
button:disabled { opacity: .5; cursor: default; }
.progress { margin-top: 10px; display: none; }
.progress.show { display: block; }
.bar { height: 8px; border-radius: 4px; background: var(--chip); overflow: hidden; }
.bar > i { display: block; height: 100%; width: 0; background: var(--accent);
  transition: width .3s; }
.bar.indet > i { width: 40%; animation: slide 1.2s ease-in-out infinite; }
@keyframes slide { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
.ptime { font-size: 13px; color: var(--muted); margin-top: 6px; }
.counts { font-size: 14px; margin-top: 10px; }
.joblist { margin-top: 8px; border-top: 1px solid var(--line); }
.jobrow { display: flex; gap: 8px; align-items: baseline; padding: 8px 0;
  border-bottom: 1px solid var(--line); font-size: 14px; }
.jobrow .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.jobrow .reason { color: var(--muted); font-size: 12px; max-width: 45%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mini { font-size: 12px; color: var(--muted); margin-top: 6px; }
details { margin-top: 10px; }
details summary { cursor: pointer; min-height: 44px; display: flex;
  align-items: center; font-size: 14px; color: var(--accent); }
pre.log { background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px; font-size: 12px; line-height: 1.6; max-height: 300px;
  overflow: auto; white-space: pre-wrap; word-break: break-word; margin: 6px 0 0; }
.formgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
.formgrid label { font-size: 13px; color: var(--muted); display: block; }
.formgrid input, .formgrid select { width: 100%; margin-top: 4px; padding: 8px 10px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card);
  color: var(--text); font-size: 14px; min-height: 44px; }
.keyrow { display: flex; gap: 8px; align-items: end; flex-wrap: wrap; margin-bottom: 10px; }
.keyrow .grow { flex: 1; min-width: 200px; }
.keyrow label { font-size: 13px; color: var(--muted); display: block; }
.keyrow input { width: 100%; margin-top: 4px; padding: 8px 10px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card);
  color: var(--text); font-size: 14px; min-height: 44px; }
.hint { font-size: 13px; color: var(--muted); }
.empty { color: var(--muted); font-size: 14px; padding: 8px 0; }
@media (max-width: 640px) {
  .formgrid { grid-template-columns: 1fr; }
  .actions button { flex: 1; }
  .jobrow .reason { display: none; }
}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <a id="back-link" href="#">← 笔记</a>
    <h1>AI 控制台</h1>
  </div>

  <div class="status" id="status-strip">
    <span class="badge" id="st-provider">加载中…</span>
    <span class="badge" id="st-key">凭据加载中…</span>
    <span class="badge" id="st-process">待处理：…</span>
  </div>
  <div class="alert" id="key-alert" hidden>
    当前 Provider 未配置 Key，作业无法运行。请先保存凭据（环境变量优先）。
    <button type="button" id="goto-creds">去设置凭据</button>
  </div>

  <div class="card" id="card-process">
    <h2>一键处理</h2>
    <p class="desc">逐篇处理：每篇先生成快速浏览卡摘要（并用主题重写标题），再加分类前缀，一次保存。多篇并发进行。</p>
    <div class="opts">
      <label><input type="checkbox" id="opt-recent" checked> 3 小时内结构不合格也重新生成</label>
      <label><input type="checkbox" id="opt-force"> 强制重新分类已有前缀的笔记</label>
      <label><input type="checkbox" id="opt-dry"> 试运行（只看结果，不保存）</label>
      <label>标题样式
        <select id="opt-style">
          <option value="bracket">[pol pow] 标题</option>
          <option value="fullwidth">［pol pow］标题</option>
          <option value="space">pol pow 标题</option>
        </select>
      </label>
      <label>数量上限 <input type="number" id="opt-limit" value="0" min="0" step="1" style="width:90px"> <span class="hint">0 = 全部</span></label>
    </div>
    <div class="actions">
      <button type="button" id="btn-scan-process">扫描预览</button>
      <button type="button" class="primary" id="btn-run-process">开始处理</button>
    </div>
    <div class="progress" id="prog-process">
      <div class="bar" id="bar-process"><i></i></div>
      <div class="ptime" id="ptime-process"></div>
    </div>
    <div class="counts" id="counts-process"></div>
    <div class="joblist" id="list-process"></div>
    <div class="mini" id="meta-process"></div>
    <details id="logbox-process">
      <summary>运行日志</summary>
      <pre class="log" id="log-process"></pre>
    </details>
  </div>

  <div class="card">
    <h2>配置</h2>
    <p class="desc">模型与运行参数。凭据不在这里保存，见下方凭据区。</p>
    <div class="formgrid">
      <label>Provider
        <select id="cfg-provider">
          <option value="flash">3.8 Flash（本机，默认）</option>
          <option value="openrouter">OpenRouter（云端）</option>
          <option value="local">本地模型（局域网）</option>
        </select>
      </label>
      <label>并发数 <input type="number" id="cfg-concurrency" min="1" max="12" step="1"></label>
      <label>Flash 档位
        <select id="cfg-flash-model">
          <option value="gemini-3.8-flash-high">高（high）</option>
          <option value="gemini-3.8-flash-medium">中（medium）</option>
          <option value="gemini-3.8-flash-low">低（low）</option>
        </select>
      </label>
      <label>Flash 地址 <input type="text" id="cfg-flash-url"></label>
      <label>Flash 超时（秒） <input type="number" id="cfg-flash-timeout" min="5" max="600" step="1"></label>
      <label>OpenRouter 模型 <input type="text" id="cfg-or-model"></label>
      <label>OpenRouter 地址 <input type="text" id="cfg-or-url"></label>
      <label>OpenRouter 超时（秒） <input type="number" id="cfg-or-timeout" min="5" max="600" step="1"></label>
      <label>本地模型 <input type="text" id="cfg-local-model"></label>
      <label>本地地址 <input type="text" id="cfg-local-url"></label>
      <label>本地超时（秒） <input type="number" id="cfg-local-timeout" min="5" max="600" step="1"></label>
      <label>单篇输入上限（字） <input type="number" id="cfg-maxchars" min="1000" max="100000" step="1000"></label>
    </div>
    <div class="actions" style="margin-top:12px">
      <button type="button" class="primary" id="btn-save-cfg">保存配置</button>
      <span class="hint" id="cfg-msg"></span>
    </div>
    <p class="hint">本地 Provider 的并发会被限制在 2 以内（与原脚本的摘要上限一致，保护本地端点）。Flash 走本机接口，无需凭据；接口一次只处理一个请求，并发数对它不提速。</p>
  </div>

  <div class="card" id="creds-card">
    <h2>凭据</h2>
    <p class="desc">只保存在服务器内部使用，页面永不回显已保存的值。环境变量优先于页面保存的值。使用 Flash（默认）时不需要凭据。</p>
    <div class="keyrow">
      <div class="grow">
        <label>OpenRouter Key <span id="st-op-key"></span>
          <input type="password" id="in-op-key" autocomplete="off" placeholder="sk-or-…">
        </label>
      </div>
      <button type="button" data-key="OP_API_KEY" class="key-save">保存</button>
      <button type="button" data-key="OP_API_KEY" class="key-clear">清除</button>
    </div>
    <div class="keyrow">
      <div class="grow">
        <label>本地模型 Key <span id="st-local-key"></span>
          <input type="password" id="in-local-key" autocomplete="off" placeholder="本地端点的 Key">
        </label>
      </div>
      <button type="button" data-key="LOCAL_API_KEY" class="key-save">保存</button>
      <button type="button" data-key="LOCAL_API_KEY" class="key-clear">清除</button>
    </div>
    <div class="hint" id="creds-msg"></div>
  </div>

  <div class="card">
    <h2>最近运行</h2>
    <div id="jobs-list"><div class="empty">加载中…</div></div>
  </div>

  <div class="card">
    <details>
      <summary>处理规则（与原批处理脚本一致）</summary>
      <div class="hint" style="line-height:1.8">
        <p><b>流程</b>：逐篇处理，每篇先摘要、后分类，最多保存一次；任一阶段失败则整篇不写，下次可重试。多篇按配置的并发数同时处理。</p>
        <p><b>摘要</b>：默认标题（空标题、随记_、note_ 开头）的笔记全部纳入；非默认标题只纳入 3 小时内更新的（开关可关）。正文前 300 字已是完整快速浏览卡（【主题】+【关键词】+【关键句】+【结论】）的跳过；顶部已有总结但结构不合格的，只在 3 小时内重新生成，旧总结保留在新总结后面。标题取 AI 返回的【主题】行。</p>
        <p><b>分类</b>：只处理正文开头 500 字内已有【主题】【关键词】【结论】的笔记；已有分类前缀的跳过（除非勾选强制重新分类）；已处理过的笔记记入缓存，不重复处理。摘要重写了标题的笔记一定会分类（新标题未经分类，旧缓存不再适用）。分类只看标题 + 正文前 200 字，输出两个小写英文代号拼成前缀；标题样式决定前缀形态，单引号包裹保证 YAML 安全。</p>
      </div>
    </details>
  </div>
</div>

<script>
(function () {
  var base = location.pathname.replace(/\/ai\/?$/, "");
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function fmtClock(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return (d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function api(path, opts) {
    return fetch(base + path, opts).then(function (r) { return r.json(); });
  }

  // ---- P12 waiting: show only after 300ms, ticking timer, seq guard ----
  var waitSeq = {process: 0};
  function waitStart(job, determinate) {
    var seq = ++waitSeq[job];
    var prog = $("prog-" + job), bar = $("bar-" + job), pt = $("ptime-" + job);
    var t0 = Date.now(), shown = false, timer = null;
    var to = setTimeout(function () {
      shown = true;
      prog.classList.add("show");
      bar.classList.toggle("indet", !determinate);
      timer = setInterval(function () {
        pt.textContent = "已用 " + ((Date.now() - t0) / 1000).toFixed(1) + " 秒";
      }, 100);
    }, 300);
    return {
      seq: seq,
      setProgress: function (done, total) {
        if (waitSeq[job] !== seq || !shown) return;
        bar.querySelector("i").style.width = (total ? Math.round(done / total * 100) : 0) + "%";
        pt.textContent = "已处理 " + done + " / " + total + " · 已用 " + ((Date.now() - t0) / 1000).toFixed(1) + " 秒";
      },
      finish: function (msg) {
        if (waitSeq[job] !== seq) return;
        clearTimeout(to);
        if (timer) clearInterval(timer);
        if (shown) {
          pt.textContent = msg + " · 共用 " + ((Date.now() - t0) / 1000).toFixed(1) + " 秒";
          setTimeout(function () { if (waitSeq[job] === seq) prog.classList.remove("show"); }, 4000);
        }
      },
      hide: function () {
        clearTimeout(to);
        if (timer) clearInterval(timer);
        if (waitSeq[job] === seq) prog.classList.remove("show");
      }
    };
  }

  var state = {
    process: {running: false, jobId: null, logSeen: 0, poller: null, wait: null}
  };

  function setBusy(job, busy) {
    state[job].running = busy;
    $("btn-run-process").disabled = busy;
    $("btn-scan-process").disabled = busy;
  }

  function badge(text, cls) {
    return '<span class="badge ' + (cls || "") + '">' + esc(text) + "</span>";
  }

  function renderScan(job, d) {
    var counts = d.counts || {};
    var parts = [];
    parts.push("待处理 " + (counts.process || 0) + " 篇");
    parts.push("其中摘要 " + (counts.summary_process || 0) + " 篇");
    parts.push("分类 " + (counts.classify_ready || 0) + " 篇");
    parts.push("跳过 " + (counts.skip || 0) + " 篇");
    parts.push("缓存跳过 " + (counts.cache_skipped || 0) + " 篇");
    parts.push("已有前缀跳过 " + (counts.prefix_skipped || 0) + " 篇");
    if (counts.error) parts.push("异常 " + counts.error + " 篇");
    $("counts-" + job).textContent = parts.join(" · ");
    var list = $("list-" + job);
    var rows = (d.targets || []).slice(0, 50).map(function (t) {
      var tags = [];
      if (t.summary === "process") tags.push(badge("摘要", "ok"));
      else if (t.summary === "error") tags.push(badge("摘要异常", "bad"));
      if (t.classify === "ready") tags.push(badge("分类", "ok"));
      var reason = t.summary === "process" ? t.summary_reason : t.classify_reason;
      return '<div class="jobrow"><span class="t">' + esc(t.title || "(无标题)") + "</span>" + tags.join("") +
        (reason ? '<span class="reason">' + esc(reason) + "</span>" : "") + "</div>";
    }).join("");
    list.innerHTML = rows || '<div class="empty">没有符合条件的笔记。</div>';
    $("meta-" + job).textContent = "共 " + d.total_docs + " 篇笔记 · 扫描用时 " + (d.scan_ms || 0) + " 毫秒 · " +
      (d.targets_total > (d.targets || []).length ? "只显示前 " + (d.targets || []).length + " 条，共 " + d.targets_total + " 条" : "已全部显示");
    $("st-process").textContent = "待处理：" + (counts.process || 0);
  }

  function renderResults(job, results) {
    var list = $("list-" + job);
    var rows = (results || []).slice(0, 100).map(function (r) {
      var b = r.outcome === "success" ? badge("成功", "ok") : r.outcome === "skip" ? badge("跳过", "") : badge("失败", "bad");
      var det = [];
      if (r.summary_detail) det.push("摘要：" + r.summary_detail);
      if (r.classify_detail) det.push("分类：" + r.classify_detail);
      var detail = r.detail || det.join("；");
      return '<div class="jobrow"><span class="t">' + esc(r.title || "(无标题)") + "</span>" + b +
        (detail ? '<span class="reason">' + esc(detail) + "</span>" : "") + "</div>";
    }).join("");
    list.innerHTML = rows || '<div class="empty">没有处理记录。</div>';
    if ((results || []).length > 100) {
      $("meta-" + job).textContent = "只显示前 100 条，共 " + results.length + " 条";
    }
  }

  function appendLog(job, lines) {
    if (!lines || !lines.length) return;
    var pre = $("log-" + job);
    pre.textContent += lines.join("\n") + "\n";
    pre.scrollTop = pre.scrollHeight;
  }

  function scanJob(job) {
    var w = waitStart(job, false);
    var body = {job: job};
    body.recent_3h_extra = $("opt-recent").checked;
    body.force_reclassify = $("opt-force").checked;
    return api("/docapi/ai/scan", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(function (d) {
        w.finish("扫描完成");
        if (!d.ok) {
          $("counts-" + job).textContent = "扫描失败：" + (d.error || "未知错误");
          return;
        }
        renderScan(job, d);
      })
      .catch(function (e) { w.hide(); $("counts-" + job).textContent = "扫描失败：" + e.message; });
  }

  function pollStatus(job) {
    var st = state[job];
    api("/docapi/ai/status?job_id=" + encodeURIComponent(st.jobId) + "&after=" + st.logSeen)
      .then(function (d) {
        if (!d.ok) { finishJob(job, "状态查询失败：" + (d.error || "")); return; }
        var j = d.job;
        st.logSeen = j.log_total;
        appendLog(job, j.log);
        if (st.wait) st.wait.setProgress(j.done, j.total);
        if (j.status === "done" || j.status === "failed") {
          finishJob(job, j.status === "done" ? "处理完成" : "作业失败");
          $("counts-" + job).textContent = "成功 " + j.counters.success + " · 跳过 " +
            j.counters.skip + " · 失败 " + j.counters.error + " · 摘要 " +
            (j.counters.summary_success || 0) + " · 分类 " + (j.counters.classify_success || 0) +
            " · 共 " + j.total + " 篇";
          if (j.results) renderResults(job, j.results);
          loadJobs();
          scanJob(job);
        } else {
          st.poller = setTimeout(function () { pollStatus(job); }, 1000);
        }
      })
      .catch(function () { st.poller = setTimeout(function () { pollStatus(job); }, 2000); });
  }

  function finishJob(job, msg) {
    var st = state[job];
    if (st.poller) clearTimeout(st.poller);
    if (st.wait) st.wait.finish(msg);
    st.wait = null;
    setBusy(job, false);
  }

  function runJob(job) {
    var body = {job: job};
    body.recent_3h_extra = $("opt-recent").checked;
    body.force_reclassify = $("opt-force").checked;
    body.dry_run = $("opt-dry").checked;
    body.title_style = $("opt-style").value;
    body.limit = parseInt($("opt-limit").value || "0", 10) || 0;
    setBusy(job, true);
    $("log-" + job).textContent = "";
    $("counts-" + job).textContent = "";
    state[job].wait = waitStart(job, true);
    api("/docapi/ai/run", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(function (d) {
        if (!d.ok) {
          finishJob(job, "启动失败");
          $("counts-" + job).textContent = "启动失败：" + (d.error || "未知错误");
          return;
        }
        state[job].jobId = d.job_id;
        state[job].logSeen = 0;
        pollStatus(job);
      })
      .catch(function (e) {
        finishJob(job, "启动失败");
        $("counts-" + job).textContent = "启动失败：" + e.message;
      });
  }

  function loadConfig() {
    return api("/docapi/ai/config").then(function (d) {
      if (!d.ok) return;
      var c = d.config || {};
      $("cfg-provider").value = c.provider || "flash";
      $("cfg-concurrency").value = c.concurrency;
      $("cfg-flash-model").value = c.flash_model || "gemini-3.8-flash-medium";
      $("cfg-flash-url").value = c.flash_base_url || "";
      $("cfg-flash-timeout").value = c.flash_timeout;
      $("cfg-or-model").value = c.openrouter_model || "";
      $("cfg-or-url").value = c.openrouter_base_url || "";
      $("cfg-or-timeout").value = c.openrouter_timeout;
      $("cfg-local-model").value = c.local_model || "";
      $("cfg-local-url").value = c.local_base_url || "";
      $("cfg-local-timeout").value = c.local_timeout;
      $("cfg-maxchars").value = c.max_input_chars;
      $("opt-recent").checked = !!c.recent_3h_extra;
      $("opt-force").checked = !!c.force_reclassify;
      $("opt-dry").checked = !!c.dry_run;
      $("opt-style").value = c.title_style || "bracket";
      var model = c.provider === "flash" ? c.flash_model : (c.provider === "local" ? c.local_model : c.openrouter_model);
      var pname = c.provider === "flash" ? "3.8 Flash" : (c.provider === "local" ? "本地模型" : "OpenRouter");
      $("st-provider").textContent = pname + " · " + model;
      var keys = d.credentials || {};
      var keyBadge = $("st-key");
      if (c.provider === "flash") {
        keyBadge.textContent = "无需 Key（本机接口）";
        keyBadge.className = "badge ok";
        $("key-alert").hidden = true;
      } else {
        var active = c.provider === "local" ? keys.LOCAL_API_KEY : keys.OP_API_KEY;
        if (active && active.configured) {
          keyBadge.textContent = "Key 已配置（" + (active.source === "env" ? "环境变量" : "页面保存") + "）";
          keyBadge.className = "badge ok";
          $("key-alert").hidden = true;
        } else {
          keyBadge.textContent = "Key 未配置";
          keyBadge.className = "badge bad";
          $("key-alert").hidden = false;
        }
      }
      function keyStatus(el, k) {
        var s = keys[k];
        $(el).textContent = s && s.configured ? "（已配置：" + (s.source === "env" ? "环境变量" : "页面保存") + "）" : "（未配置）";
      }
      keyStatus("st-op-key", "OP_API_KEY");
      keyStatus("st-local-key", "LOCAL_API_KEY");
    });
  }

  function loadJobs() {
    return api("/docapi/ai/jobs").then(function (d) {
      var el = $("jobs-list");
      var jobs = (d && d.jobs) || [];
      if (!jobs.length) {
        el.innerHTML = '<div class="empty">尚未运行过作业。</div>';
        return;
      }
      el.innerHTML = jobs.map(function (j) {
        var name = j.job === "process" ? "一键处理" : (j.job === "summary" ? "自动生成摘要" : "标题分类");
        var b = j.status === "done" ? badge("完成", "ok") : badge("失败", "bad");
        var secs = j.elapsed_ms != null ? (j.elapsed_ms / 1000).toFixed(1) + " 秒" : "";
        return '<div class="jobrow"><span class="t">' + esc(name) + "</span>" + b +
          '<span class="reason">成功 ' + j.counters.success + " · 跳过 " + j.counters.skip +
          " · 失败 " + j.counters.error + " · " + secs + " · " + fmtClock(j.started_at) + "</span></div>";
      }).join("");
    }).catch(function () { $("jobs-list").innerHTML = '<div class="empty">加载失败。</div>'; });
  }

  $("back-link").href = base + "/docs";
  $("goto-creds").addEventListener("click", function () {
    $("creds-card").scrollIntoView({behavior: "smooth", block: "start"});
  });
  $("btn-scan-process").addEventListener("click", function () { scanJob("process"); });
  $("btn-run-process").addEventListener("click", function () { runJob("process"); });
  $("btn-save-cfg").addEventListener("click", function () {
    var btn = $("btn-save-cfg");
    btn.disabled = true;
    var body = {
      provider: $("cfg-provider").value,
      concurrency: parseInt($("cfg-concurrency").value || "6", 10),
      flash_model: $("cfg-flash-model").value,
      flash_base_url: $("cfg-flash-url").value.trim(),
      flash_timeout: parseInt($("cfg-flash-timeout").value || "290", 10),
      openrouter_model: $("cfg-or-model").value.trim(),
      openrouter_base_url: $("cfg-or-url").value.trim(),
      openrouter_timeout: parseInt($("cfg-or-timeout").value || "120", 10),
      local_model: $("cfg-local-model").value.trim(),
      local_base_url: $("cfg-local-url").value.trim(),
      local_timeout: parseInt($("cfg-local-timeout").value || "50", 10),
      max_input_chars: parseInt($("cfg-maxchars").value || "30000", 10),
      recent_3h_extra: $("opt-recent").checked,
      force_reclassify: $("opt-force").checked,
      dry_run: $("opt-dry").checked,
      title_style: $("opt-style").value
    };
    api("/docapi/ai/config", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(function (d) {
        btn.disabled = false;
        $("cfg-msg").textContent = d.ok ? "已保存" : "保存失败：" + (d.error || "");
        if (d.ok) loadConfig();
      })
      .catch(function (e) { btn.disabled = false; $("cfg-msg").textContent = "保存失败：" + e.message; });
  });
  document.querySelectorAll(".key-save").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var key = btn.getAttribute("data-key");
      var input = key === "OP_API_KEY" ? $("in-op-key") : $("in-local-key");
      var v = input.value.trim();
      if (!v) { $("creds-msg").textContent = "请先粘贴 Key"; return; }
      var body = {};
      body[key] = v;
      btn.disabled = true;
      api("/docapi/credentials", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
        .then(function (d) {
          btn.disabled = false;
          input.value = "";
          $("creds-msg").textContent = d.ok ? "已保存" : "保存失败：" + (d.error || "");
          if (d.ok) loadConfig();
        })
        .catch(function (e) { btn.disabled = false; $("creds-msg").textContent = "保存失败：" + e.message; });
    });
  });
  document.querySelectorAll(".key-clear").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var key = btn.getAttribute("data-key");
      var body = {};
      body[key] = "";
      btn.disabled = true;
      api("/docapi/credentials", {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
        .then(function (d) {
          btn.disabled = false;
          $("creds-msg").textContent = d.ok ? "已清除" : "清除失败：" + (d.error || "");
          if (d.ok) loadConfig();
        })
        .catch(function (e) { btn.disabled = false; $("creds-msg").textContent = "清除失败：" + e.message; });
    });
  });

  loadConfig().then(function () { scanJob("process"); });
  loadJobs();
})();
</script>
</body>
</html>
'''
