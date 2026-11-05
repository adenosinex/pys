"""Short-video link parsing: Douyin + Bilibili, lowest quality.

Ported from the user's mini_parse.py (standard-library urllib port;
the original used requests). Its core logic derives from
jiuhunwl/short_videos: https://github.com/jiuhunwl/short_videos

Why this runs server-side rather than in the browser: resolving the
share short links means reading redirect targets, which are opaque
to a web page cross-origin; Douyin's detail endpoint expects a
mobile User-Agent, a header browsers cannot set; and neither
platform API sends CORS headers a page could rely on. The browser
only receives the resolved direct URL - playback and download then
go through /api/video/stream, which re-adds the Referer the CDN
anti-leech checks require.
"""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

if __package__:
    from .extractor import ZhihuError
else:
    from extractor import ZhihuError

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/123.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13; 2210132C "
             "Build/TKQ1.220829.002) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36")

SOURCE_REPO = "https://github.com/jiuhunwl/short_videos"

_DOWNLOAD_SUFFIXES = ("douyin.com", "douyinvod.com", "amemv.com",
                      "snssdk.com", "zjcdn.com", "douyincdn.com",
                      "bilibili.com", "bilivideo.com", "hdslb.com")
_REFERER_SUFFIXES = ("douyin.com", "bilibili.com")


def _host_matches(host, suffixes):
    host = (host or "").lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _final_url(url, ua, timeout=15):
    """Follow redirects and return the final URL (body not read)."""
    req = Request(url, headers={"User-Agent": ua})
    try:
        with build_opener().open(req, timeout=timeout) as resp:
            return resp.geturl()
    except HTTPError as exc:
        # The platform answered with an error status (Bilibili's
        # page returns 412 to non-browser clients), but the redirect
        # chain was still followed and the final URL is all we need
        # - requests, which the original used, does not raise here.
        final = exc.geturl()
        if final:
            return final
        raise ZhihuError("短链跳转失败：HTTP " + str(exc.code),
                         "upstream_error", 502) from None
    except (URLError, OSError) as exc:
        raise ZhihuError("短链跳转失败：" + type(exc).__name__,
                         "upstream_error", 502) from None


