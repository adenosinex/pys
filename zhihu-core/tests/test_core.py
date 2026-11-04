"""Offline parser tests and real local HTTP tests; no external packages or credentials."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, build_opener
from wsgiref.util import setup_testing_defaults

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import extractor
from app import create_app

URL = 'https://zhuanlan.zhihu.com/p/123456'
COOKIE = 'd_c0=fixture-only; z_c0=fixture-session'


def article_page(content=None, **fields):
    article = {'id': '123456', 'title': '测试文章', 'author': {'name': '样本作者'},
               'content': content if content is not None else '<p>这是一篇测试文章正文。</p><pre><code>print("keep code")</code></pre>',
               'voteupCount': 3, **fields}
    return '<script id="js-initialData">' + json.dumps({'initialState': {'entities': {
        'articles': {'123456': article}}}}, ensure_ascii=False) + '</script>'


def api_call(app, data=None, *, token='test-api-key', method='POST', path='/api/extract', raw=None, **values):
    environ = {}
    setup_testing_defaults(environ)
    body = json.dumps(data if data is not None else {'url': URL}, ensure_ascii=False).encode() if raw is None else raw
    environ.update(REQUEST_METHOD=method, PATH_INFO=path, CONTENT_TYPE='application/json',
                   CONTENT_LENGTH=str(len(body)), HTTP_AUTHORIZATION='Bearer '+token,
                   **{'wsgi.input': io.BytesIO(body)})
    environ.update(values)
    captured = {}
    def start_response(status, headers):
        captured['status'] = int(status.split()[0])
        captured['headers'] = dict(headers)
    body = b''.join(app(environ, start_response))
    return captured['status'], json.loads(body), captured['headers']


class ParserTests(unittest.TestCase):
    def test_cli_help_is_utf8_without_site_packages(self):
        result = subprocess.run([sys.executable, '-S', str(Path(__file__).resolve().parent.parent/'cli.py'), '--help'],
                                capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('知乎文章', result.stdout)

    def test_urls_are_canonicalized(self):
        for url, expected_kind, expected_url in [
            ('http://zhuanlan.zhihu.com/p/123456/?utm_source=example#title', 'article', URL),
            ('https://www.zhihu.com/question/12/answer/34', 'answer', 'https://www.zhihu.com/question/12/answer/34'),
            ('https://m.zhihu.com/answer/34', 'answer', 'https://www.zhihu.com/answer/34'),
            ('https://zhihu.com/question/12', 'question', 'https://www.zhihu.com/question/12')]:
            with self.subTest(url=url):
                canonical, kind, _ = extractor.target_url(url)
                self.assertEqual((canonical, kind), (expected_url, expected_kind))

    def test_arbitrary_hosts_credentials_ports_and_paths_are_rejected(self):
        for url in ['https://evil.example/zhuanlan.zhihu.com/p/123456',
                    'https://zhuanlan.zhihu.com.evil.example/p/123456',
                    'https://127.0.0.1/p/123456', 'file:///p/123456',
                    'https://user@zhuanlan.zhihu.com/p/123456', 'https://zhuanlan.zhihu.com:8080/p/123456',
                    'https://zhuanlan.zhihu.com/p/123456/extra', 'https://www.zhihu.com/signin']:
            with self.subTest(url=url), self.assertRaises(extractor.ZhihuError):
                extractor.target_url(url)

    def test_article_markdown_metadata_and_code_in_plain_text(self):
        doc = extractor.extract_html(URL, article_page())
        self.assertEqual(doc.title, '测试文章')
        self.assertEqual(doc.author, '样本作者')
        self.assertEqual(doc.voteup, 3)
        self.assertIn('```', doc.markdown)
        self.assertIn('print("keep code")', doc.text)
        self.assertEqual(json.loads(extractor.render(doc, 'json'))['text'], doc.text)

    def test_image_links_can_be_excluded_without_fetching_images(self):
        html = article_page('<p>图片说明</p><img src="https://pic.example/image.png" alt="样本">')
        doc = extractor.extract_html(URL, html, keep_images=False)
        self.assertNotIn('![', doc.markdown)
        self.assertEqual(doc.images, ['https://pic.example/image.png'])

    def test_summary_is_not_treated_as_full_article(self):
        html = article_page('', excerpt='只有摘要') + '<div class="RichText">'+'摘要内容'*50+'</div>'
        with self.assertRaises(extractor.ZhihuError) as caught:
            extractor.extract_html(URL, html)
        self.assertEqual(caught.exception.code, 'incomplete_content')

    def test_answer_includes_question_details(self):
        data = {'initialState': {'entities': {
            'answers': {'34': {'id': '34', 'author': {'name': '回答作者'},
                              'question': {'id': '12', 'title': '问题标题'}, 'content': '<p>回答正文</p>'}},
            'questions': {'12': {'id': '12', 'title': '问题标题', 'detail': '<p>问题描述</p>'}}}}}
        doc = extractor.extract_html('https://www.zhihu.com/question/12/answer/34',
                                     '<script id="js-initialData">'+json.dumps(data)+'</script>')
        self.assertEqual(doc.question_id, '12')
        self.assertIn('问题描述', doc.markdown)
        self.assertIn('回答正文', doc.text)

    def test_question_extracts_description(self):
        data = {'initialState': {'entities': {'questions': {'12': {
            'id': '12', 'title': '问题标题', 'detail': '<p>问题描述正文</p>'}}}}}
        doc = extractor.extract_html('https://www.zhihu.com/question/12',
                                     '<script id="js-initialData">'+json.dumps(data)+'</script>')
        self.assertEqual(doc.kind, 'question')
        self.assertIn('问题描述正文', doc.text)

    def test_dom_fallback_and_no_body_error(self):
        doc = extractor.extract_html(URL, '<title>DOM 标题 - 知乎</title><div class="Post-RichText">'+
                                     '<p>正文段落。</p>'*30+'</div>')
        self.assertEqual(doc.title, 'DOM 标题')
        with self.assertRaises(extractor.ZhihuError):
            extractor.extract_html(URL, '<title>安全验证</title>')

    def test_cookie_configuration_does_not_search_parent_directory(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(extractor.ZhihuError) as caught:
            extractor.configured_cookie()
        self.assertEqual(caught.exception.code, 'cookie_not_configured')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'fixture.txt'
            path.write_text(COOKIE+'\n', encoding='utf-8')
            with patch.dict(os.environ, {'ZHIHU_COOKIE': '', 'ZHIHU_COOKIE_FILE': str(path)}):
                self.assertEqual(extractor.configured_cookie(), COOKIE)
        with self.assertRaises(extractor.ZhihuError):
            extractor.validate_cookie('d_c0=x\r\nInjected: secret')


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes, cls.seen = {}, []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.seen.append((self.path, self.headers.get('Cookie')))
                status, headers, body = cls.routes.get(self.path, (404, {}, b''))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        self.routes.clear()
        self.seen.clear()
        self.routes['/p/123456'] = (200, {'Content-Type': 'text/html; charset=utf-8'}, article_page().encode())
        # Only the test transport maps canonical requests onto a real local HTTP fixture.
        # Production URL validation remains active and never accepts local addresses.
        local = build_opener(extractor._NoRedirect())
        address = 'http://127.0.0.1:'+str(self.server.server_port)
        class Transport:
            def open(inner, request, timeout):
                path = extractor.urlsplit(request.full_url).path
                return local.open(Request(address+path, headers=dict(request.header_items())), timeout=timeout)
        self.patched = patch.object(extractor, 'build_opener', return_value=Transport())
        self.patched.start()

    def tearDown(self):
        self.patched.stop()

    def test_real_http_fetch_parse_and_cookie_header(self):
        doc = extractor.extract(URL, cookie=COOKIE)
        self.assertEqual(doc.title, '测试文章')
        self.assertEqual(self.seen, [('/p/123456', COOKIE)])

    def test_gzip_and_expansion_limit(self):
        self.routes['/p/123456'] = (200, {'Content-Type': 'text/html', 'Content-Encoding': 'gzip'},
                                    gzip.compress(article_page().encode()))
        self.assertIn('正文', extractor.extract(URL, cookie=COOKIE).text)
        self.routes['/p/123456'] = (200, {'Content-Type': 'text/html', 'Content-Encoding': 'gzip'},
                                    gzip.compress(b'a'*2000))
        with self.assertRaises(extractor.ZhihuError) as caught:
            extractor.fetch_html(URL, COOKIE, max_bytes=1000)
        self.assertEqual(caught.exception.code, 'page_too_large')

    def test_bounded_plain_response(self):
        self.routes['/p/123456'] = (200, {'Content-Type': 'text/html'}, b'a'*1001)
        with self.assertRaises(extractor.ZhihuError) as caught:
            extractor.fetch_html(URL, COOKIE, max_bytes=1000)
        self.assertEqual(caught.exception.status, 413)

    def test_known_redirect_and_external_redirect_rejection(self):
        self.routes['/p/123456'] = (302, {'Location': 'https://zhuanlan.zhihu.com/p/987'}, b'')
        self.routes['/p/987'] = (200, {'Content-Type': 'text/html'}, b'<html>fixture</html>')
        self.assertIn('fixture', extractor.fetch_html(URL, COOKIE))
        self.routes['/p/123456'] = (302, {'Location': 'http://127.0.0.1/private'}, b'')
        self.seen.clear()
        with self.assertRaises(extractor.ZhihuError) as caught:
            extractor.fetch_html(URL, COOKIE)
        self.assertEqual(caught.exception.code, 'login_required')
        self.assertEqual(len(self.seen), 1)

    def test_upstream_failure_codes(self):
        for status, expected in [(403, 'login_required'), (404, 'not_found'), (429, 'rate_limited'), (500, 'upstream_response')]:
            with self.subTest(status=status):
                self.routes['/p/123456'] = (status, {}, b'')
                with self.assertRaises(extractor.ZhihuError) as caught:
                    extractor.fetch_html(URL, COOKIE)
                self.assertEqual(caught.exception.code, expected)

    def test_complete_api_over_real_http_transport(self):
        with patch.dict(os.environ, {'ZHIHU_COOKIE': COOKIE}):
            status, data, headers = api_call(create_app(api_key='test-api-key'), {'url': URL, 'format': 'text'})
        self.assertEqual(status, 200)
        self.assertIn('正文', data['content'])
        self.assertIn('keep code', data['content'])
        self.assertNotIn(COOKIE, json.dumps(data))
        self.assertEqual(headers['Cache-Control'], 'no-store')


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.doc = extractor.extract_html(URL, article_page())
        self.app = create_app(api_key='test-api-key', extract_fn=lambda *args, **kwargs: self.doc)

    def test_authorization_health_and_no_cookie_management(self):
        self.assertEqual(api_call(self.app, token='wrong')[0], 401)
        self.assertEqual(api_call(create_app(api_key=''))[0], 503)
        status, data, _ = api_call(self.app, method='GET', path='/health', token='')
        self.assertEqual(status, 200)
        self.assertNotIn('cookie', json.dumps(data))
        self.assertEqual(api_call(self.app, path='/api/cookie')[0], 404)
        self.assertEqual(api_call(self.app, path='/api/services')[0], 404)

    def test_input_limits_and_formats(self):
        for body in [{'url': URL, 'format': 'text'}, {'url': URL, 'format': 'markdown'}, {'url': URL, 'format': 'json'}]:
            self.assertEqual(api_call(self.app, body)[0], 200)
        for body in [{'url': URL, 'cookie': 'do-not-store'}, {'url': URL, 'format': 'unknown'},
                     {'url': URL, 'keep_images': 'false'}, {'url': ['not a string']}]:
            self.assertEqual(api_call(self.app, body)[0], 400)
        self.assertEqual(api_call(self.app, raw=b'invalid json')[0], 400)
        self.assertEqual(api_call(self.app, raw=b'a'*17000)[0], 413)
        self.assertEqual(api_call(self.app, method='GET')[0], 405)
        self.assertEqual(api_call(self.app, QUERY_STRING='cookie=do-not-log')[0], 400)

    def test_concurrency_rejection_and_slot_release(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(5)
            return self.doc
        app = create_app(api_key='test-api-key', extract_fn=slow, max_concurrency=1)
        results = []
        thread = threading.Thread(target=lambda: results.append(api_call(app)), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(api_call(app)[0], 429)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(results[0][0], 200)
        self.assertEqual(api_call(app)[0], 200)


if __name__ == '__main__':
    unittest.main()
