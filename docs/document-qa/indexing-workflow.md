# Q05b n8n 색인 workflow 연결

2026-10-07: 별도 색인 workflow/실행권·QA API 연결과 기본 추론 금지/모델 준비 gate를 구현했다. **새48개·문서 QA 전체324개 검사, 서버17파일 적용·48개(7.989초) 검사·초기화·별도 Flow 등록/게시 정의·TXT 보류 callback이 통과**했다. [실제 서버 근거](../testing/document-qa-q05b-server-observed-20261007.json). 실제 색인/검색·질문은 미완료다. [오프라인 근거](../testing/document-qa-q05b-offline-20261007.json), [Q05a 서버 적용/후보 plan](../testing/document-qa-q05a-server-observed-20261007.json)을 구분한다.

## 연결 구조와 경계

기존 n8n 2.40.5의 진입/CPU 정제 Flow 정의·resource ID·키를 보존한다. 원래 CPU 전용 단계가 canonical_verified를 확정하면 QA API가 별도의 색인 Flow로 handoff를 전달한다. **새 Flow는 등록 후에만 자동 전달**하며 기존 성공 TXT는 별도 명시 전달로 확인한다. 업로드·Data 정제와 원본/canonical을 다시 생성하지 않는다.

[색인 Flow 저장/실행권](../../apps/document_qa/indexing_flow.py), [9-node 고정 정의](../../apps/document_qa/indexing_flow_template.py), [모델 준비/추론 gate](../../apps/document_qa/indexing_runtime.py), [보류 연결 확인](../../apps/document_qa/indexing_connection.py)를 추가했다. [QA API](../../apps/document_qa/api.py)에 전용 callback·조회/전달 route와 완료 단계 연결을 추가했다. 문서·모델·Knowledge DB 직접 접근은 없고 Knowledge 동작은 [Q05a adapter](indexing.md)에 위임한다.

등록은 명시 prepare에서만 수행한다. 진입/CPU Flow의 활성 정의·게시 버전과 같은 QA/n8n origin을 먼저 검증한다. 새 전용 credential2개와 workflow/게시 버전을 자기 setup에 저장한다. prepare 반복은 기존 자산을 검증/재사용하며 응답이 유실된 생성은 고유 소유 이름으로 확인한다. 기존 자산을 수정/삭제하거나 설정/secret을 회전하지 않는다.

## 실행권과 GPU 보류

| 조건 | 실행 의미 |
|---|---|
| 기본 QA 서버, 추론 옵션 없음 | 신규 색인 쓰기 전에 GPU_VERIFICATION_DEFERRED로 blocked. Model 조회·index 생성/build/activate 모두 수행하지 않음 |
| 작업에 고정된 paused_check | 서버 정책이 허용돼도 해당 세대는 추론을 시작할 수 없음. 연결 인수용 |
| server_policy 작업 + 서버의 명시 추론 opt-in | 고정 embedding profile/청커의 준비 GET gate까지 통과해야 신규 index 생성/build/activate 가능. 실제 사용은 Q05c |
| 이미 receipt가 있는 build | 보류 상태에서도 Knowledge GET으로 결과 확인 가능. 활성화 쓰기는 별도 허용 조건 필요 |
| 불확실 build/create 접수 | 자동 POST 반복 없음. workflow resume으로 domain intent/key를 새로 만들지 않음 |

추론 opt-in은 서버 기동 옵션 --allow-indexing-inference다. **현재 GPU 보류 기간에는 추가하지 않는다.** 사용자/n8n 요청 본문에는 이를 켜는 필드가 없으며 추가 필드는 거부한다. Q05c에서만 기존 Q02 준비/선택 기본값과 실제 Model 상태를 확인하고 별도 명시 실행한다. 모델 다운로드/기동, OCR/reranker/chat 호출은 이 단계에 없다.

실행권은 저장된 job·canonical·자료함 sequence·workflow generation·정의/게시 버전·최초 execution·grant에 고정된다. n8n JWT는 path/handoff/body hash와120초를 검증한다. 중복 execution은 같은 receipt를 받지만 최초 execution의 grant로만 단계 동작을 수행한다. 새 색인 callback 키는 기존 operator/진입/CPU API를 호출할 수 없고 그 키들도 색인 callback을 호출할 수 없다. 공개 job 조회에는 grant가 없다.

## 단계·예산·재개

첫 단계는 논리 index 구성, 다음은 frozen snapshot build 접수, 이후는 상태 GET, 마지막은 fresh revision CAS 활성화/readback이다. 각 단계는 이전 Q05a intent/key/receipt를 재사용한다. 기존 활성 문서와 새 문서를 함께 입력하고, 실패/버전 충돌 시 이전 정상 head를 유지한다. 활성화 완료도 question_ready=false다. 질문 연결은 Q06이다.

