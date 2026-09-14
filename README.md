# vLLM Self-Healer

**English** | [한국어](README.ko.md)

**Detect stalled inference in a running vLLM instance and automatically restart its container.**

vLLM Self-Healer is a lightweight watchdog for bounded recovery on Docker or Kubernetes. It combines /health checks with real synthetic generation requests to detect cases where the HTTP server responds but inference no longer completes.

```text
/health: 200 OK + inference: timeout
                 ↓
       consecutive failure threshold
                 ↓
        restart policy evaluation
                 ↓
     Container restart → recovery probes
```

A single failure does not trigger a restart. Recovery attempts are bounded, and each restart is followed by actual inference verification. The watchdog itself does not need a GPU.

## Features

- **Real inference probes:** exercise the generation path with a small request.
- **Automatic recovery:** recover the target container through Docker or Kubernetes after repeated failures.
- **Recovery verification:** wait for model loading, then require both probes to succeed.
- **Restart safeguards:** cooldown, rolling limits, and a cap on attempts without recovery.
- **Persistent state:** retain restart history and FAILED across watchdog restarts.
- **Operations:** JSON logs, optional webhooks, and amd64/arm64 images.

## Docker / Compose quick start

Add the watchdog to the Compose project containing your existing vLLM service. The example assumes Linux and an accessible host Docker socket.

Pull the image:

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:latest
```

Find the Docker socket group ID:

```bash
stat -c '%g' /var/run/docker.sock
```

Add the served model name and socket group to your project's .env:

```dotenv
VLLM_MODEL=your-served-model
DOCKER_SOCKET_GID=998
VLLM_API_KEY=
```

Replace 998 with the actual group ID. Set VLLM_API_KEY if the API requires authentication. The model must support chat completions and have a chat template.

Merge this service into your existing services mapping and add watchdog-data to the top-level volumes mapping:

```yaml
services:
  vllm-self-healer:
    image: ghcr.io/pyg410/vllm-self-healer:latest
    restart: unless-stopped
    environment:
      VLLM_BASE_URL: http://vllm:8000
      VLLM_CONTAINER_NAME: vllm
      VLLM_MODEL: ${VLLM_MODEL:?Set the served model name}
      VLLM_API_KEY: ${VLLM_API_KEY:-}
      STATE_FILE: /data/watchdog_state.json
    group_add:
      - "${DOCKER_SOCKET_GID:?Set the Docker socket group ID}"
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

Ensure vllm resolves to the API service on the shared Docker network. Set VLLM_CONTAINER_NAME to the actual container name or ID to restart; Compose service names and generated container names can differ.

```bash
docker compose up -d vllm-self-healer
docker compose logs -f vllm-self-healer
```

On a fresh watchdog startup and after a vLLM restart, the default startup grace is 300 seconds. Probes run during grace; both must succeed in the same iteration before normal monitoring begins. Early success ends recovery immediately, but automatic restart remains suppressed until the original grace boundary. Previously saved recovery deadlines and FAILED state are preserved.

> Docker socket access grants extensive control over the host Docker daemon. Give it only to trusted code and images.

For production, select a published version or pin an image digest. A previously published build is also available as ghcr.io/pyg410/vllm-self-healer:sha-440c4334. Use v0.1.0 after its publishing workflow succeeds for Kubernetes support.

## Kubernetes

Set RECOVERY_MODE=kubernetes to run the watchdog as a sidecar. The shared probe/controller policy is unchanged; recovery signals go to kubelet instead of Docker.

```text
Pod: vLLM ← /health + inference ← self-healer
                                  ↓ /ready, /live
                                kubelet → restart vLLM
```

The example includes a Deployment, Service and a small startup-wrapper ConfigMap:

```bash
kubectl apply -f kubernetes/deployment.example.yaml
kubectl logs -f deployment/vllm -c vllm-self-healer
```

