# 선택형 청킹 계약 — 첫 구현

갱신일: 2026-09-21. 새 구조 청커 구현은 `2026-09-21.1`, recursive는 `2026-09-18.2`다. 기존 `.1`·`.2` 결과는 저장한 버전으로 실행·검증한다. [변경·검증 기록](api-chunking.md#structure-context)을 따르며 새 버전 실행·48개와 recursive 44개 결과를 [사용자 자료로 확인](acceptance.md#k08)했다. 기존 `.2`의 실제 48개 결과와 색인·검색·rerank는 확인했다.

## 구현 경계

Knowledge Gateway → Knowledge 도메인 gRPC → 전용 도메인 DB/전달 의도 → Operation/Outbox/Kafka → Dispatcher → 승인된 CPU Job → 청크 저장/등록 → 완료 보고다. Reconciler가 시작 전 lease와 등록된 결과의 보고 유실을 처리한다.

Model 배포나 GPU는 필요 없다. Data의 기존 canonical만 읽으며 수집·정제/OCR을 다시 실행하지 않는다. 이 절은 CPU 청킹 경로다. 구현된 색인·검색·선택적 rerank 경로는 각 API 계약에서 별도로 설명한다.

| 저장/실행 대상 | 이름 |
|---|---|
| 도메인 DB | `knowledge_catalog` |
| 실행 DB | `knowledge_operations` |
| 청크 저장 버킷 | `knowledge-chunks` |
| Kafka 토픽 / consumer group | `knowledge.operations.requested` / `knowledge-platform-dispatchers` |
| 실행 종류 / resource type | `chunking.run` / `chunking_run` |
| CPU Job 이미지 | `knowledge-platform-chunking-job:dev` |
| 기본 HTTP 주소 | API와 화면을 함께 `:15100`에 제공, 기본 LAN 바인딩 |

Data/Model의 DB·버킷을 직접 읽지 않는다. 실행 제어는 현재 Data의 승인·Outbox·멱등성·Reconciler 패턴을 Knowledge namespace로 분리해 사용한다. 삭제 정리는 Knowledge 소유의 종료 Job만 대상으로 기존 성공 1시간/실패 7일 보존 기준을 적용한다.

## 입력과 재현성

첫 입력 방식은 **사용자가 명시적으로 고른 canonical ID 목록**이다. 그룹은 자료 목록의 조회 조건이다. 그룹 전체를 원자적으로 snapshot한 것으로 표시하지 않는다. 접수 시 각 canonical의 source ID/hash, canonical hash/크기, 정제 실행·스키마·processor·규칙·이미지·형식을 읽어 정렬된 manifest를 저장한다.

`snapshot_id`는 정렬된 manifest의 SHA-256이며 `spec_sha256`는 snapshot과 청커 정책을 포함한 명세의 SHA-256이다. 청커 ID·구현 버전·기본값을 적용한 옵션·정책 hash를 고정한다. 재전송에서는 이전 접수 응답을 반환하며 Data를 다시 읽지 않는다. 실행 Job은 고정한 입력 identity와 실제 수신 바이트의 해시/크기를 재확인한다.

그룹 소속 변경이나 새 정제 결과는 이미 접수된 작업에 반영되지 않는다. 아직 실행하지 않은 canonical이 삭제/숨김/영구 삭제 중이면 첫 구현은 명확히 실패하며 최신 결과로 바꾸지 않는다. 저장된 ChunkSet 조회는 Data 재실행을 요구하지 않는다.

청킹 미리보기 자체는 Data 참조 보호를 등록하지 않는다. K04 인덱스 빌드에서는 참조 등록·purge 보호를 구현했고 실제 빌드의 held 상태를 확인했다. 실제 삭제·참조 해제 검증은 별도로 남아 있다.

## 청커와 보존 규칙

| ID | 구현 |
|---|---|
| `recursive_character_v1` | 인접 텍스트 블록을 출처 구간과 함께 묶고 문단→줄바꿈→문장/공백 경계를 재귀적으로 찾는다. 한도를 넘는 구간은 문자 기준으로 나눈다. |
| `structure_v1` | 인식된 절 안에서 블록 끝을 우선해 크기와 overlap을 적용한다. 연속 제목은 뒤 본문과 묶고 필요하면 재귀 구분자/문자 경계로 나눈다. |

새 구조 청커 작업은 `2026-09-21.1`, recursive 작업은 기존 `2026-09-18.2`다. 과거 `.1`·`.2` 작업은 저장된 정책과 ID를 유지한다. 옵션 변경에는 새 작업/key를 사용한다. 두 청커 모두 문서 경계를 넘지 않고 짧은 사실을 버리지 않는다. 새 구조 청커의 크기 분할에는 짧은 여러 블록 사이에서도 overlap이 적용된다. 단위는 연결용 `\n\n`을 포함한 출력 Unicode code point이며 실제 출처 범위만 spans로 기록한다. 본문 뒤의 heading, 표·코드·제외·문서 경계에서는 overlap을 넘기지 않는다. 제목 자체가 한도를 넘거나 뒤 본문이 없으면 제목 단독 조각이 남을 수 있다. 일반 text의 제목 추정과 canonical 수정은 하지 않는다. PDF 파싱·OCR·구조 판별은 Data 책임이며 제목 보완은 이 API의 동작·완료 조건에 포함하지 않는다. 번호나 서식으로 새로운 heading을 만들지 않고 입력에 제공된 유형을 사용한다.

한도 이하의 표·코드는 한 청크로 보존한다. 큰 표는 cells와 원문 text를 순서대로 정확히 대응시킨 행으로 분할하며, 명시적인 pipe Markdown 표의 헤더/구분선도 지원하며 구분선은 헤더 행 범위에 포함한다. 단순 OCR 줄바꿈을 행으로 가정하지 않는다. 선두 헤더와 이어지는 병합 셀의 기준 행은 원문 범위로 반복한다. 코드 블록은 줄 단위로 묶고 들여쓰기·개행을 유지한다. 구조 분할에는 문자 overlap을 적용하지 않으며 반복 문맥을 포함한 청크 전체가 `max_characters` 이하이다. 한 행(반복 문맥 포함)/한 줄이 초과하면 `TABLE_ROW_TOO_LARGE`/`CODE_LINE_TOO_LARGE`에 `required_characters`를 표시한다. 행 대응 불가·병합 Markdown 등 안전하게 해석할 수 없는 표는 `TABLE_ROW_BOUNDARIES_UNAVAILABLE`로 실패한다. 임의 셀 절단·본문 유실·조용한 한도 확대는 하지 않는다. cells·병합·수식·bbox·시트·기타 필드는 원래 block metadata에 남는다. 본문을 읽은 image/unhandled_structure 블록은 읽힌 텍스트를 포함하고, 미인식 그림과 빈 블록은 위치·제외 이유로 기록한다. 원본 품질 경고를 그대로 보존한다.

source spans는 Unicode code point 기준 `[start, end)`이며 청크 내 대응 범위 `chunk_start/chunk_end`도 제공한다. 블록 사이에 넣는 `\n\n`은 원문 span으로 가장하지 않는다. 표/코드/text의 출력 종류와 원래 block type을 구분한다. 청크 ID에는 canonical identity·청커 정책·내용 hash·출처 spans를 포함한다.

| 한도/기본값 | 값 |
|---|---|
| 입력 개수 | canonical 1~20개, 중복 ID 거절 |
| 입력 전체 JSON | 합계 32 MiB 이하 |
| 최대 청크 문자 수 | 기본 1,000, 설정 100~8,000 |
| overlap | 기본 100, 0 이상이고 최대 글자 수의 절반 미만 |
| 최대 청크 수 / 결과 JSON | 20,000개 / 64 MiB |
| 미리보기 요약 | 1 MiB 이하 |
| 페이지 | 기본 20개, 최대 100개·응답 2 MiB 이하로 조정 |

문자 수는 모델 토큰 한도가 아니다. 후속 embedding 단계에서 별도의 입력 한도 검증이 필요하다. 한도 때문에 조용히 자료를 누락하지 않는다.

## API

| 메서드·경로 | 동작 |
|---|---|
| `GET /api/chunkers` | 실제 두 청커·버전·설명·기본 옵션·한도 |
| `GET /api/source-groups` | Data 그룹 조회, `limit/cursor/q` |
| `GET /api/sources` | Data 원본 목록, `group_id/q/limit/cursor` |
| `GET /api/sources/{id}/canonical-versions` | 해당 source의 정제 실행 목록. `output_document_id`가 있는 버전을 선택 |
| `POST /api/chunking-runs` | 고정 입력·옵션 접수, `Idempotency-Key` 필수, 202 |
| `GET /api/chunking-runs` | 저장된 작업의 가벼운 목록, `limit/cursor` |
| `GET /api/chunking-runs/{id}` | 명세·전달 상태·결과 등록·coverage·품질·제외 사유 |
| `GET /api/chunking-runs/{id}/chunks` | 저장된 청크 페이지, `limit/cursor` |
| `GET /api/operations/{id}` | 실행 상태·phase·오류. 실행 승인 token은 노출하지 않음 |

Data에는 기존 `GET /api/refinement-runs`와 ListRuns gRPC에 선택적 `source_document_id` 필터를 추가했다. 기존 필터 없는 호출은 유지한다. Knowledge의 계약 사본은 wire package를 유지하며 Data 수정본과 함께 빌드해야 한다.

접수 본문 예시:

```json
{
  "name": "안내문 청크 확인",
  "canonical_ids": ["22222222-2222-4222-8222-222222222222"],
  "chunker": {
    "id": "structure_v1",
    "options": {"max_characters": 1000, "overlap_characters": 100}
  }
}
```

202 응답은 `chunking_run_id`, `operation_id`, `status: accepted`를 반환한다. 실행 완료는 결과 등록 여부와 Operation 상태로 구분한다. 같은 key와 같은 명시적 요청은 같은 두 ID를 반환하고 다른 요청은 409다. 실패한 작업을 새 설정으로 실행하려면 새 요청/key를 사용한다.

청크는 `chunk_id`, `source_document_id`, `canonical_id`, `ordinal`, `text`, `content_sha256`, `kind`, `source_spans`, `quality`를 제공한다. 분할한 표·코드에만 `structure`가 추가된다: `unit`은 rows/lines, `start/end`는 0부터 시작하는 원본 행/줄의 반열린 범위, `part/parts`는 1부터 시작하는 분할 순번/총수, `context_rows`는 반복한 원본 행 번호다. 화면은 1부터 표시한다. 반복 문맥도 `source_spans`에 실제 원문 위치를 기록하며 유일한 원문 문자 coverage에는 중복 합산하지 않는다. 구조 정보는 청크 ID에 포함한다. 이전 결과에는 이 필드가 없어도 동일 바이트·ID로 조회된다. 원문 metadata는 span에 보존한다. 미리보기 커서는 run과 결과 hash에 묶이며 다른 결과에 재사용할 수 없다.

첫 저장 형식은 단일 JSON artifact다. 페이지 조회 때도 전체 artifact를 해시 검증한다. 소규모 미리보기의 단순한 시작점이며 대용량 조회 최적화는 별도 후속 작업이다.

## 화면과 실패 처리

화면은 자료/그룹 조회 → 정제 버전 선택 → 청커/옵션 → 생성 → 상태/청크/출처 확인이다. 완료된 결과에서 ‘같은 입력으로 다른 청커 준비’를 누르면 canonical 목록과 크기 옵션을 그대로 설정한다. 비교 화면은 실제 snapshot hash가 같을 때만 같은 입력 비교로 표시한다.

응답 유실 시 전송 본문/key를 브라우저에 보존하고 같은 요청으로 재전송한다. 요청을 바꾸어 중복 생성하지 않는다. 202를 받은 뒤 결과 조회만 실패하면 접수된 ID를 안내한다. 내용은 HTML로 실행하지 않고 텍스트로 표시한다.

Job은 승인 전 Data 읽기/계산/쓰기 작업을 하지 않는다. 확정된 입력/청킹/업로드 실패는 실패로 보고한다. 결과 등록 응답 유실은 실패의 증거가 아니므로 덮어쓰지 않고 Reconciler가 등록 receipt를 관찰한다. 실행한 사실이 불명확한 작업을 무조건 다시 실행하지 않는다.


<a id="structure-context"></a>

## 구조 청커의 문맥 보존

새로 접수하는 `structure_v1`의 구현 버전은 `2026-09-21.1`이다. `recursive_character_v1`은 `2026-09-18.2`를 유지한다. 이전 structure/recursive `.1`·`.2` 명세는 고정한 버전으로 실행·검증한다. 새 정책의 해시와 청크 ID는 이전 정책과 구분된다. API 접수 형식과 DB 스키마는 동일하다.

- 구조 청커는 인식된 절 안의 텍스트를 모으고 블록 끝을 우선해 크기 창을 나눈다. 적절한 블록 끝이 없으면 기존 재귀 구분자 또는 문자 경계를 사용한다.
- 길이로 나눈 인접 창에는 지정한 overlap을 유지한다. 단위는 Unicode code point이며 출력의 블록 연결용 `\n\n`도 문자 수에 포함한다. 출처 spans에는 실제 원문만 기록한다.
- 본문 뒤의 heading은 새 절을 시작한다. 본문 앞의 연속 heading은 함께 모아 뒤 본문과 배치한다. 제목 자체가 한도를 넘거나 문서 끝에 본문이 없는 경우에는 제목만 있는 조각이 남을 수 있으며 원문을 버리지 않는다.
- 문서·인식된 절·표·코드·제외 블록 경계를 가로질러 overlap을 넘기지 않는다. 겹침과 연결용 개행만 있는 청크를 추가로 만들지 않는다.
- 표/코드의 행·줄 분할과 반복 헤더 규칙, 최대 길이, 원문 위치·metadata·품질 경고 보존은 유지한다. 빈 블록도 기존 제외·경계 정책을 유지한다.
- `text`를 제목으로 재분류하거나 OCR 문구를 교정하지 않는다. Data 구조 인식 개선은 별도 보류이며 향후 착수한다면 Data 범위에서 검증한다.