새 테이블 indexing_flow_metadata/setup/runs/current/executions와 전용 키2개만 같은 자기 DB/볼륨에 추가한다. 기존 schema/user_version=1·Q05a head/generation·원본/정제 job·key/receipt를 유지한다. 비밀·본문·벡터를 workflow 본문으로 복제하지 않는다. 단일 호스트 file lock이며 여러 replica/Kubernetes 실행 인수는 후속이다.

한 세대는 최대80단계·1200초, 대기는5초다. callback의 준비 GET와 Knowledge 호출은 합쳐125초 예산이며 n8n HTTP timeout은135초다. 종료 전 time/step limit을 저장하고 domain 호출 전에 확인한다. 같은 자료함의 다른 build lane은 이 예산 안에서 대기한다. operator4·단계callback4·receipt 여유4의 총12개 연결로 CPU→새Flow 전달 중 receipt가 들어올 공간을 확보한다.

| Flow 상태 | 후속 |
|---|---|
| awaiting_execution / running | 접수 대기 또는 제한된 단계/조회 진행 |
| blocked | GPU 보류 또는 모델 미준비. 자동 재개 없음 |
| indexing_activated | 정확한 입력 검증·활성 readback과 Q05a head 전환 확인 |
| attention_required | 권한/설정/입력·유실/충돌 등 확인 필요. 자동 POST 반복 없음 |
| expired | time/step budget 종료. 저장된 intent/receipt 유지 |

retry는 불확실 n8n handoff를 같은 body로 명시 확인한다. resume은 blocked/attention/expired workflow의 새 실행 세대/최초 grant만 만든다. 이전 grant는 즉시 무효가 되며 기존 Knowledge build는 재조회한다. 알 수 없는 create/build/activate 결과의 domain POST를 자동 재전송하지 않는다. 정상 진행/완료 Flow는 resume하지 않는다. paused_check를 실제 색인으로 전환하는 것은 Q05c의 별도 명시 resume이며 GPU 보류 중에는 수행하지 않는다.

## 확인된 서버 결과

2026-10-07 사용자 출력 기준으로 prepare-paused는 workflow_registered/published_definition_readback_verified=true였고, accept-paused는 n8n_callback_execution_verified/saved_paused_receipt_verified=true였다. 새 인증 API와 서버 추론 금지 정책을 확인했다. 실제 프로세스 재실행/listening 출력은 별도로 제공되지 않았으므로 결과 확인과 구분한다.

| 대상 | 관찰 결과 |
|---|---|
| 적용/초기화 | written=17·48개(7.989초) OK·init/status passed |
| 색인 Flow | workflow_id=st0prKQNT3XIYasC · 게시 버전=838e64bc-84e3-4fe7-9fe0-0f4d4afb77bc |
| 실행권 | generation=1 · 최초 execution=8 · delivery=accepted · steps=1 |
| 보류 | state=blocked · error_code=GPU_VERIFICATION_DEFERRED · inference_mode=paused_check · may_enable_inference=false |
| 영향 | 등록과 handoff의 n8n POST 합계5회, Data/Knowledge POST=0 · inference/GPU=false |
| 한계 | production_indexing_verified=false · question_ready=false · 실제 색인/검색 성공 아님 |

아래는 재현/새 설치용 절차다. 이미 통과한 이 서버에서 prepare/dispatch를 반복하거나 보류 Flow를 resume하지 않는다. 이번 완료 기록 패치는 문서만 변경하므로 QA API 재실행이 필요하지 않다.

## 1. 코드 패치 적용과 로컬 검사

Q05a까지 초기화한 프로젝트 루트에서 원래 shshin 사용자로 실행한다.

```bash
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_indexing_flow.py' -q
python3 -B -m apps.document_qa.indexing_flow init
python3 -B -m apps.document_qa.indexing_flow status
python3 -B -m apps.document_qa.api status
```

이 구간의 운영 API/추론/GPU 요청은0이다. 첫 상태의 workflow_configured=false, flow_counts={}, question_ready=false는 정상이다. Q05a 후보 plan과 CPU 정제를 다시 실행할 필요가 없다. 이후 아래 수동 QA API 재실행을 한 번 수행해야 새 route와 키를 읽는다.

## 2. 수동 QA API 재실행

기존 QA foreground 터미널만 Ctrl+C로 종료하고 아래 기존 주소로 재실행한다. 다른 업무 서비스나 GPU 작업을 제어하지 않는다. root 환경에서는 runuser로 원래 사용자의 상태를 사용한다.

