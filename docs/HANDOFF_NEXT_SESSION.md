# 다음 세션·Codex 인수인계

2026-10-17 PR25 후속 자동 대응: 제출 전 고정한 UUIDv4와 exact userMessage 원문으로 turn/item을 자동 연결한다. 완결된 pre/post 이력·5초 fence·신규 claim·dispatch 기록을 요구하며 중복/본문 불일치/불완전 조회는 미확정, 재제출은 금지한다. [새7그룹 최초 PASS·단일 의미 변이 검출·최종 독립 PASS](testing/codex-owner-message-correlation-observed-20261017.json)를 보존했다. 실제 고정 test runner 결과와 standalone reviewer 응답을 원본 bytes/hash/이전 단계 digest로 연결했다. Draft 실제 응답은 게시 후 전용 체크포인트에 기록한다. native ACK/clientId 동일성·인증된 required-check 발행·고정 role workflow는 미인수이며 owner RPC/실제 queue/설치/서비스/PAUSE 변경0, 기존 PASS·패키지·원본11개를 유지한다.

2026-10-17 CLI 연결 인계: PR24 `02724c5f7fbc0aadecebe2befdaf9f8990374869` 위 별도 개발 브랜치에 owner CLI controller·bounded completion read scope·codex_shshin 설치 절차를 추가했다. [새6그룹/의미 변이 근거](testing/codex-owner-cli-controller-observed-20261017.json)와 [지원 계약·미연결 부분](operations/codex-owner-cli-controller.md)을 읽는다. 기존 PASS는 재사용했으며 실제 CLI 제출/owner RPC는0이다. 자동 turn 매핑은 구현하지 않았고 owner가 실제 관측한 binding이 없으면 UNBOUND로 멈춘다. 원본 미커밋11개·운영·인증정보·VMS·scheduler PAUSE·닫힌 D18 범위를 유지한다.

2026-10-17 owner ACK adapter: PR21 고정 commit 위에 기존 소유 client의 decoded 결과를 제한된 canonical bytes/hash 또는 고정 오류 코드로 준비하는 순수 함수를 추가했다. [이번 단위 근거](testing/codex-owner-ack-adapter-observed-20261017.json)는 이전 ACK 원장 PASS와 분리한다. 새4개 최초 실행 PASS(FAIL0/ERROR0/SKIP0)·의미 변이2개 검출(각 FAIL1/ERROR0, 실제 잘못 준비된 bytes/hash)과 임시 SQLite 저장·재개방 결합을 확인했다. 실행과 체크포인트 갱신을 묶었으며 최종 독립 판정은 같은 근거의 고정 보고서를 따른다. 실제 client 호출·controller 연결·두 native 작업은 미검증이고 PAUSE/D18 보류를 유지한다.

2026-10-17 ACK: [전용 원장 실행 근거](testing/codex-queue-receipts-observed-20261017.json)의 새4개 최초 PASS·2의미 변이 검출을 재사용한다. 검사 실행 뒤 상태 갱신 지연으로 남았던 NOT_RUN을 정정했다. 기존 검토·PASS를 반복하지 않는다. 최종 독립 검토 판정은 같은 근거의 고정 보고서를 따른다. 원본11개·첫 오류를 보존하며 native RPC/usage 재조회·old thread-resume·PAUSE/D18 해제를 하지 않는다.

2026-10-17: 최신 owner query1회는 ordinaryUsageAllowed=true/주간14%이며 과거 차단을 보존했다. [U01b2 실행 기록](testing/codex-owner-binding-observed-20261017.json)의 새6개/2변이와 최초 오류2개를 재사용한다. quota3·binding2 PASS를 반복하지 않고 실패 fault1만 보정·재검증했다. 최종 검토·Draft PR 연결 뒤 다음 ACK/turn/result 단위로 이어간다. old six-RPC/thread-resume 재실행과 PAUSE/native D18 해제는 하지 않는다.

2026-10-17 KST U01b2: PR18 고정 `780d0755378b9f95f0282ec766ca99300b2b0b53` 위에 오프라인 queue/list 관측기를 추가했다. 기존 공개 `read_intent` 한 번으로 원래 intent를 읽고, client·prompt bytes·thread·원문 hash와 완결 pagination의 유일 후보를 대조한다. 전용 임시 SQLite/독립 소스 복사본의 새4개 최초 PASS와 두 의미 변이 검출(각 FAIL1/ERROR0), 원래 UNKNOWN/raw/hash/revision 및 재개방 보존을 확인했다. 기존 PASS는 반복하지 않았다. 최종 독립 검토는 별도 근거에 기록한다. 실제 ACK 저장·turn/result·controller/scheduler 연결과 두 실제 작업 연속 실행은 미완료이며 runtime D18 보류·두 PAUSE·운영/인증정보/main/VMS 보호를 유지한다. [새 근거](testing/codex-queue-correlation-observed-20261016.json).

