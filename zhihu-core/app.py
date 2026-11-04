"""Small WSGI JSON API. Linux production: gunicorn -c gunicorn.conf.py app:app."""
from http import HTTPStatus
import hmac
from html import escape
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

if __package__:
    from .auth import AccountAuth, LoginLimited, hash_password, load_stored_account, store_account
    from .cookie_manager import CookieManager, DEFAULT_TEST_URL
    from .site_settings import SiteSettings
    from .site_router import SiteRouter
    from .extractor import ZhihuError, extract, render
else:
    from auth import AccountAuth, LoginLimited, hash_password, load_stored_account, store_account
    from cookie_manager import CookieManager, DEFAULT_TEST_URL
    from site_settings import SiteSettings
    from site_router import SiteRouter
    from extractor import ZhihuError, extract, render

MAX_REQUEST_BYTES = 16 * 1024


def create_app(*, api_key=None, extract_fn=None, max_concurrency=None,
               username=None, password_hash=None, session_secret=None, secure_cookie=None, cookie_manager=None,
               site_settings=None):
    key = os.environ.get('EXTRACTOR_API_KEY', '') if api_key is None else api_key
    secure_flag = (os.environ.get('SESSION_COOKIE_SECURE', '1' if os.environ.get('K_SERVICE') else '0') == '1'
                   if secure_cookie is None else secure_cookie)
    account = AccountAuth(
        os.environ.get('AUTH_USERNAME', 'xin') if username is None else username,
        os.environ.get('AUTH_PASSWORD_HASH', '') if password_hash is None else password_hash,
        os.environ.get('SESSION_SECRET', '') if session_secret is None else session_secret,
        secure_cookie=secure_flag)
    if not account.configured:
        # First-run web setup may have stored an account locally; explicit
        # environment configuration always takes precedence over the file.
        stored = load_stored_account()
        if stored is not None:
            account = AccountAuth(*stored, secure_cookie=secure_flag)
    setup_lock = threading.Lock()
    cookies = CookieManager.from_environment() if cookie_manager is None else cookie_manager
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
                    + "; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
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
                    and not parsed.path and not parsed.query and not parsed.fragment
                    and (not account.secure_cookie or parsed.scheme == 'https'))
        except ValueError:
            return False

    def application(environ, start_response):
        nonlocal account
        path, method = environ.get('PATH_INFO', ''), environ.get('REQUEST_METHOD', 'GET')
        if path in {'/health', '/api/health'} and method == 'GET':
            return response(start_response, 200, {'ok': True, 'service': 'zhihu-core', 'version': '1.4.0',
                            'authentication_configured': bool(key) or account.configured})
        if path in assets and method == 'GET':
            if path == '/':
                origins = []
                if account.session_user(environ.get('HTTP_COOKIE', '')):
                    try:
                        origins = settings.browser_origins()
                    except (ZhihuError, ValueError):
                        pass  # The settings API can explain an unavailable store after login.
                body, content_type = assets[path]
                body = body.replace(b'<meta name="routing-origins" content="[]">',
                    ('<meta name="routing-origins" content="' + escape(json.dumps(origins), quote=True) + '">').encode())
                return raw_response(start_response, 200, body, content_type, connect_origins=origins)
            return raw_response(start_response, 200, *assets[path])
        methods = {'/api/extract': ('POST',), '/api/login': ('POST',), '/api/logout': ('POST',),
                   '/api/setup': ('POST',),
                   '/api/session': ('GET',), '/api/cookie': ('GET', 'POST'),
                   '/api/cookie/test': ('POST',), '/api/cookie/remove': ('POST',),
                   '/api/settings': ('GET', 'POST')}
        if path not in methods:
            return error(start_response, 404, '接口不存在。', 'not_found')
        if method not in methods[path]:
            return response(start_response, 405, {'ok': False, 'error': '请求方法无效。', 'code': 'method_not_allowed'},
                            [('Allow', ', '.join(methods[path]))])
        if method == 'POST' and not same_origin(environ):
            return error(start_response, 403, '不允许跨站请求。', 'forbidden_origin')
        user = account.session_user(environ.get('HTTP_COOKIE', ''))
        if path == '/api/session':
            return response(start_response, 200, {'ok': True, 'authenticated': bool(user),
                            'username': user, 'account_login_enabled': account.configured,
                            'setup_required': not account.configured})
        if path == '/api/logout':
            return response(start_response, 200, {'ok': True}, [account.cookie_header(clear=True)])
        if path == '/api/login':
            if not account.configured:
                return error(start_response, 503, '账户登录尚未配置，请联系管理员。', 'account_not_configured')
            if environ.get('QUERY_STRING'):
                return error(start_response, 400, '登录信息请放在 JSON 请求体中。', 'invalid_request')
            try:
                data = read_json(environ)
                if (set(data) != {'username', 'password'} or not isinstance(data['username'], str)
                        or not isinstance(data['password'], str) or not 1 <= len(data['username']) <= 128
                        or not 1 <= len(data['password']) <= 1024):
                    raise ZhihuError('请填写账户名和密码。', 'invalid_request', 400)
                if not account.login(data['username'], data['password']):
                    return error(start_response, 401, '账户名或密码错误。', 'invalid_credentials')
                return response(start_response, 200, {'ok': True, 'username': account.username},
                                [account.cookie_header(account.issue_session())])
            except LoginLimited as exc:
                return response(start_response, 429, {'ok': False, 'error': '登录尝试过于频繁，请稍后重试。',
                                'code': 'login_rate_limited'}, [('Retry-After', str(exc.retry_after))])
            except ZhihuError as exc:
                return error(start_response, exc.status, str(exc), exc.code)
            except UnicodeError:
                return error(start_response, 400, '账户名或密码的字符编码无效。', 'invalid_request')
        if path == '/api/setup':
            if account.configured:
                return error(start_response, 409, '管理员账户已设置，请直接登录。', 'setup_unavailable')
            if environ.get('QUERY_STRING'):
                return error(start_response, 400, '设置信息请放在 JSON 请求体中。', 'invalid_request')
            try:
                data = read_json(environ)
                if (set(data) != {'username', 'password'} or not isinstance(data['username'], str)
                        or not isinstance(data['password'], str)):
                    raise ZhihuError('请填写账户名和密码。', 'invalid_request', 400)
                new_username = data['username'].strip()
                new_password = data['password']
                if not 1 <= len(new_username) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in new_username):
                    raise ZhihuError('账户名无效。', 'invalid_request', 400)
                if not 10 <= len(new_password) <= 1024:
                    raise ZhihuError('密码至少需要 10 位。', 'invalid_request', 400)
                with setup_lock:
                    if account.configured:
                        return error(start_response, 409, '管理员账户已设置，请直接登录。', 'setup_unavailable')
                    new_hash = hash_password(new_password)
                    new_secret = secrets.token_hex(32)
                    try:
                        store_account(new_username, new_hash, new_secret)
                    except OSError:
                        return error(start_response, 500, '账户保存失败，请检查服务目录权限。', 'setup_store_failed')
                    account = AccountAuth(new_username, new_hash, new_secret, secure_cookie=secure_flag)
                return response(start_response, 200, {'ok': True, 'username': new_username},
                                [account.cookie_header(account.issue_session())])
            except ZhihuError as exc:
                return error(start_response, exc.status, str(exc), exc.code)
            except (ValueError, UnicodeError):
                return error(start_response, 400, '账户名或密码的字符编码无效。', 'invalid_request')
        if path == '/api/settings':
            if not user:
                return error(start_response, 401, '请先登录账户后管理网站设置。', 'unauthorized')
            if environ.get('QUERY_STRING'):
                return error(start_response, 400, '设置参数请放在 JSON 请求体中。', 'invalid_request')
            try:
                result = settings.public() if method == 'GET' else settings.save(read_json(environ, limit=80 * 1024))
                return response(start_response, 200, {'ok': True, **result})
            except ZhihuError as exc:
                return error(start_response, exc.status, str(exc), exc.code)
        if path in {'/api/cookie', '/api/cookie/test', '/api/cookie/remove'}:
            if not user:
                return error(start_response, 401, '请先登录账户后管理知乎 Cookie。', 'unauthorized')
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
        if not key and not account.configured:
            return error(start_response, 503, '请先设置 EXTRACTOR_API_KEY。', 'api_key_not_configured')
        auth = environ.get('HTTP_AUTHORIZATION', '')
        if auth:
            authorized = bool(key) and hmac.compare_digest(auth.encode('utf-8'), ('Bearer '+key).encode('utf-8'))
        else:
            authorized = bool(user)
        if not authorized:
            return error(start_response, 401, '请先登录，或提供有效的 API 访问密钥。', 'unauthorized')
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
