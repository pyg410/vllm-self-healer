import unittest
import requests
from watchdog.inference import InferenceProbe
from watchdog.types import FailureReason as R
from .helpers import config, client, completion


class InferenceTests(unittest.TestCase):
    def test_valid(self):
        for content in ("x", "", None):
            self.assertTrue(InferenceProbe(config(), client(body=completion(content))).check().ok)

    def test_invalid(self):
        for body in (None, [], {}, {"choices": []}, {"object": "chat.completion", "choices": [None]},
                     {"object": "chat.completion", "choices": [{"index": 0, "message": {}}]}):
            with self.subTest(body=body):
                self.assertEqual(InferenceProbe(config(), client(body=body)).check().reason, R.INFERENCE_INVALID_RESPONSE)

    def test_errors(self):
        for error, reason in [(requests.Timeout(), R.INFERENCE_TIMEOUT),
                              (requests.ConnectionError(), R.INFERENCE_CONNECTION_ERROR),
                              (ValueError(), R.INFERENCE_INVALID_RESPONSE)]:
            self.assertEqual(InferenceProbe(config(), client(error=error)).check().reason, reason)
        self.assertEqual(InferenceProbe(config(), client(500)).check().reason, R.INFERENCE_HTTP_ERROR)

    def test_request(self):
        http = client(body=completion())
        InferenceProbe(config(probe_prompt="synthetic", probe_max_tokens=2, vllm_api_key="secret"), http).check()
        args = http.request.call_args.kwargs
        self.assertEqual(args["json_body"]["max_tokens"], 2)
        self.assertEqual(args["json_body"]["messages"][0]["content"], "synthetic")
        self.assertEqual(args["timeout"], 15)
        self.assertEqual(args["headers"]["Authorization"], "Bearer secret")
