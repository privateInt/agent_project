# 데이터셋 버전 등록과 학습 검증

현재 구현·검증 완료 여부는 [현재 상태](../status.md), API와 제한은 [데이터셋 계약](../reference/datasets.md)에 둔다. 이 패치는 Dataset 등록·버전·학습 pin을 추가하며, 기존 상시 서비스 수와 Compose include 구조를 유지한다.

2026-09-09 사용자 서버에서 **데이터셋 등록 → 버전을 고정한 GPU 학습 → 새 adapter 등록 → 실제 vLLM 추론·중지**까지 통과했다. 아래 명령은 새 환경 적용 또는 재검증용이며 같은 코드와 결과를 반복 검증할 필요는 없다.

## 적용

tar를 `~/workspace/agent_project/`에 직접 업로드한 뒤 실행한다. 새 디렉터리로 감싸지 않고 실제 파일을 교체한다. 삭제할 기존 파일은 없다.

```bash
cd ~/workspace/agent_project/model_platform
```

## Go·PostgreSQL·Python 검증

2026-09-09 사용자 서버에서 아래 Go·PostgreSQL 검증의 최상위 테스트 28개와 하위 사례 29개가 모두 PASS했고 실패·SKIP은 없었다. Python 8개는 이전 작성 환경에서 통과한 별도 결과다. 이후 Training·Gateway 전환과 실제 MinIO 등록·GPU 학습·새 adapter 서빙도 통과했다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  python -m unittest discover -s tests -p 'test_live_dataset_smoke.py' -v
  python tests/check_build_layout.py

  docker build --target builder -t model-platform-check:dataset-versioning .
  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL

  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:dataset-versioning \
    go test -count=1 -v \
      ./internal/datasets ./internal/storage ./internal/idempotency \
      ./training/... ./gateway/... ./jobs/training/axolotl ./tests/creation
)
```

DB 검증은 연결 DB 안에 고유 schema를 만들고 종료 후 제거한다. 운영 Dataset·Training 행을 수정하지 않는다. `OPERATION_TEST_POSTGRES_URL`이 없으면 DB 테스트가 SKIP되므로 실제 실행 여부를 확인한다. 이 단계의 객체 저장소는 메모리 fake이고 실제 MinIO 검증은 다음 smoke가 수행한다.

주요 검증은 전체 JSONL·UTF-8·대소문자·형식·용량 제한, 파일 바이트 변경 탐지, 실패한 로컬 파일 비공개, 기존 fingerprint 보존, multipart와 gRPC의 5 MiB 업로드, 잘못된 스트림, 등록 재요청·경합·DB rollback·저장 실패, 학습 pin과 완료 결과 불일치 거부다. 기존 생성 요청·전달 테스트도 함께 실행한다.

## 이미지 빌드와 서비스 전환

위 검증 통과 후 실행한다. `MODEL_DATASET_BUCKET`을 별도로 정하지 않으면 기존 MinIO에 `model-datasets` 버킷을 만든다. 현재 환경 파일의 MinIO 연결 값을 그대로 사용한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build training gateway
  docker buildx bake --builder default -f docker-bake.hcl --load training-job
  docker compose up -d --no-build --no-deps --force-recreate training
  docker compose up -d --no-build --no-deps --force-recreate gateway
  docker compose ps training gateway
  docker compose logs --tail=30 training gateway
)
```

새 Job 바이너리와 Python config adapter가 함께 들어 있는 `model-platform-training-axolotl:launch-v1` 이미지를 빌드한다. Compose `up`은 학습 Job을 시작하지 않는다. 새 gRPC 필드는 기존 호출과 호환되므로 이번 변경으로 Operation·Management·Dispatcher·Deployment를 재기동할 필요는 없다.

## 실제 등록 smoke — GPU 사용 없음

