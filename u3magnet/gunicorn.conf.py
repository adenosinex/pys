# u3magnet: reads are short SQL queries; one worker keeps the
# exclusion writes coherent, threads absorb page reads.
workers = 1
threads = 8
worker_class = "gthread"
timeout = 120
graceful_timeout = 30
