"""Read-only HTTP view of the main loop's published state."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import threading

from .logging_config import log
from .types import WatchdogState as S


class ProbeStatus:
    def __init__(self, recovery):
        self.lock = threading.Lock()
        self.state = S.RECOVERING
        self.recovery = recovery

    def publish(self, state):
        with self.lock:
            self.state = state

    def response(self, path):
        with self.lock:
            state = self.state
        if path == "/ready":
            ok = state == S.HEALTHY and not self.recovery.liveness_failed()
        elif path == "/live":
            ok = state == S.FAILED or not self.recovery.liveness_failed()
        else:
            return 404, {"error": "not_found"}
        return (200 if ok else 503), {"status": "healthy" if ok else "unhealthy", "state": state.value}


class ProbeServer:
    def __init__(self, host, port, status):
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(2)
                super().setup()

            def log_message(self, *args):
                pass

            def do_GET(self):
                code, data = status.response(self.path)
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                    if self.path == "/live" and code == 503:
                        log("liveness failure exposed", logging.WARNING, state=data["state"])
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
        self.server = HTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .1}, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=3)
        self.server.server_close()
