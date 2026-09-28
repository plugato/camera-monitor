bind = "0.0.0.0:8090"
workers = 1
worker_class = "gthread"
threads = 8
accesslog = "-"
errorlog = "-"
timeout = 120


def post_worker_init(worker):
    import server
    server.start_capture_worker()