from dataclasses import dataclass, fields
import difflib
import logging
import math
import re
import os
from urllib.parse import urlsplit
from .logging_config import log
from .webhooks import WEBHOOK_PREFIXES, METHODS, json_object, validate_headers, event_names


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
    log_file: str = ""
    log_max_bytes: int = 10485760
    log_backup_count: int = 5
    event_webhook_url: str = ""
    event_webhook_method: str = "POST"
    event_webhook_timeout: float = 5
    event_webhook_events: str = ""
    event_webhook_headers: str = "{}"
    event_webhook_body: str = "{}"
    pre_restart_webhook_url: str = ""
    pre_restart_webhook_method: str = "POST"
    pre_restart_webhook_timeout: float = 10
    pre_restart_webhook_headers: str = "{}"
    pre_restart_webhook_body: str = "{}"
    pre_restart_webhook_failure_policy: str = "continue"
    post_recovery_webhook_url: str = ""
    post_recovery_webhook_method: str = "POST"
    post_recovery_webhook_timeout: float = 10
    post_recovery_webhook_headers: str = "{}"
    post_recovery_webhook_body: str = "{}"
    post_recovery_webhook_failure_policy: str = "continue"

    @classmethod
    def from_env(cls):
        warn_unknown_environment(os.environ)
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
        for name in ("vllm_base_url", "alert_webhook_url", *(prefix + "_url" for prefix in WEBHOOK_PREFIXES)):
            value = getattr(self, name)
            if not value and name != "vllm_base_url":
                continue
            try:
                url = urlsplit(value)
                _ = url.port
            except ValueError:
                raise ValueError(f"Invalid {name.upper()}") from None
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError(f"Invalid {name.upper()}")
        if self.recovery_mode == "docker" and self.docker_api_timeout <= self.docker_stop_timeout:
            raise ValueError("DOCKER_API_TIMEOUT must exceed DOCKER_STOP_TIMEOUT")
        for prefix in WEBHOOK_PREFIXES:
            if getattr(self, prefix + "_method") not in METHODS:
                raise ValueError(f"Invalid {prefix.upper()}_METHOD; use POST, PUT or PATCH")
            validate_headers(getattr(self, prefix + "_headers"), prefix.upper() + "_HEADERS")
            json_object(getattr(self, prefix + "_body"), prefix.upper() + "_BODY")
        event_names(self.event_webhook_events)
        for prefix in ("pre_restart_webhook", "post_recovery_webhook"):
            if getattr(self, prefix + "_failure_policy") not in {"continue", "abort"}:
                raise ValueError(f"Invalid {prefix.upper()}_FAILURE_POLICY")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid LOG_LEVEL")


    def effective(self):
        # Only numeric knobs and validated enum values are safe to emit.
        # Paths, URLs, model/container names, prompts, headers and bodies stay out.
        result = {field.name: getattr(self, field.name) for field in fields(self)
                  if isinstance(getattr(self, field.name), (int, float))}
        for name in ("recovery_mode", "log_level", "event_webhook_method",
                     "pre_restart_webhook_method", "post_recovery_webhook_method",
                     "pre_restart_webhook_failure_policy", "post_recovery_webhook_failure_policy"):
            result[name] = getattr(self, name)
        for name in ("vllm_api_key", "log_file", "alert_webhook_url", "event_webhook_url",
                     "pre_restart_webhook_url", "post_recovery_webhook_url"):
            result[name.removesuffix("_url") + "_configured"] = bool(getattr(self, name))
        return result


SUPPORTED_ENVIRONMENT = frozenset(field.name.upper() for field in fields(Config))
# Valid Compose/Docker inputs which are not parsed by Config.
EXTERNAL_ENVIRONMENT = {
    "VLLM_IMAGE", "DOCKER_SOCKET_GID", "DOCKER_HOST", "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH", "DOCKER_CONFIG", "DOCKER_API_VERSION",
}
APPLICATION_PREFIXES = (
    "LOG_", "STARTUP_GRACE_", "RECOVERY_", "RESTART_", "WATCHDOG_", "HEALTH_",
    "INFERENCE_", "PROBE_", "ALERT_WEBHOOK_", "EVENT_WEBHOOK_",
    "PRE_RESTART_WEBHOOK_", "POST_RECOVERY_WEBHOOK_",
)


def warn_unknown_environment(environment):
    for name in sorted(environment):
        if name in SUPPORTED_ENVIRONMENT or name in EXTERNAL_ENVIRONMENT:
            continue
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", name):
            continue
        matches = difflib.get_close_matches(name, sorted(SUPPORTED_ENVIRONMENT), n=1, cutoff=.86)
        if name.startswith(APPLICATION_PREFIXES) or matches:
            log("unknown environment variable", logging.WARNING, name=name,
                suggestion=matches[0] if matches else None)
