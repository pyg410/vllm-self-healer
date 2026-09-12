# vLLM Self-Healer

**English** | [한국어](README.ko.md)

An independent Python watchdog that detects and recovers vLLM containers whose **process is alive but inference has stalled** in Docker Compose. It runs as a single synchronous process without a separate HTTP server.

## Architecture

```text
watchdog container
  ├─ GET /health ────────────────┐
  ├─ POST /v1/chat/completions ──┤→ vLLM container → EngineCore → GPU → decode
  ├─ Controller + RestartPolicy ← independent ProbeResult values
  ├─ Docker SDK → Docker socket → restart target container
  ├─ StateStore → /data/watchdog_state.json
  └─ Alert → optional HTTP webhook
```

Docker's restart policy does not check whether a running process is making progress on inference. Likewise, /health cannot substitute for a successful generation request. A separate synthetic inference probe sends a fixed prompt with a small token budget to verify that HTTP input, scheduling, model execution, decoding, and the response complete.

The probe uses the chat completions format in the [official vLLM API documentation](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/). A generation model with a chat template is required. A valid response requires HTTP 200, JSON object=chat.completion, nonempty choices, an integer index, an assistant message, and a stop/length finish_reason. Empty content/null is accepted within a valid terminal completion structure to accommodate immediate EOS or a reasoning model with a short token budget.

## States and restart policy

- Initial startup: RECOVERING → startup grace → both probes succeed → HEALTHY.
- Normal monitoring: either probe failing transitions to SUSPECT. Both probes run independently in each iteration.
- A restart is attempted when either probe's consecutive failure counter reaches FAILURE_THRESHOLD.
- health=true/inference=false is logged as ALIVE_BUT_STALLED, with detailed timeout/HTTP/response errors logged separately.
- health=false/inference=true also triggers a restart when the health counter reaches the threshold.
- A successful probe resets **only its own counter** to zero. Clearing inference failures on /health success would prevent hang detection.
- RESTARTING → Docker call → RECOVERING. After STARTUP_GRACE_PERIOD, probes run at RECOVERY_CHECK_INTERVAL for up to RECOVERY_TIMEOUT. The recovery timeout starts **after** the grace period.
- Recovery requires both probes to succeed in the same iteration. A recovery timeout goes through the restart policy again.
- RESTART_COOLDOWN is the minimum interval between the start times of restart attempts. If both probes recover during cooldown, the watchdog can return to HEALTHY.

Each interval is a wait **after work completes**. Requests do not overlap; a normal monitoring iteration can take up to the sum of both probe timeouts in addition to the interval. During recovery, each HTTP deadline is capped by the remaining recovery time.

### Preventing restart loops

Up to MAX_RESTARTS attempts are allowed within RESTART_WINDOW. The last allowed attempt still receives recovery verification; if another restart is needed afterward, the watchdog latches into FAILED. It also stops restarting after **MAX_RESTARTS attempts without successful recovery**, even if the rolling window has expired. This prevents endless retries when model loading takes longer than the window. Successful verification resets the consecutive recovery attempt count but retains recent restart history.

