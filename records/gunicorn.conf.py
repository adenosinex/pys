# yt-audio: a single worker keeps the in-memory job table coherent;
# background fetch threads do the long work, HTTP requests stay short.
workers = 1
threads = 8
worker_class = "gthread"
timeout = 120
graceful_timeout = 30
