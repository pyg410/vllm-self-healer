import logging
import time
from .logging_config import log, log_error
from .hooks import NoopHooks
from .state import RestartPolicy, StateError
from .recovery import RecoveryBackend, ImmediateRecovery, RecoveryFailure
from .types import FailedReason as F, RecoveryReason as C
from .types import AlertEvent as E, FailureReason as R, WatchdogState as S


class Controller:
    def __init__(self, config, health, inference, recovery, alert, store,
                 clock=time.monotonic, wall_clock=time.time, stopping=lambda: False, status=None, hooks=None, events=None):
        self.c, self.health, self.inference = config, health, inference
        self.recovery = recovery if isinstance(recovery, RecoveryBackend) else ImmediateRecovery(recovery)
        self.alert, self.store, self.status = alert, store, status
        self.clock, self.wall, self.stopping = clock, wall_clock, stopping
        self.hooks = hooks if hooks is not None else NoopHooks()
        self.events = events
        self.recovery_reason = C.STARTUP
        self.failed_reason = None
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
                self.recovery_reason = C.RESTORED_RECOVERY
                if old_state == S.FAILED:
                    self.failed_reason = F(saved.get("failed_reason") or F.UNKNOWN.value)
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
                log("persisted state restored", state=old_state.value,
                    backend=config.recovery_mode, effective_state=self.state.value,
                    recovery_reason=self.recovery_reason.value,
                    failed_reason=self.failed_reason.value if self.failed_reason else None,
                    recovery_ready_at=saved["recovery_ready_at"],
                    recovery_deadline=saved["recovery_deadline"],
                    restart_history_count=len(self.policy.history),
                    stored_recovery_timing_applied=old_state == S.RECOVERING)
                # RESTARTING means an ambiguous/interrupted Docker operation;
                # verify recovery first and retain its already reserved attempt.
            if self.state == S.FAILED:
                self.recovery.cancel()
            self.persist()
        except StateError as error:
            log_error("state restore failed", error, "state_restore")
            self.persistence_broken = True
            self.fail(R.STATE_ERROR, E.RECOVERY_FAILED)
        self.publish()
        log("watchdog started", state=self.state.value)
        if self.state == S.RECOVERING:
            log("recovery started", state=self.state.value, recovery_reason=self.recovery_reason.value, initial=True)

    def emit(self, event, reason):
        # Keep legacy ALERT_WEBHOOK_URL payloads and its original event set.
        if event != E.RESTART_CONFIRMED:
            self.alert.send(event, reason, len(self.policy.history))
        if self.events is not None:
            self.events.send(event, reason, len(self.policy.history),
                             recovery_reason=self.recovery_reason.value,
                             failed_reason=self.failed_reason.value if self.failed_reason else None)

    def persist(self):
        if self.persistence_broken:
            return
        self.store.save({
            "version": 1, "state": self.state.value,
            "recovery_reason": self.recovery_reason.value,
            "failed_reason": self.failed_reason.value if self.failed_reason else None,
            "backend": {} if self.state == S.SUSPECT else self.recovery.snapshot(),
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
            log("state transition", previous=old.value, state=new.value,
                recovery_reason=self.recovery_reason.value)
            self.publish()

    def fail(self, reason, event, failed_reason=None):
        self.failed_reason = failed_reason or {
            R.RECOVERY_TIMEOUT: F.RECOVERY_TIMEOUT, R.HOOK_FAILURE: F.HOOK_FAILURE,
            R.STATE_ERROR: F.STATE_ERROR, R.INTERNAL_ERROR: F.INTERNAL_ERROR,
        }.get(reason, F.BACKEND_FAILURE)
        self.recovery.cancel()
        self.transition(S.FAILED)
        log("automatic restart disabled", logging.ERROR, reason=reason.value,
            failed_reason=self.failed_reason.value, state=self.state.value)
        try:
            self.persist()
        except StateError:
            self.persistence_broken = True
            log("state persistence failed", logging.ERROR)
        self.emit(event, reason.value)

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
        # Early readiness does not remove the original grace protection.
        if self.clock() < self.ready:
            return
        now = self.wall()
        if self.policy.limit_reached(now, self.recovery_attempts):
            log("restart budget exceeded", logging.ERROR)
            self.fail(reason, E.MAX_RESTART_EXCEEDED, F.RESTART_BUDGET_EXHAUSTED)
            return
        if self.policy.cooling_down(now):
            log("restart cooldown active", logging.DEBUG, state=self.state.value)
            return
        # Reserve in SUSPECT before running external actions. A crash during a
        # PRE hook must not restore an armed Kubernetes request without approval.
        self.transition(S.SUSPECT)
        self.policy.history.append(now)
        self.recovery_attempts += 1
        # Reserve before side effect, including ambiguous timeout/daemon failures.
        try:
            self.persist()
            self.recovery.prepare()
        except StateError:
            self.persistence_broken = True
            self.fail(R.STATE_ERROR, E.RECOVERY_FAILED)
            return
        except Exception as error:
            self.fail(self.recovery.error_reason, E.RECOVERY_FAILED,
                      error.failed_reason if isinstance(error, RecoveryFailure) else F.BACKEND_FAILURE)
            return
        log("restart triggered", logging.WARNING, state=self.state.value, reason=reason.value,
            health_failure_count=self.health_failures, inference_failure_count=self.inference_failures,
            last_successful_inference=self.last_successful_inference,
            restart_count=len(self.policy.history))
        self.emit(E.RESTART_TRIGGERED, reason.value)
        if self.stopping():
            return
        if not self.hooks.pre_restart(reason.value, len(self.policy.history)):
            self.fail(R.HOOK_FAILURE, E.RECOVERY_FAILED)
            return
        if self.stopping():
            return
        try:
            # Start the Kubernetes confirmation timeout after hooks finish.
            self.recovery.finalize_request()
            self.transition(S.RESTARTING)
            self.persist()
        except StateError:
            raise
        except Exception as error:
            self.fail(self.recovery.error_reason, E.RECOVERY_FAILED,
                      error.failed_reason if isinstance(error, RecoveryFailure) else F.BACKEND_FAILURE)
            return
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
            self.emit(E.RECOVERY_FAILED, self.recovery.error_reason.value)
        else:
            self.emit(E.RESTART_CONFIRMED, "backend_restart_completed")
        # A timed-out Docker call may have succeeded server-side. Always verify.
        self.begin_recovery()

    def begin_recovery(self):
        self.recovery_reason = C.POST_RESTART
        self.transition(S.RECOVERING)
        self.ready = self.clock() + self.c.startup_grace_period
        self.expires = self.ready + self.c.recovery_timeout
        log("recovery started", state=self.state.value, recovery_reason=self.recovery_reason.value)
        self.persist()

    def step(self):
        """One bounded iteration; scheduling and signal handling live in main."""
        if self.state == S.FAILED or self.stopping():
            return
        try:
            if self.state == S.RESTARTING:
                try:
                    completed = self.recovery.poll()
                except Exception as error:
                    self.fail(self.recovery.error_reason, E.RECOVERY_FAILED,
                              error.failed_reason if isinstance(error, RecoveryFailure) else F.BACKEND_FAILURE)
                    return
                if completed:
                    self.emit(E.RESTART_CONFIRMED, "backend_restart_completed")
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
        if self.clock() < self.expires:
            result = self.probes(recovery=True)
            if self.stopping():
                return
            if result and result[0] and self.clock() < self.expires:
                if not self.hooks.post_recovery("probes_successful", len(self.policy.history)):
                    self.fail(R.HOOK_FAILURE, E.RECOVERY_FAILED)
                    return
                if self.stopping():
                    return
                self.transition(S.HEALTHY)
                self.failed_reason = None
                self.recovery_attempts = 0
                self.health_failures = self.inference_failures = 0
                self.persist()
                log("recovery successful", state=self.state.value, recovery_reason=self.recovery_reason.value)
                self.emit(E.RECOVERY_SUCCESS, "probes_successful")
                return
        if self.clock() >= self.expires:
            log("recovery failed", logging.ERROR, reason=R.RECOVERY_TIMEOUT.value)
            self.emit(E.RECOVERY_FAILED, R.RECOVERY_TIMEOUT.value)
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
