# 데이터셋 등록·버전 계약

이 문서는 **현재 Model 소유의 legacy 계약**이다. 2026-10-12 [F1 Data 이전 계획](../plans/platform-expansion-work-plan-20261012.md)을 채택했으나 구현/데이터 이전은 아직 아니다. 이전 학습·요청 키·버전/파일 pin은 이 계약으로 보존하고 새 Data 경로의 API/namespace·resolve/읽기 연동·생성 전환은 단계별 검증한다.

데이터셋 메타데이터는 Training 서비스의 `model_training` DB에서 관리한다. 새 상시 서비스는 추가하지 않는다. JSONL 파일 하나를 등록하면 검증된 저장 위치·SHA-256·바이트 수·레코드 수를 가진 `ready` 버전이 생긴다. 학습은 버전 UUID를 지정해 정확한 파일을 고정한다.

## API

| 요청 | 입력 / 응답 |
|---|---|
| `POST /api/datasets` | JSON `name`, 선택적 `description`. Dataset 반환, HTTP 201 |
| `GET /api/datasets` | `datasets`, `next_after_id` |
| `GET /api/datasets/:id` | Dataset 조회 |
| `POST /api/datasets/:id/versions` | multipart의 `version` 필드 하나와 `file` 파일 하나. DatasetVersion 반환, HTTP 201 |
| `GET /api/datasets/:id/versions` | `versions`, `next_after_id` |
| `GET /api/dataset-versions/:id` | DatasetVersion 조회 |

목록은 UUID 오름차순이다. `limit`은 1..100, 기본 50이다. 응답의 `next_after_id`를 다음 요청의 `after_id`에 넣고 빈 문자열이면 끝낸다. 생성 시간순 목록이나 조회 중 추가된 행 전체를 보장하는 스냅샷 페이지는 아니다.

Dataset 이름은 trim 후 1..128자, 설명은 최대 4096자다. 이름은 중복될 수 있으며 UUID가 식별자다. 버전 이름은 영문·숫자로 시작하는 1..64자의 영문·숫자·점·밑줄·하이픈이고 같은 Dataset 안에서 유일하다. 버전 수정·삭제 API는 제공하지 않는다.

## 파일 형식과 범위

- UTF-8의 압축하지 않은 `.jsonl` 파일 하나, 최대 64 MiB.
- 빈 줄을 제외한 레코드가 최소 2개. 현재 Axolotl debug preprocess의 최소 데이터 수에 맞춘다.
- 한 레코드는 최대 1 MiB. 모든 레코드를 검사한다.
- 레코드에는 소문자 `messages` 필드만, 각 메시지에는 소문자 `role`, `content` 필드만 허용한다. `role`은 `system`, `user`, `assistant`; `content`는 공백만으로 이루어지지 않은 문자열이다.
- 각 레코드에 `user`와 `assistant` 메시지가 모두 있어야 한다. 도구 호출·이미지·추가 컬럼·null content·Parquet·여러 파일·train/eval split은 이번 범위에 포함하지 않는다.

```json
{"messages":[{"role":"user","content":"2+3?"},{"role":"assistant","content":"5"}]}
{"messages":[{"role":"user","content":"2*4?"},{"role":"assistant","content":"8"}]}
```

줄 끝 LF/CRLF, 공백, 마지막 개행을 포함한 **업로드 원본 바이트**를 해시한다. JSON을 정규화하거나 재작성하지 않는다. 빈 줄은 레코드 수에 포함하지 않지만 해시와 바이트 수에는 포함한다. 구조 검증을 통과해도 특정 모델의 chat template·토큰 길이·학습 품질 적합성을 보장하지는 않는다. 실제 tokenizer 검증은 기존 Axolotl preprocess 단계가 수행한다.

