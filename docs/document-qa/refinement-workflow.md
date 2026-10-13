# 문서 QA Q04b — n8n CPU 정제 workflow

[Q04a 서버 41개 검사/초기화](../testing/document-qa-q04a-server-observed-20261007.json), [Q04b 서버 32개 검사/초기화](../testing/document-qa-q04b-server-observed-20261007.json)와 [오프라인 근거](../testing/document-qa-q04b-offline-20261007.json)를 보존한다.

현재 TXT CPU 인수 범위와 실패/후속 성공은 [상태표](../status.md)에 기록한다. 이 안내의 명령은 필요할 때의 인터페이스이며 완료 검사를 다시 실행할 지시가 아니다.

실제 TXT n8n callback·Data 정제·canonical 본문 인수는 통과했다. 질문 가능·검색/답변 의미 품질 인수는 후속이다. GPU/OCR/embedding/chat은 보류한다. 이미 통과한 전체 검사를 다시 실행하지 않는다.

## 역할과 기존 상태 보존

Q03b의 고정 5-node 진입 Flow·setup·handoff·receipt·callback 키는 유지한다. 해당 receipt의
may_execute_next_stage=false도 그대로다. Q04b는 별도 owned 9-node CPU Flow와 private credential을
등록한다. 업로드 API가 Data 원본을 확인하고 Q03b receipt를 받은 뒤 Q04b handoff를 전달한다.
Q03b/정제 Flow의 등록은 각각 명시 prepare로 수행한다. 적용/init/status로 원격 Flow를 등록하지 않는다.

[코드](../../apps/document_qa/refinement_flow.py)는 원본/자료함/job/sequence를 자기 저장에서
고정하고 정제는 [Q04a adapter](refinement.md)에 위임한다. Data/Knowledge/Agent DB·저장소에
직접 접근하지 않는다. 본문과 플랫폼 credential을 Flow JSON에 담지 않는다. n8n에는 참조와
QA 단계 API credential, 현재 실행에만 유효한 grant가 전달된다. 새 모델 선택/추론/색인은 없다.

## 처리 순서와 실행권

1. QA가 승인된 Q03b handoff에 대해 별도 Flow intent를 저장한 뒤 signed webhook을 전달한다.
2. n8n은 JWT의 body SHA/audience/120초 만료와 job/generation을 검증한다.
3. 전용 receipt callback이 첫 execution ID와 grant/600초 기한을 원자 저장한다.
4. Respond to Webhook이 이 접수 결과를 먼저 반환한다. owner gate는 같은 receipt를 받은 중복 execution을 종료한다.
5. 최초 execution이 전용 step API를 호출한다. 서버는 credential 외에 job/handoff/generation/owner/digest/grant를 검사한다.
6. 미요청 정제는 Q04a intent로 접수하고, known receipt는 같은 run을 한 번 조회한다. 계속 처리 중이면 5초 Wait 뒤 다음 step이다.
7. canonical 검증 완료 또는 attention/기한·횟수 초과에서 Flow를 종료한다. 질문 연결은 Q05/Q06 후속이다.

정제 실행 전에는 같은 library의 durable lane을 확인한다. 다른 문서 처리 중이면 새 Data POST 없이
제한된 대기를 한다. webhook 응답을 받는 중 첫 step이 먼저 도착하는 경합도 부작용 없이 대기한다.
Data 응답 유실/미확인 접수는 자동 POST 재전송하지 않고 원래 intent/key를 유지한다.
partial/OCR/needs_review/빈 본문·해시/ID 불일치는 검색 준비 성공이 아니다.

| 제한 | 동작 |
|---|---|
| Flow step | 최대 40회 Data 동작 시도. 초과 확인은 새 Data 요청 없이 종료 |
| 실행권 기한 | 첫 receipt부터 600초. 시작 전 검사하며 개별 Data 호출은 최대 60초 budget |
| n8n executionTimeout | 600초. 실제 설치 버전의 timeout/Wait 동작 인수는 남음 |
| 대기 | 5초 interval. 무한 반복·백그라운드 worker 없음 |
| webhook 전달/중복 execution | generation당 각각 최대 16개. 자동 무조건 재전송 없음 |
| 명시 resume | job당 최대 16 generation. 기존 Data run/intent를 그대로 조회 |