History is maintained in a deque and persisted as JSON using atomic replacement and fsync. The latest timestamp is retained so cooldown still works when it exceeds the window. Attempts are saved **before** the Docker API call, so daemon unavailability, permission errors, and timeouts also consume the budget. A Docker timeout leaves server-side success uncertain, so recovery is checked before another call is attempted. The [Docker SDK](https://docker-py.readthedocs.io/en/stable/containers.html) stop timeout and the overall API deadline are configured separately.

FAILED is persisted and does not automatically clear when the watchdog container restarts. State corruption and read/write errors block restarts and trigger an alert attempt. Corrupt files are preserved for diagnosis. A lock at the state path prevents duplicate processes from using the same file.

To clear FAILED, stop the watchdog, resolve the cause, back up the state JSON, delete only that file, and start the watchdog again. This also resets the restart budget. Do not delete an entire volume containing other data.

## Configuration

All settings are read from environment variables. Durations are in seconds and must be positive; grace, cooldown, and Docker stop timeout may be zero. Invalid values are rejected at startup.

| Variable | Default | Description |
|---|---|---|
| VLLM_BASE_URL | http://vllm:8000 | API base URL |
| VLLM_CONTAINER_NAME | vllm | Target Docker container name/ID |
| VLLM_MODEL | Required | Exact model name served by the API |
| VLLM_API_KEY | Empty | Bearer authentication; never logged |
| PROBE_PROMPT | ping | Fixed synthetic prompt |
| PROBE_MAX_TOKENS | 1 | Generation token budget |
| CHECK_INTERVAL | 30 | Wait between normal monitoring iterations |
| HEALTH_TIMEOUT | 5 | Overall /health request deadline |
| INFERENCE_TIMEOUT | 15 | Overall inference request deadline |
| FAILURE_THRESHOLD | 3 | Consecutive failure threshold per probe |
| STARTUP_GRACE_PERIOD | 300 | Model loading grace at startup/after restart |
| RECOVERY_CHECK_INTERVAL | 10 | Interval between recovery checks |
| RECOVERY_TIMEOUT | 600 | Recovery deadline after grace |
| RESTART_COOLDOWN | 300 | Minimum interval between restart attempts |
| RESTART_WINDOW | 600 | Rolling restart window |
| MAX_RESTARTS | 3 | Maximum attempts within the window/without recovery |
| DOCKER_STOP_TIMEOUT | 30 | Wait before forcibly stopping the container |
| DOCKER_API_TIMEOUT | 60 | Overall Docker operation deadline; must exceed stop timeout |
| ALERT_WEBHOOK_URL | Empty | Empty disables alerts |
| ALERT_TIMEOUT | 5 | Overall webhook request deadline |
| STATE_FILE | /data/watchdog_state.json | Persistent state path |
| LOG_LEVEL | INFO | DEBUG/INFO/WARNING/ERROR/CRITICAL |

Compose-only variables: VLLM_IMAGE (the example uses latest; pin a validated version/digest in production) and DOCKER_SOCKET_GID (the numeric group ID of the host socket). See [.env.example](.env.example) for all settings.

## Running with Docker Compose

Requires Linux Docker Engine, NVIDIA Container Toolkit, a GPU, and Docker Compose with GPU device reservation support.

```sh
cp .env.example .env
stat -c '%g' /var/run/docker.sock
# Set DOCKER_SOCKET_GID in .env to the number above and adjust model/image/timeouts.
docker compose -f docker-compose.example.yml up -d --build
docker compose -f docker-compose.example.yml logs -f vllm-watchdog
```

The example downloads a small chat model and runs it on the GPU. Check model access and GPU memory availability. The API is exposed only on the internal Compose network. To integrate with an existing Compose deployment, copy the watchdog service and watchdog-data volume, and point build to this project. Both services must share a network, and VLLM_CONTAINER_NAME must match the actual target. For a local model path, add a model volume to the vLLM service.

The example passes VLLM_API_KEY from .env to both vLLM and the watchdog. Authentication errors and incorrect model names count as failures, so verify that they match before deployment. Because depends_on does not guarantee readiness, the watchdog applies its own startup recovery grace.

The image runs as UID/GID 10001. Named volumes use the ownership of /data. For bind mounts, grant UID 10001 write access to the directory. The socket group is added through group_add; rootless Docker requires adjusting the socket mount path and GID. Do not make the socket world-writable to resolve permission issues.

## Tests and local execution

Python 3.12 is recommended on Linux/macOS. Tests use unittest and run without Docker or a GPU.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
VLLM_MODEL=my-model VLLM_BASE_URL=http://localhost:8000 STATE_FILE=/tmp/watchdog-state.json .venv/bin/python -m watchdog.main
```

The HTTP client, Docker manager, clocks, alerts, and state store are injectable, and policy tests use simulated time. Tests cover healthy operation, single/consecutive timeouts, health-only failures, recovery success/failure, restart limits, counter resets on success, history restoration and corruption, persistence failures, daemon outages, cooldown, shutdown, and webhook failures.

SIGTERM/SIGINT immediately interrupt waits and prevent new restarts. In-flight I/O finishes within its deadline, followed by state persistence, client closure, and logging flush before exit. Set Compose stop_grace_period comfortably above the longest request deadline plus alert time (90 seconds by default).

## Logging and alerts

JSON logs are written to stdout. Successful probes use DEBUG, failures use WARNING, and state transitions/restarts/recovery use INFO or higher. Restart logs include a timestamp, reason, health/inference failure counts, the Unix timestamp of the last successful inference, and the stored restart count.

Webhook events: RESTART_TRIGGERED, RECOVERY_SUCCESS, RECOVERY_FAILED, and MAX_RESTART_EXCEEDED. The payload contains service, container, event, reason, restart_count, and an ISO timestamp. Initial readiness also emits RECOVERY_SUCCESS. Docker call failures, recovery timeouts, and internal errors leading to FAILED also emit RECOVERY_FAILED. Delivery failures are logged and the control loop continues. Delivery is best-effort with no retry queue.

Authorization headers, API keys, prompts, response bodies, raw exceptions, and webhook URLs are never logged. HTTP redirects and automatic environment proxy/.netrc authentication are disabled. Response bodies are limited to 1 MiB.

## Docker socket security

**Access to the Docker socket effectively grants host administrator privileges.** A compromised container may be able to control other containers and access host files. Running as non-root, dropping capabilities, and using a read-only filesystem do not constrain that authority by themselves. Run only trusted code/images and minimize socket exposure. Stronger isolation requires a separate proxy or authorization layer restricting target containers and API operations.

## Known limitations and review notes

- Assumes one target and one watchdog. A lock protects a shared state file, but watchdogs using separate state volumes are not coordinated.
- Overload, queue latency, network outages, and incorrect authentication/model settings cannot be distinguished from a real hang. Tune timeouts, thresholds, and cooldown to production latency. Restarting interrupts in-flight requests.
- A POSIX SIGALRM overall deadline supplements requests' socket timeout, bounding DNS and slow body transfers. Execution must remain on the main thread. Windows and embedding alongside another SIGALRM user are unsupported.
- Recovery timing uses a monotonic clock during execution, while persisted restart history uses wall time. Large system clock changes can alter window/grace calculations after process restart.
- State write failures stop automatic restart attempts. History cannot be restored if the file is deleted or the persistent volume is replaced. I/O deadlines do not cover OS-level failures such as a disk hang.
- Docker daemon failures consume the limited budget and can lead to FAILED after recovery verification. The watchdog does not reset the daemon/GPU or reboot the host.
- Malformed HTTP/JSON responses count as failures; unexpected internal exceptions transition to FAILED. Raw HTTP bodies and exceptions are omitted to protect secrets.
- Webhook failures can delay an iteration by up to ALERT_TIMEOUT but do not terminate the main loop.
- A very short completion does not establish the health of every GPU function, long generation, or every replica.
- The last attempt allowed by MAX_RESTARTS receives a recovery opportunity. In FAILED, automatic probes/restarts stop while the process remains alive awaiting operator action.
- State corruption or persistence error alerts may recur when the process restarts. Alert delivery itself is not guaranteed.

## Future improvements (Phase 2, not implemented)

Prometheus running/waiting requests, generation/prompt tokens, and KV cache usage can be added through an independent collector and passed through a separate observation interface. The Controller currently makes decisions using only HTTP ProbeResult values, so metric collection failures are not coupled to recovery policy. Prometheus/Grafana, HAProxy draining, multiple instances, GPU reset, and host reboot remain future work.