```bash
runuser -l shshin -c '
  cd /home/shshin/workspace/agent_project || exit 1
  exec /home/shshin/miniforge3/envs/sh_test/bin/python3 -B     -m apps.document_qa.api serve --bind 172.25.0.1 --port 17110
'
```

이 서버 인수 주소는 앞선 n8n↔QA 연결에서 확인한 host 주소다. 다른 배포의 기본값이나 Kubernetes Service 주소가 아니다. listening 출력의 indexing_inference_enabled=false를 확인하고 터미널을 유지한다. 17100은 기존 Ops 포트다.

## 3. 명시 등록과 GPU 없는 callback 인수

별도 터미널에서 shshin 사용자·프로젝트 루트로 아래 명령을 실행한다. root에서는 위와 동일하게 runuser를 사용한다. **기존 진입/CPU prepare를 반복하지 않는다.**

```bash
python3 -B -m apps.document_qa.indexing_connection prepare-paused \
  --qa-origin http://172.25.0.1:17110
python3 -B -m apps.document_qa.indexing_connection accept-paused \
  --qa-origin http://172.25.0.1:17110 \
  --job-id 430b0e7b-7b4c-4ae7-9c57-6c979ca94343
```

먼저 인증된 자료함·새 route·서버 추론 금지를3GET으로 검증한다. 이전 API가 계속 실행 중이면 등록 전에 QA_API_RELOAD_REQUIRED로 중단한다. prepare-paused는 새 n8n credential/Flow 등록·활성 정의/게시 버전 readback만 수행한다. accept-paused는 기존 canonical TXT에 inference_mode=paused_check를 고정해 새 n8n handoff를 전달하고 최대30초 callback 결과를 로컬 상태로 관찰한다. 원본/정제/Knowledge를 재전송하지 않는다.

성공은 result=passed, n8n_callback_execution_verified=true, saved_paused_receipt_verified=true이며 workflow.state=blocked, error_code=GPU_VERIFICATION_DEFERRED가 **기대 결과**다. steps≥1·accepted receipt와 Knowledge binding/task/head가 생성되지 않았음을 확인한다. 이 인수는 n8n→QA callback/권한 경로이며 실제 Model/색인/검색 성공은 아니다. 기존 보류 handoff를 재조회한 경우 existing_handoff_reused=true로 구분한다. 자동 retry/resume은 수행하지 않는다.

문서에 현재 실제 결과가 없는 구간은 통과로 기록하지 않는다. Q05a의 정적 n8n_indexing_connected/configuration_live_verified flag를 임의 변경하지 않고 별도 Flow의 setup 및 개별 callback 인수 기록으로 확인한다.

## 검증·원복·다음 단계

새48개 검사는 기존 키/schema/두Flow 정의 유지·새등록/생성유실·최초 execution·세대/권한/예산·GPU 보류·고정 paused_check·동시 lane·build/activation 응답 유실·resume 비재전송·실패 시 기존 문서 보존을 확인한다. Node로 JWT/receipt/owner/결과/Wait 정의를 실행하고 실제 loopback QA HTTP로 CPU완료→색인 callback과 보류 인수 절차를 확인했다. 전체QA324개 검사도 통과했다. 이 오프라인 검사는 실제 n8n/Model/GPU·장기 운영·Kubernetes 인수를 대신하지 않는다. 후속 사용자 서버 출력으로 실제 n8n 등록·게시 정의·보류 callback은 확인했다. 실제 Model/GPU 색인·장기 운영·Kubernetes 인수는 미실행이다.

패치는 기존 업무 플랫폼 실행 파일·서비스·Compose·설정/secret을 변경하지 않는다. QA API만 수동 재실행이 필요하다. 패키지 --rollback은 코드/문서만 원복하며 생성된 SQLite 기록/전용 키·n8n 자산을 자동 삭제하지 않는다. 원복한 API를 쓰려면 QA 프로세스도 다시 읽혀야 한다. 새 Flow의 비활성화/제거는 별도 명시 작업이다.

Q05b 서버 등록/보류 callback 확인은 완료했다. GPU 없는 후속 개발은 Q06a 검색 Tool 연결 adapter/계약이며, Q05c 실제 GPU 색인 인수 후 운영 Tool/Agent 연결·질문 대상 전환을 별도로 수행한다. GPU 사용 가능 예상일은 자동 실행 예약이 아니다. Agent 연결/Q06·화면·질문/출처·품질 인수는 [전체 작업계획서](../plans/document-qa-work-plan-20261002.md)를 따른다.
