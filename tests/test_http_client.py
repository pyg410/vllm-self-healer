"""Exercise real HTTP parsing and total deadlines without Docker or GPU."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from watchdog.http_client import HttpClient, DeadlineExceeded


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        try:
            if self.path == "/slow":
                for _ in range(100):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(.01)
            elif self.path == "/large":
                self.wfile.write(b" " * (1024 * 1024 + 1))
            elif self.path == "/invalid":
                self.wfile.write(b"{")
            else:
                self.wfile.write(b'{"ok": true}')
        except (BrokenPipeError, ConnectionResetError):
            pass


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.client = HttpClient()
        self.addCleanup(self.client.close)

    def test_json(self):
        self.assertEqual(self.client.request("GET", self.url + "/ok", timeout=1, parse_json=True),
                         (200, {"ok": True}))

    def test_slow_trickle_total_timeout(self):
        start = time.monotonic()
        with self.assertRaises(DeadlineExceeded):
            self.client.request("GET", self.url + "/slow", timeout=.08, parse_json=True)
        self.assertLess(time.monotonic() - start, .5)

    def test_no_redirect(self):
        status, _ = self.client.request("GET", self.url + "/redirect", timeout=1)
        self.assertEqual(status, 302)

    def test_invalid_and_large(self):
        for path in ("/invalid", "/large"):
            with self.assertRaises(ValueError):
                self.client.request("GET", self.url + path, timeout=1, parse_json=True)