[Respond to Webhook 공식 설명](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.respondtowebhook/)은
응답 이후 입력을 다음 출력으로 넘기는 동작을 설명한다. [Wait 공식 설명](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/)에 따르면
65초 미만 대기는 실행을 메모리에 유지한다. 따라서 5초 Wait 자체가 n8n 재시작 후 자동 복구를 보장하지 않는다.
자기 intent/receipt/정제 ID가 복구 기준이다. 엔진 중단으로 실행권이 만료되면 명시 resume으로 새 owner를
받고 기존 run을 조회한다. n8n timeout/재시작 인수는 실제 연결 확인과 Q09에서 수행한다.

## 적용과 로컬 상태

```bash
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_refinement_flow.py' -q
python3 -B -m apps.document_qa.refinement_flow init
python3 -B -m apps.document_qa.refinement_flow status
python3 -B -m apps.document_qa.api status
```

init은 자기 uploads.sqlite3에 별도 metadata/setup/run/current/execution 테이블 5개를 추가하고
refinement-workflow-api-token/refinement-workflow-hook-secret을 자기 private state에 생성한다.
기존 operator/Q03b 키와 upload user_version=1·workflow/refinement schema는 유지한다.
원격 API/서비스/GPU를 실행하지 않는다. 이미 초기화된 저장의 반복 init은 키를 유지한다.
초기 workflow_configured=false, flow_counts={}, question_ready=false는 정상이다.

Q04a refinement status의 n8n_refinement_connected=false는 실제 엔진 연결을 확인한 값이 아니다.
Q04b의 /api/refinement-workflows 또는 aggregate API status.refinement_workflow에서 별도 설정을 확인한다.
workflow_configured는 저장된 등록 상태이고 configuration_live_verified는 로컬 status에서 false다.
실제 연결은 개별 handoff/owner/정제 결과와 엔진 근거를 함께 확인해야 한다.

## QA API와 Flow 등록 — 완료한 연결의 참조 절차

QA API는 shshin 계정으로 http://172.25.0.1:17110에 foreground 실행 중이며,
operator 인증·동일 자료함·Q04b stage route 조회가 통과했다. 기존 Ops 17100과 분리한다.
n8n의 HTTP 503은 flows PostgreSQL 저장 디스크의 ENOSPC와 함께 관찰됐고,
사용하지 않는 Docker build cache 정리 뒤 디스크 가용 31G·DB 접속 허용·n8n readiness 200을 확인했다.
후속 settings/Flow 목록 GET은 모두 200, owned pinned image로 버전 2.40.5 확인이 통과했다.
앞선 HTTP/버전 실패 기록은 역사 근거로 보존하며 현재 재개 단계로 반복하지 않는다.

앞선 등록에서 진입 Flow는 active였지만 CPU Flow는 Wait webhookId 비교 때문에 inactive로 중단됐다.
후속 비교 수정 적용·9개 검사 뒤 기존 CPU Flow prepare와 두 Flow 활성 정의/게시 버전 GET 확인이 통과했다.
이 등록 단계의 실패를 삭제하지 않으며 현재 진행 단계로 반복하지 않는다.
이번 수정은 `wait` v1.1·`resume=timeInterval`·기대 정의에 webhookId가 없는 경우에만,
서버 응답의 canonical·nonzero UUID를 비교에서 제외한다. 대기 시간·단위·resume 방식,
노드 종류/버전·추가 필드·주소·credential ID·코드·연결·settings는 계속 비교한다.
진입 Webhook의 명시 webhookId와 webhook/form 재개 방식에는 적용하지 않는다.

템플릿·요청 SHA·정의 digest·DB schema는 유지한다. 저장된 CPU 등록 intent와 credential/Flow ID로
prepare를 재개하며, 정의와 현재 버전 확인이 통과하면 기존 Flow의 publish만 요청한다.
새 회귀 검사 9개와 영향받는 기존 등록 검사 4개가 통과했다. 후속 사용자 서버에서도 수정 적용·9개 검사와 등록/활성 정의·게시 버전 확인이 통과했다.

```bash
# 기존 QA API를 유지한 별도 터미널, 프로젝트 루트에서 수행한다.
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_wait_webhook_metadata.py' -q
python3 -B -m apps.document_qa.refinement_flow prepare --qa-origin http://172.25.0.1:17110
# 패키지 verify_registration.py는 두 Flow의 활성 정의/버전만 GET으로 확인한다.
```

등록 확인 이후 사용자가 QA foreground 프로세스를 같은 shshin 계정/환경/bind/port로 수동 재실행했다.
아래 재실행 설명은 해당 코드 수정 시 필요한 배포 경계이며 문서 반영 때문에 반복할 절차가 아니다. 실행 중인 Python은 비교 함수의
이전 버전을 이미 import했으므로 파일 적용만으로 바뀌지 않는다. 이 재실행은 수동 QA
프로세스에 한정하며, 패치와 등록 명령은 서비스 제어를 수행하지 않는다.
기존 TXT job의 실제 두 callback→Data 정제→canonical 본문 검증은 통과했다. Markdown/PDF·신규 업로드 전체 경로는 후속이다.
질문 가능은 색인/Agent 연결까지 검증한 뒤 전환한다.

