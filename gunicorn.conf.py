"""Gunicorn configurável por ambiente: WEB_CONCURRENCY, GUNICORN_THREADS, GUNICORN_TIMEOUT, PORT."""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 20
keepalive = 5
max_requests = 2000
max_requests_jitter = 200
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(m)s %(U)s" %(s)s %(b)s %(L)s'   # %(U)s = sem query string (tokens não vão pro log)
