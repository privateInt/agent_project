# 수집 API 계약

기본 URL: `http://127.0.0.1:14000`. 내부 gRPC API는 Docker 네트워크용이며 호스트에 공개하지 않는다. 인증·권한 관리는 이후 운영 확장 범위다. 첫 배포의 HTTP 포트는 loopback에 바인딩한다.

| Method | 경로 | 동작 |
|---|---|---|
| POST | `/api/crawl-runs` | 수집 요청 접수, 202와 crawl_run_id/operation_id |
| GET | `/api/crawl-runs?limit=20&cursor=...&q=...` | 수집 요청 목록 |
| GET | `/api/crawl-runs/{id}` | 수집 요청, 전달 상태, Operation 상태·단계·오류, 대표/전체 결과 문서 ID·한도 도달 flag |
| POST | `/api/source-documents` | 파일 업로드·원본 등록, HTTP 200 ([계약](api-upload.md)) |
| GET | `/api/source-documents?limit=20&cursor=...&q=...&group_id=...` | 등록된 원본 버전 목록 |
| GET | `/api/source-documents/{id}` | 원본 출처·해시·크기·처리기·상태·deleted_at |
| DELETE | `/api/source-documents/{id}` | 자료 목록에서 원본 항목 제외, 파일·출처 보존 |
| GET | `/api/source-documents/{id}/content` | 검증한 수집 결과 다운로드. 브라우저 문서는 DOM |
| GET | `/api/source-documents/{id}/http-response` | 브라우저 문서의 최초 HTTP 응답 다운로드 |
| GET | `/api/operations?status=...&resource_type=crawl_run&resource_id=...` | 작업 목록 |
| GET | `/api/operations/{id}` | 공개 작업 상태 |
| GET | `/health` | Gateway 프로세스 응답 확인, 의존 서비스 준비를 보장하는 probe는 아님 |

자료 그룹 생성·이름 변경·멤버 추가/제외는 [그룹 API](api-groups.md)를 따른다. 원본·정제 결과를 복사하거나 변경하지 않는다.

## 요청

```http
POST /api/crawl-runs
Content-Type: application/json
Idempotency-Key: crawl-demo-001
```

```json
{
  "name": "예제 문서",
  "url": "https://example.com/",
  "options": {
    "max_bytes": 8388608,
    "timeout_seconds": 120
  }
}
```

이름은 공백 정리 후 1~200자이며, options를 생략하면 표의 기본값을 적용한다. 현재 options 값 0도 기본값을 뜻한다. URL fragment를 제거하고 호스트·기본 포트를 정규화한다. 쿼리 문자열은 보존한다. API 본문은 16 KiB까지며, 중복 키·알 수 없는 키·잘못된 대소문자·잘못된 자료형을 거절한다.

```json
{"crawl_run_id":"UUID","operation_id":"UUID","status":"accepted"}
```

접수 응답은 동일 키 재요청 시에도 동일하다. 실행 중/완료 여부는 GET으로 조회한다. 접수했다고 원본이 바로 만들어졌다는 뜻은 아니다. `submission_status=blocked`는 Operation 전달에 조치가 필요함을 뜻하며 실제 수집 실패와 구분한다.

## 확장 옵션과 다중 결과

같은 호스트 HTML 링크와 첨부를 수집하려면 아래 옵션을 명시한다. 기본 단일 URL 요청의 기존 fingerprint는 변하지 않는다. 확장 옵션 변경은 같은 키에서 409다.

```json
{
  "name": "공개 자료 수집",
  "url": "https://example.com/documents/",
  "options": {
    "follow_links": true,
    "include_attachments": true,
    "max_depth": 1,
    "max_documents": 5,
    "max_bytes": 8388608,
    "max_total_bytes": 33554432,
    "timeout_seconds": 120
  }
}
```

`output_document_id`는 seed 원본이며 `output_document_ids`는 seed부터 수집 순서대로 정렬된 전체 원본 ID 배열이다. 미완료는 빈 배열, 기존 완료 요청은 대표 ID 하나다. 원본 상세에는 `parent_url`, `crawl_depth`, `crawl_role`(`seed`/`page`/`attachment`)을 추가한다. 이전 원본과 업로드는 역할/부모가 빈 문자열이다.

