# vLLM Self-Healer

[English](README.md) | **한국어**

Docker Compose에서 실행 중인 vLLM의 **프로세스는 살아 있지만 inference가 멈춘 상태**를 탐지하고 복구하는 독립 Python watchdog입니다. 단일 동기 프로세스이며 별도 HTTP 서버는 없습니다.

## Architecture

```text
watchdog container
  ├─ GET /health ────────────────┐
  ├─ POST /v1/chat/completions ──┤→ vLLM container → EngineCore → GPU → decode
  ├─ Controller + RestartPolicy ← 각각의 ProbeResult
  ├─ Docker SDK → Docker socket → 대상 container restart
  ├─ StateStore → /data/watchdog_state.json
  └─ Alert → optional HTTP webhook
```

Docker의 restart 정책은 실행 중인 프로세스의 inference 진행 여부를 확인하지 않습니다. /health 역시 실제 생성 요청의 성공을 대신할 수 없습니다. Synthetic inference probe는 별도로 고정 prompt와 작은 token budget을 보내 HTTP 입력, scheduler, model execution, decode, 응답까지 완료되는지 확인합니다.

[공식 vLLM API 문서](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/)의 chat completions 형식을 사용합니다. Chat template이 있는 생성 모델이 필요합니다. 정상 응답은 HTTP 200, JSON object=chat.completion, 비어 있지 않은 choices, 정수 index, assistant message 및 stop/length finish_reason을 요구합니다. 즉시 EOS 또는 reasoning 모델의 짧은 budget을 고려해 정상 terminal 구조의 빈 content/null은 허용합니다.

## 상태와 restart policy

- 최초 실행: RECOVERING → startup grace → 두 probe 성공 → HEALTHY.
- 정상 감시: 하나라도 실패하면 SUSPECT. 두 probe를 매 iteration에서 독립적으로 실행합니다.
- 각각의 연속 실패 카운터가 FAILURE_THRESHOLD에 도달하면 재시작을 시도합니다.
- health=true/inference=false는 ALIVE_BUT_STALLED로 기록하고 상세 timeout/HTTP/응답 오류도 별도로 기록합니다.
- health=false/inference=true도 health 카운터가 임계치에 도달하면 재시작합니다.
- 성공은 **해당 probe의 카운터만** 0으로 초기화합니다. /health 성공으로 inference 장애 카운터를 지우면 hang을 탐지할 수 없기 때문입니다.
- RESTARTING → Docker 호출 → RECOVERING. STARTUP_GRACE_PERIOD 동안 기다린 뒤, RECOVERY_TIMEOUT 동안 RECOVERY_CHECK_INTERVAL 간격으로 확인합니다. Timeout은 grace **이후부터** 계산합니다.
- 복구는 두 probe가 같은 iteration에 성공한 경우에만 인정합니다. 복구 timeout은 다시 재시작 정책을 거칩니다.
- RESTART_COOLDOWN은 재시작 시도 시작 시간 사이의 최소 간격입니다. 두 probe가 회복하면 cooldown 중에도 HEALTHY로 돌아갑니다.

각 주기는 작업 **완료 후** 대기 시간입니다. 요청은 겹치지 않으며 정상 감시 iteration은 두 probe timeout의 합만큼 추가로 걸릴 수 있습니다. 복구 중에는 남은 복구 시간으로 각 HTTP deadline을 줄입니다.

### 무한 재시작 방지

최근 RESTART_WINDOW 내 MAX_RESTARTS회까지 시도를 허용합니다. 마지막 허용 시도도 복구 검증을 받으며, 그 후 추가 재시작이 필요하면 FAILED로 고정됩니다. 또한 **복구 성공 없이 MAX_RESTARTS회 시도**하면 시간 구간이 지나도 더 재시작하지 않습니다. 긴 모델 로딩으로 rolling window가 만료되는 반복을 방지합니다. 검증 성공은 연속 복구 시도 횟수만 초기화하며 최근 재시작 이력은 유지합니다.

