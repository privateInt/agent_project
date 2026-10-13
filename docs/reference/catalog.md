# 목록·검색 API

비전문가용 Streamlit 체험 페이지에서 모델을 이름으로 찾고, 버전을 선택한 다음 관련 학습·배포 이력을 조회할 수 있도록 하는 API다. Streamlit 화면은 [체험 페이지](streamlit-playground.md)로 구현했으며 서버 검증 상태는 [status](../status.md)를 따른다. 모델·학습·배포 생성이나 실행 정책은 바꾸지 않는다.

## 신규 GET 경로

아래 경로는 모두 `limit`, `cursor`를 받는다.

| 경로 | 추가 필터 | 검색·선택 대상 |
|---|---|---|
| `/api/models` | `q`, `model_type` | 모델 이름·설명의 부분 문자열 |
| `/api/model-versions` | `q`, `model_id`, `state`, `artifact_kind`, `source_type` | 모델 이름·버전 이름·HF repo ID의 부분 문자열 |
| `/api/models/:id/versions` | `q`, `state`, `artifact_kind`, `source_type` | 경로의 모델에 속한 버전. `model_id` 쿼리를 함께 보내면 400 |
| `/api/training-runs` | `status`, `source_model_version_id`, `dataset_version_id` | 선택한 원본 버전·데이터셋 버전의 학습 이력 |
| `/api/deployments` | `status`, `model_version_id`, `runtime_state` | 선택한 버전의 배포 이력·현재 서빙 상태 |
| `/api/operations` | `status`, `resource_type`, `resource_id`, `kind`, `phase` | 리소스의 실행·전달·GPU 대기 상태 |

