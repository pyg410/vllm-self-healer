import unittest
import requests
from watchdog.health import HealthProbe
from watchdog.http_client import DeadlineExceeded
from watchdog.types import FailureReason as R
from .helpers import config, client


class HealthTests(unittest.TestCase):
    def test_status(self):
        for status in (200, 201, 301, 401, 500):
            with self.subTest(status=status):
                self.assertEqual(HealthProbe(config(), client(status)).check().ok, status == 200)

    def test_errors(self):
        for error, reason in [(requests.Timeout(), R.HEALTH_TIMEOUT),
                              (DeadlineExceeded(), R.HEALTH_TIMEOUT),
                              (requests.ConnectionError(), R.HEALTH_CONNECTION_ERROR)]:
            with self.subTest(reason=reason):
                self.assertEqual(HealthProbe(config(), client(error=error)).check().reason, reason)

    def test_auth_and_timeout(self):
        http = client()
        HealthProbe(config(vllm_api_key="secret"), http).check()
        self.assertEqual(http.request.call_args.kwargs["headers"], {"Authorization": "Bearer secret"})
        self.assertEqual(http.request.call_args.kwargs["timeout"], 5)