Review the model, GPU resources, image versions and storage before applying. v0.1.0 is the first image with this mode. The sidecar requires neither a Docker socket nor Kubernetes API credentials, SDK or Pod-delete RBAC. ServiceAccount token mounting is disabled.

**Define readinessProbe and livenessProbe on the vLLM container, pointing to the sidecar's numeric port 9090.** Containers share the Pod network. Attaching these probes to the sidecar would restart the wrong container. A Service uses Pod readiness to exclude it from new traffic; existing connections are not forcibly drained.

| Endpoint | HTTP 200 | HTTP 503 |
|---|---|---|
| /ready | HEALTHY | SUSPECT, RESTARTING, RECOVERING, FAILED |
| /live | No active restart request, including FAILED | Active request awaiting a new vLLM start ID |

An HTTP server thread reads synchronized status; it does not execute controller transitions or recovery actions. Only Kubernetes mode starts this server.

### Confirming restart without an API

HTTP unavailability alone cannot distinguish an existing hang from a new restart. The example wraps vLLM startup with a short Python script which writes a fresh UUID atomically to a shared volume and then execs vLLM. Keep this wrapper when adapting the manifest.

The backend records the old UUID and deadline **before** exposing /live=503. Entering RESTARTING or RECOVERING does not automatically clear the request. The signal remains until a different valid UUID proves a new launch. The HTTP reader immediately suppresses the old signal for that new UUID, even before the controller's next iteration, preventing it from restarting the new process again.

The controller then enters RECOVERING, probes immediately (including during STARTUP_GRACE_PERIOD) and requires both probes to succeed. A new UUID alone never marks the workload ready. Pending requests and their deadlines survive sidecar restarts through the state file. Legacy version 1 Docker state files without backend data are accepted.

If the UUID is missing/invalid when requesting restart, or no new UUID appears within KUBERNETES_RESTART_TIMEOUT (default 120 seconds), the controller enters FAILED. At the deadline /live returns 200 even if the control loop has not yet processed the failure, stopping a stale signal; /ready remains 503. Set this timeout above the liveness period plus termination grace and expected container restart delay.

### Probe and storage choices

The example startupProbe checks the sidecar /live endpoint, not vLLM /health. This enables the shared controller to own model-loading deadlines. A model-health startupProbe can restart vLLM independently of this project's budget.

The shared start-ID volume survives container restarts. The example state volume is emptyDir: history survives sidecar/container restarts **within the same Pod**, but disappears when the Pod is replaced. Use suitable persistent storage for /data if history must survive Pod replacement, and use distinct state storage for each workload. Do not share one state file across replicas.

Kubelet can still restart containers independently after process exits, startup probe failures or sidecar/network outages. These native actions are outside the watchdog's budget. The no-repeat signaling guarantee assumes the provided wrapper writes a new ID on every launch. A broken wrapper, inaccessible sidecar, or hung filesystem can defeat that assumption. Protect port 9090 with appropriate cluster networking; it exposes status without authentication. Multi-Pod coordination/Operator support remains future work.

## Failure detection

Each normal monitoring cycle runs two separate checks:

| Probe | Success condition | Default deadline |
|---|---|---|
| GET /health | HTTP 200 | 5 seconds |
| POST /v1/chat/completions | HTTP 200 and valid terminal completion JSON | 15 seconds |

The inference request uses the configured model, prompt ping, max_tokens=1, temperature=0, and stream=false by default. Prompt and token budget are configurable.

| Health | Inference | Interpretation |
|---|---|---|
| Success | Success | Healthy |
| Success | Failure | Suspected inference-path problem |
| Failure | Success | Possible health endpoint problem |
| Failure | Failure | Possible service or connectivity problem |

**Every health-success/inference-failure result is logged with reason ALIVE_BUT_STALLED, including the first failure.** This is a diagnostic reason, not a separate watchdog state or proof of a GPU hang. During normal monitoring the state becomes SUSPECT.

