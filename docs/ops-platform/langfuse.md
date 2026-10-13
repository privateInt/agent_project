# Agent 추적 — Langfuse 배치와 연동 결정

**2026-09-28 관측 백업·복원 계획:** [대상·절차·O04 남은 범위](../history/ops-evidence-catalog.md#backup-restore-plan-20260928)를 정리했다. Prometheus·Langfuse 네 저장소·Alertmanager·Ops 설정/키/캐시를 대상으로 하며 업무 DB/모델/문서 전체 백업은 별도다. 다음은 실제 mount·크기·권한·복원 자원·목적지 후보의 읽기 전용 사전 조사 도구다. 초기 정상 종료/복사안은 관측 최대 9개 컨테이너의 중단을 수반하므로 실측과 실행 범위 확정이 먼저다. 백업/복원 도구·실제 복사·서비스 중지·정기화는 아직 미실행이다. 이번 문서 적용에는 빌드·재시작·추론이 필요 없다. 필요한 자료·디스크 경보 두 건·원래 Agent 실패와 품질 보류·외부 발송 미설정은 유지한다.

**2026-09-28 Ops LAN·사용자 화면 확인 PASS:** [서버 결과와 화면 근거](../history/ops-platform-202609-20261001.md#browser-verified-20260928). 09:17 KST 주소 변경 도구가 passed였고 같은 Ops 컨테이너/이미지/마운트, 접속 코드·캐시 19개/시각과 보호 대상 61개를 유지했다. 09:19~09:25 KST 사용자 화면 9장으로 로그인 후 네 플랫폼 현황, 자원 7개 그룹, 규칙 8개와 디스크 경보 2건 발생/수신, 기존 Agent 실패·5단계·추적 10개 표시를 확인했다. 이는 해당 핵심 조회 흐름의 인수이며 모든 목록/페이지·오류 화면·Agent 전용 콘솔의 인수는 아니다. 다음은 O04 남은 범위와 관측 데이터 백업/복원 범위 정리다. 필요한 자료·원래 인용 누락 실패·답변 품질 보류, 외부 발송 미설정을 유지한다. 이번 패치는 문서/작은 요약뿐이며 완료한 주소 변경·빌드·재시작·API 검사·추론은 반복하지 않는다. 아래 날짜별 이력의 대기/보류는 당시 상태이며 현재 Ops 화면 상태는 이 기록을 따른다.

**2026-09-28 운영 Alertmanager 재시작 서버 인수 기록:** [사용자 서버 근거](../history/ops-platform-202609-20261001.md#alertmanager-restart-verified-20260928)에서 `passed`를 확인했다. 표시된 검사 시각은 9월 27일 15:12 KST이며 같은 컨테이너/이미지/마운트·설정/저장 연결, 빈 snapshot 2개·silence 0건, 규칙 8개와 firing 2건 수신, 보호 대상 컨테이너 61개 유지를 확인했다. 출력 끝이 잘려 이번 호출의 `evidence_reused` 값은 미확인이다. 이를 오늘 새 재시작·새 API 검사로 기록하지 않는다. nonempty 상태/nflog 논리 보존·외부 발송·백업 복원은 별도다. 이번 변경은 문서/작은 요약뿐이며 완료한 `run/check`, 빌드·재시작·추론을 반복하지 않는다. 필요한 자료와 원래 `MISSING_REFERENCE` 실패·Agent 품질 보류를 유지한다. 당시 브라우저 보류는 상단 후속 결정에서 해제했다.

**2026-09-27 디스크 감시 보완 서버 PASS:** [14:28 KST 서버 결과](../history/ops-platform-202609-20261001.md#filesystem-verified-20260927)로 `/mnt/nvme1`을 포함한 디스크 4개와 native 규칙 8개 시나리오·24개 시점 검사, Prometheus 같은 이미지/TSDB·과거 시계열 164개/표본 984개 보존을 확인했다. 엔진 갱신 검사 시점 보호 대상 57개와 Alertmanager를 유지했고, 이후 Ops만 `o04.2`로 갱신해 Healthy와 API 조회 PASS를 확인했다. 당시 `/mnt/nvme1` 91.86%, SSD_EXT4 96.85%의 두 경보는 재평가 시작 후 pending이었다. 이후 firing/해소는 이번 출력에 없다. 필요한 자료는 사용자 결정에 따라 보존하며 정리·임계치 변경을 하지 않는다. 당시 다음 작업인 운영 Alertmanager 인수의 현재 절차는 상단 최신 안내를 따른다. 원래 Agent `finalize/ANSWER_REFERENCE_INVALID` (`MISSING_REFERENCE`) 실패, 사용자 브라우저/답변 품질 보류, 외부 발송 미설정을 유지한다. 당시 디스크 완료 기록 패치는 문서/작은 결과 요약뿐이므로 완료한 적용·빌드·검사/API 조회를 반복하거나 원본 기준·키를 초기화하지 않는다.

**2026-09-27 O02 후속 서버 확인:** [4개 플랫폼 공식 API 표본 조회](../history/ops-platform-202609-20261001.md#platform-api-verified-20260927)가 사용자 서버에서 모두 통과했다. 기존 AgentRun/단계/ToolCall을 다시 읽었으며 새 추론·추적 수집 시험은 요청하지 않았다. 원래 인용 누락 업무 실패를 유지한다. Langfuse API 통합 조회 서버/화면은 후속 O03에서 구현/로컬 검증됐으며 기존 추적 시험을 반복하지 않는다.

**2026-09-27 O02 Agent 추적 서버 확인:** 수정된 DB 통합 검사와 `run/replay/check`가 모두 PASS다. 같은 AgentRun/request/trace ID에 연결된 10개 span과 누락 없음, 본문 제외·원래 상태/오류 일치·동일 요청 재사용을 확인했다. **추적 검사는 통과했지만 업무 실행은 실패**했다. 원본 AgentVersion `6f0fb4b9-2796-4e33-b040-cc6149bd0988`은 generate까지 성공한 뒤 `finalize/ANSWER_REFERENCE_INVALID`로 종료됐고, 읽기 전용 진단에서 `MISSING_REFERENCE`, 문맥 5개, 인식 인용 0개, stop·176/512토큰을 확인했다. 별도 버전의 과거 답변 전달 성공과 구분한다. 문서 때문에 재검사·재시작·새 추론을 반복하지 않는다. 당시 후속이던 플랫폼별 API 조회도 서버 표본 인수를 마쳤으며 최신 기록을 따른다. O03 운영 API/화면의 후속 상태는 상단 최신 기록을 따른다. [근거·완료 범위](../history/ops-platform-202609-20261001.md#agent-tracing-verified-20260927).

**2026-09-26 Langfuse 재시작 보존 서버 인수 PASS:** 읽기 전용 diagnose/compare로 원래 6개 컨테이너의 재시작·Healthy, 같은 ID/이미지/설정/저장 연결과 고정 trace 3개 span 보존을 확인했다. 기준 hash도 유지됐고 이번 조회의 추가 재시작·POST·추론은 없었다. 현재 차이 항목과 순서 동등성 적용 목록은 모두 비어 있어 최초 비교 오류의 원인은 미확정으로 남긴다. [서버 근거·완료 범위](../history/ops-platform-202609-20261001.md#langfuse-restart-verified-20260926). 당시 다음 작업인 O02 Agent 계측의 후속 결과는 상단 최신 기록을 따른다. 완료한 인프라 검사는 반복하지 않는다.

**재시작 검증 구현 이력:** 원래 trace/설정/컨테이너 기준을 보존하는 run/compare와 읽기 전용 diagnose를 구현했고 후속 보완까지 로컬 56개 검사를 통과했다. 위 서버 compare PASS로 재시작 보존 인수도 완료했다. 최초 비교 오류와 보완 과정은 [진단 기록](../history/ops-evidence-catalog.md#langfuse-restart-compare-20260926)에 유지한다.

결정일: 2026-09-26. 사용자가 Agent 추적 저장소로 **Langfuse를 선택**하고 `infra` 기동·`ops_platform` 관리 구조를 제안했다. 이 결정을 기존 플랫폼 경계에 반영한다. **후속 O01 infra 코드를 구현하고 로컬 검사를 마쳤다. 실제 서버 기동·합성 추적 수집/재조회·고정 trace 재시작 보존까지 통과했으며 Agent 계측은 위 O02 DB 재검사·10개 span 수집/재사용/재조회까지 서버에서 확인했다. 업무 성공·O02 전체 인수는 별도다. ops 조회 연동은 O03 서버 기동과 원본에 연결된 10개 span 조회까지 통과했다. 사용자 Ops 화면에서도 2026-09-28 고정 실행의 10개 span과 부모·상태·소요 시간·토큰 표시를 확인했다.** [실행·인수 안내](../../infra/langfuse/README.md). 기존 MLflow는 모델 학습/실험 기록 용도로 유지한다.

**2026-09-26 Langfuse 기동·합성 추적 서버 인수 PASS:** Web/Worker의 loopback 연결 거절·컨테이너 주소 HTTP 200으로 바인딩 불일치를 확인하고 두 앱만 수정했다. Langfuse 4.46.0 서비스 6개 Healthy, 저장소 4개·설정·키 보존을 확인했다. 합성 OTLP trace 1개/3개 span의 부모·자식·상태·시간 검사와 같은 ID/hash의 읽기 전용 재조회가 모두 passed다. 실제 모델 추론·업무 서비스 재시작은 없었다. 고정 추적 재시작 보존은 위 후속 서버 인수까지 완료했다. **당시 후속이던 O02 계측 수집/재사용 결과는 상단 최신 기록을 따른다. 당시 미구현이던 ops API/화면의 후속 상태는 상단 O03 기록을 따른다**. [서버 기록](../history/ops-platform-202609-20261001.md#langfuse-verified-20260926).

## 1. 책임 분담

| 위치 | 맡을 책임 | 제공할 경계 |
|---|---|---|
| `infra/langfuse/` — 기동·합성 수집·재시작 보존 서버 PASS | Langfuse Web/Worker와 의존 저장소의 Compose, 선택 기동/중지/점검, 버전·계정·키·볼륨·보관·복구 | Langfuse 인증 API와 UI 주소 |
| `agent_platform/` | 실행 단계·Tool·모델 호출을 계측해 Langfuse로 전송, 원래 실행 ID와 연결 | AgentRun·ToolCall·버전·결과는 Agent가 계속 소유 |
| `ops_platform/` — 구현 예정 | Langfuse API 연동, 실행별 추적 조회·요약·원본 화면 링크, 연결 상태·수집 지연/누락 표시, 운영 설정 | 인증된 운영 조회 API/화면. 조회 호출의 키는 서버에 보관 |
| `infra/mlflow/` + Model | 기존 학습/실험 기록과 연동 | Agent 추적은 Langfuse, 학습 기록은 MLflow로 역할 분리 |

운영 플랫폼에 들어갈 “관리 코드”는 Langfuse를 **사용하는 API 연동·조회·운영 설정 코드**다. 컨테이너 생성/중지·DB migration·volume 관리 코드는 infra에 둔다. ops에 Docker socket을 연결하거나 Langfuse의 ClickHouse/PostgreSQL을 직접 조회하는 구조로 만들지 않는다. 프로젝트/계정/보관 정책 변경이 필요하면 선택 버전·에디션에서 지원하는 공식 API와 인증·감사 범위를 확인한 뒤 추가한다. 모든 관리 API가 OSS에서 제공된다고 가정하지 않는다.

기록 경로는 **Agent → Langfuse**, 조회 경로는 **브라우저 → ops 서버 → Langfuse API**다. ops는 Agent 업무 호출이나 추적 전송의 필수 중계 서버가 아니다. 초기에 Langfuse 상세 화면은 별도 로그인하는 새 탭으로 연결하고 iframe/SSO·관리 화면 전체 재구현은 후속으로 둔다.

## 2. 인프라 배치

공식 자체 호스팅 구성에는 Web·Worker와 PostgreSQL·ClickHouse·Redis/Valkey·S3 호환 저장소가 필요하다. 전용 S3 서비스까지 컨테이너로 둘 경우 기본 계획은 6개 상시 서비스다. 일회성 초기화 작업은 별도다. 공식 Compose는 단일 서버 설치 방법이며 고가용성·백업을 자동으로 제공하는 구성이 아니다.

`infra/langfuse/`를 별도 Compose 프로젝트의 선택 기동 그룹으로 구현했고 `infra/ops/up.py --langfuse-only` 진입점을 추가했다. Web/Worker 버전·호환 저장소 버전과 이미지 digest를 고정하고 재실행 시 기존 비밀/볼륨/프로젝트를 재사용한다. 처음 부팅하는 infra 구성에 포함할 방법도 같은 전용 수명주기 코드를 통해 제공한다. 기존 infra를 올릴 때 아직 준비되지 않은 새 저장소가 자동 생성되지는 않게 한다.

첫 구현의 기본안은 전용 DB/큐/객체 저장소다. 기존 PostgreSQL·Redis·MinIO와 물리 공유가 필요하면 호환성·용량·장애 영향을 확인하고 전용 계정/DB/버킷/큐 경계를 명시한다. 기존 도메인 DB·n8n DB·비밀을 그대로 복사해 연결하지 않는다. 이는 이 프로젝트의 배치 결정이며 제품 자체가 항상 전용 물리 인스턴스를 요구한다는 뜻은 아니다.

호스트에는 필요한 Langfuse UI/API만 접근 경로를 마련하고 DB/Redis/ClickHouse는 전용 내부 네트워크에서 통신한다. Agent/ops에는 Langfuse Web API 경로만 연결한다. 사용자 LAN 접속 URL, 인증 callback URL, 내부 API 주소는 구분한다. 미디어 기능을 켤 경우 브라우저용 S3 주소도 검토해야 하며 이번 첫 메타데이터 추적은 미디어 업로드를 포함하지 않는다. 포트 16320은 기동 전 충돌 검사하며 공개 주소는 최초 CLI 입력으로 고정한다. 저장소 호스트 포트는 열지 않는다.

기존 서버 조사에서 Docker 디스크 사용률이 97%였으므로 이미지 공간과 실제 추적 데이터 저장 경로를 기동 전 다시 확인한다. 관측 TSDB와 분리한 데이터 디렉터리·보관/용량 정책을 정하며, 보관 일수가 하드 디스크 quota라고 가정하지 않는다. 이번 코드는 Langfuse 4.46.0과 의존 이미지 digest, 홈의 별도 상태/데이터 경로, 공개 포트 16320을 고정했다. 메모리 한도 합계 21GiB이며 파일시스템 하드 quota는 없다. 프로젝트 자동 보관기간은 해당 버전에서 라이선스 권한이 필요해 이번 OSS 초기화에 설정하지 않는다. 합성 추적만 확인하며 실제 Agent 대량 수집 전에 보관 정책을 확정한다. 공식 문서는 확인 시점 v4를 안내하므로 이전 v3 예시/이동 태그를 그대로 적용하지 않고 구현 단계에 이미지와 SDK/API 호환 조합을 검증한다.

## 3. Agent 계측 범위

현재 `runtime/agent_runtime/agents.py`의 `validate → search → context → generate → finalize`와 `knowledge_tools.py`의 임베딩/검색 호출을 기준으로 붙인다. 기존 코드는 Model/Knowledge HTTP API를 호출하므로 Langfuse 설치나 LangGraph 사용만으로 해당 호출 전체의 추적이 자동 완성되지는 않는다. 실행 제어와 추적 내보내기를 분리한 작은 adapter로 명시적 span을 기록하는 방향이다. 기존 LangSmith 원격 추적 비활성화와 업무 재시도/멱등 정책은 유지한다.

| 기록 | 첫 수집 항목 |
|---|---|
| Agent 실행 | 원래 AgentRun/버전/request ID, 시작·종료, 상태/오류 코드, 추적 연결 식별자 |
| 5개 실행 단계 | 단계명, 시간/지연, 성공·실패·skipped, 부모 실행 관계 |
| 검색 Tool | ToolCall/버전, 임베딩·검색 호출 지연, 선택 지식/모델 버전, 결과 개수 |
| LLM 호출 | 배포/모델 버전, 응답에 있는 토큰 사용량, 지연·종료/오류 |
| n8n 연관 | 이미 검증된 workflow/execution/caller 관계. 없는 식별자는 추정하지 않음 |

AgentRun에 안정적으로 연결되는 trace ID와 단계/호출 span ID 매핑을 정의한다. 같은 업무 요청을 다시 조회/전달해도 모델·검색을 다시 실행하지 않고 기존 실행/trace와 연결한다. trace의 지연 도착·일시 누락은 업무 실패나 실행 없음으로 바꾸지 않는다. 새 계측 전에 끝난 과거 실행을 다시 실행해 추적을 만들지 않는다.

첫 버전은 ID·상태·시간·사용량 중심이다. 질문·프롬프트·답변·문서 전문·벡터·인증 헤더/비밀은 기본 전송에서 제외하고 전송 전 허용 목록/마스킹을 적용한다. 상세 본문 수집은 필요한 범위·접근 권한·보관 정책을 정한 후 옵션으로 확장한다. 초기 화면에서는 본문이 수집되지 않았음을 표시한다. 모델 사용량이 없으면 미수집으로 표시하고, 자체 GPU 모델의 비용을 임의 단가로 계산하지 않는다.

Langfuse 오류·timeout·큐 포화는 업무 요청의 상태를 변경하지 않도록 처리한다. 전송은 제한된 큐/timeout/재시도와 종료 시 한정된 flush를 사용하고 실패·버림 수를 별도 기록한다. 비동기 전송만으로 무손실 또는 exactly-once를 보장한다고 기록하지 않는다. 계측 장애 중 Agent API의 성공/실패·원래 실행 ID는 계속 기준이다.

## 4. ops 연동 범위

ops 서버에 Langfuse API client와 설정을 두고 시간 범위·페이지 수·응답 크기·timeout을 제한해 조회한다. 프로젝트용 키는 브라우저에 노출하지 않는다. Agent 전송과 ops 조회의 자격 증명을 구분해 운영하며, 실제 읽기 전용 scope 지원 여부는 고정 버전/에디션에서 확인한다. 단순히 키를 두 개 발급했다고 권한이 분리됐다고 가정하지 않는다.

첫 화면은 연결 상태, 최근 실행 추적, 단계별 지연/실패, 모델 토큰 사용량, 원래 AgentRun/ToolCall 및 Langfuse 상세 링크를 제공한다. 업무 상태는 Agent API에서, 추적 상세는 Langfuse API에서 가져온다. 데이터 미도착·조회 실패·미계측을 0건/정상으로 표시하지 않는다. 운영 설정은 연결 주소/프로젝트 참조·timeout·조회 범위부터 시작한다. 계정/프로젝트 일괄 관리와 파괴적 삭제는 첫 조회 화면의 필수 기능으로 넣지 않는다.

Langfuse가 제공하는 프롬프트/평가 기능 도입은 별도다. 발행 Agent 정의·프롬프트 버전·Tool 정의·실행 결과의 기준은 계속 Agent Platform이며, 추적 화면에 보이는 복제 정보를 업무 원본으로 바꾸지 않는다. 기존 질문 반복/답변 품질 보류도 유지한다.

## 5. 다음 구현 순서와 인수

1. **O01-Langfuse infra:** 위 기능의 구현·로컬 검사와 실제 서버 6개 Healthy·인증된 합성 추적 수집/재조회가 통과했다. 저장된 같은 trace의 재시작 보존도 서버 인수를 마쳤다. 완료한 검사는 반복하지 않는다. 이번 합성 검사는 모델 추론을 실행하지 않았다.
2. **O02-Agent 계측:** 실행·Tool·임베딩/검색·LLM span과 원래 ID 연결 → 실패/skipped/no_evidence·동일 요청 재사용·전송 장애 중 업무 독립성 검증. 대표 실모델 확인은 필요한 새 요청으로 제한하며 과거 완료 실행을 재생성하지 않는다.
3. **O03-ops 조회:** Langfuse API client → Agent 원본과 연결한 운영 조회 API → 최소 화면/상세 링크. 고정 실행 1건의 실제 사용자 화면 인수는 2026-09-28 완료했다. 다른 실행/오류/미관측 화면은 이 근거에 포함하지 않는다.
4. **O04-운영 확장:** 추적 보관/복구·장애 지연/누락·경보, 필요 시 관리 API와 권한/감사를 확장한다.

각 단계는 구현·로컬 검사·서버 인수를 구분한다. `Healthy`만으로 end-to-end 추적 성공을 판정하지 않고 합성 trace ID가 조회되고 부모/자식·상태/시간이 맞는지 확인한다. 기존 O01 호스트/컨테이너/GPU 인수와 A09 Agent/n8n 인수는 완료 기록을 유지한다.

## 6. 확인 근거

2026-09-26 확인. 제품 공식 문서의 기능/의존성 사실과 위 프로젝트 배치 결정은 구분한다.

- [Langfuse 자체 호스팅](https://langfuse.com/self-hosting): Web/Worker, PostgreSQL·ClickHouse·Redis/Valkey·S3 구성.
- [Docker Compose](https://langfuse.com/self-hosting/deployment/docker-compose): 단일 서버 구성과 초기 계정/headless 초기화, 고가용성/백업 경계.
- [Langfuse 구조](https://langfuse.com/handbook/product-engineering/architecture): SDK 계측·API 수신과 비동기 처리.
- [Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api): 프로젝트 API와 인증, 조회 연동. 조직/인스턴스 관리 API와 구분.
- [전송 전 마스킹](https://langfuse.com/docs/observability/features/masking): 앱에서 입력/출력/메타데이터를 제외하거나 변환하는 방법.
- 소스 확인: `infra/mlflow/docker-compose.yml`, `agent_platform/runtime/requirements.in`, `agent_platform/runtime/agent_runtime/agents.py`, `agent_platform/runtime/agent_runtime/knowledge_tools.py`. MLflow 선언과 기존 Agent 단계는 확인했으며 Langfuse 연동은 미구현이다.
