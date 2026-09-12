# vLLM Self-Healer

[English](README.md) | **한국어**

> **프로세스는 살아 있지만 추론이 멈춘(alive-but-stalled) vLLM 인스턴스를 감지하고 자동으로 복구합니다.**

**vLLM Self-Healer**는 Docker 환경에서 실행되는 vLLM을 위한 경량 외부 watchdog입니다.

일반적인 컨테이너 헬스 모니터링으로 놓칠 수 있는 다음과 같은 장애 상태를 감지합니다.

```text
Container       running
vLLM process    alive
GET /health     200 OK
Inference       stalled / timeout
```

단순히 프로세스나 HTTP 서버가 살아 있는지만 확인하지 않고, 작은 synthetic inference request를 실제로 전송하여 **추론이 정상적으로 진행되고 있는지(forward progress)** 확인합니다.

반복적인 실패가 감지되면 다음 작업을 수행할 수 있습니다.

* 문제가 발생한 vLLM 컨테이너 재시작
* 재시작 후 실제 추론 복구 여부 검증
* 무한 재시작 루프 방지
* watchdog 재시작 이후에도 restart history 유지
* 선택적 webhook 알림

watchdog 자체에는 GPU가 필요하지 않습니다.

---

## 왜 필요한가?

Docker restart policy는 프로세스가 종료되었을 때 유용합니다.

하지만 프로세스는 살아 있으면서 실제 서비스가 더 이상 진행되지 않는 상태는 복구하지 못합니다.

vLLM에서도 비슷한 상황이 발생할 수 있습니다.

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

이 경로의 더 깊은 계층에서 문제가 발생하더라도 HTTP 서버 자체는 계속 응답할 수 있습니다.

실제로 이러한 유형의 문제가 vLLM upstream에서도 보고되어 있습니다.

| Upstream 사례                   | 증상                                                                          |
| ----------------------------- | --------------------------------------------------------------------------- |
| `vllm-project/vllm #52319`    | `/health`와 `/metrics`가 HTTP 200을 반환하지만 generation이 완전히 중단됨                  |
| `vllm-project/vllm #52247`    | EngineCore가 GPU synchronization event에서 멈춰 있지만 `/health`는 계속 200을 반환        |
| `vllm-project/vllm #42897`    | 지속적인 트래픽 중 token generation이 멈추지만 HTTP 계층은 계속 응답                            |
| `vllm-project/vllm #36960`    | process liveness만으로 inference availability를 보장할 수 없어 GPU-aware readiness 제안 |
| `vllm-project/vllm PR #36451` | alive-but-hung 상태를 감지하기 위해 EngineCore forward-progress detection 추가         |

예를 들어 upstream issue #52247에서는 GPU kernel이 종료되지 않아 EngineCore가 살아 있는 상태로 멈추고, `/health`는 계속 HTTP 200을 반환했지만 실제 inference는 장시간 제공되지 않은 production incident가 보고되었습니다.

이 프로젝트는 이러한 빈틈을 외부에서 보완하는 것을 목표로 합니다.

단순히 다음만 확인하는 대신,

```text
프로세스가 살아 있는가?
```

한 가지를 더 확인합니다.

```text
이 인스턴스가 실제 inference request를 완료할 수 있는가?
```

---

## 동작 방식

각 monitoring cycle에서는 두 가지 probe를 독립적으로 실행합니다.

### 1. Health probe

```http
GET /health
```

vLLM engine의 기본적인 health 상태를 확인합니다.

### 2. Synthetic inference probe

```http
POST /v1/chat/completions
```

다음과 같은 최소 크기의 요청을 전송합니다.

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

이를 통해 단순한 HTTP 응답 여부보다 더 깊은 inference path를 확인합니다.

정상적인 inference probe는 다음 경로가 실제로 동작하고 있음을 외부에서 검증합니다.

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

health probe와 inference probe는 **서로 독립적인 failure counter**를 유지합니다.

따라서 `/health` 요청이 성공했다고 해서 inference failure counter가 초기화되지 않습니다.