The probes have independent consecutive failure counters. Success resets only that probe's counter. By default, when either counter reaches three, the watchdog evaluates the restart policy. Restart occurs only if the budget and cooldown permit it.

Completion validation requires object=chat.completion, nonempty choices, an integer choice index, an assistant message with a content field, and finish_reason=stop or length. Empty/null content is allowed for immediate EOS or a small reasoning-model budget. This validates completion structure, not answer quality. Malformed JSON and HTTP/connection/timeout errors count as failures.

## Recovery policy and states

```text
Startup → RECOVERING → both probes succeed → HEALTHY
HEALTHY → probe failure → SUSPECT
SUSPECT → threshold + restart policy permit → RESTARTING
RESTARTING → RECOVERING → success → HEALTHY
RECOVERING → timeout → retry policy → restart or FAILED
```

| State | Meaning |
|---|---|
| HEALTHY | Both probes succeeded |
| SUSPECT | Probe failures; may also be waiting for cooldown after reaching the threshold |
| RESTARTING | Attempt recorded; Recovery backend restart requested |
| RECOVERING | Verifying readiness, including during startup grace |
| FAILED | Automatic probes/restarts stopped; operator action required |

At startup and after a confirmed restart, recovery probes start immediately and repeat at RECOVERY_CHECK_INTERVAL (10 seconds). STARTUP_GRACE_PERIOD (300 seconds) suppresses automatic restart, not probing. The overall recovery deadline remains grace + RECOVERY_TIMEOUT (600 seconds), so persistent probe failure permits policy evaluation after 900 seconds by default. Both probes must succeed in the same iteration; POST_RECOVERY must also allow completion.

Three safeguards apply:

| Safeguard | Default |
|---|---|
| Minimum interval between restart attempt start times | 300 seconds |
| Maximum attempts within the last 600 seconds | 3 |
| Maximum consecutive attempts without verified recovery | 3 |

The last allowed attempt still gets recovery verification. If another restart is needed after the budget is exhausted, the watchdog latches into FAILED. Successful recovery clears the unrecovered attempt count but preserves recent restart timestamps.

Docker daemon failures and ambiguous API timeouts consume an attempt too. Because a timeout may still mean Docker acted server-side, the watchdog checks recovery before trying again.

The loop is synchronous: each normal iteration takes health request time + inference request time + CHECK_INTERVAL. Requests do not overlap. Recovery probe deadlines are capped by the remaining recovery time.

## Detection scope and recovery limits

A successful /health response does not establish successful inference. Synthetic generation can expose EngineCore stalls, silent generation hangs, CUDA/decode-path hangs or NCCL stalls when they affect the probe. **Detection does not guarantee container-level recovery.** GPU reset, node drain, host restart or operator intervention may still be necessary. The watchdog stops retrying at its configured budget and enters FAILED. See [known failure modes](docs/known-failure-modes.md).

Recovery targets one vLLM container: directly through Docker or through kubelet liveness in Kubernetes. The watchdog does not diagnose or repair CUDA, NCCL, driver, or hardware bugs.

- Restarting interrupts in-flight requests.
- Network outages, incorrect credentials/model names, and overload can all cause probe failures. Tune deadlines and thresholds to real service latency.
- A short completion cannot establish the health of every replica, long generation, or all GPU functions.
- One watchdog targets one container. Separate watchdogs with independent state volumes are not coordinated.
- GPU reset, host reboot, traffic draining, and Prometheus-based decisions are not implemented.

## Configuration

