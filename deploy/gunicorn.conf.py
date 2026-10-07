"""Production worker topology: independent DB pools, concurrent readers."""
bind = "0.0.0.0:5100"
workers = 2
worker_class = "gthread"
threads = 4
# create_app opens PostgreSQL connections. Never inherit them across fork.
preload_app = False
timeout = 120