2026-09-09 05:40 UTC 실제 등록 smoke가 PASS했다. 버전 `4aae2c55-d458-482a-901a-f6ea654b6412`는 `ready/v1`, 186 bytes·2개 레코드이며 보고서는 `tests/results/datasets/272f2108-5ef1-4079-a0aa-6b7f7f90cedb.json`이다. 이 등록 검증은 반복할 필요 없다. 아래 명령은 이후 재검증용이다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_dataset_smoke.py
```

스크립트는 두 레코드의 JSONL을 **등록된 파일**로 업로드한다. 실제 Gateway → Training → PostgreSQL·MinIO를 통해 등록·내용 hash·멱등 재요청·다른 바이트의 409·GET/list를 확인한다. 이 모드에서 Training Run·Operation·GPU Job은 만들지 않는다. 기존 CPU demo 배포 테스트와는 별개의 데이터 등록 검증이다.

보고서는 `tests/results/datasets/<run UUID>.json`에 저장한다. 성공하면 `registration_passed=true`, `passed=true`이며 `training_passed`는 없다. Dataset과 파일은 남긴다. 삭제 API를 제공하지 않는 현재 단계에서 테스트 데이터의 자동 삭제를 시도하지 않는다.

중단·응답 유실 후에는 출력된 보고서 경로로 이어서 실행한다. 같은 요청 키를 사용한다.

```bash
python tests/live_dataset_smoke.py --resume tests/results/datasets/실제-run-UUID.json
```

재실행 시 새 학습·데이터셋을 만들 의도가 아니라면 새 명령을 시작하지 말고 `--resume`을 사용한다. 기본 새 실행은 별도 UUID의 Dataset을 만든다. 실패·timeout은 기존 학습 취소나 자동 재학습을 의미하지 않는다.

## 실제 JSONL 사용

아래 파일 경로를 준비한 데이터 파일로 바꾼다. 최소 2개의 유효 레코드와 64 MiB 제한은 [계약](../reference/datasets.md)을 따른다.

```bash
python tests/live_dataset_smoke.py --file /absolute/path/to/train.jsonl
```

사용자 파일은 내용과 등록 hash를 비교하는 데 사용한다. 다른 바이트의 충돌 검사는 작은 별도 fixture를 같은 버전 이름에 보내 409를 확인하며, 새 버전으로 저장하지 않는다. 보고서에는 파일 내용 대신 원본 경로·hash·크기를 기록한다. `--resume`은 해당 원본 파일이 그대로 존재해야 한다.

## 버전을 고정한 실제 GPU 학습

2026-09-09 공간 확보 후 실행에서 실제 GPU 학습·결과 pin·MinIO 업로드·새 adapter 등록까지 PASS했다. 보고서는 `tests/results/datasets/d33aa64d-50d7-476d-ae0c-16126c3aacb7.json`, 새 adapter는 `4460a3b4-a059-4a9f-8785-ea9884564bcd`다. 후속 실제 서빙도 통과했으며 다음 학습 명령은 이후 재검증용이다.

등록 smoke 성공과 새 Job 이미지 빌드를 확인한 뒤, 사용 가능한 GPU를 지정한다. 후속 [GPU 점유 관리 패치](multi-gpu.md)는 플랫폼의 중복 생성을 제어한다. 호스트의 외부 GPU 작업은 별도 확인한다. 아래 명령은 파일을 등록하고 기존 Gemma snapshot으로 QLoRA 1 step을 **실제로 실행**한다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_dataset_smoke.py \
  --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
  --gpu-id 0
```

자체 JSONL로 검증하려면 같은 명령에 `--file /absolute/path/to/train.jsonl`을 추가한다. `--file` 생략 시 작은 등록 fixture로 연결만 검증하며 모델 품질 검증은 아니다. 일반 학습 클라이언트는 기존에 등록한 버전 ID를 `POST /api/training-runs`에 직접 지정하면 된다. 이 smoke의 새 실행은 자기 검증용 Dataset을 만든다.

등록 전용 보고서의 `--resume`은 원래 모드로만 실행하므로 GPU 학습으로 전환되지 않는다. 위 GPU 명령은 별도 검증 Dataset·버전을 등록하고 그 버전을 학습 입력으로 고정한다. GPU 실행을 재개할 때는 그 실행에서 새로 출력된 `[record]` 경로를 사용한다.

`training_passed=true`는 TrainingRun의 고정 URI·hash·크기·레코드 수, 완료 결과의 동일 pin, Operation 성공, 새 `ready/training/adapter`의 원본·학습 연결을 확인했다는 뜻이다. 결과는 같은 보고서에 저장한다. 실행 도중 중단되면 위 `--resume`을 사용하며 모델·파일·API 주소는 보고서의 원래 값을 따른다.

## 새 adapter 실제 서빙 — 검증 완료