## 명시 운영 연결과 조회

기존 QA serve의 private bind/인증을 사용한다. QA 주소는 n8n 컨테이너가 도달하는 주소여야 한다.
새 port 공개·systemd·Compose 변경/재시작은 이번 패치가 수행하지 않는다.
이미 수동 serve 중이면 새 코드로 QA 프로세스를 실행할 때 새 route가 반영된다.

```bash
# qa_origin은 실제로 확인한 QA private 주소다. 같은 주소/엔진의 Q03b Flow를 먼저 등록한다.
python3 -B -m apps.document_qa.workflows prepare --qa-origin "$qa_origin"
python3 -B -m apps.document_qa.refinement_flow prepare --qa-origin "$qa_origin"

# 이후 업로드 API는 Data 원본 확인 -> 기존 진입 receipt -> CPU Flow 전달을 수행한다.
# 이미 업로드되고 Q03b receipt가 있는 job은 아래 명시 dispatch로 연결한다.
python3 -B -m apps.document_qa.refinement_flow dispatch --job-id "$qa_job_id"
python3 -B -m apps.document_qa.refinement_flow status
```

prepare는 [버전 확인 정책](workflows.md)에 따라 현재 n8n 2.40.5와 원래 Q03b owned active version을 검증한다.
전용 credential/새 CPU Flow의 create intent를 저장하고 readback/publish를 검증한다. 응답 유실 시
고유 owned name으로 확인하며 새 create를 무조건 재전송하지 않는다. 기존 Flow를 수정/삭제하지 않는다.
위 운영 연결은 기본 적용 명령에 포함하지 않으며 이 세션에서 실행하지 않았다.

| 경로 | 인증·역할 |
|---|---|
| POST /api/workflow/refinement/receipts | Q04b 전용 credential; 저장된 trigger intent에 첫 owner grant 반환 |
| POST /api/workflow/refinement/step | 전용 credential + 현재 owner grant; 해당 job CPU 접수/한 번 조회 |
| GET /api/refinement-workflows | operator; 로컬 설정/한도/전달 수 |
| GET /api/jobs/{job_id}/refinement-workflow | operator; grant를 숨긴 실행 참조/진행 상태 |
| POST /api/jobs/{job_id}/dispatch-refinement-workflow | operator + {}; 첫 전달 |
| POST /api/jobs/{job_id}/retry-refinement-workflow | operator + {}; 불확실 webhook을 원래 body로 재전달 |
| POST /api/jobs/{job_id}/resume-refinement-workflow | operator + {}; attention/만료 Flow만 새 generation으로 재개 |

기존 operator/Q03b callback credential은 새 stage route에 사용할 수 없다. 새 credential도
업로드·operator 정제 API·Q03b receipt에 사용할 수 없다. grant만으로 인증할 수 없다.
public job/status에는 grant 값·credential·원문·canonical 본문을 노출하지 않는다.

resume은 살아 있는 owner/완료 Flow를 덮어쓰지 않는다. 이전 grant는 새 generation에서 거부한다.
Data 접수 자체가 confirmation_required면 resume해도 새 POST를 하지 않는다. 이 경우 Q04a의
명시 retry/원래 key를 통한 확인이 필요하다. terminal partial/실패가 정상 결과로 바뀌지 않는다.

## 원복과 배포 경계

패치 rollback은 프로젝트 파일만 되돌린다. 자기 DB·키·이전 Flow 자산·Data run/canonical은 유지한다.
Q04a API는 기존 upload/workflow/refinement 테이블을 읽을 수 있고 재적용하면 새 Flow 기록을 읽는다.
로컬 파일 rollback이 원격 n8n 자산의 삭제·실행 중단을 뜻하지 않는다.

현재 자기 SQLite와 Linux file lock은 단일 호스트/볼륨의 구현이다. Docker ID/systemd/Ops에 결합하지
않았지만 Kubernetes 다중 replica의 분산 실행권/볼륨·서비스 주소 인수는 후속이다.
운영 native engine/CPU parser 연결은 기존 TXT job 범위에서 통과했다. 다른 형식·timeout/재시작과 질문 가능 인수로 확대해 표시하지 않는다.
