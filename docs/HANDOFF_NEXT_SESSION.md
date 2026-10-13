# 다음 세션·Codex 인수인계

2026-10-13 추가: 현재 개발 사본의 첫 작업은 src 구조·Ops 통합과 경로 검증이며 다음 기능 작업은 F0a다. [소스 구조](architecture/source-layout.md)를 따른다. 아래 날짜별 인수 상태와 운영 자산은 유지한다.

갱신: 2026-10-12. 사용자 서버 출력과 PC 확인으로 첫 TXT의 GPU 색인·검색 Tool·QA Agent 시험/발행·head CAS revision 1까지 확인했다. 실제 PC 질문은 인용 누락으로 실패했고 prompt-only 초안도 같은 유형으로 실패했다. 상세 근거는 [10월 12일 관찰](testing/document-qa-live-observed-20261012.json), 완료/미완료는 [상태표](status.md)에 둔다. 현재 소스/프로세스가 이 기록과 일치하는지는 새 환경에서 조회로 확인한다.

## 첫 작업: F0a 인용 누락 해결

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
