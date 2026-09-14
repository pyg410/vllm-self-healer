import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from watchdog.config import Config, warn_unknown_environment, SUPPORTED_ENVIRONMENT
from watchdog.logging_config import configure, configuration_loaded, log, log_error
from watchdog.main import main
from .helpers import config


class OperationalLoggingTests(unittest.TestCase):
    def tearDown(self):
        logger = logging.getLogger("watchdog")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

    def test_effective_config_excludes_all_sensitive_values(self):
        c = config(vllm_api_key="KEY_SECRET", vllm_model="MODEL_SECRET",
                   probe_prompt="PROMPT_SECRET", event_webhook_url="https://example.com/URL_SECRET",
                   event_webhook_headers='{"Authorization":"HEADER_SECRET"}',
                   event_webhook_body='{"token":"BODY_SECRET"}',
                   state_file="/STATE_SECRET", log_file="/PATH_SECRET")
        output = io.StringIO()
        with patch("sys.stdout", output):
            configure("ERROR")
            configuration_loaded(c.effective())
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "configuration loaded")
        self.assertEqual(records[0]["startup_grace_period"], 300)
        self.assertTrue(records[0]["vllm_api_key_configured"])
        self.assertTrue(records[0]["event_webhook_configured"])
        self.assertNotIn("SECRET", output.getvalue())

    def test_unknown_environment_warns_without_values(self):
        with self.assertLogs("watchdog", level="WARNING") as captured:
            warn_unknown_environment({"STARTUP_GRACE_PEROID": "SECRET"})
        record = captured.records[0]
        self.assertEqual(record.details["suggestion"], "STARTUP_GRACE_PERIOD")
        self.assertNotIn("SECRET", str(record.__dict__))

    def test_unrelated_and_correct_environment_are_quiet(self):
        env = {name: "" for name in SUPPORTED_ENVIRONMENT}
        env.update({"HOME": "/home/test", "PATH": "x", "HOSTNAME": "x", "DOCKER_HOST": "x",
                    "VLLM_IMAGE": "x", "VLLM_USE_V1": "1", "KUBERNETES_SERVICE_HOST": "x",
                    "DOCKER_SOCKET_GID": "0", "LOGNAME": "x", "PYTHON_VERSION": "3.12"})
        with patch("watchdog.config.log") as log_mock:
            warn_unknown_environment(env)
        log_mock.assert_not_called()

    def test_stdout_default_and_rotation(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", output):
            configure("INFO")
            self.assertEqual(len(logging.getLogger("watchdog").handlers), 1)
            path = Path(directory) / "watchdog.log"
            configure("INFO", str(path), 250, 2)
            handlers = logging.getLogger("watchdog").handlers
            self.assertEqual(len(handlers), 2)
            self.assertIsInstance(handlers[1], RotatingFileHandler)
            for i in range(20):
                log("rotation example", index=i)
            self.assertEqual(len(output.getvalue().splitlines()), 20)
            self.assertTrue(Path(str(path) + ".1").exists())
            self.assertTrue(Path(str(path) + ".2").exists())
            self.assertFalse(Path(str(path) + ".3").exists())
            self.assertEqual(json.loads(path.read_text().splitlines()[-1])["index"], 19)

    def test_startup_file_error_is_safe(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"VLLM_MODEL": "test", "LOG_FILE": directory}, clear=True), patch("sys.stdout", output):
                self.assertEqual(main(), 1)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            error = next(r for r in records if r["event"] == "watchdog startup failed")
            self.assertEqual(error["stage"], "logging_setup")
            self.assertEqual(error["error_type"], "IsADirectoryError")
            self.assertNotIn(directory, output.getvalue())

    def test_startup_permission_error_is_identifiable(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"VLLM_MODEL": "test"}, clear=True), patch("sys.stdout", output):
            with patch("watchdog.main.StateStore.acquire", side_effect=PermissionError("SECRET")):
                self.assertEqual(main(), 1)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        error = next(r for r in records if r["event"] == "watchdog startup failed")
        self.assertEqual(error["error_type"], "PermissionError")
        self.assertEqual(error["stage"], "state_directory_and_lock")
        self.assertNotIn("SECRET", output.getvalue())

    def test_startup_bind_error_is_identifiable(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            env = {"VLLM_MODEL": "test", "RECOVERY_MODE": "kubernetes",
                   "WATCHDOG_HTTP_HOST": "127.0.0.1",
                   "WATCHDOG_HTTP_PORT": str(occupied.getsockname()[1]),
                   "STATE_FILE": str(Path(directory) / "state.json")}
            with patch.dict(os.environ, env, clear=True), patch("sys.stdout", output):
                self.assertEqual(main(), 1)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        error = next(r for r in records if r["event"] == "watchdog startup failed")
        self.assertEqual(error["stage"], "probe_server_bind")
        self.assertEqual(error["error_type"], "OSError")
        self.assertEqual(sum(r["event"] == "configuration loaded" for r in records), 1)

    def test_error_helper_omits_exception_message(self):
        with self.assertLogs("watchdog", level="ERROR") as captured:
            log_error("watchdog startup failed", OSError(98, "SECRET"), "probe_server_bind")
        self.assertEqual(captured.records[0].details["error_code"], 98)
        self.assertNotIn("SECRET", str(captured.records[0].__dict__))

    def test_invalid_json_and_policy_configuration(self):
        for field, value in (
            ("EVENT_WEBHOOK_BODY", '{"token": SECRET}'),
            ("EVENT_WEBHOOK_BODY", "[]"),
            ("EVENT_WEBHOOK_BODY", '{"n": NaN}'),
            ("EVENT_WEBHOOK_HEADERS", '{"Authorization": 1}'),
            ("EVENT_WEBHOOK_HEADERS", '{"Authorization": "bad\\r\\nvalue"}'),
            ("EVENT_WEBHOOK_METHOD", "DELETE"),
            ("EVENT_WEBHOOK_EVENTS", "DOES_NOT_EXIST"),
            ("PRE_RESTART_WEBHOOK_FAILURE_POLICY", "retry"),
            ("POST_RECOVERY_WEBHOOK_FAILURE_POLICY", "retry"),
            ("LOG_MAX_BYTES", "0"),
            ("LOG_BACKUP_COUNT", "0"),
        ):
            with self.subTest(field=field, value=value), patch.dict(
                    os.environ, {"VLLM_MODEL": "test", field: value}, clear=True):
                with self.assertRaises(ValueError) as caught:
                    Config.from_env()
                self.assertNotIn("SECRET", str(caught.exception))

    def test_invalid_json_startup_log_names_setting_only(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"VLLM_MODEL": "test", "EVENT_WEBHOOK_BODY": "SECRET"}, clear=True), patch("sys.stdout", output):
            self.assertEqual(main(), 2)
        self.assertIn("EVENT_WEBHOOK_BODY", output.getvalue())
        self.assertNotIn("SECRET", output.getvalue())
