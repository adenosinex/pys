# dohrelay: single worker keeps ctl calls serial-ish; threads
# absorb page reads (each state read makes short ssh calls).
workers = 1
threads = 8
worker_class = "gthread"
timeout = 120
graceful_timeout = 30
