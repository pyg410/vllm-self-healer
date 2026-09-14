import io
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from watchdog.controller import Controller
from watchdog.docker_manager import DockerManager
from watchdog.kubernetes_recovery import KubernetesRecovery
from watchdog.logging_config import JsonFormatter, configure, log
from watchdog.state import StateStore, StateError
from watchdog.types import WatchdogState as S, FailureReason as R, AlertEvent as E
from watchdog.types import FailedReason as F, RecoveryReason as C
from .helpers import config, OK, BAD


class RecoveryFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000.
        self.c = config(startup_grace_period=300, recovery_timeout=600, restart_cooldown=0)
        self.store = StateStore(str(Path(self.tmp.name) / 'state.json'))
        self.health, self.inference, self.backend, self.events = (Mock() for _ in range(4))
        self.health.check.return_value = self.inference.check.return_value = OK

    def make(self):
        return Controller(self.c, self.health, self.inference, self.backend, Mock(), self.store,
                          clock=lambda: self.now, wall_clock=lambda: self.now, events=self.events)

    def test_early_success_then_failure_retains_grace_protection(self):
        ctl = self.make()
        ctl.step()
        self.assertEqual(ctl.state, S.HEALTHY)
        self.assertEqual(self.events.send.call_args.kwargs['recovery_reason'], 'startup')
        self.inference.check.return_value = BAD
        for _ in range(10):
            ctl.step()
        self.backend.restart.assert_not_called()
        self.assertEqual(list(ctl.policy.history), [])
        self.now = 1300
        ctl.step()
        self.backend.restart.assert_called_once()
        self.assertEqual(ctl.recovery_reason, C.POST_RESTART)
        self.inference.check.return_value = OK
        ctl.step()
        self.assertEqual(ctl.state, S.HEALTHY)
        self.assertEqual(self.events.send.call_args.kwargs['recovery_reason'], 'post_restart')

    def test_failures_use_full_recovery_deadline_not_threshold(self):
        self.inference.check.return_value = BAD
        ctl = self.make()
        for now in (1000, 1100, 1299, 1300, 1500, 1899):
            self.now = now
            ctl.step()
            self.assertEqual(ctl.state, S.RECOVERING)
            self.assertEqual(ctl.recovery_attempts, 0)
        self.now = 1900
        ctl.step()
        self.backend.restart.assert_called_once()

    def test_restored_old_state_keeps_deadline_and_probes_early(self):
        ctl = self.make()
        data = self.store.load()
        data.pop('failed_reason'); data.pop('recovery_reason')
        self.store.save(data)
        self.c = config(startup_grace_period=9999, recovery_timeout=9999)
        self.now = 1100
        ctl = self.make()
        self.assertEqual(ctl.ready, 1300)
        self.assertEqual(ctl.expires, 1900)
        self.assertEqual(ctl.recovery_reason, C.RESTORED_RECOVERY)
        ctl.step()
        self.assertEqual(ctl.state, S.HEALTHY)

    def test_expired_restore_does_not_extend_recovery(self):
        self.make()
        self.now = 1901
        ctl = self.make()
        ctl.step()
        self.backend.restart.assert_called_once()
        self.health.check.assert_not_called()

    def test_failed_reason_roundtrip_and_old_unknown(self):
        ctl = self.make()
        ctl.fail(R.HOOK_FAILURE, E.RECOVERY_FAILED)
        self.assertEqual(self.store.load()['failed_reason'], 'hook_failure')
        restored = self.make()
        self.assertEqual(restored.failed_reason, F.HOOK_FAILURE)
        restored.step()
        self.backend.restart.assert_not_called()
        data = self.store.load(); data.pop('failed_reason'); self.store.save(data)
        self.assertEqual(self.make().failed_reason, F.UNKNOWN)

    def test_budget_reason(self):
        ctl = self.make()
        ctl.ready = self.now
        ctl.recovery_attempts = self.c.max_restarts
        ctl.restart(R.RECOVERY_TIMEOUT)
        self.assertEqual(self.store.load()['failed_reason'], 'restart_budget_exhausted')
        self.assertEqual(self.events.send.call_args.kwargs['failed_reason'], 'restart_budget_exhausted')

    def test_invalid_persisted_reasons_fail_closed(self):
        self.make()
        data = self.store.load()
        for key in ('failed_reason', 'recovery_reason'):
            self.store.save({**data, key: 'untrusted-secret'})
            with self.assertRaises(StateError):
                self.store.load()

    def test_kubernetes_early_recovery_requires_restart_confirmation(self):
        marker = Path(self.tmp.name) / 'id'
        marker.write_text(str(uuid.uuid4()))
        self.c = config(recovery_mode='kubernetes', vllm_start_id_file=str(marker),
                        startup_grace_period=300, kubernetes_restart_timeout=20)
        self.backend = KubernetesRecovery(self.c, wall_clock=lambda: self.now)
        ctl = self.make()
        ctl.step()
        self.now = 1300
        ctl.restart(R.RECOVERY_TIMEOUT)
        self.health.reset_mock()
        ctl.step()
        self.assertEqual(ctl.state, S.RESTARTING)
        self.health.check.assert_not_called()
        marker.write_text(str(uuid.uuid4()))
        ctl.step()
        self.assertEqual(ctl.recovery_reason, C.POST_RESTART)
        ctl.step()
        self.assertEqual(ctl.state, S.HEALTHY)

    def test_kubernetes_terminal_reasons(self):
        marker = Path(self.tmp.name) / 'id'
        self.c = config(recovery_mode='kubernetes', vllm_start_id_file=str(marker),
                        startup_grace_period=0, kubernetes_restart_timeout=20)
        self.backend = KubernetesRecovery(self.c, wall_clock=lambda: self.now)
        ctl = self.make()
        ctl.restart(R.RECOVERY_TIMEOUT)
        self.assertEqual(ctl.failed_reason, F.INVALID_START_ID)
        self.store.path.unlink()
        marker.write_text(str(uuid.uuid4()))
        ctl = self.make()
        ctl.restart(R.RECOVERY_TIMEOUT)
        self.now += 21
        ctl.step()
        self.assertEqual(self.store.load()['failed_reason'], 'restart_confirmation_timeout')
        self.assertFalse(self.backend.liveness_failed())


