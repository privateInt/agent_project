# Agent 프로젝트 세부 규칙

공통 배포판: [1.1-docs](common/release.json). 공통 정책은 [RULES.md](common/RULES.md), 검사 기준은 [TESTING.md](common/TESTING.md)를 참조한다.

## 목표와 기준 위치

최종 목표는 문서 업로드부터 질문·실제 근거가 있는 답변까지 연결하고 Data·Knowledge·Model·Agent·Ops의 책임을 유지하는 제품이다. 현재 소스 구조는 [구조 문서](../architecture/source-layout.md), 실제 인수와 실패는 [상태 문서](../status.md), 작업 순서·완료선은 [확장 계획](../plans/platform-expansion-work-plan-20261012.md), 재개 입력은 [인수인계](../HANDOFF_NEXT_SESSION.md)가 기준이다.

준비 기준 GitHub main은 `6dd46d069079fc20df5b3d4bac71fe3450550bf8`이다. 이 값은 이번 문서 준비의 입력이며 다음 작업의 실제 HEAD를 고정하는 값이 아니다. 구조 반영 후 첫 기능 작업은 F0a 인용 누락 해결이다. 기능 개발과 무인 실행은 아직 시작하지 않았다.

## 역할별 변경 경계

아래는 담당 후보 경로다. 작업별로 파일 목록을 더 좁히고 공유 파일은 순서대로 수정한다. 역할 설명은 실제 접근 제한을 강제하지 않는다.

| 역할 | 프로젝트 경로·책임 | 함께 확인할 경계 |
|---|---|---|
| front_agent | `src/apps/document_qa/web`, 각 console의 화면 파일, `src/ops/platform/web` | console에는 서버 코드도 있으므로 전체 앱 경로를 front 소유로 두지 않음 |
| backend_agent | `src/platform/{data,knowledge,model,agent}`, `src/apps`의 서버 로직, `src/ops/platform`의 API | Data 원본/canonical, Knowledge 색인/검색, Model 학습/추론, Agent Tool/Run의 소유권 유지. Dataset은 F1 이전까지 Model 소유 |
| infra_agent | `src/infra`, `src/ops/tools`, 담당 플랫폼의 Docker/Compose/빌드 구성 | `src/ops/candidates`는 보류 이력이며 운영 후보를 자동 활성화하지 않음 |
| code_module_agent | 먼저 선정한 도메인 독립 helper·SDK·계약 파일 | 플랫폼 internal 전체를 공용 모듈로 취급하지 않음. 계약 원본은 소유 플랫폼에 유지 |
| ui_design_agent | 화면 흐름과 상태 설계 문서 | 데이터·오류 의미는 backend 계약, 구현은 front와 연결 |
| test_agent | 변경 대상 옆의 tests·Go/CJS 검사와 인수 기준 | fixture, 실제 연동, 브라우저, LLM 품질 근거를 구분 |
| reviewer | 이번 diff·검사 계획·실행 증거의 읽기와 독립 판정 | 자체 구현의 최종 승인 금지 |

## 보존과 프로젝트 제한

- 실제 운영 DB·키·intent/request key·receipt·Flow/job·Agent/Tool/index 버전·head revision1·원래 질문과 terminal 실패 이력을 보존한다. 상세 대상은 인수인계에서 읽는다.
- 인용 gate 완화, 답변 뒤 자동 `[S1]` 부착, 기대 정답을 모델 prompt에 주입, 숨은 fallback으로 성공을 만들지 않는다.
- Ops 운영 백업/복원·추가 운영 자동화·실제 재부팅·Kubernetes·과거 후보 hash 재결합은 기존 보류를 유지한다. 공통 자동 진행 정책이 이번 준비 단계에서 이 보류를 해제하지 않는다.
- Python 3.10 호환성과 지원 Node 기능을 확인한다. 과거 GPU 및 서버 인수는 현재 실행 환경의 검증을 대신하지 않는다.
- 새 `.patch-backups`나 작업마다 전체 압축 백업을 만들지 않는다. 입력·인증 저장소·현재 증거를 확인 없이 정리하지 않는다.

## 검사 연결

| 변경 범위 | 검사 시작점 | 판정 한계 |
|---|---|---|
| 문서 준비 | 링크·공통 파일 일치·manifest·diff 및 소스 불변 확인 | 제품 기능 인수가 아님 |
| 소스 구조 | `python3 -B -m src.ops.tools.check_source_layout` | 운영 root/hash 재결합을 수행하지 않는 구조 검사 |
| F0a QA | `python3 -B -m unittest src.apps.document_qa.tests.test_qa_agent` 및 `src/platform/agent/runtime/tests/test_agents.py`의 관련 검사 | 변경 영향을 본 뒤 test_agent가 실행 환경과 추가 회귀를 계획하고 reviewer가 검토. fixture만으로 실제 답변 품질 통과 아님 |
| LLM/질문 흐름 | 고정 문서·질문·기대 근거, 실제 모델/검색 조건, 해당 화면 E2E | 코드/담당 부서/근거 없는 질문의 기대 결과는 기존 확장 계획 기준. 기준 완화는 사용자 결정 |

관련 runtime 의존성·실제 API·GPU·브라우저 환경을 확인하기 전에 필수 검사를 통과했다고 표시하지 않는다. 이전 QA 581개 등 유효한 성공 증거는 관련 변경이 없으면 반복하지 않는다.

## 실행 준비 상태

서버의 개발 worktree·branch·HEAD·실행 계정·운영 원본 경로는 비공개 실행 기록과 실제 서버 조회로 확인한다. 공개 규칙에 계정별 절대 경로를 배포하지 않는다. 개발 worktree와 운영 원본은 구분하며 현재 일치 여부를 확인하기 전 원본에 문서나 코드를 적용하지 않는다. 실제 환경 확인 절차는 [실행 준비](common/EXECUTION_SETUP.md)를 따른다.

현재 상태 보고 위치는 [docs/status.md](../status.md), 작업 상세·독립 검토는 작업 PR에 둔다. 첫 작업과 준비 목록은 [work-plan.md](work-plan.md), 새 작업 기록의 틀은 [task.template.md](templates/task.template.md)를 사용한다.
