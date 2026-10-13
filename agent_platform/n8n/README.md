# Agent · n8n 연동

**현재 A09 — 운영 전달·동일 요청 재사용·Agent/n8n 재시작 후 기록 보존 PASS:** [재시작 서버 인수 기록](../../docs/history/agent-platform-202609.md#restart-verified-20260925). 2026-09-25 19:48 KST에 Agent 5개와 infra n8n 2개 컨테이너가 모두 Healthy로 복귀했다. 고정된 AgentRun 3개와 n8n 실행 4·5, Flow·게시 버전·원래 실행/요청/ToolCall ID가 유지됐고 lookup=passed, compare=preserved/변경 없음이었다. 새 추론은 없었다. 이번 7개 서비스의 재시작 보존 검증은 완료이며 반복하지 않는다. 질문 반복 품질 문제와 화면 확인은 보류, 전체 MVP 인수는 미완료다. 문서·검증 요약만 갱신하며 재빌드·재시작이 필요 없다.

**이전 A08 — 구현·로컬 검증 및 서버 적용·읽기 검사 통과:** [기능·화면 체크리스트](../../docs/agent-platform/console-a08-20260924.md), [서버 확인 기록](../../docs/history/agent-platform-202609.md#a08-server-check-20260924). 사용자 로그에서 24개 파일 적용, Service·Gateway a08.1 빌드/Healthy, A08 read-only smoke PASS를 확인했다. Agent 실행 23개·연결 2개 조회와 원래 실행/요청/ToolCall ID·실패 단계 보존을 확인했으며 추가 추론은 없었다. 사용자 화면 인수·답변 품질은 보류한다. 이번 기록 갱신은 문서/검증 요약만 변경하므로 재빌드·재시작·재시험이 필요 없다.

**2026-09-24 이전 A07 운영 질문 확인:** accessfix 적용 후 n8n 최초 계정 등록·API 키 연결과 운영 Flow 등록/게시가 성공했다. 첫 운영 질문은 validate/search/context/generate 성공 후 finalize에서 `ANSWER_REFERENCE_INVALID`로 실패했다. 인용 누락·잘못된 형식·제공 범위 밖 ID 중 정확한 원인은 현재 기록만으로 확정할 수 없다. 출처 검증을 유지하며 인용 형식 준수를 품질 후속에 기록한다. 운영 질문의 답변 전달 성공·재실행 인수는 미완료다. `set -e`로 두 번째 run과 별도 lookup은 실행되지 않았으며 첫 run 내부 조회가 위 실패를 확인했다.

**이전 A07 — 운영 워크플로 기능 구현·로컬 검증 완료:** [적용·등록·실행·조회](../../docs/agent-platform/n8n-operational-20260924.md). 상시 n8n HTTP API에 인증된 질문 워크플로를 등록·게시하고 Agent 버전 발행, 멱등 질문 실행, 원래 오류/출처 조회를 연결했다. 사용자 서버의 계정 연결·운영 Flow 등록/게시·Agent 실행 접수는 확인했다. 첫 운영 질문은 인용 검증에서 실패해 답변 전달·재실행 인수는 남아 있다. 이 문단의 A07 운영 연동 패치만 적용할 때는 Agent 재빌드·DB schema 변경·기존 서비스 재시작이 필요 없었다. 최신 패치 적용 절차는 문서 상단의 현재 단계 안내를 따른다.

**직전 실제 서버 질문 성공 확인:** 별도 n8n CLI → 실제 Agent/임베딩/검색/LLM 경로가 A07-live passed였다. 검색 5개·인용 2개·원래 요청 재사용, 잘림/경고 없음. AgentVersion `6f0fb4b9-2796-4e33-b040-cc6149bd0988`은 당시 초안이었으며 이후 운영 prepare에서 발행됐다. 이 성공은 운영 n8n Flow/페이지/재시작 전체 인수를 뜻하지 않는다. [답변 품질 후속 목록](../../docs/agent-platform/answer-quality-followup-20260924.md)은 사용자 요청으로 보류하고 기능을 우선한다.

이 폴더는 Agent 호출 워크플로 템플릿·생성기·WorkflowLink 등록·연동 테스트를 담당합니다. n8n 운영 구성·DB·기동·비밀 관리는 `infra/n8n`에 있습니다.

프로젝트 루트에서 `python3 infra/ops/up.py --n8n-only`로 기존 A07 구성을 이전/실행하세요. 기존 설정과 키는 자동 이전의 원본으로 보존합니다. 예전 이 폴더의 Compose/기동 스크립트는 제거했습니다.

- [infra 이전·기동·진단](../../docs/agent-platform/n8n-infra-20260924.md)
- [워크플로 사용·멱등 재시도·연결 등록](../../docs/agent-platform/n8n-a07-20260924.md)

Agent Platform 폴더에서 `docker compose -f n8n/tests/docker-compose.yml run --build --rm n8n-checks`를 실행하면 실제 n8n CLI와 격리 HTTP 대역을 검사합니다. 운영 DB·비밀·외부 네트워크를 연결하지 않습니다.

검사 파일·워크플로 템플릿은 고정 n8n 2.40.5 기반의 검사 이미지에 포함됩니다. 호스트 bind mount를 사용하지 않습니다. 이미지는 검사 코드 세 파일만 복사하며, 전용 Dockerfile.dockerignore로 운영 설정·비밀을 빌드 컨텍스트에서도 제외합니다. [검사 오류 수정·현재 서버 상태](../../docs/agent-platform/n8n-checks-fix-20260924.md)를 참고하세요.

2026-09-24 사용자 서버에서 a07.3 검사 이미지 빌드와 격리 CLI 검사 7개가 통과했습니다. 이미 통과한 검사를 문서 반영 목적으로 반복할 필요는 없습니다. 실제 사용자 Agent API/모델·지식 질문 검증은 이 격리 대역 검사에 포함되지 않습니다.

## 화면 없이 실제 API 시험

`python3 n8n/ops/live_check.py prepare`로 질문과 자원을 고르고 `python3 n8n/ops/live_check.py run`으로 실제 임베딩·검색·답변을 검증합니다. 별도 검사 CLI를 사용하며 운영 n8n DB/키와 기존 서비스를 변경하지 않습니다. [실행·재개·조회 안내](../../docs/agent-platform/n8n-live-check-20260924.md)를 따르세요.

실패 진단: `python3 n8n/ops/live_check.py diagnose`는 빌드·추론 없이 기존 실행/단계/검색 기록만 조회합니다. [답변 모델 필터 수정·진단 안내](../../docs/history/agent-platform-202609.md#n8n-livefix-20260924).
