"""Private Cookie storage and real Zhihu-content access checks."""
import base64
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

if __package__:
    from .extractor import ZhihuError, _NoRedirect, configured_cookie, extract, target_url, validate_cookie
else:
    from extractor import ZhihuError, _NoRedirect, configured_cookie, extract, target_url, validate_cookie

DEFAULT_TEST_URL = 'https://www.zhihu.com/question/30311473/answer/2093279761095209383'
MAX_MANAGED_COOKIE_BYTES = 32 * 1024


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def normalize_cookie(value):
    value = validate_cookie(value)
    if len(value.encode('utf-8')) > MAX_MANAGED_COOKIE_BYTES:
        raise ZhihuError('Cookie 不能超过 32 KiB。', 'invalid_cookie', 400)
    pairs = {}
    for part in value.split(';'):
        if not part.strip():
            continue
        name, separator, content = part.strip().partition('=')
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ZhihuError('请粘贴完整的单行 Cookie 请求头。', 'invalid_cookie', 400)
        pairs[name] = content.strip().strip('"')
    if not pairs.get('d_c0') or not pairs.get('z_c0'):
        raise ZhihuError('Cookie 必须包含非空的 d_c0 和 z_c0 登录项。', 'invalid_cookie', 400)
    return value


def decode_record(raw):
    if not raw.strip():
        return None
    if raw.lstrip().startswith('{'):
        record = json.loads(raw)
        if not isinstance(record, dict) or not isinstance(record.get('cookie'), str):
            raise ValueError('Invalid saved Cookie record')
        return record
    # Also accept raw Cookie secrets written by the original CLI deployment.
    return {'cookie': validate_cookie(raw), 'test_url': DEFAULT_TEST_URL}


def storage_error():
    return ZhihuError('无法读取或保存知乎 Cookie，请稍后重试。', 'cookie_store_unavailable', 503)


