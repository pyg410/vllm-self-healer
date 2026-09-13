from dataclasses import dataclass, fields
import math
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    recovery_mode: str = "docker"
    watchdog_http_host: str = "0.0.0.0"
    watchdog_http_port: int = 9090
    vllm_start_id_file: str = "/run/vllm-watchdog/start-id"
    kubernetes_restart_timeout: float = 120
    vllm_base_url: str = "http://vllm:8000"
    vllm_container_name: str = "vllm"
    vllm_model: str = ""
    vllm_api_key: str = ""
    probe_prompt: str = "ping"
    probe_max_tokens: int = 1
    check_interval: float = 30
    health_timeout: float = 5
    inference_timeout: float = 15
    failure_threshold: int = 3
    startup_grace_period: float = 300
    recovery_check_interval: float = 10
    recovery_timeout: float = 600
    restart_cooldown: float = 300
    restart_window: float = 600
    max_restarts: int = 3
    docker_stop_timeout: int = 30
    docker_api_timeout: float = 60
    alert_webhook_url: str = ""
    alert_timeout: float = 5
    state_file: str = "/data/watchdog_state.json"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls):
        defaults = cls()
        values = {}
        for field in fields(cls):
            raw = os.environ.get(field.name.upper())
            if raw is not None:
                try:
                    values[field.name] = type(getattr(defaults, field.name))(raw)
                except ValueError:
                    raise ValueError(f"Invalid {field.name.upper()}") from None
        config = cls(**values)
        config.validate()
        return config

    def validate(self):
        if self.recovery_mode not in {"docker", "kubernetes"}:
            raise ValueError("Invalid RECOVERY_MODE")
        if not self.watchdog_http_host or not 1 <= self.watchdog_http_port <= 65535:
            raise ValueError("Invalid watchdog HTTP address")
        if self.recovery_mode == "kubernetes" and not self.vllm_start_id_file:
            raise ValueError("VLLM_START_ID_FILE is required")
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, (int, float)):
                minimum = 0 if field.name in {"startup_grace_period", "restart_cooldown", "docker_stop_timeout"} else 1e-9
                if not math.isfinite(value) or value < minimum:
                    raise ValueError(f"Invalid {field.name.upper()}")
        if not self.vllm_model.strip() or not self.state_file:
            raise ValueError("VLLM_MODEL and STATE_FILE are required")
        if self.recovery_mode == "docker" and not self.vllm_container_name.strip():
            raise ValueError("VLLM_CONTAINER_NAME is required in Docker mode")
        for name in ("vllm_base_url", "alert_webhook_url"):
            value = getattr(self, name)
            if not value and name == "alert_webhook_url":
                continue
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError(f"Invalid {name.upper()}")
        if self.recovery_mode == "docker" and self.docker_api_timeout <= self.docker_stop_timeout:
            raise ValueError("DOCKER_API_TIMEOUT must exceed DOCKER_STOP_TIMEOUT")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL")
