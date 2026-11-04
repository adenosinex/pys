"""Single-account authentication using only the Python standard library."""
from collections import deque
import base64
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time

SESSION_SECONDS = 12 * 60 * 60
COOKIE_NAME = 'zhihu_session'


def account_state_path():
    return Path(os.environ.get(
        'ACCOUNT_STATE_FILE',
        str(Path(__file__).parent / '.runtime' / 'account.json')))


def load_stored_account():
    """Return (username, password_hash, session_secret) saved by the
    first-run web setup, or None when no account has been stored."""
    try:
        data = json.loads(account_state_path().read_text(encoding='utf-8'))
        username, password_hash, session_secret = (
            data['username'], data['password_hash'], data['session_secret'])
        AccountAuth(username, password_hash, session_secret)
        return username, password_hash, session_secret
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store_account(username, password_hash, session_secret):
    """Persist the account created by the first-run web setup (0600)."""
    path = account_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({'username': username, 'password_hash': password_hash,
                          'session_secret': session_secret}, ensure_ascii=False)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode('utf-8'))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def password_parts(encoded):
    try:
        algorithm, rounds, salt, digest = encoded.split('$')
        rounds, salt, digest = int(rounds), bytes.fromhex(salt), bytes.fromhex(digest)
        if algorithm != 'pbkdf2_sha256' or not 100_000 <= rounds <= 2_000_000 or len(salt) != 16 or len(digest) != 32:
            raise ValueError
        return rounds, salt, digest
    except (AttributeError, TypeError, ValueError):
        raise ValueError('AUTH_PASSWORD_HASH must be a valid PBKDF2 SHA-256 password hash') from None


def hash_password(password, *, iterations=600_000):
    if not isinstance(password, str) or not 8 <= len(password) <= 1024:
        raise ValueError('Password must contain between 8 and 1024 characters')
    if not 100_000 <= iterations <= 2_000_000:
        raise ValueError('Invalid PBKDF2 iteration count')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return f'pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}'


class LoginLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class AccountAuth:
    def __init__(self, username, password_hash, session_secret, *, secure_cookie=False):
        self.username = username
        self.configured = bool(username and password_hash and session_secret)
        if (password_hash or session_secret) and not self.configured:
            raise ValueError('Configure AUTH_USERNAME, AUTH_PASSWORD_HASH and SESSION_SECRET together')
        self.parts = password_parts(password_hash) if self.configured else None
        if self.configured and (len(session_secret) < 32 or not isinstance(username, str) or len(username) > 128):
            raise ValueError('SESSION_SECRET must have at least 32 characters; username must have at most 128')
        # Changing the account or password invalidates previously signed sessions.
        self.signing_key = hmac.digest(session_secret.encode('utf-8'),
                                       (username + '\0' + password_hash).encode('utf-8'), 'sha256')
        self.secure_cookie = secure_cookie
        self.attempts = deque()
        self.lock = threading.Lock()
        self.hash_slot = threading.BoundedSemaphore(1)

    def login(self, username, password):
        now = time.monotonic()
        with self.lock:
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 10:
                raise LoginLimited(max(1, math.ceil(60 - (now - self.attempts[0]))))
            self.attempts.append(now)
        if not self.hash_slot.acquire(blocking=False):
            raise LoginLimited(1)
        try:
            rounds, salt, expected = self.parts
            actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, rounds)
            user_matches = hmac.compare_digest(username.encode('utf-8'), self.username.encode('utf-8'))
            return hmac.compare_digest(actual, expected) and user_matches
        finally:
            self.hash_slot.release()

    def issue_session(self):
        payload = json.dumps({'user': self.username, 'exp': int(time.time()) + SESSION_SECONDS,
                              'nonce': secrets.token_hex(16)}, separators=(',', ':')).encode('utf-8')
        encoded = base64.urlsafe_b64encode(payload).rstrip(b'=')
        signature = hmac.digest(self.signing_key, encoded, 'sha256').hex()
        return encoded.decode('ascii') + '.' + signature

    def session_user(self, cookie_header):
        if not self.configured or len(cookie_header) > 8192:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(cookie_header)
            token = cookies[COOKIE_NAME].value
            encoded, signature = token.rsplit('.', 1)
            expected = hmac.digest(self.signing_key, encoded.encode('ascii'), 'sha256').hex()
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            if (payload['user'] == self.username and isinstance(payload['exp'], int)
                    and time.time() < payload['exp'] <= time.time() + SESSION_SECONDS + 1):
                return self.username
        except (CookieError, KeyError, ValueError, TypeError, UnicodeError, RecursionError):
            pass
        return None

    def cookie_header(self, token='', *, clear=False):
        cookie = SimpleCookie()
        cookie[COOKIE_NAME] = token
        morsel = cookie[COOKIE_NAME]
        morsel['path'] = '/'
        morsel['httponly'] = True
        morsel['samesite'] = 'Strict'
        morsel['max-age'] = 0 if clear else SESSION_SECONDS
        if self.secure_cookie:
            morsel['secure'] = True
        return ('Set-Cookie', morsel.OutputString())
