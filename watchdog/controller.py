import logging
import time
from .logging_config import log
from .state import RestartPolicy, StateError
from .recovery import RecoveryBackend, ImmediateRecovery
from .types import AlertEvent as E, FailureReason as R, WatchdogState as S


class Controller:
    def __init__(self, config, health, inference, recovery, alert, store,
                 clock=time.monotonic, wall_clock=time.time, stopping=lambda: False, status=None):
        self.c, self.health, self.inference = config, health, inference
        self.recovery = recovery if isinstance(recovery, RecoveryBackend) else ImmediateRecovery(recovery)
        self.alert, self.store, self.status = alert, store, status
        self.clock, self.wall, self.stopping = clock, wall_clock, stopping
        self.state = S.RECOVERING
        self.health_failures = self.inference_failures = 0
        self.last_successful_inference = None
        self.recovery_attempts = 0
        self.policy = RestartPolicy(config)
        self.ready = self.clock() + config.startup_grace_period
        self.expires = self.ready + config.recovery_timeout
        self.persistence_broken = False
        try:
            saved = store.load()
            if saved:
                self.policy = RestartPolicy(config, saved["restart_history"])
                self.recovery_attempts = saved["recovery_attempts"]
                self.last_successful_inference = saved["last_successful_inference"]
                old_state = S(saved["state"])
                try:
                    self.recovery.restore(saved.get("backend", {}))
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise StateError("Invalid backend state") from None
                if old_state == S.FAILED:
                    self.state = S.FAILED
                elif old_state == S.RESTARTING and saved.get("backend"):
                    self.state = S.RESTARTING
                elif old_state == S.RECOVERING:
                    self.ready = self.clock() + max(0, saved["recovery_ready_at"] - self.wall())
                    self.expires = self.clock() + max(0, saved["recovery_deadline"] - self.wall())
                # RESTARTING means an ambiguous/interrupted Docker operation;
                # verify recovery first and retain its already reserved attempt.
            if self.state == S.FAILED:
                self.recovery.cancel()
            self.persist()
        except StateError:
            self.persistence_broken = True
            self.fail(R.STATE_ERROR, E.RECOVERY_FAILED)
        self.publish()
        log("watchdog started", state=self.state.value)
        if self.state == S.RECOVERING:
            log("recovery started", state=self.state.value, initial=True)

    def persist(self):
        if self.persistence_broken:
            return
        self.store.save({
            "version": 1, "state": self.state.value,
            "backend": self.recovery.snapshot(),
            "restart_history": list(self.policy.history),
            "recovery_attempts": self.recovery_attempts,
            "last_successful_inference": self.last_successful_inference,
            "recovery_ready_at": self.wall() + max(0, self.ready - self.clock()),
            "recovery_deadline": self.wall() + max(0, self.expires - self.clock()),
        })

    def publish(self):
        if self.status is not None:
            self.status.publish(self.state)

    def transition(self, new):
        if self.state != new:
            old, self.state = self.state, new
            log("state transition", previous=old.value, state=new.value)
            self.publish()

    def fail(self, reason, event):
        self.recovery.cancel()
        self.transition(S.FAILED)
        log("automatic restart disabled", logging.ERROR, reason=reason.value, state=self.state.value)
        try:
            self.persist()
        except StateError:
            self.persistence_broken = True
            log("state persistence failed", logging.ERROR)
        self.alert.send(event, reason.value, len(self.policy.history))

    def probes(self, recovery=False):
        remaining = lambda maximum: min(maximum, max(0.001, self.expires - self.clock())) if recovery else maximum
        health = self.health.check(timeout=remaining(self.c.health_timeout))
        if self.stopping():
            return None
        if recovery and self.clock() >= self.expires:
            return None
        inference = self.inference.check(timeout=remaining(self.c.inference_timeout))
        self.health_failures = 0 if health.ok else self.health_failures + 1
        self.inference_failures = 0 if inference.ok else self.inference_failures + 1
        if inference.ok:
            self.last_successful_inference = self.wall()
        for name, result in (("health check", health), ("inference probe", inference)):
            log(name + (" success" if result.ok else " failure"),
                logging.DEBUG if result.ok else logging.WARNING,
                state=self.state.value, reason=result.reason.value if result.reason else None)
        reason = R.ALIVE_BUT_STALLED if health.ok and not inference.ok else (inference.reason or health.reason)
        log("probe result", logging.DEBUG if health.ok and inference.ok else logging.WARNING,
            state=self.state.value, health=health.ok, inference=inference.ok,
            health_failure_count=self.health_failures, inference_failure_count=self.inference_failures,
            reason=reason.value if reason else None)
        return health.ok and inference.ok, reason

    def restart(self, reason):
        if self.stopping():
            return
        now = self.wall()
        if self.policy.limit_reached(now, self.recovery_attempts):
            log("restart budget exceeded", logging.ERROR)
            self.fail(reason, E.MAX_RESTART_EXCEEDED)
            return
        if self.policy.cooling_down(now):
            log("restart cooldown active", logging.DEBUG, state=self.state.value)
            return
        self.transition(S.RESTARTING)
        self.policy.history.append(now)
        self.recovery_attempts += 1
        # Reserve before side effect, including ambiguous timeout/daemon failures.
        try:
            self.recovery.prepare()
            self.persist()
        except StateError:
            self.persistence_broken = True
            self.fail(R.STATE_ERROR, E.RECOVERY_FAILED)
            return
        except Exception:
            self.fail(self.recovery.error_reason, E.RECOVERY_FAILED)
            return
        log("restart triggered", logging.WARNING, state=self.state.value, reason=reason.value,
            health_failure_count=self.health_failures, inference_failure_count=self.inference_failures,
            last_successful_inference=self.last_successful_inference,
            restart_count=len(self.policy.history))
        self.alert.send(E.RESTART_TRIGGERED, reason.value, len(self.policy.history))
        if self.stopping():
            return
        try:
            if self.recovery.restart() is False:
                self.persist()
                return
            log("restart completed", state=self.state.value)
        except StateError:
            raise
        except Exception:
            event = "docker restart failed" if self.recovery.error_reason == R.DOCKER_ERROR else "recovery request failed"
            log(event, logging.ERROR, reason=self.recovery.error_reason.value)
            self.alert.send(E.RECOVERY_FAILED, self.recovery.error_reason.value, len(self.policy.history))
        # A timed-out Docker call may have succeeded server-side. Always verify.
        self.begin_recovery()

    def begin_recovery(self):
        self.transition(S.RECOVERING)
        self.ready = self.clock() + self.c.startup_grace_period
        self.expires = self.ready + self.c.recovery_timeout
        log("recovery started", state=self.state.value)
        self.persist()

    def step(self):
        """One bounded iteration; scheduling and signal handling live in main."""
        if self.state == S.FAILED or self.stopping():
            return
        try:
            if self.state == S.RESTARTING:
                try:
                    completed = self.recovery.poll()
                except Exception:
                    self.fail(self.recovery.error_reason, E.RECOVERY_FAILED)
                    return
                if completed:
                    self.begin_recovery()
            elif self.state == S.RECOVERING:
                self.recover()
            else:
                result = self.probes()
                if result is None or self.stopping():
                    return
                ok, reason = result
                self.transition(S.HEALTHY if ok else S.SUSPECT)
                if max(self.health_failures, self.inference_failures) >= self.c.failure_threshold:
                    self.restart(reason)
                self.persist()
        except StateError:
            self.persistence_broken = True
            self.fail(R.STATE_ERROR, E.RECOVERY_FAILED)
        except Exception:
            # A watchdog bug must not become an uncontrolled Docker restart loop.
            self.fail(R.INTERNAL_ERROR, E.RECOVERY_FAILED)

    def recover(self):
        if self.clock() < self.ready:
            return
        if self.clock() < self.expires:
            result = self.probes(recovery=True)
            if self.stopping():
                return
            if result and result[0] and self.clock() < self.expires:
                self.transition(S.HEALTHY)
                self.recovery_attempts = 0
                self.health_failures = self.inference_failures = 0
                self.persist()
                log("recovery successful", state=self.state.value)
                self.alert.send(E.RECOVERY_SUCCESS, "probes_successful", len(self.policy.history))
                return
        if self.clock() >= self.expires:
            log("recovery failed", logging.ERROR, reason=R.RECOVERY_TIMEOUT.value)
            self.alert.send(E.RECOVERY_FAILED, R.RECOVERY_TIMEOUT.value, len(self.policy.history))
            # Move to SUSPECT so cooldown does not repeat recovery alerts.
            self.transition(S.SUSPECT)
            self.health_failures = self.inference_failures = self.c.failure_threshold
            self.restart(R.RECOVERY_TIMEOUT)
        self.persist()

    def delay(self):
        if self.state == S.RESTARTING:
            return min(1.0, self.c.recovery_check_interval)
        if self.state == S.RECOVERING:
            target = self.ready if self.clock() < self.ready else self.expires
            return max(0.001, min(self.c.recovery_check_interval, target - self.clock()))
        return self.c.check_interval
