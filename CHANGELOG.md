# Changelog

## v0.1.0

- Add Kubernetes sidecar recovery using kubelet liveness and readiness probes.
- Share detection, cooldown, budgets, persistence and recovery verification across Docker and Kubernetes backends.
- Confirm vLLM launches using an atomic shared startup UUID; persist pending requests before exposing liveness failure.
- Add a bounded confirmation timeout and stop signaling in FAILED.
- Provide a Kubernetes Deployment/Service example without Docker socket or Kubernetes API permissions.
- Preserve Docker defaults, existing environment variables, and version 1 state files.
