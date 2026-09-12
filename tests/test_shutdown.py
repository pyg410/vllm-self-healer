import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class ShutdownTests(unittest.TestCase):
    def test_sigterm_and_sigint_persist_and_exit(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                env = {**os.environ, "VLLM_MODEL": "test", "STATE_FILE": str(path),
                       "STARTUP_GRACE_PERIOD": "300", "ALERT_WEBHOOK_URL": ""}
                process = subprocess.Popen([sys.executable, "-m", "watchdog.main"],
                                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    limit = time.monotonic() + 5
                    while not path.exists() and process.poll() is None and time.monotonic() < limit:
                        time.sleep(.01)
                    self.assertTrue(path.exists(), "process failed to initialize")
                    process.send_signal(sig)
                    stdout, stderr = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertEqual(json.loads(path.read_text())["state"], "recovering")
                    self.assertIn("watchdog stopped", stdout)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