한도 때문에 생략한 후보는 `document_limit_reached`, `depth_limit_reached`, `link_limit_reached`로 표시한다. 이 flag가 켜진 `succeeded`는 한도 내 선택 문서 등록이며 전체 사이트의 완전 수집을 뜻하지 않는다. 선택 문서의 다운로드/저장 실패·용량/시간 초과는 실패다. 모든 결과를 검증하고 하나의 트랜잭션으로 등록한다.

기본값·한도, 같은 호스트/첨부 판정, HTTP 모드의 JavaScript 미실행, 마이그레이션과 실제 검증은 [확장 가이드](testing-collection-scope.md)를 따른다.

## 브라우저 수집

`options.render_javascript=true`는 별도 `collection.browser` CPU Job을 선택한다. `render_wait_ms`는 load 후 대기(기본 1000ms, 최대 10000ms)이며 나머지 범위 옵션을 함께 쓸 수 있다. DOM은 `/content`, 최초 HTML 응답은 `/http-response`에 저장한다. `browser_capture` 메타데이터로 표현·응답 해시/크기/객체 주소·네트워크 지표를 구분한다. 첨부는 렌더링하지 않는다. [전체 옵션·한도·검증](testing-browser-collection.md)을 따른다. 오류는 `BROWSER_CAPTURE_FAILED`, 렌더링 단계는 `rendering_pages`다.

## 상태와 단계

Operation 상태는 `pending → dispatching → running → succeeded/failed`다. 수집 상세 응답은 Operation 등록 전에는 `created`/`submission_pending`을 반환한다. 실행 준비 복구 시 같은 Operation이 다시 pending이 될 수 있지만 이미 시작한 다운로드를 자동 반복하지 않는다.

| 단계 | 의미 |
|---|---|
| queued / launch_retry_queued | 실행 요청 대기 / 시작 전 재전달 대기 |
| dispatching | 컨테이너 시작 권한 예약 |
| execution_started | 프로세스 시작 승인 완료 |
| downloading_source | URL HTTP 응답 다운로드 |
| uploading_source | MinIO 원본 저장 |
| verifying_source | Collection이 저장 파일 검증·등록 |
| source_registered | 원본 문서 ID 등록 확인 |
| collecting_sources | 확장 범위 URL 순차 수집·저장 |
| verifying_sources | 확장 원본 전체 검증·원자적 등록 |
| sources_registered | 확장 원본 ID 목록 등록 확인 |
| completed / reconciled_succeeded | 직접 성공 보고 / 등록 결과에 따른 완료 보정 |

진행률 숫자는 이번 단계에서 아직 제공하지 않는다. 백분율을 임의로 추정하지 않고 단계를 표시한다. 목록 응답은 요청 메타데이터를 페이지 단위로 반환하고, 개별 실행의 최신 상태는 상세 API 또는 Operation 목록을 통해 읽는다. 공개 API에는 dispatch token·실행 프로세스 권한을 반환하지 않는다.

목록 기본 limit=20, 최대 100. 최신 생성 시각·ID 내림차순의 cursor pagination이며 cursor는 검색 조건에 연결된다. `q`는 이름과 URL의 부분 검색이며, 원본 문서 목록에서는 업로드 원래 파일명도 검색한다. Operation 목록은 status/resource_type/resource_id/kind/phase로 필터한다. 응답은 `items`와 `next_cursor`이며 마지막 페이지의 cursor는 빈 문자열이다.

원본 크기는 JSON 정수, SHA-256은 소문자 64자리 hex다. 수집 문서의 `processor_image_id`는 Job이 실제 실행한 Docker image ID다. 업로드 문서는 Job이 없어 이 값과 `crawl_run_id`가 빈 문자열이며 `source_type=upload`로 구분한다. 수집 문서는 `source_type=crawl`이다. 본문 다운로드는 `application/octet-stream`, `Content-Disposition: attachment`로 응답하므로 원본 HTML을 Gateway origin에서 실행하지 않는다. `X-Content-SHA256` 헤더도 제공한다.

## 오류

