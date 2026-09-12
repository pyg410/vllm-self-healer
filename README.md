# vLLM Self-Healer

**English** | [한국어](README.ko.md)

> Detect **alive-but-stalled vLLM instances** and automatically recover them.

**vLLM Self-Healer** is a lightweight external watchdog for vLLM deployments running with Docker.

It detects a failure mode that ordinary container health monitoring can miss:

```text
Container       running
vLLM process    alive
GET /health     200 OK
Inference       stalled / timeout
```

Instead of relying only on process or HTTP liveness, vLLM Self-Healer verifies **actual inference forward progress** using a small synthetic generation request.

When repeated failures are detected, it can:

* restart the affected vLLM container
* verify that inference really recovered
* prevent infinite restart loops
* persist restart history across watchdog restarts
* optionally send webhook alerts

The watchdog itself does not require a GPU.

---

## Why?

Docker restart policies are useful when a process exits.

They cannot recover a service that is still alive but no longer making useful progress.

The same problem can occur with vLLM:

```text
HTTP API
   ↓
EngineCore
   ↓
Scheduler
   ↓
ModelRunner
   ↓
CUDA / NCCL
   ↓
GPU
```

The HTTP server may remain responsive even when something deeper in this path has stalled.

This type of failure has also been reported upstream in vLLM.

Examples include:

| Upstream case               | Symptom                                                                                                  |
| --------------------------- | -------------------------------------------------------------------------------------------------------- |
| vllm-project/vllm #52319    | Generation stops completely while `/health` and `/metrics` continue returning HTTP 200                   |
| vllm-project/vllm #52247    | EngineCore remains alive while blocked on a GPU synchronization event; `/health` continues returning 200 |
| vllm-project/vllm #42897    | Token generation stops under sustained traffic while the HTTP layer remains responsive                   |
| vllm-project/vllm #36960    | Proposal for GPU-aware readiness because process liveness alone cannot prove inference availability      |
| vllm-project/vllm PR #36451 | Adds EngineCore forward-progress detection specifically for an alive-but-hung state                      |

For example, upstream issue #52247 describes a production incident where a GPU kernel never terminated, EngineCore remained alive, `/health` continued returning 200, and affected instances served no inference for hours.

That is the gap this project is designed to cover externally.

Instead of asking only:

```text
Is the process alive?
```

vLLM Self-Healer also asks:

```text
Can this instance actually complete an inference request?
```

---

## How it works

Each monitoring cycle runs two independent probes.

### 1. Health probe

```http
GET /health
```

Checks basic vLLM engine health.

### 2. Synthetic inference probe

```http
POST /v1/chat/completions
```

with a minimal request similar to:

```json
{
  "model": "your-model",
  "messages": [
    {
      "role": "user",
      "content": "ping"
    }
  ],
  "max_tokens": 1,
  "temperature": 0
}
```

This verifies significantly more than HTTP availability.

A successful inference probe exercises the path through:

```text
HTTP request
    ↓
request processing
    ↓
scheduler
    ↓
model execution
    ↓
GPU
    ↓
decode
    ↓
completed response
```

The health and inference probes maintain **independent failure counters**.

A successful `/health` response therefore does not clear an inference failure.

That distinction is critical for detecting an alive-but-stalled instance.

---

## State machine

```text
                  ┌─────────────┐
                  │   HEALTHY   │
                  └──────┬──────┘
                         │
                    probe failure
                         │
                         ▼
                  ┌─────────────┐
                  │   SUSPECT   │
                  └──────┬──────┘
                         │
              consecutive failures
                         │
                         ▼
                 ┌──────────────┐
                 │  RESTARTING  │
                 └──────┬───────┘
                        │
                  Docker restart
                        │
                        ▼
                 ┌──────────────┐
                 │  RECOVERING  │
                 └──────┬───────┘
                        │
               health + inference
                    ┌───┴───┐
                    │       │
                  success   timeout
                    │       │
                    ▼       ▼
                 HEALTHY   retry policy
                              │
                        restart limit
                              │
                              ▼
                           FAILED
```

### HEALTHY

Both probes are succeeding.

### SUSPECT

At least one probe is failing, but the configured failure threshold has not yet been reached.

### RESTARTING

