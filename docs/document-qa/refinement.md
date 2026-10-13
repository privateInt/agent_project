# Q04a — CPU 문서 정제 접수와 결과 검증

2026-10-07: Data 정제 API를 연결하는 adapter와 새 검사 41개를 구현했다.
[검증 기록](../testing/document-qa-q04a-offline-20261007.json)은 임시 SQLite, loopback Data/
QA HTTP, 대역 비동기 상태와 **기존 Data TXT·Markdown CPU 파서의 실제 출력**을 포함한다.
서버 applied/written=18·41개(1.413초) OK와 자기 init/status는 [기록](../testing/document-qa-q04a-server-observed-20261007.json)했다. 초기 단계의 운영 연결은 후속이었고, 현재는 [기존 TXT job의 운영 Data Gateway/정제 작업·canonical 본문 검증](../testing/document-qa-cpu-live-observed-20261007.json)이 통과했다. PDF·OCR·색인·답변 품질
인수도 미실행이다. Q03b 서버 applied/written=19·36개(1.803초) OK·자기 init/status와
동일 자료함 보존은 [사용자 출력](../testing/document-qa-q03b-server-observed-20261007.json)으로 확인했다.

Q04는 두 단위로 나눴다. **Q04a**는 운영자용 정제 접수/조회/검증 adapter다.
**Q04b**는 [별도 n8n Flow와 제한된 실행권](refinement-workflow.md)을 구현했다. 기존 TXT job의 명시 업로드 재시도 뒤 native 진입/CPU Flow를 전달해 운영 정제까지
확인했다. 두 Flow 등록·활성 버전·QA API 재실행은 완료했고 신규 업로드의 자동 전체 경로 인수는 후속이다. 일반 사용자는 후속 UI에서
업로드와 질문만 수행하며 아래 기술 명령을 입력하지 않는다.

## 적용과 로컬 확인

Q03b까지 적용하고 자기 자료함을 초기화한 프로젝트에서 실행한다.

```bash
python3 -B -m unittest discover -s src/apps/document_qa/tests -p 'test_refinement.py' -q
python3 -B -m src.apps.document_qa.refinement init
python3 -B -m src.apps.document_qa.refinement status
python3 -B -m src.apps.document_qa.api status
```

패치/검사/init/status는 운영 API·GPU·모델·HTTP 서버를 실행하지 않는다. init은 기존
uploads.sqlite3에 refinement 전용 metadata/task/lane 테이블을 한 트랜잭션으로 추가한다.
upload user_version=1, workflow_metadata=1, 기존 자료함/job/key/원본/키 파일을 유지한다.
새 credential은 만들지 않는다. 기존 Q03a/Q03b 코드도 원래 테이블을 읽을 수 있다.
기본 저장은 프로젝트 밖 `$HOME/.local/state/agent-project-document-qa/uploads`다.
초기 active_job_id=null, refinement_counts={}는 정상이다. question_ready=false,
n8n_refinement_connected=false, ocr_supported=false를 유지한다.

## 명시 CPU 정제 — 실제 연결 인수 때만

기존 Q02와 같은 settings/connections-from의 **Data origin/auth 파일 참조**만 재사용한다.
Ops 코드를 import하거나 플랫폼 DB/스토리지에 직접 접근하지 않는다. serve는 기존 QA
API에 아래 운영자 경로를 제공하며, 서버를 띄우는 것만으로 정제 요청을 보내지 않는다.
Data 요청은 기존 API bearer 참조를 필요한 시점에 읽으며 키/설정을 수정하지 않는다.

Data receipt가 검증된 preparation_pending job을 대상으로만 다음 동작이 가능하다.
아래는 실제 Data API를 호출하는 별도 인수 절차이며 적용 명령에는 포함하지 않는다.

```bash
# DOC_QA_JOB_ID: 해당 자료함의 확정된 업로드 job ID
python3 -B -m src.apps.document_qa.refinement dispatch --job-id "$DOC_QA_JOB_ID"
python3 -B -m src.apps.document_qa.refinement observe --job-id "$DOC_QA_JOB_ID"
# 접수 응답이 불확실하고 운영자가 재확인하기로 한 경우에만:
python3 -B -m src.apps.document_qa.refinement retry --job-id "$DOC_QA_JOB_ID"
```

