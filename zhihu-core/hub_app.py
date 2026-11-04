"""App Hub entrypoint: mount the zhihu app under the panel's unified
/web/<slug>/ route (the hub's database-driven "virtual blueprint").

The panel strips the /web/<slug> prefix before forwarding and passes it
back in the X-Forwarded-Prefix header. This adapter honors that contract:
it rewrites the app's absolute asset/API references in HTML/JS/CSS
responses so they resolve under the prefix. The original app modules are
untouched; without the header (direct port access, Cloud Run) the prefix
rewriting is a transparent pass-through and the plain `app:app` behavior
is unchanged.

It also bridges panel single sign-on: when a request carries a valid
App Hub panel session but no zhihu session, the adapter mints a zhihu
session with the app's own auth module (same account resolution as
app.py: environment first, then the first-run account file) and injects
it into the request, returning it as a Set-Cookie so the browser keeps
it. Visitors without a panel session use the app's normal login.
"""
import os
from urllib.parse import urlsplit

from app import app as _inner_app
from auth import COOKIE_NAME as _ZHIHU_COOKIE
from auth import AccountAuth, load_stored_account

import panel_auth

_REWRITE_TYPES = ("text/html", "text/javascript", "text/css")
_TOKENS = (b"/static/", b"/api/")


def _rewrite(body, prefix):
    p = prefix.encode("utf-8")
    for token in _TOKENS:
        body = body.replace(b'"' + token, b'"' + p + token)
        body = body.replace(b"'" + token, b"'" + p + token)
    body = body.replace(b'href="/"', b'href="' + p + b'/"')
    return body


def _resolve_account():
    """Mirror app.py's account resolution (env first, stored file second)."""
    secure_flag = (os.environ.get("SESSION_COOKIE_SECURE",
                                  "1" if os.environ.get("K_SERVICE") else "0") == "1")
    try:
        account = AccountAuth(
            os.environ.get("AUTH_USERNAME", "xin"),
            os.environ.get("AUTH_PASSWORD_HASH", ""),
            os.environ.get("SESSION_SECRET", ""),
            secure_cookie=secure_flag,
        )
        if account.configured:
            return account
    except ValueError:
        pass
    stored = load_stored_account()
    if stored:
        try:
            return AccountAuth(*stored, secure_cookie=secure_flag)
        except ValueError:
            return None
    return None


def _panel_sso(environ):
    """If the request has a valid panel session but no zhihu session,
    mint a zhihu session and inject it. Returns a Set-Cookie header
    tuple to add to the response, or None."""
    try:
        cookie_header = environ.get("HTTP_COOKIE", "")
        account = _resolve_account()
        if account is None or not account.configured:
            return None
        if account.session_user(cookie_header):
            return None
        if not panel_auth.panel_username(cookie_header):
            return None
        token = account.issue_session()
        addition = _ZHIHU_COOKIE + "=" + token
        environ["HTTP_COOKIE"] = (
            cookie_header + "; " + addition) if cookie_header else addition
        return account.cookie_header(token)
    except Exception:
        return None


class _PrefixAdapter:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        prefix = (environ.get("HTTP_X_FORWARDED_PREFIX") or "").rstrip("/")
        # Panel SSO first: it may need to extend the Cookie header, so work
        # on a copy of the environ whenever it fires.
        environ = dict(environ)
        sso_header = _panel_sso(environ)
        if not prefix:
            if sso_header is None:
                return self.app(environ, start_response)

            def sso_start_response(status, headers, exc_info=None):
                return start_response(
                    status, list(headers) + [sso_header], exc_info)

            return self.app(environ, sso_start_response)
        # The panel rewrites Host to the loopback upstream (127.0.0.1:<port>)
        # but forwards the browser's Origin unchanged, so the app's
        # Origin-vs-Host same-origin check would reject every browser POST.
        # Normalize the Origin's authority to the forwarded Host whenever
        # the request is not attested cross-site: some browsers/webviews do
        # not send Sec-Fetch-Site at all, while a genuine cross-site page
        # POST always carries Sec-Fetch-Site: cross-site (a forbidden
        # header pages cannot forge) and keeps its Origin, so the app
        # still rejects it. Non-browser clients could omit Origin entirely
        # and pass the app's check anyway, so this weakens nothing.
        origin = environ.get("HTTP_ORIGIN")
        fetch_site = environ.get("HTTP_SEC_FETCH_SITE", "")
        host = environ.get("HTTP_HOST", "")
        if origin and host and fetch_site != "cross-site":
            parsed = urlsplit(origin)
            if parsed.scheme in ("http", "https") and parsed.netloc.lower() != host.lower():
                environ["HTTP_ORIGIN"] = parsed.scheme + "://" + host
        captured = {}

        def capture_start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = list(headers)
            captured["exc_info"] = exc_info
            return lambda data: None

        body_iter = self.app(environ, capture_start_response)
        try:
            body = b"".join(body_iter)
        finally:
            if hasattr(body_iter, "close"):
                body_iter.close()
        headers = captured["headers"]
        content_type = next(
            (v for k, v in headers if k.lower() == "content-type"), "")
        if any(kind in content_type for kind in _REWRITE_TYPES):
            body = _rewrite(body, prefix)
            headers = [(k, v) for k, v in headers
                       if k.lower() != "content-length"]
            headers.append(("Content-Length", str(len(body))))
        if sso_header is not None:
            headers.append(sso_header)
        start_response(captured["status"], headers, captured.get("exc_info"))
        return [body]


app = _PrefixAdapter(_inner_app)