The watchdog records the restart attempt and asks Docker to restart the target vLLM container.

### RECOVERING

After the configured startup grace period, both probes are executed repeatedly.

Recovery succeeds only when:

```text
health probe    = success
inference probe = success
```

in the same recovery iteration.

### FAILED

Automatic recovery is stopped after the restart budget is exhausted or when persistent state cannot be safely maintained.

`FAILED` is persisted intentionally.

Restarting the watchdog itself does not silently reset the recovery budget.

---

## Alive-but-stalled detection

One particularly important state is:

```text
health     = success
inference  = failure
```

The watchdog reports this as:

```text
ALIVE_BUT_STALLED
```

Example:

```text
03:21:01  health probe       success
03:21:05  inference probe    timeout

03:21:31  health probe       success
03:21:35  inference probe    timeout

03:22:01  health probe       success
03:22:05  inference probe    timeout

failure threshold reached

03:22:05  ALIVE_BUT_STALLED
03:22:05  RESTART_TRIGGERED
```

After the restart:

```text
RECOVERING

health     success
inference  success

RECOVERY_SUCCESS
HEALTHY
```

---

## Restart-loop protection

Blind restart loops can make an incident worse.

vLLM Self-Healer therefore uses bounded recovery.

It tracks restart timestamps and limits how many restart attempts may occur within a configured window.

For example:

```text
MAX_RESTARTS=3
RESTART_WINDOW=600
```

allows at most three restart attempts within ten minutes.

The final permitted restart still receives a normal recovery check.

If recovery fails and another restart would be required, the watchdog enters:

```text
FAILED
```

instead of restarting forever.

A separate restart cooldown prevents repeated restart calls in rapid succession.

---

## Persistent state

Restart history is stored in:

```text
/data/watchdog_state.json
```

by default.

State is persisted using atomic file replacement and `fsync`.

Restart attempts are recorded **before** the Docker restart call.

This matters because a Docker API timeout does not necessarily mean that the server-side restart failed.

After such an uncertain result, the watchdog verifies recovery before attempting another restart.

State corruption or persistence failures prevent automatic restarts rather than silently discarding the recovery history.

To intentionally reset a latched `FAILED` state:

1. stop the watchdog
2. resolve the underlying problem
3. back up the state file if needed
4. delete only the watchdog state JSON
5. start the watchdog again

This resets the restart budget.

---

## Features

* Detects **alive-but-stalled vLLM instances**
* `/health` probing
* Real synthetic inference probing
* Independent health/inference failure counters
* Consecutive-failure thresholds
* Automatic Docker container restart
* Startup grace period
* Post-restart recovery verification
* Restart cooldown
* Rolling restart budget
* Restart-loop protection
* Persistent recovery state
* Optional webhook alerts
* Structured JSON logs
* Graceful SIGTERM / SIGINT handling
* Non-root container
* Read-only-container friendly
* `linux/amd64`
* `linux/arm64`
* No GPU dependency for the watchdog itself

---

# Quick Start

## Pull the image

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:latest
```

For production deployments, prefer a released version:

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

or pin the image digest for fully reproducible deployment.

---

## Docker Compose

Add the watchdog next to your existing vLLM service.

```yaml
services:

  vllm:
    # your existing vLLM configuration

  vllm-self-healer:
    image: ghcr.io/pyg410/vllm-self-healer:v0.1.0
    restart: unless-stopped

    environment:
      VLLM_BASE_URL: http://vllm:8000
      VLLM_CONTAINER_NAME: vllm
      VLLM_MODEL: ${VLLM_MODEL}

      VLLM_API_KEY: ${VLLM_API_KEY:-}

      STATE_FILE: /data/watchdog_state.json

    group_add:
      - "${DOCKER_SOCKET_GID}"

    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - watchdog-data:/data

    read_only: true

    security_opt:
      - no-new-privileges:true

    cap_drop:
      - ALL

    stop_grace_period: 90s

volumes:
  watchdog-data:
