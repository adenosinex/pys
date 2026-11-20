#!/usr/bin/env python3
"""obscu: Obscura real-browser page test (debi).

Loads a URL in the Obscura headless browser (Rust/V8, no Chromium)
and reports whether the page's resources actually load in a real
browser: a PNG screenshot, per-image load status taken from the
rendered DOM, and the sub-resource list the page references.

Runs the Obscura binary at /srv/apphub/bin/obscura/obscura as the
apphub user. No service/autostart of Obscura itself; this app just
spawns it per test (subprocess, hard timeout).
"""
import html
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

OBSCURA = "/srv/apphub/bin/obscura/obscura"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/srv/apphub/data/projects/obscu"))
RUNS_DIR = DATA_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

MAX_WAIT = 30
OBSCURA_TIMEOUT = 120

# ---- Activation guard (activation.json, 2026-11-19) ----
# Code is mirrored to both boxes, but a service only runs where the
# activation table says so. The app self-checks at import: if this
# box is not in the project's active_on list, refuse to start.
# Fail-open when the table is missing/unreadable or the box is unknown,
# so a missing table never breaks a legitimate start.
_SLUG = "obscu"
_BOX_BY_HOST = {"shock3": "shoc", "fperiod": "debi"}


def _current_box():
    env = (os.environ.get("APPHUB_BOX") or "").strip()
    if env:
        return env
    try:
        import socket
        return _BOX_BY_HOST.get(socket.gethostname(), "unknown")
    except Exception:
        return "unknown"


def _activation_guard():
    try:
        table_path = None
        for cand in (DATA_DIR / "activation.json", Path("/srv/apphub/data/activation.json")):
            if cand.is_file():
                table_path = cand
                break
        if table_path is None:
            return
        table = json.loads(table_path.read_text(encoding="utf-8"))
        entry = (table.get("projects") or {}).get(_SLUG)
        if not isinstance(entry, dict):
            return
        active = entry.get("active_on") or []
        box = _current_box()
        if box != "unknown" and box not in active:
            raise SystemExit(
                "obscu is not activated on this box (%s); active_on=%s. "
                "Code is mirrored here but the service must stay inactive." % (box, active))
    except SystemExit:
        raise
    except Exception:
        return


_activation_guard()

EVAL_JS = (
    "JSON.stringify({title: document.title, url: location.href, "
    "imgs: [...document.images].map(function(im){return {src: im.currentSrc||im.src, "
    "ok: !!(im.complete && im.naturalWidth>0), w: im.naturalWidth, h: im.naturalHeight};}), "
    "n_img: document.images.length, "
    "body_len: (document.body?document.body.innerText.length:0)})"
)

CSS = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;"
    "margin:0;background:#f8fafc;color:#1e293b}"
    ".wrap{max-width:960px;margin:0 auto;padding:20px 16px 48px}"
    "h1{font-size:20px;margin:4px 0 4px}"
    ".sub{color:#64748b;font-size:13px;margin:0 0 16px}"
    ".card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin:0 0 16px}"
    "label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px}"
    "input[type=text],input[type=number]{width:100%;box-sizing:border-box;padding:9px 10px;"
    "border:1px solid #cbd5e1;border-radius:8px;font-size:14px}"
    ".row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}"
    ".btn{display:inline-block;background:#1d4ed8;color:#fff;border:0;border-radius:8px;"
    "padding:9px 18px;font-size:14px;cursor:pointer;text-decoration:none}"
    ".btn2{background:#fff;color:#1d4ed8;border:1px solid #bfdbfe}"
    ".muted{color:#64748b;font-size:12px}"
    ".ok{color:#15803d;font-weight:700}"
    ".bad{color:#b91c1c;font-weight:700}"
    "table{width:100%;border-collapse:collapse;font-size:13px}"
    "th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #e2e8f0;vertical-align:top}"
    "th{color:#64748b;font-size:12px}"
    ".mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;word-break:break-all}"
    "img.shot{width:100%;border:1px solid #e2e8f0;border-radius:8px}"
    ".pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#eff6ff;color:#1d4ed8}"
    ".pillbad{background:#fef2f2;color:#b91c1c}"
    ".pillok{background:#f0fdf4;color:#15803d}"
)


def esc(s):
    return html.escape(str(s if s is not None else ""))


def _resp(start_response, status, body, ctype="text/html; charset=utf-8"):
    if isinstance(body, str):
        body = body.encode("utf-8")
    start_response(status, [("Content-Type", ctype), ("Content-Length", str(len(body)))])
    return [body]


def _page(title, inner):
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>" + esc(title) + "</title><style>" + CSS + "</style></head><body>"
        "<div class=\"wrap\">" + inner + "</div></body></html>"
    )


