# 학습 취소와 로그 조회

Docker 단일 서버의 `training.run`을 대상으로 한다. Kubernetes·실제 원격 노드 적용은 보류하며, 활성 원격 학습의 취소와 로그 조회는 409로 거절한다. 기존 학습 실행 명세와 Job 이미지는 유지한다.

## REST 계약

| 요청 | 의미 |
|---|---|
| `POST /api/training-runs/{id}/cancel` | 취소 의도를 저장한다. body·query 없음. 리소스 ID 자체로 멱등하며 별도 Operation을 만들지 않는다. |
| `GET /api/training-runs/{id}` | 기존 정보와 `cancel_requested_at`, `cancelled_at`, `cancellation_error_code`를 반환한다. |
| `GET /api/training-runs/{id}/logs?tail_lines=200&max_bytes=65536` | 현재 dispatch의 stdout/stderr 끝부분을 조회한다. |

취소 요청 응답은 TrainingRun 상세와 같다. 진행 중이면 HTTP 202와 `status=cancelling`, 이미 취소되었거나 성공·실패한 terminal 작업이면 HTTP 200과 기존 상태를 반환한다. 202는 중지 확인 완료를 뜻하지 않는다. `cancel_requested_at`은 최초 요청 시각이며 재요청으로 갱신하지 않는다. 목록의 `status` 필터에도 `cancelling`·`cancelled`를 추가했다.

로그 응답은 `training_run_id`, `operation_id`, `source`, `availability`, `stdout`, `stderr`, `truncated`, `tail_lines`, `max_bytes`다. `Cache-Control: no-store`를 설정한다. `tail_lines`는 기본 200·범위 1..2000, `max_bytes`는 각 스트림의 원본 byte 기준 기본 65536·범위 1024..262144다. `truncated`는 byte 한도로 잘렸음을 뜻하며 전체 로그가 요청한 줄 수보다 긴지 뜻하지는 않는다. UTF-8 중간 경계나 잘못된 문자는 대체 문자로 표현한다.

`source=docker|archive`와 `availability=available`이면 조회 성공이다. 로그가 없는 상황은 `source=none`과 `not_started`, `not_retained`, `legacy_execution_unavailable`로 구분한다. 조회 장애·무결성 오류는 빈 로그 성공으로 처리하지 않고 503을 반환한다. 외부 container ID·경로·follow·임의 Docker 명령을 받지 않는다. execution token·환경 변수·명령·host mount를 메타데이터로 반환하지 않는다. stdout/stderr는 원래 Job 출력이므로 사내 사용 범위의 접근 제어와 로그 작성 정책을 따른다.

### 이전 로컬 Job의 로그 호환

노드 이름 설정 전 생성된 Job에는 `model-platform.execution_node` 라벨이 없을 수 있다. 현재 Training에 `DISPATCHER_NODE_NAME`이 설정되어 있어도, 다음 조건을 모두 만족하면 해당 Job의 보존 로그를 읽을 수 있다.

- `execution_node` 키 자체가 없고, Operation의 요청 노드와 컨테이너의 `requested_node`가 모두 비어 있다.
- Operation은 `succeeded`·`failed`·`cancelled` 중 하나이고 컨테이너는 `exited`다. running·paused·restarting 플래그가 없어야 한다.
- 소유자·Operation ID·현재 dispatch token·컨테이너 이름과 ID, 실행 유형·컨테이너 정책 검사를 모두 통과한다.

노드 라벨이 존재하면서 다른 값이거나 명시적으로 빈 값이면 이 호환 경로를 적용하지 않는다. 명시적으로 노드를 지정한 요청에도 적용하지 않는다. 검증 뒤에는 고정 container ID로 로그를 읽으며 이름·라벨·환경 변수·컨테이너를 수정하지 않는다. **취소·중지는 계속 기존 실행 노드 일치 검사를 사용한다.** 이 호환 처리는 로그 GET에만 적용된다.

## 취소 처리와 경합

