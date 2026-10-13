# Q03b — 문서 준비 workflow 전달

Q03b의 전달 의도·receipt·중복/응답 유실 처리와 새 검사 36개를 구현했다. [오프라인 근거](../testing/document-qa-q03b-offline-20261007.json)와 [Q03a 서버 적용](../testing/document-qa-q03a-server-observed-20261007.json)을 보존한다.

현재 TXT CPU 인수 범위와 실패/후속 성공은 [상태표](../status.md)에 기록한다. 이 안내의 명령은 필요할 때의 인터페이스이며 완료 검사를 다시 실행할 지시가 아니다.

이번 결과는 확정된 Data 원본 ID를 n8n 진입 Flow에 전달하고 같은 QA 작업에 접수 기록을
연결하는 것이다. [Q04a](refinement.md)에서 operator CPU 정제 adapter를 구현했으며,
[Q04b 자동 정제/실행권](refinement-workflow.md)을 별도 owned Flow로 구현했고 새 32개 검사가 통과했다. 원래 5-node 진입 Flow/키/receipt는 유지한다. 기존 TXT job의 실제 n8n/Data CPU 연결은 통과했고 다음은 Q05a 색인 adapter다. 실제 색인·Agent·사용자 화면은 후속이다.
`workflow_started=true`는 검증된 진입 receipt가 자기 DB에 저장됐다는 뜻이며
`question_ready=false`, `may_execute_next_stage=false`를 유지한다.

## 설치와 자기 저장

프로젝트 루트에서 Q03b 검사와 명시 초기화만 실행한다.

```bash
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_workflows.py' -q
python3 -B -m apps.document_qa.workflows init
python3 -B -m apps.document_qa.workflows status
```

Q03a의 기존 자료함/DB가 필요하다. `init`은 동일 uploads.sqlite3에 workflow 전용
metadata/setup/handoff/실행 참조 테이블을 한 트랜잭션으로 추가한다. 업로드 user_version=1과
기존 테이블을 유지해 이전 업로드 코드도 저장을 읽을 수 있다. 자료함 ID·job·사용자 key·
원본·기존 API bearer는 그대로 둔다. 재실행은 같은 저장/키를 사용한다.
새 `workflow-api-token`, `workflow-hook-secret`은 자기 저장의 0600 파일이다.

기본 저장은 `$HOME/.local/state/agent-project-document-qa/uploads`다.
패치/init/status/검사는 운영 API·GPU·모델을 호출하거나 서버/서비스를 기동하지 않는다.
초기 `workflow_configured=false`, `handoff_counts={}`는 연결 준비 전의 정상 상태다.
status의 `configuration_live_verified=false`는 로컬 저장 조회이며 현재 엔진을 조회하지 않는다.
Node가 없는 호스트에서는 JavaScript 실행 검사 3개가 건너뛰어지므로 전체 36개 통과와 구분한다.

## 명시 연결 준비 — 실제 연결 인수 때만

기존 n8n 2.40.5와 API key JSON `url`/`api_key` 참조를 읽는다. 기본 파일은
`infra/n8n/.secrets/agent-integration-api-key.json`이며 기존 키를 생성/회전/출력하지 않는다.
현재 infra는 Code node의 crypto builtin을 허용한다. 다른 엔진/설정으로 옮기면 별도 검증한다.

QA API는 기본 127.0.0.1:17110이다. n8n 컨테이너가 같은 호스트의 loopback에 접근한다고
가정하지 않는다. 연결 인수 전에 **n8n에서 도달 가능한 QA origin**을 운영자가 확인해야 한다.
`serve --bind`는 literal IPv4 loopback 또는 RFC1918 주소만 받으며 wildcard/public bind를
거부한다. HTTP callback origin도 loopback/localhost/RFC1918만 허용하며 그 밖에는 HTTPS가
필요하다. 기존 LAN/TLS/proxy/Compose를 자동 변경하거나 Docker bridge 주소를 추측하지 않는다.

예를 들어 운영자가 검증한 private host 주소와 포트를 환경변수에 설정한 뒤 별도 터미널에서
API를 띄우고 등록한다. 다음은 실제 연결 검증 절차이며 패치 적용 명령에 포함하지 않는다.

```bash
# DOC_QA_BIND: 검증한 private host IPv4, DOC_QA_ORIGIN: n8n에서 도달 가능한 origin
python3 -B -m apps.document_qa.api serve --bind "$DOC_QA_BIND" --port 17110
# 위 API가 실행 중인 상태에서 다른 터미널
python3 -B -m apps.document_qa.workflows prepare --qa-origin "$DOC_QA_ORIGIN"
```