이 구분이 alive-but-stalled 상태를 감지하는 핵심입니다.

---

## 상태 머신

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

두 probe가 모두 정상적으로 성공하고 있는 상태입니다.

### SUSPECT

하나 이상의 probe가 실패했지만 아직 설정된 failure threshold에 도달하지 않은 상태입니다.

### RESTARTING

watchdog이 restart attempt를 기록하고 Docker를 통해 대상 vLLM 컨테이너 재시작을 요청합니다.

### RECOVERING

설정된 startup grace period 이후 두 probe를 반복적으로 실행합니다.

다음 두 조건이 **같은 recovery iteration에서 모두 성공해야** 복구된 것으로 판단합니다.

```text
health probe    = success
inference probe = success
```

### FAILED

restart budget을 모두 소진했거나 persistent state를 안전하게 유지할 수 없는 경우 자동 복구를 중단합니다.

`FAILED` 상태는 의도적으로 persistent state에 저장됩니다.

따라서 watchdog 컨테이너만 재시작해서 recovery budget이 자동으로 초기화되지는 않습니다.

---

## Alive-but-stalled 감지

특히 중요한 상태는 다음과 같습니다.

```text
health     = success
inference  = failure
```

watchdog은 이 상태를 다음과 같이 기록합니다.

```text
ALIVE_BUT_STALLED
```

예를 들어:

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

재시작 이후에는:

```text
RECOVERING

health     success
inference  success

RECOVERY_SUCCESS
HEALTHY
```

처럼 실제 inference가 다시 정상적으로 완료되는지 확인합니다.

---

## Restart loop 방지

무조건적인 자동 재시작은 오히려 장애 상황을 악화시킬 수 있습니다.

따라서 vLLM Self-Healer는 **bounded recovery** 방식을 사용합니다.

restart timestamp를 기록하고 설정된 시간 범위 내에서 허용되는 restart attempt 수를 제한합니다.

예를 들어:

```text
MAX_RESTARTS=3
RESTART_WINDOW=600
```

이면 10분 동안 최대 3회의 restart attempt를 허용합니다.

마지막으로 허용된 restart 이후에도 정상적인 recovery verification을 수행합니다.

복구되지 않아 추가 restart가 필요한 상황이 되면 무한히 재시작하는 대신:

```text
FAILED
```

상태로 전환합니다.

별도의 restart cooldown도 적용하여 짧은 시간 동안 반복적인 restart call이 발생하지 않도록 합니다.

---

## Persistent state

Restart history는 기본적으로 다음 경로에 저장됩니다.

```text
/data/watchdog_state.json
```

state는 atomic file replacement와 `fsync`를 사용하여 저장됩니다.

restart attempt는 **Docker restart API를 호출하기 전에** 먼저 기록됩니다.

이는 Docker API timeout이 발생했다고 해서 실제 서버 측 restart까지 실패했다고 단정할 수 없기 때문입니다.

결과가 불확실한 경우 watchdog은 즉시 또 재시작하지 않고 recovery 상태를 먼저 검증합니다.

state corruption이나 persistence failure가 발생한 경우 restart history를 조용히 폐기하는 대신 자동 restart를 차단합니다.

의도적으로 `FAILED` 상태를 초기화하려면:

1. watchdog을 중지합니다.
2. 근본적인 장애 원인을 해결합니다.
3. 필요하면 state file을 백업합니다.
4. watchdog state JSON 파일만 삭제합니다.
5. watchdog을 다시 시작합니다.

이 과정에서 restart budget도 초기화됩니다.

---

## 주요 기능

* **alive-but-stalled vLLM 인스턴스 감지**
* `/health` probe
* 실제 synthetic inference probe
* health/inference 독립 failure counter
* consecutive failure threshold
* Docker 컨테이너 자동 재시작
* startup grace period
* 재시작 후 recovery verification
* restart cooldown
* rolling restart budget
* restart-loop protection
* persistent recovery state
* 선택적 webhook alert
* structured JSON logging
* graceful SIGTERM / SIGINT 처리
* non-root container
* read-only container 환경 지원
* `linux/amd64`
* `linux/arm64`
* watchdog 자체는 GPU 불필요

