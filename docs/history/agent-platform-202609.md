# Agent 검증 이력 — 2026년 9월

완료 검증 기록 13개를 통합했다. 사용자 서버에서 전달받은 당시 결과와 로컬 검사 범위를 구분하며 이번 정리가 검사를 다시 실행한 것은 아니다. 현재 상태는 [상태표](../status.md), 다음 작업은 [로드맵](../roadmap.md)를 따른다.

기존 상세 문서 원문은 정리 패키지의 `originals/`에 보존한다. 각 항목의 제목은 옛 파일명을 유지해 기존 문서 링크가 통합 항목으로 이어지게 했다. 테스트 결과 JSON·테스트 코드·실행 중 보고서는 그대로 둔다. 당시 UUID·컨테이너·디스크 수치는 현재 상태를 보장하지 않는다.

## testing-foundation-20260922

**A02 검증 기록 — 2026-09-22**

A02 기반 구현의 서버 smoke·실제 PostgreSQL 검사·네 서비스 재기동 후 기존 진단 조회가 PASS했다. 최초 로컬 DB skip과 이후 실제 서버 검증을 구분한다. 화면·답변 의미 품질 인수는 별도다.

근거: [foundation-local-20260922.json](../../agent_platform/tests/results/foundation-local-20260922.json), [foundation-acceptance-20260922.json](../../agent_platform/tests/results/foundation-acceptance-20260922.json).

## testing-tools-20260922

**A03 검증 기록 — 2026-09-22**

A03 REST/MCP·Tool 관리/실행·DB·재시작 후 ToolCall 조회의 실제 PostgreSQL 16/Compose 서버 인수가 완료됐다. 로컬 보조 PGlite 검사의 단일 backend 한계와 서버 근거를 구분한다. 로컬 테스트 ID를 서버에서 재사용하지 않는다.

근거: [tools-local-20260922.json](../../agent_platform/tests/results/tools-local-20260922.json), [tools-server-20260923.json](../../agent_platform/tests/results/tools-server-20260923.json).

## a05-server-ui-check-20260923

**A05 서버 화면 시험 기록**

사용자가 제공한 검색 시험 화면은 성공·결과 5개로 표시됐다. 전달된 본문은 1~3번과 4번 제목까지여서 전체 5개 내용을 확인하지 않았다. 직접 API 등록→발행→시험의 화면 인수와 관련 품질 상세는 별도다.

## a06-server-check-20260924

**A06 서버 기능 검사와 화면 확인 보류**

A06 서버 적용·기능 검사와 Foundation 포함 실제 DB 검사가 통과했다. 전달 로그에 근거한 판정이며 화면 확인과 답변 품질은 사용자 결정에 따라 보류했다.

## a08-server-check-20260924

**A08 서버 적용·읽기 검사 확인 — 2026-09-24**

A08 Service/Gateway 갱신·기동과 읽기 전용 smoke가 PASS했다. 기존 운영 질문의 ANSWER_REFERENCE_INVALID를 보존했다. 사용자 화면·답변 품질 성공으로 확대하지 않는다.

## n8n-db-permission-fix-20260924

**n8n 시작 실패 — 전용 DB CREATE 권한 복구**

infra 이전 시 기존 볼륨을 보존했으나 n8n 전용 DB CREATE 권한 부족으로 기동이 실패했다. 권한 복구 후 n8n/DB Healthy를 확인했다. 후속 검사 이미지 경로 오류는 별도 보완이며 다른 플랫폼 데이터 초기화는 하지 않았다.

## n8n-livefix-20260924

**A07 live 후속: 답변 모델 선택과 실패 진단 수정**

답변 모델 선택/실패 진단을 보완하고 Runtime 재빌드·실제 Gemma 배포 후 A07-live 질문이 PASS했다. 이전 MODEL_NOT_READY는 당시 LLM 미배포와 관련된 실패로 보존한다. live 성공이 운영 n8n 전체 인수 완료를 뜻하지 않는다.

## citation-trial-20260925

**A09 인용 누락 확인과 새 초안 시험**

prompt만 바꾼 새 초안의 실제 시험·lookup이 PASS했다. 원본과 같은 문맥 5개, 유효 인용·잘림 없음·기존 실행 조회를 확인했다. 단일 후보/질문 검사이며 모든 질문의 형식·의미 품질을 보장하지 않는다.

## citation-verified-20260925

**A09 초안 인용 시험 성공과 운영 n8n 인수 절차**

새 초안의 단일 질문 인용 검사가 PASS했다. 이후 발행·새 n8n Flow 등록과 운영 전달 검증으로 이어졌다. 원본 버전의 MISSING_REFERENCE는 보존하며 인용 문법과 유용한 답변 품질을 구분한다.

## n8n-operational-verified-20260925

**A09 운영 n8n 전달·동일 요청 재사용 검증**

운영 답변·출처 전달과 동일 요청 재사용, capture/compare가 PASS했다. 실제 답변은 질문을 5번 반복해 의미 품질이 미흡했다. 기능 성공을 전체 MVP 사용자 인수 완료로 기록하지 않는다.

## n8n-session-recovery-20260925

**A09 서버 기록과 n8n 로컬 운영 세션 복구**

기존 n8n 세션의 restore/status/compare/audit가 성공했으나 workspace 세션 재소실 원인은 미확정이었다. ~/.local/state/agent-platform/n8n으로 명시 복구 후 실행 접수를 확인했고 당시 새 실행은 finalize/ANSWER_REFERENCE_INVALID로 실패했다. 복구를 반복하거나 원래 실패를 지우지 않는다.

## reference-diagnostics-20260925

**A09 인용 실패 진단과 서버 후속 기록**

인용 실패 진단 패치·host 5개 테스트·Runtime tests가 PASS했다. 저장 진단에서 MISSING_REFERENCE와 finish_reason=stop을 확인했다. 진단 추가 자체가 LLM 답변 생성 문제의 해결은 아니며 prompt-only 후보를 별도로 시험했다.

## restart-verified-20260925

**A09 Agent·n8n 재시작 후 기록 보존 — 서버 확인 완료**

Agent 5개·infra n8n 2개 재시작/Healthy와 기준에 고정된 연결·실행·출처·세션 보존을 PASS로 확인했다. 파일별 설정/키 해시 전체 재검사와는 다르다. 화면·Flow 사용성·답변 품질은 보류 상태를 유지한다.

