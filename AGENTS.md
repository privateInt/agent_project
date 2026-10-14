# Agent Project 작업 규칙

## 공통 규칙과 프로젝트 진입점

[Codex 문서](docs/codex/README.md)에서 공통 배포판·테스트 기준·에이전트 구성·프로젝트 규칙을 읽는다. 확정 공통 정책은 동일 버전으로 모든 프로젝트에 포함한다. 기존 플랫폼/운영 보류와 보호 대상은 유지한다. 실행 설정 미확인 상태에서 기능 개발이나 무인 실행을 시작하지 않는다.

## 시작점과 현재 우선순위

먼저 `docs/HANDOFF_NEXT_SESSION.md`, `docs/status.md`, `docs/roadmap.md`를 읽고 실제 소스/runtime을 조회로 대조한다. 확장 순서·작은 단위·완료선은 `docs/plans/platform-expansion-work-plan-20261012.md`를 따른다. src 구조·Ops 통합의 로컬 적용과 검증은 완료됐으며 [구조 안내](docs/architecture/source-layout.md)와 `docs/testing/source-layout-native-20261013.json`을 따른다. 이 구조의 GitHub main 반영은 `6dd46d069079fc20df5b3d4bac71fe3450550bf8`에서 확인했다. 현재 단계는 공통 규칙·프로젝트 문서와 실행 환경 준비다. 2026-10-13 사용자 추가 지시에 따라, 공통 규칙·에이전트 구성·프로젝트별 세부 규칙과 템플릿은 문서로 정리했다. 현재는 실제 서버 실행 구성을 확인하는 준비 단계이며 본격적인 Codex 기능 개발 시작 전에 사용자에게 알린다. 이 준비를 마치기 전에는 F0a/H01 등의 기능 개발이나 무인 실행을 시작하지 않는다. 준비 후 첫 Agent 기능 작업은 F0a: 문서 QA의 `MISSING_REFERENCE` 해결이다. 과거 부팅 후보/승인 hash 재결합은 보류이며 이 구조 작업 때문에 자동 활성화하지 않는다. 최초 smoke 성공을 일반 질문 품질 통과로 해석하지 않는다.

사용자가 연속 작업을 요청하면 의존성이 만족된 첫 미완료 단위를 코드→관련 검사→필요한 실제 인수→문서 기록까지 끝내고 다음 단위로 진행한다. 매번 계속 여부를 묻지 않는다. 한 번에 여러 플랫폼을 대규모로 바꾸지 않는다. 진행 중 범위/판단/남은 검증을 한국어로 간결하게 보고한다. 실행 지침만으로 상시 실행 worker가 설치됐다고 말하지 않는다.

## 플랫폼 책임과 제품 기준

- Data: 원본·canonical·문서/template/artifact와 향후 Dataset/Version·가공/일정. 현재 Dataset은 Model에 있으며 F1 점진 이전 전까지 기존 계약을 유지한다.
- Model: 모델/버전·학습·checkpoint·평가 실행·배포·추론과 향후 external provider. Knowledge: 청킹·색인/검색·그래프와 검색 평가. Agent: Connection·Tool/MCP·AgentVersion/Run·세션/기억. Ops는 공식 API/관측 조회, infra는 공용 서비스 운영이다.
- 타 플랫폼 DB/객체를 직접 수정하지 않고 공식 API를 사용한다. 자동화·적응성 기준은 `docs/design-principles.md`다. 기존 설정/credential 참조를 재사용하고 주소·계정·job/모델 ID를 일반 기능에 하드코딩하지 않는다.
- n8n/Langfuse를 새 설치로 취급하지 않는다. Airflow는 Data 정기 배치, n8n은 상위 업무 Flow다. LightRAG/Neo4j는 평가 실험, LiteLLM은 Model 추론 adapter/경로이며 도메인 소유권을 대체하지 않는다.

## 보존과 실행

- 사용자 미커밋 변경을 먼저 확인하고 덮어쓰지 않는다. 실제 Git/remote/branch/runner를 조사해 작업을 격리하며, 스냅샷에 Git이 없다는 이유로 사용자의 서버도 없다고 단정하지 않는다.
- 문서 QA DB·API 키·job/Flow·intent/request key·receipt·index/Tool/Agent 버전·질문 target/head revision·terminal 실패를 보존한다. 기존 init/migrate·성공 단계·완료한 모델 생성/Flow resume을 반복하지 않는다. 응답 유실은 기존 intent/key/receipt로 조회한다. 실패한 Run을 새 Run으로 몰래 교체하지 않는다.
- 인용 검증을 약화하거나 답변 뒤에 `[S1]`을 자동 부착하지 않는다. 기대 정답은 검사 기준으로 쓰고 Agent의 모델 prompt에 넣어 통과시키지 않는다. head 활성화는 검증된 published 자산과 expected revision CAS를 사용한다. static status/production flag를 수정해 완료로 가장하지 않는다. 모델·prompt·검색 조건을 동시에 바꿔 비교 근거를 잃지 않는다.
- GPU는 2026-10-12 문서 QA 인수에 사용 가능하다고 확인됐다. 새 작업의 실제 배포·provider·비용 한도·데이터 전송 범위는 그 단계에서 기존 설정/사용자 지시로 확인한다. 이 계획만으로 모든 후보 infra를 즉시 기동하지 않는다. 현재 Ops 운영 백업/복원·추가 운영 자동화·실제 재부팅·Kubernetes 보류를 유지한다.
- 코드/문서/테스트·격리 환경 검증은 요청된 단계 범위에서 진행한다. 실제 서버/API 접근이 없으면 검증 가능한 산출물과 정확한 명령을 남기고 독립 작업을 진행하되 인수를 통과한 것으로 표시하지 않는다. routine/reversible 작업에 반복 확인을 요구하지 않는다. 삭제/원본 교체·새 비용/계정·배포 목적지처럼 기존 지시로 결정되지 않는 사항만 구체 결과를 준비한 뒤 필요한 입력을 요청한다.
- credential 값·개인 자료·대용량 원문은 로그/문서/git/테스트 산출물에 넣지 않는다. 토큰 파일은 필요한 호출에서만 읽고 값을 출력하지 않는다. `.patch-backups`를 새로 만들지 않는다. 배포 변경은 선택 단계의 복귀 계획과 원본 보존을 따른다.

## 검증과 문서

먼저 `docs/development.md`, `docs/testing/README.md`, 해당 플랫폼의 검사 절차를 따른다. 변경 범위에 맞는 기존 검사를 실행하고 실제 실패/회귀가 필요한 곳에만 검사를 보완한다. 스킵/대역 성공/실제 서버 인수/품질은 구분한다.

사용자 서버는 shshin의 sh_test Python 3.10 환경이었다. Python 변경은 3.10 호환을 유지한다. Node 12는 최신 JS integration 검사를 수행하지 못하며 skip은 통과가 아니다. CI 화면 lane은 지원 Node와 필수 기능을 준비해 실제 검사를 수행한다. 필수 검사가 끝난 뒤 새 변경/실패 없이 전체 검사를 반복하지 않는다.

완료 상태/검증 범위는 `docs/status.md`, 우선순위는 `docs/roadmap.md`, 확장 계약/완료선은 확장 계획, 환경/첫 작업은 인수인계에 기록한다. 날짜별 실패·과거 검사 JSON은 보존하고 현재 결과는 새 소형 근거로 기록한다. README에 긴 진행 로그를 복제하지 않는다. 검증 산출물은 `docs/testing/artifact-retention.md`를 따른다.
