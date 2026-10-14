# <PROJECT_NAME> 작업 규칙

상태: DRAFT / 프로젝트 AGENTS.md로 정리하기 위한 틀
프로젝트 규칙 버전: <VERSION>
공통 규칙·구성표: <COMMON_RULES_AND_ROSTER_PATHS_AND_VERSIONS>

## 목표와 현재 단계

- 최종 제품 목표: <PRODUCT_GOAL>
- 이번 단계 목표·완료 조건: <CURRENT_UNIT_AND_ACCEPTANCE>
- 첫 작업: <NEXT_TASK_ID>
- 현재 운영 상태와 미완료 요구: <OBSERVED_STATE_AND_GAPS>
- 기능 개발 시작 상태: <PREPARATION / DEVELOPMENT>
- 시작 전 읽을 문서: <HANDOFF_STATUS_TASKS_AND_CONTRACTS>

## 저장소와 환경

공통 실행 구성은 D22를 따른다. 프로젝트별 서버·실행 계정·작업 경로·인증 참조·권한과 실제 확인 상태를 아래에 기록한다. 준비 상태와 실제 실행/활성화 상태를 구분한다.

| 항목 | 값 |
|---|---|
| 저장소 URL·기준 branch/commit | <REPOSITORY_AND_BASE> |
| 개발 worktree·branch | <DEVELOPMENT_WORKTREE> |
| 보호할 원본/운영 경로 | <PROTECTED_ROOTS> |
| 실행 계정·권한 | <EXECUTION_IDENTITY> |
| 언어/runtime·지원 버전 | <RUNTIMES> |
| 기존 환경/설정 참조 | <CONFIG_REFERENCES_WITHOUT_SECRET_VALUES> |
| 공개 반영·운영 배포 범위 | <PUBLISH_AND_DEPLOY_SCOPE> |

## 구조·도메인·계약

| 영역 | 담당 경로 | 소유 데이터/책임 | 외부 연결 계약 |
|---|---|---|---|
| <DOMAIN> | <PATH> | <OWNERSHIP> | <API_OR_INTERFACE> |

- 보존할 API/DB/schema/버전/요청 key/receipt: <IMMUTABLE_OR_COMPATIBILITY_CONTRACTS>
- 보류한 운영 작업과 범위: <HELD_OPERATIONS>
- 프로젝트별 추가 규칙·공통 기본값과의 차이: <PROJECT_SPECIFIC_RULES>

## 검사 절차

기준 구성은 D27, 공통 검사 범주는 ../common/TESTING.md의 D28 절을 따른다. 공통 기준은 복사하지 않고 참조하며, 프로젝트별 실행 명령·환경과 고유한 추가 기준을 연결한다. 아래는 구체적인 검사 목록을 확정하기 위한 틀이다.

- 공통 검사 기준 문서·버전: <COMMON_TEST_STANDARD_REFERENCE>
- 프로젝트별 추가 기준 문서·버전: <PROJECT_TEST_STANDARD_REFERENCE>
- 실제 연동 검사 대상·환경·보호 경계: <REAL_INTEGRATION_ENVIRONMENT_AND_BOUNDARIES> (D30 참조)
- 핵심 사용자 흐름·브라우저 E2E 환경·데이터 분리 기준: <BROWSER_E2E_FLOWS_ENVIRONMENT_AND_ISOLATION> (D31 참조)
- 비동기 작업의 복구·재시도·중복 처리 계약과 검사 환경: <ASYNC_RECOVERY_RETRY_DEDUP_CONTRACTS_AND_TEST_ENVIRONMENT> (D32 참조)
- 데이터 변경·복원 검사의 선정 기준과 기존 복구 수단: <DATA_VALIDATION_SELECTION_AND_RECOVERY_REFERENCES> (D33~D34 참조)
- 권한·데이터 격리·도구 실행 정책과 검사 환경: <ACCESS_ISOLATION_TOOL_POLICIES_AND_TEST_ENVIRONMENT> (D35 참조)
- LLM·RAG 평가 데이터·지표·기준·반복 방식의 참조: <LLM_RAG_EVALUATION_DATA_METRICS_CRITERIA_AND_REPETITION> (D36 참조)
- 성능 요구사항·검사 선정 기준·시간과 자원 범위: <PERFORMANCE_REQUIREMENTS_SELECTION_TIME_AND_RESOURCE_SCOPE> (D37 참조)
- 핵심 테스트 검증력의 선정 기준과 격리 환경: <CRITICAL_TEST_EFFECTIVENESS_SELECTION_AND_ISOLATION> (D38 참조)

작업별 검사 계획은 D29에 따라 구현 전에 독립 검토한다. 구체적인 작업 계획과 검토 근거는 해당 작업 기록을 참조하며 여기에 복사하지 않는다.

| 검사 | 실행 명령·환경 | 확인할 동작 | 필수 여부 | 필요한 실제 인수 |
|---|---|---|---|---|
| <CHECK> | <COMMAND_AND_ENVIRONMENT> | <BEHAVIOR> | <REQUIRED_OR_OPTIONAL> | <LIVE_ACCEPTANCE> |

- 기준선 실패·skip과 소스/환경 근거: <BASELINE_EVIDENCE>
- 운영 state를 읽거나 바꾸는 검사의 구분: <STATE_BOUNDARIES>
- 기능별 의미/품질 기준: <QUALITY_EXPECTATIONS>

## 작업과 기록

문서 배치는 공통 규칙의 D19를 따른다. 현재 개발 문서는 docs/에서 필요한 하위 디렉터리로 관리하고, 사용하지 않는 과거 일지는 history_docs/로 보관한다. 루트 README는 프로젝트 진입 안내로 별도 관리한다. 아래 항목은 기존 기준 문서의 실제 위치를 연결하며 같은 내용을 다시 작성하지 않는다.

- 작업 목록: <TASKS_DOCUMENT>
- 작업별 PR·검토·사용자 판단 대기 위치: <TASK_PR_REFERENCES> (D26 참조)
- 현재 상태: <STATUS_DOCUMENT>
- 결정과 계약 변경: <DECISIONS_DOCUMENT>
- 검사 결과·보관할 입력: <EVIDENCE_AND_RETENTION>
- 다음 세션 인수인계: <HANDOFF_DOCUMENT>

소스 구현·검사·GitHub 반영·운영 배포의 완료 상태를 따로 기록한다. 비밀 값은 참조 경로만 기록한다.
