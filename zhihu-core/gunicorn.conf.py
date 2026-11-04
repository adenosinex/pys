"""One small Linux WSGI service; the cloud platform controls PORT."""
import os

bind = '0.0.0.0:' + os.environ.get('PORT', '8000')
workers = 1
worker_class = 'gthread'
threads = 4
timeout = 90
graceful_timeout = 30
accesslog = None
errorlog = '-'
max_requests = 500
max_requests_jitter = 50
