#!/usr/bin/env python3
# API 账单: Google API spend computed from apirelay request logs.
# Stdlib-only WSGI. No auth in project code; the panel gate is the
# only identity check. Reads the debi relay log directly and merges
# the shoc static JSON export. Cost = prompt * in_rate +
# (total - prompt) * out_rate (total includes reasoning; completion
# is the fallback when total is missing). Empty-token rows and
# unknown models carry cost null, never a guess.
# Test hooks (unset in production): BILL_DEBI_DB, BILL_SHOC_URL.
import json
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

DEBI_DB = os.environ.get(
    "BILL_DEBI_DB", "/srv/apphub/data/projects/apirelay/relay_log.db")
SHOC_URL = os.environ.get(
    "BILL_SHOC_URL",
    "http://208.123.119.252:8080/mesh/relay-usage-shoc.json")
SHOC_TIMEOUT = 10
MAX_ROWS_PER_BOX = 50000
DAYS = 90
CACHE_TTL = 60

# (effective-from UTC date or None, input USD/1M, output USD/1M).
# Entries ascending; the last entry whose date is <= the row date wins.
RATES = {
    "gemini-3.8-flash": [(None, 0.75, 3.75), ("2027-01-01", 1.50, 7.50)],
    "gemini-3.7-flash": [(None, 0.75, 3.75)],
    "gemini-3.5-flash": [(None, 1.50, 9.00)],
    "gemini-4-hydrogen": [(None, 1.50, 7.50)],
    "gemini-4-argon": [(None, 2.0, 10.0), ("2026-12-01", 4.0, 20.0)],
    "gemini-4-carbon": [(None, 2.0, 10.0), ("2026-12-01", 4.0, 20.0)],
}


def _data_dir():
    d = os.environ.get("DATA_DIR")
    if d:
        return d
    here = os.path.dirname(os.path.abspath(__file__))
    link = os.path.join(here, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(here, ".runtime")


DATA_DIR = _data_dir()
CACHE_FILE = os.path.join(DATA_DIR, "shoc_usage_cache.json")


def _to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def strip_model(m):
    if not m:
        return ""
    return m.split("/", 1)[-1] if "/" in m else m


def rate_for(model, date_s):
    table = RATES.get(model)
    if not table:
        return None
    chosen = table[0]
    for eff, i, o in table:
        if eff is not None and date_s >= eff:
            chosen = (eff, i, o)
    return chosen[1], chosen[2]


def price_row(row):
    """Return (output_tokens, cost); either may be None (uncostable)."""
    p = row["prompt_tokens"]
    t = row["total_tokens"]
    c = row["completion_tokens"]
    if p is None:
        return None, None
    if t is not None:
        out = t - p
        if out < 0:
            out = c
    else:
        out = c
    if out is None:
        return None, None
    r = rate_for(row["model"], row["ts"][:10])
    if r is None:
        return out, None
    return out, p * r[0] / 1e6 + out * r[1] / 1e6


def _norm_row(box, rid, ts, model, status, pt, ct, tt, lat):
    row = {
        "box": box,
        "id": rid,
        "ts": ts or "",
        "model": strip_model(model),
        "status": _to_int(status),
        "prompt_tokens": _to_int(pt),
        "completion_tokens": _to_int(ct),
        "total_tokens": _to_int(tt),
        "latency_ms": _to_int(lat),
    }
    out, cost = price_row(row)
    row["output_tokens"] = out
    row["cost"] = cost
    return row


def read_debi_rows():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS))
    cutoff_s = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect("file:%s?mode=ro" % DEBI_DB, uri=True, timeout=5)
    try:
        cur = conn.execute(
            "SELECT id, ts, model, status, prompt_tokens,"
            " completion_tokens, total_tokens, latency_ms"
            " FROM relay_log WHERE ts >= ? ORDER BY id LIMIT ?",
            (cutoff_s, MAX_ROWS_PER_BOX))
        return [_norm_row("debi", *r) for r in cur]
    finally:
        conn.close()


def _shoc_rows_from_payload(payload):
    rows = []
    for r in payload.get("rows") or []:
        rows.append(_norm_row(
            "shoc", r.get("id"), r.get("ts"), r.get("model"),
            r.get("status"), r.get("prompt_tokens"),
            r.get("completion_tokens"), r.get("total_tokens"),
            r.get("latency_ms")))
    return rows


