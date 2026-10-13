# 문서 QA Q01 — API 연결과 작업 상태 계약

Q01 최초 범위(2026-10-02)는 순수 계약·합성 fixture이며 서버 API/workflow/모델 호출·HTTP/DB/화면 구현이 없었다. 이후 업로드 저장/API와 n8n·CPU·색인 adapter를 구현하고 TXT 실제 CPU 인수·Q05b 보류 callback을 확인했다. 최신 범위는 [상태표](../status.md)에 두며 문서 QA 제품은 아직 미완료다. 진행 범위는 [전체 계획](../plans/document-qa-work-plan-20261002.md)을 따른다.

구현은 [contracts.py](../../src/apps/document_qa/contracts.py), API별 입력/receipt/조회 경로와
코드 해시는 [API 계약 목록](../../src/apps/document_qa/api-contract.json), 결과는
[오프라인 검증](../testing/document-qa-q01-offline-20261002.json)에 둔다. 합성 fixture의 ID·
본문·시간은 검사용이며 운영 리소스나 실제 모델 실행 결과가 아니다.

## 기존 API의 접수와 완료

| 단계 | 요청 | 반환과 다음 확인 |
|---|---|---|
| 원본 | `POST /api/source-documents`, multipart `name/file` | **200** 원본 `document_id/state/sha256/size_bytes`. Operation/Job 없음. 보낸 원본의 hash/크기와 ready를 확인 |
| 정제 | `POST /api/refinement-runs`, `name/document_id`, 선택 `rules_version/ocr_deployment_id` | **202** `refinement_run_id/operation_id/status=accepted`. `GET /api/refinement-runs/{id}`의 status와 output ID를 조회 |
| 본문 | `GET /api/canonical-documents/{output_document_id}`와 `/content` | output ID·source·refinement·schema·byte hash·본문을 확인. 초기 계약은 JSON을 읽음 |
| 색인 생성 | `POST /api/indexes/{id}/builds` | **202** index version 전체. `index_version_id/operation_id/chunking_run_id`를 보존하고 `GET /api/index-versions/{id}` 조회 |
| 활성화 | `POST /api/indexes/{id}/activate` | `index_version_id/expected_active_version_id/expected_revision`, **200** logical index. 생성 멱등 키가 아니라 revision 비교 계약 |
| 검색 Tool 시험 | `POST /api/tool-calls`, `tool_version_id/arguments/mode=test` | **202** `tool_call_id/tool_version_id/state/status_url`. 실제 검색은 `result.data`, 고정 모델/색인 정보는 `result.metadata` |
| QA 시험/질문 | `POST /api/agent-runs`, `agent_version_id/input.question/mode` | **202** `agent_run_id/agent_version_id/state/status_url`. 조회의 `succeeded`와 `answered/no_evidence`를 구분 |

색인 build는 완료된 `chunking_run_id` 또는 새 `chunking={name,canonical_ids,chunker}` 중
하나만 받는다. 새 chunking과 별도 canonical subset을 함께 보내지 않는다. 새 버전은
기존+추가 자료의 **전체 입력 snapshot**으로 준비하며 이전 버전에 자동 합쳐진다고 가정하지 않는다.

**build 접수 때도 logical index revision이 증가한다.** ready 확인 뒤 최신 index를 읽어
활성화 조건을 만든다. 오래된 build 전 revision을 그대로 사용하지 않는다. 활성화 응답이
유실되면 먼저 GET으로 목표 버전이 이미 활성화됐는지 확인한다. 같은 조건의 POST를 무조건
반복하거나 새 상태를 모른 채 expected_revision만 바꾸지 않는다.

Agent의 Tool/Agent 생성·버전 생성·시험·발행은 해당 공개 API를 사용한다. 발행은 같은
draft version의 성공한 test가 있어야 가능하며 `expected_revision`을 보낸다. Tool과 Agent
정의/Connection은 Agent가 소유하고 연결 계층은 ID와 버전 참조를 저장한다. publish 응답
200만으로 자료함의 질문 대상 전환이 끝났다고 표시하지 않는다.

## 질문 가능 조건

원본 등록 → 정제 완료 → 본문 확인 → 색인 ready/활성화 → Tool/Agent 시험·발행·readback
→ 자료함 질문 대상 전환 순서다. 단계별 성공과 마지막 전환을 구분한다.