| 코드 / HTTP | 의미 |
|---|---|
| InvalidArgument / 400 | 입력·URL·옵션·cursor 오류 |
| AlreadyExists / 409 | 같은 멱등 키에 다른 요청 |
| NotFound / 404 | 리소스 없음 |
| Unavailable / 503 | 의존 서비스 접근 불가 |
| DeadlineExceeded / 504 | 조회·요청 처리 대기 초과 |
| DataLoss / 502 | 다운로드 원본 해시·크기 불일치 |
| Operation `LAUNCH_REJECTED` | Job 이미지 없음, 시작 전 실행 설정 오류 |
| Operation `CRAWL_FETCH_FAILED` | HTTP status, URL 접근, 제한 초과 또는 전송 오류 |
| Operation `CRAWL_STORAGE_FAILED` | MinIO 연결 또는 저장 실패 |
| Operation `CRAWL_INPUT_UNAVAILABLE` | 등록된 수집 요청 확인 실패 |

API 재시도는 원래 키·원래 요청을 유지한다. 결과 등록 응답 유실은 실패라고 단정하지 않고 저장된 원본과 결과 등록을 관찰한다. 단순히 smoke 대기 시간이 끝났다고 원격 작업이 취소되지는 않는다.


## 자료 목록에서 삭제 (2026-09-16)

`DELETE /api/source-documents/{id}`는 단일 원본 문서의 `deleted_at`을 기록한다. 본문·쿼리 매개변수 없이 정규형 UUID를 사용한다. 성공은 HTTP 200과 해당 document_id 및 UTC deleted_at이 포함된 원본 메타데이터다. 반복·동시 요청은 최초 삭제 시각을 유지한다. 존재하지 않는 ID는 404, 잘못된 ID/본문/쿼리는 400이다. 별도의 요청 키가 필요하지 않다.

ListDocuments는 삭제된 항목을 검색·커서 페이지에서 제외한다. 원본 파일·브라우저 최초 응답·수집 내역·정제 결과·Operation은 지우지 않으며 GET/다운로드는 기존 ID로 계속 가능하다. 웹 수집 상세에 같은 원본이 남아 있으면 UI에서 목록 삭제 상태를 표시한다. 저장 공간을 비우거나 접근 권한을 폐기하는 API가 아니다.

정제 서비스는 삭제된 원본으로 새 요청을 생성하지 않는다. 삭제 전에 원본 확인을 마친 동시 접수 요청과 이미 등록된 작업은 계속될 수 있다. Collection/Refinement 사이의 분산 잠금이나 실행 취소는 추가하지 않았다. 기존 수집 완료/업로드 요청의 멱등 재전송은 같은 ID를 반환하며 삭제 상태를 복원하지 않는다. 삭제 상태는 GET 메타데이터에서 확인한다.

Collection 시작 시 `source_documents.deleted_at TIMESTAMPTZ`를 추가한다. 기존 행은 NULL로 유지하며 원본 바이트·해시·식별자와 다른 테이블은 바꾸지 않는다. [UI 확인·검증](testing-console.md#목록-숨김과-파일-영구-삭제)을 따른다.

## 원본 파일 영구 삭제

`POST /api/source-documents/{id}/purge` (본문·쿼리 없음)는 원본 객체와 브라우저 HTTP 응답 증거의 **모든 객체 버전**을 실제 삭제한다. 기존 DELETE는 목록 삭제다. `purge_state`, `purged_at`을 응답에 포함한다.

수집 작업 종료와 Refinement 참조 확인이 필요하다. 해당 원본에 연결된 정리 작업이 하나라도 영구 삭제되지 않았으면 `FailedPrecondition`/`SOURCE_HAS_RESULTS`로 거절한다. 먼저 정리 결과를 영구 삭제하고 원본을 지운다. Refinement의 생성과 공유하는 DB 잠금·영구 차단 기록으로 조회 후 생성 경합을 막는다. 새 정리, 다운로드, 완료 통지는 삭제된 파일을 복원하지 않는다. 기존 upload 요청 replay는 원래 ID를 반환하되 파일을 다시 쓰지 않는다.

원본 삭제 중 실패 시 `purge_state=purging`이며 같은 UUID로 다시 요청한다. 확인된 삭제만 `purged`로 표시한다. 삭제 시각·ID·원본 해시와 작업 이력은 남기고 다른 원본·결과를 연쇄 삭제하지 않는다. 외부 백업은 삭제 대상이 아니다. `GET /api/source-documents?include_deleted=true`는 목록에서 숨긴 원본을 포함하되 파일 삭제 완료 항목은 제외한다.
