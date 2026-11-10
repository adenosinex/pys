"""Small WSGI JSON API. Linux production: gunicorn -c gunicorn.conf.py app:app."""
from http import HTTPStatus
from html import escape
import json
import logging
import os
import re
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit

if __package__:
    from .cookie_manager import CookieManager, DEFAULT_TEST_URL, FileCookieStore, timestamp
    from .site_settings import SiteSettings, matches_host
    from .site_router import SiteRouter
    from .extractor import ZhihuError, extract, render, validate_cookie
    from .video_parse import bili_cookie_probe, open_download, parse_video, safe_filename
    from . import yt_text
else:
    from cookie_manager import CookieManager, DEFAULT_TEST_URL, FileCookieStore, timestamp
    from site_settings import SiteSettings, matches_host
    from site_router import SiteRouter
    from extractor import ZhihuError, extract, render, validate_cookie
    from video_parse import bili_cookie_probe, open_download, parse_video, safe_filename
    import yt_text

YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com',
                 'youtu.be', 'www.youtu.be', 'music.youtube.com'}

MAX_REQUEST_BYTES = 16 * 1024
DEFAULT_BILI_TEST_URL = 'https://www.bilibili.com/video/BV1VeHQ6tEaS/'


def create_app(*, extract_fn=None, max_concurrency=None,
               cookie_manager=None, site_settings=None):
    cookies = CookieManager.from_environment() if cookie_manager is None else cookie_manager
    bili_store = FileCookieStore(os.environ.get(
        'BILI_COOKIE_STATE_FILE',
        str(Path(__file__).parent / '.runtime' / 'bili-cookie-state.json')))

    def bili_cookie_value():
        try:
            record = bili_store.read()
        except ZhihuError:
            return ''
        return (record or {}).get('cookie', '') or ''

    def bili_record():
        try:
            return bili_store.read() or {}
        except ZhihuError:
            return {}

    def bili_status():
        record = bili_record()
        configured = bool(record.get('cookie'))
        validation = record.get('validation') or {
            'valid': False, 'state': 'unchecked', 'message': '尚未测试。'}
        if not configured:
            validation = {'valid': False, 'state': 'not_configured',
                          'message': '尚未配置 Bilibili Cookie。'}
        return {'bili_configured': configured,
                'test_url': record.get('test_url') or DEFAULT_BILI_TEST_URL,
                'validation': validation,
                'updated_at': record.get('updated_at')}

    def bili_test_url(value):
        m = re.search(r'BV[0-9A-Za-z]{10}', value or '')
        if not m:
            raise ZhihuError('测试链接需为含 BV 号的 Bilibili 视频链接。', 'invalid_request', 400)
        return 'https://www.bilibili.com/video/' + m.group(0) + '/'
    settings = SiteSettings.from_environment() if site_settings is None else site_settings
    router = SiteRouter(settings)
    extract_stats = threading.local()
    if extract_fn is None:
        def extract_fn(url, **options):
            stats = {}
            doc = extract(url, cookie=cookies.current_cookie(), _stats=stats, **options)
            extract_stats.data = stats
            return doc
    count = int(os.environ.get('MAX_CONCURRENCY', '2')) if max_concurrency is None else max_concurrency
    if not isinstance(count, int) or not 1 <= count <= 16:
        raise ValueError('MAX_CONCURRENCY must be between 1 and 16')
    slots = threading.BoundedSemaphore(count)
    assets = {}
    for route, name, content_type in [('/', 'index.html', 'text/html; charset=utf-8'),
                                      ('/static/app.js', 'app.js', 'text/javascript; charset=utf-8'),
                                      ('/static/settings.js', 'settings.js', 'text/javascript; charset=utf-8'),
                                      ('/static/style.css', 'style.css', 'text/css; charset=utf-8'),
                                      ('/static/logo.svg', 'logo.svg', 'image/svg+xml'),
                                      ('/favicon.ico', 'logo.svg', 'image/svg+xml')]:
        assets[route] = ((Path(__file__).parent / 'static' / name).read_bytes(), content_type)

    def raw_response(start_response, status, body, content_type, extra=(), *, connect_origins=()):
        headers = [('Content-Type', content_type), ('Content-Length', str(len(body))),
                   ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                   ('X-Frame-Options', 'DENY'), ('Referrer-Policy', 'no-referrer'),
                   ('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self' " + ' '.join(connect_origins)
                    + "; img-src 'self'; media-src 'self' https://*.douyinvod.com https://*.douyin.com" 
                    " https://*.snssdk.com https://*.bilivideo.com https://*.bilibili.com" 
                    " https://*.hdslb.com https://*.akamaized.net https://*.zjcdn.com" 
                    " https://*.douyincdn.com; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
        start_response(str(status)+' '+HTTPStatus(status).phrase, headers+list(extra))
        return [body]

    def response(start_response, status, data, extra=()):
        return raw_response(start_response, status, json.dumps(data, ensure_ascii=False).encode('utf-8'),
                            'application/json; charset=utf-8', extra)

    def error(start_response, status, message, code):
        return response(start_response, status, {'ok': False, 'error': message, 'code': code})

    def read_json(environ, *, limit=MAX_REQUEST_BYTES):
        if environ.get('CONTENT_TYPE', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise ZhihuError('请求必须是 application/json。', 'unsupported_media_type', 415)
        try:
            size = int(environ.get('CONTENT_LENGTH') or '0')
        except ValueError:
            raise ZhihuError('请求长度无效。', 'invalid_request', 400) from None
        if size > limit:
            raise ZhihuError('请求超过大小上限。', 'request_too_large', 413)
        if size <= 0:
            raise ZhihuError('JSON 请求体不能为空。', 'invalid_request', 400)
        try:
            raw = environ['wsgi.input'].read(size)
            if len(raw) != size:
                raise ValueError
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeError, RecursionError):
            raise ZhihuError('JSON 请求体无效。', 'invalid_request', 400) from None

    def same_origin(environ):
        if environ.get('HTTP_SEC_FETCH_SITE') == 'cross-site':
            return False
        origin = environ.get('HTTP_ORIGIN')
        if not origin:
            return True  # Non-browser clients do not send Origin.
        try:
            parsed = urlsplit(origin)
            return (parsed.scheme in {'https', 'http'} and not parsed.username and not parsed.password
                    and parsed.netloc.lower() == environ.get('HTTP_HOST', '').lower()
                    and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    def application(environ, start_response):
        path, method = environ.get('PATH_INFO', ''), environ.get('REQUEST_METHOD', 'GET')
        if path in {'/health', '/api/health'} and method == 'GET':
            return response(start_response, 200, {'ok': True, 'service': 'zhihu-core', 'version': '1.5.0'})
        if path in assets and method == 'GET':
            if path == '/':
                origins = []
                try:
                    origins = settings.browser_origins()
                except (ZhihuError, ValueError):
                    pass  # The settings API can explain an unavailable store.
                body, content_type = assets[path]
                body = body.replace(b'<meta name="routing-origins" content="[]">',
                    ('<meta name="routing-origins" content="' + escape(json.dumps(origins), quote=True) + '">').encode())
                return raw_response(start_response, 200, body, content_type, connect_origins=origins)
            return raw_response(start_response, 200, *assets[path])
        methods = {'/api/extract': ('POST',), '/api/video/parse': ('POST',),
                   '/api/session': ('GET',), '/api/cookie': ('GET', 'POST'),
                   '/api/cookie/test': ('POST',), '/api/cookie/remove': ('POST',),
                   '/api/settings': ('GET', 'POST'),
                   '/api/bili-cookie': ('GET', 'POST'),
                   '/api/bili-cookie/remove': ('POST',),
                   '/api/bili-cookie/test': ('POST',),
                   '/api/mac-code': ('GET', 'POST'),
                   '/api/mac-code/remove': ('POST',)}
        if path == '/api/video/stream' and method == 'GET':
            query = parse_qs(environ.get('QUERY_STRING', ''))
            try:
                status, headers, body = open_download(
                    (query.get('u') or [''])[0],
                    (query.get('ref') or [''])[0],
                    environ.get('HTTP_RANGE'))
            except ZhihuError as exc:
                return error(start_response, exc.status, str(exc), exc.code)
            if (query.get('dl') or [''])[0] == '1':
                name = safe_filename((query.get('name') or [''])[0])
                headers.append(('Content-Disposition',
                                'attachment; filename="' + name + '"'))
            start_response(str(status) + ' ' + HTTPStatus(status).phrase,
                           headers)

            def stream():
                try:
                    while True:
                        chunk = body.read(256 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    body.close()

            return stream()
        if path.startswith('/api/yt-task/'):
            if method != 'GET':
                return response(start_response, 405, {'ok': False, 'error': '请求方法无效。', 'code': 'method_not_allowed'},
                                [('Allow', 'GET')])
            task = yt_text.get_task(path.rsplit('/', 1)[-1])
            if task is None:
                return error(start_response, 404, '任务不存在或已过期。', 'not_found')
            return response(start_response, 200, {'ok': True, 'task': task})
        if path not in methods:
            return error(start_response, 404, '接口不存在。', 'not_found')
        if method not in methods[path]:
            return response(start_response, 405, {'ok': False, 'error': '请求方法无效。', 'code': 'method_not_allowed'},
                            [('Allow', ', '.join(methods[path]))])
        if method == 'POST' and not same_origin(environ):
            return error(start_response, 403, '不允许跨站请求。', 'forbidden_origin')
        if path == '/api/session':
            # Identity verification lives at the platform layer (the App
            # Hub gate in front of /web/<slug>/). This app performs no
            # verification itself: any request that reaches it has either
            # passed the platform gate or arrived on the local port,
            # which is trusted.
            return response(start_response, 200, {'ok': True, 'authenticated': True,
                            'via': 'platform', 'username': 'platform'})
        if path == '/api/settings':
            if environ.get('QUERY_STRING'):
                return error(start_response, 400, '设置参数请放在 JSON 请求体中。', 'invalid_request')
            try:
                result = settings.public() if method == 'GET' else settings.save(read_json(environ, limit=80 * 1024))
                return response(start_response, 200, {'ok': True, **result})
            except ZhihuError as exc:
                return error(start_response, exc.status, str(exc), exc.code)
        if path in {'/api/mac-code', '/api/mac-code/remove'}:
            # Write-only store for the mac-compute access code used by the
            # YouTube ASR fallback: the value is accepted and reported
            # only as configured / not configured, never read back.
            if environ.get('QUERY_STRING'):
                return error(start_response, 400, '参数请放在 JSON 请求体中。', 'invalid_request')
            try:
                if method == 'GET':
                    return response(start_response, 200,
                                    {'ok': True, 'configured': yt_text.mac_code_configured()})
                data = read_json(environ)
                if path == '/api/mac-code/remove':
                    if set(data):
                        raise ZhihuError('请求参数无效。', 'invalid_request', 400)
                    return response(start_response, 200,
                                    {'ok': True, 'configured': yt_text.remove_mac_code()})
                if set(data) - {'access_code'}:
                    raise ZhihuError('请求参数无效。', 'invalid_request', 400)
                code = data.get('access_code')
                if not isinstance(code, str):
                    raise ValueError('invalid access code')
                yt_text.save_mac_code(code)
                return response(start_response, 200, {'ok': True, 'configured': True})
            except ZhihuError as exc:
                return error(start_response, exc.status, str(exc), exc.code)
            except ValueError:
                return error(start_response, 400, '访问码无效（应为非空文本）。', 'invalid_request')
            except OSError:
                return error(start_response, 503, '无法保存访问码，请稍后重试。', 'store_unavailable')
        if path in {'/api/cookie', '/api/cookie/test', '/api/cookie/remove'}:
            if environ.get('QUERY_STRING'):
                return error(start_response, 400, 'Cookie 和测试参数请放在 JSON 请求体中。', 'invalid_request')
            if not slots.acquire(blocking=False):
                return response(start_response, 429, {'ok': False, 'error': '任务已满，请稍后重试。', 'code': 'busy'},
                                [('Retry-After', '3')])
            try:
                if method == 'GET':
                    return response(start_response, 200, {'ok': True, **cookies.status()})
                data = read_json(environ, limit=80 * 1024)
                allowed = {'cookie', 'test_url'} if path != '/api/cookie/remove' else set()
                if set(data) - allowed:
                    raise ZhihuError('Cookie 请求参数无效。', 'invalid_request', 400)
                if path == '/api/cookie/remove':
                    return response(start_response, 200, {'ok': True, **cookies.remove()})
                url = data.get('test_url', DEFAULT_TEST_URL)
                if path == '/api/cookie':
                    result = cookies.save(data.get('cookie'), url)
                else:
                    result = {'validation': cookies.test(url, cookie=data.get('cookie')),
                              'preview': 'cookie' in data}
                return response(start_response, 200, {'ok': True, **result})
            except ZhihuError as exc:
                if exc.status == 429:
                    return response(start_response, 429, {'ok': False, 'error': str(exc), 'code': exc.code},
                                    [('Retry-After', '3')])
                return error(start_response, exc.status, str(exc), exc.code)
            except (ValueError, UnicodeError, TypeError):
                return error(start_response, 400, 'Cookie 或测试链接无效。', 'invalid_request')
            finally:
                slots.release()
        if path in {'/api/bili-cookie', '/api/bili-cookie/test', '/api/bili-cookie/remove'}:
            if environ.get('QUERY_STRING'):
                return error(start_response, 400, 'Cookie 和测试参数请放在 JSON 请求体中。', 'invalid_request')
            if not slots.acquire(blocking=False):
                return response(start_response, 429, {'ok': False, 'error': '任务已满，请稍后重试。', 'code': 'busy'},
                                [('Retry-After', '3')])
            try:
                if method == 'GET':
                    return response(start_response, 200, {'ok': True, **bili_status()})
                data = read_json(environ, limit=80 * 1024)
                allowed = {'cookie', 'test_url'} if path != '/api/bili-cookie/remove' else set()
                if set(data) - allowed:
                    raise ZhihuError('Cookie 请求参数无效。', 'invalid_request', 400)
                if path == '/api/bili-cookie/remove':
                    record = bili_record()
                    bili_store.write({'cookie': '',
                                      'test_url': record.get('test_url') or DEFAULT_BILI_TEST_URL,
                                      'updated_at': timestamp()})
                    return response(start_response, 200, {'ok': True, **bili_status()})
                test_url = bili_test_url(data.get('test_url') or bili_status()['test_url'])
                if path == '/api/bili-cookie/test':
                    candidate = data.get('cookie')
                    preview = candidate is not None
                    if preview:
                        if not isinstance(candidate, str):
                            raise ValueError('invalid cookie')
                        candidate = validate_cookie(candidate)
                        if 'SESSDATA=' not in candidate:
                            raise ZhihuError('Cookie 必须包含 SESSDATA 登录项。', 'invalid_cookie', 400)
                    else:
                        candidate = bili_cookie_value()
                        if not candidate:
                            raise ZhihuError('请先在页面中配置 Bilibili Cookie。', 'cookie_not_configured', 503)
                    validation = bili_cookie_probe(candidate, test_url)
                    validation['checked_at'] = timestamp()
                    validation['test_url'] = test_url
                    if not preview:
                        record = bili_record()
                        if record.get('cookie') == candidate:
                            record['validation'] = validation
                            record['test_url'] = test_url
                            bili_store.write(record)
                    return response(start_response, 200, {'ok': True, 'validation': validation,
                                                          'preview': preview})
                value = data.get('cookie')
                if not isinstance(value, str):
                    raise ValueError('invalid cookie')
                value = validate_cookie(value)
                if len(value.encode('utf-8')) > 32 * 1024 or 'SESSDATA=' not in value:
                    raise ZhihuError('Cookie 必须包含 SESSDATA 登录项，且不超过 32 KiB。', 'invalid_cookie', 400)
                validation = bili_cookie_probe(value, test_url)
                validation['checked_at'] = timestamp()
                validation['test_url'] = test_url
                bili_store.write({'cookie': value, 'test_url': test_url,
                                  'validation': validation,
                                  'updated_at': timestamp()})
                return response(start_response, 200, {'ok': True, **bili_status()})
            except ZhihuError as exc:
                if exc.status == 429:
                    return response(start_response, 429, {'ok': False, 'error': str(exc), 'code': exc.code},
                                    [('Retry-After', '3')])
                return error(start_response, exc.status, str(exc), exc.code)
            except (ValueError, UnicodeError, TypeError):
                return error(start_response, 400, 'Cookie 或测试链接无效。', 'invalid_request')
            finally:
                slots.release()
        if path == '/api/video/parse':
            if environ.get('QUERY_STRING'):
                return error(start_response, 400, '参数请放在 JSON 请求体中。', 'invalid_request')
            try:
                data = read_json(environ)
                if set(data) - {'text'}:
                    raise ValueError('unknown fields')
                text = data.get('text')
                if not isinstance(text, str) or not text.strip() \
                        or len(text) > 8000:
                    raise ValueError('invalid text')
            except ZhihuError as exc:
                return error(start_response, exc.status, str(exc), exc.code)
            except (ValueError, UnicodeError, RecursionError):
                return error(start_response, 400, '请求参数无效，请粘贴分享文本或链接。', 'invalid_request')
            if not slots.acquire(blocking=False):
                return response(start_response, 429, {'ok': False, 'error': '任务已满，请稍后重试。', 'code': 'busy'},
                                [('Retry-After', '3')])
            try:
                result = parse_video(text, bili_cookie=bili_cookie_value())
                return response(start_response, 200, {'ok': True, **result})
            except ZhihuError as exc:
                return error(start_response, exc.status, str(exc), exc.code)
            except Exception as exc:
                logging.getLogger(__name__).error('Video parse failed (%s)', type(exc).__name__)
                return error(start_response, 500, '内部处理失败，请检查服务端配置。', 'internal_error')
            finally:
                slots.release()
        if environ.get('QUERY_STRING'):
            return error(start_response, 400, '参数请放在 JSON 请求体中。', 'invalid_request')
        try:
            data = read_json(environ)
            if set(data) - {'url', 'format', 'keep_images'}:
                raise ValueError('unknown fields')
            url, fmt, images = data.get('url'), data.get('format', 'markdown'), data.get('keep_images', True)
            if not isinstance(url, str) or not url.strip() or len(url) > 8000:
                raise ValueError('invalid URL')
            if fmt not in ('markdown', 'text', 'json') or not isinstance(images, bool):
                raise ValueError('invalid options')
        except ZhihuError as exc:
            return error(start_response, exc.status, str(exc), exc.code)
        except (ValueError, UnicodeError, RecursionError):
            return error(start_response, 400, '请求参数无效，请检查 url、format 和 keep_images。', 'invalid_request')
        # YouTube links with no configured routing rule take the built-in
        # subtitle-first / ASR-fallback pipeline (yt_text). It runs
        # asynchronously: this request only creates the task, and the
        # page polls /api/yt-task/<id> for progress and the result.
        # Configured rules (browser or server transport) keep their
        # existing behavior via the router below.
        try:
            _host = (urlsplit(url.strip()).hostname or '').lower().rstrip('.')
        except ValueError:
            _host = ''
        if _host in YOUTUBE_HOSTS:
            try:
                _routes = settings.read().get('routes', [])
            except Exception:
                _routes = []
            _ruled = any(isinstance(rule, dict) and rule.get('enabled')
                         and any(matches_host(_host, pattern)
                                 for pattern in rule.get('hosts', []))
                         for rule in _routes)
            if not _ruled:
                try:
                    task = yt_text.create_task(url.strip(), fmt)
                except OSError:
                    task = None
                    _ruled = True  # fall back to the router's explanation
                if not _ruled:
                    if task is None:
                        return response(start_response, 429,
                                        {'ok': False, 'error': '转写任务已满，请稍后重试。', 'code': 'busy'},
                                        [('Retry-After', '3')])
                    return response(start_response, 200,
                                    {'ok': True, 'async': True,
                                     'task_id': task['id'], 'task': task})
        if not slots.acquire(blocking=False):
            return response(start_response, 429, {'ok': False, 'error': '提取任务已满，请稍后重试。', 'code': 'busy'},
                            [('Retry-After', '3')])
        try:
            extract_stats.data = {}
            started = time.perf_counter()
            doc = router.extract(url, extract_fn, keep_images=images)
            content = render(doc, fmt)
            stats = {'elapsed_ms': int(round((time.perf_counter() - started) * 1000)),
                     'fetched_bytes': extract_stats.data.get('fetched_bytes'),
                     'output_bytes': len(content.encode('utf-8')),
                     'output_chars': len(content)}
            metadata = doc.to_dict()
            metadata.pop('markdown', None)
            metadata.pop('text', None)
            return response(start_response, 200, {'ok': True, **metadata, 'format': fmt,
                            'content': content, 'stats': stats})
        except ZhihuError as exc:
            return error(start_response, exc.status, str(exc), exc.code)
        except Exception as exc:
            logging.getLogger(__name__).error('Extraction failed (%s)', type(exc).__name__)
            return error(start_response, 500, '内部处理失败，请检查服务端配置。', 'internal_error')
        finally:
            slots.release()

    return application


app = create_app()
