"""App Hub entrypoint for zhihu-core.

The application itself contains no identity verification: entry is
gated entirely by the platform (the App Hub gate in front of
/web/<slug>/, or the trusted local port). This adapter only bridges
the panel's reverse-proxy shape:

* the panel strips the /web/zhihu-core prefix before forwarding, but
  the app's static assets use absolute paths ("/static/...",
  "/api/..."), so text responses get the prefix prepended;
* the panel rewrites Host to the loopback upstream but forwards the
  browser's Origin unchanged, which the app's same-origin check
  would reject on POSTs; the Origin's authority is normalized to the
  forwarded Host unless the request is attested cross-site
  (Sec-Fetch-Site is a forbidden header pages cannot forge, so a
  genuine cross-site POST keeps its Origin and is still rejected).
"""
import os
from urllib.parse import urlsplit


def _state_dir():
    """Persistent state directory: the platform's DATA_DIR env var
    when provided, else the ./data symlink the panel creates inside
    the project folder, else ./.runtime for standalone local runs."""
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    link = os.path.join(here, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(here, ".runtime")


# The app reads its Zhihu-cookie and site-settings state file paths
# from the environment; point them at the platform data directory so
# the state is covered by platform backups. Explicitly configured
# values always win.
_state = _state_dir()
os.environ.setdefault("COOKIE_STATE_FILE", os.path.join(_state, "cookie-state.json"))
os.environ.setdefault("APP_SETTINGS_FILE", os.path.join(_state, "site-settings.json"))

try:
    from app import app as _inner_app
except ImportError:  # package-style import fallback
    from .app import app as _inner_app

_REWRITE_TYPES = ("text/html", "text/javascript", "application/javascript",
                  "text/css")


def _rewrite(body, prefix):
    text = body.decode("utf-8", "surrogateescape")
    # href first: afterwards the asset tokens no longer match inside the
    # already-prefixed href values, so nothing gets prefixed twice.
    text = text.replace('href="/', 'href="' + prefix + '/')
    for token in ('"/static/', "'/static/", '"/api/', "'/api/"):
        text = text.replace(token, token[0] + prefix + token[1:])
    return text.encode("utf-8", "surrogateescape")


class _PrefixAdapter:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        prefix = (environ.get("HTTP_X_FORWARDED_PREFIX") or "").rstrip("/")
        if not prefix:
            return self.app(environ, start_response)
        environ = dict(environ)
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
        start_response(captured["status"], headers, captured.get("exc_info"))
        return [body]


app = _PrefixAdapter(_inner_app)
