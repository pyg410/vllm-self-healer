# vLLM Self-Healer

[English](README.md) | **한국어**

**vLLM 프로세스는 살아 있지만 추론이 멈췄을 때, 이를 감지하고 컨테이너를 자동으로 재시작합니다.**

vLLM Self-Healer는 vLLM 옆에서 실행하는 경량 외부 watchdog입니다. /health 확인과 실제 생성 요청을 함께 사용하여, HTTP 서버는 응답하지만 추론이 완료되지 않는 장애를 감지합니다.

```text
/health: 200 OK + inference: timeout
                 ↓
       consecutive failure threshold
                 ↓
        restart policy evaluation
                 ↓
     Docker restart → recovery probes
```

단발 실패만으로 재시작하지 않습니다. 재시도 횟수를 제한하고 매 재시작 후 실제 추론 복구를 확인합니다. Watchdog 자체에는 GPU가 필요하지 않습니다.

## 주요 기능

- **실제 추론 검사:** 작은 요청으로 generation 경로의 응답 여부 확인
- **자동 복구:** 반복 실패 시 대상 Docker 컨테이너 재시작
- **복구 검증:** 모델 로딩을 기다린 뒤 두 probe의 성공 여부 확인
- **재시작 제한:** cooldown, rolling limit, 복구 없는 연속 시도 제한
- **상태 보존:** watchdog 재기동 후에도 재시작 이력과 FAILED 유지
- **운영 지원:** JSON 로그, 선택적 webhook, amd64·arm64 이미지

## 빠른 시작

기존 vLLM service가 있는 Compose 프로젝트에 watchdog을 추가합니다. 아래 예시는 Linux와 접근 가능한 호스트 Docker socket을 전제로 합니다.