2026-10-16: PR17 고정86b8861 위의 [intent 저널 실행 근거](testing/codex-unattended-intent-observed-20261016.json)를 이어받는다. source902fb39e는 유지했고 검사 helper 두 호출만 수정했다. 최초4PASS/1FAIL과 오류 원문을 보존했으며5번째만 재검증PASS, 두 변이 의미FAIL1/ERROR0이다. 기존 PASS·전체5개·R03/R04/R15를 반복하지 않는다. 실제 두 개발 작업/하위 Auto-review/native 연결은 미완료이며 보호 설정과 runtime 조사 상한을 유지한다.

2026-10-16: 고정 PR #15 commit `c862bfc6951491192a9fe98b535c9a8be921ee35` 위의 [U01a 검증](testing/codex-unattended-admission-observed-20261016.json)과 [전송 Draft PR #16](https://github.com/privateInt/agent_project/pull/16)을 이어받는다. agent_project만 자동 작업 대상으로 유지하고 vms_project는 제외한다. 순수 입력 검증은 owner/서명/OS 격리 인수가 아니며 실제 queue 두 작업·Auto-review는 미완료다. source·기존 결과와 첫 오류·원본 11개 변경은 전용 개발 경로에 보존했다.

2026-10-15 L01 재개 기준: PR #14의 `c91e9e628402fcc89c2daa4301d54c3c321221ed`에서 분리한 fixture 설정 로더는 [계약](codex/github-fixture-config.md)과 [새 실행 근거](testing/github-fixture-config-observed-20261015.json)를 참조한다. 개발용 임시 설정만 사용했고 실제 소유자 승인값·운영 설치는 미제공/미실행이다. 기존13+3·R03/R04/R15와 전체 suite를 반복하지 않는다. 다음에는 보호된 실제 설정 입력·설치 인수를 준비하고 별도 역할/issuer 연결을 선정하며, 원래 개발 checkout의 변경11개·최초 실패·기존 sandbox 보류를 보존한다.

2026-10-13 규칙 준비: 공통 정책과 템플릿·프로젝트별 책임·첫 작업 연결을 [Codex 문서](codex/README.md)에 정리했다. 문서의 GitHub main 반영과 서버 실행 환경 적용은 아직 완료되지 않았다. [현재 상태](status.md)를 기준으로 읽고 본문에 보존한 과거 근거와 구분한다.

2026-10-13 추가: src 구조·Ops 통합의 로컬 적용과 검증은 완료됐다. 구조 커밋은 `59c3da1121d3934804fb489e7794f364a0484331`이며 [native 검사 근거](testing/source-layout-native-20261013.json)에 새 회귀가 없음을 기록했다. [소스 구조](architecture/source-layout.md)를 따른다. 구조의 GitHub main 반영은 `6dd46d069079fc20df5b3d4bac71fe3450550bf8`에서 확인했으며 원본 운영 프로젝트와 VMS는 보존한다. 사용자 추가 지시에 따라 본격적인 Codex 기능 개발 전에 사용자에게 먼저 알리고, 정리한 공통 규칙·구성·프로젝트 문서를 기준으로 실제 서버 실행 준비를 확인한다. 이 준비 전에는 F0a/H01 기능 작업이나 무인 실행을 시작하지 않는다. 아래 날짜별 인수 상태와 운영 자산은 유지한다.

갱신: 2026-10-12. 사용자 서버 출력과 PC 확인으로 첫 TXT의 GPU 색인·검색 Tool·QA Agent 시험/발행·head CAS revision 1까지 확인했다. 실제 PC 질문은 인용 누락으로 실패했고 prompt-only 초안도 같은 유형으로 실패했다. 상세 근거는 [10월 12일 관찰](testing/document-qa-live-observed-20261012.json), 완료/미완료는 [상태표](status.md)에 둔다. 현재 소스/프로세스가 이 기록과 일치하는지는 새 환경에서 조회로 확인한다.

## 템플릿 준비 후 첫 기능 작업: F0a 인용 누락 해결

