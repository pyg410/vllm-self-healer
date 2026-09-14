import logging
from .http_client import deadline
from .logging_config import log
from .recovery import RecoveryBackend
from .types import FailureReason


def docker_factory(**kwargs):
    # Kubernetes-only installations never import or require the Docker SDK.
    import docker
    return docker.from_env(**kwargs)


class DockerManager(RecoveryBackend):
    error_reason = FailureReason.DOCKER_ERROR

    def __init__(self, config, factory=docker_factory):
        self.config, self.factory = config, factory

    def diagnose(self):
        # The SDK supports Unix sockets and remote/TLS daemons. Do not assume
        # a local socket path; actual connection checks also validate access.
        stage = "daemon_connection"
        try:
            with deadline(self.config.docker_api_timeout):
                client = self.factory(timeout=self.config.docker_api_timeout)
                try:
                    stage = "daemon_ping"
                    if not client.ping():
                        raise ConnectionError()
                    stage = "target_lookup"
                    client.containers.get(self.config.vllm_container_name)
                finally:
                    client.close()
        except Exception as error:
            log("docker startup diagnostic failed", logging.WARNING,
                stage=stage, error_type=type(error).__name__)
            return False
        log("docker startup diagnostic passed")
        return True

    def restart(self):
        c = self.config
        with deadline(c.docker_api_timeout):
            client = self.factory(timeout=c.docker_api_timeout)
            try:
                client.containers.get(c.vllm_container_name).restart(timeout=c.docker_stop_timeout)
            finally:
                client.close()