이미지를 받습니다.

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:latest
```

Docker socket group ID를 확인합니다.

```bash
stat -c '%g' /var/run/docker.sock
```

프로젝트의 .env에 실제 제공하는 모델 이름과 socket group을 추가합니다.

```dotenv
VLLM_MODEL=your-served-model
DOCKER_SOCKET_GID=998
VLLM_API_KEY=
```

998은 실제 group ID로 교체합니다. API가 인증을 요구하면 VLLM_API_KEY도 설정합니다. 모델은 chat completions를 지원하고 chat template을 갖춰야 합니다.

기존 services 항목에 아래 service를 합치고, 최상위 volumes에 watchdog-data를 추가합니다.

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

공유 Docker network에서 vllm이 API service로 연결되는지 확인합니다. VLLM_CONTAINER_NAME은 실제 재시작할 컨테이너 이름 또는 ID로 설정합니다. Compose service 이름과 자동 생성된 컨테이너 이름은 다를 수 있습니다.

```bash
docker compose up -d vllm-self-healer
docker compose logs -f vllm-self-healer
```

새 watchdog 시작 및 vLLM 재시작 이후에는 기본 300초의 startup grace를 적용합니다. 이후 두 probe가 모두 성공하면 정상 감시를 시작합니다. 기존에 저장된 복구 제한 시각과 FAILED 상태는 유지합니다.

> Docker socket 접근은 호스트 Docker daemon을 제어할 수 있는 높은 권한입니다. 신뢰할 수 있는 코드와 이미지에만 부여하세요.

운영에서는 게시된 버전 또는 image digest를 고정합니다. 이미 게시된 빌드 ghcr.io/pyg410/vllm-self-healer:sha-440c4334도 사용할 수 있습니다. 아래 v0.1.0은 릴리스 절차의 예시이며 해당 버전이 게시되었다는 의미가 아닙니다.

## 장애 판단 기준

일반 감시 회차마다 두 검사를 별도로 수행합니다.

| Probe | 성공 조건 | 기본 제한 시간 |
|---|---|---|
| GET /health | HTTP 200 | 5초 |
| POST /v1/chat/completions | HTTP 200 및 정상 terminal completion JSON | 15초 |

추론 요청은 기본적으로 설정된 모델에 prompt=ping, max_tokens=1, temperature=0, stream=false를 사용합니다. Prompt와 token budget은 변경할 수 있습니다.

| Health | Inference | 해석 |
|---|---|---|
| 성공 | 성공 | 정상 |
| 성공 | 실패 | 추론 경로 이상 의심 |
| 실패 | 성공 | Health endpoint 이상 가능성 |
| 실패 | 실패 | 서비스 또는 연결 전반의 이상 가능성 |

**Health 성공·inference 실패는 첫 실패부터 매번 ALIVE_BUT_STALLED라는 reason으로 로그에 기록합니다.** 이는 진단용 reason이며 별도 watchdog 상태나 GPU hang의 확정 판정은 아닙니다. 정상 감시 중에는 상태가 SUSPECT로 전환됩니다.

두 probe는 독립적인 연속 실패 카운터를 가집니다. 성공은 해당 probe의 카운터만 초기화합니다. 기본 설정에서는 둘 중 하나가 3회 연속 실패하면 재시작 정책을 평가합니다. 예산과 cooldown 조건을 통과해야 실제 재시작합니다.

Completion 검증은 object=chat.completion, 비어 있지 않은 choices, 정수 choice index, content 필드가 있는 assistant message, finish_reason=stop 또는 length를 요구합니다. 즉시 EOS나 짧은 reasoning budget을 고려해 빈/null content는 허용합니다. 응답 구조를 검증하며 답변 품질을 평가하지는 않습니다. JSON 파싱 오류와 HTTP·연결·timeout 오류는 실패입니다.

## 복구 정책과 상태

```text
Startup → RECOVERING → 두 probe 성공 → HEALTHY
HEALTHY → probe 실패 → SUSPECT
SUSPECT → 임계치 + 재시작 정책 통과 → RESTARTING
RESTARTING → RECOVERING → 성공 → HEALTHY
RECOVERING → timeout → 재시도 정책 → 재시작 또는 FAILED
```

| 상태 | 의미 |
|---|---|
| HEALTHY | 두 probe 성공 |
| SUSPECT | Probe 실패 중이며, 임계치 도달 후 cooldown을 기다리는 경우도 포함 |
| RESTARTING | 시도를 기록하고 Docker 재시작 요청 |
| RECOVERING | Startup grace 대기 또는 준비 상태 검증 |
| FAILED | 자동 probe·재시작 중단, 운영자 조치 필요 |

재시작 후 STARTUP_GRACE_PERIOD(300초)를 기다리고 RECOVERY_CHECK_INTERVAL(10초) 간격으로 복구를 검사합니다. RECOVERY_TIMEOUT(600초)은 **grace가 끝난 뒤부터** 계산합니다. 같은 회차에 두 probe가 모두 성공해야 복구입니다.

세 가지 제한을 적용합니다.

| 제한 | 기본값 |
|---|---|
| 재시작 시도 시작 시각 사이 최소 간격 | 300초 |
| 최근 600초 내 최대 시도 | 3회 |
| 복구 성공 없이 최대 연속 시도 | 3회 |

마지막 허용 시도에도 복구 기회를 줍니다. 예산 소진 후 추가 재시작이 필요하면 FAILED로 고정됩니다. 복구 성공은 미복구 연속 시도 횟수를 초기화하지만 최근 재시작 시각은 보존합니다.

Docker daemon 장애와 결과가 불확실한 API timeout도 시도 횟수에 포함됩니다. Timeout이어도 Docker가 서버 측에서 재시작했을 수 있으므로 다시 요청하기 전에 복구부터 확인합니다.

동기 루프이므로 일반 회차는 health 요청 시간 + inference 요청 시간 + CHECK_INTERVAL만큼 걸립니다. 요청은 겹치지 않습니다. 복구 probe의 deadline은 남은 복구 시간으로 제한합니다.

## 복구 범위와 제한사항

복구 수단은 Docker 컨테이너 하나의 재시작입니다. CUDA·NCCL·드라이버·하드웨어 버그의 원인을 진단하거나 수리하지 않습니다.

- 재시작은 처리 중 요청을 끊습니다.
- 네트워크 장애, 잘못된 인증·모델 이름, 과부하도 probe 실패가 됩니다. 실제 서비스 지연에 맞춰 timeout과 임계치를 조정하세요.
- 짧은 completion만으로 모든 replica, 장시간 generation, 모든 GPU 기능의 정상을 보장하지 않습니다.
- Watchdog 하나는 컨테이너 하나를 관리합니다. 독립 state volume을 사용하는 여러 watchdog은 서로 조정되지 않습니다.
- GPU reset, 호스트 재부팅, traffic drain, Prometheus 기반 판단은 미구현입니다.

## 설정

모든 설정은 환경변수로 받으며 시간 단위는 초입니다. 양수여야 하지만 startup grace, cooldown, Docker stop timeout은 0을 허용합니다. DOCKER_API_TIMEOUT은 DOCKER_STOP_TIMEOUT보다 커야 합니다.

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

RECOVERY_TIMEOUT에는 startup grace가 포함되지 않습니다. MAX_RESTARTS는 rolling window와 미복구 연속 시도에 모두 적용합니다. Compose 전용 이미지·socket group 설정을 포함한 전체 예시는 [.env.example](.env.example)을 참고하세요.

## 로그와 알림

stdout에 JSON 로그를 출력합니다. 성공 probe는 DEBUG, 실패는 WARNING이며 상태 변경·복구는 INFO 이상입니다. 성공 검사까지 확인하려면 LOG_LEVEL=DEBUG로 설정합니다.

읽기 쉽게 들여쓰기한 probe 로그 예시입니다.

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

state는 검사 시점의 상태입니다. 뒤따르는 상태 전환 로그에 healthy → suspect가 기록됩니다. 재시작 로그에는 두 실패 카운터, reason, 재시작 횟수, 마지막 추론 성공 시각이 포함됩니다.

ALERT_WEBHOOK_URL을 설정하면 best-effort HTTP 알림을 사용합니다. 이벤트는 RESTART_TRIGGERED, RECOVERY_SUCCESS, RECOVERY_FAILED, MAX_RESTART_EXCEEDED입니다. 최초 준비 완료도 RECOVERY_SUCCESS를 보냅니다.

Webhook payload 예시:

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

Webhook 실패는 로그만 남기고 루프를 종료하지 않지만 최대 ALERT_TIMEOUT만큼 지연시킬 수 있습니다. 재전송 queue는 없습니다. API key, Authorization header, prompt, 응답 본문, exception 원문, webhook URL은 로그에서 제외합니다.

## 상태 저장과 FAILED 대응

/data/watchdog_state.json에 atomic replacement와 fsync로 저장합니다. 재시작 시도는 **Docker API 호출 전에** 기록합니다. 복구 시각, 미복구 연속 시도 횟수, 마지막 추론 성공 시각도 저장합니다.

파일 lock은 같은 상태 경로를 사용하는 동시 실행을 막습니다. 손상되거나 쓸 수 없는 상태 파일은 자동 재시작을 차단하며 손상 파일은 보존합니다. FAILED는 watchdog을 재시작해도 유지됩니다. 저장 오류가 있으면 FAILED 자체를 저장하지 못할 수도 있습니다.

의도적으로 초기화하려면:

1. Watchdog을 중지합니다.
2. 로그를 확인하고 근본 원인을 해결합니다.
3. 필요하면 상태 파일을 백업합니다.
4. Volume 전체가 아닌 watchdog 상태 JSON만 삭제합니다.
5. Watchdog을 다시 시작합니다.

이 작업은 재시작 예산도 초기화합니다. 단순한 watchdog 재시작으로는 초기화되지 않습니다.

SIGTERM/SIGINT는 대기를 깨우고 새 재시작을 막습니다. 진행 중 I/O는 deadline 안에 마무리한 뒤 상태 저장과 로그 flush를 수행합니다. stop_grace_period는 가장 긴 Docker/probe deadline과 alert 시간을 합친 값보다 넉넉하게 설정합니다. 예시는 90초입니다.

## 배포와 릴리스

### 로컬 빌드

빠른 시작의 게시 이미지는 clone 없이 사용합니다. 소스에서 배포하려면 [docker-compose.example.yml](docker-compose.example.yml)을 사용합니다.

```bash
git clone https://github.com/pyg410/vllm-self-healer.git
cd vllm-self-healer
cp .env.example .env
# Configure .env before starting.
docker compose -f docker-compose.example.yml up -d --build
```

실행 전에 .env를 설정합니다. 예시 vLLM service에는 호환되는 GPU 환경이 필요합니다. Watchdog 이미지만 빌드하려면 저장소 root에서 docker build -t vllm-self-healer:test .을 실행합니다.

Watchdog은 UID/GID 10001로 실행됩니다. Named volume은 /data 소유권을 사용하며 bind mount는 UID 10001에 쓰기 권한이 필요합니다. Rootless Docker는 socket 경로와 group ID를 조정합니다.

### GHCR과 버전 릴리스

[GitHub Actions](.github/workflows/docker-publish.yml)는 Buildx/QEMU와 layer cache로 linux/amd64·linux/arm64를 빌드합니다. 게시 전 amd64 컨테이너 테스트와 시작·종료를 검사하고, 게시 후 두 아키텍처의 pull 및 runtime import를 검증합니다.

| Push | 이미지 태그 |
|---|---|
| master | latest 및 sha-xxxxxxxx |
| v* 태그 | Git 태그 원문 및 sha-xxxxxxxx |

SHA는 앞 8자리를 사용합니다. 버전 태그 push는 latest를 바꾸지 않습니다. 게시는 기본 GITHUB_TOKEN과 contents: read, packages: write 권한을 사용하며 workflow용 별도 PAT는 필요하지 않습니다.

아래 v0.1.0 명령은 예시입니다. 실제 릴리스가 필요할 때 workflow가 포함된 커밋에서 새 태그를 만들고 Actions 성공 후 이미지를 받습니다.

```bash
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