Gateway의 전체 body 제한은 파일·multipart 여유분을 합쳐 65 MiB이며, 개별 파일 제한은 64 MiB다. Gateway는 multipart를 파싱하고 Training에는 256 KiB 이하의 gRPC 스트림 조각으로 전달한다. HTTP 전체 경로가 메모리를 사용하지 않는 스트리밍이라고 가정하면 안 된다. 구현에 사용하는 [Fiber multipart/body 제한](https://docs.gofiber.io/api/fiber/)과 [gRPC client streaming](https://grpc.io/docs/languages/go/generated-code/)의 동작을 따른다.

## 저장과 불변성

Training은 업로드를 임시 파일에 받고 전체 JSONL을 검사한다. 이후 MinIO에 저장하고 실제 객체를 다시 읽어 SHA-256·크기·레코드 수를 대조한 뒤 버전 메타데이터를 커밋한다. 등록 요청은 동기 처리이며 Operation·Kafka·GPU Job을 만들지 않는다.

기본 버킷은 `model-datasets`이고 `MODEL_DATASET_BUCKET`으로 바꿀 수 있다. Training은 기존 `MODEL_MINIO_*` 연결 설정을 사용한다. 저장 키는 다음과 같다.

```text
datasets/<dataset UUID>/sha256/<원본 파일 SHA-256>/train.jsonl
```

같은 Dataset·버전 이름·동일 바이트의 재전송은 같은 버전과 최초 응답을 반환한다. 같은 이름에 다른 바이트는 HTTP 409이며 새 버전 이름을 사용해야 한다. 같은 파일을 별도 버전 이름으로 등록할 수 있고 저장 객체는 같은 내용 주소를 사용한다.

두 POST 모두 선택적 `Idempotency-Key`를 지원한다. 같은 키의 다른 정규화 요청도 HTTP 409다. Dataset 생성에서 키를 생략하면 새 Dataset이 생긴다. 버전 업로드는 키를 생략해도 Dataset·버전 이름의 고유 제약이 중복을 막는다. 동시 업로드는 DB 고유 제약으로 한 버전에 수렴한다. 이미 등록된 동일 버전의 응답 재현은 MinIO 재쓰기를 요구하지 않는다.

객체 저장과 DB는 단일 트랜잭션이 아니다. 업로드 후 DB 실패나 동시 요청의 충돌은 참조 없는 내용 객체를 남길 수 있다. 이 경우 새 `ready` 버전·요청 키는 커밋되지 않는다. 중간 객체 GC는 아직 없으며 기존 모델/Job GC가 데이터셋을 삭제하지 않는다.

여기서 불변성은 **플랫폼 API의 버전 교체 금지와 학습 파일 무결성 검사**를 의미한다. MinIO Object Lock이나 관리자 SQL/객체 직접 수정 차단을 추가한 것은 아니다. `ready`는 등록 시 검증 성공 기록이며 매 GET마다 객체를 재검증하지 않는다. 저장 객체를 외부에서 바꾸거나 지우면 Job이 학습 전에 실패한다.

## 학습 버전 고정

```json
{
  "source_model_version_id": "<ready snapshot UUID>",
  "dataset": {
    "source_type": "version",
    "dataset_version_id": "<DatasetVersion UUID>"
  },
  "resources": {"gpu_ids": [0]}
}
```

Training은 요청 키 재현을 먼저 확인한다. 최초 요청이면 같은 DB 트랜잭션에서 버전을 읽고 URI·해시·크기·레코드 수를 `training_runs.dataset_spec`에 복사한 뒤 학습과 예약 Operation 전달 행을 함께 커밋한다. 새 데이터셋 버전을 올려도 이전 학습의 입력은 바뀌지 않는다. 호출자는 `source_uri`, `sha256`, `size_bytes`, `record_count`를 지정할 수 없다.

Job은 실행 승인 후 고정 URI에서 파일을 읽고 모든 메타데이터가 맞을 때만 로컬 학습 파일을 확정한다. 값이 다르거나 파일이 없으면 `DATASET_RESOLVE_FAILED`로 종료한다. 다른 버전이나 synthetic 데이터로 대체하지 않는다.

학습 결과에는 `dataset_version_id`, `dataset_sha256`, `dataset_bytes`, `dataset_record_count`, `dataset_source_type`, `dataset_source_uri`, `dataset_format`을 남긴다. Training의 완료 커밋도 저장된 pin과 이 값을 대조한다. MLflow에는 `src.platform.model.dataset_version_id`, `src.platform.model.dataset_sha256` 태그를 추가한다. 모델 결과에서 `training_run_id` → TrainingRun의 dataset pin을 통해 데이터 계보를 조회할 수 있다.

기존 `synthetic`·`s3` 요청과 기존 요청 키 fingerprint는 유지한다. 기존 S3 직접 지정은 파일 내용 고정을 보장하지 않으며, 버전이 필요한 학습에는 등록 경로를 사용한다. 이전 Job 이미지는 `source_type=version`을 지원하지 않아 실패하므로 새 등록 버전의 학습 전에 Training·Gateway와 Axolotl Job 이미지를 함께 빌드해야 한다. [적용·검증 절차](../testing/datasets.md)를 따른다.

## Dataset 이름 검색

`GET /api/datasets?q=검색어`로 이름·설명을 검색한다. 검색어는 공백 제거 후 200자 이하의 부분 문자열이며, `%`·`_`·역슬래시는 입력 문자 그대로 비교한다. 기존 `limit`·`after_id`, UUID 오름차순, `datasets/next_after_id` 응답은 유지한다. 검색어 변경 시 `after_id`를 비운다. 다른 도메인의 새 목록 형식은 [목록·검색 계약](catalog.md)을 참고한다.
