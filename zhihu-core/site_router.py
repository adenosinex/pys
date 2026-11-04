"""Route recognized website URLs to fixed, administrator-configured processors."""
from contextlib import closing
from http.client import HTTPConnection, HTTPException
import ipaddress
import json
import os
import socket
import ssl
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

if __package__:
    from .extractor import ZhihuError, target_url
    from .parser import Doc
    from .site_settings import matches_host, clean_url
else:
    from extractor import ZhihuError, target_url
    from parser import Doc
    from site_settings import matches_host, clean_url

MAX_PROXY_BYTES = 2 * 1024 * 1024
LAN_NETWORKS = tuple(ipaddress.ip_network(item) for item in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))


class PinnedConnection(HTTPConnection):
    def __init__(self, host, port, address, *, tls, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address, self.tls = address, tls

    def connect(self):
        stream = socket.create_connection((self.address, self.port), self.timeout)
        if self.tls:
            try:
                stream = ssl.create_default_context().wrap_socket(stream, server_hostname=self.host)
            except Exception:
                stream.close()
                raise
        self.sock = stream


def source_url(value):
    if not isinstance(value, str) or len(value) > 8000 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ZhihuError('链接无效。', 'invalid_url', 400)
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443):
            raise ValueError
        host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
    except (ValueError, UnicodeError):
        raise ZhihuError('链接无效。', 'invalid_url', 400) from None
    return value.strip(), host


def endpoint_address(endpoint):
    parsed = urlsplit(clean_url(endpoint, endpoint=True))
    host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
    allow_lan = os.environ.get('PROXY_ALLOW_PRIVATE_NETWORK', '0' if os.environ.get('K_SERVICE') else '1') == '1'
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses:
        raise ZhihuError('无法解析处理服务地址。', 'proxy_network_error', 502)
    for address in addresses:
        ip = ipaddress.ip_address(address)
        lan = any(ip in network for network in LAN_NETWORKS if network.version == ip.version)
        if not ip.is_global and not (allow_lan and lan):
            raise ZhihuError('该处理服务位于不可访问的内网，请配置公网 HTTPS 入口或已接通的私有网络。', 'proxy_private_network', 503)
        if parsed.scheme != 'https' and not (allow_lan and lan):
            raise ZhihuError('公网处理服务必须使用 HTTPS。', 'proxy_https_required', 400)
    # Connect to the checked IP while retaining the original TLS name and Host header.
    return parsed, host, port, addresses[0]


def response_text(raw, content_type, field):
    decoded = raw.decode('utf-8-sig', errors='replace')
    if 'json' in content_type or (not content_type and decoded.lstrip().startswith(('{', '['))):
        try:
            data = json.loads(decoded)
        except (ValueError, RecursionError):
            raise ZhihuError('处理服务返回了无效 JSON。', 'proxy_invalid_response', 502) from None
        if isinstance(data, dict) and (data.get('ok') is False or data.get('success') is False):
            raise ZhihuError('处理服务返回失败，请检查节点配置或日志。', 'proxy_upstream_error', 502)
        paths = [field] if field else ['content', 'text', 'transcript', 'data.content', 'data.text', 'data.transcript']
        if isinstance(data, str):
            return data, {}
        for path in paths:
            current = data
            for key in path.split('.'):
                current = current.get(key) if isinstance(current, dict) else None
            if isinstance(current, str) and current.strip():
                return current, data if isinstance(data, dict) else {}
        raise ZhihuError('处理服务未返回文本，请检查“返回文本字段”。', 'proxy_text_missing', 502)
    if content_type and not any(kind in content_type for kind in ('text/plain', 'text/markdown')):
        raise ZhihuError('处理服务未返回纯文本或 JSON。', 'proxy_invalid_response', 502)
    return decoded, {}


def proxy_extract(rule, url):
    connection = None
    try:
        parsed, host, port, address = endpoint_address(rule['endpoint'])
        timeout = rule.get('timeout', 40)
        deadline = time.monotonic() + timeout
        connection = PinnedConnection(host, port, address, tls=parsed.scheme == 'https', timeout=min(10, timeout))
        path = quote(parsed.path or '/', safe='/%:@')
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != rule['url_parameter']]
        body = None
        headers = {'Accept': 'application/json, text/plain', 'Accept-Encoding': 'identity', 'User-Agent': 'zhihu-core/1.4'}
        if rule['method'] == 'GET':
            query.append((rule['url_parameter'], url))
        else:
            body = json.dumps({rule['url_parameter']: url}).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if query:
            path += '?' + urlencode(query)
        connection.request(rule['method'], path, body=body, headers=headers)
        with closing(connection.getresponse()) as response:
            if response.status in (401, 403):
                raise ZhihuError('处理服务鉴权失败，请检查已保存的密钥或请求头。', 'proxy_auth_failed', 502)
            if response.status != 200:
                raise ZhihuError('处理服务返回 HTTP ' + str(response.status) + '，请检查节点状态。', 'proxy_upstream_error', 502)
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise ZhihuError('处理服务响应超时。', 'proxy_timeout', 504)
                chunk = response.read1(min(65536, MAX_PROXY_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_PROXY_BYTES:
                    raise ZhihuError('处理服务返回内容超过 2 MiB。', 'proxy_response_too_large', 413)
            text, metadata = response_text(b''.join(chunks), response.headers.get('Content-Type', '').lower(), rule.get('response_field', ''))
        if not text.strip():
            raise ZhihuError('处理服务返回了空文本。', 'proxy_text_missing', 502)
        title = metadata.get('title') or rule['name'] + ' · ' + urlsplit(url).hostname
        author = metadata.get('author', '')
        return Doc(url=url, kind='proxy', title=str(title)[:200], author=str(author)[:160],
                   text=text, markdown=text, source=rule['name'], extra={'routing_rule': rule['id']})
    except ZhihuError:
        raise
    except (TimeoutError, socket.timeout):
        raise ZhihuError('连接处理服务超时，请稍后重试。', 'proxy_timeout', 504) from None
    except (OSError, HTTPException, ValueError, UnicodeError):
        raise ZhihuError('无法连接处理服务，请检查入口地址、证书和网络。', 'proxy_network_error', 502) from None
    finally:
        if connection is not None:
            connection.close()


class SiteRouter:
    def __init__(self, settings):
        self.settings = settings

    def extract(self, url, native, *, keep_images=True):
        url, host = source_url(url)
        for rule in self.settings.read().get('routes', []):
            if rule.get('enabled') and any(matches_host(host, pattern) for pattern in rule['hosts']):
                if rule.get('transport', 'browser') == 'browser':
                    raise ZhihuError('该网站配置为浏览器局域网直连，请在网页中提取；如刚更新设置，请刷新页面。',
                                     'browser_route_required', 409)
                return proxy_extract(rule, url)
        if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be', 'music.youtube.com'}:
            raise ZhihuError('请在“链接与分流设置”中添加 YouTube 对应的局域网 IP 和端口，并启用规则。', 'route_not_configured', 503)
        target_url(url)
        return native(url, keep_images=keep_images)