```bash
docker pull ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

기존 Git 태그는 소급 빌드되지 않습니다. 릴리스 버전을 재사용하지 마세요. Base image와 의존성은 빌드 사이에 달라질 수 있으므로 불변 결과물이 필요하면 digest를 고정합니다.

익명 pull에는 GHCR package가 Public이어야 합니다. Public 저장소라고 package까지 자동 공개되지는 않습니다. 소유자는 GitHub Packages 설정에서 변경할 수 있습니다. Private package는 읽기 권한이 있는 인증정보와 docker login ghcr.io가 필요합니다. 수동 인증은 workflow의 GITHUB_TOKEN과 별개입니다.

### 폐쇄망 배포

외부망 PC에서 목적지 아키텍처에 맞는 게시 빌드를 받고 저장합니다.

```bash
docker pull --platform linux/amd64 ghcr.io/pyg410/vllm-self-healer:sha-440c4334
docker save -o vllm-self-healer-sha-440c4334.tar ghcr.io/pyg410/vllm-self-healer:sha-440c4334
```

필요하면 linux/arm64로 바꿉니다. 승인된 절차로 파일을 옮긴 뒤 폐쇄망 호스트에서 로드합니다.

```bash
docker load -i vllm-self-healer-sha-440c4334.tar
```

필요하면 사내 Nexus/Harbor로 retag·push하고 Compose의 image 경로를 바꿉니다. vLLM 이미지와 모델 가중치는 별도로 준비해야 합니다. 이 archive에는 watchdog만 포함됩니다.

## Docker socket 보안

**/var/run/docker.sock 접근은 사실상 호스트 Docker 관리자 수준의 권한입니다.** Privileged container 생성, 호스트 파일 mount, 다른 workload 제어가 가능할 수 있습니다.

Non-root, capability 제거, read-only filesystem은 추가 보호 수단이지만 socket 권한 자체를 제거하지 않습니다. 신뢰할 수 있는 이미지만 사용하세요. 더 강한 격리가 필요하면 authorization 계층에서 대상 컨테이너와 Docker 작업을 제한합니다. 권한 문제 해결을 위해 socket을 world-writable로 만들지 마세요.

## 배경과 upstream 사례

**프로세스가 살아 있는 것은 inference service를 제공하기 위한 필요조건이지만 충분조건은 아닙니다.** Docker restart policy는 프로세스 종료에 대응하며 HTTP health 응답만으로 generation 진행 여부를 보장할 수 없습니다.

관련 upstream 보고와 작업입니다.

| 참고 | 주제 |
|---|---|
| [Issue #52319](https://github.com/vllm-project/vllm/issues/52319) | Health/metrics는 응답하지만 generation이 멈춘 사례 보고 |
| [Issue #52247](https://github.com/vllm-project/vllm/issues/52247) | GPU synchronization에서 EngineCore가 멈춘 사례 |
| [Issue #42897](https://github.com/vllm-project/vllm/issues/42897) | 지속 트래픽 중 EngineCore hang |
| [Issue #36960](https://github.com/vllm-project/vllm/issues/36960) | GPU-aware readiness 검사 제안 |
| [PR #36451](https://github.com/vllm-project/vllm/pull/36451) | Alive-but-hung EngineCore 탐지 작업 |

설계 배경을 위한 참고 자료이며 watchdog이 모든 보고 사례를 재현하거나 해결한다는 의미는 아닙니다. vLLM 자체 health 기능을 외부 생성 검사로 보완합니다.

## 개발과 테스트

Linux/macOS의 Python 3.12를 권장합니다. Docker나 GPU 없이 테스트할 수 있으며 HTTP 테스트는 loopback listener를 엽니다.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
```

로컬 실행:

```bash
VLLM_MODEL=my-model VLLM_BASE_URL=http://localhost:8000 STATE_FILE=/tmp/watchdog-state.json .venv/bin/python -m watchdog.main
```

Probe 검증, 연속 실패, 복구, 예산, cooldown, 상태 저장·손상, Docker 오류, webhook, 종료 신호를 테스트합니다.

동기 HTTP client는 POSIX SIGALRM deadline을 사용하므로 main thread에서 실행해야 합니다. Windows 및 다른 SIGALRM 사용자와의 embedding은 지원하지 않습니다. 실행 중 복구는 monotonic clock, 저장 시각은 wall clock을 사용하므로 큰 시스템 시각 변경은 복원 시간에 영향을 줄 수 있습니다. OS 수준 disk hang은 HTTP/Docker deadline 범위 밖입니다.

## License

Apache License 2.0. [LICENSE](LICENSE)를 참고하세요.
