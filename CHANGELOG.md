# Changelog

## v0.2.0

- Probe during startup and post-restart grace, allowing early readiness while retaining restart protection and the full recovery deadline.
- Expose recovery context and persist terminal FAILED reasons with backward-compatible version 1 state loading.
- Add validated LOG_TIMEZONE with UTC defaults and timezone data in core dependencies.
- Diagnose Docker daemon and target access at startup without mutations or fatal handling of temporary outages.
- Offer core-only dependencies and a Kubernetes image build option without the Docker SDK.
- Document migration, early recovery semantics, failure codes and dependency choices in English and Korean.

## v0.1.1

- Log effective non-sensitive settings, likely environment typos, safe startup errors, and restored recovery timing.
- Add optional rotating JSON file logs while retaining stdout.
- Add filtered informational event webhooks and restart-confirmed events without changing legacy alerts.
- Add generic PRE_RESTART and POST_RECOVERY HTTP hooks with explicit continue/abort semantics.
- Preserve Kubernetes start-ID safety across slow or interrupted PRE hooks.
- Document logging alternatives, timing/state reset, trusted integrations and detection/recovery limits.
- Keep grace behavior, UTC timestamps, restart budgets, and version 1 state compatibility unchanged.

## v0.1.0

- Add Kubernetes sidecar recovery using kubelet liveness and readiness probes.
- Share detection, cooldown, budgets, persistence and recovery verification across Docker and Kubernetes backends.
- Confirm vLLM launches using an atomic shared startup UUID; persist pending requests before exposing liveness failure.
- Add a bounded confirmation timeout and stop signaling in FAILED.
- Provide a Kubernetes Deployment/Service example without Docker socket or Kubernetes API permissions.
- Preserve Docker defaults, existing environment variables, and version 1 state files.
