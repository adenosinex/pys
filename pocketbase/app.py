"""Proxy-mapping stub for the standalone PocketBase unit.

The real service is /etc/systemd/system/pocketbase.service
(loopback 127.0.0.1:8090, data in /srv/apphub/data/pocketbase/).

This registry row exists ONLY so the panel /web/ proxy and the
direct-code generator can route /web/pocketbase/ to it. Do NOT start
this project from the console: the launcher would die on EADDRINUSE
(port 8090 is held by the standalone unit), the same shape as the
shoc apirelay registry row.
"""


def app(environ, start_response):
    body = b"pocketbase is served by its standalone unit; this stub never runs"
    start_response(
        "503 Service Unavailable",
        [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))],
    )
    return [body]
