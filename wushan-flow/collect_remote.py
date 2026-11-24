# -*- coding: utf-8 -*-
"""巫山客流预测：debi 采集端（12306 出口经 hwd SOCKS 代理）。

采集器留在 debi，只改出口：12306 请求经 SSH 隧道到 hwd 的 SOCKS5 代理
（DATA_DIR/proxy_url，socks5://127.0.0.1:11080），12306 看到 hwd 的国内 IP。
debi/shoc 海外出口间歇性返回 HTML 错误页；hwd 国内出口实测稳定。
跑一轮 collect_once()，把结果 POST 到 shoc 的 /api/ingest 入库并由 shoc 发等级/简报提醒。
若本轮全部失败，记失败计数；连续 3 轮全失败时直接经 hwd mailapi 发一次运维提醒。
用法：DATA_DIR=/srv/apphub/data-local/wushan-flow python3 collect_remote.py
"""
import json
import os
import sys
import urllib.request
from datetime import date, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from core import collect_once, consecutive_failures, init_db  # noqa: E402

SHOC_INGEST = "https://pys.shoc.zin7.de/web/wushan-flow/api/ingest"


def _load(name):
    p = os.path.join(os.environ.get("DATA_DIR", BASE), name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _post_ingest(result):
    token = _load("ingest_token")
    panel_token = _load("panel_token")
    if not token:
        print("collect_remote: no ingest_token, skip post")
        return False
    url = SHOC_INGEST
    if panel_token:
        # 面板 /web/ 代理会先验 gate；带上项目固定 token 才能到达 Flask 应用。
        # Flask 侧的 ingest_token 是第二层鉴权。
        url += "?token=" + panel_token
    payload = json.dumps({
        "weekend_date": result["weekend_date"],
        "crawl_date": result["crawl_date"],
        "lead_days": result["lead_days"],
        "inbound_score": result["inbound_score"],
        "outbound_score": result["outbound_score"],
        "level": result["level"],
        "valid_trains": result["valid_trains"],
        "inbound_detail": result["inbound_detail"],
        "outbound_detail": result["outbound_detail"],
        "crawl_time": result["crawl_time"],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return bool(data.get("ok"))


def _post_ingest_safe(result):
    """ingest 失败不抛异常（数据仍在 debi 本地库，下轮可重试），只返回 False。"""
    try:
        return _post_ingest(result)
    except Exception as exc:  # noqa: BLE001
        print("collect_remote: ingest failed: %s" % exc)
        return False


def _mailapi_send(to, subject, text):
    """运维提醒直发 hwd mailapi（debi 出口直连）。"""
    token = _load("mailapi_token")
    if not to or not token:
        print("collect_remote: ops mail skipped (no token/email)")
        return False
    payload = json.dumps({
        "to": to, "from": "noreply@zin7.de",
        "subject": subject, "text": text,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://139.9.100.221:18443/send", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as exc:
        print("collect_remote: ops mail failed: %s" % exc)
        return False


def main():
    init_db()
    today = date.today()
    today_s = today.isoformat()
    try:
        result = collect_once(today)
    except Exception as exc:
        fails = consecutive_failures()
        print("collect_remote: round failed (%s); consecutive=%d" % (exc, fails))
        # 连续 3 轮全失败：发一次运维提醒（按天去重，记在本地 notification_log）
        from core import notified, mark_notified
        if fails >= 3 and not notified("ops", "fail3:" + today_s):
            email = _load("notify_email") or "wq66@zin7.de"
            if _mailapi_send(email, "巫山客流采集异常：连续 %d 轮失败" % fails,
                             "巫山客流预测（debi 采集端）连续 %d 轮 12306 采集全部失败（%s）。"
                             "可能是接口变更或 IP 被限流，请检查。页面会显示红色过期条。"
                             % (fails, today_s)):
                mark_notified("ops", "fail3:" + today_s)
        return 0
    ok = _post_ingest_safe(result)
    print("collect_remote: ok level=%s in=%s out=%s valid=%d lead=%d ingest=%s"
          % (result["level"], result["inbound_score"], result["outbound_score"],
             result["valid_trains"], result["lead_days"], ok))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