| 대상 | 초기 계약의 확인 조건 |
|---|---|
| 정제 | 예상 source/run/operation과 일치, `status=succeeded`, output ID 존재 |
| 본문 | 조회한 output ID와 일치, source/run과 byte hash/크기 일치, schema 일치, 읽을 text 존재 |
| 본문 품질 | metadata와 JSON quality가 모두 `ready`. `needs_ocr/needs_review`, unresolved OCR, 빈 본문은 주의 필요로 중단 |
| 색인 | 같은 index/version/operation, 전체 canonical ID 집합 일치, `state=ready`, `reference_state=held`, 양수 청크 수=실제 색인 수, 고정 embedding 배포/공간 일치 |
| Tool | `published/knowledge_search_v1`, 같은 색인 버전·embedding 배포/공간, 같은 version의 성공한 검색 시험과 envelope identity |
| Agent | `published/knowledge_qa_v1` v1, 위 Tool 고정, 같은 version의 성공한 QA 시험, Tool/Knowledge 결과 identity 일치 |
| 자료함 | 위 readback으로 만든 `VerifiedHead`를 사용하고, 현재 revision 비교 후 자기 DB에서 원자적으로 질문 대상 전환 |

`verify_bindings`는 확인한 참조를 반환하고 `switch_head`는 revision 전환의 순수 판정을 한다.
현재 코드가 DB 트랜잭션이나 실제 서버 상태 검증을 대신하지 않는다. 저장·API 호출·원자적
직렬화는 Q03/Q05/Q06에서 구현한다. 이후 질문은 저장된 head의 immutable Agent 버전에
접수하며 실행별 당시 revision/버전을 보존한다.

새 문서 추가가 실패하면 기존 정상 head를 사용하되 새 문서가 준비됐다고 표시하지 않는다.
**제외 전환 중에는 새 질문을 대기**시킨다. 제외 전에 접수된 실행은 당시 버전을 유지한다.
마지막 문서를 제외한 상태는 ready head 없음으로 표시한다. 검색 제외는 Data 원본의
영구 삭제가 아니며 Q09에서 실제 변경/재개 흐름을 인수한다.

Agent 조회의 `no_evidence`는 정상 실행 결과지만 질문에 답했다는 뜻은 아니다. `answered`는
빈 답변이나 빈 인용으로 표시하지 않는다. 현재 계약 검사는 응답 구조·버전·근거 참조를
검사하며, 인용이 주장에 맞는지와 답변 정확도는 Q10의 별도 평가다. 발행/실행 성공을
의미 품질 PASS로 바꾸지 않는다.

## 연결 계층이 보존할 자기 소유 참조

| 기록 | 값과 소유 경계 |
|---|---|
| 자료함 | UUID, 현재 revision/head, 목표 문서 구성, 변경 종류. 참조만 저장 |
| job | UUID·자료함·목표 revision·현재 단계·상태·실패/주의 이유·확인 시각 |
| 문서 | source ID/hash/크기·refinement/operation·canonical ID/hash/크기. 본문 원본은 Data |
| 단계 intent | 논리 operation·호출 대상/정규화 body fingerprint·멱등 키·attempt·원래 receipt/조회 참조 |
| 질문 head | 전체 canonical 참조·index/version·embedding 배포/공간·Tool/Agent version |
| 질문/workflow 실행 | 당시 head revision·AgentRun과 n8n 실행 참조. Tool/Agent 정의나 n8n 기록의 두 번째 원본 없음 |

job 상태는 `pending/running/uncertain/attention_required/failed/completed`, 단계는
`upload/refinement/indexing/binding`이다. 사용자 표시의 업로드·내용 읽기·검색 준비·질문 연결과
매핑한다. 같은 stage의 완료를 두 번 전달해도 다음다음 단계로 넘어가지 않는다. 이미 끝난
receipt를 재사용하는 것은 후속 dispatcher의 책임이며, 순서가 다른 이벤트는 진행을 거부한다.
failed/attention 상태를 임의 완료로 바꾸지 않는다.

## 동일 요청과 불확실 접수

Data/Knowledge 생성 키는 1~128 ASCII 규칙이고 Agent POST 키는 canonical nonzero UUID다.
연결 계층의 `request_intent`는 공통으로 UUIDv5를 사용해 job·고정 목표 revision·operation·
attempt에 같은 키를 만든다. 페이지 새로고침·프로세스 재시작·n8n execution ID 변화로 키를
바꾸지 않는다. **body가 바뀌어도 새 키를 자동 생성하지 않고 충돌로 판정**한다.

