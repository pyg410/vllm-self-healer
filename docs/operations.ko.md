# 운영 가이드 (v0.1.1)

[English](operations.md) | [README](../README.ko.md)

## 시작 진단

시작 시 JSON "configuration loaded" 로그를 한 번 출력합니다. 실제 적용된 숫자 설정, 검증된 모드·정책, 설정 여부 boolean을 포함합니다. LOG_LEVEL로 다른 INFO가 숨겨져도 이 기록은 출력합니다. URL, 경로, 모델·컨테이너 이름, prompt, API key, header와 body 값은 제외합니다.

Watchdog 설정으로 보이는 미지원 이름은 WARNING을 남기고 계속 시작합니다. 예를 들어 STARTUP_GRACE_PEROID에는 STARTUP_GRACE_PERIOD를 제안합니다. 값은 기록하지 않습니다. 일반 시스템 변수와 문서화된 Compose/Docker 입력은 무시합니다. 이는 휴리스틱이며 모든 vLLM 환경변수를 검증하는 기능은 아닙니다.

시작 오류에는 error_type, 고정 stage(logging_setup, state_directory_and_lock, probe_server_bind 등), 가능한 경우 OS 오류 번호를 기록합니다. 설정 오류 요약에는 설정 이름을 포함하며 값은 제외합니다. I/O exception 원문은 출력하지 않습니다.

"persisted state restored"에는 저장된 상태, 선택한 backend, 실제 적용 상태, recovery_ready_at/recovery_deadline, 재시작 이력 개수와 stored_recovery_timing_applied를 기록합니다.

## 시간 설정과 적용 시점

환경변수는 **프로세스 시작 시 한 번** 읽습니다. .env를 수정해도 실행 중 프로세스는 변경되지 않으며 새 환경으로 재생성·재시작해야 합니다.

| 설정 | 사용 시점 | v0.1.1 기본 의미 |
|---|---|---|
| CHECK_INTERVAL | 정상 감시 회차 사이 | Probe 완료 후 30초 대기 |
| STARTUP_GRACE_PERIOD | 새 복구 회차 시작 | Probe 없이 300초 대기 |
| RECOVERY_TIMEOUT | 새 복구 회차 시작 | Grace 이후 검증에 600초 허용 |
| RESTART_COOLDOWN | 재시작 정책 평가 | 시도 시작 시각 사이 최소 300초 |
| RESTART_WINDOW | 재시작 예산 평가 | 최근 600초의 시도 집계 |
| EVENT_WEBHOOK_* | 이벤트 전송 | 프로세스 시작 시 읽은 설정 사용 |
| PRE/POST hook 설정 | 해당 lifecycle action | 프로세스 시작 시 읽은 설정 사용 |

RECOVERING 복원 시 저장된 recovery_ready_at과 recovery_deadline이 새 grace/timeout 환경변수보다 우선합니다. 새 값은 다음 복구 회차 또는 의도적인 상태 초기화 이후 적용됩니다. v0.1.1에서는 grace 도중 조기 probe를 하지 않습니다. FAILURE_THRESHOLD는 HEALTHY/SUSPECT용이며 RECOVERING은 복구 deadline으로 판단하므로 카운터가 임계치를 넘을 수 있습니다.

## 로그: 두 방식 중 선택

### A. 애플리케이션 파일 로그 + stdout

기존 Compose service와 최상위 volumes에 다음을 합칩니다.

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

실제 service 이름에 맞추세요(빠른 시작은 vllm-self-healer를 사용합니다). LOG_FILE 기본값은 빈 값으로 stdout만 사용합니다. 설정하면 stdout을 유지하면서 UTF-8 JSON RotatingFileHandler를 추가합니다. 기본값은 현재 파일과 약 10 MiB 크기의 백업 5개이며 단일 큰 로그 항목은 제한 크기를 넘을 수 있습니다. LOG_MAX_BYTES와 LOG_BACKUP_COUNT는 양수여야 합니다.

이미지는 /logs를 UID/GID 10001용으로 준비합니다. 새 named volume은 이 소유권을 사용하지만 기존 volume과 bind mount는 해당 사용자의 쓰기 권한을 확인해야 합니다. 상위 디렉터리는 미리 존재해야 합니다. 잘못되었거나 쓰기 불가능한 경로는 logging_setup 시작 오류가 됩니다. Kubernetes의 read-only root filesystem에서는 쓰기 가능한 로그 volume을 mount하세요. 여러 watchdog이 같은 파일을 동시에 회전시키면 안 됩니다.

### B. stdout + Docker logging driver

