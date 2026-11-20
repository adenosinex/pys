"""Bilibili transcription wiring tests (offline, stdlib only).

Covers the 2026-11-17 wiring: dispatch routing, the platform field,
the Bilibili subtitle leg, the dash audio leg, the token-API Mac
leg, and the _purge_old KeyError hardening plus same-shape siblings.
The old tests/test_core.py is expired (it calls create_app(api_key=...),
which no longer exists) and is not used here.

Run from the project root: python3 -m unittest discover -s tests -p "test_bili_wiring.py"
"""
import copy
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import zipfile
from wsgiref.util import setup_testing_defaults

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as app_module
import mac_asr
import video_parse
import yt_text
from extractor import ZhihuError
from site_router import SiteRouter
from site_settings import SiteSettings

BILI_URL = "https://www.bilibili.com/video/BV1VeHQ6tEaS/"
BV = "BV1VeHQ6tEaS"
BILI_PAGE = "https://www.bilibili.com/video/" + BV + "/"
DUMMY_TOKEN = "dummy-token-for-tests-only-0123456789abcdef"


class TempDataDir:
    """Temp DATA_DIR with no Bilibili cookie file, plus worker
    globals saved and restored (no real worker thread is started
    by tests using this helper unless they arrange it)."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ,
            {"DATA_DIR": self.tmp.name,
             "BILI_COOKIE_STATE_FILE": os.path.join(self.tmp.name, "no-cookie.json")})
        self.env.__enter__()
        self._queue = list(yt_text._queue)
        self._worker = dict(yt_text._worker_state)
        yt_text._queue.clear()
        return self.tmp.name

    def __exit__(self, *exc):
        yt_text._queue.clear()
        yt_text._queue.extend(self._queue)
        yt_text._worker_state.clear()
        yt_text._worker_state.update(self._worker)
        self.env.__exit__(*exc)
        self.tmp.cleanup()
        return False


def make_task(**kw):
    task = {"id": "aabbccddeeff", "url": BILI_URL, "format": "text",
            "platform": "bilibili", "state": "queued", "stage": "queued",
            "stage_label": "x", "detail": "", "stage_percent": None,
            "title": "", "origin": "", "created_at": time.time(),
            "updated_at": time.time(), "result": None, "error": None,
            "_mac_job_id": "", "_audio_name": ""}
    task.update(kw)
    return task


def write_task_file(data_dir, task):
    folder = Path(data_dir) / "yt-text" / "tasks"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (task["id"] + ".json")).write_text(
        json.dumps(task), encoding="utf-8")


def wsgi_post(app, path, payload):
    environ = {}
    setup_testing_defaults(environ)
    body = json.dumps(payload, ensure_ascii=False).encode()
    environ.update(REQUEST_METHOD="POST", PATH_INFO=path,
                   CONTENT_TYPE="application/json",
                   CONTENT_LENGTH=str(len(body)),
                   **{"wsgi.input": io.BytesIO(body)})
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    raw = b"".join(app(environ, start_response))
    return int(captured["status"].split()[0]), json.loads(raw.decode())


class FakeStore:
    def __init__(self, record):
        self.record = copy.deepcopy(record)

    def read(self):
        return copy.deepcopy(self.record)

    def write(self, record):
        self.record = copy.deepcopy(record)


class FakeDoc:
    def to_dict(self):
        return {"url": "https://example.com/", "kind": "proxy",
                "title": "T"}


class DispatchTest(unittest.TestCase):
    def _make_app(self, routes):
        settings = SiteSettings(
            FakeStore({"routes": routes, "shortcuts": []}))
        return app_module.create_app(extract_fn=lambda *a, **k: None,
                                     cookie_manager=object(),
                                     site_settings=settings)

    _UNSET = object()
    _DEFAULT_TASK = {"id": "aabbccddeeff", "state": "queued"}

    def _extract(self, app, url, create_result=_UNSET, create_error=None):
        if create_result is self._UNSET:
            create_result = dict(self._DEFAULT_TASK)
        with mock.patch.object(yt_text, "create_task") as mock_create, \
             mock.patch.object(SiteRouter, "extract",
                               return_value=FakeDoc()) as mock_router, \
             mock.patch.object(app_module, "render", return_value="TEXT"):
            if create_error is not None:
                mock_create.side_effect = create_error
            else:
                mock_create.return_value = create_result
            status, data = wsgi_post(
                app, "/api/extract",
                {"url": url, "format": "text", "keep_images": True})
        return status, data, mock_create, mock_router

    def test_bilibili_hosts_take_pipeline(self):
        app = self._make_app([])
        for host in ("bilibili.com", "www.bilibili.com",
                     "m.bilibili.com", "b23.tv"):
            url = "https://" + host + "/video/" + BV + "/"
            status, data, mock_create, mock_router = self._extract(app, url)
            self.assertEqual(status, 200, host)
            self.assertTrue(data["async"], host)
            self.assertEqual(data["task_id"], "aabbccddeeff", host)
            mock_create.assert_called_once_with(url, "text")
            mock_router.assert_not_called()
            mock_create.reset_mock()
            mock_router.reset_mock()

    def test_bilibili_host_variants(self):
        app = self._make_app([])
        for url in ("https://WWW.BILIBILI.COM/video/" + BV + "/",
                    "https://www.bilibili.com./video/" + BV + "/",
                    "  https://m.bilibili.com/video/" + BV + "/  "):
            status, data, mock_create, _ = self._extract(app, url)
            self.assertEqual(status, 200, url)
            self.assertTrue(data["async"], url)

    def test_youtube_still_takes_pipeline(self):
        app = self._make_app([])
        status, data, mock_create, mock_router = self._extract(
            app, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(status, 200)
        self.assertTrue(data["async"])
        mock_create.assert_called_once()
        mock_router.assert_not_called()

    def test_non_pipeline_hosts_take_router(self):
        app = self._make_app([])
        for url in ("https://www.douyin.com/video/123",
                    "https://evilbilibili.com/video/" + BV + "/",
                    "https://api.bilibili.com/x/web-interface/view",
                    "https://www.zhihu.com/question/123"):
            status, data, mock_create, mock_router = self._extract(app, url)
            self.assertEqual(status, 200, url)
            self.assertEqual(data["content"], "TEXT", url)
            mock_create.assert_not_called()
            mock_router.assert_called_once()
            mock_create.reset_mock()
            mock_router.reset_mock()

    def test_enabled_rule_wins_over_pipeline(self):
        app = self._make_app([{"enabled": True,
                               "hosts": ["www.bilibili.com"]}])
        status, _, mock_create, mock_router = self._extract(app, BILI_URL)
        self.assertEqual(status, 200)
        mock_create.assert_not_called()
        mock_router.assert_called_once()

    def test_wildcard_rule_matches_subdomain(self):
        app = self._make_app([{"enabled": True, "hosts": ["*.bilibili.com"]}])
        status, _, mock_create, mock_router = self._extract(app, BILI_URL)
        self.assertEqual(status, 200)
        mock_create.assert_not_called()
        mock_router.assert_called_once()

    def test_disabled_rule_does_not_win(self):
        app = self._make_app([{"enabled": False, "hosts": ["bilibili.com"]}])
        status, data, mock_create, mock_router = self._extract(app, BILI_URL)
        self.assertEqual(status, 200)
        self.assertTrue(data["async"])
        mock_create.assert_called_once()
        mock_router.assert_not_called()

    def test_pipeline_full_returns_429(self):
        app = self._make_app([])
        status, data, _, _ = self._extract(app, BILI_URL, create_result=None)
        # create_result None means the mock returns None: task list full.
        self.assertEqual(status, 429)
        self.assertEqual(data["code"], "busy")

    def test_create_task_oserror_falls_back_to_router(self):
        app = self._make_app([])
        status, data, mock_create, mock_router = self._extract(
            app, BILI_URL, create_error=OSError("disk"))
        self.assertEqual(status, 200)
        self.assertEqual(data["content"], "TEXT")
        mock_create.assert_called_once()
        mock_router.assert_called_once()

    def test_host_tables_agree(self):
        expected = {"bilibili.com", "www.bilibili.com",
                    "m.bilibili.com", "b23.tv"}
        self.assertEqual(yt_text.BILIBILI_HOSTS, expected)
        self.assertEqual(app_module.BILIBILI_HOSTS, expected)
        js = (Path(__file__).resolve().parent.parent / "static" / "app.js"
              ).read_text(encoding="utf-8")
        self.assertIn("'bilibili.com', 'www.bilibili.com', "
                      "'m.bilibili.com', 'b23.tv'", js)
        self.assertIn("bilibili\\.com|b23\\.tv", js)


class PlatformAndFinishTest(unittest.TestCase):
    def test_create_task_platform(self):
        with TempDataDir():
            with mock.patch.object(yt_text, "_ensure_worker", lambda: None):
                bili = yt_text.create_task(BILI_URL, "text")
                tube = yt_text.create_task(
                    "https://youtu.be/dQw4w9WgXcQ", "text")
            self.assertEqual(bili["platform"], "bilibili")
            self.assertEqual(tube["platform"], "youtube")

    def test_platform_for_unknown_hosts(self):
        self.assertEqual(yt_text._platform_for("not a url"), "youtube")
        self.assertEqual(yt_text._platform_for(""), "youtube")
        self.assertEqual(
            yt_text._platform_for("https://b23.tv/abc"), "bilibili")

    def test_finish_youtube_text_exact(self):
        with TempDataDir():
            task = make_task(platform="youtube",
                             url="https://www.youtube.com/watch?v=abc",
                             title="My Title", format="text",
                             _video_id="abc")
            yt_text._finish(task, "hello text",
                            "origin label", 1234)
            result = task["result"]
            self.assertEqual(result["ok"], True)
            self.assertEqual(result["kind"], "youtube")
            self.assertEqual(result["id"], "abc")
            self.assertEqual(result["title"], "My Title")
            self.assertEqual(result["author"], "YouTube")
            self.assertEqual(result["source"], "YouTube")
            self.assertEqual(result["format"], "text")
            self.assertEqual(result["content"], "hello text")
            self.assertEqual(result["origin"], "origin label")
            self.assertEqual(result["stats"]["fetched_bytes"], 1234)
            self.assertEqual(result["stats"]["output_chars"], 10)
            self.assertIsInstance(result["stats"]["elapsed_ms"], int)
            self.assertEqual(task["state"], "done")

    def test_finish_youtube_json_exact(self):
        with TempDataDir():
            task = make_task(platform="youtube",
                             url="https://www.youtube.com/watch?v=abc",
                             title="", format="json", _video_id="abc")
            yt_text._finish(task, "hello", "origin", 0)
            doc = json.loads(task["result"]["content"])
            self.assertEqual(doc, {
                "url": "https://www.youtube.com/watch?v=abc",
                "kind": "youtube", "id": "abc",
                "title": "YouTube 视频", "author": "YouTube",
                "published": "", "text": "hello", "markdown": "hello",
                "source": "YouTube", "extra": {"origin": "origin"}})

    def test_finish_bilibili_fields(self):
        with TempDataDir():
            task = make_task(platform="bilibili", title="",
                             _video_id=BV)
            yt_text._finish(task, "hello", "B站字幕", 0)
            result = task["result"]
            self.assertEqual(result["kind"], "bilibili")
            self.assertEqual(result["id"], BV)
            self.assertEqual(result["title"], "B站视频")
            self.assertEqual(result["author"], "B站")
            self.assertEqual(result["source"], "B站")
            task2 = make_task(platform="bilibili", title="Real Title",
                              format="json", _video_id=BV)
            yt_text._finish(task2, "hello", "B站字幕", 0)
            doc = json.loads(task2["result"]["content"])
            self.assertEqual(doc["kind"], "bilibili")
            self.assertEqual(doc["title"], "Real Title")
            self.assertEqual(doc["source"], "B站")

    def test_finish_missing_platform_defaults_youtube(self):
        with TempDataDir():
            task = make_task(title="")
            del task["platform"]
            yt_text._finish(task, "hello", "origin", 0)
            self.assertEqual(task["result"]["kind"], "youtube")
            self.assertEqual(task["result"]["title"], "YouTube 视频")


class BiliSubtitleTest(unittest.TestCase):
    def _run(self, meta=None, sub=None, cookie="SESSDATA=x"):
        task = make_task()
        with TempDataDir():
            with mock.patch.object(yt_text, "_bili_cookie",
                                   return_value=cookie), \
                 mock.patch.object(yt_text, "bili_video_meta") as mock_meta, \
                 mock.patch.object(yt_text, "_bili_subtitle") as mock_sub:
                if isinstance(meta, Exception):
                    mock_meta.side_effect = meta
                else:
                    mock_meta.return_value = meta or (
                        BV, 123, "Title", BILI_PAGE)
                mock_sub.return_value = sub
                result = yt_text._bili_subtitle_path(task)
        return result, task, mock_meta, mock_sub

    def test_subtitle_success(self):
        text = "x" * 30
        result, task, mock_meta, mock_sub = self._run(
            sub={"text": text, "lang": "中文", "lan": "zh-Hans"})
        self.assertEqual(result, (text, "B站字幕（中文）", 0))
        self.assertEqual(task["title"], "Title")
        self.assertEqual(task["_video_id"], BV)
        self.assertEqual(task["_bvid"], BV)
        self.assertEqual(task["_cid"], 123)
        self.assertEqual(task["_page"], BILI_PAGE)
        mock_meta.assert_called_once_with(BILI_URL, "SESSDATA=x")
        mock_sub.assert_called_once_with(BV, 123, BILI_PAGE, "SESSDATA=x")

    def test_subtitle_ai_origin(self):
        result, _, _, _ = self._run(
            sub={"text": "y" * 30, "lang": "中文", "lan": "ai-zh"})
        self.assertEqual(result[1], "B站字幕（AI · 中文）")

    def test_subtitle_origin_without_doc(self):
        result, _, _, _ = self._run(
            sub={"text": "y" * 30, "lang": "", "lan": "zh"})
        self.assertEqual(result[1], "B站字幕")
        result, _, _, _ = self._run(
            sub={"text": "y" * 30, "lang": "", "lan": "AI-zh"})
        self.assertEqual(result[1], "B站AI字幕")

    def test_subtitle_short_text_falls_back(self):
        result, _, _, _ = self._run(
            sub={"text": "x" * 19, "lang": "中文", "lan": "zh"})
        self.assertEqual(result, (None, "", 0))

    def test_subtitle_missing_falls_back(self):
        result, _, _, _ = self._run(sub=None)
        self.assertEqual(result, (None, "", 0))

    def test_meta_failure_falls_back(self):
        result, _, _, mock_sub = self._run(
            meta=ZhihuError("view failed", "parse_failed", 422),
            sub={"text": "y" * 30, "lang": "", "lan": "zh"})
        self.assertEqual(result, (None, "", 0))
        mock_sub.assert_not_called()


class BiliCookieTest(unittest.TestCase):
    def test_cookie_from_state_file(self):
        from cookie_manager import FileCookieStore
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bili-cookie-state.json")
            FileCookieStore(path).write(
                {"cookie": "SESSDATA=abc", "test_url": BILI_URL})
            with mock.patch.dict(os.environ,
                                 {"BILI_COOKIE_STATE_FILE": path}):
                self.assertEqual(yt_text._bili_cookie(), "SESSDATA=abc")

    def test_cookie_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"BILI_COOKIE_STATE_FILE":
                                              os.path.join(d, "none.json")}):
                self.assertEqual(yt_text._bili_cookie(), "")

    def test_cookie_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bili-cookie-state.json")
            Path(path).write_text("{not json", encoding="utf-8")
            with mock.patch.dict(os.environ,
                                 {"BILI_COOKIE_STATE_FILE": path}):
                self.assertEqual(yt_text._bili_cookie(), "")

    def test_cookie_default_path_fallback(self):
        from cookie_manager import FileCookieStore
        with tempfile.TemporaryDirectory() as d:
            runtime = Path(d) / ".runtime"
            runtime.mkdir()
            FileCookieStore(str(runtime / "bili-cookie-state.json")).write(
                {"cookie": "SESSDATA=default", "test_url": BILI_URL})
            env = {"DATA_DIR": d}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(yt_text, "__file__",
                                   os.path.join(d, "yt_text.py")):
                self.assertEqual(yt_text._bili_cookie(), "SESSDATA=default")


class VideoParseTest(unittest.TestCase):
    def test_bili_video_meta(self):
        view = {"code": 0, "data": {"title": "T",
                                    "pages": [{"cid": 123}]}} 
        with mock.patch.object(video_parse, "_final_url",
                               return_value=BILI_PAGE), \
             mock.patch.object(video_parse, "_bili_api",
                               return_value=view) as mock_api:
            self.assertEqual(
                video_parse.bili_video_meta("https://b23.tv/abc", "ck"),
                (BV, 123, "T", BILI_PAGE))
            mock_api.assert_called_once_with(
                "/x/web-interface/view", {"bvid": BV}, BILI_PAGE, "ck")

    def test_bili_video_meta_bv_in_share_text(self):
        view = {"code": 0, "data": {"title": "T",
                                    "pages": [{"cid": 9}]}} 
        with mock.patch.object(video_parse, "_final_url",
                               return_value="https://www.bilibili.com/"), \
             mock.patch.object(video_parse, "_bili_api",
                               return_value=view):
            result = video_parse.bili_video_meta(
                "看看这个 https://b23.tv/xyz " + BV, "")
            self.assertEqual(result[0], BV)
            self.assertEqual(result[1], 9)

    def test_bili_video_meta_failures(self):
        with mock.patch.object(video_parse, "_final_url",
                               return_value=BILI_PAGE), \
             mock.patch.object(video_parse, "_bili_api") as mock_api:
            mock_api.return_value = {"code": -404, "data": {}}
            with self.assertRaises(ZhihuError):
                video_parse.bili_video_meta("https://b23.tv/abc", "")
            mock_api.return_value = {"code": 0, "data": {"pages": []}}
            with self.assertRaises(ZhihuError):
                video_parse.bili_video_meta("https://b23.tv/abc", "")
            mock_api.return_value = {"code": 0,
                                     "data": {"pages": [{}]}}
            with self.assertRaises(ZhihuError):
                video_parse.bili_video_meta("https://b23.tv/abc", "")
        with self.assertRaises(ZhihuError):
            video_parse.bili_video_meta("no link here", "")

    def test_bilibili_lowest_regression_video(self):
        view = {"code": 0, "data": {"title": "T",
                                    "pages": [{"cid": 123}]}}
        play = {"data": {"durl": [{"url": "https://cdn.example.com/v.mp4"}]}}
        with mock.patch.object(video_parse, "_final_url",
                               return_value=BILI_PAGE), \
             mock.patch.object(video_parse, "_bili_api",
                               side_effect=[view, play]), \
             mock.patch.object(video_parse, "_bili_subtitle",
                               return_value=None):
            result = video_parse.bilibili_lowest("https://b23.tv/abc", "")
        self.assertEqual(result, {
            "platform": "bilibili", "platform_name": "B站",
            "id": BV, "title": "T", "referer": BILI_PAGE,
            "page_url": BILI_PAGE, "source_repo": video_parse.SOURCE_REPO,
            "kind": "video", "quality": "最低画质 · 360P",
            "play_url": "https://cdn.example.com/v.mp4",
            "audio_only": False, "filename": "bilibili_" + BV + ".mp4",
            "subtitle_text": None, "subtitle_lang": ""})

    def test_bilibili_lowest_regression_subtitle(self):
        view = {"code": 0, "data": {"title": "T",
                                    "pages": [{"cid": 123}]}}
        with mock.patch.object(video_parse, "_final_url",
                               return_value=BILI_PAGE), \
             mock.patch.object(video_parse, "_bili_api",
                               return_value=view) as mock_api, \
             mock.patch.object(video_parse, "_bili_subtitle", return_value={
                 "text": "SUB", "lang": "中文", "lan": "zh"}):
            result = video_parse.bilibili_lowest("https://b23.tv/abc", "")
        self.assertEqual(result["kind"], "subtitle")
        self.assertEqual(result["subtitle_text"], "SUB")
        self.assertEqual(result["subtitle_lang"], "中文")
        self.assertIsNone(result["play_url"])
        mock_api.assert_called_once()  # playurl never fetched

    def test_bili_subtitle_returns_lan(self):
        tracks = {"data": {"subtitle": {"subtitles": [
            {"lan": "ai-zh", "lan_doc": "中文",
             "subtitle_url": "//example.com/s.json"}]}}}
        with mock.patch.object(video_parse, "_wbi_sign",
                               return_value={}), \
             mock.patch.object(video_parse, "_bili_api",
                               return_value=tracks), \
             mock.patch.object(video_parse, "_get_json", return_value={
                 "body": [{"content": "a"}, {"content": "b"}]}):
            result = video_parse._bili_subtitle(BV, 123, BILI_PAGE, "")
        self.assertEqual(result, {"text": "a\nb", "lang": "中文",
                                  "lan": "ai-zh"})

    def test_bili_dash_audio_url_best_bandwidth(self):
        play = {"data": {"dash": {"audio": [
            {"bandwidth": 100, "base_url": "https://cdn/a1.m4a"},
            {"bandwidth": 300, "baseUrl": "https://cdn/a2.m4a"},
            {"bandwidth": 200, "base_url": "https://cdn/a3.m4a"}]}}}
        with mock.patch.object(video_parse, "_bili_api",
                               return_value=play) as mock_api:
            url = video_parse.bili_dash_audio_url(BV, 123, BILI_PAGE, "")
        self.assertEqual(url, "https://cdn/a2.m4a")
        mock_api.assert_called_once()
        params = mock_api.call_args[0][1]
        self.assertEqual(params["fnval"], "16")
        # platform=html5 forces the muxed durl shape and suppresses
        # dash entirely (verified live 2026-11-17); it must be absent.
        self.assertNotIn("platform", params)

    def test_bili_dash_audio_url_ignores_durl_only(self):
        play = {"data": {"durl": [
            {"url": "https://cdn/muxed.mp4"}]}}
        with mock.patch.object(video_parse, "_bili_api",
                               return_value=play):
            with self.assertRaises(ZhihuError):
                video_parse.bili_dash_audio_url(BV, 123, BILI_PAGE, "")

    def test_bili_dash_audio_url_none(self):
        with mock.patch.object(video_parse, "_bili_api",
                               return_value={"data": {}}):
            with self.assertRaises(ZhihuError):
                video_parse.bili_dash_audio_url(BV, 123, BILI_PAGE, "")


class FakeResp:
    def __init__(self, chunks, length=None):
        self._chunks = list(chunks)
        total = length if length is not None else sum(len(c) for c in chunks)
        self.headers = {"Content-Length": str(total)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class DashDownloadTest(unittest.TestCase):
    def _run(self, task, chunks, length=None, urlopen_error=None,
             meta=None):
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            captured = {}

            def fake_urlopen(req, timeout=None):
                captured["req"] = req
                if urlopen_error is not None:
                    raise urlopen_error
                return FakeResp(chunks, length)

            with mock.patch.object(yt_text, "bili_dash_audio_url",
                                   return_value="https://cdn/a.m4a"), \
                 mock.patch.object(yt_text, "bili_video_meta") as mock_meta, \
                 mock.patch("urllib.request.urlopen", fake_urlopen):
                if isinstance(meta, Exception):
                    mock_meta.side_effect = meta
                else:
                    mock_meta.return_value = meta or (
                        BV, 123, "Title", BILI_PAGE)
                result = yt_text._dash_audio_download(task, folder)
                content = result.read_bytes()
                name = result.name
        return name, content, captured, mock_meta

    def test_download_success_with_stash(self):
        task = make_task(_bvid=BV, _cid=123, _page=BILI_PAGE)
        name, content, captured, mock_meta = self._run(
            task, [b"aa", b"bb", b"cc"])
        self.assertEqual(name, "audio.m4a")
        self.assertEqual(content, b"aabbcc")
        self.assertEqual(task["_audio_name"], "audio.m4a")
        self.assertEqual(task["_audio_size"], 6)
        mock_meta.assert_not_called()
        req = captured["req"]
        self.assertEqual(req.get_header("Referer"), BILI_PAGE)
        self.assertEqual(req.get_header("Range"), "bytes=0-")
        self.assertEqual(req.get_header("User-agent"), video_parse.UA_DESKTOP)
        self.assertEqual(req.full_url, "https://cdn/a.m4a")

    def test_download_fetches_meta_without_stash(self):
        task = make_task(title="")
        name, content, _, mock_meta = self._run(task, [b"zz"])
        self.assertEqual((name, content), ("audio.m4a", b"zz"))
        mock_meta.assert_called_once_with(BILI_URL, "")
        self.assertEqual(task["_bvid"], BV)
        self.assertEqual(task["_cid"], 123)
        self.assertEqual(task["title"], "Title")

    def test_download_meta_failure(self):
        task = make_task()
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(
                    yt_text, "bili_video_meta",
                    side_effect=ZhihuError("gone", "parse_failed", 422)):
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._dash_audio_download(task, folder)
        self.assertEqual(ctx.exception.stage, "download")

    def test_download_no_audio_url(self):
        task = make_task(_bvid=BV, _cid=123, _page=BILI_PAGE)
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(
                    yt_text, "bili_dash_audio_url",
                    side_effect=ZhihuError("none", "parse_failed", 422)):
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._dash_audio_download(task, folder)
        self.assertEqual(ctx.exception.stage, "download")

    def test_download_size_cap_from_header(self):
        task = make_task(_bvid=BV, _cid=123, _page=BILI_PAGE)
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(yt_text, "bili_dash_audio_url",
                                   return_value="https://cdn/a.m4a"), \
                 mock.patch.object(yt_text, "MAX_AUDIO_BYTES", 10), \
                 mock.patch("urllib.request.urlopen",
                            return_value=FakeResp([b"x" * 12], length=12)):
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._dash_audio_download(task, folder)
        self.assertIn("300 MB", ctx.exception.message)

    def test_download_size_cap_mid_stream(self):
        task = make_task(_bvid=BV, _cid=123, _page=BILI_PAGE)
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(yt_text, "bili_dash_audio_url",
                                   return_value="https://cdn/a.m4a"), \
                 mock.patch.object(yt_text, "MAX_AUDIO_BYTES", 10), \
                 mock.patch("urllib.request.urlopen", return_value=FakeResp(
                     [b"x" * 6, b"x" * 6], length=5)):
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._dash_audio_download(task, folder)
        self.assertIn("300 MB", ctx.exception.message)

    def test_download_empty(self):
        task = make_task(_bvid=BV, _cid=123, _page=BILI_PAGE)
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(yt_text, "bili_dash_audio_url",
                                   return_value="https://cdn/a.m4a"), \
                 mock.patch("urllib.request.urlopen",
                            return_value=FakeResp([], length=0)):
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._dash_audio_download(task, folder)
        self.assertIn("为空", ctx.exception.message)

    def test_download_transport_error(self):
        task = make_task(_bvid=BV, _cid=123, _page=BILI_PAGE)
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(yt_text, "bili_dash_audio_url",
                                   return_value="https://cdn/a.m4a"), \
                 mock.patch("urllib.request.urlopen",
                            side_effect=urllib.error.URLError("boom")):
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._dash_audio_download(task, folder)
        self.assertEqual(ctx.exception.stage, "download")

    def test_download_deadline(self):
        task = make_task(_bvid=BV, _cid=123, _page=BILI_PAGE)
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(yt_text, "bili_dash_audio_url",
                                   return_value="https://cdn/a.m4a"), \
                 mock.patch.object(yt_text, "DL_DEADLINE_SEC", -1), \
                 mock.patch("urllib.request.urlopen",
                            return_value=FakeResp([b"x" * 6], length=6)):
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._dash_audio_download(task, folder)
        self.assertIn("超时", ctx.exception.message)


class FakeHTTPResp:
    def __init__(self, status, data):
        self.status = status
        self._data = data

    def read(self):
        return self._data


class FakeConn:
    response = FakeHTTPResp(200, b"{}")
    fail_request = None
    instances = []

    def __init__(self, host, port=None, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.requests = []
        FakeConn.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        if FakeConn.fail_request is not None:
            raise FakeConn.fail_request
        if isinstance(body, mac_asr._CountingBody):
            chunks = []
            while True:
                chunk = body.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            body_bytes = b"".join(chunks)
        else:
            body_bytes = body
        self.requests.append((method, path, body_bytes, headers))

    def getresponse(self):
        return FakeConn.response

    def close(self):
        pass


class MacAsrClientTest(unittest.TestCase):
    def setUp(self):
        FakeConn.response = FakeHTTPResp(200, b"{}")
        FakeConn.fail_request = None
        FakeConn.instances.clear()
        self.conn_patch = mock.patch.object(http.client, "HTTPSConnection",
                                            FakeConn)
        self.conn_patch.start()

    def tearDown(self):
        self.conn_patch.stop()

    def test_request_headers_and_host(self):
        status, data = mac_asr.request("GET", "/api/jobs", DUMMY_TOKEN)
        conn = FakeConn.instances[-1]
        self.assertEqual((conn.host, conn.port), ("cp.zin7.de", 443))
        method, path, body, headers = conn.requests[0]
        self.assertEqual((method, path), ("GET", "/api/jobs"))
        self.assertEqual(headers["X-Access-Code"], DUMMY_TOKEN)
        self.assertEqual(status, 200)

    def test_submit_multipart(self):
        FakeConn.response = FakeHTTPResp(200, b'{"id": "job1"}')
        progress = []
        with tempfile.TemporaryDirectory() as d:
            audio = Path(d) / "audio.m4a"
            audio.write_bytes(b"AUDIOBYTES")
            job_id = mac_asr.submit(audio, DUMMY_TOKEN,
                                    lambda s, t: progress.append((s, t)))
        self.assertEqual(job_id, "job1")
        method, path, body, headers = FakeConn.instances[-1].requests[0]
        self.assertEqual((method, path), ("POST", "/api/jobs"))
        self.assertIn(b'name="type"\r\n\r\nasr\r\n', body)
        self.assertIn(b'name="params"\r\n\r\n{}\r\n', body)
        self.assertIn(b'filename="audio.m4a"', body)
        self.assertIn(b"AUDIOBYTES", body)
        self.assertTrue(
            headers["Content-Type"].startswith("multipart/form-data; "))
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(headers["X-Access-Code"], DUMMY_TOKEN)
        self.assertTrue(progress)
        self.assertEqual(progress[-1], (len(body), len(body)))

    def test_submit_job_id_shapes(self):
        for payload, expected in ((b'{"job_id": "j2"}', "j2"),
                                  (b'{"job": {"id": "j3"}}', "j3"),
                                  (b'{"id": 42}', "42")):
            FakeConn.response = FakeHTTPResp(201, payload)
            with tempfile.TemporaryDirectory() as d:
                audio = Path(d) / "a.m4a"
                audio.write_bytes(b"x")
                self.assertEqual(mac_asr.submit(audio, DUMMY_TOKEN),
                                 expected)

    def test_submit_errors(self):
        with tempfile.TemporaryDirectory() as d:
            audio = Path(d) / "a.m4a"
            audio.write_bytes(b"x")
            FakeConn.response = FakeHTTPResp(401, b"{}")
            with self.assertRaises(mac_asr.MacAsrError) as ctx:
                mac_asr.submit(audio, DUMMY_TOKEN)
            self.assertEqual(ctx.exception.status, 401)
            FakeConn.response = FakeHTTPResp(500, b"{}")
            with self.assertRaises(mac_asr.MacAsrError) as ctx:
                mac_asr.submit(audio, DUMMY_TOKEN)
            self.assertEqual(ctx.exception.status, 500)
            FakeConn.response = FakeHTTPResp(200, b"not json")
            with self.assertRaises(mac_asr.MacAsrError):
                mac_asr.submit(audio, DUMMY_TOKEN)
            FakeConn.response = FakeHTTPResp(200, b"{}")
            with self.assertRaises(mac_asr.MacAsrError):
                mac_asr.submit(audio, DUMMY_TOKEN)
            FakeConn.fail_request = OSError("down")
            with self.assertRaises(mac_asr.MacAsrError) as ctx:
                mac_asr.submit(audio, DUMMY_TOKEN)
            self.assertIsNone(ctx.exception.status)

    def test_get_job_and_download(self):
        FakeConn.response = FakeHTTPResp(
            200, b'{"id": "j", "status": "done"}')
        job = mac_asr.get_job(DUMMY_TOKEN, "j")
        self.assertEqual(job["status"], "done")
        self.assertEqual(FakeConn.instances[-1].requests[0][1],
                         "/api/jobs/j")
        FakeConn.response = FakeHTTPResp(200, b"ZIPBYTES")
        self.assertEqual(mac_asr.download(DUMMY_TOKEN, "j"), b"ZIPBYTES")
        FakeConn.response = FakeHTTPResp(401, b"{}")
        with self.assertRaises(mac_asr.MacAsrError) as ctx:
            mac_asr.get_job(DUMMY_TOKEN, "j")
        self.assertEqual(ctx.exception.status, 401)
        FakeConn.fail_request = OSError("down")
        with self.assertRaises(mac_asr.MacAsrError) as ctx:
            mac_asr.get_job(DUMMY_TOKEN, "j")
        self.assertIsNone(ctx.exception.status)
        FakeConn.fail_request = None
        FakeConn.response = FakeHTTPResp(200, b"junk")
        with self.assertRaises(mac_asr.MacAsrError):
            mac_asr.get_job(DUMMY_TOKEN, "j")

    def test_cancel_swallows_errors(self):
        FakeConn.fail_request = OSError("down")
        mac_asr.cancel(DUMMY_TOKEN, "j")  # must not raise
        FakeConn.fail_request = None
        FakeConn.response = FakeHTTPResp(500, b"{}")
        mac_asr.cancel(DUMMY_TOKEN, "j")


class TokenConfigTest(unittest.TestCase):
    def test_token_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"DATA_DIR": d}, clear=True):
                self.assertFalse(yt_text.mac_asr_token_configured())
                self.assertEqual(yt_text._mac_asr_token(), "")

    def test_token_from_credentials_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "credentials.json").write_text(
                json.dumps({"mac_asr_token": "  dummy123  ",
                            "other": 1}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"DATA_DIR": d}, clear=True):
                self.assertTrue(yt_text.mac_asr_token_configured())
                self.assertEqual(yt_text._mac_asr_token(), "dummy123")

    def test_token_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "credentials.json").write_text(
                json.dumps({"mac_asr_token": "file-token"}),
                encoding="utf-8")
            env = {"DATA_DIR": d, "MAC_ASR_TOKEN": "env-token"}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(yt_text._mac_asr_token(), "env-token")


def make_zip(**files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class AsrCollectTest(unittest.TestCase):
    def _collect(self, jobs, download_bytes=None, download_error=None):
        task = make_task(_mac_job_id="job1", _mac_started=time.time(),
                         _audio_size=999)
        with TempDataDir():
            with mock.patch.object(mac_asr, "get_job",
                                   side_effect=list(jobs)), \
                 mock.patch.object(mac_asr, "download") as mock_dl, \
                 mock.patch.object(mac_asr, "cancel") as mock_cancel, \
                 mock.patch.object(yt_text, "_asr_position",
                                   return_value=None), \
                 mock.patch("time.sleep", lambda s: None):
                if download_error is not None:
                    mock_dl.side_effect = download_error
                else:
                    mock_dl.return_value = download_bytes
                yt_text._asr_collect(task)
        return task, mock_cancel

    def test_collect_happy_path(self):
        text = "x" * 30
        jobs = [{"id": "job1", "status": "queued"},
                {"id": "job1", "status": "running", "progress": 0.5,
                 "message": "working"},
                {"id": "job1", "status": "done"}]
        task, _ = self._collect(jobs, make_zip(**{"result.txt": text}))
        self.assertEqual(task["state"], "done")
        self.assertEqual(task["result"]["content"], text)
        self.assertEqual(task["origin"], "音频转写（Mac ASR）")
        self.assertEqual(task["result"]["stats"]["fetched_bytes"], 999)

    def test_collect_srt_fallback(self):
        srt = ("1\n00:00:01,000 --> 00:00:03,000\n"
               "这是第一句比较长的字幕内容\n\n"
               "2\n00:00:04,000 --> 00:00:06,000\n"
               "这是第二句比较长的字幕内容\n")
        jobs = [{"id": "job1", "status": "done"}]
        task, _ = self._collect(
            jobs, make_zip(**{"result.txt": "tiny", "result.srt": srt}))
        self.assertEqual(task["state"], "done")
        self.assertIn("这是第一句比较长的字幕内容",
                      task["result"]["content"])

    def test_collect_empty_result(self):
        jobs = [{"id": "job1", "status": "done"}]
        with self.assertRaises(yt_text.TaskFail) as ctx:
            self._collect(jobs, make_zip(**{"other.txt": "x"}))
        self.assertEqual(ctx.exception.stage, "assemble")

    def test_collect_bad_zip(self):
        jobs = [{"id": "job1", "status": "done"}]
        with self.assertRaises(yt_text.TaskFail) as ctx:
            self._collect(jobs, b"not a zip")
        self.assertIn("损坏", ctx.exception.message)

    def test_collect_failed_status(self):
        jobs = [{"id": "job1", "status": "failed", "error": "boom"}]
        with self.assertRaises(yt_text.TaskFail) as ctx:
            self._collect(jobs, b"")
        self.assertEqual(ctx.exception.stage, "asr")
        self.assertIn("boom", ctx.exception.message)

    def test_collect_unauthorized(self):
        jobs = [mac_asr.MacAsrError("no", status=401)]
        with mock.patch.object(mac_asr, "get_job",
                               side_effect=jobs), \
             mock.patch("time.sleep", lambda s: None):
            task = make_task(_mac_job_id="job1",
                             _mac_started=time.time())
            with TempDataDir(), self.assertRaises(yt_text.TaskFail) as ctx:
                yt_text._asr_collect(task)
        self.assertIn("token 被拒绝", ctx.exception.message)

    def test_collect_transport_misses_then_gives_up(self):
        err = mac_asr.MacAsrError("down")
        with mock.patch.object(mac_asr, "get_job",
                               side_effect=[err] * 6), \
             mock.patch("time.sleep", lambda s: None):
            task = make_task(_mac_job_id="job1",
                             _mac_started=time.time())
            with TempDataDir(), self.assertRaises(yt_text.TaskFail) as ctx:
                yt_text._asr_collect(task)
        self.assertIn("无法连接", ctx.exception.message)

    def test_collect_http_error_raises_at_once(self):
        err = mac_asr.MacAsrError("HTTP 500", status=500)
        with mock.patch.object(mac_asr, "get_job", side_effect=[err]), \
             mock.patch("time.sleep", lambda s: None):
            task = make_task(_mac_job_id="job1",
                             _mac_started=time.time())
            with TempDataDir(), self.assertRaises(yt_text.TaskFail) as ctx:
                yt_text._asr_collect(task)
        self.assertEqual(ctx.exception.message, "HTTP 500")

    def test_collect_timeout_cancels(self):
        task = make_task(_mac_job_id="job1",
                         _mac_started=time.time() - 4000)
        with TempDataDir(), \
             mock.patch.object(mac_asr, "cancel") as mock_cancel, \
             mock.patch.object(mac_asr, "get_job") as mock_get:
            with self.assertRaises(yt_text.TaskFail) as ctx:
                yt_text._asr_collect(task)
        self.assertIn("超时", ctx.exception.message)
        mock_get.assert_not_called()
        mock_cancel.assert_called_once()

    def test_asr_position(self):
        payload = [{"id": "a", "status": "running"},
                   {"id": "mine", "status": "queued"},
                   {"id": "b", "status": "done"}]
        with mock.patch.object(mac_asr, "request", return_value=(
                200, json.dumps(payload).encode())):
            self.assertEqual(
                yt_text._asr_position(DUMMY_TOKEN, {"id": "mine"}), 2)
            self.assertIsNone(
                yt_text._asr_position(DUMMY_TOKEN, {"id": "ghost"}))
        with mock.patch.object(mac_asr, "request", return_value=(
                200, json.dumps({"jobs": payload}).encode())):
            self.assertEqual(
                yt_text._asr_position(DUMMY_TOKEN, {"id": "mine"}), 2)
        with mock.patch.object(mac_asr, "request",
                               return_value=(500, b"")):
            self.assertIsNone(
                yt_text._asr_position(DUMMY_TOKEN, {"id": "mine"}))
        with mock.patch.object(mac_asr, "request",
                               side_effect=OSError("down")):
            self.assertIsNone(
                yt_text._asr_position(DUMMY_TOKEN, {"id": "mine"}))

    def test_asr_submit_progress_and_errors(self):
        task = make_task()
        with TempDataDir():
            captured = {}

            def fake_submit(path, token, on_progress):
                captured["cb"] = on_progress
                captured["token"] = token
                return "job9"

            with mock.patch.object(mac_asr, "submit",
                                   side_effect=fake_submit), \
                 mock.patch.object(yt_text, "_mac_asr_token",
                                   return_value=DUMMY_TOKEN):
                job_id = yt_text._asr_submit(task, Path("/tmp/a.m4a"))
            self.assertEqual(job_id, "job9")
            self.assertEqual(captured["token"], DUMMY_TOKEN)
            captured["cb"](50, 100)
            self.assertEqual(task["stage"], "upload")
            self.assertEqual(task["stage_percent"], 50.0)
            with mock.patch.object(
                    mac_asr, "submit",
                    side_effect=mac_asr.MacAsrError("no", status=401)):
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._asr_submit(task, Path("/tmp/a.m4a"))
            self.assertIn("token 被拒绝", ctx.exception.message)


class AudioPathTest(unittest.TestCase):
    def _run_audio_path(self, task, *, token=True, code=True,
                        saved_file=False):
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            audio = folder / "audio.m4a"
            if saved_file:
                audio.write_bytes(b"OLD")
                task["_audio_name"] = "audio.m4a"
            calls = {}
            with mock.patch.object(yt_text, "mac_asr_token_configured",
                                   return_value=token), \
                 mock.patch.object(yt_text, "mac_code_configured",
                                   return_value=code), \
                 mock.patch.object(yt_text, "_dash_audio_download",
                                   return_value=audio) as mock_dash, \
                 mock.patch.object(yt_text, "_ytaudio_download",
                                   return_value=audio) as mock_yt, \
                 mock.patch.object(yt_text, "_asr_submit",
                                   return_value="job1") as mock_asr, \
                 mock.patch.object(yt_text, "_mac_submit",
                                   return_value="job2") as mock_mac, \
                 mock.patch.object(yt_text, "_asr_collect") as mock_ac, \
                 mock.patch.object(yt_text, "_mac_collect") as mock_mc:
                yt_text._audio_path(task)
                calls.update(dash=mock_dash, yt=mock_yt, asr=mock_asr,
                             mac=mock_mac, ac=mock_ac, mc=mock_mc,
                             audio=audio)
        return task, calls

    def test_bilibili_full_leg(self):
        task = make_task()
        task, calls = self._run_audio_path(task)
        calls["dash"].assert_called_once()
        calls["yt"].assert_not_called()
        calls["asr"].assert_called_once()
        calls["mac"].assert_not_called()
        calls["ac"].assert_called_once_with(task)
        calls["mc"].assert_not_called()
        self.assertEqual(task["_mac_job_id"], "job1")
        self.assertFalse(calls["audio"].exists())
        self.assertEqual(task["_audio_name"], "")

    def test_bilibili_no_token_fails_at_upload(self):
        task = make_task()
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            audio = folder / "audio.m4a"
            with mock.patch.object(yt_text, "mac_asr_token_configured",
                                   return_value=False), \
                 mock.patch.object(yt_text, "_dash_audio_download",
                                   return_value=audio) as mock_dash:
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._audio_path(task)
            mock_dash.assert_called_once()
        self.assertEqual(ctx.exception.stage, "upload")
        self.assertIn("mac_asr_token", ctx.exception.message)

    def test_bilibili_saved_audio_skips_download(self):
        task = make_task()
        task, calls = self._run_audio_path(task, saved_file=True)
        calls["dash"].assert_not_called()
        calls["asr"].assert_called_once()
        self.assertFalse(calls["audio"].exists())

    def test_youtube_old_leg_unchanged(self):
        task = make_task(platform="youtube",
                         url="https://www.youtube.com/watch?v=abc")
        task, calls = self._run_audio_path(task)
        calls["yt"].assert_called_once()
        calls["dash"].assert_not_called()
        calls["mac"].assert_called_once()
        calls["asr"].assert_not_called()
        calls["mc"].assert_called_once_with(task)
        calls["ac"].assert_not_called()
        self.assertEqual(task["_mac_job_id"], "job2")

    def test_youtube_no_code_old_message(self):
        task = make_task(platform="youtube",
                         url="https://www.youtube.com/watch?v=abc")
        with TempDataDir() as d:
            folder = Path(d) / "yt-text" / "tmp" / task["id"]
            folder.mkdir(parents=True, exist_ok=True)
            audio = folder / "audio.m4a"
            with mock.patch.object(yt_text, "mac_code_configured",
                                   return_value=False), \
                 mock.patch.object(yt_text, "_ytaudio_download",
                                   return_value=audio):
                with self.assertRaises(yt_text.TaskFail) as ctx:
                    yt_text._audio_path(task)
        self.assertIn("访问码尚未配置", ctx.exception.message)


class RunTaskRoutingTest(unittest.TestCase):
    def _run(self, task, subtitle_result=("TEXT", "origin", 0)):
        with TempDataDir(), \
             mock.patch.object(yt_text, "_subtitle_path",
                               return_value=subtitle_result) as mock_yt, \
             mock.patch.object(yt_text, "_bili_subtitle_path",
                               return_value=subtitle_result) as mock_bili, \
             mock.patch.object(yt_text, "_audio_path") as mock_audio, \
             mock.patch.object(yt_text, "_finish") as mock_finish, \
             mock.patch.object(yt_text, "_mac_collect") as mock_mc, \
             mock.patch.object(yt_text, "_asr_collect") as mock_ac:
            yt_text._run_task(task)
        return {"yt": mock_yt, "bili": mock_bili, "audio": mock_audio,
                "finish": mock_finish, "mc": mock_mc, "ac": mock_ac}

    def test_youtube_subtitle_path(self):
        task = make_task(platform="youtube")
        calls = self._run(task)
        calls["yt"].assert_called_once_with(task)
        calls["bili"].assert_not_called()
        calls["finish"].assert_called_once_with(task, "TEXT", "origin", 0)
        calls["audio"].assert_not_called()

    def test_bilibili_subtitle_path(self):
        task = make_task()
        calls = self._run(task)
        calls["bili"].assert_called_once_with(task)
        calls["yt"].assert_not_called()
        calls["finish"].assert_called_once_with(task, "TEXT", "origin", 0)

    def test_no_subtitle_goes_audio(self):
        task = make_task()
        calls = self._run(task, subtitle_result=(None, "", 0))
        calls["audio"].assert_called_once_with(task)
        calls["finish"].assert_not_called()

    def test_resume_bilibili_collects_new_leg(self):
        task = make_task(_mac_job_id="job1")
        calls = self._run(task)
        calls["ac"].assert_called_once_with(task)
        calls["mc"].assert_not_called()
        calls["bili"].assert_not_called()

    def test_resume_youtube_collects_old_leg(self):
        task = make_task(platform="youtube", _mac_job_id="job1")
        calls = self._run(task)
        calls["mc"].assert_called_once_with(task)
        calls["ac"].assert_not_called()

    def test_resume_missing_platform_defaults_old_leg(self):
        task = make_task(_mac_job_id="job1")
        del task["platform"]
        calls = self._run(task)
        calls["mc"].assert_called_once_with(task)
        calls["ac"].assert_not_called()
        calls["yt"].assert_not_called()


class PurgeHardeningTest(unittest.TestCase):
    def test_purge_old_with_legacy_records(self):
        with TempDataDir() as d:
            now = time.time()
            old = now - 25 * 3600
            cases = [
                ({"id": "111111111111", "created_at": old,
                  "state": "done"}, False),
                ({"id": "222222222222", "created_at": old,
                  "state": "error"}, False),
                ({"id": "333333333333", "created_at": old,
                  "state": "queued"}, True),
                ({"id": "444444444444", "created_at": old}, True),
                ({"id": "555555555555", "state": "done"}, False),
                ({"id": "666666666666", "created_at": now,
                  "state": "done"}, True),
            ]
            for task, _ in cases:
                write_task_file(d, task)
            tmp_folder = Path(d) / "yt-text" / "tmp" / "111111111111"
            tmp_folder.mkdir(parents=True, exist_ok=True)
            (tmp_folder / "x.bin").write_bytes(b"x")
            yt_text._purge_old()  # must not raise
            folder = Path(d) / "yt-text" / "tasks"
            for task, kept in cases:
                self.assertEqual((folder / (task["id"] + ".json")).exists(),
                                 kept, task["id"])
            self.assertFalse(tmp_folder.exists())

    def test_position_tolerates_legacy_records(self):
        with TempDataDir() as d:
            now = time.time()
            write_task_file(d, {"id": "111111111111", "state": "queued",
                                "created_at": now - 10})
            write_task_file(d, {"id": "222222222222"})
            write_task_file(d, {"id": "333333333333", "state": "done",
                                "created_at": now - 20})
            task = {"id": "444444444444", "state": "queued",
                    "created_at": now}
            self.assertEqual(yt_text._position(task), 2)
            legacy = {"id": "555555555555"}
            self.assertEqual(yt_text._position(legacy), 1)
            done = {"id": "666666666666", "state": "done"}
            self.assertEqual(yt_text._position(done), 0)

    def test_public_and_overall_tolerate_missing_fields(self):
        with TempDataDir():
            out = yt_text._public({"id": "aabbccddeeff",
                                   "url": BILI_URL})
            self.assertEqual(out["stage_label"], "排队等待…")
            self.assertEqual(out["overall_percent"], 0)
            self.assertEqual(out["elapsed_sec"], 0)
            self.assertEqual(out["position"], 1)

    def test_create_task_ignores_legacy_pending(self):
        with TempDataDir() as d:
            write_task_file(d, {"id": "111111111111"})
            with mock.patch.object(yt_text, "_ensure_worker", lambda: None):
                task = yt_text.create_task(BILI_URL, "text")
            self.assertIsNotNone(task)
            self.assertEqual(task["platform"], "bilibili")

    def test_create_task_pending_cap(self):
        with TempDataDir() as d:
            for i in range(5):
                write_task_file(d, {"id": "%012d" % i, "state": "queued",
                                    "created_at": time.time()})
            with mock.patch.object(yt_text, "_ensure_worker", lambda: None):
                self.assertIsNone(yt_text.create_task(BILI_URL, "text"))

    def test_ensure_worker_resumes_legacy(self):
        class FakeThread:
            created = []

            def __init__(self, **kw):
                self.kw = kw
                FakeThread.created.append(self)

            def start(self):
                pass

            def is_alive(self):
                return True

        with TempDataDir() as d:
            write_task_file(d, {"id": "aabbccddeeff", "url": BILI_URL,
                                "state": "queued"})
            write_task_file(d, {"id": "bbccddeeff00", "url": BILI_URL,
                                "created_at": 1.0})
            write_task_file(d, {"id": "ccddeeff0011", "url": BILI_URL,
                                "state": "done", "created_at": 1.0})
            yt_text._worker_state.clear()
            yt_text._worker_state.update(pid=None, thread=None)
            with mock.patch.object(threading, "Thread", FakeThread):
                yt_text._ensure_worker()
            self.assertIn("aabbccddeeff", yt_text._queue)
            self.assertNotIn("bbccddeeff00", yt_text._queue)
            self.assertNotIn("ccddeeff0011", yt_text._queue)
            self.assertEqual(len(FakeThread.created), 1)
            self.assertEqual(FakeThread.created[0].kw.get("name"),
                             "yt-text-worker")

    def test_worker_survives_legacy_record(self):
        with TempDataDir() as d:
            write_task_file(d, {"id": "aabbccddeeff", "url": BILI_URL})
            yt_text._queue.append("aabbccddeeff")
            calls = []
            sleeps = {"n": 0}

            def fake_sleep(seconds):
                sleeps["n"] += 1
                if sleeps["n"] > 1:
                    raise KeyboardInterrupt

            with mock.patch.object(yt_text, "_run_task",
                                   lambda t: calls.append(t["id"])), \
                 mock.patch("time.sleep", fake_sleep):
                with self.assertRaises(KeyboardInterrupt):
                    yt_text._worker()
            self.assertEqual(calls, ["aabbccddeeff"])


if __name__ == "__main__":
    unittest.main()
