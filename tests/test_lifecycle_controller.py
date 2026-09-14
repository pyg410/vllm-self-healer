import json
import requests
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock

from watchdog.controller import Controller
from watchdog.hooks import LifecycleHooks
from watchdog.kubernetes_recovery import KubernetesRecovery
from watchdog.probe_server import ProbeStatus
from watchdog.state import StateStore
from watchdog.types import AlertEvent as E, WatchdogState as S
from watchdog.webhooks import EventWebhook
from .helpers import config, OK, BAD


class LifecycleControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000.
        self.sequence = []
        self.failing = set()
        self.pre_callback = lambda: None

    def setup_controller(self, mode="docker", pre="continue", post="continue"):
        self.marker = Path(self.tmp.name) / "start-id"
        self.marker.write_text(str(uuid.uuid4()))
        self.c = config(recovery_mode=mode, vllm_start_id_file=str(self.marker),
                        startup_grace_period=0, recovery_timeout=10, restart_cooldown=0,
                        event_webhook_url="http://local/event",
                        pre_restart_webhook_url="http://local/pre",
                        post_recovery_webhook_url="http://local/post",
                        pre_restart_webhook_failure_policy=pre,
                        post_recovery_webhook_failure_policy=post)
        self.http = Mock()
        def request(method, url, **kwargs):
            self.sequence.append(kwargs["json_body"]["event"])
            if url.endswith("/pre"):
                self.pre_callback()
            return (500 if url.rsplit("/", 1)[-1] in self.failing else 204), None
        self.http.request.side_effect = request
        if mode == "docker":
            self.backend = Mock()
            self.backend.restart.side_effect = lambda: self.sequence.append("BACKEND")
            self.status = None
        else:
            self.backend = KubernetesRecovery(self.c, wall_clock=lambda: self.now)
            original = self.backend.restart
            def restart():
                self.sequence.append("BACKEND")
                return original()
            self.backend.restart = restart
            self.status = ProbeStatus(self.backend)
        self.health, self.inference, self.legacy = Mock(), Mock(), Mock()
        self.health.check.return_value = self.inference.check.return_value = OK
        self.store = StateStore(str(Path(self.tmp.name) / (mode + ".json")))
        self.controller = self.make(self.backend)
        self.controller.step()
        self.sequence.clear()
        self.legacy.reset_mock()

    def make(self, backend):
        return Controller(self.c, self.health, self.inference, backend, self.legacy, self.store,
                          clock=lambda: self.now, wall_clock=lambda: self.now, status=self.status,
                          hooks=LifecycleHooks(self.c, self.http), events=EventWebhook(self.c, self.http))

    def stall(self):
        self.inference.check.return_value = BAD
        for _ in range(3):
            self.controller.step()

    def finish(self):
        if self.c.recovery_mode == "kubernetes":
            self.marker.write_text(str(uuid.uuid4()))
            self.controller.step()
        self.inference.check.return_value = OK
        self.controller.step()

    def test_docker_sequence_and_legacy_event_compatibility(self):
        self.setup_controller()
        self.stall()
        self.finish()
        self.assertEqual(self.sequence, ["RESTART_TRIGGERED", "PRE_RESTART", "BACKEND",
                                        "RESTART_CONFIRMED", "POST_RECOVERY", "RECOVERY_SUCCESS"])
        self.assertEqual([call.args[0] for call in self.legacy.send.call_args_list],
                         [E.RESTART_TRIGGERED, E.RECOVERY_SUCCESS])

    def test_kubernetes_confirmation_only_after_uuid_change(self):
        self.setup_controller("kubernetes")
        self.stall()
        self.assertEqual(self.sequence, ["RESTART_TRIGGERED", "PRE_RESTART", "BACKEND"])
        self.assertEqual(self.status.response("/live")[0], 503)
        self.controller.step()
        self.assertNotIn("RESTART_CONFIRMED", self.sequence)
        self.finish()
        self.assertEqual(self.sequence, ["RESTART_TRIGGERED", "PRE_RESTART", "BACKEND",
                                        "RESTART_CONFIRMED", "POST_RECOVERY", "RECOVERY_SUCCESS"])
        self.assertEqual(self.status.response("/ready")[0], 200)

    def test_pre_abort_never_restarts_both_modes(self):
        for mode in ("docker", "kubernetes"):
            with self.subTest(mode=mode):
                self.setup_controller(mode, pre="abort")
                self.failing.add("pre")
                self.stall()
                self.assertEqual(self.controller.state, S.FAILED)
                self.assertNotIn("BACKEND", self.sequence)
                self.assertNotIn("RESTART_CONFIRMED", self.sequence)
                self.assertEqual(self.store.load()["state"], "failed")
                self.assertEqual(self.controller.recovery_attempts, 1)
                self.controller.step()
                if self.status:
                    self.assertEqual(self.status.response("/live")[0], 200)
                    self.assertEqual(self.status.response("/ready")[0], 503)
                self.failing.clear()

    def test_post_abort_never_marks_ready_or_success(self):
        for mode in ("docker", "kubernetes"):
            with self.subTest(mode=mode):
                self.setup_controller(mode, post="abort")
                self.failing.add("post")
                self.stall()
                self.finish()
                self.assertEqual(self.controller.state, S.FAILED)
                self.assertNotIn("RECOVERY_SUCCESS", self.sequence)
                self.assertIn("RECOVERY_FAILED", self.sequence)
                self.assertEqual(self.controller.recovery_attempts, 1)
                if self.status:
                    self.assertEqual(self.status.response("/live")[0], 200)
                    self.assertEqual(self.status.response("/ready")[0], 503)
                self.failing.clear()

    def test_continue_hooks_and_failed_event_delivery_preserve_recovery(self):
        for mode in ("docker", "kubernetes"):
            with self.subTest(mode=mode):
                self.setup_controller(mode)
                self.failing.update({"pre", "post", "event"})
                self.stall()
                self.finish()
                self.assertEqual(self.controller.state, S.HEALTHY)
                self.assertIn("BACKEND", self.sequence)
                self.failing.clear()

    def test_event_timeout_does_not_change_recovery(self):
        self.setup_controller("kubernetes")
        original = self.http.request.side_effect
        def timeout_event(method, url, **kwargs):
            if url.endswith("/event"):
                raise requests.Timeout("SECRET")
            return original(method, url, **kwargs)
        self.http.request.side_effect = timeout_event
        self.stall()
        self.finish()
        self.assertEqual(self.controller.state, S.HEALTHY)

    def test_failed_restore_log_and_liveness_remain_safe(self):
        self.setup_controller("kubernetes", pre="abort")
        self.failing.add("pre")
        self.stall()
        backend = KubernetesRecovery(self.c, wall_clock=lambda: self.now)
        with self.assertLogs("watchdog", level="INFO") as captured:
            restored = self.make(backend)
        record = next(r for r in captured.records if r.getMessage() == "persisted state restored")
        self.assertEqual(record.details["state"], "failed")
        self.assertEqual(restored.state, S.FAILED)
        self.assertFalse(backend.liveness_failed())

    def test_no_confirmation_on_docker_restart_error(self):
        self.setup_controller()
        self.backend.restart.side_effect = RuntimeError("daemon unavailable")
        self.stall()
        self.assertEqual(self.controller.state, S.RECOVERING)
        self.assertNotIn("RESTART_CONFIRMED", self.sequence)

    def test_no_post_hook_on_failed_probes(self):
        self.setup_controller()
        self.stall()
        self.controller.step()
        self.assertNotIn("POST_RECOVERY", self.sequence)

    def test_kubernetes_replacement_during_slow_pre_hook_is_not_killed(self):
        self.setup_controller("kubernetes")
        def replace_during_hook():
            self.assertFalse(self.backend.liveness_failed())
            self.marker.write_text(str(uuid.uuid4()))
            self.now += self.c.kubernetes_restart_timeout + 1
        self.pre_callback = replace_during_hook
        self.stall()
        self.assertEqual(self.status.response("/live")[0], 200)
        self.assertGreater(self.backend.snapshot()["request"]["deadline"], self.now)
        self.controller.step()
        self.assertIn("RESTART_CONFIRMED", self.sequence)
        self.assertEqual(self.controller.state, S.RECOVERING)

    def test_interrupted_pre_hook_cannot_restore_an_armed_request(self):
        self.setup_controller("kubernetes")
        def interrupt():
            raise SystemExit()
        self.pre_callback = interrupt
        with self.assertRaises(SystemExit):
            self.stall()
        self.controller.persist()  # main's shutdown path
        saved = self.store.load()
        self.assertEqual(saved["state"], "suspect")
        self.assertEqual(saved["backend"], {})
        restored = KubernetesRecovery(self.c, wall_clock=lambda: self.now)
        self.make(restored)
        self.assertFalse(restored.liveness_failed())

    def test_restore_log_preserves_old_recovery_timing(self):
        self.setup_controller("kubernetes")
        self.stall()
        with self.assertLogs("watchdog", level="INFO") as captured:
            self.make(KubernetesRecovery(self.c, wall_clock=lambda: self.now))
        restored = next(r.details for r in captured.records if r.getMessage() == "persisted state restored")
        self.assertEqual(restored["state"], "restarting")
        self.assertEqual(restored["backend"], "kubernetes")
        self.assertEqual(restored["restart_history_count"], 1)
        self.marker.write_text(str(uuid.uuid4()))
        self.controller.step()
        saved = self.store.load()
        from dataclasses import replace
        self.c = replace(self.c, startup_grace_period=999, recovery_timeout=999)
        with self.assertLogs("watchdog", level="INFO") as captured:
            restored_controller = self.make(KubernetesRecovery(self.c, wall_clock=lambda: self.now))
        restored = next(r.details for r in captured.records if r.getMessage() == "persisted state restored")
        self.assertTrue(restored["stored_recovery_timing_applied"])
        self.assertEqual(restored_controller.expires, saved["recovery_deadline"])
        self.assertEqual(restored_controller.ready, saved["recovery_ready_at"])
