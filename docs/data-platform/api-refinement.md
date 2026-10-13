# 정제 API와 공통 문서 JSON

2026-09-11. 저장된 원본 버전 하나를 입력으로 받아 독립적인 CPU 정제 실행을 만든다. Go API와 Python 문서 파서는 별도 역할이며, 외부 REST → 내부 gRPC 계약을 유지한다.

## 요청·조회

| 메서드·경로 | 용도 |
|---|---|
| `POST /api/refinement-runs` | 원본 document_id로 정제 실행 생성, 202 |
| `GET /api/refinement-runs?limit=20&q=이름&cursor=...&source_document_id=...` | 정제 실행 목록. source_document_id는 선택적 원본 필터이며 커서에 묶임 |
| `GET /api/refinement-runs/{id}` | 요청 정보, 전달 상태, Operation 상태·단계, 결과 ID·deleted_at |
| `DELETE /api/refinement-runs/{id}` | 종료된 정리 작업을 결과 목록에서 제외, 원본·canonical 보존 |
| `GET /api/canonical-documents/{id}` | 결과 버전 메타데이터, 품질 상태·해시·크기·출처 |
| `GET /api/canonical-documents/{id}/content` | 해시 검증된 공통 JSON 파일 다운로드 |
| `GET /api/operations?resource_type=refinement_run&resource_id={id}` | 정제 실행의 Operation 조회 |

POST에는 `Idempotency-Key` 헤더 하나와 다음 JSON을 보낸다. `rules_version` 생략 시 현재 규칙을 사용한다. 중복·알 수 없는 JSON 키와 null은 거절한다.

```json
{
  "name": "업로드 문서 정제",
  "document_id": "a4de1327-b37f-4e77-b1d1-42dc1c63644e",
  "rules_version": "basic-cleaning-v1"
}
```

동일 키·동일 정규화 요청은 같은 refinement_run_id와 operation_id를 반환한다. 이름·원본·규칙·선택적 OCR 배포가 달라지면 409다. 새 키는 같은 원본이라도 별도 정제 버전을 만든다. 새 요청에서 Collection gRPC로 원본 상태·해시·형식을 고정하고, 원본이 없거나 지원하지 않는 형식이면 실행을 생성하지 않는다. 재전송은 Collection을 다시 조회하지 않으므로 Collection 장애 중에도 이미 커밋한 응답을 재사용한다.

PNG/JPEG/PDF는 선택적 `ocr_deployment_id`로 ready Surya 배포를 지정할 수 있다. 생략하면 CPU 정제만 수행한다. UI는 빠른 작업이 켜졌거나 사용 가능한 OCR을 선택하지 않은 경우, 선택한 배포의 요청 직전 조회가 실패한 경우 이 필드를 생략한다. OCR을 지정한 새 PDF 표 결과는 `ocr-priority-v1` 정책을 사용하고 실제 방식은 선택적 `processing.mode`에 기록한다. 기존 요청의 멱등 본문과 모델 식별자는 변경하지 않는다. [OCR 요청·모델 식별·품질 계약](api-ocr.md)을 따른다.

## 데이터와 실행 소유권

- `data_refinement` DB: refinement_runs, canonical_documents, api_requests, operation_submissions.
- `data-canonical` 버킷: `canonical/{refinement_run_id}/sha256/{digest}/document.json`.
- 실행 종류: `refinement.parse`, 리소스: `refinement_run`, 시도 횟수: 1.
- Operation DB와 Kafka 토픽은 기존 Data 전용 `data_operations` / `data.operations.requested`를 공유한다.
- Collection DB를 직접 읽지 않고 Collection gRPC로 조회·다운로드한다. 원본 ID·바이트·메타데이터를 수정하지 않는다.

정제 API가 자체 DB를 시작 시 생성한다. 상시 정제 API의 DB 생성 권한 요구는 기존 Collection과 같다. 모델 플랫폼 DB·버킷·토픽은 사용하지 않는다.

## CPU 실행과 결과 확정

Job은 실행 시작 승인을 확인한 뒤 원본을 읽는다. 다운로드 크기·해시를 검증하고 Python 파서를 실행하며, 결과의 출처·규칙·이미지 ID를 검사한 뒤 저장한다. Python 프로세스에는 MinIO/gRPC 자격 증명을 넘기지 않는다. URL·외부 파일·매크로·수식 실행은 하지 않는다.

