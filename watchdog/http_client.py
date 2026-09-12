"""Bounded synchronous HTTP on the Linux/macOS main thread.

requests' socket timeout alone is not a total deadline (e.g. slow trickles).
SIGALRM additionally bounds DNS and the whole response body.
"""
from contextlib import contextmanager
import json
import signal
import requests


class DeadlineExceeded(Exception):
    # Do not inherit OSError: urllib3 could wrap it as a connection error.
    pass


@contextmanager
def deadline(seconds):
    def expired(signum, frame):
        raise DeadlineExceeded()
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class HttpClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False

    def request(self, method, url, *, timeout, headers=None, json_body=None, parse_json=False):
        with deadline(timeout):
            with self.session.request(method, url, headers=headers, json=json_body,
                                      timeout=timeout, allow_redirects=False, stream=True) as response:
                status = response.status_code
                body = None
                if status == 200 and parse_json:
                    chunks = bytearray()
                    for chunk in response.iter_content(8192):
                        chunks.extend(chunk)
                        if len(chunks) > 1024 * 1024:
                            raise ValueError("Response too large")
                    body = json.loads(chunks)
                return status, body

    def close(self):
        self.session.close()