현재 서버 settings/Flow 목록 GET은 모두 200이지만 settings.data에는 versionCli가 없었다.
[버전 확인 코드](../../apps/document_qa/engine_identity.py)는 이 필드가 있을 때 기존 2.40.5 검사를 유지한다.
필드가 없는 경우에만 local Docker 배포 adapter가 API port와 연결된 infrastructure/n8n owned
실행 컨테이너·image ID·고정 repo digest를 조회한다. tag만으로 확인하지 않고 다른 이미지/포트·
remote Docker·불명확한 소유권을 거부한다. 값이 있는데 다르면 fallback하지 않는다.
이 확인은 prepare의 원격 생성 전과 게시 readback/후속 전달 전마다 수행한다.
Docker exec·이미지 build/pull·서비스 제어·환경 변수/credential export는 없다.
배포 adapter는 업무 job/receipt/Flow 정의에 Docker ID를 저장하지 않는다. Kubernetes 이식 때
해당 배포의 identity reader로 바꿀 경계이며 현재 Kubernetes 버전 인수는 하지 않았다.
조회 방식은 [Docker inspect](https://docs.docker.com/reference/cli/docker/inspect/)와
[image inspect](https://docs.docker.com/reference/cli/docker/image/inspect/)의 공식 metadata API를 따른다.

[후속 서버 실행](../testing/document-qa-n8n-http-server-observed-20261007.json)에서 버전 패치/15개 검사는 통과했으나 첫 GET이 HTTP 오류로 중단됐다. 이미지 metadata/QA 실행/prepare는 도달하지 않았다. 공통 EngineHTTP는 본문이 있을 때만 Content-Type: application/json을 붙인다. 본문 없는 GET은 앞서 성공한 독립 조회와 같은 Accept 헤더를 사용한다. POST canonical JSON·API key·webhook 서명·no proxy/redirect/retry·60초/80회/2MiB 제한은 유지한다. 요청별 metadata는 최대 80개이며 route의 query/cursor·인증 헤더·응답 본문을 기록하지 않는다. check_engine.py는 HTTP 상태를 함께 출력한다. 정확한 기존 HTTP 상태와 헤더 차이의 실제 영향은 서버 출력으로 확인해야 한다.

prepare는 기존 API key를 이용해 **새 QA scoped credential 2개와 새 고정 진입 Flow 1개**를
등록/게시한다. 기존 Agent 질문 Flow·플랫폼 키·도메인 설정을 수정하지 않는다. engine version,
정의/credential 참조와 게시 version의 API readback이 일치해야 configured로 저장한다.
등록 intent와 임의 owned 이름을 POST 전에 저장한다. 생성 응답 유실 후 같은 prepare는
유일한 owned 이름으로 조정하며 없거나 여러 개면 자동 선택/삭제/재등록을 하지 않는다.
prepare 재실행은 같은 리소스를 사용한다. 실제 n8n API 지원·게시/도달성은 이 단계에서
확인할 미완료 인수 항목이다. 전체 엔진 smoke나 GPU 검사를 반복할 필요는 없다.

## 전달과 접수 기록

고정 Flow는 JWT webhook → 요청 검증 → QA receipt commit → receipt 검증 → JSON 응답이다.
사용자/원격 payload가 callback URL·모델·Data/Knowledge 실행 경로를 지정할 수 없다.
전달 본문은 schema, handoff_id, job_id, library_id, sequence, source_document_id뿐이다.
원문·플랫폼 비밀을 복제하지 않는다. webhook JWT는 120초 이내이며 path/handoff/body SHA에
묶는다. 장기 secret은 n8n credential에만 등록하고 Flow JSON에는 credential ID를 둔다.

확정된 Data receipt와 preparation_pending job이 있어야 전달한다. 최초 전달 전에 stable
handoff/body SHA/고정 setup을 저장하고 POST 직전 시도 횟수와 확인 필요 상태를 commit한다.
엔진 version/정의가 변경됐으면 실행하지 않는다. 원격 I/O 동안 DB 트랜잭션은 잡지 않는다.

callback은 별도 scoped bearer로 인증하며 일반 업로드/자료함 API에 접근할 수 없다.
기존 intent·원본/library/sequence와 pinned Flow/정의가 일치해야 receipt를 commit한다.
동시/중복 trigger는 동일 first receipt를 반환하고 bounded execution ID 참조만 추가한다.
이미 commit된 receipt 뒤 webhook 응답이 끊겨도 다음 조회/dispatch는 재전송하지 않는다.
단순 started 응답이나 성공 HTTP 코드만으로 접수 확정을 만들지 않는다.

미확인 전송은 자동 반복하지 않는다. 운영자가 원래 job으로 조회 후 필요한 경우에만 같은
intent를 명시 재전송한다. 프로세스 종료 뒤에도 DB/OS lock으로 재개한다. 별도 n8n execution이
생길 수 있으므로 전체 Flow의 exactly-once를 보장하지 않는다. **Q04 도메인 단계의 직렬화/
실행권/중복 접수는 후속 구현**이며 이 진입 receipt가 정제 실행 권한을 주지는 않는다.

| API | 인증과 역할 |
|---|---|
| GET /api/workflows | operator bearer; 자기 설정/집계 조회 |
| GET /api/jobs/{job_id}/workflow | operator bearer; intent 상태/receipt/실행 참조 조회 |
| POST /api/jobs/{job_id}/dispatch-workflow | operator bearer + JSON {}; 최초 한 번 전달, 확정된 receipt 재사용 |
| POST /api/jobs/{job_id}/retry-workflow | operator bearer + JSON {}; 미확인 동일 intent를 명시 한 번 재전송 |
| POST /api/workflow/receipts | workflow scoped bearer; 고정 callback만 허용 |

prepare 완료 후 업로드 Data receipt가 확인되면 최초 handoff를 한 번 요청한다. n8n 연결이
없거나 전달 실패여도 이미 접수된 Data 원본/job을 버리지 않는다. GET job/자료함·API status는
durable receipt의 workflow_started를 표시한다. UploadStore 자체 projection은 기존 업로드
단계 표현이며 API가 workflow 결과를 덧붙인다. 검색/질문 가능으로 확대하지 않는다.

operator 요청은 최대 4개, workflow callback 연결을 포함한 전체 HTTP 연결은 최대 8개다.
동시 operator 전달이 callback용 연결을 모두 점유하지 않도록 한다. n8n JSON은 2MiB,
callback은 8KiB, 원격 호출 timeout 8초/공유 budget 60초/최대 80회다. DNS까지 강제 중단하는
실행기나 대규모 비동기 worker는 아니다. job당 명시 시도와 실행 참조는 각각 16개를 넘으면
별도 검토가 필요하다. receipt/metadata를 보관하며 원문 저장을 추가하지 않는다.

## 오류·원복·이식 경계

WORKFLOW_NOT_INITIALIZED/NOT_CONFIGURED는 자기 초기화/명시 연결이 필요하다는 뜻이다.
ENGINE_VERSION_UNVERIFIED/WORKFLOW_CHANGED/SETUP_CHANGED는 pinned 연결을 재검토한다.
ENGINE_CREATE_UNCONFIRMED는 원래 등록 intent를 보존한다. HANDOFF_OUTCOME_UNKNOWN/
RECEIPT_UNCONFIRMED는 같은 job의 자기 receipt를 먼저 조회하며 새 job/key를 만들지 않는다.
HANDOFF_REVIEW_REQUIRED는 시도/참조 한도다. 오류 원격 본문이나 credential 값은 출력하지 않는다.

패치 rollback은 프로젝트 파일만 원복한다. 자기 DB·키·receipt와 n8n에 명시 생성한 리소스는
보존한다. 기존 Q03a 코드는 확장 후 DB의 원래 업로드 테이블을 읽을 수 있다. 등록한 Flow의
unpublish/delete나 자기 저장 삭제는 rollback이 자동 수행하지 않는다. 운영 연결을 등록한 뒤
원복할 때는 해당 새 Flow의 사용 상태를 별도로 확인해야 한다.

현재 SQLite/file lock·단일 Linux 호스트 adapter를 유지한다. Kubernetes에서 핵심 API/
workflow 소유권은 유지할 수 있지만 단일 replica/PV 또는 분산 저장/실행권 전환과 네트워크/
credential 공급은 별도 인수다. Docker ID·systemd·Ops recovery 코드 의존은 추가하지 않았다.

코드: [workflows.py](../../apps/document_qa/workflows.py),
[고정 Flow](../../apps/document_qa/workflow_template.py),
[API 계약/source pins](../../apps/document_qa/workflow-contract.json).
전체 순서는 [작업계획서](../plans/document-qa-work-plan-20261002.md)를 따른다.
외부 참고는 [n8n Webhook](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/),
[Respond to Webhook](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.respondtowebhook/),
[공식 API](https://docs.n8n.io/api/)다. 2026-10-07 문서 참조이며 로컬 엔진 인수의 대체 근거가 아니다.