단계는 `reading_source → parsing_and_cleaning → uploading_canonical → verifying_canonical → canonical_registered → completed`다. 현재는 단계 표시이며 퍼센트나 처리 페이지 진행률을 추정하지 않는다.

정제 API는 완료 요청의 operation_id·dispatch_token·execution_instance_id·시작 승인·실행 종류를 확인한다. 저장 객체의 실제 SHA-256·크기, 공통 문서 스키마·원본 해시·규칙·프로세서 버전·품질 상태를 다시 확인한 후 결과 행을 커밋한다. 완료 응답 유실 시 같은 결과만 다시 등록하며, 다른 결과로 기존 버전을 덮어쓸 수 없다.

등록은 성공했지만 Operation 완료 보고가 유실되면 Reconciler가 등록된 결과를 관찰하여 succeeded로 보정한다. 실제 시작 후 자동 재파싱하지 않는다. 등록 여부가 불확실한 종료는 수동 확인 대상으로 남고 원본·결과 객체·종료 컨테이너를 보존한다. 업로드와 DB가 하나의 원자적 트랜잭션인 것은 아니므로, 실패한 실행에 미등록 객체가 남을 수 있다.

Job 제한: CPU 1개, 메모리 1 GiB, PID 128개, 읽기 전용 root, 임시 공간 128 MiB, GPU 없음. 전체 20분, CPU 파서 최대 7분(OCR 지정 시 17분), 원본·결과 각 32 MiB. OOXML 전개 합계 128 MiB, 파트 32 MiB, 엔트리 4096개, 최대 블록·셀 각 100,000개, PDF 페이지·XLSX 시트 각 300개. CPU 구조 한도 초과 시 잘라서 성공시키지 않고 실패한다. OCR 페이지별 한도와 보류 규칙은 [OCR 계약](api-ocr.md)에 있다.

## 결과 품질 상태

| 상태 | 의미 |
|---|---|
| `ready` | 이번 CPU 검사에서 추가 검토 사유가 발견되지 않음. 완벽한 의미·레이아웃 복원을 보장하지 않음 |
| `needs_ocr` | 이미지 영역·추출 불가/의심스러운 PDF 텍스트 등 OCR 검토 대상 존재 |
| `needs_review` | 구조·페이지·읽기 순서 등 CPU 추출의 제한으로 검토가 필요함 |

Operation `succeeded`는 처리와 결과 등록이 끝났다는 뜻이다. 결과가 `needs_ocr`여도 Operation은 succeeded다. 선택적 Surya 연결을 구현했으며 [서버 검증](testing-ocr.md)을 기다린다. LLM 보강은 수행하지 않는다.

## 공통 문서 필드

| 필드 | 내용 |
|---|---|
| `schema_version` | `canonical-document-v1` |
| `source` | 원본 document_id, SHA-256, 입력 형식 |
| `processor` | CPU는 `cpu-parser-v1`, OCR 지정 실행은 `cpu-parser-surya-v1`; `basic-cleaning-v1`과 실제 Docker image ID |
| `blocks` | 문서 순서의 블록, 고정 `b1...` ID와 0부터 시작하는 order |
| `pages` | 추출 가능한 실제 페이지 크기·회전·좌표 기준 |
| `quality` | 상태와 위치별 issues, 각 issue의 requires_ocr |
| `cleaning` | 규칙 버전·규칙 목록·변경 필드 수·보호 대상 |

| 입력 | 이번 보존 범위와 제한 |
|---|---|
| TXT | UTF-8 텍스트 전체, 들여쓰기·탭·연속 공백 보존, 줄바꿈만 LF 통일 |
| Markdown | 전체 Markdown 원문 블록. 코드 fence·hard break 공백 보존. Markdown AST 변환은 하지 않음 |
| HTML | 문단·제목·pre 코드·표의 행/열 병합, DOM 위치. script/style 등 제외. 외부 이미지 URL은 참조로만 보존하고 needs_ocr 표시 |
| DOCX | 본문 문단·표 순서, 셀 gridSpan/vMerge, 스타일 이름, 탭·줄바꿈. 페이지 위치를 계산하지 않아 needs_review. 중첩 표는 텍스트를 보존하고 구조 제한 기록. 머리글·바닥글·각주 등 미추출 부가 파트와 이미지·수식 구조를 표시 |
| XLSX | 희소 셀 주소·행열·원본 값 타입·수식·캐시값·병합 범위·시트 이름/숨김 상태·셀 스타일과 숫자 형식. 원시 숫자값을 날짜 표시 문자열로 바꾸거나 수식을 계산하지 않음. shared/array 수식은 XML 속성을 남기고 확장하지 않음을 표시 |
| PDF | 페이지별 텍스트 줄·표 셀·이미지 영역 bbox, 페이지 회전, top-left 기준 PDF point 좌표. 정렬은 위→아래·왼쪽→오른쪽 휴리스틱이므로 needs_review. 표 병합/무테 표·다단 읽기 순서의 완전 복원은 보장하지 않음 |
| PNG/JPEG | 이미지 크기·pixel 좌표, needs_ocr. 가짜 텍스트를 만들지 않음 |

