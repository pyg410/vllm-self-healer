from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WatchdogState(str, Enum):
    HEALTHY = "healthy"
    SUSPECT = "suspect"
    RESTARTING = "restarting"
    RECOVERING = "recovering"
    FAILED = "failed"


class FailureReason(str, Enum):
    HEALTH_TIMEOUT = "health_timeout"
    HEALTH_HTTP_ERROR = "health_http_error"
    HEALTH_CONNECTION_ERROR = "health_connection_error"
    INFERENCE_TIMEOUT = "inference_timeout"
    INFERENCE_HTTP_ERROR = "inference_http_error"
    INFERENCE_CONNECTION_ERROR = "inference_connection_error"
    INFERENCE_INVALID_RESPONSE = "inference_invalid_response"
    ALIVE_BUT_STALLED = "ALIVE_BUT_STALLED"
    RECOVERY_TIMEOUT = "recovery_timeout"
    DOCKER_ERROR = "docker_error"
    STATE_ERROR = "state_error"
    INTERNAL_ERROR = "internal_error"


class AlertEvent(str, Enum):
    RESTART_TRIGGERED = "RESTART_TRIGGERED"
    RECOVERY_SUCCESS = "RECOVERY_SUCCESS"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    MAX_RESTART_EXCEEDED = "MAX_RESTART_EXCEEDED"


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    reason: FailureReason | None = None