---

# 빠른 시작

## 이미지 Pull

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:latest
```

production 환경에서는 release version 사용을 권장합니다.

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

완전히 동일한 artifact를 고정해야 한다면 image digest를 사용할 수 있습니다.

---

## Docker Compose

기존 vLLM service 옆에 watchdog을 추가합니다.

```yaml
services:

  vllm:
    # 기존 vLLM 설정

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

Docker socket의 group ID를 확인합니다.

```bash
stat -c '%g' /var/run/docker.sock
```

실행:

```bash
docker compose up -d vllm-self-healer
```

로그 확인:

```bash
docker compose logs -f vllm-self-healer
```

두 컨테이너는 동일한 Docker network를 통해 서로 통신할 수 있어야 합니다.

`VLLM_CONTAINER_NAME`에는 watchdog이 실제로 재시작할 vLLM 컨테이너를 지정해야 합니다.

---

# 설정

모든 설정은 environment variable을 통해 지정합니다.

시간 관련 설정의 단위는 초입니다.

| Variable                  |                     Default | 설명                                      |
| ------------------------- | --------------------------: | --------------------------------------- |
| `VLLM_BASE_URL`           |          `http://vllm:8000` | vLLM API base URL                       |
| `VLLM_CONTAINER_NAME`     |                      `vllm` | 재시작할 Docker container name 또는 ID        |
| `VLLM_MODEL`              |                          필수 | vLLM이 제공하는 정확한 model name               |
| `VLLM_API_KEY`            |                       empty | 선택적 Bearer token                        |
| `PROBE_PROMPT`            |                      `ping` | synthetic inference prompt              |
| `PROBE_MAX_TOKENS`        |                         `1` | 최대 generation token 수                   |
| `CHECK_INTERVAL`          |                        `30` | 일반 monitoring interval                  |
| `HEALTH_TIMEOUT`          |                         `5` | `/health` request deadline              |
| `INFERENCE_TIMEOUT`       |                        `15` | inference probe deadline                |
| `FAILURE_THRESHOLD`       |                         `3` | restart 전 연속 실패 횟수                      |
| `STARTUP_GRACE_PERIOD`    |                       `300` | watchdog startup/restart 후 grace period |
| `RECOVERY_CHECK_INTERVAL` |                        `10` | recovery probe interval                 |
| `RECOVERY_TIMEOUT`        |                       `600` | 최대 recovery verification 시간             |
| `RESTART_COOLDOWN`        |                       `300` | restart attempt 사이 최소 간격                |
| `RESTART_WINDOW`          |                       `600` | rolling restart history window          |
| `MAX_RESTARTS`            |                         `3` | 최대 restart attempt                      |
| `DOCKER_STOP_TIMEOUT`     |                        `30` | Docker stop timeout                     |
| `DOCKER_API_TIMEOUT`      |                        `60` | 전체 Docker API deadline                  |
| `ALERT_WEBHOOK_URL`       |                       empty | 선택적 webhook endpoint                    |
| `ALERT_TIMEOUT`           |                         `5` | webhook request deadline                |
| `STATE_FILE`              | `/data/watchdog_state.json` | persistent watchdog state               |
| `LOG_LEVEL`               |                      `INFO` | logging level                           |

전체 설정값은 `.env.example`에서도 확인할 수 있습니다.

---

# Probe 검증 방식

## Health validation

health probe는 `/health` 요청의 정상 응답을 확인합니다.

## Inference validation

generation request는 단순히 HTTP 200만 반환한다고 성공으로 처리하지 않습니다.

정상적인 OpenAI-compatible terminal chat completion 구조인지 확인합니다.

예를 들어 다음 항목을 검증합니다.

* HTTP status
* JSON object 구조
* completion object type
* `choices`
* choice index
* assistant message
* terminal `finish_reason`