이력은 deque로 관리하며 JSON에 atomic replace + fsync로 저장합니다. Cooldown이 window보다 길어도 적용되도록 마지막 timestamp는 보존합니다. Docker API 호출 **전에** 시도를 저장하므로 daemon unavailable, permission error, timeout도 예산을 소비합니다. Docker timeout은 서버 측 성공 여부가 불명확하므로 즉시 재호출하지 않고 복구를 확인합니다. [Docker SDK](https://docker-py.readthedocs.io/en/stable/containers.html)의 stop timeout과 전체 API deadline을 별도로 설정합니다.

FAILED도 저장하므로 watchdog 컨테이너를 다시 시작해도 자동 해제되지 않습니다. 파일 손상/읽기/쓰기 오류는 재시작을 차단하고 alert를 시도합니다. 손상 파일은 진단을 위해 덮어쓰지 않습니다. 상태 저장 경로의 lock으로 같은 파일을 사용하는 중복 프로세스를 방지합니다.

FAILED 해제: watchdog을 정지하고 원인을 해결한 뒤 상태 JSON을 백업하고 해당 파일만 삭제하여 재기동합니다. 이 작업은 재시작 예산도 초기화합니다. 다른 데이터가 있는 volume 전체를 삭제하지 마세요.

## Configuration

모든 변수는 환경변수로 읽습니다. 시간 단위는 초이며 양수여야 합니다(grace, cooldown, Docker stop timeout은 0 허용). 잘못된 값은 시작 시 거부합니다.

| 환경변수 | 기본값 | 의미 |
|---|---|---|
| VLLM_BASE_URL | http://vllm:8000 | API base URL |
| VLLM_CONTAINER_NAME | vllm | Docker 대상 이름/ID |
| VLLM_MODEL | 필수 | API에 제공되는 정확한 model 이름 |
| VLLM_API_KEY | 빈 값 | Bearer 인증, 로그 출력 금지 |
| PROBE_PROMPT | ping | 고정 synthetic prompt |
| PROBE_MAX_TOKENS | 1 | 생성 token budget |
| CHECK_INTERVAL | 30 | 정상 감시 대기 |
| HEALTH_TIMEOUT | 5 | /health 전체 요청 deadline |
| INFERENCE_TIMEOUT | 15 | inference 전체 요청 deadline |
| FAILURE_THRESHOLD | 3 | probe별 연속 실패 임계치 |
| STARTUP_GRACE_PERIOD | 300 | 최초/재시작 후 모델 로딩 유예 |
| RECOVERY_CHECK_INTERVAL | 10 | 복구 확인 간격 |
| RECOVERY_TIMEOUT | 600 | grace 후 복구 제한 시간 |
| RESTART_COOLDOWN | 300 | 재시작 시도 최소 간격 |
| RESTART_WINDOW | 600 | rolling restart window |
| MAX_RESTARTS | 3 | window 내/미복구 연속 최대 시도 |
| DOCKER_STOP_TIMEOUT | 30 | 강제 종료 전 대기 |
| DOCKER_API_TIMEOUT | 60 | Docker 작업 전체 deadline, stop timeout보다 커야 함 |
| ALERT_WEBHOOK_URL | 빈 값 | 빈 값이면 alert 비활성 |
| ALERT_TIMEOUT | 5 | webhook 전체 요청 deadline |
| STATE_FILE | /data/watchdog_state.json | 영속 상태 경로 |
| LOG_LEVEL | INFO | DEBUG/INFO/WARNING/ERROR/CRITICAL |

Compose 전용 변수: VLLM_IMAGE(예시는 latest, 운영에서는 검증한 버전/digest로 고정), DOCKER_SOCKET_GID(호스트 socket의 숫자 group ID). .env.example에 전체 설정이 있습니다.

## Docker 이미지 설치

빌드된 watchdog 이미지는 **ghcr.io/pyg410/vllm-self-healer**에 게시됩니다. 저장소를 clone하거나 직접 빌드하지 않고 받을 수 있습니다. linux/amd64와 linux/arm64를 빌드하며 watchdog 자체는 GPU 연산을 하지 않습니다. 별도로 운영하는 vLLM에는 호환되는 GPU 환경이 필요합니다.

기본 브랜치의 최신 빌드:

```sh
docker pull ghcr.io/pyg410/vllm-self-healer:latest
```

운영환경에서는 릴리스 버전 사용을 권장합니다.

```sh
docker pull ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

v0.1.0 명령어는 릴리스 예시입니다. 해당 Git 태그를 push하고 게시 workflow가 성공한 뒤에 사용할 수 있습니다. Workflow를 추가해도 기존 v0.0.1 등의 태그를 소급하여 빌드하지 않습니다.

### 빌드된 이미지를 Compose에서 사용

기존 vllm service가 있는 Compose 프로젝트에 다음 service와 volume을 추가합니다. VLLM_MODEL에는 실제 제공하는 모델 이름, DOCKER_SOCKET_GID에는 `stat -c '%g' /var/run/docker.sock`으로 확인한 숫자 group ID를 설정합니다. 인증을 사용하는 API라면 VLLM_API_KEY도 설정합니다. 두 service는 같은 network에 있어야 합니다.

```yaml
services:
  vllm-watchdog:
    image: ghcr.io/pyg410/vllm-self-healer:v0.1.0
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

합친 설정을 compose.yml로 저장하고 환경변수를 설정한 뒤 실행합니다.

```sh
docker compose pull vllm-watchdog
docker compose up -d vllm-watchdog
```

소스 빌드용 [docker-compose.example.yml](docker-compose.example.yml)도 유지합니다. 해당 파일에서 빌드된 이미지를 사용하려면 watchdog의 `build: .`을 위 `image:` 항목으로 교체하고 `--build`를 생략합니다. 아래의 로컬 빌드 방법도 그대로 사용할 수 있습니다.

### 자동 게시와 버전 릴리스

[게시 workflow](.github/workflows/docker-publish.yml)는 Docker Buildx, arm64용 QEMU, GitHub Actions layer cache를 사용합니다. amd64 이미지를 빌드하고 이미지 내부 테스트 및 기본 CMD, non-root 시작, 상태 저장, 정상 종료를 확인한 뒤 게시합니다. 게시 후에는 digest로 이미지를 pull하여 두 아키텍처에서 Python import를 확인합니다. 실제 vLLM/GPU 환경을 검증하는 과정은 아닙니다.

| Push 이벤트 | 생성 태그 |
|---|---|
| 기본 브랜치 master | latest 및 sha-xxxxxxxx |
| v* 패턴의 Git 태그 | Git 태그 원문(예: v0.1.0) 및 sha-xxxxxxxx |

SHA 태그는 커밋 SHA의 앞 8자리입니다. 버전 태그 push는 latest를 변경하지 않습니다. 저장소 기본 브랜치 이름을 바꾸면 workflow의 branch filter도 수정해야 합니다. 릴리스 버전은 재사용하지 마세요. 의존성과 base image 업데이트를 허용하므로 태그를 다시 빌드하면 이미지가 달라질 수 있습니다. 정확히 동일한 결과물이 필요하면 image digest를 고정합니다.

릴리스할 커밋에 workflow가 포함된 것을 확인한 뒤:

```sh
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

저장소 Actions 탭에서 **Build and Push Docker Image** 성공을 확인하고 이미지를 받습니다.

```sh
docker pull ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

게시는 기본 GITHUB_TOKEN으로 인증하며 contents: read와 packages: write만 부여합니다. Workflow용 별도 PAT나 repository secret은 필요하지 않습니다. 저장소/조직 정책에서 GitHub Actions와 package 게시가 허용되어야 합니다. Package가 이미 있다면 이 저장소에 Actions 쓰기 권한이 있는지도 확인합니다.

### Public과 Private package

새 GHCR package는 기본적으로 private입니다. 인증 없는 pull을 허용하려면 GitHub Packages의 해당 package에서 **Package settings → Danger Zone → Change visibility**를 열어 **Public**으로 변경합니다. 저장소가 public이어도 package가 자동으로 public이 되지는 않습니다. Public package는 위 docker pull 명령만으로 받을 수 있습니다.

Private package는 접근 권한이 있는 계정으로 인증합니다.

```sh
docker login ghcr.io
```

로컬에서 수동 로그인할 때는 read:packages 권한의 적절한 personal access token(classic)을 비밀번호로 사용하며 소스 파일이나 로그에 넣지 않습니다. 이 로컬 pull 인증정보는 workflow가 자동으로 사용하는 GITHUB_TOKEN과 별개입니다. [GitHub Container registry 문서](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)를 참고하세요.

### 폐쇄망 반입

외부망 PC에서 목적지 아키텍처에 맞춰 pull하고 저장합니다(필요하면 linux/arm64로 변경).

```sh
docker pull --platform linux/amd64 ghcr.io/pyg410/vllm-self-healer:v0.1.0
docker save \
  -o vllm-self-healer-v0.1.0.tar \
  ghcr.io/pyg410/vllm-self-healer:v0.1.0
```

허용된 절차로 tar 파일을 옮긴 뒤 폐쇄망 서버에서:

```sh
docker load -i vllm-self-healer-v0.1.0.tar
```

Archive에는 외부망 PC에서 pull한 아키텍처가 들어가므로 목적지와 일치시켜야 합니다. 사내 Nexus 또는 Harbor가 있다면 로드한 이미지를 retag하고 push할 수 있습니다(예시 registry/project를 실제 주소로 변경).

```sh
docker tag ghcr.io/pyg410/vllm-self-healer:v0.1.0 registry.example.com/ai/vllm-self-healer:v0.1.0
docker push registry.example.com/ai/vllm-self-healer:v0.1.0
```

Compose의 image 경로도 변경합니다. 폐쇄망용 vLLM 이미지와 모델 가중치는 별도로 준비해야 합니다. 이 archive에는 watchdog만 들어 있습니다.

## Docker Compose 실행

Linux Docker Engine, NVIDIA Container Toolkit, GPU 및 GPU device reservation을 지원하는 Docker Compose가 필요합니다.

```sh
cp .env.example .env
stat -c '%g' /var/run/docker.sock
# .env의 DOCKER_SOCKET_GID에 위 숫자를 입력하고 모델/이미지/timeout을 조정합니다.
docker compose -f docker-compose.example.yml up -d --build
docker compose -f docker-compose.example.yml logs -f vllm-watchdog
```

예시는 작은 chat 모델을 다운로드하고 GPU에서 실행합니다. 모델 접근 권한과 GPU 메모리를 확인하세요. API는 Compose 내부 network에만 노출합니다. 이미 실행 중인 Compose에는 watchdog service와 watchdog-data volume을 복사하고 build 경로를 이 프로젝트로 지정합니다. 두 service가 같은 network에 있어야 하며 VLLM_CONTAINER_NAME은 실제 대상과 일치해야 합니다. 로컬 모델 경로라면 vLLM service에 모델 volume을 추가합니다.

.env의 VLLM_API_KEY를 예시의 vLLM과 watchdog에 함께 전달합니다. 인증 오류나 model 이름 오류도 장애로 집계되므로 배포 전 일치 여부를 확인하세요. depends_on은 준비 완료를 보장하지 않으므로 watchdog 자체가 초기 복구 유예를 적용합니다.

이미지는 UID/GID 10001로 실행합니다. Named volume은 /data 소유권을 사용합니다. Bind mount 사용 시 디렉터리에 UID 10001 쓰기 권한을 부여해야 합니다. Socket group은 group_add로 추가하며 rootless Docker는 socket mount 경로와 GID를 조정해야 합니다. 권한 해결을 위해 socket을 world-writable로 만들지 마세요.

## 테스트와 로컬 실행

Python 3.12 권장(Linux/macOS). Docker나 GPU 없이 unittest로 실행할 수 있습니다.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
VLLM_MODEL=my-model VLLM_BASE_URL=http://localhost:8000 STATE_FILE=/tmp/watchdog-state.json .venv/bin/python -m watchdog.main
```

HTTP client, Docker manager, 시계, alert, state store가 주입 가능하며 정책 테스트는 가상 시간으로 실행합니다. 정상, 단발/연속 timeout, health 단독 실패, 복구 성공/실패, 재시작 제한, 성공 후 카운터 초기화, 이력 복원과 손상, 저장 실패, daemon 장애, cooldown, 종료, webhook 장애를 검사합니다.

SIGTERM/SIGINT는 대기를 즉시 깨우고 새 재시작을 막습니다. 진행 중인 I/O는 deadline까지 마무리한 뒤 상태 저장, client close, logging flush 후 종료합니다. Compose stop_grace_period는 가장 긴 요청 deadline과 alert 시간을 합친 값보다 넉넉하게 설정합니다(기본 90초).

## 로그와 alert

stdout에 JSON logging을 출력합니다. 정상 probe는 DEBUG, 실패는 WARNING, 상태 전환/재시작/복구는 INFO 이상입니다. 재시작 로그에는 timestamp, reason, health/inference 실패 횟수, 마지막 inference 성공의 Unix timestamp와 저장된 재시작 횟수가 포함됩니다.

Webhook 이벤트: RESTART_TRIGGERED, RECOVERY_SUCCESS, RECOVERY_FAILED, MAX_RESTART_EXCEEDED. Payload는 service, container, event, reason, restart_count, ISO timestamp입니다. 최초 준비 성공도 RECOVERY_SUCCESS입니다. Docker 호출 실패/복구 timeout/내부 오류로 FAILED가 된 경우도 RECOVERY_FAILED를 보냅니다. 전송 실패는 로그만 남기고 제어 루프는 계속됩니다. 전송은 best-effort이며 재전송 queue는 없습니다.

Authorization header, API key, prompt, 응답 본문, exception 원문 및 webhook URL은 로그에 기록하지 않습니다. HTTP redirect와 환경 proxy/.netrc 자동 인증은 사용하지 않습니다. 응답 본문은 1 MiB로 제한합니다.

## Docker socket 보안

**Docker socket 접근은 사실상 호스트 관리자 수준의 권한입니다.** 컨테이너가 침해되면 다른 컨테이너 제어와 호스트 파일 접근이 가능할 수 있습니다. Non-root, cap_drop, read-only filesystem만으로 이 권한이 제한되지는 않습니다. 신뢰할 수 있는 코드/이미지만 실행하고 socket 노출 범위를 최소화하세요. 더 강한 분리가 필요하면 대상 컨테이너와 API 작업을 제한하는 별도 proxy/authorization 계층을 설계해야 합니다.

## Known limitations와 자체 검토

- 단일 대상, 단일 watchdog을 전제로 합니다. 같은 state file은 lock으로 보호하지만 서로 다른 state volume을 쓰는 복수 watchdog은 조정하지 못합니다.
- 과부하/queue 지연/네트워크 장애/잘못된 인증·모델 설정도 실제 hang과 구분되지 않습니다. 운영 지연 분포에 맞춰 timeout/threshold/cooldown을 조정해야 합니다. 재시작은 진행 중 요청을 끊습니다.
- requests의 socket timeout에 POSIX SIGALRM 전체 deadline을 추가합니다. DNS와 느린 body 전송도 제한하며 main thread에서만 실행해야 합니다. Windows/다른 SIGALRM 사용자와의 embedding은 지원하지 않습니다.
- 실행 중 복구 시간은 monotonic clock, 영속 restart 이력은 wall clock을 사용합니다. 큰 시스템 시각 변경은 재기동 시 window/grace 계산을 바꿀 수 있습니다.
- 상태 쓰기 실패는 fail-closed로 멈춥니다. 파일이 삭제되거나 영속 volume이 교체되면 이력을 복원할 수 없습니다. Disk hang 같은 OS 수준 장애에는 I/O deadline이 적용되지 않습니다.
- Docker daemon 장애도 제한된 예산을 소비하고 복구 검증 후 FAILED로 갈 수 있습니다. Watchdog은 daemon/GPU reset/host reboot를 수행하지 않습니다.
- HTTP/JSON malformed 응답은 실패 처리하고, 예상 밖 내부 예외는 FAILED로 전환합니다. HTTP body/exception 원문은 비밀정보 보호를 위해 남기지 않습니다.
- Webhook 장애는 최대 ALERT_TIMEOUT만큼 iteration을 지연시킬 수 있으나 main loop를 중단하지 않습니다.
- 매우 짧은 completion은 GPU 전체 기능/장시간 generation/전체 replica의 정상 여부를 보장하지 않습니다.
- MAX_RESTARTS는 제한에 도달한 마지막 시도에 복구 기회를 줍니다. FAILED에서는 자동 probe/restart를 중단하고 프로세스는 살아서 운영자 조치를 기다립니다.
- 상태 파일 손상이나 영속 저장 오류 알림은 프로세스 재기동 때 다시 발생할 수 있습니다. 알림 전달 자체는 보장하지 않습니다.

## 향후 개선 (Phase 2, 현재 미구현)

Prometheus running/waiting requests, generation/prompt tokens, KV cache를 독립 collector로 추가하고 관측 정보를 별도 인터페이스로 전달할 수 있습니다. 현재 Controller는 HTTP ProbeResult만으로 결정하므로 metric 수집 장애와 복구 정책은 결합되지 않습니다. Prometheus/Grafana, HAProxy drain, 다중 instance, GPU reset, host reboot는 후속 범위입니다.