class FileCookieStore:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            if not self.path.exists():
                return None
            return decode_record(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            raise storage_error() from None

    def write(self, record):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(self.path)
        except OSError:
            raise storage_error() from None


class SecretCookieStore:
    """Read/add versions of one pre-created secret using the Cloud Run identity."""
    def __init__(self, resource):
        if not re.fullmatch(r'projects/[a-z0-9-]+/secrets/[A-Za-z0-9_-]+', resource):
            raise ValueError('Invalid ZHIHU_COOKIE_SECRET resource name')
        self.resource = resource
        self.opener = build_opener(_NoRedirect())
        self.metadata = build_opener(ProxyHandler({}), _NoRedirect())
        self.token = ''
        self.token_until = 0

    def _json(self, request, *, metadata=False):
        opener = self.metadata if metadata else self.opener
        with opener.open(request, timeout=10) as response:
            body = response.read(192 * 1024 + 1)
            if len(body) > 192 * 1024:
                raise ValueError('Cloud response too large')
            return json.loads(body)

    def _request(self, suffix, payload=None):
        if time.monotonic() >= self.token_until:
            token = self._json(Request(
                'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
                headers={'Metadata-Flavor': 'Google'}), metadata=True)
            self.token = token['access_token']
            self.token_until = time.monotonic() + max(1, int(token['expires_in']) - 60)
        body = None if payload is None else json.dumps(payload).encode('utf-8')
        return self._json(Request('https://secretmanager.googleapis.com/v1/' + self.resource + suffix,
                                 data=body, headers={'Authorization': 'Bearer ' + self.token,
                                                     'Content-Type': 'application/json'}))

    def read(self):
        try:
            result = self._request('/versions/latest:access')
            raw = base64.b64decode(result['payload']['data'], validate=True).decode('utf-8')
            return decode_record(raw)
        except HTTPError as exc:
            if exc.code == 404:
                return None  # A newly created secret has no versions yet.
            raise storage_error() from None
        except (OSError, URLError, HTTPException, ValueError, KeyError, TypeError):
            raise storage_error() from None

    def write(self, record):
        raw = json.dumps(record, ensure_ascii=False).encode('utf-8')
        if len(raw) > 64 * 1024:
            raise ZhihuError('Cookie 超过云端存储大小上限。', 'invalid_cookie', 400)
        try:
            self._request(':addVersion', {'payload': {'data': base64.b64encode(raw).decode('ascii')}})
        except (OSError, URLError, HTTPException, ValueError, KeyError, TypeError):
            raise storage_error() from None


class CookieManager:
    def __init__(self, store, *, probe=None, min_interval=3):
        self.store = store
        self.probe = extract if probe is None else probe
        self.min_interval = min_interval
        self.lock = threading.RLock()
        self.slot = threading.BoundedSemaphore(1)
        self.record = None
        self.loaded_until = 0
        self.last_attempt = float('-inf')
        self.checked = None
        self.preview = None

    @classmethod
    def from_environment(cls):
        resource = os.environ.get('ZHIHU_COOKIE_SECRET', '')
        store = SecretCookieStore(resource) if resource else FileCookieStore(
            os.environ.get('COOKIE_STATE_FILE', str(Path(__file__).parent / '.runtime' / 'cookie-state.json')))
        return cls(store)

    def _load(self):
        if time.monotonic() >= self.loaded_until:
            self.record = self.store.read()
            self.loaded_until = time.monotonic() + 60
        if self.record is None:
            try:
                return {'cookie': configured_cookie(), 'test_url': DEFAULT_TEST_URL}
            except ZhihuError as exc:
                if exc.code != 'cookie_not_configured':
                    raise
        return self.record or {'cookie': '', 'test_url': DEFAULT_TEST_URL}

    @staticmethod
    def fingerprint(cookie, url):
        return hashlib.sha256((cookie + '\0' + url).encode('utf-8')).hexdigest()

    def status(self):
        with self.lock:
            record = self._load()
            configured = bool(record.get('cookie'))
            test_url = record.get('test_url', DEFAULT_TEST_URL)
            validation = record.get('validation') or {'valid': False, 'state': 'unchecked', 'message': '尚未测试。'}
            fingerprint = self.fingerprint(record.get('cookie', ''), test_url)
            if self.checked and self.checked[0] == fingerprint:
                validation = self.checked[1]
            return {'configured': configured, 'test_url': test_url, 'updated_at': record.get('updated_at'),
                    'validation': validation if configured else {
                        'valid': False, 'state': 'not_configured', 'message': '尚未配置知乎 Cookie。'}}

    def current_cookie(self):
        with self.lock:
            cookie = self._load().get('cookie', '')
        if not cookie:
            raise ZhihuError('请先在页面中配置知乎 Cookie。', 'cookie_not_configured', 503)
        return cookie

    def _check(self, cookie, url, *, use_cache=True):
        fingerprint = self.fingerprint(cookie, url)
        if use_cache and self.preview and self.preview[0] == fingerprint and time.monotonic() < self.preview[2]:
            return self.preview[1]
        now = time.monotonic()
        if now - self.last_attempt < self.min_interval:
            raise ZhihuError('测试过于频繁，请稍等几秒再试。', 'cookie_check_busy', 429)
        self.last_attempt = now
        try:
            doc = self.probe(url, cookie=cookie, keep_images=False)
            if not doc.text.strip():
                raise ZhihuError('没有获取到测试正文。', 'parse_failed', 422)
            validation = {'valid': True, 'state': 'valid', 'checked_at': timestamp(),
                          'message': '验证成功，已获取测试链接的正文。',
                          'title': doc.title[:160], 'content_length': len(doc.text)}
        except ZhihuError as exc:
            message = {'login_required': '知乎拒绝了访问，可能是 Cookie 过期、内容权限不足或触发访问验证。',
                       'network_error': '连接知乎失败，请稍后重试。',
                       'upstream_timeout': '知乎页面读取超时，请稍后重试。',
                       'not_found': '测试内容不存在或已删除，请更换测试链接。',
                       'rate_limited': '知乎请求过于频繁，请稍后重新测试。'}.get(
                           exc.code, '没有获取到有效的测试正文，暂时无法确认内容访问状态。')
            validation = {'valid': False, 'state': 'failed', 'checked_at': timestamp(), 'code': exc.code,
                          'message': message}
        except Exception:
            validation = {'valid': False, 'state': 'unavailable', 'checked_at': timestamp(),
                          'code': 'cookie_check_unavailable', 'message': '暂时无法完成验证，请稍后重试。'}
        validation['test_url'] = url
        self.preview = (fingerprint, validation, time.monotonic() + 30)
        return validation

    def _acquire(self):
        if not self.slot.acquire(blocking=False):
            raise ZhihuError('已有 Cookie 测试正在进行，请稍后重试。', 'cookie_check_busy', 429)

    def test(self, test_url, *, cookie=None):
        url = target_url(test_url)[0]
        candidate = normalize_cookie(cookie) if cookie is not None else self.current_cookie()
        self._acquire()
        try:
            validation = self._check(candidate, url, use_cache=cookie is not None)
            if cookie is None:
                with self.lock:
                    record = self._load()
                    if record.get('cookie') == candidate:
                        self.checked = (self.fingerprint(candidate, record.get('test_url', DEFAULT_TEST_URL)), validation)
            return validation
        finally:
            self.slot.release()

    def save(self, cookie, test_url):
        cookie, url = normalize_cookie(cookie), target_url(test_url)[0]
        self._acquire()
        try:
            validation = self._check(cookie, url)
            with self.lock:
                previous = self._load()
                record = {'cookie': cookie, 'test_url': url, 'updated_at': timestamp(), 'validation': validation}
                if previous.get('cookie') != cookie or previous.get('test_url') != url:
                    self.store.write(record)
                else:
                    record['updated_at'] = previous.get('updated_at')
                self.record = record
                self.loaded_until = time.monotonic() + 60
                self.checked = (self.fingerprint(cookie, url), validation)
            return self.status()
        finally:
            self.slot.release()

    def remove(self):
        self._acquire()
        try:
            with self.lock:
                previous = self._load()
                record = {'cookie': '', 'test_url': previous.get('test_url', DEFAULT_TEST_URL), 'updated_at': timestamp()}
                if previous.get('cookie'):
                    self.store.write(record)
                self.record = record
                self.loaded_until = time.monotonic() + 60
                self.checked = self.preview = None
            return self.status()
        finally:
            self.slot.release()
