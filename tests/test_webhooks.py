import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
import requests
from watchdog.hooks import LifecycleHooks
from watchdog.webhooks import EventWebhook
from watchdog.types import AlertEvent as E
from watchdog.http_client import DeadlineExceeded, HttpClient
from .helpers import config, client


class WebhookTests(unittest.TestCase):
    def test_disabled_defaults(self):
        http = client()
        EventWebhook(config(), http).send(E.RESTART_TRIGGERED, "test", 1)
        hooks = LifecycleHooks(config(), http)
        self.assertTrue(hooks.pre_restart("test", 1))
        self.assertTrue(hooks.post_recovery("test", 1))
        http.request.assert_not_called()

    def test_filter_and_static_body_metadata_precedence(self):
        http = client(status=204)
        c = config(event_webhook_url="https://example.com/events", event_webhook_method="PATCH",
                   event_webhook_events="RESTART_CONFIRMED, RECOVERY_SUCCESS",
                   event_webhook_body='{"route":"inference","event":"override"}',
                   event_webhook_headers='{"Authorization":"Bearer SECRET"}')
        events = EventWebhook(c, http)
        events.send(E.RESTART_TRIGGERED, "test", 1)
        http.request.assert_not_called()
        events.send(E.RESTART_CONFIRMED, "test", 1)
        args, kwargs = http.request.call_args
        self.assertEqual(args[0], "PATCH")
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["json_body"]["event"], "RESTART_CONFIRMED")
        self.assertEqual(kwargs["json_body"]["route"], "inference")
        self.assertEqual(kwargs["json_body"]["recovery_mode"], "docker")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer SECRET")

    def test_errors_do_not_escape_or_log_secrets(self):
        for error in (requests.Timeout("SECRET"), DeadlineExceeded(),
                      requests.ConnectionError("SECRET"), ValueError("SECRET")):
            with self.subTest(error=type(error).__name__):
                http = client(error=error)
                events = EventWebhook(config(event_webhook_url="https://example.com/SECRET",
                                      event_webhook_headers='{"Authorization":"SECRET"}',
                                      event_webhook_body='{"token":"SECRET"}'), http)
                with self.assertLogs("watchdog", level="WARNING") as captured:
                    events.send(E.RESTART_TRIGGERED, "test", 1)
                for record in captured.records:
                    self.assertNotIn("SECRET", json.dumps(record.details))
        with self.assertLogs("watchdog", level="WARNING") as captured:
            EventWebhook(config(event_webhook_url="https://example.com"), client(500)).send(E.RECOVERY_FAILED, "test", 1)
        self.assertEqual(captured.records[0].details["http_status"], 500)

    def test_hooks_continue_and_abort_for_both_phases(self):
        for policy in ("continue", "abort"):
            c = config(pre_restart_webhook_url="http://example.com/pre",
                       post_recovery_webhook_url="http://example.com/post",
                       pre_restart_webhook_failure_policy=policy,
                       post_recovery_webhook_failure_policy=policy)
            hooks = LifecycleHooks(c, client(500))
            self.assertEqual(hooks.pre_restart("test", 1), policy == "continue")
            self.assertEqual(hooks.post_recovery("test", 1), policy == "continue")


class RealWebhookTests(unittest.TestCase):
    def test_authenticated_json_request_and_non_json_success_response(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_PUT(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.headers.get("Authorization"), json.loads(body)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"accepted")  # webhook responses need not be JSON

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        http = HttpClient()
        try:
            c = config(event_webhook_url=f"http://127.0.0.1:{server.server_port}",
                       event_webhook_method="PUT",
                       event_webhook_headers='{"Authorization":"Bearer SECRET"}',
                       event_webhook_body='{"token":"BODY_SECRET","event":"ignored"}')
            with self.assertLogs("watchdog", level="INFO") as captured:
                EventWebhook(c, http).send(E.RESTART_CONFIRMED, "test", 1)
            self.assertEqual(received[0][0], "Bearer SECRET")
            self.assertEqual(received[0][1]["token"], "BODY_SECRET")
            self.assertEqual(received[0][1]["event"], "RESTART_CONFIRMED")
            self.assertNotIn("SECRET", str([r.__dict__ for r in captured.records]))
        finally:
            http.close()
            server.shutdown()
            server.server_close()
            thread.join()
