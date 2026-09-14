"""Recovery contracts; the controller does not depend on an orchestrator API."""
from .types import FailureReason


class RecoveryFailure(Exception):
    def __init__(self, failed_reason):
        self.failed_reason = failed_reason
        super().__init__(failed_reason.value)


class RecoveryBackend:
    error_reason = FailureReason.RECOVERY_ERROR

    def diagnose(self):
        """Optional read-only startup check; never requests recovery."""

    def prepare(self):
        """Reserve backend state before the controller persists its attempt."""

    def finalize_request(self):
        """Finalize confirmation timing after lifecycle hooks, before persistence."""

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