기본 cleaning은 줄바꿈 통일과 일반 HTML 본문의 NFC 정규화다. 코드·Markdown·셀 원시 값·수식은 보호하며, 모든 연속 공백을 제거하거나 문서를 한 줄로 합치지 않는다. `quality.issues`는 후속 OCR·품질 검증 입력이다.


## 결과 목록에서 삭제 (2026-09-16)

`DELETE /api/refinement-runs/{id}`는 하나의 정리 작업을 결과 목록에서 제외한다. 정규형 refinement_run_id를 경로로 전달하며 본문·쿼리는 받지 않는다. 성공은 HTTP 200과 해당 refinement_run_id·deleted_at이 포함된 메타데이터다. 최초 삭제 시각은 반복 요청에서도 유지한다. 이미 삭제된 항목의 재확인은 Operation 서비스 장애 중에도 같은 상태를 반환한다.

처음 삭제할 때 Operation을 5초 제한으로 조회하여 operation_id·resource_type/refinement_run·resource_id·execution_type/refinement.parse를 확인한다. 상태가 succeeded/failed/cancelled 중 하나인 경우만 허용한다. 미전달·실행 중·잘못된 연결은 409이며 관측 오류는 성공으로 처리하지 않는다. 현재 Operation의 종료 상태는 다시 실행 상태로 전환되지 않으므로 확인한 종료 조건은 유지된다.

`refinement_runs.deleted_at TIMESTAMPTZ`만 기록하고 canonical_documents·원본·Operation·객체 저장소는 수정하지 않는다. 목록 검색/커서에서 삭제된 실행을 제외하며 기존 ID의 GET 및 canonical 다운로드·원본 비교는 유지한다. 실행 취소·연쇄 삭제·파일 영구 삭제 API는 아니다.

CreateRun/CreateOCRRun은 새 논리 요청에서 Collection의 deleted_at을 확인하여 삭제된 원본을 거절한다. 이미 커밋한 원래 키의 재전송은 기존 실행 ID를 반환하고 삭제 상태를 복원하지 않는다. 새 컬럼은 Refinement 시작 시 기존 행을 보존하며 추가한다. 이번 변경의 DB 검사는 적용 전 필수 검사로 실행하고, 로컬 검증과 구분한다.

## 정리 결과 파일 영구 삭제

`POST /api/refinement-runs/{id}/purge` (본문·쿼리 없음)는 종료된 정리 작업의 `data-canonical/canonical/<run_id>/` 아래 파일과 모든 객체 버전을 삭제한다. 완료 콜백 전에 남은 해당 작업의 파일도 포함한다. 원본 자료는 별도로 삭제한다.

Operation의 식별자·종류·최종 상태를 확인하며 실행 중/미확인 작업은 거절한다. 파일 삭제 전에 `purge_state=purging`을 기록해 결과 다운로드와 늦은 완료 콜백을 막는다. 삭제가 확인된 후 `purged_at`을 고정하고 목록에서 제외한다. 실패 시 동일 ID로 재시도하며 삭제 완료로 표시하지 않는다. `include_deleted=true`로 목록에서만 지운 결과도 조회할 수 있다.

Collection 전용 내부 RPC `BlockSourcePurge`는 새 정리 생성과 같은 source ID 잠금을 잡아 미삭제 결과를 검사하고 영구 차단한다. 이 RPC는 외부 HTTP 경로로 노출하지 않는다. API 접수/출처/Operation 메타데이터는 삭제되지 않는다.