def read_shoc_rows():
    """Return (rows, generated_at, status). Status: fresh/stale/missing."""
    try:
        req = urllib.request.Request(
            SHOC_URL, headers={"User-Agent": "bill/1.0"})
        with urllib.request.urlopen(req, timeout=SHOC_TIMEOUT) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8"))
        rows = _shoc_rows_from_payload(payload)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, CACHE_FILE)
        except OSError:
            pass
        return rows, payload.get("generated_at"), "fresh"
    except Exception:
        pass
    try:
        with open(CACHE_FILE, "rb") as f:
            payload = json.loads(f.read().decode("utf-8"))
        return _shoc_rows_from_payload(payload), \
            payload.get("generated_at"), "stale"
    except Exception:
        return [], None, "missing"


_dataset_lock = threading.Lock()
_dataset_cache = {"at": 0.0, "rows": [], "shoc_at": None, "shoc_status": "missing"}


def get_dataset():
    now = time.time()
    with _dataset_lock:
        if now - _dataset_cache["at"] < CACHE_TTL and _dataset_cache["rows"]:
            return (_dataset_cache["rows"], _dataset_cache["shoc_at"],
                    _dataset_cache["shoc_status"])
        try:
            debi = read_debi_rows()
        except Exception:
            debi = []
        shoc, shoc_at, shoc_status = read_shoc_rows()
        rows = debi + shoc
        rows.sort(key=lambda r: (r["ts"], r["box"], r["id"] or 0),
                  reverse=True)
        _dataset_cache.update({"at": now, "rows": rows,
                               "shoc_at": shoc_at,
                               "shoc_status": shoc_status})
        return rows, shoc_at, shoc_status


def build_summary():
    t0 = time.time()
    rows, shoc_at, shoc_status = get_dataset()
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    cards = {"today_cost": 0.0, "today_requests": 0,
             "month_cost": 0.0, "month_requests": 0, "uncosted": 0}
    today_by_box = {"debi": 0, "shoc": 0}
    boxes = {"debi": 0, "shoc": 0}
    models = {}
    days = {}
    for r in rows:
        boxes[r["box"]] = boxes.get(r["box"], 0) + 1
        d = r["ts"][:10]
        if d == today:
            today_by_box[r["box"]] = today_by_box.get(r["box"], 0) + 1
            cards["today_requests"] += 1
            if r["cost"] is not None:
                cards["today_cost"] += r["cost"]
        if d[:7] == month:
            cards["month_requests"] += 1
            if r["cost"] is not None:
                cards["month_cost"] += r["cost"]
        if r["cost"] is None:
            cards["uncosted"] += 1
        m = models.setdefault(r["model"], {
            "model": r["model"], "requests": 0, "prompt_tokens": 0,
            "output_tokens": 0, "cost": 0.0, "uncosted": 0,
            "priced": rate_for(r["model"], "2999-01-01") is not None})
        m["requests"] += 1
        if r["prompt_tokens"] is not None:
            m["prompt_tokens"] += r["prompt_tokens"]
        if r["output_tokens"] is not None:
            m["output_tokens"] += r["output_tokens"]
        if r["cost"] is not None:
            m["cost"] += r["cost"]
        else:
            m["uncosted"] += 1
        day = days.setdefault(d, {"date": d, "requests": 0,
                                  "cost": 0.0, "uncosted": 0})
        day["requests"] += 1
        if r["cost"] is not None:
            day["cost"] += r["cost"]
        else:
            day["uncosted"] += 1
    model_list = sorted(models.values(),
                        key=lambda m: (-(m["cost"] or 0), -m["requests"]))
    for m in model_list:
        if not m.pop("priced"):
            m["cost"] = None
    day_list = sorted(days.values(), key=lambda x: x["date"], reverse=True)
    day_list = day_list[:30]
    if shoc_status == "missing":
        asof = "shoc 数据不可用，仅显示 debi 数据（实时）"
    else:
        asof = "shoc 数据截至 %s（UTC），debi 数据实时" % (shoc_at or "未知")
        if shoc_status == "stale":
            asof += "（shoc 拉取失败，显示缓存快照）"
    return {
        "today": today,
        "cards": cards,
        "today_by_box": today_by_box,
        "boxes": boxes,
        "shoc_status": shoc_status,
        "shoc_generated_at": shoc_at,
        "asof_text": asof,
        "models": model_list,
        "days": day_list,
        "query_ms": int((time.time() - t0) * 1000),
    }


