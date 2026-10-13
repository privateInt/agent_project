# Job GC 검증

[동작·설정](../operations/gc.md)과 [현재 검증 상태](../status.md)를 함께 확인한다. 기존 DB·프로토콜·Job 코드는 수정하지 않았으므로 이 변경의 실행 검증은 GC 패키지와 Compose 연결에 집중한다.

## 단위 테스트와 격리 통합 테스트

명령은 다음 순서로 실행한다. 테스트 이미지는 기존 builder에 Docker CLI만 추가한 `gc-test` target이다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  python tests/check_build_layout.py
  docker build --target gc-test -t model-platform-check:gc .
  docker run --rm model-platform-check:gc \
    go test -count=1 -v ./gc -skip '^TestLive(GCArchiveAndRemoval|TrainingLogArchiveFallback)$'
  python tests/run_gc_integration.py
)
```

단위 테스트는 소유·token·실행 승인 검사, 성공/실패 보존 기간, read-only dry-run, 로그 용량 초과, 저장 실패·읽기 검증 실패, manifest 게시 실패, 상태 변경, 재시도 중복 방지, 캐시 후 원격 검증, Docker 인자와 stdout/stderr 분리, 후보 순환을 확인한다.

`run_gc_integration.py`는 현재 Compose의 MinIO·네트워크 설정과 실행 중인 `model-operation`의 이미지 ID를 사용한다. 비밀 값은 출력하거나 명령 인자로 넣지 않고 테스트 컨테이너 환경으로 전달한다. 새로운 이미지를 pull하거나 GPU를 요청하지 않는다.

실제 통합 테스트가 만드는 리소스는 다음 두 종류다.

- 임의 UUID Operation/token label을 붙인 CPU 컨테이너 하나. stdout/stderr를 각각 남기고 exit code 7로 종료한다.
- `model-gc-test-<UUID>` MinIO 버킷 하나. 원래 없던 버킷임을 확인하고 로그·manifest만 저장한다.

실제 Docker·MinIO를 사용하고, Operation 조회와 시계는 테스트 내부 fixture로 제공한다. 프로덕션 DB·Kafka에 행이나 이벤트를 만들지 않는다. 상시 GC는 이 임의 Operation ID를 DB에서 찾을 수 없으므로 수집하지 않는다. 실패 보존 기간을 테스트 내부에서 1초로 설정하고 fixture 시각을 사용하므로 실제 7일을 기다리지 않는다.

검증 순서는 dry-run 무변경 → archive 모드의 실제 업로드·readback → stdout/stderr와 exit code 확인 → apply 제거 → 제거 후 원격 로그 읽기다. 성공·실패 시 생성한 정확한 fixture와 버킷을 정리한다. cleanup 실패도 테스트 실패로 보고하며 표시된 UUID 리소스를 확인한다. 강제 제거는 이 테스트의 고유 label·이름을 확인한 fixture cleanup에만 사용한다.

일반 `go test ./...`에서는 명시적 연결 설정이 없으면 `TestLiveGCArchiveAndRemoval`과 `TestLiveTrainingLogArchiveFallback`이 SKIP된다. 위 기본 전용 절차는 기존 GC 통합 테스트만 실행한다. Training 로그 reader의 컨테이너 제거 후 조회는 [학습 제어 검증 7번](training-control.md)의 별도 이미지와 `--training-logs` 옵션을 사용한다. runner는 지정한 테스트의 PASS를 확인하므로 테스트 미포함·SKIP도 실패로 처리한다. 기존 Operation DB 테스트의 연결 조건은 [테스트 목차](README.md)를 따른다.

2026-09-10 사용자 서버에서 `--training-logs` 실행이 PASS했다. `training.run/training_run/cancelled` Operation fixture와 실제 Docker·MinIO를 사용해 Training reader의 Docker 조회 → archive 저장·readback → 컨테이너 제거 → archive 조회, stdout/stderr·실행 식별자 보존을 확인했다. 테스트와 cleanup에 실패·SKIP은 없었다. 자세한 ID·이미지·원본 로그 근거는 [학습 제어 이력](../history/model-platform-202609.md#2026-09-10-01-training-control)에 있다. 상시 GC 모드 전환이나 프로덕션 학습 컨테이너 삭제를 실행한 것은 아니다.

## GC 서비스 dry-run 기동

다음 명령은 신규 설치 또는 의도적인 관찰 모드 전환용으로 dry-run을 명시한다. 현재 Compose 기본은 apply이며 자동 정리 동작은 사용자 확인을 받았다. 이미 운영 중인 GC를 문서 확인만을 위해 dry-run으로 되돌리지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build gc
  MODEL_GC_MODE=dry-run docker compose up -d --no-build --no-deps --force-recreate gc
  docker compose ps gc
  docker compose logs --tail=80 gc
)
```

`job GC ready mode=dry-run`과 대상별 action을 확인한다. `listed=0`은 현재 소유한 `exited` Job이 없다는 뜻이다. 대상이 `retained`이면 note에 소유·승인·terminal·로그 형식 등 제외 이유가 나타난다. `would_archive`/`would_archive_and_remove`여도 dry-run은 MinIO와 삭제 경로를 검증하지 않는다.

전환에 필요한 추가 로그는 다음 명령으로 공유한다.

```bash
cd ~/workspace/agent_project/model_platform
docker compose logs --tail=100 gc
```

실제 사용자 Job의 archive/apply 전환은 [운영 안내](../operations/gc.md)의 모드 설정을 사용한다. 전체 Go/DB 회귀나 GPU 학습을 이 기능 확인만을 위해 반복할 필요는 없다. 이번 통합 테스트는 실제 Operation gRPC 연결을 대체하지 않으므로 dry-run에서 사용자 Job의 조회·소유 매칭 결과도 확인한다.
