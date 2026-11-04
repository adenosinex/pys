"""Public extraction API. No third-party packages, browser, or parent-project imports."""
from __future__ import annotations

from contextlib import closing
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zlib

if __package__:
    from .parser import Doc, IncompleteContent, ZhihuExtractor
else:
    from parser import Doc, IncompleteContent, ZhihuExtractor

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_COOKIE_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 20
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


class ZhihuError(RuntimeError):
    def __init__(self, message, code='extract_failed', status=422):
        super().__init__(message)
        self.code, self.status = code, status


def target_url(value: str) -> tuple[str, str, tuple[str, ...]]:
    """Accept known Zhihu paths and always fetch the canonical HTTPS URL."""
    error = ZhihuError('请提供有效的知乎专栏文章、回答或问题链接。', 'invalid_url', 400)
    if not isinstance(value, str) or len(value) > 8000:
        raise error
    value = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise error
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
                or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)):
            raise error
    except ValueError:
        raise error from None
    path, host = parsed.path.rstrip('/'), parsed.hostname
    if host == 'zhuanlan.zhihu.com':
        match = re.fullmatch(r'/p/([0-9]{1,30})', path)
        if match:
            return 'https://zhuanlan.zhihu.com' + path, 'article', match.groups()
    if host in {'zhihu.com', 'www.zhihu.com', 'm.zhihu.com'}:
        for kind, pattern in [('answer', r'/question/([0-9]{1,30})/answer/([0-9]{1,30})'),
                              ('answer', r'/answer/([0-9]{1,30})'),
                              ('question', r'/question/([0-9]{1,30})')]:
            match = re.fullmatch(pattern, path)
            if match:
                return 'https://www.zhihu.com' + path, kind, match.groups()
    raise error


def validate_cookie(value: str) -> str:
    if not isinstance(value, str):
        raise ZhihuError('Cookie 必须是文本。', 'invalid_cookie', 400)
    value = value.strip()
    if value.lower().startswith('cookie:'):
        value = value[7:].strip()
    if (not value or len(value.encode('utf-8')) > MAX_COOKIE_BYTES
            or any(ord(character) < 32 or ord(character) > 126 for character in value)):
        raise ZhihuError('请配置有效的单行 Cookie 请求头。', 'invalid_cookie', 400)
    return value


def configured_cookie() -> str:
    """Read explicit configuration only; never search the parent project's files."""
    value = os.environ.get('ZHIHU_COOKIE', '').strip()
    if value:
        return validate_cookie(value)
    filename = os.environ.get('ZHIHU_COOKIE_FILE', '').strip()
    if not filename:
        raise ZhihuError('请设置 ZHIHU_COOKIE 或 ZHIHU_COOKIE_FILE。', 'cookie_not_configured', 503)
    return load_cookie(filename)