def _read_form(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    raw = environ["wsgi.input"].read(length) if length > 0 else b""
    return urllib.parse.parse_qs(raw.decode("utf-8", "replace"))


def _recent_runs(limit=10):
    out = []
    try:
        dirs = sorted(RUNS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return out
    for d in dirs[:limit]:
        res = d / "result.json"
        if not res.is_file():
            continue
        try:
            data = json.loads(res.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append((d.name, data))
    return out


def _run_obscura(url, wait, allow_private):
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    rdir = RUNS_DIR / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    shot = rdir / "shot.png"
    storage = rdir / "storage"
    storage.mkdir(exist_ok=True)
    base = [OBSCURA, "fetch", "--wait", str(wait), "--timeout", "60",
            "--storage-dir", str(storage)]
    if allow_private:
        base.append("--allow-private-network")
    result = {"id": run_id, "url": url, "wait": wait,
              "allow_private": allow_private, "started_at": time.time(),
              "assets": [], "eval": None, "shot": False, "error": ""}
    # Pass 1: sub-resource list.
    try:
        p = subprocess.run(base + ["--dump", "assets", url],
                           capture_output=True, text=True, timeout=OBSCURA_TIMEOUT)
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("url"):
                result["assets"].append({"url": obj.get("url"), "type": obj.get("type", "")})
        if p.returncode != 0 and not result["assets"]:
            result["error"] = (p.stderr or p.stdout or "")[-500:]
    except subprocess.TimeoutExpired:
        result["error"] = "Obscura assets pass timed out."
    except Exception as exc:
        result["error"] = "assets pass failed: %s" % exc
    # Pass 2: screenshot + rendered-DOM image status.
    try:
        p = subprocess.run(
            base + ["-s", str(shot), "--eval", EVAL_JS, url],
            capture_output=True, text=True, timeout=OBSCURA_TIMEOUT)
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ev = obj.get("evaluation")
            if isinstance(ev, str):
                try:
                    result["eval"] = json.loads(ev)
                except Exception:
                    result["eval"] = {"raw": ev}
                break
        result["shot"] = shot.is_file() and shot.stat().st_size > 0
        if p.returncode != 0 and not result["shot"] and not result["eval"]:
            err = (p.stderr or p.stdout or "")[-500:]
            result["error"] = (result["error"] + "\n" + err).strip()
    except subprocess.TimeoutExpired:
        result["error"] = (result["error"] + "\nObscura render pass timed out.").strip()
    except Exception as exc:
        result["error"] = (result["error"] + "\nrender pass failed: %s" % exc).strip()
    result["finished_at"] = time.time()
    (rdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _home():
    recent = _recent_runs()
    items = ""
    for rid, data in recent:
        ev = data.get("eval") or {}
        n_img = ev.get("n_img", 0)
        imgs = ev.get("imgs") or []
        n_ok = sum(1 for im in imgs if im.get("ok"))
        n_fail = sum(1 for im in imgs if not im.get("ok"))
        badge = ("<span class=\"pill pillok\">图片 %d/%d 加载</span>" % (n_ok, n_img)) if n_img else "<span class=\"pill\">无图片</span>"
        if n_fail:
            badge = "<span class=\"pill pillbad\">图片 %d 失败 / 共 %d</span>" % (n_fail, n_img)
        items += ("<tr><td class=\"mono\"><a href=\"/run/" + esc(rid) + "\">" + esc(rid) + "</a></td>"
                  "<td class=\"mono\">" + esc((data.get("url") or "")[:80]) + "</td>"
                  "<td>" + badge + "</td>"
                  "<td class=\"muted\">" + esc(str(len(data.get("assets") or []))) + " 个资源</td></tr>")
    recent_html = ""
    if items:
        recent_html = ("<div class=\"card\"><h1>最近测试</h1><table><tr><th>测试</th><th>网址</th>"
                       "<th>图片</th><th>资源</th></tr>" + items + "</table></div>")
    inner = (
        "<h1>Obscura 页面实测</h1>"
        "<p class=\"sub\">用 debi 上的 Obscura 无头浏览器（Rust/V8）真实打开一个网页，"
        "核对它的资源是否在实际环境加载出来：截图 + 渲染后图片加载状态 + 子资源清单。</p>"
        "<div class=\"card\"><form method=\"post\" action=\"/run\">"
        "<label>网址</label>"
        "<input type=\"text\" name=\"url\" required placeholder=\"https://pys.shoc.zin7.de/web/xlikes/\" "
        "value=\"https://pys.shoc.zin7.de/web/xlikes/\">"
        "<div class=\"row\"><div><label>加载后等待（秒）</label>"
        "<input type=\"number\" name=\"wait\" value=\"5\" min=\"0\" max=\"30\" style=\"width:110px\"></div>"
        "<label style=\"font-weight:400\"><input type=\"checkbox\" name=\"allow_private\" value=\"1\"> "
        "允许内网地址（测本机/回环地址时勾选）</label></div>"
        "<p><button class=\"btn\" type=\"submit\">开始测试</button></p>"
        "<p class=\"muted\">每次测试在 debi 上跑两次 Obscura（一次取子资源清单，一次截图+取渲染后图片状态），"
        "一般 10-30 秒。登录态页面请在网址里带 token 参数（如 ?token=…）；"
        "本工具不保存 token，只当次使用。</p>"
        "</form></div>" + recent_html
    )
    return _page("Obscura 页面实测", inner)


def _run_page(data):
    rid = data["id"]
    ev = data.get("eval") or {}
    imgs = ev.get("imgs") or []
    n_ok = sum(1 for im in imgs if im.get("ok"))
    n_fail = sum(1 for im in imgs if not im.get("ok"))
    assets = data.get("assets") or []
    shot_html = ""
    if data.get("shot"):
        shot_html = ("<div class=\"card\"><h1>截图</h1>"
                     "<img class=\"shot\" src=\"/shot/" + esc(rid) + ".png\" alt=\"screenshot\"></div>")
    img_rows = ""
    for im in imgs:
        src = im.get("src") or ""
        ok = im.get("ok")
        img_rows += ("<tr><td class=\"mono\">" + esc(src[:160]) + "</td>"
                     "<td>" + ("<span class=\"ok\">加载成功</span>" if ok else "<span class=\"bad\">加载失败</span>") + "</td>"
                     "<td class=\"muted\">" + esc(str(im.get("w"))) + "x" + esc(str(im.get("h"))) + "</td></tr>")
    img_html = ("<div class=\"card\"><h1>图片（渲染后，共 " + esc(str(len(imgs))) + " 张，"
                "成功 " + esc(str(n_ok)) + " / 失败 " + esc(str(n_fail)) + "）</h1>"
                + ("<table><tr><th>图片地址</th><th>状态</th><th>尺寸</th></tr>" + img_rows + "</table>"
                   if img_rows else "<p class=\"muted\">页面里没有图片元素。</p>") + "</div>")
    asset_rows = ""
    for a in assets:
        asset_rows += ("<tr><td class=\"mono\">" + esc((a.get("url") or "")[:180]) + "</td>"
                       "<td class=\"muted\">" + esc(a.get("type") or "") + "</td></tr>")
    asset_html = ("<div class=\"card\"><h1>子资源清单（共 " + esc(str(len(assets))) + " 个）</h1>"
                  + ("<table><tr><th>资源地址</th><th>类型</th></tr>" + asset_rows + "</table>"
                     if asset_rows else "<p class=\"muted\">未取到子资源清单。</p>") + "</div>")
    err = (data.get("error") or "").strip()
    err_html = ("<div class=\"card\"><h1>提示</h1><p class=\"bad mono\">" + esc(err) + "</p></div>") if err else ""
    inner = (
        "<p><a class=\"btn btn2\" href=\"/\">← 再测一个</a></p>"
        "<h1>测试结果</h1>"
        "<p class=\"sub mono\">" + esc(data.get("url") or "") + " · " + esc(rid) + "</p>"
        + err_html + shot_html + img_html + asset_html
    )
    return _page("测试结果 " + rid, inner)


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"
    try:
        if path == "/" and method == "GET":
            return _resp(start_response, "200 OK", _home())
        if path == "/health" and method == "GET":
            return _resp(start_response, "200 OK", json.dumps({"ok": True}), "application/json")
        if path == "/run" and method == "POST":
            form = _read_form(environ)
            url = (form.get("url") or [""])[0].strip()
            if not url.startswith(("http://", "https://", "data:text/html,")):
                return _resp(start_response, "400 Bad Request",
                             _page("参数错误", "<p class=\"bad\">网址必须以 http:// 或 https:// 开头。</p><p><a href=\"/\">返回</a></p>"))
            try:
                wait = int((form.get("wait") or ["5"])[0])
            except ValueError:
                wait = 5
            wait = max(0, min(MAX_WAIT, wait))
            allow_private = bool(form.get("allow_private"))
            data = _run_obscura(url, wait, allow_private)
            start_response("302 Found", [("Location", "/run/" + data["id"]), ("Content-Length", "0")])
            return [b""]
        m = re.match(r"^/run/([A-Za-z0-9\-_]+)$", path)
        if m and method == "GET":
            res = RUNS_DIR / m.group(1) / "result.json"
            if not res.is_file():
                return _resp(start_response, "404 Not Found", _page("不存在", "<p>没有这个测试。</p><p><a href=\"/\">返回</a></p>"))
            data = json.loads(res.read_text(encoding="utf-8"))
            return _resp(start_response, "200 OK", _run_page(data))
        m = re.match(r"^/shot/([A-Za-z0-9\-_]+)\.png$", path)
        if m and method == "GET":
            shot = RUNS_DIR / m.group(1) / "shot.png"
            if not shot.is_file():
                return _resp(start_response, "404 Not Found", "no screenshot")
            body = shot.read_bytes()
            start_response("200 OK", [("Content-Type", "image/png"), ("Content-Length", str(len(body)))])
            return [body]
        return _resp(start_response, "404 Not Found", _page("不存在", "<p>页面不存在。</p><p><a href=\"/\">返回</a></p>"))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return _resp(start_response, "500 Internal Server Error",
                     _page("服务器错误", "<p class=\"bad\">" + esc(exc) + "</p><p><a href=\"/\">返回</a></p>"))


app = application

if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19998
    print("obscu on :%d" % port, flush=True)
    make_server("127.0.0.1", port, application).serve_forever()