모든 POST 입력은 서버가 보존한 job에서 만든다. 고정 name=document-qa-{job_id},
document_id=확정 Data 원본, rules_version=basic-cleaning-v1이며 OCR 필드는 보내지 않는다.
사용자가 모델·URL·rules·새 요청 key를 덧붙일 수 없다. parser_only는 이번 단계의 명시
CPU 모드이며 OCR 실패를 CPU 성공으로 대체하는 fallback이 아니다. 스캔/이미지 PDF에서
OCR이 필요하면 OCR_DEFERRED로 남긴다. GPU 보류 기간에 OCR 기동/추론을 추가하지 않는다.

| API | 역할 |
|---|---|
| GET /api/refinements | 로컬 정제 상태 집계/현재 library lane 조회 |
| GET /api/jobs/{job_id}/refinement | 로컬 intent 상태/접수 및 canonical 참조 조회 |
| POST /api/jobs/{job_id}/start-refinement | operator bearer + JSON {}; 최초 명시 정제 전달 |
| POST /api/jobs/{job_id}/retry-refinement | operator bearer + JSON {}; 미확인 접수를 같은 key/body로 한 번 재확인 |
| POST /api/jobs/{job_id}/observe-refinement | operator bearer + JSON {}; 원격 상태를 한 번 조회, 완료 시 canonical 확인 |

일반 QA operator bearer만 실행을 허용한다. Q03b n8n receipt용 credential은 이 정제
경로에 접근할 수 없다. Q03b receipt의 may_execute_next_stage=false와 고정 Flow는 그대로다.
Q04b 전용 credential과 현재 job/generation/최초 execution grant를 검사한 별도 route가 adapter에 위임한다. 기존 operator route와 Q03b callback 키의 권한은 유지한다.

## 내구성과 직렬화

최초 요청 전에 Data source의 ID/name/filename/SHA/size/format/ready와 삭제 상태를 확인한다.
Data origin은 기존 업로드와 같아야 한다. 요청 body/SHA/Q01 data.refine key와 library lane을
먼저 commit하고 POST 직전에 시도 횟수·confirmation_required를 저장한다. 재시작/응답 유실
뒤에도 같은 job/sequence/원본/요청을 사용한다. 새로운 key나 logical generation을 자동 생성하지 않는다.

확정된 202 receipt의 run/operation ID를 저장해 waiting으로 표시한다. **202는 정제 완료가
아니다.** receipt가 알려지면 retry도 POST를 반복하지 않으며 observe로 기존 run만 조회한다.
receipt가 없을 때 자동 반복하지 않고 명시 retry만 허용한다. 최초 source 확인 뒤 저장한
intent의 재전송은 Collection 조회 성공을 다시 요구하지 않아 기존 접수 회수가 가능하다.
Data origin이 바뀌면 미확인 요청을 다른 서버로 재전송하지 않는다.

한 자료함에는 비종료 정제 작업 하나만 durable lane을 가진다. OS file lock으로 start/retry/
observe를 직렬화하지만 원격 I/O 동안 SQLite 트랜잭션을 잡지 않는다. 프로세스가 종료되면
OS lock은 풀리고 durable lane/intent는 남는다. 모르는 job의 lock 파일을 만들지 않는다.
다른 문서는 업로드할 수 있지만 기존 정제의 불확실 상태를 건너뛰어 새 정제를 시작하지 않는다.

확인된 terminal 실패·검증된 canonical 결과에서 같은 트랜잭션으로 lane을 비운다. valid
needs_ocr/needs_review·빈 본문도 종료된 주의 상태로 남기고 검색 입력으로 승인하지 않는다.
identity/SHA/구조 불일치·접속/권한/조회 오류는 receipt/lane을 보존하며 같은 run을 재조회할
수 있다. 이미 종료된 실패를 retry로 부활시키거나 새로운 원격 작업을 만들지 않는다.
현재 새 generation/문서 제외·관리자 lane 해제 도구는 구현하지 않았으며 자동 DB reset은 없다.

## 완료와 본문 검증

