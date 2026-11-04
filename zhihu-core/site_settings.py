"""Private online settings: shortcut links and extensible website routing."""
import base64
import copy
from http.client import HTTPException
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError

if __package__:
    from .cookie_manager import SecretCookieStore, FileCookieStore
    from .extractor import ZhihuError
else:
    from cookie_manager import SecretCookieStore, FileCookieStore
    from extractor import ZhihuError

DEFAULTS = {'shortcuts': [], 'routes': []}
LAN_IPV4 = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def settings_error():
    return ZhihuError('无法读取或保存网站设置，请稍后重试。', 'settings_store_unavailable', 503)


class SettingsFileStore(FileCookieStore):
    def read(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else None
        except (OSError, ValueError):
            raise settings_error() from None

    def write(self, record):
        try:
            super().write(record)
        except ZhihuError:
            raise settings_error() from None


class SettingsSecretStore(SecretCookieStore):
    def read(self):
        try:
            result = self._request('/versions/latest:access')
            return json.loads(base64.b64decode(result['payload']['data'], validate=True))
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise settings_error() from None
        except (OSError, URLError, HTTPException, ValueError, KeyError, TypeError):
            raise settings_error() from None

    def write(self, record):
        try:
            super().write(record)
        except ZhihuError:
            raise settings_error() from None


def clean_url(value, *, endpoint=False):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError('请填写有效网址。')
    value = value.strip()
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('网址仅支持 HTTP / HTTPS，不能包含账户密码。')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('端口无效。')
    if endpoint and (parsed.hostname.lower().endswith(('.google.internal', '.localhost'))
                     or parsed.hostname.lower() in {'metadata.google.internal', 'metadata', 'localhost'}):
        raise ValueError('不能将系统元数据或回环地址设为处理服务。')
    return value


def normalize_host(value):
    if not isinstance(value, str):
        raise ValueError('网站域名无效。')
    value = value.strip().lower().rstrip('.')
    plain = value[2:] if value.startswith('*.') else value
    plain = plain.encode('idna').decode('ascii')
    if (len(plain) > 253 or '.' not in plain or not re.fullmatch(r'[a-z0-9.-]+', plain)
            or any(not label or label.startswith('-') or label.endswith('-') for label in plain.split('.'))
            or plain.endswith(('.internal', '.localhost', '.local'))):
        raise ValueError('请填写网站域名，例如 youtube.com 或 *.youtube.com。')
    try:
        ipaddress.ip_address(plain)
    except ValueError:
        return ('*.' if value.startswith('*.') else '') + plain
    raise ValueError('识别规则应填写网站域名，不能填写 IP 地址。')


def browser_endpoint(value):
    """A browser node has one fixed contract, on an explicit RFC1918 IPv4 address."""
    if not isinstance(value, str):
        raise ValueError('请填写局域网 IP:端口，例如 192.168.1.10:8765。')
    value = value.strip()
    if '://' not in value:
        value = 'http://' + value
    parsed = urlsplit(clean_url(value, endpoint=True))
    try:
        address = ipaddress.IPv4Address(parsed.hostname)
        if not any(address in network for network in LAN_IPV4) or parsed.port is None:
            raise ValueError
    except ValueError:
        raise ValueError('浏览器直连需要局域网 IPv4 地址和端口，例如 192.168.1.10:8765。') from None
    if parsed.path not in ('', '/', '/extract') or parsed.query or parsed.fragment:
        raise ValueError('局域网节点接口统一为 /extract，请只填写 IP:端口。')
    return f'{parsed.scheme}://{address}:{parsed.port}/extract'


def matches_host(host, pattern):
    return host.endswith(pattern[1:]) and host != pattern[2:] if pattern.startswith('*.') else host == pattern


class SiteSettings:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.record = None
        self.until = 0

    @classmethod
    def from_environment(cls):
        resource = os.environ.get('APP_SETTINGS_SECRET', '')
        if resource:
            return cls(SettingsSecretStore(resource))
        if os.environ.get('K_SERVICE'):
            # Existing deployments without this feature may still extract native Zhihu pages.
            return cls(None)
        return cls(SettingsFileStore(os.environ.get('APP_SETTINGS_FILE',
                   str(Path(__file__).parent / '.runtime' / 'site-settings.json'))))

    def read(self):
        with self.lock:
            if self.record is None or time.monotonic() >= self.until:
                self.record = (self.store.read() if self.store else None) or copy.deepcopy(DEFAULTS)
                if not isinstance(self.record, dict):
                    raise settings_error()
                self.until = time.monotonic() + 60
            return copy.deepcopy(self.record)

    def public(self):
        result = self.read()
        result['storage_configured'] = self.store is not None
        result['browser_origins'] = self.browser_origins()
        return result

    def browser_origins(self):
        result = set()
        for route in self.read().get('routes', []):
            if route.get('enabled') and route.get('transport', 'browser') == 'browser' and route.get('endpoint'):
                parsed = urlsplit(browser_endpoint(route['endpoint']))
                result.add(parsed.scheme + '://' + parsed.netloc)
        return sorted(result)

    def save(self, payload):
        if self.store is None:
            raise ZhihuError('在线设置尚未初始化，请先配置网站设置存储。', 'settings_not_configured', 503)
        try:
            if not isinstance(payload, dict) or set(payload) - {'shortcuts', 'routes'}:
                raise ValueError('设置字段无效。')
            shortcuts, routes = payload.get('shortcuts', []), payload.get('routes', [])
            if not isinstance(shortcuts, list) or not isinstance(routes, list) or len(shortcuts) > 30 or len(routes) > 20:
                raise ValueError('最多保存 30 个快捷链接和 20 条分流规则。')
            with self.lock:
                result = {'shortcuts': [], 'routes': []}
                ids = set()
                for link in shortcuts:
                    if not isinstance(link, dict) or set(link) - {'id', 'name', 'url'}:
                        raise ValueError('快捷链接字段无效。')
                    name = link.get('name', '').strip()
                    if not name or len(name) > 60:
                        raise ValueError('链接名称应为 1–60 个字符。')
                    result['shortcuts'].append({'id': self._id(link), 'name': name, 'url': clean_url(link.get('url'))})
                for item in routes:
                    if not isinstance(item, dict) or set(item) - {'id', 'name', 'hosts', 'endpoint', 'transport',
                            'method', 'url_parameter', 'response_field', 'timeout', 'enabled'}:
                        raise ValueError('分流规则字段无效。')
                    route_id = self._id(item)
                    if route_id in ids:
                        raise ValueError('分流规则 ID 重复。')
                    ids.add(route_id)
                    hosts = item.get('hosts', [])
                    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 20:
                        raise ValueError('每条规则需要 1–20 个网站域名。')
                    hosts = list(dict.fromkeys(normalize_host(host) for host in hosts))
                    name = item.get('name', '').strip() or hosts[0].removeprefix('*.') + ' 文本'
                    if len(name) > 60:
                        raise ValueError('规则名称应为 1–60 个字符。')
                    transport = item.get('transport', 'browser')
                    if transport not in {'browser', 'server'}:
                        raise ValueError('请求位置无效。')
                    enabled = item.get('enabled', True)
                    if not isinstance(enabled, bool):
                        raise ValueError('启用状态无效。')
                    endpoint = item.get('endpoint', '').strip()
                    if endpoint:
                        endpoint = browser_endpoint(endpoint) if transport == 'browser' else clean_url(endpoint, endpoint=True)
                    elif enabled:
                        raise ValueError('启用规则前，请填写处理服务的完整接口地址。')
                    method = 'POST' if transport == 'browser' else item.get('method', 'POST')
                    parameter = 'url' if transport == 'browser' else item.get('url_parameter', 'url')
                    field = '' if transport == 'browser' else item.get('response_field', '').strip()
                    timeout = item.get('timeout', 120 if transport == 'browser' else 40)
                    if method not in {'GET', 'POST'} or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,49}', parameter):
                        raise ValueError('请求方式或链接参数名无效。')
                    maximum = 300 if transport == 'browser' else 60
                    if (not re.fullmatch(r'[A-Za-z0-9_.-]{0,100}', field) or type(timeout) is not int
                            or not 5 <= timeout <= maximum):
                        raise ValueError('返回字段或最长等待时间无效。')
                    result['routes'].append({'id': route_id, 'name': name, 'hosts': hosts, 'endpoint': endpoint,
                        'transport': transport, 'method': method, 'url_parameter': parameter, 'response_field': field,
                        'timeout': timeout, 'enabled': enabled})
                if len(json.dumps(result, ensure_ascii=False).encode()) > 60 * 1024:
                    raise ValueError('设置内容超过 60 KiB，请减少链接或规则。')
                current = self.read()
                if any(result[key] != current.get(key, []) for key in ('routes', 'shortcuts')):
                    result['updated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
                    self.store.write(result)
                    self.record = result
                    self.until = time.monotonic() + 60
                return self.public()
        except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
            message = str(exc) if isinstance(exc, ValueError) and str(exc).endswith('。') else '网站设置格式无效。'
            raise ZhihuError(message, 'invalid_settings', 400) from None

    @staticmethod
    def _id(item):
        value = item.get('id') or secrets.token_hex(8)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
            raise ValueError('设置 ID 无效。')
        return value