All settings use environment variables. Durations are in seconds. Positive values are required except startup grace, cooldown, and Docker stop timeout, which may be zero. DOCKER_API_TIMEOUT must exceed DOCKER_STOP_TIMEOUT.

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
| RECOVERY_MODE | docker | docker or kubernetes |
| WATCHDOG_HTTP_HOST | 0.0.0.0 | Kubernetes probe server bind address |
| WATCHDOG_HTTP_PORT | 9090 | Kubernetes probe server port |
| VLLM_START_ID_FILE | /run/vllm-watchdog/start-id | Shared vLLM launch UUID |
| KUBERNETES_RESTART_TIMEOUT | 120 | Maximum wait for a new launch UUID |
| LOG_TIMEZONE | UTC | IANA log timezone; invalid values fail startup |
| LOG_FILE | Empty | Optional JSON log file; stdout stays enabled |
| LOG_MAX_BYTES | 10485760 | Positive rotation size |
| LOG_BACKUP_COUNT | 5 | Positive number of rotated backups |
| EVENT_WEBHOOK_URL | Empty | Informational event endpoint |
| EVENT_WEBHOOK_METHOD | POST | POST, PUT or PATCH |
| EVENT_WEBHOOK_TIMEOUT | 5 | Total request deadline |
| EVENT_WEBHOOK_EVENTS | Empty | All events or comma-separated filter |
| EVENT_WEBHOOK_HEADERS | {} | JSON object of string headers; never logged |
| EVENT_WEBHOOK_BODY | {} | Static JSON object; never logged |
| PRE_RESTART_WEBHOOK_URL | Empty | Optional action endpoint |
| PRE_RESTART_WEBHOOK_METHOD | POST | POST, PUT, PATCH |
| PRE_RESTART_WEBHOOK_TIMEOUT | 10 | Total deadline |
| PRE_RESTART_WEBHOOK_HEADERS | {} | Secret JSON headers |
| PRE_RESTART_WEBHOOK_BODY | {} | Static JSON body |
| PRE_RESTART_WEBHOOK_FAILURE_POLICY | continue | continue or abort |
| POST_RECOVERY_WEBHOOK_URL | Empty | Optional action endpoint |
| POST_RECOVERY_WEBHOOK_METHOD | POST | POST, PUT, PATCH |
| POST_RECOVERY_WEBHOOK_TIMEOUT | 10 | Total deadline |
| POST_RECOVERY_WEBHOOK_HEADERS | {} | Secret JSON headers |
| POST_RECOVERY_WEBHOOK_BODY | {} | Static JSON body |
| POST_RECOVERY_WEBHOOK_FAILURE_POLICY | continue | continue or abort |

VLLM_CONTAINER_NAME and the Docker timeout relationship are required only in Docker mode. RECOVERY_MODE defaults to docker; existing Compose deployments need no new settings.


RECOVERY_TIMEOUT excludes startup grace. MAX_RESTARTS applies to both the rolling window and consecutive attempts without recovery. See [.env.example](.env.example) for the complete environment example, including Compose-only image and socket group settings.

## v0.2.0 recovery and diagnostics