1. [AGENTS.md](../AGENTS.md)와 [확장 계획](plans/platform-expansion-work-plan-20261012.md)을 읽고 실제 repository와 실행 중 QA/Agent/Model의 신원을 확인한다. 기본 설정은 기존 사용자 state에서 읽는다.
2. 원래 질문/Run과 후보 Run의 저장 진단을 읽는다. 두 실행 모두 validate/search/context/generate 성공, finalize에서 ANSWER_REFERENCE_INVALID, MISSING_REFERENCE, finish_reason=stop이다. 최신 후보는 생성21토큰/49bytes, max_tokens512·선택 source1·인식 인용0이다. 원문 답변은 진단에 저장되지 않아 내용을 추측하지 않는다.
3. native AgentVersion의 prompt·runtime chat payload·Model 전달·실제 model/template를 확인한다. 실제 모델은 google/gemma-3-1b-it이며 크기만으로 원인을 확정하지 않는다. 검색/문맥이 통과했다는 사실과 답변의 의미 정확도는 구분한다. 변화 한 곳의 근거와 소형 비교를 만든다.
4. 인용 검증은 유지한다. 이미 terminal 실패한 두 Run/plan을 재실행하거나 성공으로 바꾸지 않는다. 새로운 비교는 새로운 명시 실험으로 고정한 intent/key/receipt를 사용한다. 반복 prompt trial/자동 repair·숨은 fallback·자동 S1 부착으로 통과시키지 않는다.
5. 코드/담당 부서 질문의 정확한 답변·실제 출처와 근거 없는 질문을 검증한다. 검증된 후보만 adapter의 발행/readback과 expected_revision=1 CAS로 전환한다. 새 head가 생겨도 과거 질문은 head1·원래 target/실패를 유지한다. 현재 후보는 failed/unpublished이므로 발행·활성화하지 않는다.

## 실제 환경과 참조

| 대상 | 마지막 사용자 확인값 |
|---|---|
| 서버/계정 | simsys4090, shshin, Conda sh_test |
| 프로젝트/Python | /home/shshin/workspace/agent_project, /home/shshin/miniforge3/envs/sh_test/bin/python3, Python3.10.19 |
| QA state/settings | ~/.local/state/agent-project-document-qa/uploads, 상위 settings.json |
| host config | ~/.local/state/agent-project-ops/server/host-config.json |
| QA API/PC | callback bind 172.25.0.1:17110, 추가 LAN bind 192.168.0.119:17110, /console q08.1 |
| 실제 옵션 | allow-indexing-inference, allow-question-head-activation, allow-question-inference |
| 현재 마지막 head | revision1 / target 01851866-e7bd-5c5f-a405-2f949c22c60d |
| 정적 상태 표시 | 구단계 CLI/production_* false와 실제 question-policy를 구분. flag 수정으로 대체 금지 |
| 실제 모델 | embedding snowflake-ko-v1; chat google/gemma-3-1b-it, resolved revision dcc83ea841ab6100d6b47a070329e1ba4cf78752 |
| 서버 Node | v12.22.9; 화면검사58개 OK(skipped=1). 신규 JS 클래스 skip은 JS 인수 통과가 아님 |

위 주소는 이 서버의 참조이며 일반 사용자 기본값이 아니다. 새 Codex가 로컬 서버에서 작업하면 해당 계정/설정을 재사용한다. 다른 환경의 root home에서 STORE_NOT_INITIALIZED가 나왔다는 이유로 새 저장소를 초기화하지 않는다. 키 값은 문서에 넣지 않으며 기존 uploads/api-token 파일로 인증한다. QA API가 이미 실행 중이면 다시 serve해 포트 충돌을 만들지 않는다.

필요할 때만 확인할 당시 수동 실행 형태:

```bash
/home/shshin/miniforge3/envs/sh_test/bin/python3 -B \
  -m src.apps.document_qa.api serve \
  --bind 172.25.0.1 --additional-bind 192.168.0.119 --port 17110 \
  --allow-indexing-inference \
  --allow-question-head-activation \
  --allow-question-inference
```

## 현재 자산과 실패 참조

| 자산 | ID |
|---|---|
| Library / job | e1172d34-4500-426e-9440-d93310b6fbf1 / 430b0e7b-7b4c-4ae7-9c57-6c979ca94343 |
| Data source / canonical | 8db38fb1-ba4e-41b8-aff3-b2421ff6ab16 / fec048c6-ea13-40f3-bef3-eddbfca9799a |
| Index / version | a09f8482-6e10-44dc-aefe-ce0ad554841d / fab90f1d-9d92-4923-8550-8c5cbcbe620f |
| embedding deployment / model version | d4d3eda4-a857-4083-8828-e36e7fa0f534 / 65c3f2ff-b1fd-4a39-8dd7-aaf34c34579e |
| chat deployment / model version | 14527d72-751a-459f-8615-7186e5faa0f3 / a6debd09-1bc7-4978-9208-8f6eb7935163 |
| published Tool plan / version | fcff20e1-4eed-53d4-aac6-4f3e318e2728 / ecc5cc0b-fb25-4578-9521-3760dfd9747f |
| original published QA plan / AgentVersion | b743f343-8b82-5a5e-a027-57003e9e2184 / 41330cd4-618f-429e-949e-a8ef3b1337ab |
| successful smoke AgentRun | 46e5c8ac-9e67-46fd-a0e8-d463683b0b2c |
| failed PC question / AgentRun | cae86bb2-49ad-5009-bf84-4aa8e962e3cf / f8923983-dfec-4b49-96e4-9933dbdbd3eb |
| failed prompt trial plan / AgentVersion | 1f903dcb-bca2-55c9-a4fa-0e0db7564661 / 73053c8f-2dd3-4e16-b37c-3c808bda43a9 |
| failed candidate AgentRun | ba35175e-002b-45d1-8e13-cd4b337fd77f |

