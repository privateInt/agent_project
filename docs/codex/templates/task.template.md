# <TASK_ID>: <TITLE>

상태: <대기 / 진행 / 검증 중 / 완료 / 보류>
기준 commit·규칙 버전: <BASE_AND_RULESET_PINS>
선행 작업: <DEPENDENCIES>
구현 / 검증 / 통합 담당: <OWNERS>
작업별 PR·검토·사용자 판단 대기 위치: <TASK_PR_REFERENCE> (D26 참조)

## 한 작업의 범위

- 사용자가 얻는 결과: <USER_VISIBLE_OUTCOME>
- 변경할 코드/계약/문서: <ALLOWED_CHANGES>
- 이번에 다루지 않는 항목: <EXCLUSIONS>
- 보호할 원본·운영 상태·ID/키·실패 이력: <PROTECTED_ASSETS>
- 현재 허용된 실행·반영 범위: <AUTHORIZATION_SCOPE>
- 작업 시간/사용량 한도와 중간 보고 지점: <LIMITS_AND_CHECKPOINTS>

## 완료 조건

- [ ] <OBSERVABLE_BEHAVIOR_1>
- [ ] <COMPATIBILITY_OR_PRESERVATION_2>
- [ ] <REQUIRED_VALIDATION_3>
- [ ] <DOCUMENTATION_AND_HANDOFF_4>

## 검증

기준 구성은 D27, 공통 검사 범주는 D28을 따른다. 해당 작업에 적용하는 공통·프로젝트별 기준을 참조하고, 변경 영역에 필요한 추가 검사를 연결한다. 검사 목록과 구체적인 통과 기준은 확정된 기준 문서를 따른다.

아래는 검사 계획의 참조 항목이다. 해당 작업에 관련된 항목을 연결하고 적용 대상이 없는 범주는 D28에 따라 이유를 기록한다. 같은 계획이나 근거를 여러 번 작성하지 않는다.

- 적용할 공통·프로젝트별 검사 기준과 버전: <TEST_STANDARD_REFERENCES>
- 변경 영향과 추가 필수 검사의 연결: <CHANGE_IMPACT_AND_REQUIRED_CHECK_REFERENCES>
- 실제 연동 대상·환경 참조·확인할 범위: <REAL_INTEGRATION_CHECK_SCOPE_AND_ENVIRONMENT> (D30 참조)
- 핵심 흐름에 대한 영향·브라우저 E2E 계획 참조: <CORE_FLOW_IMPACT_AND_BROWSER_E2E_PLAN> (D31 참조)
- 비동기 작업에 대한 영향·복구·중복 처리 검사 계획 참조: <ASYNC_IMPACT_AND_RECOVERY_DEDUP_TEST_PLAN> (D32 참조)
- 데이터 변경·복원 검사의 선정·생략 이유와 reviewer 근거: <DATA_VALIDATION_SELECTION_AND_REVIEW_EVIDENCE> (D33 참조)
- 기존 복구 수단·새 백업 필요성·최소 범위와 소요 시간: <RECOVERY_REUSE_AND_BACKUP_NEED_SCOPE_TIME> (D34 참조)
- 권한 정책에 대한 영향·허용·거부·격리 검사 계획 참조: <ACCESS_CONTROL_IMPACT_AND_TEST_PLAN> (D35 참조)
- LLM·RAG 결과에 대한 영향·고정 평가와 이전 버전 비교 계획 참조: <LLM_RAG_IMPACT_EVALUATION_AND_BASELINE_COMPARISON_PLAN> (D36 참조)
- 성능·장시간 검사의 선정·생략 근거와 대상·부하·시간: <PERFORMANCE_TEST_SELECTION_SCOPE_LOAD_AND_DURATION> (D37 참조)
- 핵심 로직·주입할 오류·실패해야 하는 검사와 검증 근거: <CRITICAL_LOGIC_FAULT_EXPECTED_FAILURE_AND_EVIDENCE> (D38 참조)

### 검사 계획

D29에 따라 구현 전에 이 계획을 작성하고 독립 reviewer가 확인한다. 실제 테스트 코드는 구현과 병행해 작성할 수 있다. 세부 절차와 기준 변경 권한은 ../common/TESTING.md의 D29 절을 참조한다.

- 사전 검토한 계획 버전·근거: <REVIEWED_PLAN_VERSION_AND_EVIDENCE>
- 독립 reviewer·검토 결과·확인 시점: <PLAN_REVIEWER_RESULT_AND_TIME>

| 검사/인수 | 요구사항 참조 | 상황·입력 | 기대 결과·확인할 데이터 | 필수 여부·통과 기준 |
|---|---|---|---|---|
| <CHECK> | <REQUIREMENT_REFERENCE> | <SCENARIO_AND_INPUT> | <EXPECTED_OUTCOME_AND_STATE> | <REQUIRED_STATUS_AND_PASS_CRITERIA> |

### 실행 결과

| 검사/인수 | 검사 계획·기준 버전 참조 | 실제 실행 결과 | 소스/환경·근거 | 한계/skip |
|---|---|---|---|---|
| <CHECK> | <PLAN_AND_CRITERIA_VERSION_REFERENCE> | <RESULT> | <EVIDENCE> | <LIMITATION> |

## 현재 결과

- 구현 변경과 이유: <IMPLEMENTATION>
- 검증 결과·새 회귀·기준선 실패: <VALIDATION_SUMMARY>
- 로컬 commit / 공개 반영 / 운영 배포 상태: <DELIVERY_STATES>
- 실제 인수·품질의 남은 항목: <REMAINING_ACCEPTANCE>

## 끊고 재개할 때

- 마지막 확인한 commit·환경: <CHECKPOINT>
- 같은 미해결 문제의 누적 수정 횟수·사용 시간: <FAILURE_ATTEMPTS_AND_ELAPSED_TIME> (D17~D18 참조)
- 사용자 결정 대기 항목과 의존 작업: <PENDING_DECISIONS_AND_DEPENDENCIES> (D06~D08 참조)
- 남겨야 할 입력·환경·인증 참조: <REQUIRED_INPUTS_WITHOUT_SECRET_VALUES>
- 그대로 재사용할 성공 근거: <REUSABLE_EVIDENCE>
- 다음 작은 실행/조회: <NEXT_ACTION>
- 실패한 조건·필요한 추가 입력: <BLOCKERS>