Grace now allows early readiness. Recovery logs and event webhooks include recovery_reason; terminal failed_reason survives process restart. LOG_TIMEZONE supports IANA zones, and Docker startup diagnostics are read-only and non-fatal. The default image still includes Docker support; a core-only build is available for Kubernetes. See the [v0.2.0 migration guide](docs/operations.md#v020-migration-and-diagnostics).

## v0.1.1 operations

Startup logs now show the effective non-sensitive configuration, likely environment-variable typos, safe startup error types/stages, and restored recovery timing. Optional rotating file logs and HTTP integrations require no settings for existing users.

- [Logging options and startup diagnostics](docs/operations.md#logging-choose-an-option): stdout plus a rotating application file, or stdout with Docker-managed rotation.
- [Event webhooks](docs/operations.md#informational-event-webhook): optional filtered lifecycle notifications, including RESTART_CONFIRMED.
- [Lifecycle action hooks](docs/operations.md#lifecycle-action-hooks): PRE_RESTART and POST_RECOVERY for trusted external drain/ready services.
- [Timing and state reset](docs/operations.md#timing-and-when-configuration-applies): when settings take effect and how to reset only watchdog state.

PRE abort enters FAILED without restarting; POST abort enters FAILED without marking readiness or emitting RECOVERY_SUCCESS. Both default to continue. Legacy alerts and recovery budgets remain compatible. Logs default to UTC; webhook timestamps remain UTC regardless of LOG_TIMEZONE.

## Logs and alerts

Logs are JSON on stdout. Successful probes use DEBUG; failures use WARNING. State transitions and recovery events use INFO or higher. Set LOG_LEVEL=DEBUG to see successful checks.

Example probe log, formatted for readability:

```json
{
  "timestamp": "2026-09-13T03:21:05+00:00",
  "level": "WARNING",
  "event": "probe result",
  "state": "healthy",
  "health": true,
  "inference": false,
  "health_failure_count": 0,
  "inference_failure_count": 1,
  "reason": "ALIVE_BUT_STALLED"
}
```

The state field reflects the state at probe time; the subsequent state transition records healthy → suspect. Restart logs include both failure counters, reason, restart count, and the last successful inference timestamp.

Set ALERT_WEBHOOK_URL to enable best-effort HTTP notifications. Legacy alert events are RESTART_TRIGGERED, RECOVERY_SUCCESS, RECOVERY_FAILED, and MAX_RESTART_EXCEEDED; the new event webhook also supports RESTART_CONFIRMED. Initial readiness also emits RECOVERY_SUCCESS.

Example webhook payload:

```json
{
  "service": "vllm-watchdog",
  "container": "vllm",
  "event": "RESTART_TRIGGERED",
  "reason": "ALIVE_BUT_STALLED",
  "restart_count": 1,
  "timestamp": "2026-09-13T03:22:15+00:00"
}
```

Webhook failures are logged and do not terminate the loop, though delivery may delay it by up to ALERT_TIMEOUT. There is no retry queue. API keys, Authorization headers, prompts, response bodies, raw exception text, and webhook URLs are excluded from logs.

## Persistent state and FAILED recovery

When an in-progress RECOVERING cycle is restored, stored recovery_ready_at/recovery_deadline override newly configured grace/timeout values until that cycle finishes or state is intentionally reset.

State is saved to /data/watchdog_state.json using atomic replacement and fsync. Restart attempts are persisted **before** the recovery backend is activated. The file also retains recovery timing, the unrecovered attempt count, and last successful inference time.

A file lock prevents concurrent watchdogs from using the same state path. Corrupt or unwritable state disables automatic restarts; corrupt files are preserved. FAILED remains latched after restarting the watchdog. Persistence errors can prevent FAILED itself from being saved.

To reset it intentionally:

1. Stop the watchdog.
2. Investigate the logs and resolve the underlying problem.
3. Back up the state file if needed.
4. Delete only the watchdog state JSON, not the entire volume.
5. Start the watchdog again.

This resets the restart budget. Merely restarting the watchdog does not.

SIGTERM/SIGINT interrupt waits and prevent new restart attempts. In-flight I/O finishes within its deadline, then state is saved and logging flushed. Set stop_grace_period above the longest backend/probe/hook deadline plus follow-on notification time; the example uses 90 seconds.

## Deployment and releases

### Local build

The prebuilt-image quick start does not require a clone. For source-based deployment, use [docker-compose.example.yml](docker-compose.example.yml):

```bash
git clone https://github.com/pyg410/vllm-self-healer.git
cd vllm-self-healer
cp .env.example .env
# Configure .env before starting.
docker compose -f docker-compose.example.yml up -d --build
```

The example vLLM service needs a compatible GPU environment. For a standalone watchdog image build, run docker build -t vllm-self-healer:test . from the repository root.

The watchdog runs as UID/GID 10001. Named volumes use /data ownership; bind mounts need write permission for UID 10001. Adjust socket paths and group IDs for rootless Docker.

### GHCR and version releases

[GitHub Actions](.github/workflows/docker-publish.yml) builds linux/amd64 and linux/arm64 with Buildx/QEMU and layer caching. It runs amd64 container tests and startup/shutdown checks before publishing, then verifies pulls and runtime imports for both architectures.

| Push | Image tags |
|---|---|
| master | latest and sha-xxxxxxxx |
| v* tag | Exact Git tag and sha-xxxxxxxx |

SHA tags use eight characters. Version-tag pushes do not update latest. Publishing uses the built-in GITHUB_TOKEN with contents: read and packages: write; no separate PAT is needed by the workflow.

The following commands publish the v0.1.0 release (run once). Create a new release only when intended, on a commit containing the workflow, and wait for Actions to succeed before pulling it:

```bash
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

Existing Git tags are not built retroactively. Do not reuse release versions. Base images/dependencies may change between builds; pin a digest for an immutable artifact.

For anonymous pulls, the GHCR package must be Public. A public repository does not automatically make its package public. Package owners can change this in GitHub Packages settings. Private packages require credentials with package read access and docker login ghcr.io. Manual authentication is separate from the workflow's GITHUB_TOKEN.

### Offline deployment

On a connected machine, pull a published build for the destination architecture and export it:

```bash
docker pull --platform linux/amd64 ghcr.io/pyg410/vllm-self-healer:sha-440c4334
docker save -o vllm-self-healer-sha-440c4334.tar ghcr.io/pyg410/vllm-self-healer:sha-440c4334
```

Use linux/arm64 instead when appropriate. Transfer the archive through your approved process, then load it on the offline host:

```bash
docker load -i vllm-self-healer-sha-440c4334.tar
```

Retag and push to an internal Nexus/Harbor registry if needed, and update Compose's image reference. Provision the vLLM image and model weights separately; this archive contains only the watchdog.

## Docker socket security

**Access to /var/run/docker.sock effectively grants host-level Docker administration.** It may allow privileged containers, host filesystem mounts, and control of other workloads.

Non-root execution, dropped capabilities, and a read-only filesystem provide additional protection but do not remove the socket's authority. Use trusted images only. For stronger isolation, restrict target containers and Docker operations through an authorization layer. Never make the socket world-writable to fix permissions.

## Background and upstream reports

**A running process is necessary for serving inference, but it is not sufficient.** Docker restart policies handle process exits; an HTTP health response alone does not demonstrate generation progress.

Related upstream reports and work include:

| Reference | Topic |
|---|---|
| [Issue #52319](https://github.com/vllm-project/vllm/issues/52319) | Reported generation stall while health/metrics remain responsive |
| [Issue #52247](https://github.com/vllm-project/vllm/issues/52247) | EngineCore blocked on GPU synchronization |
| [Issue #42897](https://github.com/vllm-project/vllm/issues/42897) | EngineCore hang under sustained traffic |
| [Issue #36960](https://github.com/vllm-project/vllm/issues/36960) | Proposal for GPU-aware readiness checks |
| [PR #36451](https://github.com/vllm-project/vllm/pull/36451) | Work on detecting alive-but-hung EngineCore |

These are background references, not a claim that this watchdog reproduces or fixes every reported failure. It complements vLLM health mechanisms with an external generation check.

## Development and tests

Python 3.12 on Linux/macOS is recommended. Tests need neither Docker nor a GPU; HTTP tests open a loopback listener.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
```

Run locally:

```bash
VLLM_MODEL=my-model VLLM_BASE_URL=http://localhost:8000 STATE_FILE=/tmp/watchdog-state.json .venv/bin/python -m watchdog.main
```

Tests cover probe validation, consecutive failures, recovery, budgets, cooldown, persistence/corruption, Docker errors, webhooks, and signal handling.

The synchronous client uses POSIX SIGALRM deadlines and must run on the main thread. Windows and embedding alongside another SIGALRM user are unsupported. Recovery uses monotonic time during execution; persisted timestamps use wall time, so major clock changes can affect restored timing. OS-level disk hangs are outside the HTTP/Docker deadline mechanism.

## License

Apache License 2.0. See [LICENSE](LICENSE).