아주 작은 token budget에서 immediate EOS가 발생하거나 reasoning model의 특수한 응답이 발생할 수 있으므로, 정상적인 terminal completion 구조라면 assistant content가 empty 또는 null이어도 성공으로 처리할 수 있습니다.

---

# Timing 동작

watchdog은 **single synchronous control loop**로 동작합니다.

probe 요청은 서로 겹쳐서 실행되지 않습니다.

따라서 최악의 경우 일반 monitoring iteration은 대략:

```text
iteration duration
≈ health probe
+ inference probe
+ CHECK_INTERVAL
```

이 됩니다.

recovery 중에는 각 I/O deadline이 남아 있는 전체 recovery deadline을 초과하지 않도록 제한됩니다.

이 설계는 고빈도 concurrent probing보다 예측 가능한 recovery 동작을 우선합니다.

---

# Logging

로그는 structured JSON 형태로 stdout에 출력됩니다.

대표적인 event:

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

restart event에는 진단을 위해 다음과 같은 정보가 포함될 수 있습니다.

* reason
* health failure count
* inference failure count
* restart count
* 마지막 successful inference timestamp

민감한 데이터는 의도적으로 로그에서 제외합니다.

다음 정보는 기록하지 않습니다.

* API key
* Authorization header
* inference prompt
* response body
* webhook URL

---

# Alert

선택적으로 webhook을 통해 주요 recovery event를 전달할 수 있습니다.

지원하는 event:

```text
RESTART_TRIGGERED
RECOVERY_SUCCESS
RECOVERY_FAILED
MAX_RESTART_EXCEEDED
```

alert delivery는 best-effort 방식입니다.

webhook 전송에 실패해도 monitoring control loop는 계속 동작합니다.

---

# Graceful shutdown

`SIGTERM`과 `SIGINT`가 전달되면 대기 중인 interval을 중단하고 새로운 restart attempt를 방지합니다.

진행 중인 I/O는 설정된 deadline 안에서 종료된 후 state persistence와 shutdown 절차를 수행합니다.

Docker Compose에서는 가장 긴 probe/alert timeout보다 충분히 긴 `stop_grace_period`를 설정하는 것이 좋습니다.

---

# Docker socket 보안

> **중요:** `/var/run/docker.sock` 접근 권한은 사실상 host Docker에 대한 관리자 수준의 권한을 제공합니다.

Docker socket에 접근할 수 있는 process는 잠재적으로 다음 작업을 수행할 수 있습니다.

* privileged container 생성
* host filesystem mount
* 다른 container 조회
* 다른 Docker workload 제어

watchdog을 non-root로 실행하고 Linux capability를 제거하거나 read-only filesystem을 사용하는 것은 유용한 defense-in-depth 조치입니다.

하지만 이러한 설정만으로 **Docker socket 자체가 제공하는 권한까지 제한되는 것은 아닙니다.**

Docker socket에 직접 접근하는 container에서는 반드시 신뢰할 수 있는 code와 image만 사용해야 합니다.

더 강한 isolation이 필요한 경우 watchdog과 Docker daemon 사이에 authorization proxy를 배치하고 대상 container와 필요한 Docker operation만 허용하는 방식을 고려하십시오.

socket permission 문제를 해결하기 위해 Docker socket을 world-writable로 변경하지 마십시오.

---

# 테스트

테스트에는 Docker나 GPU가 필요하지 않습니다.

```bash
python3 -m venv .venv

.venv/bin/pip install -r requirements.txt

.venv/bin/python -m unittest discover -v
```

로컬 실행:

```bash
VLLM_MODEL=my-model \
VLLM_BASE_URL=http://localhost:8000 \
STATE_FILE=/tmp/watchdog-state.json \
.venv/bin/python -m watchdog.main
```

test suite에는 다음 시나리오가 포함됩니다.

* healthy operation
* single probe failure
* consecutive failure
* inference-only failure
* health-only failure
* alive-but-stalled detection
* recovery success
* recovery timeout
* restart limit
* cooldown
* state persistence
* corrupted state
* Docker daemon error
* webhook failure
* graceful shutdown

