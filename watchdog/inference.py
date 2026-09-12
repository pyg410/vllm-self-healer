import requests
from .http_client import DeadlineExceeded
from .types import FailureReason as R, ProbeResult


def valid_completion(body):
    if not isinstance(body, dict) or body.get("object") != "chat.completion":
        return False
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    for choice in choices:
        if not isinstance(choice, dict) or type(choice.get("index")) is not int:
            continue
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        # Empty content is legitimate for immediate EOS or reasoning models
        # with a one-token budget, but requires a terminal completion structure.
        if choice.get("finish_reason") in {"stop", "length"} and "content" in message and isinstance(message["content"], (str, type(None))):
            return True
    return False


class InferenceProbe:
    def __init__(self, config, client):
        self.config, self.client = config, client

    def check(self, timeout=None):
        c = self.config
        headers = {"Authorization": f"Bearer {c.vllm_api_key}"} if c.vllm_api_key else {}
        try:
            status, body = self.client.request(
                "POST", c.vllm_base_url.rstrip("/") + "/v1/chat/completions",
                timeout=timeout or c.inference_timeout, headers=headers, parse_json=True,
                json_body={"model": c.vllm_model, "messages": [{"role": "user", "content": c.probe_prompt}],
                           "max_tokens": c.probe_max_tokens, "temperature": 0, "stream": False})
            if status != 200:
                return ProbeResult(False, R.INFERENCE_HTTP_ERROR)
            return ProbeResult(True) if valid_completion(body) else ProbeResult(False, R.INFERENCE_INVALID_RESPONSE)
        except (requests.Timeout, DeadlineExceeded):
            return ProbeResult(False, R.INFERENCE_TIMEOUT)
        except requests.RequestException:
            return ProbeResult(False, R.INFERENCE_CONNECTION_ERROR)
        except (ValueError, TypeError, RecursionError):
            return ProbeResult(False, R.INFERENCE_INVALID_RESPONSE)
