"""Lifecycle actions are separate from informational event notifications."""
from .logging_config import log
from .webhooks import Webhook, metadata


class NoopHooks:
    def pre_restart(self, reason, restart_count):
        return True

    def post_recovery(self, reason, restart_count):
        return True


class LifecycleHooks:
    def __init__(self, config, client):
        self.config = config
        self.pre = Webhook(config, client, "pre_restart_webhook")
        self.post = Webhook(config, client, "post_recovery_webhook")

    def _run(self, webhook, event, reason, restart_count, policy):
        succeeded = webhook.send(metadata(self.config, event, reason, restart_count))
        allowed = succeeded or policy == "continue"
        if not succeeded:
            log("lifecycle hook policy applied", hook=event, failure_policy=policy,
                recovery_allowed=allowed)
        return allowed

    def pre_restart(self, reason, restart_count):
        return self._run(self.pre, "PRE_RESTART", reason, restart_count,
                         self.config.pre_restart_webhook_failure_policy)

    def post_recovery(self, reason, restart_count):
        return self._run(self.post, "POST_RECOVERY", reason, restart_count,
                         self.config.post_recovery_webhook_failure_policy)
