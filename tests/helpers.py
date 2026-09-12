from dataclasses import replace
from unittest.mock import Mock
from watchdog.config import Config
from watchdog.types import ProbeResult, FailureReason as R

OK = ProbeResult(True)
BAD = ProbeResult(False, R.INFERENCE_TIMEOUT)
HEALTH_BAD = ProbeResult(False, R.HEALTH_TIMEOUT)


def config(**kwargs):
    return replace(Config(vllm_model="test"), **kwargs)


def client(status=200, body=None, error=None):
    mock = Mock()
    mock.request.return_value = (status, body)
    if error:
        mock.request.side_effect = error
    return mock


def completion(content="x"):
    return {"object": "chat.completion", "choices": [
        {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "length"}]}
