# Operational guide (v0.1.1)

[한국어](operations.ko.md) | [README](../README.md)

## Startup diagnostics

One JSON "configuration loaded" entry reports effective numeric settings, validated modes/policies and configured/not-configured flags. It is emitted even if LOG_LEVEL suppresses other INFO messages. URLs, paths, model/container names, prompts, API keys, headers and body values are omitted.

Unsupported names resembling watchdog settings produce WARNING and startup continues. For example STARTUP_GRACE_PEROID suggests STARTUP_GRACE_PERIOD. Values are never included. Unrelated system settings and documented Compose/Docker inputs are ignored. This is a heuristic, not a validator for every vLLM environment variable.

Startup failures identify error_type, a fixed stage (logging_setup, state_directory_and_lock, probe_server_bind, etc.) and an OS error code when available. Configuration errors identify setting names using safe summaries. Raw exception messages from I/O are not logged.

"persisted state restored" reports the saved state, selected backend, effective state, stored recovery_ready_at/recovery_deadline, restart history count and stored_recovery_timing_applied.

## Timing and when configuration applies

Environment variables are read **once per process startup**. Editing .env does not update a running process; recreate/restart it with the new environment.

| Setting | Used when | Default meaning in v0.1.1 |
|---|---|---|
| CHECK_INTERVAL | Between normal monitoring iterations | Wait 30 seconds after probes complete |
| STARTUP_GRACE_PERIOD | A new recovery cycle begins | Wait 300 seconds without probing |
| RECOVERY_TIMEOUT | A new recovery cycle begins | Allow 600 seconds of verification after grace |
| RESTART_COOLDOWN | Restart policy evaluation | At least 300 seconds between attempt start times |
| RESTART_WINDOW | Restart budget evaluation | Count attempts within the last 600 seconds |
| EVENT_WEBHOOK_* | Event dispatch | Use settings loaded at process startup |
| PRE/POST hook settings | Corresponding lifecycle action | Use settings loaded at process startup |

When RECOVERING is restored, saved recovery_ready_at and recovery_deadline take precedence over new grace/timeout values for that cycle. New values apply to subsequent cycles, or after intentional state reset. There is no early probing during grace in v0.1.1. FAILURE_THRESHOLD governs HEALTHY/SUSPECT, not RECOVERING: counters can exceed it while the recovery deadline is still running.

## Logging: choose an option

### Option A: application file logging plus stdout

Merge this into the existing Compose service and top-level volumes:

```yaml
services:
  vllm-watchdog:
    environment:
      LOG_FILE: /logs/watchdog.log
      LOG_MAX_BYTES: "10485760"
      LOG_BACKUP_COUNT: "5"
    volumes:
      - watchdog-logs:/logs
volumes:
  watchdog-logs:
```

Use your actual service name (the quick-start snippet uses vllm-self-healer). LOG_FILE is empty by default, meaning stdout only. Setting it enables a UTF-8 JSON RotatingFileHandler in addition to stdout. The defaults retain the active file plus five rotated backups of approximately 10 MiB each; an individual large entry can exceed that size. LOG_MAX_BYTES and LOG_BACKUP_COUNT must be positive.

The image prepares /logs for UID/GID 10001. A new named volume inherits that ownership; existing volumes and bind mounts must be writable by this user. The parent directory must exist. Invalid/unwritable paths fail startup with an identifiable logging_setup error. In Kubernetes, mount a writable log volume if the root filesystem is read-only. Do not let multiple watchdog processes rotate the same log file.

### Option B: stdout with Docker-managed rotation

Keep LOG_FILE empty and merge this under the watchdog service:

```yaml
logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "5"
```

These are alternatives; both are not required. Docker wraps each application JSON line in its own logging envelope.

## Informational event webhook

| Variable | Default |
|---|---|
| EVENT_WEBHOOK_URL | Empty: disabled |
| EVENT_WEBHOOK_METHOD | POST |
| EVENT_WEBHOOK_TIMEOUT | 5 seconds |
| EVENT_WEBHOOK_EVENTS | Empty: all events; otherwise comma-separated names |
| EVENT_WEBHOOK_HEADERS | {} |
| EVENT_WEBHOOK_BODY | {} |

Supported events: RESTART_TRIGGERED, RESTART_CONFIRMED, RECOVERY_SUCCESS, RECOVERY_FAILED, MAX_RESTART_EXCEEDED.

RESTART_CONFIRMED is emitted only after the Docker restart call succeeds or Kubernetes observes a new start-ID UUID. Arming /live=503 is not confirmation. A timed-out Docker call emits no confirmation even if later probes recover.

Example environment:

```dotenv
EVENT_WEBHOOK_URL=https://operations.example.com/events
EVENT_WEBHOOK_METHOD=POST
EVENT_WEBHOOK_EVENTS=RESTART_CONFIRMED,RECOVERY_FAILED,MAX_RESTART_EXCEEDED
EVENT_WEBHOOK_HEADERS={"Authorization":"Bearer REPLACE_ME"}
EVENT_WEBHOOK_BODY={"environment":"production","service_group":"inference"}
```

