"""Signal kubelet; confirm a new vLLM execution using a shared startup ID."""
import logging
import math
from pathlib import Path
import threading
import time
import uuid

from .logging_config import log
from .recovery import RecoveryBackend


class KubernetesRecovery(RecoveryBackend):
    def __init__(self, config, wall_clock=time.time):
        self.config = config
        self.wall = wall_clock
        self.lock = threading.Lock()
        self.request = None
        self.armed = False

    def generation(self):
        try:
            with Path(self.config.vllm_start_id_file).open() as handle:
                value = handle.read(80).strip()
            return str(uuid.UUID(value))
        except (OSError, ValueError):
            return None

    def prepare(self):
        generation = self.generation()
        if generation is None:
            raise RuntimeError("A valid vLLM start ID is required")
        with self.lock:
            self.armed = False
            self.request = {
                "generation": generation,
                "deadline": self.wall() + self.config.kubernetes_restart_timeout,
            }

    def restart(self):
        with self.lock:
            self.armed = True
        log("kubernetes restart requested", logging.WARNING)
        return False

    def poll(self):
        with self.lock:
            request = dict(self.request) if self.request else None
        if request is None:
            raise RuntimeError("Missing pending restart")
        generation = self.generation()
        if generation and generation != request["generation"]:
            with self.lock:
                self.request = None
            log("vllm restart detected")
            return True
        if self.wall() >= request["deadline"]:
            raise TimeoutError("Kubelet restart was not observed")
        return False

    def liveness_failed(self):
        with self.lock:
            request = dict(self.request) if self.request else None
            armed = self.armed
        if not armed or not request or self.wall() >= request["deadline"]:
            return False
        # Read-only suppression prevents another failure for the new process
        # before the main loop gets its next scheduling slot.
        generation = self.generation()
        return generation is None or generation == request["generation"]

    def snapshot(self):
        with self.lock:
            return {"mode": "kubernetes", "request": dict(self.request) if self.request else None}

    def restore(self, data):
        if not data:
            return
        if not isinstance(data, dict) or data.get("mode") != "kubernetes":
            raise ValueError("Invalid recovery state")
        request = data.get("request")
        if request is not None:
            if not isinstance(request, dict) or set(request) != {"generation", "deadline"}:
                raise ValueError("Invalid restart request")
            uuid.UUID(request["generation"])
            value = request["deadline"]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid restart deadline")
        with self.lock:
            self.request = dict(request) if request else None
            self.armed = bool(request)

    def cancel(self):
        with self.lock:
            self.request = None
            self.armed = False
