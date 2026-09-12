import requests
from .http_client import DeadlineExceeded
from .types import FailureReason as R, ProbeResult


class HealthProbe:
    def __init__(self, config, client):
        self.config, self.client = config, client

    def check(self, timeout=None):
        c = self.config
        headers = {"Authorization": f"Bearer {c.vllm_api_key}"} if c.vllm_api_key else {}
        try:
            status, _ = self.client.request("GET", c.vllm_base_url.rstrip("/") + "/health",
                                            timeout=timeout or c.health_timeout, headers=headers)
            return ProbeResult(status == 200, None if status == 200 else R.HEALTH_HTTP_ERROR)
        except (requests.Timeout, DeadlineExceeded):
            return ProbeResult(False, R.HEALTH_TIMEOUT)
        except requests.RequestException:
            return ProbeResult(False, R.HEALTH_CONNECTION_ERROR)