1. TrainingRun 행 잠금으로 취소 요청과 학습 완료·실패 commit의 순서를 정한다. 완료가 먼저면 산출물·결과를 유지한다. 취소가 먼저면 이후 tracking 시작·완료·실패 보고가 TrainingRun을 덮어쓰지 못한다.
2. Training의 취소 controller가 원본 durable submission으로 Operation을 확인하고 `cancelling`으로 바꾼다. 이 상태는 새 Claim·BeginExecution·phase/성공/실패 보고·lease 복구를 차단한다.
3. Dispatcher·Deployment·Training이 공유하는 GPU 잠금 아래에 영속 취소 기록을 저장한다. 취소 전에 멈춰 있던 Dispatcher도 나중에 같은 Operation을 생성할 수 없다. 모든 공유 파일 작성 서비스를 함께 교체해야 한다.
4. 같은 Operation의 로컬 Job들을 이름·ID·소유 label·세대·실행 노드로 확인한다. 실행 중인 Job은 Docker stop의 10초 유예 뒤 종료를 다시 관찰한다. never-started `created` 컨테이너만 force 없이 제거하고, exited 컨테이너와 로그는 남긴다. paused/restarting 또는 소유권이 다른 컨테이너는 자동 중지하지 않는다.
5. 종료 확인 뒤 GPU 예약을 해제하고 Operation → TrainingRun 순서로 `cancelled`를 확정한다. 중간 응답 유실·DB rollback은 다음 controller 실행에서 이어 처리한다. 취소 기록에는 TTL이 없으며, volume과 DB를 따로 초기화하지 않는다.

Docker stop은 기본 SIGTERM 후 유예 시간이 지나면 SIGKILL을 보낸다. 컨테이너의 종료 신호 설정에 영향을 받으며 **학습 checkpoint 저장을 보장하지 않는다**. [Docker stop 문서](https://docs.docker.com/reference/cli/docker/container/stop/)

취소 controller는 매초 다음 대상을 고르며 실패한 항목은 최소 5초 간격으로 재관찰한다. Docker 생성 응답을 잃은 뒤 한 번도 컨테이너를 관찰하지 못한 예약은 `CANCEL_CREATION_UNCONFIRMED`로 남긴다. 단순한 현재 부재만으로 뒤늦은 생성을 부정하지 않고 `cancelling`을 유지한다. 사용자가 claims 파일을 지워 해결하는 방식은 사용하지 않는다.

기존 Job 이미지가 Operation에 실패를 먼저 보고한 좁은 경합에서는 이미 확정된 Operation 실패를 보존한다. 컨테이너 중지를 확인한 뒤 TrainingRun도 `failed`, `cancellation_error_code=CANCEL_LOST_TO_FAILURE`로 맞춘다. 정상 취소로 보고하지 않는다. Operation 성공과 Training 결과가 불일치하면 `CANCEL_OPERATION_ALREADY_SUCCEEDED`로 관찰을 남기고 수동 확인을 요구한다.

실행 중지 뒤 MLflow가 RUNNING/SCHEDULED로 남으면 KILLED로 갱신한다. 이미 FINISHED/FAILED/KILLED인 run은 유지한다. MLflow 연결 실패 시 플랫폼의 취소 확정은 유지하고 `CANCEL_TRACKING_SYNC_PENDING`을 기록해 동기화만 재시도한다. 모델 가중치·MLflow 기록·기등록 adapter를 삭제하지 않는다. 결과 등록과 취소가 겹치면 Management에 이미 등록된 산출물이 남을 수 있으나 취소된 TrainingRun의 성공 산출물로 연결하지 않는다.

## 로그 보관 연결

로컬 Docker Engine API v1.45와 Unix socket을 사용한다. Docker inspection으로 얻은 고정 container ID를 사용하며 stdout/stderr multiplex frame을 분리한다. Docker 조회 한 번의 원본 출력은 최대 32 MiB다. 초과하면 줄 수를 줄여 요청한다.

컨테이너가 없거나 GC와 조회가 겹치면 기존 `model-job-logs` 버킷의 현재 Operation/dispatch 경로를 찾는다. manifest 경로 hash·파일 크기·SHA-256과 metadata의 Operation·TrainingRun·실행 식별자를 검증한다. 보관 로그 전체는 스트리밍으로 검증하고 반환할 끝부분만 메모리에 둔다. 경로 탐색은 최대 200개 object, manifest는 정확히 한 개여야 하며 모호한 여러 사본을 임의 선택하지 않는다.

GC는 실행 승인을 받은 cancelled Job을 실패 Job과 같은 기본 7일 보존 정책으로 처리한다. 미시작 취소에는 실행 로그가 없을 수 있다. GC의 기존 dry-run/archive/apply 설정은 그대로 유지한다. 이 API가 로그를 새로 아카이브하거나 컨테이너를 삭제하는 것은 아니다. [GC 운영](../operations/gc.md)

검증 상태·명령은 [학습 제어 테스트](../testing/training-control.md), 구현 이력은 [변경 기록](../history/model-platform-202609.md#2026-09-10-01-training-control)에 있다.