호출 전 intent를 자기 저장소에 기록한다. body fingerprint에는 요청할 대상 ID/path parameter와
정규화된 실제 body를 함께 넣는다. upload에는 원래 name/filename/byte hash/크기를 포함한다.
같은 논리 operation의 body를 다시 구성해 모델·청커 기본값이나 문서 집합이 바뀌지 않게 한다.
operation 하나가 여러 대상을 처리해야 하면 개별 child job으로 나누고 서로 다른 intent를 둔다.

| 확인된 상태 | 다음 행동 |
|---|---|
| 완료 | 기존 receipt 재사용. 새 source/run/version 생성 안 함 |
| 처리 중, ID 있음 | 원래 resource ID로 상태 조회 |
| 응답 유실, ID 없음 | 원래 키·body로 해당 생성 API replay. 응답의 ID/identity 다시 확인 |
| 활성화 응답 유실 | 현재 index 조회, 목표 버전/revision 확인 후 충돌/완료 구분 |
| 실패/주의 필요 | 이유를 검토. 같은 키로 실패한 작업을 재개했다고 주장하지 않음 |

새 attempt는 **기존 작업이 terminal 실패임을 확인하고 재실행 조건을 정한 경우**에만 별도
intent로 만든다. 불확실 상태에서 attempt를 증가시키면 중복 작업을 만들 수 있다. 자동 재개
정책/실행은 Q09까지 미구현이며 Q01은 이 조건과 판정만 제공한다.

Q03 업로드 단계는 응답 유실 시 같은 파일을 재전달할 수 있도록 접수 중 bytes의 제한된
임시 보관/참조도 정해야 한다. Data 등록 확인 뒤 영구적인 두 번째 원본으로 남기지 않는다.
보관 없이 응답/ID도 잃은 상태를 새 업로드의 성공으로 덮지 않는다. 대기 timeout은 원래
작업의 취소나 terminal 실패를 뜻하지 않는다. `recovery_action`은 실행 없는 행동 판정이다.

## 입력 한도와 Q02 준비 정보

초기 사용자 형식은 텍스트 PDF·TXT·Markdown, 원본 파일은 32MiB 이하다. 준비/활성 문서
예약 수를 합쳐 20개까지로 제한하며 동시 접수의 예약은 Q03 저장소에서 직렬화해야 한다.
Knowledge 입력 canonical JSON의 합계는 32MiB 이하, 청크는 20,000개 이하다. 합계는
정제 뒤 build 전에 확인한다. 초과를 잘라내거나 미완료 자료를 질문 가능으로 표시하지 않는다.
확장자만으로 PDF의 실제 처리 가능/품질을 보장하지 않는다.

Q02가 조회할 연결/기본값은 [API 계약의 q02_inputs](../../src/apps/document_qa/api-contract.json)에
둔다. Data/Knowledge/Agent/Model Gateway와 n8n 시작점의 연결·인증 참조, 자료함 이름,
embedding/chat 배포, 청커와 options가 필요하다. OCR/reranker는 지원 범위의 선택 항목이다.
기존 설정과 배포 목록을 재사용해 0/1/여러 호환 후보·중지·미지원·권한 부족을 구분한다.
실제 준비 상태 조회·자동 선택·설정 저장은 Q02에서 구현하며 Q01에서 완료했다고 표시하지 않는다.

검증한 모델/버전/공간의 논리 ID를 저장하고 host 경로·컨테이너 ID·systemd unit을 업무 계약에
넣지 않는다. 다른 플랫폼 DB/MinIO/OpenSearch에 직접 연결하지 않는다. 현재 source hash는
코드 검토 범위를 표시하며 Docker/Kubernetes 배포 인수의 근거가 아니다.

## 검증과 다음 작업

프로젝트 루트에서 아래 검사만 실행한다.

```bash
python3 -B -m unittest discover \
  -s src/apps/document_qa/tests -p 'test_*.py' -q
```

동기/비동기 접수, 미완료/부분/빈 본문, 결과 유실·동일 키, 전체 자료 snapshot, 색인/Agent
버전 불일치, revision 충돌, 추가 실패/제외 대기, no_evidence/빈 답변, 요청 한도를 검증한다.
외부 API·DB·모델과 기존 서비스 제어는 없다. 전체 플랫폼/Ops 검사를 다시 실행하지 않는다.

다음은 **Q02 연결과 모델 기본값 확인**이다. 그 뒤 Q03a 업로드/job 접수, Q03b workflow
전달로 진행한다. Q01 코드에는 서버 endpoint·자격 값·DB schema·배포 manifest가 없다.
