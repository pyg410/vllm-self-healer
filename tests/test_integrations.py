import time
import unittest
from unittest.mock import Mock, patch
from watchdog.alert import Alert
from watchdog.config import Config
from watchdog.docker_manager import DockerManager
from watchdog.http_client import deadline, DeadlineExceeded
from watchdog.types import AlertEvent as E
from .helpers import config, client


class IntegrationTests(unittest.TestCase):
    def test_webhook_failure_is_contained(self):
        for http in (client(500), client(error=RuntimeError("secret"))):
            with self.assertLogs("watchdog", level="WARNING") as logs:
                Alert(config(alert_webhook_url="http://alert"), http).send(E.RESTART_TRIGGERED, "test", 1)
            self.assertNotIn("secret", str(logs.output))

    def test_webhook_disabled(self):
        http = client()
        Alert(config(), http).send(E.RECOVERY_SUCCESS, "test", 0)
        http.request.assert_not_called()

    def test_docker_sdk(self):
        factory = Mock()
        DockerManager(config(), factory).restart()
        factory.assert_called_once_with(timeout=60)
        factory.return_value.containers.get.assert_called_once_with("vllm")
        factory.return_value.containers.get.return_value.restart.assert_called_once_with(timeout=30)
        factory.return_value.close.assert_called_once()

    def test_docker_close_on_error(self):
        factory = Mock()
        factory.return_value.containers.get.side_effect = RuntimeError()
        with self.assertRaises(RuntimeError):
            DockerManager(config(), factory).restart()
        factory.return_value.close.assert_called_once()

    def test_total_deadline(self):
        start = time.monotonic()
        with self.assertRaises(DeadlineExceeded):
            with deadline(.02):
                time.sleep(1)
        self.assertLess(time.monotonic() - start, .5)

    def test_bad_configuration(self):
        for key, value in (("CHECK_INTERVAL", "0"), ("INFERENCE_TIMEOUT", "nan"),
                           ("FAILURE_THRESHOLD", "1.5"), ("VLLM_MODEL", ""),
                           ("DOCKER_API_TIMEOUT", "20"), ("HEALTH_TIMEOUT", "-1")):
            with self.subTest(key=key), patch.dict("os.environ", {"VLLM_MODEL": "test", key: value}, clear=True):
                with self.assertRaises(ValueError):
                    Config.from_env()