LOG_FILE은 비워 두고 watchdog service 아래에 추가합니다.

```yaml
logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "5"
```

두 방식은 선택지이며 모두 설정할 필요가 없습니다. Docker는 애플리케이션 JSON 한 줄을 자체 로그 형식으로 감쌉니다.

## 정보 전달용 event webhook

| 변수 | 기본값 |
|---|---|
| EVENT_WEBHOOK_URL | 빈 값: 비활성 |
| EVENT_WEBHOOK_METHOD | POST |
| EVENT_WEBHOOK_TIMEOUT | 5초 |
| EVENT_WEBHOOK_EVENTS | 빈 값: 전체, 그 외 쉼표로 구분한 이벤트 |
| EVENT_WEBHOOK_HEADERS | {} |
| EVENT_WEBHOOK_BODY | {} |

이벤트는 RESTART_TRIGGERED, RESTART_CONFIRMED, RECOVERY_SUCCESS, RECOVERY_FAILED, MAX_RESTART_EXCEEDED입니다.

RESTART_CONFIRMED는 Docker 재시작 호출 성공 또는 Kubernetes 새 start-ID UUID 관찰 후에만 보냅니다. /live=503 활성화 자체는 확인이 아닙니다. Docker 호출이 timeout이면 나중에 probe가 복구되어도 confirmation은 보내지 않습니다.

환경변수 예시:

```dotenv
EVENT_WEBHOOK_URL=https://operations.example.com/events
EVENT_WEBHOOK_METHOD=POST
EVENT_WEBHOOK_EVENTS=RESTART_CONFIRMED,RECOVERY_FAILED,MAX_RESTART_EXCEEDED
EVENT_WEBHOOK_HEADERS={"Authorization":"Bearer REPLACE_ME"}
EVENT_WEBHOOK_BODY={"environment":"production","service_group":"inference"}
```

Static body에 service, container, event, timestamp(UTC), recovery_mode, reason, restart_count를 합칩니다. 충돌 시 표준 필드가 우선합니다. Body/header는 JSON object이며 header 값은 유효한 문자열이어야 합니다. POST, PUT, PATCH만 지원합니다. 잘못된 JSON, method, event 이름, failure policy는 값을 출력하지 않고 설정 오류로 처리합니다.

HTTP 2xx이면 성공입니다. JSON 응답 계약은 없으며 원격 응답 body는 읽지 않습니다. Timeout, 연결·프로토콜 오류, non-2xx는 WARNING을 남기고 복구 상태를 바꾸지 않습니다. 동기 요청이며 유한 timeout으로 한 번만 전송하므로 루프가 잠시 지연될 수 있습니다. 재전송 queue와 template engine은 없습니다.

기존 ALERT_WEBHOOK_URL/ALERT_TIMEOUT은 기존 payload와 4개 이벤트를 유지하며 RESTART_CONFIRMED를 받지 않습니다. 두 endpoint를 모두 설정하면 각각 독립적으로 전송합니다.

## Lifecycle action hook

Hook은 선택적 작업 실행용이며 정보 전달 이벤트와 분리됩니다. 외부 service에서 HAProxy 등의 drain/ready 작업을 구현할 수 있습니다. Watchdog에 HAProxy 전용 API나 shell 실행 기능을 넣지는 않습니다.

PRE_RESTART_WEBHOOK과 POST_RECOVERY_WEBHOOK 각각에 대해:

| 접미사 | 기본값 |
|---|---|
| _URL | 빈 값: 비활성 |
| _METHOD | POST |
| _TIMEOUT | 10초 |
| _HEADERS | {} |
| _BODY | {} |
| _FAILURE_POLICY | continue, abort도 지원 |

예시:

```dotenv
PRE_RESTART_WEBHOOK_URL=https://operations.example.com/drain
PRE_RESTART_WEBHOOK_BODY={"server":"inference-a"}
PRE_RESTART_WEBHOOK_FAILURE_POLICY=abort
POST_RECOVERY_WEBHOOK_URL=https://operations.example.com/ready
POST_RECOVERY_WEBHOOK_BODY={"server":"inference-a"}
POST_RECOVERY_WEBHOOK_FAILURE_POLICY=continue
```

HTTP 검증·metadata 병합 규칙은 event webhook과 같으며 event는 PRE_RESTART 또는 POST_RECOVERY입니다. Payload에는 관리자가 설정한 비밀정보가 있을 수 있으므로 로그에 기록하지 않습니다.

정상 순서:

