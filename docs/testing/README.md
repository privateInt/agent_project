# 테스트 안내

2026-10-01 업로드 코드 검토의 새 검사 범위는 [검증 JSON](source-review-20261001.json), 판정은 [분석 보고서](../reviews/code-review-20261001.md)를 따른다. 문서 정리 때문에 아래 전체 서버 검사를 반복하지 않는다.

각 테스트가 확인하는 범위를 구분한다. 최신 실행 완료 상태는 [현재 상태](../status.md), 과거 결과는 [이력](../history/README.md)에 기록한다.

검증 문서의 UUID는 과거 예시이므로 현재 목록의 실제 ID로 바꾼다. `--resume`에는 이번 실행이 출력한 보관 중인 보고서만 사용한다. 일부 과거 결과는 요약 후 삭제했고, 이번 Ops 완료 결과는 [압축 이력](../history/ops-evidence-catalog.md)으로 무손실 통합했다. 대표/현재 결과는 별도 보존한다. [보관 정책](artifact-retention.md)을 따른다.  문서 변경만으로 GPU 작업이나 초기화를 재실행하지 않는다.

| 변경 / 목적 | 선택할 검증 |
|---|---|
| 모델/버전 이름·진행률·배포 삭제·재배포 | [관리 수정 Go/DB·UI·적용 검사](model-management-update.md) |
| Model v1 당시 완료 범위 | [완료 범위·제외된 백업 복원](results-summary-20260916.md#model-v1) |
| Streamlit 관리·학습·배포·채팅 | [Playground 재빌드·AppTest·버튼으로 학습/배포·채팅](streamlit-playground.md) |
| Compose·Dockerfile·빌드 연결 변경 | `python tests/check_build_layout.py`, 아래 Go/DB 테스트, 이미지 빌드와 [제어 smoke](request-creation.md#control-smoke) |
| 생성 API·idempotency·도메인 전달 변경 | [생성 요청 REST·gRPC·DB 테스트](request-creation.md)와 다섯 서비스 전환 후 CPU smoke |
| Operation SQL·상태·lease 변경 | 아래 Go + 실제 PostgreSQL 회귀 테스트 |
| Dispatcher·Reconciler 변경 | 같은 Go 테스트와 필요 시 [제어 smoke](request-creation.md#control-smoke) |
| 프로세스 장애·실행 전 복구 경계 변경 | [실제 Dispatcher 장애 테스트](request-creation.md#dispatch-fault) |
| 데이터셋 등록·버전·학습 pin 변경 | [데이터셋 Go/DB·등록 smoke·GPU 테스트](datasets.md) |
| 로컬 DDP·GPU 생성 점유 변경 | [다중 GPU 단위·DB·이미지·GPU 검증](multi-gpu.md) |
| GPU_BUSY 뒤 dispatching 정체·Dispatcher 재시작 | [GPU 대기 수정·기존 요청 복구·10분 관찰](gpu-wait.md) |
| 모델 import·Axolotl·결과물 변경 | [모델 작업 테스트](model-workflow.md) |
| K03 임베딩 API/profile·용도 표시 | [최소 이미지 교체·Go/AppTest·기존 배포 실호출](embedding.md) |
| vLLM 생성·readiness·추론·중지 | [서빙 테스트](serving.md). CPU demo와 별도로 실제 GPU 실행 필요 |
| 학습 adapter·원본 연결·서빙용 사본 변경 | [adapter 서빙 테스트](adapter-serving.md). 기존 snapshot 회귀와 실제 adapter GPU 검증 |
| 장애 테스트 도구 변경 | Python unittest와 Go helper 테스트 후 실제 장애 테스트 |
| Job 로그 보존·컨테이너 GC | [GC 단위·Docker/MinIO 검증](gc.md) |
| 원격 노드 선택·SSH worker·원격 GPU 관찰 | [원격 노드 Go/DB·probe·GPU 검증](remote-nodes.md) |
| 목록·검색·필터·페이지네이션 | [조회 API Go/DB·REST smoke](catalog.md) |
| 학습 취소·실행 중지·현재/보관 로그 API | [학습 제어 Go/DB·로그·취소 smoke](training-control.md) |
| 완료 학습 출력 보존·업로드만 재시도·로컬 정리 | [결과 복구 Go/DB·GPU/MinIO 검증](training-output.md) |
| checkpoint 보존·실패/취소 후 재개 | [checkpoint Go/DB·이미지·남은 step 재개](training-checkpoints.md) |
| train/eval 분리·지표·MLflow·결과 비교 | [평가 Go/DB·이미지·GPU 비교 검증](training-evaluation.md) |
| Ops 측정·수집/명시 복구 실행기 | [실행기 계약](../operations/ops-backup-execution.md), [오프라인 검사](ops-backup-execution-offline-20261002.json), 신규 native 연결 인수는 후속 |
| 문서만 변경 | 경로·링크·명령의 작업 디렉터리 확인 |

## Operation 관련 회귀 검증

호스트의 기존 Compose 설정에서 DB URL을 가져오며 값 자체는 출력하지 않는다. 테스트는 해당 연결 DB 안에 고유 schema를 생성하고 종료 시 제거한다. URL의 DB가 `postgres`여도 테스트 schema가 그 DB에 격리되므로 정상이다. role에는 그 DB의 schema 생성·삭제 권한이 필요하다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail

  docker build --target builder -t model-platform-check:build-layout .

  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL

  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:build-layout \
    go test -count=1 -v \
      ./...
)
```

`OPERATION_TEST_POSTGRES_URL`이 없으면 DB 테스트가 SKIP된다. 별도 Docker/MinIO 연결을 쓰는 GC live 테스트도 전용 설정이 없으면 SKIP되며 [GC 절차](gc.md)로 따로 실행한다. **PASS만 보지 말고 DB 테스트가 실제로 실행됐는지 확인한다.** 네트워크 이름을 재정의한 환경에서는 `--network`도 같은 값으로 바꾼다.

주요 검증은 상태 전이·terminal 보존, Outbox rollback·재발행·잠금, lease 회수·동시 BeginExecution·기존 token 차단, Reconciler CAS·관찰, Dispatcher commit 정책, Job 승인·보고 재시도다. Outbox의 `broker offline` 로그는 fake publisher 장애를 의도한 테스트이며 실제 Kafka 중단을 의미하지 않는다.

빌드 구조 전환은 이 테스트가 통과하면 [전환 기록](../history/model-platform-202609.md#2026-09-08-03-build-layout)에 따라 개별 이미지를 빌드하고 제어 서비스를 재기동한다. 일반 변경은 영향을 받는 패키지를 골라 테스트한다. GPU 학습과 실제 SIGKILL smoke를 매번 반복할 필요는 없다.

## 결과 기록

PASS/SKIP/실패 여부, 검증한 이미지·소스, 실제 DB 사용 여부와 시각을 남긴다. 실행·분석 중 결과는 각 플랫폼의 `tests/results/`에 생성하며, 검증 완료 후 핵심 내용을 문서로 요약하고 과거 결과 파일을 정리한다. 테스트 도구가 실패하면 결과와 cleanup 오류를 먼저 확인하고 성공 기록을 갱신하지 않는다.

2026-09-16에 과거 결과 170개를 정리했다. [검증 요약·삭제 범위·서버 적용 방법](results-summary-20260916.md)을 참고한다. 삭제된 보고서를 이용한 재개·오프라인 재평가는 불가능하며 새 테스트는 기존대로 결과 파일을 생성한다. 테스트 코드·fixtures와 운영용 `reset.lock`은 유지한다.

완료 결과의 1 MB 예산은 Model·Data·Knowledge의 tests/results 합계에 적용한다. docs/testing과 코드/fixtures는 별도다. [대표 결과·전체 판정 요약·정리 범위](artifact-retention.md)를 참고한다. 테스트 코드·입력자료는 이 예산에서 제외하며 자동 삭제는 적용하지 않았다.
