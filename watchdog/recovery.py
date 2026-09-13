"""Recovery contracts; the controller does not depend on an orchestrator API."""
from .types import FailureReason


class RecoveryBackend:
    error_reason = FailureReason.RECOVERY_ERROR

    def prepare(self):
        """Reserve backend state before the controller persists its attempt."""

    def restart(self):
        """Return False when asynchronous restart confirmation is required."""
        raise NotImplementedError

    def poll(self):
        """Return True only after the requested restart has been observed."""
        return True

    def snapshot(self):
        return {}

    def restore(self, data):
        if data:
            raise ValueError("Recovery backend state is incompatible")

    def cancel(self):
        """Disable further recovery requests."""


class ImmediateRecovery(RecoveryBackend):
    """Compatibility adapter for existing restart-only dependencies."""
    error_reason = FailureReason.DOCKER_ERROR

    def __init__(self, target):
        self.target = target

    def restart(self):
        self.target.restart()
        return True


def create_recovery(config):
    if config.recovery_mode == "docker":
        from .docker_manager import DockerManager
        return DockerManager(config)
    from .kubernetes_recovery import KubernetesRecovery
    return KubernetesRecovery(config)