보고서 `tests/results/serving/06f308d7-bb14-4e30-8847-afc0fa875b59.json`의 터미널 summary에서 passed·idempotent_replay·stopped가 모두 true였다. Deployment `723e900e-7243-4224-ae9f-a974eeed6141`이 ready가 된 후 새 adapter 이름으로 `Hello!`를 반환했고, 중지 후 apply_status=ready·Operation succeeded가 유지됐다. 런타임은 중지됐고 로그·캐시는 남아 있다.

아래는 같은 adapter의 재검증 명령이다. GPU 0이 비어 있는 상태에서 실행하며 adapter 이름으로 채팅 응답·멱등 재요청·중지·기존 완료 상태 보존을 확인한다. 문서 변경을 적용하기 위한 이미지 재빌드는 필요 없다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_serving_smoke.py \
  --model-version-id 4460a3b4-a059-4a9f-8785-ea9884564bcd \
  --gpu-id 0
```

이 마지막 단계까지 통과해 등록 데이터셋 → 학습 → 새 adapter → 실제 추론 경로를 확인했다. 과거 adapter `80e34e1f-f5da-4448-a0ca-282e76d78526`와 별개로 이번 학습 결과 자체를 검증했다. 중지된 서빙 보고서를 `--resume`하여 다시 띄우지 않으며, 의도한 재검증은 위 명령으로 새 배포를 만든다.

## 실패 확인

| 증상 | 확인할 사항 |
|---|---|
| HTTP 400 | JSONL 구조, 소문자 필드명, 최소 2개 레코드, multipart의 version/file, 잘못된 UUID |
| HTTP 409 | 같은 버전 이름의 다른 파일 또는 같은 요청 키의 다른 입력. 새 내용은 새 버전 이름으로 등록 |
| HTTP 413 | 파일 64 MiB 또는 multipart body 65 MiB 제한 |
| HTTP 503 | Training의 MinIO 연결·버킷 권한·readback. 동일 파일·버전·키로 재요청 |
| `unsupported dataset source_type "version"` | Axolotl Job 이미지가 이전 코드. 새 이미지 빌드 후 **새 학습 요청**으로 실행; 실패한 승인 Job은 자동 재실행하지 않음 |
| `DATASET_RESOLVE_FAILED` | 등록 객체 유실·변경 또는 pin 오류. Training GET의 dataset과 해당 Job 로그 확인 |
| `TRAINING_ARTIFACT_PUBLISH_FAILED`와 최소 여유 공간 오류 | 학습 후 MinIO 결과물 업로드 실패. MinIO `/data`의 실제 파일시스템 공간과 inode를 확인한다. 이번에는 같은 SSD의 미사용 Docker 빌드 캐시를 정리한 뒤 새 학습 요청으로 성공했다. MinIO 데이터 디렉터리를 직접 지우지 않는다 |
| 완료 pin 불일치 | 새 Training과 Job 이미지가 함께 적용됐는지, 결과의 dataset 필드가 저장된 pin과 같은지 확인 |
| 서빙 중 `RUNTIME_CREATE_UNCONFIRMED` | Docker 생성 결과를 확정하지 못한 중간 관찰 상태. 이번에는 후속 확인으로 ready가 된 뒤 추론·중지까지 PASS했다. 계속 반복되면 Deployment GET의 runtime_error_message와 Deployment·Docker 로그를 확인한다. 코드만으로 원인을 단정하지 않는다 |

실패한 학습 요청의 `--resume`은 상태를 성공으로 바꾸거나 GPU 작업을 다시 시작하지 않는다. 완료 출력이 보존된 업로드 실패는 [출력 복구](training-output.md)로 학습 없이 재시도할 수 있다. 학습 자체가 실패했다면 checkpoint 유무와 [재개 조건](training-checkpoints.md)을 확인한다. 중단·응답 유실 후 아직 진행 중인 요청을 관찰할 때는 원래 GPU 실행 보고서로 `--resume`한다.

2026-09-09 작성 환경의 Python 8개 테스트·정적 검사와 사용자 서버의 Go 28개·하위 사례 29개 PASS를 확인했다. Go 테스트는 실제 PostgreSQL과 메모리 객체 저장소를 사용했다. 이후 Training·Gateway 전환, 실제 MinIO 등록, 등록 버전을 고정한 GPU 학습·새 adapter 등록·실제 추론·중지까지 PASS했다. 확인한 범위는 [현재 상태](../status.md)와 [이력](../history/model-platform-202609.md#2026-09-09-04-dataset-versioning)에 기록했다.