class DiagnosticsAndTimezoneTests(unittest.TestCase):
    def test_diagnostics_are_read_only_and_close_client(self):
        factory = Mock()
        self.assertTrue(DockerManager(config(), factory).diagnose())
        factory.return_value.ping.assert_called_once()
        factory.return_value.containers.get.assert_called_once_with('vllm')
        factory.return_value.containers.get.return_value.restart.assert_not_called()
        factory.return_value.close.assert_called_once()

    def test_diagnostics_failures_are_safe_and_bounded(self):
        for stage in ('factory', 'ping', 'get'):
            factory = Mock()
            target = factory if stage == 'factory' else getattr(factory.return_value, stage) if stage == 'ping' else factory.return_value.containers.get
            target.side_effect = RuntimeError('secret-socket-url')
            with self.assertLogs('watchdog', level='WARNING') as logs:
                self.assertFalse(DockerManager(config(), factory).diagnose())
            self.assertNotIn('secret-socket-url', str(logs.output))
            if stage != 'factory': factory.return_value.close.assert_called_once()
        factory = Mock(side_effect=lambda **kw: time.sleep(1))
        start = time.monotonic()
        self.assertFalse(DockerManager(config(docker_api_timeout=.02), factory).diagnose())
        self.assertLess(time.monotonic() - start, .5)

    def test_timezone_validation_and_offsets(self):
        for zone, offset in [('UTC', '+00:00'), ('Asia/Seoul', '+09:00')]:
            config(log_timezone=zone).validate()
            record = logging.LogRecord('watchdog', logging.INFO, '', 0, 'test', (), None)
            self.assertTrue(json.loads(JsonFormatter(zone).format(record))['timestamp'].endswith(offset))
        for zone in ('Secret/Invalid', '/etc/passwd', ''):
            with self.assertRaisesRegex(ValueError, '^Invalid LOG_TIMEZONE$'):
                config(log_timezone=zone).validate()

    def test_stdout_and_file_use_same_timezone(self):
        with tempfile.TemporaryDirectory() as tmp, patch('sys.stdout', new_callable=io.StringIO) as out:
            path = Path(tmp) / 'log'
            configure('INFO', str(path), timezone_name='Asia/Seoul')
            log('timezone test')
            self.assertTrue(json.loads(out.getvalue())['timestamp'].endswith('+09:00'))
            self.assertTrue(json.loads(path.read_text())['timestamp'].endswith('+09:00'))
        configure('INFO')

    def test_kubernetes_factory_never_imports_docker(self):
        source = '''
import sys
class BlockDocker:
    def find_spec(self, fullname, *args):
        if fullname == 'docker' or fullname.startswith('docker.'):
            raise AssertionError('Docker import in Kubernetes mode')
sys.meta_path.insert(0, BlockDocker())
from watchdog.config import Config
from watchdog.recovery import create_recovery
assert create_recovery(Config(recovery_mode='kubernetes')).diagnose() is None
'''
        subprocess.run([sys.executable, '-c', source], check=True, capture_output=True)
