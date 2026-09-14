import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from watchdog.controller import Controller
from watchdog.state import StateStore, StateError, RestartPolicy
from watchdog.types import WatchdogState as S, AlertEvent as E
from .helpers import config, OK, BAD, HEALTH_BAD


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.
        self.c = config(startup_grace_period=0, recovery_timeout=10, restart_cooldown=0)
        self.store = StateStore(str(Path(self.temp.name) / "state.json"))
        self.health, self.inference, self.docker, self.alert = (Mock() for _ in range(4))
        self.health.check.return_value = self.inference.check.return_value = OK
        self.controller = self.make()
        self.controller.step()
        self.alert.reset_mock()

    def make(self):
        return Controller(self.c, self.health, self.inference, self.docker, self.alert, self.store,
                          clock=lambda: self.now, wall_clock=lambda: self.now)

    def stall(self):
        self.inference.check.return_value = BAD
        for _ in range(3):
            self.controller.step()

    def test_healthy(self):
        self.assertEqual(self.controller.state, S.HEALTHY)
        self.docker.restart.assert_not_called()

    def test_single_timeout(self):
        self.inference.check.return_value = BAD
        self.controller.step()
        self.assertEqual(self.controller.state, S.SUSPECT)
        self.docker.restart.assert_not_called()

    def test_three_timeouts(self):
        self.stall()
        self.docker.restart.assert_called_once()
        self.assertEqual(self.controller.state, S.RECOVERING)
        self.assertEqual(self.alert.send.call_args_list[0].args[:2], (E.RESTART_TRIGGERED, "ALIVE_BUT_STALLED"))

    def test_health_failures_even_when_inference_succeeds(self):
        self.health.check.return_value = HEALTH_BAD
        for _ in range(3):
            self.controller.step()
        self.docker.restart.assert_called_once()
        self.assertEqual(self.controller.inference_failures, 0)

    def test_recovery_success(self):
        self.stall()
        self.inference.check.return_value = OK
        self.controller.step()
        self.assertEqual(self.controller.state, S.HEALTHY)
        self.assertEqual(self.controller.recovery_attempts, 0)
        self.assertIn(E.RECOVERY_SUCCESS, [call.args[0] for call in self.alert.send.call_args_list])

    def test_failed_latches_and_persists(self):
        self.stall()
        for _ in range(3):
            self.now += 11
            self.controller.step()
        self.assertEqual(self.docker.restart.call_count, 3)
        self.assertEqual(self.controller.state, S.FAILED)
        self.now += 10000
        self.controller.step()
        restored = self.make()
        restored.step()
        self.assertEqual(restored.state, S.FAILED)
        self.assertEqual(self.docker.restart.call_count, 3)
        self.assertIn(E.MAX_RESTART_EXCEEDED, [call.args[0] for call in self.alert.send.call_args_list])

    def test_success_resets_counters(self):
        self.inference.check.return_value = BAD
        self.controller.step()
        self.controller.step()
        self.inference.check.return_value = OK
        self.controller.step()
        self.assertEqual(self.controller.inference_failures, 0)
        self.docker.restart.assert_not_called()

    def test_history_restored(self):
        self.stall()
        restored = self.make()
        self.assertEqual(list(restored.policy.history), [1000])
        self.assertEqual(restored.recovery_attempts, 1)
        self.assertEqual(restored.expires, self.controller.expires)

    def test_grace(self):
        self.c = config(startup_grace_period=300)
        self.store.path.unlink()
        self.health.reset_mock()
        controller = self.make()
        controller.step()
        self.health.check.assert_called_once()
        self.assertEqual(controller.state, S.HEALTHY)
        self.now += 300
        controller.step()
        self.assertEqual(controller.state, S.HEALTHY)

    def test_slow_recovery_cannot_evade_window(self):
        self.c = config(startup_grace_period=0, recovery_timeout=1000, restart_window=600, restart_cooldown=0)
        self.controller = self.make()
        self.controller.step()
        self.stall()
        for _ in range(3):
            self.now += 1001
            self.controller.step()
        self.assertEqual(self.controller.state, S.FAILED)
        self.assertEqual(self.docker.restart.call_count, 3)

    def test_docker_failure_is_bounded(self):
        self.docker.restart.side_effect = RuntimeError("daemon unavailable")
        self.test_failed_latches_and_persists()

    def test_corrupt_state_disables_restart(self):
        self.store.path.write_text("{")
        controller = self.make()
        controller.step()
        self.assertEqual(controller.state, S.FAILED)
        self.docker.restart.assert_not_called()

    def test_save_failure_prevents_docker_call(self):
        self.store.save = Mock(side_effect=StateError())
        self.stall()
        self.assertEqual(self.controller.state, S.FAILED)
        self.docker.restart.assert_not_called()

    def test_attempt_saved_before_docker(self):
        def restart():
            saved = self.store.load()
            self.assertEqual(saved["state"], "restarting")
            self.assertEqual(saved["restart_history"], [1000])
        self.docker.restart.side_effect = restart
        self.stall()
        self.docker.restart.assert_called_once()

    def test_cooldown(self):
        self.controller.policy.config = config(restart_cooldown=300)
        self.controller.policy.history.append(999)
        self.stall()
        self.docker.restart.assert_not_called()
        self.now += 301
        self.controller.step()
        self.docker.restart.assert_called_once()

    def test_stop_prevents_restart(self):
        self.controller.stopping = lambda: True
        self.stall()
        self.docker.restart.assert_not_called()


class PolicyTests(unittest.TestCase):
    def test_window_expiry(self):
        p = RestartPolicy(config(), [100, 101, 102])
        self.assertTrue(p.limit_reached(103, 0))
        self.assertFalse(p.limit_reached(702, 0))
