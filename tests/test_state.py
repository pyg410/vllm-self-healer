import json
import tempfile
import unittest
from pathlib import Path
from watchdog.state import StateStore, StateError


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(str(Path(self.temp.name) / "state.json"))
        self.data = {"version": 1, "state": "healthy", "restart_history": [100, 200],
                     "recovery_attempts": 2, "last_successful_inference": None,
                     "recovery_ready_at": 0, "recovery_deadline": 0}

    def test_roundtrip(self):
        self.store.save(self.data)
        self.assertEqual(StateStore(str(self.store.path)).load(), self.data)

    def test_corruption(self):
        for content in ("{", "[]", '{"version": 99}', json.dumps({**self.data, "restart_history": [float("nan")]}),
                        json.dumps({**self.data, "restart_history": [200, 100]})):
            self.store.path.write_text(content)
            with self.assertRaises(StateError):
                self.store.load()

    def test_exclusive_lock(self):
        self.store.acquire()
        self.addCleanup(self.store.close)
        other = StateStore(str(self.store.path))
        with self.assertRaises(StateError):
            other.acquire()
        self.store.close()
        other.acquire()
        other.close()

    def test_missing(self):
        self.assertIsNone(self.store.load())
