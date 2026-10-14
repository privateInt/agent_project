# agent_project Codex 문서 안내

공통 규칙·구성표·템플릿을 포함하는 준비 단계다. 기능 개발·무인 실행·자동화 활성화는 아직 시작하지 않았다. 실제 실행 설정은 검증 후 적용한다.

| 읽을 문서 | 책임 |
|---|---|
| [공통 규칙](common/RULES.md) · [결정 기록](common/DECISIONS.md) | 모든 프로젝트에 적용하는 확정 정책과 이유 |
| [테스트 기준](common/TESTING.md) | 요구사항 기반 검사·사용 가능 판정·기준 변경 권한 |
| [에이전트 구성표](common/AGENT_ROSTER.md) | 역할·모델·협업 경계 |
| [프로젝트 세부 규칙](project.md) | 이 프로젝트의 경로·보호 대상·기존 계약과 검사 연결 |
| [준비와 첫 작업](work-plan.md) | 실행 전 조건과 기존 backlog의 첫 기능 작업 |
| [실행 환경 준비](common/EXECUTION_SETUP.md) | 서버 CLI·계정·권한·크레딧 미사용 확인 순서 |
| [조회 도구](common/READINESS.md) · [병합 명세](common/GITHUB_GATES.md) | 모델 호출 없는 점검과 독립 reviewer 필수 검사 연결 |
| [비활성 설정 적용](config-preparation.md) | 기존 설정을 보존하는 CAS batch와 관련 검사·native 미인수 범위 |
| [사전 검사 계획](common/PREPARATION_TEST_PLAN.md) | 이번 도구 구현 범위와 후속 native·관리 인수의 경계 |
| [프로젝트 템플릿](templates/project-rules.template.md) · [작업 템플릿](templates/task.template.md) | 새 프로젝트와 작은 작업을 기록할 틀 |
| [공통 배포판](common/release.json) | 두 저장소에 포함하는 동일 본문·버전의 파일 해시 |

설정 후보는 [config.toml.template](templates/config.toml.template)과 `templates/agents`에 둔다. 실제 `.codex` 설정으로 설치하거나 실행하지 않았다. 공통 배포 파일은 저장소별 독립 변형을 만들지 않는다. 현재 상태는 프로젝트 세부 규칙에서 연결한 상태 문서 한 곳에 기록하고 PR에는 작업 상세·검토·근거를 남긴다.
