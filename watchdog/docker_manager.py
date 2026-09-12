import docker
from .http_client import deadline


class DockerManager:
    def __init__(self, config, factory=docker.from_env):
        self.config, self.factory = config, factory

    def restart(self):
        c = self.config
        # Lazy connection: daemon outages do not crash watchdog startup.
        with deadline(c.docker_api_timeout):
            client = self.factory(timeout=c.docker_api_timeout)
            try:
                client.containers.get(c.vllm_container_name).restart(timeout=c.docker_stop_timeout)
            finally:
                client.close()
