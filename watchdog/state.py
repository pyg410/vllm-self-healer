"""Atomic persistent state and exclusive single-process ownership."""
from collections import deque
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
from .types import WatchdogState


class StateError(Exception):
    pass


class StateStore:
    def __init__(self, filename):
        self.path = Path(filename)
        self.lock_file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(str(self.path) + ".lock", "a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.close()
            raise StateError("State file is already in use") from None

    def close(self):
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def load(self):
        try:
            with self.path.open() as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError()
            WatchdogState(data["state"])
            history = data["restart_history"]
            if not isinstance(history, list) or any(not self.timestamp(x) for x in history):
                raise ValueError()
            if history != sorted(history):
                raise ValueError()
            if type(data["recovery_attempts"]) is not int or data["recovery_attempts"] < 0:
                raise ValueError()
            for key in ("last_successful_inference", "recovery_ready_at", "recovery_deadline"):
                if data[key] is not None and not self.timestamp(data[key]):
                    raise ValueError()
            if data["state"] == WatchdogState.RECOVERING.value:
                if data["recovery_ready_at"] is None or data["recovery_deadline"] is None:
                    raise ValueError()
                if data["recovery_deadline"] < data["recovery_ready_at"]:
                    raise ValueError()
            return data
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError):
            raise StateError("Cannot read valid state; automatic restart disabled") from None

    @staticmethod
    def timestamp(value):
        return type(value) in (int, float) and math.isfinite(value) and value >= 0

    def save(self, data):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as handle:
                temporary = handle.name
                json.dump(data, handle, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, ValueError):
            raise StateError("Cannot persist state; automatic restart disabled") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


class RestartPolicy:
    def __init__(self, config, history=()):
        self.config = config
        self.history = deque(history)

    def prune(self, now):
        # Keep the latest attempt for cooldowns longer than the rolling window.
        while len(self.history) > 1 and self.history[0] <= now - self.config.restart_window:
            self.history.popleft()

    def limit_reached(self, now, recovery_attempts):
        self.prune(now)
        # Also cap attempts without verified recovery. A long model load can
        # outlast the rolling window and must not permit an endless loop.
        recent = sum(timestamp > now - self.config.restart_window for timestamp in self.history)
        return recent >= self.config.max_restarts or recovery_attempts >= self.config.max_restarts

    def cooling_down(self, now):
        return bool(self.history) and now - self.history[-1] < self.config.restart_cooldown
