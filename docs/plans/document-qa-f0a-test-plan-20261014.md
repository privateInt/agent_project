# F0a 첫 진단 단위 검사 계획

계획 버전: 1.0 / 2026-10-14. 작성 역할: test_agent. 독립 reviewer의 사전 검토 전이며 구현 승인이나 제품 인수 기록이 아니다.

## 범위와 기준

이번 단위는 **재개 상태 확인과 읽기 전용 native 정의·payload·template 조사 계획**이다. 제품 코드, 테스트 코드, prompt, 모델, 검색 설정, 운영 상태를 바꾸지 않는다. 새 질문·추론·trial 실행, Agent 발행, head 활성화도 이 계획만으로 시작하지 않는다. 총괄이 별도로 정한 격리 대화형 작업과 Draft PR 범위를 따른다.

요구사항은 [AGENTS.md](../../AGENTS.md), [인수인계의 F0a](../HANDOFF_NEXT_SESSION.md#템플릿-준비-후-첫-기능-작업-f0a-인용-누락-해결), [확장 계획 F0a](platform-expansion-work-plan-20261012.md), [프로젝트 검사 연결](../codex/project.md#검사-연결)을 참조한다. 공통 정책은 [TESTING.md](../codex/common/TESTING.md)의 D28~D38 및 [DECISIONS.md](../codex/common/DECISIONS.md)의 D04~D09·D17~D18을 따른다. 공통 판정 규칙을 이 문서에 복제하지 않는다.

이번 계획의 완료선과 F0a 완료선은 다르다. 이번에는 확인된 코드 경로, 원인으로 단정할 수 없는 사항, 새로 필요한 native 증거, 검사 선정과 미실행 이유를 검토 가능하게 남긴다. 실제 답변과 출처의 F0a 인수는 계속 미완료다. 이 문서의 사전 검토 PASS는 다음 제품 구현 승인이 아니다. 조사로 변경 지점이 확정되면 영향별 구체 검사·명령·환경·판정 기준을 추가하고 다시 사전 검토를 받아야 한다.

## 고정 baseline

| 대상 | 이번 계획의 고정 입력과 해석 |
|---|---|
| 저장소 | `5962f897d70d0d754cbcd43627548a84750ba0b6`; 이번 새 격리 clone에서 작업 시작 전 변경 없음. 사용자 서버 working tree는 미조회. 이후 변경은 별도 diff로 확인 |
| 과거 실제 관찰 | [document-qa-live-observed-20261012.json](../testing/document-qa-live-observed-20261012.json), SHA-256 `32bafbab94ac41ac4bdd0fc5715aae136f61dca9020720578b864652957f6e31`; 당시 사용자 출력 근거이며 현재 서버 조회가 아님 |
| 원래 실패와 후보 | 관찰 JSON의 `PC_question`, `failed_prompt_trial` 두 terminal 실패. validate/search/context/generate 성공, finalize의 `ANSWER_REFERENCE_INVALID`·`MISSING_REFERENCE`, finish_reason=stop. 각각 completion 13/21토큰, answer 15/49bytes, 선택 source 1, 인식 인용 0, max_tokens 512 |
| 현재 자산 | 같은 JSON의 head revision1·target, published Tool/AgentVersion·index/model pin, 기존 question/run/plan·intent/key/receipt. 새 head나 run으로 원래 이력을 대체하지 않음 |
| 문서·정답 기준 | 같은 JSON의 source/canonical/chunk ID·SHA-256과 기대 코드·담당 부서. 정답은 채점에만 사용하며 모델 prompt에는 넣지 않음 |
| 실제 모델에 대한 마지막 관찰 | `google/gemma-3-1b-it`, resolved revision `dcc83ea841ab6100d6b47a070329e1ba4cf78752`, vLLM, max_model_len 4096. 현재 실행 모델/template와 같다고 가정하지 않음 |
| 소스·Agent | [agents.py](../../src/platform/agent/runtime/agent_runtime/agents.py), SHA-256 `4b2763a06e875878dde936c80b9fd9af31943dba33388038f01cf575b78cc122` |
| 소스·Model | [chat.go](../../src/platform/model/gateway/app/chat.go), SHA-256 `e85d1a9a2399dd9f205da52740ce6f77c73aa34eb6ea820cf6264c4b94739263` |
| 기존 진단 도구 | [diagnose_answer.py](../../src/platform/agent/ops/diagnose_answer.py), SHA-256 `342c520d9591e36e47669dc1a90a062d070d6b27388b64d1364b9bad23bc268d`; 저장된 Run/steps GET의 요약이며 새 추론이 아님 |

이미 확보된 두 Run의 단계·인용 진단을 반복해서 얻는 것은 이번 단위의 필수 검사가 아니다. 새 조회는 최신 정의/실행 신원의 부재나 드리프트를 확인하는 데 필요한 항목으로 제한한다. 과거 원문 생성 답변은 기록되지 않았으므로 내용을 복원했다고 말하거나 의미 오류를 추정하지 않는다. finish_reason=stop 관찰만으로 문맥 누락, prompt 무시, template 결함 또는 모델 능력 중 어느 하나를 원인으로 확정하지 않는다.

## 이번 단위의 필수 확인

| ID | 요구사항·상황/입력 | 확인할 데이터와 통과 기준 | 현재 상태·환경 |
|---|---|---|---|
| P01 | 격리 재개와 baseline | HEAD, branch/remote의 신원, 시작 diff, 허용 파일 목록과 실제 diff가 연결됨. 운영 원본·보류 후보·DB·키를 변경하지 않음 | 저장소 읽기 가능; 총괄의 작업 기록과 연결 |
| P02 | 기존 관찰 재사용 | 두 실패, original/candidate 신원·stage·진단과 smoke의 제한이 관찰 JSON과 일치. 원문 미보존·현재 runtime 미조회·의미 품질 미인수를 명시 | 로컬 자료 확인 가능; 재추론 불필요 |
| P03 | Agent→Model 정적 경로 | `chat_payload`는 POLICY+version.prompt를 system, question+선택 source JSON을 user로 구성. source id 순서·원문과 max_tokens/temperature가 `generate`의 공식 chat API body로 연결됨. `select_context`의 제외/크기 경계와 finalize의 검증을 함께 확인 | 소스 읽기로 확인; 현재 native payload가 같다는 인수는 아님 |
| P04 | Model→vLLM 정적 경로 | `parseChat`의 role/content·범위 검증, 배포/모델 신원 검사, 저장 배포의 endpoint/alias, `model`과 `stream=false` 추가, 응답 content·finish_reason·usage 전달을 확인. template 선택을 이 gateway가 수행한다고 추정하지 않음 | 소스 읽기로 확인; serving의 template 적용은 P06 필요 |
| P05 | native Agent 정의와 payload 재구성 | 승인된 서버의 기존 설정/credential 참조를 재사용하여 원래 published AgentVersion과 failed candidate의 immutable 정의·해시, Tool/Model pin, prompt·context budget·generation 설정을 읽기 전용 대조. 원래 질문과 선택 chunk로 **추론 없이** payload를 재구성해 구조·길이·해시·source 개수/id만 요약. live 정의가 현재 소스와 다르면 차이를 먼저 기록 | **BLOCKED: 이 작업 환경의 실제 서버/API 접근 미확인**. 외부 서버 접근 없이 fixture로 대체 통과 불가 |
| P06 | 활성 runtime와 template 메타데이터 | QA/Agent/Model의 실제 소스/이미지·실행 버전, chat 배포/모델 revision·max_model_len·served alias, 선택된 tokenizer/chat-template 파일 해시와 명시 옵션을 승인된 read-only 경로로 확인. 실제 template의 system/user 처리와 special-token 동작을 대조. 모델 파일/환경 전체 dump 없이 필요한 허용 필드만 읽음 | **BLOCKED: 실행 호스트·활성 serving artifact 접근 없음**. 저장소 값·과거 기록은 최신 native 근거가 아님 |
| P07 | 보존·비노출·실행 경계 | 공개 diff/요약에는 credential 값·token·비공개 질문/문서/생성 원문·전체 config/env가 없음. 조회 전후 head revision/target, 기존 실패 상태·키/receipt 참조가 동일하며 새 POST/추론·서비스 조작 0건임을 사용 경로/요청 근거로 확인. 조회 불가인 보존 항목은 미확인으로 남김 | 로컬 diff 확인 가능; 실제 서버 전후 대조는 **BLOCKED** |
| P08 | 문서 산출물 | 상대 링크와 코드 경로가 존재하고 Markdown 표/명령의 의미가 정확함. baseline·현재 조사·계획·fixture·native·품질 상태를 혼동하지 않음. `git diff --check` 이상 0건, 허용 diff만 존재 | 이번 문서 단위의 실행 검사; 결과는 총괄 기록에 연결 |

P05의 재구성 payload는 메모리 또는 제한된 임시 위치에서만 다룬다. 비교에는 실제 원문이 필요할 수 있으나 공개 로그/PR/결과 JSON에 본문을 출력하지 않는다. 해시와 길이는 전달 형태를 대조하는 보조 근거이며 의미 품질이나 실제 vLLM 수신을 증명하지 않는다. native 증거가 없으면 원인을 좁히는 다음 구현이나 prompt 수정에 착수하지 않는다.

## 후속 구현 전 재사용할 회귀 검사

이번에는 문서만 변경하므로 아래 제품 검사를 새로 실행한 것으로 기록하지 않는다. 후속 수정 경로와 진단 가설이 정해진 뒤 이 표의 해당 검사를 필수 목록으로 확정하고 reviewer의 사전 검토를 받는다. 기존 검사로 같은 범위를 검증할 수 있으면 새 검사를 만들지 않는다.

| ID | 상황·입력 | 기대 결과·확인 데이터·통과 기준 | 기존 검사/실행 시작점 |
|---|---|---|---|
| R01 | 정상 답변 `[S1]`, 중복 `[S1]`, 실제 선택 source 1개 | answered, 중복 없는 실제 source citation, 선택 chunk/source 보존, 모델 호출 1회. 인용 문법 통과와 정답 여부는 별도 | `src/platform/agent/runtime/tests/test_agents.py`: `test_answer_sources_steps_and_single_model_call`, `test_reference_diagnostic_grammar_matches_finalization` |
| R02 | 인용 없음/`[1]`/잘린 `[S1`, malformed `[S0]`·`[S01]`·비ASCII 숫자, 범위 밖 `[S2]`·매우 긴 숫자 | finalize에서 `ANSWER_REFERENCE_INVALID`; 진단의 missing/malformed/out_of_range 분류 유지, 실패를 성공으로 승격하지 않음. 자동 인용 추가·repair·추론 재시도 0건, 모델 호출 1회 | 같은 파일의 `test_unknown_and_missing_citations_fail_without_retry`, `test_reference_diagnostics_survive_finalize_failure_without_answer_text`, `test_reference_diagnostic_grammar_matches_finalization` |
| R03 | 빈 검색, 충분치 않은 source, `INSUFFICIENT_EVIDENCE`+stop, length 종료 | 빈 검색은 Model 조회/생성 없이 no_evidence. signal+stop은 no_evidence·빈 인용, length는 truncated 표시이며 정상 인수와 혼동하지 않음 | 같은 파일의 `test_empty_search_skips_all_model_lookups_and_generation`, `test_model_can_report_insufficient_evidence_without_fake_citations`, `test_truncated_answer_is_marked`; QA `test_empty_and_model_no_evidence_pass_without_invented_answer`, `test_model_no_evidence_is_verified_with_selected_sources_and_usage` |
| R04 | 올바른 `[S1]`이지만 invalid identity/usage/content, Model 불준비/legacy embedding, 초과 budget | 문법 진단 valid가 전체 성공이 되지 않음. 원래 오류·단계 유지, 잘못된 모델에는 chat 0회, payload/byte 한도 유지 | runtime의 `test_reference_diagnostic_cannot_promote_bad_response_or_usage`, `test_model_version_mismatch_prevents_chat`, `test_legacy_non_chat_repositories_cannot_resolve_or_generate`, `test_full_chunk_budget_and_original_order`, `test_prompt_and_byte_budget_are_bounded` |
| R05 | finalize 실패 후 저장 진단 조회, raw/private 필드 주입, 기존 receipt·누락 진단·cross-run/source 개수 불일치 | 기존 GET 2개만 사용, 필드 allowlist·bounded 수치 유지, 원문/credential 출력 0건. 누락은 not_recorded로 유지, 부정합 거부. receipt bytes 불변 | `python3 -B -m unittest discover -s src/platform/agent/tests -p test_answer_diagnostics.py`; 관련 runtime callback 검사는 `test_grpc_persists_diagnostic_before_error_and_retries_only_callback` |
| R06 | 실패 trial 재조회, 유실 응답, 같은 키 재시도, 변경 body/definition, terminal 실패 | 새 실행·새 키·새 plan 자동 생성 없음, 원래 intent/key/receipt·terminal 실패 보존. 허용 retry는 계약상 명시 동작만 수행 | `python3 -B -m unittest discover -s src/platform/agent/tests -p test_citation_trial.py`; 실행 도구의 `--execute`를 제품 인수 수단으로 재사용하지 않음 |
| R07 | adapter opt-in OFF, failed candidate, head revision 변경, 동일 질문 재조회/응답 유실 | OFF에서 원격 요청 0건, failed/unpublished 발행·활성화 거부, CAS 계약 유지. 이전 질문은 원래 head/target·실패·receipt 유지 | `python3 -B -m unittest src.apps.document_qa.tests.test_qa_agent src.apps.document_qa.tests.test_question_head`; 특히 terminal/opt-in/lost-response/failed-candidate/frozen-head 계열 |
| R08 | Model chat의 정상·오류 입력/신원·upstream 실패 | 전달 endpoint/alias·generation 값 유지, invalid 입력은 조회/추론 전에 거부, redirect/proxy 불사용, 원래 Model 오류 분류 유지 | `src/platform/model/gateway/app/chat_test.go`의 `TestChat*`; 기존 Model builder의 해당 gateway package에서 실행. 호스트에 go.mod 생성 금지 |

runtime 검사는 [runtime Dockerfile](../../src/platform/agent/runtime/Dockerfile)의 의존성과 generated protobuf가 준비된 기존 격리 builder의 `/build`에서 `PYTHONPATH=/build/generated:/build python -B -m unittest discover -s tests -p test_agents.py`로 실행한다. 현재 Dockerfile의 Python 3.12 lane과 사용자 Python 3.10 호환 인수를 구분한다. 원문 진단/조회 helper를 새로 작성할 경우는 **stdlib만 사용하며 Python 3.10 호환**을 필수로 둔다. 기존 runtime 자체의 langgraph/grpc 의존성을 stdlib로 간주하지 않는다. Python 3.10 실제 실행이 없으면 최신 Python의 AST 검사만으로 3.10 실행 통과라 표시하지 않는다. 현재 환경의 builder/runtime 준비 여부가 확인되지 않은 검사는 BLOCKED이며 SKIP을 통과로 합산하지 않는다.

## fixture·실제 전달·품질 인수의 구분

| 근거 | 증명할 수 있는 범위 | 증명하지 못하는 범위/남은 기준 |
|---|---|---|
| 정적 소스와 fixture | payload 구성·인용 grammar·오류 처리·비노출·보존 계약. R01~R08은 mock/loopback 또는 격리 저장소 범위를 각 결과에 표시 | 과거 누락 답변의 내용, 실제 활성 모델/template, 한국어 의미 품질 |
| P05/P06 read-only native 대조 | 현재 자산/배포/실행 신원과 추론 없는 payload/template 비교 | 실제 vLLM이 받은 request와 실제 생성 답변. POST를 하지 않았으므로 추론 품질 통과 아님 |
| 실제 Model payload 인수 | 후속 구현이 전달 경로에 영향을 줄 때 실제 Agent→Model→활성 serving 경로의 messages·source mapping·generation 값과 모델 신원을 제한된 관찰로 대조. 원문을 공개하지 않는 trace/계측 경로를 먼저 검토 | fixture HTTP body 검사만으로 대신하지 못함. 계측이 추가로 필요하면 별도 작은 변경·사전 계획·검토 필요. 현재 **BLOCKED** |
| 실제 F0a 품질 | 동일 고정 canonical/index/Tool·질문·모델 revision·prompt 해시·generation/검색 조건으로 코드·담당 부서·근거 없는 질문의 사실/출처를 평가 | answered나 `[S1]` 존재만으로 품질 통과가 아님. 기존 smoke는 코드/담당 부서/일반 품질을 대신하지 않음. 현재 **BLOCKED** |

후속 품질 평가의 최소 질문·의미 기준은 다음과 같다. 아래는 기존 공개 시험 질문과 합성 시험 질문 예시이며 사용자 비공개 원문이나 생성 답변 본문이 아니다. 이 표는 실제 호출 허가나 이번 단위 실행 목록이 아니다.

| 질문 | 채점 기준 |
|---|---|
| `연결 검증 코드는 무엇인가요?` | 관찰 JSON의 `expected_code`와 정확히 일치하는 코드, 해당 주장에 실제 선택 source의 유효 인용. 잘못된/누락된 코드·다른 source·무근거 설명·truncated는 실패 |
| `이 문서의 담당 부서는 어디인가요?` | `expected_department`와 의미상 같은 부서명, 실제 원문에 그 근거가 있고 해당 주장에 유효 인용. 부서 조작·틀린 source·불필요한 무근거 사실·truncated는 실패 |
| `이 문서의 승인 예산은 얼마인가요?` | 먼저 같은 canonical에 답이 없음을 평가자가 확인하고 고정. no_evidence·빈 인용, 금액 조작/아는 척 없음. 실제로 근거가 있으면 질문을 호출 전에 변경하고 기준 버전/사전 검토를 갱신 |

품질 비교를 시작하기 전에 reviewer가 고정 입력과 변경 한 곳, 반복 횟수·총 추론 상한·집계 방식을 새 계획 버전에 확정한다. 이번에는 원인 가설과 실행 경로가 미확정이라 호출 횟수를 승인하지 않는다. 모든 시도와 실패를 기록하며 성공한 결과만 선택하지 않는다. 예전 terminal Run을 재개하거나 실패 후보를 수정/발행하여 baseline을 만들지 않는다. 새로운 baseline/candidate 실험이 필요하면 각 실험의 고정 intent/key/receipt와 원래 자산과의 연결을 먼저 정한다.

## D38 핵심 검증력 후보

이번 문서 단위에는 실행 로직 변경이 없어 오류 주입은 적용 대상이 아니다. 후속 변경 전에 reviewer가 다음 후보 중 실제 영향받는 핵심만 선정한다. 원본/운영/배포 산출물이 아닌 격리 복사본에서 정상 검사 통과와 목표 오류에 의한 실패를 쌍으로 남긴다.

| 후보 로직 | 주입할 목표 오류 | 실패해야 하는 검사와 기대 근거 |
|---|---|---|
| 읽기 전용 진단/새 조회 helper | 허용 GET을 POST/실행 경로로 변경하거나 receipt를 재작성 | R05 및 새 helper의 요청 allowlist/파일 불변 검사. 예상치 못한 method/path 또는 before/after 차이가 원인이어야 하며 import/환경 오류는 인정하지 않음 |
| 진단 필드 allowlist·비노출 | `raw_answer`/private question/source/secret 필드를 결과에 복사 | R05의 private sentinel 비노출 검사가 실제 누출 문자열 때문에 실패. count/status 검사 실패만으로 대체하지 않음 |
| 인용 gate(해당 경로 변경 때만) | 누락 인용을 허용하거나 자동 `[S1]` 부착 | R02의 missing/invalid 입력이 거부되지 않아 실패. 정상 R01 통과와 함께 기록하고 문법/import 실패를 검증력 성공으로 세지 않음 |

## 기존 실패·회귀 범위와 결과 기록

현재 baseline의 직접 관련 실제 미해결 실패는 두 terminal `MISSING_REFERENCE`다. 이 계획은 이를 고치거나 성공으로 재분류하지 않는다. [구조 검사 근거](../testing/source-layout-native-20261013.json)의 QA fixture 581개 성공은 그 고정 소스/환경 범위로 재사용한다. 기존 Ops adapter 실패 1개와 native skip 15개, observability native skip 4개는 별도 영역의 과거 기록이며 QA 필수 검사 실패를 면제하는 목록이 아니다. Node12의 화면 검사 skip은 JS 인수 통과가 아니다.

이번 문서 변경에는 문서 경로·링크·diff 검사만 실행한다. DB/queue/schema/화면·재시도·성능 코드를 바꾸지 않아 전체 581개·Ops·GPU·브라우저·부하/장시간 검사를 반복하지 않는다. 실제 변경에 새 영향이 생기면 그 범위의 필수 검사를 추가하고 이유를 기록한다. 미확인 native/품질은 적용 대상 없음이 아니라 BLOCKED다.

실패 횟수/시간은 D17~D18의 같은 문제 누적 기록을 이어받는다. 과거 prompt-only 실패를 새로운 성공 기회로 지우지 않고, 과거 자동 수정 누적 횟수·활성 분석 시간이 기록되지 않은 경우 미확인으로 남긴다. 기존 실패를 고치는 한도를 임의로 초기화하거나 이번 문서 작업으로 제품 실패를 해소했다고 세지 않는다.

실행 근거에는 계획 버전·소스/입력 hash·환경·명령·검사 수·PASS/FAIL/SKIP/BLOCKED·확인 데이터·실제 연동/추론 여부를 남긴다. 새 요약은 [보관 정책](../testing/artifact-retention.md)을 따르며 과거 관찰 JSON을 덮어쓰지 않는다. reviewer는 이 계획의 정확한 버전/SHA-256과 사전 판정, 후속 구현이 있으면 그 diff와 최종 검사 근거를 별도로 기록한다. 필수 native/품질 인수가 BLOCKED인 동안 F0a 사용 가능·완료·병합 가능으로 판정하지 않는다.

## 정의조회 도구 사전계획 v1

2026-10-14 추가. 기준 부모 `55c714728806443463e8bb946c2621407071d4f6`, branch `codex/f0a-definition-probe-20261014`. 새 격리 clone의 시작 clean만 확인했으며 사용자 서버 working tree는 미조회다. 위 기준을 변경하지 않고 다음 작은 구현 단위의 범위와 검사를 구체화한다. **이 추가 계획의 reviewer 사전 PASS 전에는 제품/검사 코드를 작성하지 않는다.**

root의 구현 경로는 `src/apps/document_qa/definition_probe.py`, test_agent는 `src/apps/document_qa/tests/test_definition_probe.py`다. 기존 settings·pure 정의 검증·AgentQAGateway를 재사용한다. 공유 transport/runtime/prompt/model/search·DB/서비스/head·기존 Run/plan/key/receipt는 변경하지 않는다. 예정 API는 `ProbeError(code)`, `load_baseline(path)`, `probe(reader, baseline)`, `FixedReader(row, allowed_paths)`, `main(argv=None)`이며 구현 전 검토에서 구체화한다.

| 계약 | 고정 조건 |
|---|---|
| 설정/입력 | 기존 settings 파일 필수. 없으면 `load_settings` 호출·Ops fallback·네트워크 모두 0건. `read_file` 보안 기준 재사용 후 `load_settings(settings, settings)`로 다른 config fallback 차단. baseline의 기존 두 Run/Version ID와 `models.chat`의 deployment/model_version pin을 내부 검증; 실제 ID 일반 코드 하드코딩 금지 |
| 읽기 범위 | `FixedReader`가 baseline의 Run GET 2개+Version GET 2개만 exact allowlist로 강제. broad AgentQAGateway가 허용하는 parent Agent GET/inspect POST도 금지. body/key/query·steps/목록/resolve·다른 플랫폼/Docker·POST·추론·발행·head·retry/redirect/환경 proxy 사용 없음 |
| 한도/보존 | total60초·per-request최대8초·최대4 HTTP 시도, monotonic clock. 실패 즉시 중단·재시도 없음. 기존 2MiB/strict JSON 한도 재사용. init/mkdir/chmod/DB/new key·state/receipt 파일 쓰기 없음; 설정/token/baseline/전체 fixture와 파일 mode/목록 불변 |
| 신원/정의 | 응답 ID와 Run↔Version pin·유효 digest 일치, Run failed+result없음+error code=ANSWER_REFERENCE_INVALID/stage=finalize. original published/candidate draft, 같은 agent·model/tool/template·입력(question/filters), baseline chat pin 비교. pure `definition_checked` 재사용, input/output schema의 필수 shape·두 Version schema 동일성 검사 유지. 서비스 hash는 schema를 포함한 Go hash이므로 재계산했다고 주장하지 않음 |
| 차이/출력 | prompt-only 여부를 판정하고 다른 정의 차이는 BLOCKED. 공개 출력은 고정 label/code·boolean/count·generation/budget·prompt UTF8 bytes/digest·허용 차이필드명/boolean만. UUID/URL/path/token/credential/invocation_key/definition·input hash·비공개 원문·생성본문/예외문구/미지 필드 출력 금지. source hash는 agents.py/chat.go/qa_agent_transport.py 3개만 repository snapshot으로 명명 |
| 한계/CLI | steps 미조회이므로 live MISSING_REFERENCE 재검증 아님(code/stage만 대조). active container/template·실행 Model payload·head/DB·추론·품질 미검증 표시. CLI는 --project-root(기본cwd), --baseline(project-root 상대 기본 관찰 JSON), --settings(기존 표준 state) 지원. absolute import로 저장소 설치 없이 별도 임시 파일 실행 가능. 실패는 고정 허용 code의 JSON stderr+nonzero, traceback/원격 body/부분 성공 없음 |

필수 검사는 아래처럼 기대 데이터와 통과 기준을 연결한다. fixture는 합성 ID·비공개 sentinel만 사용하며 별도 두 ID 세트로 실제 ID 하드코딩을 잡는다. 기존 전체 QA/runtime·Ops/GPU/브라우저 검사는 공유 코드가 그대로면 반복하지 않는다. 한 번 확인한 정상 결과는 새 변경/실패 없이 반복하지 않는다.

| ID | 상황/입력 | 기대 결과와 근거 | 환경 |
|---|---|---|---|
| DP01 | 정상 settings/baseline·두 Run/Version, prompt만 다름 | exact4 GET·body/key 없음, state/pin/입력 동일·차이 prompt-only, 정확한 generation/byte/digest, sourcehash3개와 unknown flag. fixture deep-copy 불변 | stdlib fixture; 필수 |
| DP02 | missing/invalid/duplicate baseline ID·다른 model pin·잘못된 JSON/shape | preflight 오류는 network0/new ID0, 응답 불일치 즉시 종료·추가 조회0. 실제 코드에 실제 IDs 없음 | fixture; 필수 |
| DP03 | missing settings+유효 Ops config, symlink/oversized/invalid settings·disabled Agent·missing/invalid credential | Ops/다른 config 읽기·fallback0, missing settings에서 load_settings0/network0. 기존 파일 bytes/mode/목록 불변, empty HOME에서도 새 state/DB/key 파일0 | tempfile·read_file·patch; 필수 |
| DP04 | Run/Version ID·hash·state·agent·입력/filters·code/stage/result·chat/model/tool pin 불일치 | 불일치 거부·4GET 이하·원본 실패/객체 불변. 동일한 임의 valid Run/Version digest는 수용하여 Python hash 재계산을 요구하지 않음 | fixture subtests; 필수 |
| DP05 | generation/budget/template 차이·prompt 동일, 누락/잘못된 정의필드·boolean정수·NaN·invalid Unicode, malformed/missing input/output schema·두 schema 차이 | 유효 비prompt 차이는 BLOCKED 고정 code; 차이필드명/boolean은 허용 출력 상한이며 실패 출력의 필수가 아님. malformed/schema 부정합도 고정 오류. flattened API 미지 metadata는 무시하되 출력 금지. 다른 후보/모델/키 생성0, 성공/품질 주장0 | fixture subtests·pure validator; 필수 |
| DP06 | prompt/question/filter/error/미지필드/URL/path/token/UUID/invocation_key/금지hash에 고유 sentinel | 성공·BLOCKED·오류와 CLI stdout/stderr 모두 allowlist만, private/금지 digest/예외텍스트0. prompt bytes/digest만 정확하고 active/native/semantic 미인수 유지 | fixture+CLI capture; 필수·M2 |
| DP07 | parent Agent GET/inspect POST/steps/query/body/key·다른 ID·다섯 번째 시도 | underlying transport 위임 전에 거부, HTTP 추가0. generic gateway 허용 경로도 거부되는 실제 wrapper 경계 확인 | adapter fixture; 필수·M1 |
| DP08 | deadline 이전/동일/이후·남은8초 미만·중간 HTTP/timeout 실패 | budget60/timeout8 전달, opener timeout≤min(8,remaining), expired network0·오류 후 retry0·시도≤4. 긴 sleep 없이 주입 clock 사용 | fake clock/opener; 필수 |
| DP09 | 정상 loopback HTTP+별도 CLI 프로세스, 임시 standalone helper 실행 | 실제 GET4/POST0·올바른 임시 bearer·body/key없음, valid JSON·비노출·파일불변. --project-root/상대baseline/--settings 및 PYTHONPATH로 설치 없이 실행 | stdlib HTTP/CLI; 필수·native 아님 |
| DP10 | loopback redirect→trap, 401/403/404/500·malformed/duplicate JSON·잘못된 content-type·2MiB 초과·timeout | redirect/proxy trap0·추가시도0, 원격 body/예외 private 출력0, 파일불변. timeout/size는 bounded fixture 병용 | HTTP+fixture; 필수 |
| DP11 | stdlib/Python3.10·import side effect | 외부 설치0·새 import가 state/서비스 읽기/쓰기0. AST3.10 검사와 실제 이용 가능한 Python 실행을 구분; 실제3.10 없으면 해당 실행 BLOCKED | 소스+실행환경; 필수 |
| DP12 | 실제 서버의 기존 settings와 native4GET | latest 정의/실패 pin의 허용 요약만 인수. fixture/loopback으로 대신하지 않음; payload/template/head/품질은 별도 | **현재 BLOCKED: native 접근 없음** |

실행 시작점은 `python3 -B -m unittest src.apps.document_qa.tests.test_definition_probe`. 실제 버전·source/plan hash·명령·검사수/PASS/FAIL/SKIP/BLOCKED·네트워크/쓰기 범위를 소형 근거에 남긴다. source/layout·문서 링크·`git diff --check`도 확인한다. 공유 코드 수정이 필요하면 먼저 계획/관련 회귀를 추가하고 다시 사전 검토한다.

D38 핵심은 reviewer가 아래 2개를 선정한다. 정상 검사 통과와 격리 복사본의 목표 오류에 의한 검사 실패를 쌍으로 기록한다. syntax/import/환경 실패는 검증력 성공이 아니며 변이 코드는 원본/PR/운영 산출물에 남기지 않는다.

| 변이 | 주입 오류 | 실패해야 하는 검사/근거 |
|---|---|---|
| M1 | exact GET allowlist를 우회해 generic gateway가 허용하는 parent Agent GET 통과 | DP07이 실제 예상 밖 transport 위임 때문에 실패. generic gateway가 자체 거부하는 경로만 쓰지 않음 |
| M2 | 요약에 raw prompt/remote private 필드 복사 | DP06이 private sentinel 노출/허용 key 위반 때문에 실패 |

사전 검토는 이 추가 계획의 정확한 hash에, 최종 독립 검토는 실제 helper/test diff·실행/변이 근거에 각각 연결한다. 통과해도 **read-only 도구의 격리 검증 완료**이며 native4GET·active runtime/template/Model payload·F0a 품질/세 고정 질문은 계속 미인수다. D17~D18의 기존 누적 미확인·시간/시도 기록을 그대로 이어받고 새 branch로 초기화하지 않는다.
