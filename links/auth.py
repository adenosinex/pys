"""Account auth for yt-audio.

Same semantics as the zhihu-core app on this hub: credentials come from the
environment when fully configured (AUTH_USERNAME + AUTH_PASSWORD_HASH +
SESSION_SECRET); otherwise the first visitor creates the admin account in
the web UI (first-run setup) and it persists to .runtime/account.json.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

_BASE = os.path.dirname(os.path.abspath(__file__))
_RUNTIME = os.path.join(_BASE, ".runtime")
ACCOUNT_FILE = os.environ.get("ACCOUNT_STATE_FILE") or os.path.join(_RUNTIME, "account.json")
PBKDF2_ROUNDS = 200_000
SESSION_TTL = 30 * 24 * 3600


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ROUNDS, salt.hex(), dk.hex())


def verify_password(password, stored):
    try:
        _algo, rounds, salt_hex, digest_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(dk.hex(), digest_hex)
    except Exception:
        return False


class Account:
    def __init__(self, username, password_hash, session_secret):
        self.username = username
        self.password_hash = password_hash
        self.session_secret = session_secret

    def check_password(self, password):
        return verify_password(password, self.password_hash)

    def make_token(self):
        payload = {"u": self.username, "exp": int(time.time()) + SESSION_TTL}
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        sig = hmac.new(self.session_secret.encode("utf-8"), raw.encode("ascii"), hashlib.sha256).hexdigest()
        return raw + "." + sig

    def check_token(self, token):
        try:
            raw, sig = token.rsplit(".", 1)
            expect = hmac.new(
                self.session_secret.encode("utf-8"), raw.encode("ascii"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expect):
                return False
            payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
            return payload.get("u") == self.username and int(payload.get("exp", 0)) > int(time.time())
        except Exception:
            return False


def load_stored_account():
    try:
        with open(ACCOUNT_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return Account(data["username"], data["password_hash"], data["session_secret"])
    except Exception:
        return None


def store_account(username, password):
    os.makedirs(os.path.dirname(ACCOUNT_FILE), exist_ok=True)
    data = {
        "username": username,
        "password_hash": hash_password(password),
        "session_secret": secrets.token_hex(32),
    }
    tmp = ACCOUNT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, ACCOUNT_FILE)
    return Account(data["username"], data["password_hash"], data["session_secret"])


def load_account():
    """Environment configuration wins; the stored first-run account is the fallback."""
    username = os.environ.get("AUTH_USERNAME", "").strip()
    password_hash = os.environ.get("AUTH_PASSWORD_HASH", "").strip()
    session_secret = os.environ.get("SESSION_SECRET", "").strip()
    if username and password_hash and session_secret:
        return Account(username, password_hash, session_secret), True
    return load_stored_account(), False