```text
정책상 시도 허용 → 예산 예약 저장
→ RESTART_TRIGGERED 알림
→ PRE_RESTART 작업
→ backend 요청 저장 → backend 재시작
→ 재시작 관찰 → RESTART_CONFIRMED 알림
→ RECOVERING → 두 probe 성공
→ POST_RECOVERY 작업 → HEALTHY → RECOVERY_SUCCESS 알림
```

| Hook 실패 | continue | abort |
|---|---|---|
| PRE_RESTART | 경고 후 재시작 진행 | FAILED 저장, 재시작 호출·confirmation 없음 |
| POST_RECOVERY | 경고 후 복구 성공 처리 | FAILED 저장, HEALTHY/readiness·RECOVERY_SUCCESS 없음 |

PRE abort에도 예약된 시도는 집계합니다. 외부 작업 반복과 crash의 불확실성을 제한하기 위한 것이며 재시작 성공을 의미하지 않습니다. POST abort도 미복구 연속 시도 횟수를 유지합니다. Kubernetes FAILED는 /live=200, /ready=503입니다. Abort 원인을 해결한 뒤 수동 상태 초기화가 필요합니다.

POST_RECOVERY는 초기·복원 후 준비 확인에서도 두 probe 성공 후 실행합니다. 기존 초기 RECOVERY_SUCCESS 동작과 맞춘 것입니다. 실패한 probe 뒤에는 실행하지 않습니다. Hook을 설정하면 작업이 끝날 때까지 readiness를 열지 않습니다. continue는 외부 작업 실패를 허용한다는 명시적 선택입니다.

PRE 실행 중 영속 상태는 활성 backend 요청이 없는 SUSPECT입니다. Crash 후 복원으로 hook을 건너뛰어 Kubernetes liveness를 켤 수 없습니다. 이전 vLLM UUID는 PRE 전에 확보하므로 느린 hook 도중 vLLM이 교체되면 오래된 요청으로 새 인스턴스를 죽이지 않습니다. Confirmation deadline은 PRE 이후, 요청 저장과 liveness 노출 전에 정합니다.

HTTP와 상태 파일만으로 외부 작업을 정확히 한 번 보장할 수 없습니다. Crash로 hook/event가 중복되거나 알림이 누락될 수 있습니다. Endpoint를 멱등하게 만들고 외부 트래픽 상태를 독립적으로 조정하세요. URL은 관리자가 지정한 신뢰할 수 있는 endpoint만 사용하며 inference 입력에서 받지 않습니다. 비밀정보를 .env와 함께 커밋하지 마세요. 자동 redirect와 환경 proxy/.netrc 인증은 계속 비활성화됩니다.

종료 요청은 진행 중 요청이 끝난 뒤 후속 작업을 막습니다. stop_grace_period는 가장 긴 요청과 이후 실패 알림(legacy alert 포함)을 고려해 설정합니다. 기본값은 기존 90초 안에 들어가지만 timeout을 크게 설정하면 늘려야 합니다.

## 안전한 상태 초기화

1. Watchdog 프로세스·컨테이너를 중지하고 근본 원인을 해결합니다.
2. 필요하면 /data/watchdog_state.json을 백업합니다.
3. 유지보수 컨테이너 또는 mount된 저장소에서 해당 JSON만 삭제합니다.
4. 의도한 환경변수로 watchdog을 시작합니다.

다른 프로세스가 실행 중일 때 .lock 파일을 지우지 마세요. 파일의 존재가 활성 lock을 의미하지는 않으며 flock은 프로세스 종료 시 해제됩니다. 잠긴 파일을 삭제하면 다른 inode에 두 번째 프로세스가 lock을 잡을 수 있습니다.

docker compose down은 일반적으로 named volume을 유지합니다. docker compose down -v는 모델 cache 등 다른 프로젝트 데이터까지 삭제할 수 있으므로 전체 초기화 수단으로 사용하지 마세요. Watchdog 상태 파일·volume만 대상으로 합니다.

Kubernetes emptyDir은 컨테이너·sidecar 재시작에는 유지되지만 Pod 교체 시 사라집니다. 적절한 PVC는 교체 후에도 이력을 유지할 수 있습니다. 여러 replica가 같은 쓰기 가능한 상태 파일을 공유하면 안 됩니다. Start-ID 래퍼는 필수이며 /live와 /ready는 kubelet·Pod 내부용으로 public Service에 노출하지 마세요.

## 후속 범위

v0.2.0은 별도 작업입니다. Startup grace 중 probe, recovery/FAILED reason 저장, timezone 설정, Docker 진단, Docker 의존성 선택 설치는 이번에 구현하지 않습니다. Kubernetes API나 다중 Pod controller도 추가하지 않습니다.