```

Get the Docker socket group ID:

```bash
stat -c '%g' /var/run/docker.sock
```

Then start:

```bash
docker compose up -d vllm-self-healer
```

View logs:

```bash
docker compose logs -f vllm-self-healer
```

Both containers must be able to communicate over the same Docker network.

`VLLM_CONTAINER_NAME` must identify the actual container the watchdog is allowed to restart.

---

# Configuration

All settings are configured through environment variables.

Durations are expressed in seconds.

| Variable                  |                     Default | Description                                 |
| ------------------------- | --------------------------: | ------------------------------------------- |
| `VLLM_BASE_URL`           |          `http://vllm:8000` | vLLM API base URL                           |
| `VLLM_CONTAINER_NAME`     |                      `vllm` | Docker container name or ID to restart      |
| `VLLM_MODEL`              |                    required | Exact model name exposed by vLLM            |
| `VLLM_API_KEY`            |                       empty | Optional Bearer token                       |
| `PROBE_PROMPT`            |                      `ping` | Synthetic inference prompt                  |
| `PROBE_MAX_TOKENS`        |                         `1` | Maximum generated tokens                    |
| `CHECK_INTERVAL`          |                        `30` | Normal monitoring interval                  |
| `HEALTH_TIMEOUT`          |                         `5` | `/health` request deadline                  |
| `INFERENCE_TIMEOUT`       |                        `15` | Inference probe deadline                    |
| `FAILURE_THRESHOLD`       |                         `3` | Consecutive failures before restart         |
| `STARTUP_GRACE_PERIOD`    |                       `300` | Grace period after watchdog startup/restart |
| `RECOVERY_CHECK_INTERVAL` |                        `10` | Recovery probe interval                     |
| `RECOVERY_TIMEOUT`        |                       `600` | Maximum recovery verification period        |
| `RESTART_COOLDOWN`        |                       `300` | Minimum time between restart attempts       |
| `RESTART_WINDOW`          |                       `600` | Rolling restart history window              |
| `MAX_RESTARTS`            |                         `3` | Maximum restart attempts                    |
| `DOCKER_STOP_TIMEOUT`     |                        `30` | Docker stop timeout                         |
| `DOCKER_API_TIMEOUT`      |                        `60` | Overall Docker API deadline                 |
| `ALERT_WEBHOOK_URL`       |                       empty | Optional webhook endpoint                   |
| `ALERT_TIMEOUT`           |                         `5` | Webhook request deadline                    |
| `STATE_FILE`              | `/data/watchdog_state.json` | Persistent watchdog state                   |
| `LOG_LEVEL`               |                      `INFO` | Logging level                               |

The example `.env.example` contains the available configuration variables.

---

# Probe behavior

## Health validation

The health probe requires a successful `/health` request.

## Inference validation

A generation request is considered successful only when the response has a valid terminal OpenAI-compatible chat completion structure.

The watchdog validates fields such as:

* HTTP status
* JSON object shape
* completion object type
* `choices`
* choice index
* assistant message
* terminal `finish_reason`

An empty or null assistant content may still be considered valid when the request completed normally, which accommodates immediate EOS and some reasoning-model behaviors with a very small generation budget.

---

# Timing behavior

The watchdog runs as a **single synchronous control loop**.

Probe executions do not overlap.

Therefore:

```text
iteration duration
≈ health probe
+ inference probe
+ CHECK_INTERVAL
```

in the worst case.

During recovery, individual I/O deadlines are additionally bounded by the remaining recovery deadline.

This design deliberately favors predictable recovery behavior over high-frequency concurrent probing.

---

# Logging

Logs are emitted as structured JSON to stdout.

Typical events include:

```text
HEALTHY
SUSPECT
ALIVE_BUT_STALLED
RESTART_TRIGGERED
RECOVERING
RECOVERY_SUCCESS
RECOVERY_FAILED
MAX_RESTART_EXCEEDED
FAILED
```

Restart events include diagnostic fields such as:

* reason
* health failure count
* inference failure count
* restart count
* last successful inference timestamp

Sensitive data is intentionally excluded.

The watchdog does not log:

* API keys
* Authorization headers
* inference prompts
* response bodies
* webhook URLs

---

# Alerts

An optional webhook can receive important recovery events.

Supported events include:

```text
RESTART_TRIGGERED
RECOVERY_SUCCESS
RECOVERY_FAILED
MAX_RESTART_EXCEEDED
```

The alert path is best-effort.

A failed webhook delivery does not stop the monitoring loop.

---

# Graceful shutdown