def _get_json(url, ua, params=None, timeout=20, extra_headers=None):
    if params:
        url = url + "?" + urlencode(params)
    headers = {"User-Agent": ua, "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, headers=headers)
    try:
        with build_opener().open(req, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as exc:
        # Platform APIs often answer errors as JSON too; hand the
        # body to the caller's own code checks when it parses.
        try:
            return json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            raise ZhihuError("平台接口返回 HTTP " + str(exc.code) + "。",
                             "upstream_error", 502) from None
    except (URLError, OSError) as exc:
        raise ZhihuError("连接平台接口失败：" + type(exc).__name__,
                         "upstream_error", 502) from None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ZhihuError("平台接口返回了无法解析的内容。",
                         "bad_upstream", 502) from None


def extract_url(text):
    m = re.search(r"https?://[^\s\"'<>]+", text or "")
    return m.group(0).rstrip("'\"，。；,.;") if m else None


# ---------------- Douyin: lowest quality, no watermark ----------------

def douyin_lowest(share_text):
    url = extract_url(share_text)
    if not url:
        raise ZhihuError("没找到链接。", "invalid_url", 400)
    real = _final_url(url, UA_DESKTOP)
    m = re.search(r"modal_id=(\d+)", real) or re.search(r"video/(\d+)", real)
    aweme_id = m.group(1) if m else \
        real.rstrip("/").split("/")[-1].split(".")[0]
    if not re.fullmatch(r"[1-9]\d{15,19}", aweme_id):
        raise ZhihuError("提取作品 ID 失败，链接可能已失效。",
                         "parse_failed", 422)
    data = _get_json(
        "https://api.amemv.com/aweme/v1/aweme/detail/",
        UA_MOBILE,
        params={"aweme_id": aweme_id, "aid": "6383",
                "device_platform": "web", "version_code": "190500",
                "version_name": "19.5.0"})
    detail = data.get("aweme_detail") or {}
    video = detail.get("video") or {}
    base = {"platform": "douyin", "platform_name": "抖音",
            "id": aweme_id,
            "title": (detail.get("desc") or "").strip() or aweme_id,
            "referer": "https://www.douyin.com/", "page_url": real,
            "source_repo": SOURCE_REPO}
    # Some works carry a ready-made text track; when present it goes
    # straight to the text channel like any other extraction.
    vt = detail.get("video_text")
    if isinstance(vt, list):
        lines = []
        for item in vt:
            if isinstance(item, str):
                lines.append(item)
            elif isinstance(item, dict):
                lines.append(item.get("text") or item.get("content") or "")
        vtext = "\n".join(x.strip() for x in lines if x and x.strip())
        if vtext:
            return {**base, "kind": "subtitle", "quality": "视频文字",
                    "subtitle_text": vtext, "subtitle_lang": "",
                    "play_url": None, "audio_only": False, "filename": ""}
    # Stream choice, sound first. Newer Douyin delivery mixes split
    # DASH-style entries into bit_rate: a /media-video-<codec>/ URL
    # is video-only (no sound at all), and its HEVC variants sort
    # first because they compress best - the old blind
    # lowest-bitrate pick landed exactly on those (silent in the
    # player, and HEVC is often undecodable in a browser anyway).
    # Prefer muxed entries, then the classic top-level play_addr,
    # then a split audio entry on its own (sound over picture),
    # and only as a last resort a split video entry.
    entries = []
    for entry in video.get("bit_rate") or video.get("bitRateList") or []:
        urls = (entry.get("play_addr") or {}).get("url_list") or []
        if not urls:
            continue
        entries.append({
            "rate": entry.get("bit_rate") or entry.get("bitRate") or 0,
            "url": urls[0],
            "h265": entry.get("is_h265") in (1, "1", True)
                    or "hvc1" in urls[0]})

    def split_kind(u):
        if "/media-audio" in u:
            return "audio"
        if "/media-video" in u:
            return "video"
        return "muxed"

    play_url, audio_only = None, False
    quality = "最低画质 · 无水印"
    muxed = [e for e in entries if split_kind(e["url"]) == "muxed"]
    avc = [e for e in muxed if not e["h265"]]
    pool = avc or muxed
    if pool:
        play_url = min(pool, key=lambda e: e["rate"])["url"]
    if not play_url:
        top = (video.get("play_addr") or {}).get("url_list") or []
        if top:
            play_url = top[0]
    if not play_url:
        auds = [e for e in entries if split_kind(e["url"]) == "audio"]
        if auds:
            play_url = min(auds, key=lambda e: e["rate"])["url"]
            audio_only, quality = True, "仅音频 · 无水印（无合成流，优先保声音）"
    if not play_url:
        vids = [e for e in entries if split_kind(e["url"]) == "video"]
        if vids:
            play_url = min(vids, key=lambda e: e["rate"])["url"]
            quality = "最低画质 · 无水印（分离流，可能无声）"
    if not play_url:
        raise ZhihuError(
            "详情接口没返回播放地址，平台接口可能已变更或该作品不可用。",
            "parse_failed", 422)
    # playwm carries the watermark; play is the clean address.
    play_url = play_url.replace("playwm", "play")
    return {**base, "kind": "audio" if audio_only else "video",
            "quality": quality, "play_url": play_url,
            "audio_only": audio_only,
            "filename": "douyin_" + aweme_id
                        + (".m4a" if audio_only else ".mp4"),
            "subtitle_text": None, "subtitle_lang": ""}


# ---------------- Bilibili: lowest quality (360P durl) ----------------

_BUVID = {"pair": None, "at": 0}


def _bili_buvid():
    """Bilibili's API risk control bans cookie-less API calls from
    datacenter addresses (code -412). Real browsers carry buvid
    fingerprint cookies issued by the SPI endpoint; fetch and cache
    a pair the same way."""
    import time
    if _BUVID["pair"] and time.time() - _BUVID["at"] < 3600:
        return _BUVID["pair"]
    data = _get_json("https://api.bilibili.com/x/frontend/finger/spi",
                     UA_DESKTOP, timeout=15)
    payload = data.get("data") or {}
    pair = "buvid3=%s; buvid4=%s" % (payload.get("b_3", ""),
                                     payload.get("b_4", ""))
    _BUVID.update(pair=pair, at=time.time())
    return pair


def _bili_api(path, params, page, user_cookie=""):
    url = "https://api.bilibili.com" + path
    cookie = _bili_buvid()
    if user_cookie:
        cookie = cookie + "; " + user_cookie
    for attempt in (1, 2):
        data = _get_json(
            url, UA_DESKTOP, params=params, timeout=15,
            extra_headers={
                "Referer": page,
                "Origin": "https://www.bilibili.com",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cookie": cookie})
        if data.get("code") != -412 or attempt == 2:
            return data
        _BUVID["pair"] = None  # banned cookie: refresh once, retry
    return data


# WBI request signing, required by the /x/player/wbi/v2 endpoint
# that carries the subtitle track list.
_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3,
              45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39,
              12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17,
              0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
              57, 62, 11, 36, 20, 34, 44, 52]
_WBI = {"mixin": None, "at": 0}


def _bili_mixin_key(user_cookie=""):
    import time
    if _WBI["mixin"] and time.time() - _WBI["at"] < 3600:
        return _WBI["mixin"]
    cookie = _bili_buvid()
    if user_cookie:
        cookie = cookie + "; " + user_cookie
    nav = _get_json("https://api.bilibili.com/x/web-interface/nav",
                    UA_DESKTOP, extra_headers={"Cookie": cookie})
    wbi = (nav.get("data") or {}).get("wbi_img") or {}
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    mixin = "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]
    _WBI.update(mixin=mixin, at=time.time())
    return mixin


def _wbi_sign(params, user_cookie=""):
    import hashlib
    import time
    signed = dict(params)
    signed["wts"] = int(time.time())
    query = urlencode(sorted(
        (k, "".join(c for c in str(v) if c not in "!'()*"))
        for k, v in signed.items()))
    signed["w_rid"] = hashlib.md5(
        (query + _bili_mixin_key(user_cookie)).encode("utf-8")).hexdigest()
    return signed


def _bili_subtitle(bvid, cid, page, user_cookie=""):
    """Direct subtitle fetch, best effort. The track list is
    login-gated in practice (verified 2026-11-05: anonymous
    buvid-only calls returned an empty list for 36/36 popular and
    knowledge-ranking videos), so tracks are only found when the
    user has saved their own Bilibili cookie. Any failure returns
    None and the caller falls back to media."""
    try:
        data = _bili_api("/x/player/wbi/v2",
                         _wbi_sign({"bvid": bvid, "cid": cid},
                                   user_cookie),
                         page, user_cookie)
        tracks = ((data.get("data") or {}).get("subtitle")
                  or {}).get("subtitles") or []

        def rank(track):
            lan = (track.get("lan") or "").lower()
            if lan.startswith("ai"):
                return 1 if "zh" in lan else 3
            if lan.startswith("zh"):
                return 0
            return 2

        tracks = [t for t in tracks if t.get("subtitle_url")]
        if not tracks:
            return None
        tracks.sort(key=rank)
        track = tracks[0]
        url = track["subtitle_url"]
        if url.startswith("//"):
            url = "https:" + url
        doc = _get_json(url, UA_DESKTOP,
                        extra_headers={"Referer": page})
        lines = [(item.get("content") or "").strip()
                 for item in (doc.get("body") or [])]
        text = "\n".join(x for x in lines if x)
        if not text:
            return None
        return {"text": text,
                "lang": track.get("lan_doc") or track.get("lan") or ""}
    except Exception:
        return None


def bilibili_lowest(share_text, user_cookie=""):
    url = extract_url(share_text)
    if not url:
        raise ZhihuError("没找到链接。", "invalid_url", 400)
    real = _final_url(url, UA_DESKTOP)
    m = re.search(r"BV[0-9A-Za-z]{10}", real) or \
        re.search(r"BV[0-9A-Za-z]{10}", share_text or "")
    if not m:
        raise ZhihuError("提取 BV 号失败，链接可能已失效。",
                         "parse_failed", 422)
    bvid = m.group(0)
    page = "https://www.bilibili.com/video/" + bvid + "/"
    view = _bili_api("/x/web-interface/view", {"bvid": bvid}, page,
                     user_cookie)
    if view.get("code") != 0:
        raise ZhihuError("B站 view 接口失败，视频可能不存在或已删除。",
                         "parse_failed", 422)
    cid = view["data"]["pages"][0]["cid"]
    title = view["data"].get("title") or bvid
    base = {"platform": "bilibili", "platform_name": "B站",
            "id": bvid, "title": title, "referer": page,
            "page_url": page, "source_repo": SOURCE_REPO}
    # Subtitles first: when a track can be fetched directly, the
    # caller outputs it through the text channel instead of media.
    sub = _bili_subtitle(bvid, cid, page, user_cookie)
    if sub:
        return {**base, "kind": "subtitle", "quality": "字幕原文",
                "subtitle_text": sub["text"],
                "subtitle_lang": sub["lang"], "play_url": None,
                "audio_only": False, "filename": ""}
    # qn=16 is the lowest rung (360P); fnval=0 returns a single
    # muxed mp4 (durl) instead of split dash streams.
    play = _bili_api(
        "/x/player/playurl",
        {"otype": "json", "fnver": "0", "fnval": "0",
         "player": "3", "qn": "16", "bvid": bvid, "cid": cid,
         "platform": "html5", "high_quality": "0"},
        page, user_cookie)
    durl = (play.get("data") or {}).get("durl") or []
    if durl:
        return {**base, "kind": "video", "quality": "最低画质 · 360P",
                "play_url": durl[0]["url"], "audio_only": False,
                "filename": "bilibili_" + bvid + ".mp4",
                "subtitle_text": None, "subtitle_lang": ""}
    # No muxed stream offered: the dash video track carries no
    # sound, so take the best dash audio track instead of handing
    # back a silent video.
    dash = (play.get("data") or {}).get("dash") or {}
    audios = dash.get("audio") or []
    if audios:
        best = max(audios, key=lambda a: a.get("bandwidth") or 0)
        play_url = best.get("base_url") or best.get("baseUrl")
        if play_url:
            return {**base, "kind": "audio",
                    "quality": "仅音频（无合成流，优先保声音）",
                    "play_url": play_url, "audio_only": True,
                    "filename": "bilibili_" + bvid + ".m4a",
                    "subtitle_text": None, "subtitle_lang": ""}
    raise ZhihuError("B站 playurl 接口没返回地址。",
                     "parse_failed", 422)


def bili_cookie_probe(cookie, test_text=""):
    """Validate a user-supplied Bilibili cookie the way the Zhihu
    cookie is validated in its own panel: a live login check via
    the nav endpoint, plus - when a test video is given - a real
    subtitle fetch against it, which is the whole point of saving
    the cookie. Returns a validation dict; never raises."""
    try:
        nav = _get_json(
            "https://api.bilibili.com/x/web-interface/nav",
            UA_DESKTOP,
            extra_headers={"Cookie": _bili_buvid() + "; " + cookie})
    except ZhihuError:
        return {"valid": False, "state": "unavailable",
                "message": "连接 Bilibili 失败，请稍后重试。"}
    data = nav.get("data") or {}
    if not data.get("isLogin"):
        return {"valid": False, "state": "failed",
                "message": "未识别为登录状态，Cookie 可能已过期或缺少 SESSDATA。"}
    uname = data.get("uname") or ""
    result = {"valid": True, "state": "valid",
              "message": "验证成功，已登录"
                         + ("：" + uname if uname else "") + "。"}
    m = re.search(r"BV[0-9A-Za-z]{10}", test_text or "")
    if m:
        bvid = m.group(0)
        page = "https://www.bilibili.com/video/" + bvid + "/"
        result["subtitle_found"] = False
        try:
            view = _bili_api("/x/web-interface/view", {"bvid": bvid},
                             page, cookie)
            if view.get("code") == 0:
                cid = view["data"]["pages"][0]["cid"]
                sub = _bili_subtitle(bvid, cid, page, cookie)
                if sub:
                    result["subtitle_found"] = True
                    result["subtitle_lang"] = sub["lang"]
                    result["message"] += (
                        "测试视频字幕已获取（"
                        + (sub["lang"] or "字幕") + "）。")
                else:
                    result["message"] += "测试视频未找到字幕轨（该视频可能没有字幕）。"
        except ZhihuError:
            result["message"] += "测试视频字幕探测失败。"
    return result


def parse_video(text, bili_cookie=""):
    """Dispatch on the share text, mirroring mini_parse.py."""
    if "douyin" in text or "v.douyin.com" in text:
        return douyin_lowest(text)
    if "bilibili" in text or "b23.tv" in text:
        return bilibili_lowest(text, bili_cookie)
    return bilibili_lowest(text, bili_cookie) if "BV" in text \
        else douyin_lowest(text)


# ---------------- download / playback proxy ----------------

def safe_filename(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "")[:80]
    return cleaned or "video.mp4"


def open_download(u, ref, range_header=None):
    """Validate a CDN URL against the allowlist and open the stream.

    Returns (status, headers, body_fileobj). Only hosts belonging to
    the two platforms' CDNs are accepted, so this endpoint cannot be
    used as an open proxy."""
    parsed = urlsplit(u or "")
    if parsed.scheme not in ("http", "https") or \
            not _host_matches(parsed.hostname, _DOWNLOAD_SUFFIXES):
        raise ZhihuError("下载地址不在允许的平台域名内。",
                         "invalid_url", 400)
    referer = "https://www.douyin.com/"
    ref_parsed = urlsplit(ref or "")
    if _host_matches(ref_parsed.hostname, _REFERER_SUFFIXES):
        referer = ref
    elif _host_matches(parsed.hostname,
                       ("bilibili.com", "bilivideo.com", "hdslb.com")):
        referer = "https://www.bilibili.com/"
    headers = {"User-Agent": UA_DESKTOP, "Referer": referer}
    if range_header:
        headers["Range"] = range_header
    req = Request(u, headers=headers)
    try:
        resp = build_opener().open(req, timeout=30)
    except HTTPError as exc:
        raise ZhihuError("平台 CDN 返回 " + str(exc.code) +
                         "，直链可能已过期，请重新解析。",
                         "upstream_error",
                         exc.code if exc.code in (403, 404, 416) else 502
                         ) from None
    except (URLError, OSError) as exc:
        raise ZhihuError("连接平台 CDN 失败：" + type(exc).__name__,
                         "upstream_error", 502) from None
    status = getattr(resp, "status", 200) or 200
    out = []
    for key in ("Content-Type", "Content-Length", "Content-Range",
                "Accept-Ranges"):
        value = resp.headers.get(key)
        if value:
            out.append((key, value))
    if not any(k == "Content-Type" for k, _ in out):
        out.append(("Content-Type", "video/mp4"))
    return status, out, resp
