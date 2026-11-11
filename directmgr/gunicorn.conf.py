# directmgr: single worker keeps the list/sync state coherent;
# sync runs in-request (ssh to shoc is short), threads absorb reads.
workers = 1
threads = 8
worker_class = "gthread"
timeout = 120
graceful_timeout = 30
