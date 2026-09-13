import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock
import requests

from watchdog.controller import Controller
from watchdog.kubernetes_recovery import KubernetesRecovery
from watchdog.probe_server import ProbeServer, ProbeStatus
from watchdog.recovery import create_recovery
from watchdog.docker_manager import DockerManager
from watchdog.state import StateStore, StateError
from watchdog.types import WatchdogState as S
from .helpers import config, OK, BAD


class KubernetesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / "start-id"
        self.marker.write_text(str(uuid.uuid4()))
        self.now = 1000.
        self.c = config(recovery_mode="kubernetes", vllm_start_id_file=str(self.marker),
                        startup_grace_period=0, recovery_timeout=10,
                        restart_cooldown=0, kubernetes_restart_timeout=30)
        self.backend = KubernetesRecovery(self.c, wall_clock=lambda: self.now)
        self.status = ProbeStatus(self.backend)
        self.health, self.inference, self.alert = Mock(), Mock(), Mock()
        self.health.check.return_value = self.inference.check.return_value = OK
        self.store = StateStore(str(Path(self.tmp.name) / "state.json"))
        self.controller = self.make(self.backend)
        self.controller.step()

    def make(self, backend):
        return Controller(self.c, self.health, self.inference, backend, self.alert, self.store,
                          clock=lambda: self.now, wall_clock=lambda: self.now,
                          status=self.status)

    def stall(self):
        self.inference.check.return_value = BAD
        for _ in range(3):
            self.controller.step()

    def reboot(self):
        self.marker.write_text(str(uuid.uuid4()))
        self.controller.step()

    def test_readiness_states(self):
        for state in S:
            self.status.publish(state)
            self.assertEqual(self.status.response("/ready")[0], 200 if state == S.HEALTHY else 503)

    def test_request_stays_latched_without_restart_even_if_probes_recover(self):
        self.stall()
        self.assertEqual(self.controller.state, S.RESTARTING)
        self.assertEqual(self.status.response("/live")[0], 503)
        self.inference.check.return_value = OK
        self.controller.step()
        self.assertEqual(self.controller.state, S.RESTARTING)
        self.assertEqual(self.status.response("/live")[0], 503)
        self.reboot()
        self.assertEqual(self.controller.state, S.RECOVERING)
        self.assertEqual(self.status.response("/live")[0], 200)
        self.assertEqual(self.status.response("/ready")[0], 503)
        self.controller.step()
        self.assertEqual(self.controller.state, S.HEALTHY)

    def test_new_generation_suppresses_signal_before_controller_poll(self):
        self.stall()
        self.marker.write_text(str(uuid.uuid4()))
        self.assertEqual(self.status.response("/live")[0], 200)
        self.assertEqual(self.controller.state, S.RESTARTING)
        self.assertEqual(self.status.response("/ready")[0], 503)

    def test_persistence_restores_pending_signal(self):
        self.stall()
        restored = KubernetesRecovery(self.c, wall_clock=lambda: self.now)
        self.status = ProbeStatus(restored)
        self.controller = self.make(restored)
        self.assertTrue(restored.liveness_failed())
        self.assertEqual(self.controller.state, S.RESTARTING)
        self.assertEqual(self.controller.recovery_attempts, 1)
        self.reboot()
        self.assertEqual(self.controller.state, S.RECOVERING)

    def test_no_signal_before_durable_reservation(self):
        self.backend.prepare()
        self.assertFalse(self.backend.liveness_failed())
        self.backend.restart()
        self.assertTrue(self.backend.liveness_failed())

    def test_missing_marker_fails_closed(self):
        self.marker.unlink()
        self.stall()
        self.assertEqual(self.controller.state, S.FAILED)
        self.assertEqual(self.status.response("/live")[0], 200)
        self.assertEqual(self.status.response("/ready")[0], 503)

    def test_timeout_stops_signal_and_latches_failed(self):
        self.stall()
        self.now += 31
        self.assertEqual(self.status.response("/live")[0], 200)
        self.controller.step()
        self.assertEqual(self.controller.state, S.FAILED)
        self.controller.step()
        self.assertEqual(self.controller.recovery_attempts, 1)

    def test_budget_caps_requests(self):
        self.stall()
        for _ in range(3):
            self.reboot()
            self.now += 11
            self.controller.step()
        self.assertEqual(self.controller.state, S.FAILED)
        self.assertEqual(self.controller.recovery_attempts, 3)
        self.assertEqual(self.status.response("/live")[0], 200)
        self.assertEqual(self.status.response("/ready")[0], 503)

    def test_concurrent_readers_do_not_clear_latch(self):
        self.stall()
        results = []
        def read():
            for _ in range(20):
                results.append(self.status.response("/live")[0])
        threads = [threading.Thread(target=read) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [503] * 80)
        self.assertTrue(self.backend.liveness_failed())

    def test_http_endpoints(self):
        server = ProbeServer("127.0.0.1", 0, self.status)
        server.start()
        self.addCleanup(server.close)
        url = f"http://127.0.0.1:{server.server.server_port}"
        for endpoint, expected in (("/live", 200), ("/ready", 200), ("/missing", 404)):
            self.assertEqual(requests.get(url + endpoint, timeout=2).status_code, expected)
        self.stall()
        self.assertEqual(requests.get(url + "/live", timeout=2).status_code, 503)
        self.assertEqual(requests.get(url + "/ready", timeout=2).status_code, 503)

    def test_invalid_backend_state(self):
        self.backend.prepare()
        saved = self.store.load()
        saved["backend"] = {"mode": "kubernetes", "request": {"generation": "bad", "deadline": 1000}}
        self.store.save(saved)
        self.controller = self.make(self.backend)
        self.assertEqual(self.controller.state, S.FAILED)

    def test_persist_failure_never_exposes_liveness_failure(self):
        def fail_save(data):
            self.assertFalse(self.backend.liveness_failed())
            raise StateError()
        self.store.save = fail_save
        self.stall()
        self.assertEqual(self.controller.state, S.FAILED)
        self.assertFalse(self.backend.liveness_failed())

    def test_restart_reserves_before_signal(self):
        original = self.store.save
        observed = []
        def save(data):
            if data["state"] == "restarting":
                observed.append(self.backend.liveness_failed())
            original(data)
        self.store.save = save
        self.stall()
        self.assertFalse(observed[0])
        self.assertTrue(self.backend.liveness_failed())

    def test_restored_failed_never_requests_restart(self):
        self.stall()
        self.now += 31
        self.controller.step()
        restored = KubernetesRecovery(self.c, wall_clock=lambda: self.now)
        self.status = ProbeStatus(restored)
        self.controller = self.make(restored)
        self.assertEqual(self.controller.state, S.FAILED)
        self.assertEqual(self.status.response("/live")[0], 200)
        self.assertEqual(self.status.response("/ready")[0], 503)

    def test_expired_request_survives_restart_without_new_budget(self):
        self.stall()
        self.now += 31
        restored = KubernetesRecovery(self.c, wall_clock=lambda: self.now)
        self.status = ProbeStatus(restored)
        self.controller = self.make(restored)
        self.assertFalse(restored.liveness_failed())
        self.controller.step()
        self.assertEqual(self.controller.state, S.FAILED)
        self.assertEqual(self.controller.recovery_attempts, 1)

    def test_v1_state_without_backend(self):
        saved = self.store.load()
        saved.pop("backend")
        self.store.save(saved)
        self.controller = self.make(self.backend)
        self.assertEqual(self.controller.state, S.RECOVERING)
        self.controller.step()
        self.assertEqual(self.controller.state, S.HEALTHY)


class ModeTests(unittest.TestCase):
    def test_default_docker(self):
        self.assertEqual(config().recovery_mode, "docker")
        self.assertIsInstance(create_recovery(config()), DockerManager)

    def test_mode_aware_validation(self):
        with self.assertRaises(ValueError):
            config(vllm_container_name="").validate()
        config(recovery_mode="kubernetes", vllm_container_name="",
               docker_api_timeout=1, docker_stop_timeout=30).validate()
        for changes in ({"recovery_mode": "bad"}, {"watchdog_http_port": 65536},
                        {"recovery_mode": "kubernetes", "vllm_start_id_file": ""}):
            with self.assertRaises(ValueError):
                config(**changes).validate()