def build_requests(qs):
    t0 = time.time()
    rows, _shoc_at, _shoc_status = get_dataset()
    model = (qs.get("model") or [""])[0]
    try:
        limit = int((qs.get("limit") or ["15"])[0])
    except ValueError:
        limit = 15
    limit = max(1, min(50, limit))
    try:
        offset = int((qs.get("offset") or ["0"])[0])
    except ValueError:
        offset = 0
    offset = max(0, offset)
    if model:
        rows = [r for r in rows if r["model"] == model]
    total = len(rows)
    page = rows[offset:offset + limit]
    return {
        "rows": [{
            "ts": r["ts"], "box": r["box"], "model": r["model"],
            "status": r["status"], "prompt_tokens": r["prompt_tokens"],
            "output_tokens": r["output_tokens"], "cost": r["cost"],
        } for r in page],
        "total": total, "limit": limit, "offset": offset,
        "query_ms": int((time.time() - t0) * 1000),
    }


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>API 账单</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--card:#f6f6f6;--line:#e2e2e2;--accent:#0b5fff;--warn:#b45309;--bad:#b91c1c}
@media (prefers-color-scheme: dark){:root{--bg:#121212;--fg:#e8e8e8;--muted:#9a9a9a;--card:#1e1e1e;--line:#2c2c2c;--accent:#6ea8ff;--warn:#f59e0b;--bad:#f87171}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:880px;margin:0 auto}
h1{font-size:22px;margin:4px 0 6px}
h2{font-size:17px;margin:28px 0 10px}
.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
@media (max-width:640px){.cards{grid-template-columns:repeat(2,1fr)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card .lab{font-size:13px;color:var(--muted)}
.card .val{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;margin:4px 0}
.card .sub2{font-size:12px;color:var(--muted)}
.tblwrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--muted);font-size:12px;font-weight:600;padding:8px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
td.wrap{white-space:normal}
.pill{display:inline-block;font-size:11px;padding:1px 7px;border:1px solid var(--line);border-radius:999px;color:var(--muted);margin-right:6px}
.badge-fail{display:inline-block;font-size:11px;padding:1px 7px;border:1px solid var(--bad);border-radius:999px;color:var(--bad);margin-left:6px}
tr.fail td:first-child{box-shadow:inset 3px 0 0 var(--bad)}
.muted{color:var(--muted)}
.alert{border-left:3px solid var(--warn);background:var(--card);padding:10px 12px;border-radius:0 8px 8px 0;font-size:14px;margin:0 0 14px}
.alert:empty{display:none}
.filterrow{display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
select,button{min-height:44px;font-size:15px;padding:0 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
button:disabled{opacity:.45}
.pager{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
.pager .info{font-size:13px;color:var(--muted)}
.wait{padding:18px 0}
.wait .bar{height:4px;background:var(--line);border-radius:2px;overflow:hidden}
.wait .bar i{display:block;width:40%;height:100%;background:var(--accent);animation:wait-slide 1.1s linear infinite}
.wait .elapsed{display:block;margin-top:8px;font-size:13px;color:var(--muted)}
@keyframes wait-slide{from{transform:translateX(-100%)}to{transform:translateX(250%)}}
.errbox,.empty{border:1px solid var(--line);border-radius:10px;padding:14px;font-size:14px}
.errbox button,.empty button{margin-left:10px}
footer{margin-top:32px;border-top:1px solid var(--line);padding-top:12px;color:var(--muted);font-size:12px;line-height:1.7}
</style>
</head>
<body>
<main>
<h1>API 账单</h1>
<p class="sub" id="asof">加载中…</p>
<div class="alert" id="alert"></div>
<section class="cards" id="cards"></section>
<section>
<h2>按模型</h2>
<div class="tblwrap"><table id="models"><thead><tr><th>模型</th><th class="n">请求</th><th class="n">输入 token</th><th class="n">输出含推理</th><th class="n">花费</th></tr></thead><tbody></tbody></table></div>
</section>
<section>
<h2>按天</h2>
<div class="tblwrap"><table id="days"><thead><tr><th>日期 (UTC)</th><th class="n">请求</th><th class="n">花费</th></tr></thead><tbody></tbody></table></div>
</section>
<section>
<h2>请求记录</h2>
<div class="filterrow">
<label>模型 <select id="modelfilter"><option value="">全部</option></select></label>
<button id="clearfilter" hidden>清除筛选</button>
</div>
<div id="reqlist"></div>
<div class="pager" id="pager"></div>
</section>
<footer id="foot"></footer>
</main>
<script>
function $(id){return document.getElementById(id);}
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function money(n){if(n===null||n===undefined)return "—";if(Math.abs(n)<1)return "$"+n.toFixed(4);return "$"+n.toFixed(2);}
function num(n){if(n===null||n===undefined)return "—";return n.toLocaleString("en-US");}
function waitFor(el){
  var t0=performance.now(),tick=null;
  var t=setTimeout(function(){
    el.innerHTML='<div class="wait"><div class="bar"><i></i></div><span class="elapsed">已等 0.0 秒</span></div>';
    tick=setInterval(function(){var e=el.querySelector(".elapsed");if(e)e.textContent="已等 "+((performance.now()-t0)/1000).toFixed(1)+" 秒";},100);
  },300);
  return function(){clearTimeout(t);if(tick)clearInterval(tick);return (performance.now()-t0)/1000;};
}
var state={model:"",offset:0,limit:15,total:0};
function card(lab,val,sub){return '<div class="card"><div class="lab">'+lab+'</div><div class="val">'+val+'</div><div class="sub2">'+sub+'</div></div>';}
function renderSummary(d){
  var c=d.cards;
  $("cards").innerHTML=
    card("今日花费",money(c.today_cost),c.today_requests+" 条请求")+
    card("本月花费",money(c.month_cost),c.month_requests+" 条请求")+
    card("今日请求",num(c.today_requests),"debi "+d.today_by_box.debi+" · shoc "+d.today_by_box.shoc)+
    card("未计费",num(c.uncosted),"无 token 记录的请求");
  var al=$("alert");
  if(d.shoc_status==="stale"){al.textContent="shoc 数据为缓存快照（截至 "+(d.shoc_generated_at||"未知")+"），本次拉取失败。";}
  else if(d.shoc_status==="missing"){al.textContent="shoc 数据暂不可用，仅显示 debi 的记录。";}
  else{al.textContent="";}
  var mb=document.querySelector("#models tbody");
  if(!d.models.length){mb.innerHTML='<tr><td colspan="5" class="wrap muted">还没有任何请求记录。</td></tr>';}
  else{
    mb.innerHTML=d.models.map(function(m){
      var name=m.model?esc(m.model):"(空)";
      return "<tr><td>"+name+"</td><td class='n'>"+num(m.requests)+"</td><td class='n'>"+num(m.prompt_tokens)+"</td><td class='n'>"+num(m.output_tokens)+"</td><td class='n'>"+money(m.cost)+"</td></tr>";
    }).join("");
  }
  var db=document.querySelector("#days tbody");
  if(!d.days.length){db.innerHTML='<tr><td colspan="3" class="wrap muted">还没有任何请求记录。</td></tr>';}
  else{
    db.innerHTML=d.days.map(function(x){
      var req=num(x.requests)+(x.uncosted?'<span class="muted">（'+x.uncosted+' 未计费）</span>':"");
      return "<tr><td>"+esc(x.date)+"</td><td class='n'>"+req+"</td><td class='n'>"+money(x.cost)+"</td></tr>";
    }).join("");
  }
  $("asof").textContent=d.asof_text+" · 计算用时 "+d.query_ms+" 毫秒";
  $("foot").innerHTML="<p>按 UTC 日期分段计价，与 GCP 账单可能有 ±1 天归属差异；缓存命中未拆分；实际花费以 GCP 账单为准；未计费请求为中继日志无 token 记录的请求（修好用量记录之前的数据）。</p>";
}
function buildFilter(models){
  var sel=$("modelfilter");
  var cur=sel.value;
  sel.innerHTML='<option value="">全部</option>'+models.filter(function(m){return m.model;}).map(function(m){return '<option value="'+esc(m.model)+'">'+esc(m.model)+"</option>";}).join("");
  sel.value=cur;
  $("clearfilter").hidden=!state.model;
}
function renderRequests(d,secs){
  var el=$("reqlist");
  if(!d.rows.length){
    if(state.model){
      el.innerHTML='<div class="empty">模型「'+esc(state.model)+'」没有请求记录。<button id="clearempty">清除筛选</button></div>';
      $("clearempty").onclick=clearFilter;
    }else{
      el.innerHTML='<div class="empty">还没有请求记录。</div>';
    }
    $("pager").innerHTML="";
    return;
  }
  var html='<div class="tblwrap"><table><thead><tr><th>时间 (UTC)</th><th>来源 · 模型</th><th class="n">输入 / 输出</th><th class="n">花费</th></tr></thead><tbody>';
  html+=d.rows.map(function(r){
    var t=r.ts?r.ts.slice(5,16):"—";
    var fail=r.status!==null&&r.status!==200;
    var name=r.model?esc(r.model):"(空)";
    var cell='<span class="pill">'+esc(r.box)+"</span>"+name+(fail?'<span class="badge-fail">失败 '+r.status+"</span>":"");
    return '<tr'+(fail?' class="fail"':"")+"><td>"+esc(t)+"</td><td>"+cell+"</td><td class='n'>"+num(r.prompt_tokens)+" / "+num(r.output_tokens)+"</td><td class='n'>"+money(r.cost)+"</td></tr>";
  }).join("");
  html+="</tbody></table></div>";
  el.innerHTML=html;
  var pages=Math.max(1,Math.ceil(d.total/d.limit));
  var page=Math.floor(d.offset/d.limit)+1;
  $("pager").innerHTML='<button id="prev"'+(d.offset<=0?" disabled":"")+'>上一页</button><button id="next"'+(d.offset+d.limit>=d.total?" disabled":"")+'>下一页</button><span class="info">共 '+d.total+' 条 · 第 '+page+"/"+pages+" 页 · 加载用时 "+secs.toFixed(1)+" 秒</span>";
  $("prev").onclick=function(){if(state.offset>0){state.offset=Math.max(0,state.offset-state.limit);loadRequests();}};
  $("next").onclick=function(){if(state.offset+state.limit<state.total){state.offset+=state.limit;loadRequests();}};
}
function loadRequests(){
  var el=$("reqlist");
  var stop=waitFor(el);
  var q="api/requests?limit="+state.limit+"&offset="+state.offset+(state.model?"&model="+encodeURIComponent(state.model):"");
  fetch(q).then(function(r){return r.json();}).then(function(d){
    var secs=stop();
    state.total=d.total;
    renderRequests(d,secs);
  }).catch(function(){
    stop();
    el.innerHTML='<div class="errbox">请求记录加载失败，可能是服务端或网络问题。<button id="retry2">重试</button></div>';
    $("retry2").onclick=loadRequests;
  });
}
function clearFilter(){
  state.model="";state.offset=0;
  $("modelfilter").value="";
  $("clearfilter").hidden=true;
  loadRequests();
}
function loadSummary(){
  var stop=waitFor($("cards"));
  fetch("api/summary").then(function(r){return r.json();}).then(function(d){
    stop();
    renderSummary(d);
    buildFilter(d.models);
    loadRequests();
  }).catch(function(){
    stop();
    $("cards").innerHTML='<div class="errbox">摘要加载失败，可能是服务端或网络问题。<button id="retry1">重试</button></div>';
    $("retry1").onclick=loadSummary;
  });
}
$("modelfilter").addEventListener("change",function(){state.model=this.value;state.offset=0;$("clearfilter").hidden=!state.model;loadRequests();});
$("clearfilter").addEventListener("click",clearFilter);
loadSummary();
</script>
</body>
</html>
"""


def _respond(start_response, status, body, ctype):
    data = body.encode("utf-8") if isinstance(body, str) else body
    start_response(status, [("Content-Type", ctype),
                            ("Content-Length", str(len(data)))])
    return [data]


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/") or "/"
    p = path.rstrip("/") or "/"
    if p == "/":
        return _respond(start_response, "200 OK", HTML,
                        "text/html; charset=utf-8")
    if p == "/api/summary":
        return _respond(start_response, "200 OK",
                        json.dumps(build_summary(), ensure_ascii=False),
                        "application/json; charset=utf-8")
    if p == "/api/requests":
        qs = parse_qs(environ.get("QUERY_STRING", ""))
        return _respond(start_response, "200 OK",
                        json.dumps(build_requests(qs), ensure_ascii=False),
                        "application/json; charset=utf-8")
    if p == "/health":
        return _respond(start_response, "200 OK", "ok", "text/plain")
    return _respond(start_response, "404 Not Found", "not found",
                    "text/plain")
