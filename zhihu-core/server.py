"""Standard-library development server for local Windows/macOS/Linux use."""
import os
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

if __package__:
    from .app import app
else:
    from app import app


class LocalServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass  # Avoid recording request query strings or credentials in development logs.


def main():
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8000'))
    print('Local development API: http://'+host+':'+str(port)+'/health', flush=True)
    print('Cloud deployment: use gunicorn with gunicorn.conf.py.', flush=True)
    with make_server(host, port, app, server_class=LocalServer, handler_class=QuietHandler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