`SIGTERM` and `SIGINT` interrupt waiting periods and prevent new restart attempts.

In-flight I/O is allowed to finish within its configured deadline before state persistence and shutdown complete.

When using Docker Compose, configure `stop_grace_period` above the longest relevant probe/alert timeout.

---

# Docker socket security

> **Important:** access to `/var/run/docker.sock` effectively grants host-level Docker administration capability.

A process with Docker socket access may be able to:

* create privileged containers
* mount host filesystems
* inspect other containers
* control other Docker workloads

Running this watchdog as a non-root user, dropping Linux capabilities, and using a read-only filesystem are useful defense-in-depth measures, but **they do not remove the authority provided by the Docker socket itself**.

Only run trusted images and code with direct Docker socket access.

For stronger isolation, place an authorization proxy between the watchdog and Docker and allow only the minimum operations required for the target container.

Do **not** solve socket permission problems by making the Docker socket world-writable.

---

# Tests

Tests do not require Docker or a GPU.

```bash
python3 -m venv .venv

.venv/bin/pip install -r requirements.txt

.venv/bin/python -m unittest discover -v
```

Local execution:

```bash
VLLM_MODEL=my-model \
VLLM_BASE_URL=http://localhost:8000 \
STATE_FILE=/tmp/watchdog-state.json \
.venv/bin/python -m watchdog.main
```

The test suite covers scenarios including:

* healthy operation
* single probe failure
* consecutive failures
* inference-only failure
* health-only failure
* alive-but-stalled detection
* recovery success
* recovery timeout
* restart limits
* cooldown
* state persistence
* corrupted state
* Docker daemon errors
* webhook failure
* graceful shutdown

---

# Container images

Prebuilt images are published to:

```text
ghcr.io/pyg410/vllm-self-healer
```

Supported architectures:

```text
linux/amd64
linux/arm64
```

The watchdog itself performs no GPU computation.

Your vLLM deployment still requires its own compatible GPU environment.

---

# Releases

The GitHub Actions publishing workflow produces images for:

```text
master push
    ├─ latest
    └─ sha-xxxxxxxx

v* tag
    ├─ exact version tag
    └─ sha-xxxxxxxx
```

Example release:

```bash
git tag -a v0.1.0 -m "Release v0.1.0"

git push origin v0.1.0
```

Then:

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

Production deployments should prefer version tags or immutable image digests over `latest`.

---

# Offline / closed-network deployment

Pull the image from a connected machine:

```bash
docker pull --platform linux/amd64 \
  ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

Export it:

```bash
docker save \
  -o vllm-self-healer-v0.1.0.tar \
  ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

Transfer the archive through your approved process.

On the offline host:

```bash
docker load -i vllm-self-healer-v0.1.0.tar
```

The loaded image can also be retagged and pushed to an internal Nexus, Harbor, or other OCI-compatible registry.

---

# Known limitations

vLLM Self-Healer intentionally has a narrow responsibility:

> Detect loss of inference forward progress and perform bounded Docker-level recovery.

It does not attempt to identify or repair the underlying vLLM, CUDA, NCCL, driver, or hardware bug.

Important limitations:

* One watchdog targets one vLLM container.
* Restarting interrupts in-flight requests.
* Network outages can look similar to an inference stall.
* Incorrect authentication or model configuration can trigger probe failures.
* Extreme overload can cause inference probes to exceed their timeout even when the engine is technically healthy.
* Timeouts and thresholds must therefore be tuned for the deployment's real latency distribution.
* A container restart cannot recover every GPU or driver failure.
* Some failures may require GPU reset, node isolation, driver recovery, or host reboot.
* Docker socket access has significant security implications.
* Multiple watchdog instances with independent state volumes are not coordinated.

The restart budget exists specifically so that an unrecoverable host-level failure does not turn into an endless container restart loop.

---

# Design philosophy

This project deliberately separates:

```text
liveness
```

from:

```text
forward progress
```

A running process is necessary for serving inference.

It is not sufficient.

The watchdog therefore treats a successful generation request as the strongest external signal that the serving path is still functional.

This is not intended to replace vLLM's own health mechanisms.

It is an additional operational safety layer for deployments where recovering from an alive-but-stalled inference server matters.

---

# License

Apache License 2.0.

See `LICENSE` for details.