observe는 run을 한 번 GET한다. created/pending/dispatching/running/retry_wait/cancelling은
waiting이다. submission blocked/error는 attention_required로 남긴다. failed/cancelled는
같은 원본/run/operation/CPU provenance를 확인한 뒤 실패로 저장한다. 원격 error_message를
출력/보관하지 않고 DATA_REFINEMENT_FAILED 같은 고정 code를 사용한다.

succeeded라도 output UUID/error/원본 SHA/format/rules/name/삭제/OCR provenance가 맞아야
canonical metadata를 조회한다. 일치하는 canonical/source/run/schema를 확인한 뒤 **고정
Data /api/canonical-documents/{id}/content**에서 JSON을 읽는다. artifact_uri나 원격 payload의
URL을 따라가지 않는다. 메타에 고정한 byte 수와 SHA, UTF-8/중복 JSON/nonfinite, CPU processor/
image/rules/cleaning/processing, block 순서/개수·page 개수, quality를 확인한다.
Q01의 ready canonical gate는 그대로 재사용한다. Data의 모든 parser/geometry 검사를
복제하거나 이 결과로 의미 품질을 인수했다고 주장하지 않는다.

ready·비어 있지 않은 결과만 canonical_verified/refinement_ready=true다. needs_ocr,
needs_review, 빈 본문은 각각 OCR_DEFERRED/CANONICAL_REVIEW_REQUIRED/CANONICAL_EMPTY다.
정제 본문은 확인 중 메모리에만 두며 자기 DB에는 canonical ID/SHA/size·processor·quality
요약/접수 참조만 보관한다. 원문·canonical·벡터/색인의 두 번째 장기 원본을 만들지 않는다.
정제 성공도 question_ready=false다. Q05의 전체 자료 snapshot/build와 Q06 Agent 연결은 후속이다.

## 한도·운영·원복

원격 동작 하나의 timeout은 10초, 공유 budget은 60초, 최대 8회 요청이다. status/GET jobs는
로컬 조회다. observe에는 무한 polling/sleep/background worker가 없고 HTTP POST 재시도도
없다. 상위 자동 Flow의 40 step/600초와 명시 resume은 [Q04b](refinement-workflow.md)에 구현했다. 실제 엔진 timeout/재시작 인수는 후속이다. DNS까지 강제로
중단하는 실행기는 아니다. 메타/접수 응답은 64KiB, canonical은 메타 size와 최대 32MiB,
blocks 100000/pages 300, 명시 POST 시도는 job당 16회다. 이전 20문서 예약 제한을 유지한다.
REFINEMENT_BUSY는 동시에 실행 중인 요청, LIBRARY_REFINEMENT_BUSY는 다른 비종료 job의
lane, REFINEMENT_REVIEW_REQUIRED는 시도 한도다. 조회 실패 후 새 key/DB 삭제로 우회하지 않는다.

기존 Data/Knowledge/Model/Agent/Ops 실행 코드·DB·Compose·worker는 바꾸지 않는다.
패치 원복은 프로젝트 파일만 되돌리고 확장 DB/기존 키/접수/검증된 참조를 보존한다.
원복 뒤 이전 Q03b API와 업로드 상태를 계속 읽을 수 있고 재적용하면 새 정제 기록을 읽는다.
운영 Data의 정제 run/canonical 삭제나 서비스를 제어하지 않는다. 이미 수동 serve 중이라면
새 API 경로는 해당 QA 프로세스를 새 코드로 실행할 때 반영된다.

현재 Linux 단일 호스트/SQLite 볼륨 adapter다. Kubernetes의 API/workflow 소유권은 유지하며
단일 replica/PV 또는 분산 저장/실행권 전환은 별도 인수다. Docker ID/systemd/Ops 의존을
추가하지 않는다. 실제 TXT n8n/정제 연결은 통과했다. 신규 업로드 전체 경로·Markdown/PDF/OCR와 검색/답변/UI/품질은 미완료로 유지한다.

[코드](../../src/apps/document_qa/refinement.py), [결과 검증](../../src/apps/document_qa/canonical_results.py),
[계약/source pins](../../src/apps/document_qa/refinement-contract.json),
[전체 계획](../plans/document-qa-work-plan-20261002.md)을 따른다.