필터끼리는 AND다. `q`는 지정된 검색 필드 중 하나만 일치해도 되며, 양끝 공백 제거 후 200자 이하다. PostgreSQL `ILIKE`를 사용한다. 영문 대소문자 구분 없이 한글을 포함한 문자열을 검색하며 `%`, `_`, `\`는 와일드카드가 아닌 입력 문자 그대로 비교한다. 형태소·초성·유사어 검색은 제공하지 않는다.

ID 필터는 UUID 형식을 검사해 정규화한다. enum 필터는 공백 제거·소문자 변환 후 허용 값을 검사한다. `kind`, `phase`는 공백 제거 후 정확히 일치하는 값으로 검색한다. 빈 필터는 조건을 생략한다. 형식은 맞지만 존재하지 않는 ID는 빈 목록을 반환한다. 중첩 모델 버전 경로도 같은 규칙을 사용한다.

| 필터 | 허용 값 |
|---|---|
| 모델 `model_type` | `llm`, `embedding`, `reranker`, `ocr` — 등록 유형이며 모든 유형의 실행 지원을 뜻하지 않음 |
| 버전 `state` | `importing`, `ready`, `failed`, `deleting`, `deleted` |
| 버전 `artifact_kind` | `file`, `snapshot`, `adapter` |
| 버전 `source_type` | `huggingface`, `upload`, `training` |
| 학습 `status` | `created`, `running`, `cancelling`, `cancelled`, `succeeded`, `failed` |
| 배포 `status` | `created`, `applying`, `ready`, `failed`, `unhealthy`, `stopping`, `stopped` |
| 배포 `runtime_state` | `queued`, `waiting_resources`, `preparing`, `unknown`, `starting`, `ready`, `failed`, `unhealthy`, `exited`, `stopping`, `stopped` |
| Operation `status` | `pending`, `dispatching`, `running`, `retry_wait`, `cancelling`, `succeeded`, `failed`, `cancelled` |
| Operation `resource_type` | `model_version`, `training_run`, `deployment` |

상태는 도메인마다 다르다. GPU 대기 중 TrainingRun은 `created`, Operation은 `pending/phase=waiting_resources`일 수 있다. 학습 상태를 임의로 `queued`로 바꾸거나 실행 중으로 추정하지 않는다. 학습 취소 범위는 [학습 제어](training-control.md)를 따른다. 모델 목록 삭제는 아래 카탈로그 삭제를 사용한다.

## 응답·페이지 이동

```json
{
  "items": [],
  "next_cursor": ""
}
```

- `limit`: 기본 20, 범위 1..100. 빈 값·잘못된 숫자는 400이다.
- 정렬: `created_at DESC, ID DESC`. 동일 생성 시각은 UUID로 순서를 고정한다.
- `next_cursor`: 다음 페이지가 있을 때만 비어 있지 않다. 클라이언트는 내용을 해석하거나 만들지 않고 그대로 전송한다.
- 검색·필터를 바꾸면 커서를 지우고 첫 페이지를 요청한다. 다른 컬렉션·필터의 커서는 400이다. 페이지 크기는 바꿀 수 있다.
- 새 항목이 추가되어도 이어 읽는 페이지의 위치를 밀지 않는다. 커서 기준 행을 삭제해도 다음 페이지를 읽을 수 있다. 새 항목은 첫 페이지를 새로 조회해 확인한다.
- 여러 요청에 걸친 DB snapshot은 아니다. 중간에 상태가 바뀌거나 데이터가 삭제되면 현재 조건에 맞는 결과도 달라질 수 있다. 전체 건수·임의 페이지 번호·다중 정렬은 제공하지 않는다.
- 신규 경로는 알 수 없는 쿼리 파라미터와 같은 키의 반복을 400으로 거절한다.
- `created_at`, `updated_at`은 UTC RFC3339 형식 문자열이다. 빈 결과는 항상 `items: []`다.

한 목록 요청은 도메인 DB의 단일 조회로 처리한다. 다른 도메인을 항목 수만큼 호출하지 않는다. 생성 시각·ID 인덱스 5개를 서비스의 스키마 초기화에 추가했다. 부분 문자열 검색은 별도 전문 검색 인덱스가 없으며, 대규모 데이터에서는 실제 쿼리 계획을 측정한 뒤 보강해야 한다.

## 목록 항목과 상세 조회

목록은 화면 선택에 필요한 요약이며, 기존 단건 GET의 모든 필드를 담지 않는다. gRPC도 기존 리소스 메시지를 재사용하되 목록에서 선택하지 않은 상세 필드는 기본값이다. 클라이언트가 목록 값을 상세 정보 전체로 덮어쓰지 않도록 구분한다.

| 목록 | 포함하는 정보 |
|---|---|
| 모델 | ID, 이름, 유형, 설명, 생성·수정 시각 |
| 버전 | 버전·모델 ID, `model_name`, `model_type`, 버전 이름·상태·출처·artifact 유형·repo ID, 학습·원본 연결, Operation 전달 상태, 생성·수정 시각 |
| 학습 | 원본·출력 버전 ID, 상태, 데이터셋 pin·학습 설정·자원, MLflow 연결, Operation 전달 상태, 생성·수정 시각 |
| 배포 | 버전 ID, 상태·apply/runtime/desired state, 오류, serving 설정·자원, artifact 유형·served model name, 사용 가능한 endpoint, Operation 전달 상태, 생성·수정 시각 |
| Operation | 기존 공개 GET의 실행 상태·오류·GPU 대기 정보와 생성·수정·시작·완료 시각. 실행 승인 token·owner·instance는 gRPC 목록에서도 제외 |

버전의 artifact URI·hash·파일 수, 학습의 `result/result_json`, 배포의 컨테이너 식별자·전체 원본 연결 등은 선택 후 기존 단건 GET으로 조회한다. 중지되었거나 준비되지 않은 배포의 목록에는 추론 endpoint를 반환하지 않는다. 모델이 실제로 응답하는지는 기존 readiness·추론 경로에서 확인한다.

## 모델 카탈로그 삭제

`DELETE /api/models/:id`는 해당 모델의 deleted_at을 기록한다. 모델 목록과 전체/모델별 버전 목록은 deleted_at이 없는 부모만 포함한다. 모델 GET/DELETE 응답에는 deleted_at(삭제 전 null)을 포함하며 같은 UUID의 반복 DELETE는 최초 삭제 시각을 보존한다.

카탈로그에서 제외해도 모델/버전 상세 GET은 지원하며 파일·학습/배포/Operation 이력과 실행은 유지한다. 삭제 뒤 새 import는 거절하고 이미 접수된 요청의 같은 키 replay는 원래 결과를 반환한다. 이름 재사용·목록 복구는 지원하지 않는다. 이 DELETE만으로 파일 공간을 회수하지 않으며, 물리 삭제는 아래 별도 purge API를 사용한다. [UI와 삭제 범위](streamlit-playground.md#모델-목록-삭제와-배포-종료)를 참고한다.

## 기존 Dataset 목록과의 호환

`GET /api/datasets`에만 `q`를 추가했다. 이름·설명의 부분 문자열을 검색하며 위와 같은 문자·길이 규칙을 적용한다. 기존 `limit` 기본 50, `after_id`, `datasets/next_after_id` 응답, UUID 오름차순은 유지한다. `/api/datasets/:id/versions`는 변경하지 않았다.

Dataset의 `after_id`는 필터를 검증하는 새 커서가 아니므로 검색어를 바꿀 때 클라이언트가 반드시 비워야 한다. Dataset에 새 `cursor/items` 형식을 보내지 않는다.

## Streamlit 연결

사용자는 모델 이름·설명으로 검색하고 버전 이름·상태로 선택한다. UUID는 화면의 주 입력으로 요구하지 않고 선택 결과에 담긴 ID를 API 호출에 사용한다. 이어서 선택한 버전의 학습·배포를 조회하고, 필요한 항목만 상세 조회한다. 검색 조건 변경 시 페이지 이력을 초기화하고 다음 커서와 이전 커서 이력은 Streamlit session state에서 관리한다.

모델 선택·예제 입력·응답 확인, 쉬운 상태·오류 안내를 갖춘 체험 페이지를 추가했다. 학습을 노출할 경우 검증한 기본 설정을 제공하고 작업 시작 전 명시적인 사용자 동작을 받는다. 목록 조회나 화면 새로고침으로 학습·배포를 시작하지 않는다.

추론은 Streamlit 서버가 Gateway의 POST /api/deployments/:id/chat에 요청하는 구조다. Gateway는 ready 배포의 내부 vLLM 주소로만 전달한다. 브라우저가 보고서의 호스트 `127.0.0.1` 주소를 직접 호출하도록 만들지 않는다. 새 연결의 실제 서버 검증은 [체험 페이지 절차](../testing/streamlit-playground.md)를 따른다. 현재 인증·권한과 외부 공개 운영을 추가 구현한 것은 아니다.

## 파일 영구 삭제

`POST /api/models/:id/purge`는 선택한 모델의 **모든 버전**을 대상으로 저장소의 `models/<model_id>/` 아래 파일과 객체 버전·삭제 마커, 종료된 배포의 전용 캐시를 삭제한다. 본문·쿼리 없이 정확한 UUID를 지정한다. 기존 `DELETE /api/models/:id`의 목록 삭제 의미는 유지한다.

가져오기·학습·결과 복구가 실행 중이거나 배포 중지 확인이 안 되면 거절한다. 새 배포·학습·결과 복구와 삭제는 해당 버전의 DB 트랜잭션 잠금을 공유한다. 도메인별 영구 차단 기록을 남겨 사전 조회와 생성 사이의 경합을 막는다. 중간 단계가 실패하면 일부 버전에 차단 기록이 남을 수 있으며 같은 모델에서 삭제를 재시도한다. 다른 작업을 자동 중지하지 않는다.

스토리지 오류·객체 잠금은 성공으로 처리하지 않는다. `purge_state=purging`에서 다운로드/실행에 사용할 준비된 버전을 제공하지 않으며, 확인된 삭제 후 `purge_state=purged`, `purged_at`과 버전 `state=deleted`를 기록한다. 반복 요청은 같은 삭제 시각을 반환한다. 관리 ID·원본 해시·작업 이력은 중복 요청과 출처 확인을 위해 보존한다. 학습 데이터셋, 별도 학습 작업 파일·체크포인트, 외부 백업과 MLflow 기록은 삭제 범위에 포함하지 않는다. 원격 노드의 학습 프로세스 중지를 로컬에서 확인할 수 없으면 거절한다.

`GET /api/models?include_deleted=true`는 목록에서만 삭제한 항목을 포함한다. 파일 삭제를 완료한 모델은 제외하며 커서는 이 필터에 묶인다. 모델 UI 필터에서도 같은 옵션을 제공한다. 파일 공간 회수는 위 영구 삭제 API에 한하며 기존 DELETE만으로는 수행하지 않는다.

## 표시 이름 변경과 배포 목록 제외

2026-09-18 추가한 PATCH 모델/버전 이름 변경과 DELETE 배포 정리는 [관리 수정 계약](model-management-update.md)을 따른다. 삭제 완료 배포는 기본 목록에서 제외하고 단건 GET 이력을 유지한다. 기존 모델 카탈로그 삭제·파일 purge와 다른 기능이다.
