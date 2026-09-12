import logging
from datetime import datetime, timezone
from .logging_config import log


class Alert:
    def __init__(self, config, client):
        self.config, self.client = config, client

    def send(self, event, reason, restart_count):
        c = self.config
        if not c.alert_webhook_url:
            return
        try:
            status, _ = self.client.request("POST", c.alert_webhook_url, timeout=c.alert_timeout,
                json_body={"service": "vllm-watchdog", "container": c.vllm_container_name,
                           "event": event.value, "reason": reason, "restart_count": restart_count,
                           "timestamp": datetime.now(timezone.utc).isoformat()})
            if not 200 <= status < 300:
                raise ValueError("Webhook HTTP error")
            log("alert sent", alert_event=event.value)
        except Exception:
            # Never log exception strings, URLs, headers, or response bodies.
            log("alert failed", logging.WARNING, alert_event=event.value)
