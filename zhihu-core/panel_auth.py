"""Verify Personal App Hub panel sessions so a user who is already logged
into the panel does not have to log in again inside this project.

The panel is a Flask app using itsdangerous signed-cookie sessions
(cookie name "session", salt "cookie-session", HMAC key derivation with
SHA-1). This module re-implements just the verification half with the
standard library: it reads the panel's secret key file at runtime (never
copies it), checks the signature and the 30-day timestamp, and returns the
panel username. Any failure simply means "not a panel session" and the
caller falls back to the project's own account login.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import zlib

SECRET_FILE = os.environ.get("PANEL_SECRET_FILE") or "/srv/apphub/config/secret.key"
MAX_AGE = 30 * 24 * 3600
_SALT = b"cookie-session"

_cache = {"mtime": None, "key": None}


def _secret():
    try:
        mtime = os.path.getmtime(SECRET_FILE)
    except OSError:
        return None
    if _cache["mtime"] != mtime:
        try:
            with open(SECRET_FILE, "r", encoding="utf-8") as fh:
                _cache["key"] = fh.read().strip().encode("utf-8")
            _cache["mtime"] = mtime
        except OSError:
            return None
    return _cache["key"]


def _b64decode(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def _b64encode(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def verify_session_cookie(value):
    """Return the panel username for a valid panel session cookie, else None."""
    secret = _secret()
    if not secret or not value:
        return None
    try:
        payload_b64, ts_b64, sig_b64 = value.split(".")
        signed = (payload_b64 + "." + ts_b64).encode("ascii")
        derived = hmac.new(secret, _SALT, hashlib.sha1).digest()
        expect = _b64encode(hmac.new(derived, signed, hashlib.sha1).digest())
        if not hmac.compare_digest(expect, sig_b64):
            return None
        ts = int.from_bytes(_b64decode(ts_b64), "big")
        age = int(time.time()) - ts
        if age < -300 or age > MAX_AGE:
            return None
        raw = _b64decode(payload_b64)
        if raw.startswith(b"."):
            raw = zlib.decompress(raw[1:])
        data = json.loads(raw.decode("utf-8"))
        username = data.get("username")
        return username if isinstance(username, str) and username else None
    except Exception:
        return None


def panel_username(cookie_header):
    """Extract and verify the panel 'session' cookie from a Cookie header."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "session" and value:
            return verify_session_cookie(value)
    return None
