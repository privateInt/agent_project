# A09 — 통합 인수 도구와 현재 인수 현황

> 2026-10-01 코드 검토: [현재 구현과 남은 작업](work-plan.md)을 함께 확인한다. 아래 날짜별 결과와 검증 한계는 당시 근거로 보존한다.

2026-09-24. 통합 점검·보존 비교 도구와 운영 문서를 구현하고 로컬 검증을 마쳤다. **MVP 전체 인수 완료는 아니다.** [2026-09-25 운영 n8n 전달·동일 요청 재사용·기록 보존](../history/agent-platform-202609.md#n8n-operational-verified-20260925)은 서버에서 통과했다. [Agent·n8n 7개 컨테이너의 재시작 보존](../history/agent-platform-202609.md#restart-verified-20260925)도 통과했다. 질문 반복으로 답변 내용 품질은 미흡하다. 사용자 화면과 의미 품질 평가는 사용자 요청에 따라 보류한다.

A09 최초 작성 당시 서버는 [A08 적용·읽기 검사](../history/agent-platform-202609.md#a08-server-check-20260924)를 통과했다. 이번 A09는 호스트 점검 도구·테스트·문서 변경이며 실행 API/DB schema/UI/이미지/모델/n8n 워크플로를 변경하지 않는다. Service/Gateway a08.1, 실행 계약 a07.1/A07/schema 5를 유지한다.

## 1. 대표 흐름과 근거

| 시나리오 | 확인된 근거 | 현재 남은 범위 |
|---|---|---|
| A02 상태·DB·진단 보존 | 서버 기동·PostgreSQL 제한 권한·재시작 후 같은 진단 조회 PASS | 완료 근거를 재사용 |
| A03 Tool 등록·시험·발행·REST/MCP·멱등 | 서버 기능/DB 검사·재시작 후 원래 ToolCall/request ID PASS | 완료 근거를 재사용 |
| A04 직접/OpenAPI 등록·HTTP 실행 | 로컬 API/SQL/브라우저, 사용자 검색 API 시험 성공 | 전체 OpenAPI→게시→MCP 대표 서버 인수 일부 미확인 |
| A05 자연어→임베딩→검색 | 로컬 REST/MCP/SQL와 사용자 화면 검색 5개 확인 | 전체 서버 인수·의미 관련성 일부 미확인 |
| A06 검색→문맥→LLM→출처 | 서버 기능/DB 검사와 별도 A07-live 실제 LLM 답변/출처·원래 요청 재사용 PASS | 선택된 완료 AgentRun의 Agent/n8n 재시작 보존 PASS; 사용자 화면·품질 보류 |
| A07 운영 n8n | 새 발행 버전/Flow, 실행 4·5의 전달·같은 AgentRun 재사용 PASS. 과거 인용 실패는 보존 | Agent/n8n 재시작 후 같은 전달 기록 보존 PASS; 질문 반복 품질·사용자 Flow 편집 보류 |
| A08 통합 UI·목록·기록 조회 | 로컬 실제 Chromium, 서버 Service/Gateway Healthy·read-only smoke PASS | 사용자 페이지·별도 n8n 로그인/편집 확인 보류 |
| A09 저장된 전체 연결 점검 | 로컬 검사 및 서버 Agent 대표 3개+n8n 4·5 기준 capture/compare PASS | Agent/n8n 재시작 보존 PASS; 화면·품질 인수 미완료 |
| 버전 고정·응답 유실·중단·독립성 | 기존 Go/Python/SQL/n8n 대역 검사와 A09 기록/재전달 검증 | 실제 장애 주입·모든 플랫폼 독립 재기동을 이번에 수행한 것은 아님 |

원래 실패 AgentRun `15981e7e-7ab3-41d6-bef8-ab2924cbe67e`는 검색/문맥/생성 성공, finalize 실패다. `ANSWER_REFERENCE_INVALID`의 정확한 인용 오류 유형은 원문 없이 확정하지 않는다. [품질 후속](answer-quality-followup-20260924.md)의 보류 결정과 출처 검증을 유지한다.

## 2. 점검 도구

`src/platform/agent/ops/acceptance.py`는 기존 Agent/n8n API에 **GET만** 보낸다. 실행·재시도·등록·게시·재시작·키 발급을 수행하지 않는다. 원래 운영 settings.json/request.json과 API 키를 읽지만 수정하거나 내용을 출력하지 않는다.

| 명령 | 동작 |
|---|---|
| audit | 현재 목록에서 대표 완료 실행을 선택하고 Agent→Tool→검색 출처·모델 식별자·n8n 전달/재실행 기록을 읽어 검사 |
| capture | 같은 검사 후 ID와 SHA-256 비교값만 담은 0600 기준 파일 생성. 기존 파일이 있으면 덮어쓰지 않고 compare 수행 |
| compare | 기준 파일에 고정된 실행·단계·ToolCall·정의·n8n 게시 설정/연결/완료 실행을 다시 읽어 비교 |

기본 경로:

- Agent: 서버 내부 `http://127.0.0.1:16100`, 토큰 `src/platform/agent/.secrets/api-token`.
- n8n: 기존 `src/infra/n8n/.secrets/agent-integration-api-key.json`에 기록된 origin/키.
- 운영 세션: `~/workspace/agent_n8n_workflow`, 원래 `request.json`.
- 비교 기준: `~/workspace/agent_acceptance_a09.json` — 프로젝트 밖의 새 파일이며 설정/요청 세션과 별개다.

기본으로 최신 성공·실패·중단 AgentRun을 각각 최대 1개 선택하고 현재 운영 요청에 연결된 완료 AgentRun을 포함한다. `--run-id`를 반복해 최대 20개를 직접 지정할 수 있다. compare는 원래 선택을 유지하며 나중에 추가된 실행까지 자동으로 인수한 것으로 취급하지 않는다. 최신 성공이 no_evidence이면 답변/인용 성공 근거로 계산하지 않는다.

점검 내용:

1. Agent ready·A07/schema 5와 완료된 AgentRun/고정 버전의 정의 hash 일치.
2. 원래 request ID, 검색 ToolCall의 부모 AgentRun/search 단계와 ToolVersion 연결.
3. 성공 응답의 5단계 상태, 실제 검색 결과와 인용 원문/순서의 일치, 고정 모델/지식 식별자.
4. 원래 n8n 논리 요청·질문·AgentVersion·caller_context의 일치. 동일 논리 요청이 서로 다른 AgentRun을 가리키면 실패.
5. 성공한 n8n 실행의 답변/인용/경고/ID가 Agent 결과와 같은지 확인. 일치하는 완료 전달 기록이 2개 이상일 때만 replay_verified=true.
6. 기준 파일과 현재 결과 비교. 질문·답변·문서·n8n 노드 내용은 해시로만 보관하고 출력하지 않는다.

이는 저장 기록의 일관성 검사다. 문서와 답변의 의미적 정확성, 현재 모델의 신규 추론 가능 여부, n8n 전체 업무의 사업적 성공까지 판정하지 않는다. caller_context는 추적 정보이며 인증 증명으로 간주하지 않는다.

### 조회 범위와 보존 주의

Agent 목록은 최대 5,000개, 운영 workflow 실행 목록은 최대 500개까지 읽는다. 초과하면 제한 오류로 중단하며 일부만 읽고 전체 통과로 표시하지 않는다. n8n 실행 데이터가 보관 정책으로 지워졌다면 답변 전달/재실행을 검증할 수 없으며, 기존 기준에 포함된 실행이 사라지면 비교가 실패하거나 변경으로 표시된다. prune 설정을 자동 변경하지 않는다.

운영 세션 폴더가 없으면 n8n을 session_missing으로 표시하고 Agent 기록만 검사한다. 초기 A09 출력의 not_configured는 같은 조건으로, n8n 서버/Flow 미구성을 뜻하지 않는다. 새 계정·키·세션·Flow를 자동 생성하지 않는다. 세션이 있으나 파일/키/권한/내용이 잘못되면 오류를 반환한다. 편집된 n8n Flow를 등록 당시 것으로 덮어쓰지 않는다.

캡처는 완료 기록만 추적한다. 진행 중인 운영 요청이 있으면 캡처를 막는다. 다른 Agent/단독 Tool 작업의 완전한 유휴 검사는 아니며 tool_queue_checked=false를 명시한다. 이 도구만으로 서비스 재시작 가능 여부를 판단하지 않는다.

기준 파일은 최대 256 KiB이며 프로젝트 밖에 보관한다. 원문 복구용 백업이 아니고 사용자 계정에 접근 가능한 사람이 변조할 수 있으므로 서명된 감사 증명도 아니다. 기존 기준을 지워 차이를 숨기지 않는다. 새 질문/새 설정의 별도 기준이 필요하면 새 `--baseline-file` 이름을 사용한다.

## 3. 서버 적용과 최초 확인

기준은 A08 코드 적용본이다. 직전 a08_verified 문서 패치는 적용했거나 아직 적용하지 않았어도 이번 패치에 포함해 처리한다. 알려진 두 버전의 hash만 허용하며 다른 로컬 변경은 쓰기 전에 중단한다.

```bash
(
set -e
cd ~/workspace
tar -xzf agent_platform_a09_20260924.tar.gz
python3 agent_platform_a09_20260924/apply_patch.py \
  --project-root ~/workspace/agent_project --apply

cd ~/workspace/agent_project/agent_platform
python3 -B ops/acceptance.py capture
python3 -B ops/acceptance.py compare
)
```

**이미지 빌드·서비스 재시작·실제 모델 호출 없이** 수행한다. capture는 처음 기준 파일 1개를 생성한다. 같은 capture를 반복하면 원래 기준과 비교한다. 비교 직전 재시작이 없었다면 결과는 두 시점의 보존 확인일 뿐, 재시작 인수로 기록하지 않는다.

파일 생성 없이 현재 상태만 확인하려면 `python3 -B ops/acceptance.py audit`을 사용한다. 세션/포트/키 경로를 바꿔 운영했다면 `--session-dir`, `--url`, `--key-file`, `--request-file`로 기존 값을 지정한다. 새 설정으로 자동 전환하지 않는다.

원래 답변 성공 기록을 특정하려면 최초 capture에 아래처럼 `--run-id`를 붙일 수 있다. 이미 기준이 있으면 선택은 고정돼 있으므로 별도 기준 파일을 사용해야 한다.

```bash
python3 -B ops/acceptance.py audit \
  --run-id dd6fd5e7-8188-45e5-a2a1-7c44375bf043 \
  --run-id 15981e7e-7ab3-41d6-bef8-ab2924cbe67e
```

### 출력 해석

| 값 | 뜻 |
|---|---|
| result=checked | audit의 읽기 검사를 수행함 |
| result=captured | 새 비교 기준을 저장함 |
| result=preserved, preservation=passed | 기준에 고정된 기록·설정이 일치함 |
| result=changed, 종료 코드 2 | 기록 또는 n8n 설정/연결이 기준과 다름. 자동 복원/덮어쓰기 없음 |
| result=failed, 종료 코드 1 | 접근·원본 기록·계약·권한·일관성 검사를 완료하지 못함 |
| record_checks=passed | 선택된 저장 기록 연결·형식이 일치함. 실행 자체가 성공했다는 뜻은 아님 |
| mvp_acceptance=incomplete | 화면·의미 품질 등 별도 인수 항목이 남아 있음. CLI는 외부 재시작 증거를 자동 반영하지 않음 |

2026-09-25 확인한 새 운영 요청은 `agent_state=succeeded`, `delivery_verified=true`, `replay_verified=true`이며 n8n 실행 4·5가 success다. 과거 실패 요청은 기존 ANSWER_REFERENCE_INVALID/finalize로 보존된다. 기록 점검 성공과 답변 내용 품질을 구분하며 pending 배열을 확인한다. 현재 답변의 질문 반복은 자동 품질 검사와 별도로 관찰한 문제다.

## 4. 재시작 보존 인수 — 서버 확인 완료

2026-09-25 19:48 KST의 별도 유휴 점검·실제 7개 컨테이너 재시작·Healthy·전후 preserved를 [완료 근거](../history/agent-platform-202609.md#restart-verified-20260925)로 기록했다. 완료한 검사는 반복하지 않는다. 아래는 향후 유지보수 시 적용할 일반 절차다.

최초 A09 도구 적용은 재시작하지 않았다. 향후 유지보수 때 실행 유입을 멈추고 Agent/Tool/n8n 작업이 끝난 것을 확인한 뒤 capture를 수행한다. 관리자가 실제로 재시작한 대상과 시간을 기록하고, 다시 준비 상태가 된 후 compare한다. 비교 기준을 만든 이후 Flow의 이름/노드/게시 버전·연결을 바꾸면 그 변경도 차이로 표시된다.

- Agent Service/Gateway만 교체해 확인한 A08 기록 보존과 DB/Runtime/MCP/n8n까지 포함한 전체 재시작 인수를 구분한다.
- n8n 게시 중지/재게시 등 유지보수 변경은 캡처 전 또는 보존 비교 후에 수행한다. Flow 중지와 연결 숨김은 다르다.
- compare가 preserved여도 도구가 프로세스 재시작 사실을 확인하지는 않는다. 실제 재시작 로그와 함께 인수 근거로 사용한다.
- 기준 파일·운영 요청·Agent DB·n8n DB/암호화 키를 삭제하지 않는다. 실행 중 worker를 재시작하면 interrupted가 될 수 있고 완료 실행으로 자동 재처리하지 않는다.

## 5. 개발 검증

- 새 Python 검사 18개 통과: 잘못된 부모/출처/인용/모델, 실패의 성공 오판 방지, 중복 Agent 실행, n8n 전달 불일치, 실행 기록 소실, 기준 보존·0600·symlink/프로젝트 경로 거절, 조회 전용 경계.
- 실제 Go Service/Gateway + Python/LangGraph + PGlite PostgreSQL WASM + HTTP 모델/검색 대역의 Agent 통합 검사 통과.
- 실제 n8n 2.40.5 HTTP 서버·격리 SQLite에서 prepare/run 재사용, 정상 전달 2회와 같은 AgentRun, capture/compare/기존 capture 재사용, 운영 세션 파일 바이트 보존을 확인했다.
- Go 테스트가 임베딩·검색·채팅 호출 수를 검사했다. 새 A09 조회는 호출을 늘리지 않았고 운영 n8n의 두 run은 합계 각 1회만 실행했다.
- 사용자 Docker/PostgreSQL 16·GPU를 여기서 실행하지 않았다. 화면 코드는 바뀌지 않아 A08 브라우저 인수 근거를 재사용한다.

작은 요약은 `src/platform/agent/tests/results/a09-acceptance-summary.json`에 저장한다. 원문 자료·모델 답변·대용량 로그·테스트 폰트는 패키지에 추가하지 않는다. 운영 절차는 [운영 안내](operations-a09-20260924.md)를 따른다.

## 2026-09-25 서버 확인과 비교 범위

[서버 결과·세션 복구](../history/agent-platform-202609.md#n8n-session-recovery-20260925)를 따른다. 기존 기준 파일의 n8n={}이면 compare/capture 재사용은 Agent 기록만 비교하고 n8n.status=not_captured_in_baseline로 표시한다. 로컬 세션 복구만으로 기존 비교 범위를 확대하거나 기준 파일을 덮어쓰지 않는다. 새 n8n 범위는 audit 또는 별도 --baseline-file로 캡처한다. 기준에 n8n 기록이 있던 경우에는 원래대로 세션/실행 소실·변경을 검사한다.