자료 본문에는 연결 검증 코드 QA-CPU-20261007과 담당 부서 플랫폼 개발팀이 있다. native 검증 chunk/source 및 hash는 관찰 JSON에서 확인한다. smoke 질문의 answered/S1 성공은 위 특정 PC 질문의 성공이 아니다. 두 번째 문서/자료 제외·장애 복구·폭넓은 품질·신규 업로드 전체 자동 경로는 아직 인수 전이다.

## 코드와 진단 시작점

- [질문 화면](../src/apps/document_qa/web/questions.js), [UUID 수정](document-qa/question-uuid-fix.md): canonical 서버 UUID v5 허용, browser client key v4 계약 유지. 실제 접수까지 후속 확인했으며 현재 막힘은 답변 인용 누락.
- [QA adapter](../src/apps/document_qa/qa_agent.py), [head](../src/apps/document_qa/question_head.py): 불변 plan/intent/receipt·시험/발행·CAS·질문 당시 target.
- [native 진단](../src/platform/agent/ops/diagnose_answer.py): 기존 Run과 /steps GET만으로 저장된 인용 진단 조회. 자격은 저장된 settings의 agent token_file 참조를 사용.
- [Agent runtime](../src/platform/agent/runtime/agent_runtime/agents.py), [Model chat](../src/platform/model/gateway/app/chat.go): system+prompt/user sources 구성, 전달과 인용 검증.
- 서버 workspace에 전달한 document_qa_citation_agent_trial_20261012.py는 실패한 후보의 기존 계획/키를 보존하는 보조 도구다. 소스 저장소 필수 파일이라고 가정하거나 --execute를 새로운 비교 생성 수단으로 반복하지 않는다.

## 인계 후 진행

F0a→F10a CI→F0b 문서 QA 제품 인수→학습데이터 이전/형식→Data 일정/Airflow→HWP→provider/LiteLLM→기본 MCP→세션/기억→문서 Tool→그래프 평가→CD 순서다. 상세 작은 단위/완료선은 [확장 계획](plans/platform-expansion-work-plan-20261012.md)에만 유지한다. Ops 운영 백업/복원·재부팅·Kubernetes 보류와 기존 설정/ID/키 보존은 계속 적용한다.

## Codex 시작 요청문

> AGENTS.md와 docs/HANDOFF_NEXT_SESSION.md, docs/status.md, docs/plans/platform-expansion-work-plan-20261012.md를 읽고 실제 repository/runtime을 조회로 대조해줘. 첫 작업은 F0a 문서 QA 인용 누락 해결이야. 기존 head revision1과 정상 색인/Tool/Agent, 실패 질문/두 Run·plan·intent/key/receipt를 보존하고, 인용 검증을 유지하면서 코드/담당 부서 답변과 실제 출처·근거 없는 질문을 확인해줘. 모델 크기만으로 원인을 확정하거나 실패 후보를 발행하지 마. 관련 검사와 필요한 실제 인수 후 상태/다음 작업을 기록하고, 의존성이 충족된 계획의 다음 작은 단위로 이어서 진행해줘. 완료한 초기화·모델 생성·Flow 등록·성공 검사는 반복하지 말고, 실제 접근/필수 입력이 없는 항목은 미완료로 남기면서 독립 작업을 진행해줘.


## 2026-10-17 소유 client의 제한된 metadata 연결

2026-10-17 한정 D18 조사는 03:16:41–03:25:25Z, metadata7/7응답·retry0으로 닫았다(절대마감03:31:41Z). 남는 RPC는 이월하지 않으며 old6script/thread/resume은 실행하지 않는다. PR22 고정 d59de21d4b43a71f3e518592c589b0761e668de5 위에서 PR16 고정 전송기를 재사용한 read_only metadata 연결 단위를 별도 구현했고 새4/변이2 검증을 최초 실행에서 통과했다. scheduler PAUSE·운영/auth·VMS 제외·원본11 보존을 유지하고, 신규4/변이2와 최종 독립 검토는 현재 근거 JSON을 확인한다. 설치/service 활성화는 별도 변경안 검토 대상이다.
