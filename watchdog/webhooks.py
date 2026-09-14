"""Optional outbound HTTP integrations; no response bodies or credentials logged."""
from datetime import datetime, timezone
import json
import logging
import re

import requests

from .http_client import DeadlineExceeded
from .logging_config import log
from .types import AlertEvent

WEBHOOK_PREFIXES = ("event_webhook", "pre_restart_webhook", "post_recovery_webhook")
METHODS = {"POST", "PUT", "PATCH"}


def json_object(value, name):
    def invalid_constant(_):
        raise ValueError()
    try:
        result = json.loads(value, parse_constant=invalid_constant)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, RecursionError):
        raise ValueError(f"{name} must be a JSON object") from None


def validate_headers(value, name):
    headers = json_object(value, name)
    for key, item in headers.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or not isinstance(item, str):
            raise ValueError(f"Invalid {name}")
        if "\r" in item or "\n" in item or item[:1].isspace():
            raise ValueError(f"Invalid {name}")
        try:
            item.encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError(f"Invalid {name}") from None
    return headers


def event_names(value):
    names = {item.strip() for item in value.split(",") if item.strip()}
    if not names <= {event.value for event in AlertEvent}:
        raise ValueError("Invalid EVENT_WEBHOOK_EVENTS")
    return names


class Webhook:
    def __init__(self, config, client, prefix):
        self.client = client
        self.url = getattr(config, prefix + "_url")
        self.method = getattr(config, prefix + "_method")
        self.timeout = getattr(config, prefix + "_timeout")
        self.headers = validate_headers(getattr(config, prefix + "_headers"), prefix.upper() + "_HEADERS")
        self.body = json_object(getattr(config, prefix + "_body"), prefix.upper() + "_BODY")
        self.name = prefix

    def send(self, metadata):
        if not self.url:
            return True
        try:
            status, _ = self.client.request(
                self.method, self.url, timeout=self.timeout, headers=self.headers,
                json_body={**self.body, **metadata})
            if not 200 <= status < 300:
                log("webhook failed", logging.WARNING, integration=self.name,
                    http_status=status, error_type="HTTPError")
                return False
        except (requests.RequestException, DeadlineExceeded, ValueError, TypeError, OSError) as error:
            log("webhook failed", logging.WARNING, integration=self.name,
                error_type=type(error).__name__)
            return False
        log("webhook sent", integration=self.name, lifecycle_event=metadata["event"])
        return True


def metadata(config, event, reason, restart_count):
    return {
        "service": "vllm-watchdog",
        "container": config.vllm_container_name,
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "recovery_mode": config.recovery_mode,
        "reason": reason,
        "restart_count": restart_count,
    }


class EventWebhook:
    def __init__(self, config, client):
        self.config = config
        self.webhook = Webhook(config, client, "event_webhook")
        self.events = event_names(config.event_webhook_events)

    def send(self, event, reason, restart_count):
        if self.events and event.value not in self.events:
            return
        self.webhook.send(metadata(self.config, event.value, reason, restart_count))
