# F0a 재개 분석 — 2026-10-14

범위: 기존 대화·현재 저장소 대조와 다음 읽기 전용 조사 준비. 제품 수정·추론·발행·운영 적용은 수행하지 않았다. F0a의 완료선은 [확장 계획](../plans/platform-expansion-work-plan-20261012.md), 검사 계획은 [F0a 계획](../plans/document-qa-f0a-test-plan-20261014.md)을 따른다.

## 재개 기준

- 입력 소스는 준비 Draft PR #1의 `5962f897d70d0d754cbcd43627548a84750ba0b6`이다. 원격 `main`은 `6dd46d069079fc20df5b3d4bac71fe3450550bf8`이었다. 이 분석은 별도 `codex/f0a-citation-diagnostics-20261014` 브랜치에서 수행했다.
- 전달된 대화 일부는 `.codex` 권한 `0775` 진단에서 끝난다. 이후 저장된 [기본 설정 근거](../testing/codex-config-native-20261014.json)와 [실행 환경 근거](../testing/codex-runtime-native-20261014.json)는 비활성 기본값 적용 성공과 후속 샌드박스 문제를 기록한다. 설치·권한 수정·설정 적용을 처음부터 반복할 단계가 아니다.
- 최신 대화의 F0a 대화형 개발 합의와 이번 사용자의 재개 요청을 따른다. 서버 Codex 무인 실행의 준비 완료로 해석하지 않는다. 샌드박스 문제의 누적 시간·시도 및 보류는 기존 근거를 유지한다.
- Experiment Advisor는 별도 [Draft PR #2](https://github.com/privateInt/agent_project/pull/2)의 비긴급 F11 설계다. F0a의 입력 브랜치에 그 PR의 문서를 섞지 않았으며 구현·모델 비교를 시작하지 않았다.

## 저장된 실패와 소스 대조

[10월 12일 실제 관찰](../testing/document-qa-live-observed-20261012.json)의 두 실패를 현재 소스 경로와 대조했다. 관찰 JSON의 실제 서버 결과와 이 세션의 정적 분석은 서로 다른 근거다.

| 확인 대상 | 저장된 관찰 / 현재 소스에서 확인한 동작 | 판정의 한계 |
|---|---|---|
| 원래 PC 질문 | validate/search/context/generate 성공, finalize에서 `ANSWER_REFERENCE_INVALID` / `MISSING_REFERENCE`. 선택 자료 1개, 인식 인용 0개, `stop`, 생성 13토큰·15바이트 | 실패 답변 원문이 없어 답변 내용을 추측할 수 없음 |
| prompt-only 후보 | 같은 단계에서 같은 오류, 선택 자료 1개, 인식 인용 0개, `stop`, 생성 21토큰·49바이트. 미발행·미활성 | prompt 지침을 더한 시도가 이미 실패했으며 재시도 근거가 아님 |
| Agent 입력 구성 | [agents.py](../../src/platform/agent/runtime/agent_runtime/agents.py)의 `chat_payload`는 system에 `POLICY + version.prompt`, user에 question/sources JSON을 넣음. `select_context`는 자료에 순서대로 S1… 식별자를 배정 | 현재 저장소 소스이며 활성 컨테이너의 코드·실제 요청 캡처를 확인한 것은 아님 |
| Model 전달 | [chat.go](../../src/platform/model/gateway/app/chat.go)는 검증한 messages/max_tokens/temperature에 배포 alias와 stream=false를 더해 vLLM `/chat/completions`로 전송. `content`는 assistant 응답에서 읽음 | 실제 vLLM 이미지·tokenizer/chat template의 적용 상태는 미확인 |
| 인용 gate | runtime은 generate 단계에 본문 없는 인용 진단을 남기고 finalize에서 누락·잘못된 형식·범위 밖 인용을 거부 | 문법 검사 통과는 답변 사실·근거 일치의 품질 인수가 아님 |

현재 증거에서 출력 길이 제한에 도달한 실패는 관찰되지 않았다. 모델 크기·system 지침 누락·template 문제 중 어느 하나를 원인으로 확정할 수 없다. 정적 전달 경로에 인용 정책이 있다는 사실만으로 실제 모델이 그 정책을 올바르게 적용했다고 판정하지 않는다.

## 다음 조사 순서

1. 실제 서버의 운영 원본과 개발 snapshot, 실행 중 QA/Agent/Model의 신원을 읽기 전용으로 대조한다. 이번 환경에는 해당 서버 접근 수단이나 인증 참조가 없으므로 **BLOCKED**다. 저장소를 받은 사실은 서버 접근 근거가 아니다.
2. 기존 설정의 Agent 주소·token_file을 재사용해 두 Run이 가리키는 AgentVersion 정의와 definition hash를 조회한다. 원본/후보의 prompt 외 필드 동일성, Tool/model/Connection pin과 max_tokens/temperature를 대조한다. 이미 저장된 두 Run 진단을 이유 없이 다시 실행하지 않는다. prompt·질문·자료·credential 원문은 공개 출력에 넣지 않는다.
3. 2의 pin에서 실제 Model deployment/version·runtime 이미지와 tokenizer/chat template 신원을 확인한다. 먼저 제한된 metadata를 읽는다. 컨테이너 전체 inspect/env/config 덤프, 임의 모델 다운로드, Model chat POST를 조회로 취급하지 않는다. 실행 중 코드와 저장소 소스의 동일성도 별도 확인한다.
4. 차이가 확인되면 수정 지점 하나와 예상 영향을 정하고 검사 계획을 구체화해 독립 사전 검토를 받는다. 그 전에는 prompt·모델·검색 조건을 바꾸지 않는다. 원문이 없는 과거 실패를 재실행해 답변을 복원하려 하지 않는다.
5. 수정 후보의 실제 비교·품질 인수와 검증된 자산 전환은 검사 계획의 후속 단계다. 현재 head·실패 이력·원래 key/receipt는 [인수인계](../HANDOFF_NEXT_SESSION.md)의 보호 대상을 유지한다.

실제 서버 조회 명령은 사용할 계정과 현재 경로·설정 참조에 맞춰 먼저 작성·검토한다. 이 문서의 조사 목록 자체는 실행된 명령이나 실제 연동 통과 근거가 아니다.

## 이번 단위의 검증과 결과

문서의 내부 링크, 소스 경로, 기존 관찰 JSON의 수치, 변경 diff와 제품 소스·공통 배포판·과거 근거 불변을 확인하고 별도 reviewer가 검토한다. 문서만 변경하므로 기존 제품/fixture·GPU·브라우저 검사를 반복하지 않는다.

결과는 **재개 분석 및 조사 계획 준비**다. F0a 오류 해결·첫 답변 품질·기능 사용 가능·무인 실행 가능 판정은 미완료다. 독립 검토와 GitHub 게시 결과는 이 작업의 Draft PR에서 기록한다.