def load_cookie(filename: str) -> str:
    """Load a raw Cookie header; file location is chosen by the operator."""
    try:
        with Path(filename).open('rb') as file:
            raw = file.read(MAX_COOKIE_BYTES + 1)
        if len(raw) > MAX_COOKIE_BYTES:
            raise ZhihuError('Cookie 文件超过 64 KiB。', 'invalid_cookie', 503)
        return validate_cookie(raw.decode('utf-8-sig'))
    except (OSError, UnicodeError):
        raise ZhihuError('无法读取 Cookie 文件，请检查路径、编码和权限。', 'cookie_not_configured', 503) from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_page(response, limit: int, deadline: float) -> str:
    encoding = (response.headers.get('Content-Encoding') or 'identity').lower().strip()
    if encoding not in {'identity', 'gzip'}:
        raise ZhihuError('知乎返回了不支持的压缩格式。', 'upstream_response', 502)
    content_type = (response.headers.get('Content-Type') or '').lower()
    if content_type and not any(value in content_type for value in ('text/html', 'application/xhtml+xml')):
        raise ZhihuError('知乎未返回 HTML 页面。', 'upstream_response', 502)
    length = response.headers.get('Content-Length')
    if length and length.isdigit() and int(length) > limit:
        raise ZhihuError('页面超过大小上限。', 'page_too_large', 413)
    read = getattr(response, 'read1', response.read)
    chunks, size = [], 0
    while True:
        if time.monotonic() >= deadline:
            raise ZhihuError('知乎页面读取超时。', 'upstream_timeout', 504)
        chunk = read(min(65536, limit + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ZhihuError('页面超过大小上限。', 'page_too_large', 413)
        chunks.append(chunk)
    body = b''.join(chunks)
    if encoding == 'gzip':
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        body = decoder.decompress(body, limit + 1)
        if len(body) > limit or decoder.unconsumed_tail:
            raise ZhihuError('解压后的页面超过大小上限。', 'page_too_large', 413)
        if not decoder.eof or decoder.unused_data:
            raise ZhihuError('页面压缩数据不完整或格式不支持。', 'upstream_response', 502)
    return body.decode('utf-8-sig', errors='replace'), size


def fetch_html(url: str, cookie: str, *, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_HTML_BYTES,
               stats=None) -> str:
    url, _, _ = target_url(url)
    cookie = validate_cookie(cookie)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise ValueError('timeout must be between 0 and 60 seconds')
    if not isinstance(max_bytes, int) or not 0 < max_bytes <= 8 * 1024 * 1024:
        raise ValueError('max_bytes must be between 1 and 8 MiB')
    opener = build_opener(_NoRedirect())
    deadline = time.monotonic() + timeout
    for redirects in range(4):
        request = Request(url, headers={'User-Agent': UA, 'Cookie': cookie,
                          'Accept': 'text/html,application/xhtml+xml', 'Accept-Encoding': 'identity',
                          'Accept-Language': 'zh-CN,zh;q=0.9', 'Referer': 'https://www.zhihu.com/'})
        try:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ZhihuError('知乎页面读取超时。', 'upstream_timeout', 504)
                response = opener.open(request, timeout=remaining)
            except HTTPError as exc:
                response = exc
            with closing(response):
                status = response.code
                if status in {301, 302, 303, 307, 308}:
                    if redirects == 3 or not response.headers.get('Location'):
                        raise ZhihuError('页面重定向过多或缺少目标。', 'upstream_redirect', 502)
                    try:
                        url, _, _ = target_url(urljoin(url, response.headers['Location']))
                    except ZhihuError:
                        raise ZhihuError('知乎跳转到了登录、验证或不支持的页面。', 'login_required', 422) from None
                    continue
                if status in {401, 403}:
                    raise ZhihuError('知乎拒绝访问，请检查 Cookie、账号权限及节点网络。', 'login_required', 422)
                if status == 404:
                    raise ZhihuError('内容不存在或已删除。', 'not_found', 404)
                if status == 429:
                    raise ZhihuError('知乎请求过于频繁，请稍后重试。', 'rate_limited', 429)
                if status != 200:
                    raise ZhihuError('知乎返回 HTTP ' + str(status) + '。', 'upstream_response', 502)
                html, fetched_bytes = _read_page(response, max_bytes, deadline)
                if stats is not None:
                    stats['fetched_bytes'] = fetched_bytes
                return html
        except (URLError, OSError, EOFError, HTTPException, zlib.error):
            raise ZhihuError('连接知乎失败、超时或响应不完整，请检查节点网络。', 'network_error', 502) from None
    raise ZhihuError('无法完成页面请求。', 'upstream_response', 502)


def extract_html(url: str, html: str, *, keep_images=True) -> Doc:
    """Parse already-fetched HTML. This entry never performs network or file I/O."""
    canonical, kind, ids = target_url(url)
    if not isinstance(html, str) or len(html.encode('utf-8')) > 8 * 1024 * 1024:
        raise ZhihuError('HTML 为空、类型不正确或超过 8 MiB。', 'invalid_html', 400)
    parser = ZhihuExtractor(keep_images=keep_images)
    try:
        doc = parser.from_initial_data(html, kind, ids)
        if doc is None:
            doc = parser.from_dom(html, kind, canonical)
    except IncompleteContent as exc:
        raise ZhihuError(str(exc), 'incomplete_content', 422) from None
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise ZhihuError('页面数据结构无法解析，可能已变化。', 'parse_failed', 422) from None
    if doc is None or not doc.text.strip():
        raise ZhihuError('页面没有可提取的正文，可能需要登录、验证或没有访问权限。', 'parse_failed', 422)
    doc.url = canonical
    return doc


def extract(url: str, *, cookie='', cookie_file='', timeout=DEFAULT_TIMEOUT,
            max_bytes=MAX_HTML_BYTES, keep_images=True, _stats=None) -> Doc:
    canonical, _, _ = target_url(url)
    if not cookie:
        cookie = load_cookie(cookie_file) if cookie_file else configured_cookie()
    return extract_html(canonical, fetch_html(canonical, cookie, timeout=timeout, max_bytes=max_bytes,
                                              stats=_stats),
                        keep_images=keep_images)


def render(doc: Doc, format='markdown') -> str:
    if format == 'markdown':
        return doc.markdown
    if format == 'text':
        return doc.text
    if format == 'json':
        return json.dumps(doc.to_dict(), ensure_ascii=False, indent=2)
    raise ValueError('format must be markdown, text, or json')