Static body fields are merged with standard fields: service, container, event, timestamp (UTC), recovery_mode, reason and restart_count. Standard fields win on collisions. Bodies and headers must be JSON objects; header values must be valid strings. Methods are limited to POST, PUT and PATCH. Invalid JSON, methods, event names or failure policies fail configuration validation without printing values.

Any HTTP 2xx response is success. No JSON response contract is required and remote response bodies are ignored. Timeouts, connection/protocol failures and non-2xx statuses log WARNING; informational delivery does not change recovery state. Requests are synchronous, finite-timeout, single-attempt and can delay the loop. There is no retry queue or templating engine.

The existing ALERT_WEBHOOK_URL/ALERT_TIMEOUT remains supported with its original payload and four events. It does not receive RESTART_CONFIRMED. Configuring both old and new event endpoints sends both independently.

## Lifecycle action hooks

Hooks perform optional actions; they are separate from event notifications. An external service can implement traffic drain/ready operations for HAProxy or another load balancer. No HAProxy API or shell commands are built into the watchdog.

For each prefix PRE_RESTART_WEBHOOK and POST_RECOVERY_WEBHOOK:

| Suffix | Default |
|---|---|
| _URL | Empty: disabled |
| _METHOD | POST |
| _TIMEOUT | 10 seconds |
| _HEADERS | {} |
| _BODY | {} |
| _FAILURE_POLICY | continue; also supports abort |

For example:

```dotenv
PRE_RESTART_WEBHOOK_URL=https://operations.example.com/drain
PRE_RESTART_WEBHOOK_BODY={"server":"inference-a"}
PRE_RESTART_WEBHOOK_FAILURE_POLICY=abort
POST_RECOVERY_WEBHOOK_URL=https://operations.example.com/ready
POST_RECOVERY_WEBHOOK_BODY={"server":"inference-a"}
POST_RECOVERY_WEBHOOK_FAILURE_POLICY=continue
```

Hooks use the same HTTP validation/metadata merge rules, with event=PRE_RESTART or POST_RECOVERY. Their payloads may include administrator-configured secrets and are never logged.

Normal order:

```text
policy allows an attempt → persist reservation
→ RESTART_TRIGGERED notification
→ PRE_RESTART action
→ persist backend request → backend restart
→ restart observed → RESTART_CONFIRMED notification
→ RECOVERING → both probes succeed
→ POST_RECOVERY action → HEALTHY → RECOVERY_SUCCESS notification
```

| Hook result | continue | abort |
|---|---|---|
| PRE_RESTART fails | Warn, proceed with restart | Enter persistent FAILED, do not call restart or emit confirmation |
| POST_RECOVERY fails | Warn, mark recovery successful | Enter persistent FAILED; no HEALTHY/readiness or RECOVERY_SUCCESS |

The reserved attempt remains counted when PRE aborts, to bound repeated external actions and ambiguous crashes. It is not a successful restart. POST abort also retains the unrecovered attempt count. In Kubernetes, FAILED gives /live=200 and /ready=503. Manual state reset is required after resolving an abort.

POST_RECOVERY runs after both probes succeed on initial/restored readiness too, matching the existing initial RECOVERY_SUCCESS behavior. It does not run after failed probes. With a hook configured, readiness remains false until the action finishes. A continue policy explicitly accepts that an external action may have failed.

During PRE, the durable state is SUSPECT without an armed backend request. A crash/restart cannot bypass the hook by restoring an already armed Kubernetes request. The original vLLM UUID is captured before PRE; if vLLM is replaced while the hook runs, the old request cannot kill the new instance. The confirmation deadline is finalized after PRE, before persistence and liveness exposure.

HTTP and persisted state cannot provide exactly-once external actions. A crash can cause duplicate hooks/events or missing notifications. Make endpoints idempotent and reconcile external traffic state independently. Notifications/hooks use only administrator-configured trusted URLs, never URLs supplied by inference traffic. Keep secrets out of committed .env files. Automatic redirects and environment proxy/.netrc authentication remain disabled.

Shutdown prevents subsequent actions once a pending request returns. Size stop_grace_period for the longest request plus any follow-on failure notifications (including legacy alerts). Defaults fit the existing 90 seconds; larger custom timeouts may require increasing it.

## Safe state reset

1. Stop the watchdog process/container and fix the root cause.
2. Back up /data/watchdog_state.json if diagnosis is needed.
3. Delete only that JSON file using a maintenance container or the mounted storage.
4. Start the watchdog with the intended environment.

Do not remove the .lock file while another process is running. Its existence does not indicate an active lock: flock is released when the process exits. Deleting a locked file can allow a second process to lock a different inode.

docker compose down normally preserves named volumes. docker compose down -v may erase other project data, including model caches; do not use it as a blanket reset. Prefer only the watchdog state file/volume.

Kubernetes emptyDir survives container/sidecar restarts but not Pod replacement. A suitable PVC can retain history across replacement. Do not share the same writable state file among replicas. The start-ID wrapper is mandatory; /live and /ready are for kubelet/Pod-local use and should not be exposed by a public Service.

## Deferred work

v0.2.0 work remains separate: probing during startup grace, explicit recovery/FAILED reason persistence, timezone configuration, Docker diagnostics and optional Docker dependency packaging. No Kubernetes API access or multi-Pod controller is introduced here.