---

# Container Image

prebuilt image는 GitHub Container Registry에 배포됩니다.

```text
ghcr.io/pyg410/vllm-self-healer
```

지원 architecture:

```text
linux/amd64
linux/arm64
```

watchdog 자체에서는 GPU 연산을 수행하지 않습니다.

별도로 실행되는 vLLM 환경에는 당연히 호환되는 GPU 환경이 필요합니다.

---

# Release

GitHub Actions publishing workflow는 다음 tag를 생성합니다.

```text
master push
    ├─ latest
    └─ sha-xxxxxxxx

v* tag
    ├─ exact version tag
    └─ sha-xxxxxxxx
```

예를 들어 `v0.1.0`을 release하려면:

```bash
git tag -a v0.1.0 -m "Release v0.1.0"

git push origin v0.1.0
```

이후:

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

production에서는 `latest`보다 version tag 또는 immutable image digest 사용을 권장합니다.

---

# 폐쇄망 배포

인터넷에 연결된 환경에서 destination architecture에 맞는 이미지를 pull합니다.

```bash
docker pull --platform linux/amd64 \
  ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

tar 파일로 저장합니다.

```bash
docker save \
  -o vllm-self-healer-v0.1.0.tar \
  ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

승인된 절차를 통해 폐쇄망으로 파일을 전달한 뒤:

```bash
docker load -i vllm-self-healer-v0.1.0.tar
```

로 불러올 수 있습니다.

필요한 경우 load한 이미지를 다시 tag하여 사내 Nexus, Harbor 또는 다른 OCI-compatible registry에 배포할 수도 있습니다.

---

# 알려진 제한사항

vLLM Self-Healer의 책임 범위는 의도적으로 제한되어 있습니다.

> **Inference forward progress의 상실을 감지하고 제한된 범위에서 Docker-level recovery를 수행합니다.**

근본적인 vLLM, CUDA, NCCL, driver 또는 hardware bug 자체를 진단하거나 수정하는 도구는 아닙니다.

주요 제한사항:

* 하나의 watchdog은 하나의 vLLM container를 대상으로 합니다.
* restart 시 처리 중이던 request는 중단됩니다.
* network outage가 inference stall과 유사하게 보일 수 있습니다.
* 잘못된 authentication 또는 model configuration도 probe failure를 발생시킬 수 있습니다.
* 극심한 overload에서는 engine 자체가 정상이어도 inference probe가 timeout될 수 있습니다.
* 따라서 timeout과 threshold는 실제 production latency 분포에 맞게 조정해야 합니다.
* 모든 GPU/driver 장애가 container restart만으로 복구되는 것은 아닙니다.
* 일부 장애는 GPU reset, node isolation, driver recovery 또는 host reboot가 필요할 수 있습니다.
* Docker socket 접근에는 강한 보안 권한이 따릅니다.
* 서로 다른 state volume을 사용하는 여러 watchdog instance는 서로 조정되지 않습니다.

restart budget을 두는 이유도 container restart만으로 복구되지 않는 host-level failure에서 무한 restart loop가 발생하는 것을 막기 위해서입니다.

---

# 설계 철학

이 프로젝트에서는 다음 두 개념을 명확하게 구분합니다.

```text
liveness
```

그리고:

```text
forward progress
```

프로세스가 살아 있는 것은 inference service를 제공하기 위한 **필요조건**입니다.

하지만 **충분조건은 아닙니다.**

따라서 watchdog은 실제 generation request가 성공적으로 완료되는 것을 serving path가 정상 동작하고 있다는 가장 강한 외부 신호로 사용합니다.

이 프로젝트는 vLLM 자체의 health mechanism을 대체하기 위한 것이 아닙니다.

**프로세스는 살아 있지만 inference가 멈춘 상황에서도 복구가 필요한 운영 환경을 위한 추가적인 safety layer입니다.**

---

# License

Apache License 2.0.

자세한 내용은 `LICENSE`를 참고하십시오.
