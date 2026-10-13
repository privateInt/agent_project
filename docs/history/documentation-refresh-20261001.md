# 코드 검토와 문서 정리 기록

2026-10-01 `agent_project(4).tar`의 주요 API·처리·저장·실행 경로를 먼저 읽고, 보존된 결과 JSON/문서와 대조했다. 현재 구현·서버 근거·품질/사용자 인수·미완료를 구분했다. [분석 보고서](../reviews/code-review-20261001.md)와 [새 검증 결과](../testing/source-review-20261001.json)를 추가했다.

## 정리한 문제

- README·상태·로드맵·인수인계·Ops 문서에 같은 진행 이력이 반복되고 서로 다른 시점의 “다음 작업”이 남아 있었다. 현재 정보는 상태/로드맵에 모으고 날짜별 근거는 링크했다.
- Knowledge의 “다음은 Agent MCP 설계”, Agent A10의 “GPU/추적/화면은 남음” 등 뒤처진 안내를 현재 코드/검증 범위로 수정했다.
- 인용 문법·답변 전달 성공과 의미 품질 인수를 구분했다. 최초 후보 불일치와 OCR 실패 기록을 보존했다.
- Model 전용 백업 절차를 별도 runbook으로 옮기고, Ops 관측 백업/전체 플랫폼 복구와 구분했다.
- GC 운영 팁의 오래된 dry-run 안내를 실제 Compose 기본 apply·1h/168h와 실행 파일 단독 fallback의 차이로 수정했다.
- 고정된 Data 서버 IP를 일반 설치 기본값으로 제시하던 안내를 제거하고 기존 설정을 참조하도록 했다.
- 공통 설계 원칙에서 진행 이력을 걷어내고 한 단계씩 진행하는 작업 원칙을 명시했다.

## 보존과 적용

기존 비 Markdown 일반 파일 1,419개의 SHA-256을 대조해 변경 없음으로 확인했다. 실행 코드·Compose·환경·기존 결과 JSON·candidate/source pin을 수정하지 않았다. 별도로 이번 검증 JSON 1개를 추가했다. 기존 날짜별 상세 결과/실패 문서는 삭제하지 않았다. 삭제 대상 파일도 없다.

총 30개 파일을 반영한다(기존 26개 갱신, 새 4개). 기존 Model 절차의 문맥/명령은 새 `operations/model-backup-restore.md`에 보존했다. 반복 로그를 포함한 수정 대상 기존 Markdown은 783,182 bytes였다. 현재 요약/분석/목차로 줄였으며 정확한 파일별 크기와 변경 전/후 SHA-256은 배포 패키지의 `manifest.json`을 따른다. 과거 원문은 적용 시 `.patch-backups` 아래에 저장한다.

적용기는 먼저 문서 원본 해시·주요 코드 근거 해시·payload 해시·경로를 검사한다. 다른 버전의 문서는 덮어쓰지 않고 충돌을 보고한다. `--apply`가 없으면 검사만 하며, 적용 후 재실행은 `already_applied`다. 부분 실패의 백업 manifest와 복원 명령을 제공한다. 서비스 제어·빌드·추론·백업 실행은 없다.

## 검증

- 새 Python 검사 314 통과·45 건너뜀·실패 0. pinned age 도구가 없어 45개를 실행하지 않았다.
- Node DOM/API 대역 검사 8개 스크립트 통과. 실제 브라우저 인수와 구분한다.
- 기존 일반 파일 Python 415개 AST·JSON 266개·JS/CJS 25개 문법 검사 정상.
- 문서의 로컬 대상 파일과 heading anchor 연결을 검사한다. 적용 패키지는 dry-run·적용·재적용·충돌 차단·코드 변경 차단·복원을 검증한다. 최종 결과는 패키지 `verification.json`에 기록한다.

## 변경 파일

| 경로 | 작업 |
|---|---|
| `README.md` | 갱신 |
| `docs/status.md` | 갱신 |
| `docs/roadmap.md` | 갱신 |
| `docs/HANDOFF_NEXT_SESSION.md` | 갱신 |
| `docs/README.md` | 갱신 |
| `docs/ops-platform/README.md` | 갱신 |
| `ops_tools/README.md` | 갱신 |
| `docs/operations/model-backup-restore.md` | 추가 |
| `docs/operations/backup-restore.md` | 갱신 |
| `ops_platform/README.md` | 갱신 |
| `infra/observability/README.md` | 갱신 |
| `docs/design-principles.md` | 갱신 |
| `knowledge_platform/README.md` | 갱신 |
| `agent_platform/README.md` | 갱신 |
| `data_platform/README.md` | 갱신 |
| `docs/agent-platform/README.md` | 갱신 |
| `docs/agent-platform/work-plan.md` | 갱신 |
| `docs/knowledge-platform/work-plan.md` | 갱신 |
| `docs/architecture.md` | 갱신 |
| `docs/development.md` | 갱신 |
| `docs/data-platform/status.md` | 갱신 |
| `docs/data-platform/README.md` | 갱신 |
| `docs/knowledge-platform/README.md` | 갱신 |
| `docs/agent-platform/acceptance-a09-20260924.md` | 갱신 |
| `docs/testing/source-review-20261001.json` | 추가 |
| `docs/reviews/code-review-20261001.md` | 추가 |
| `docs/testing/README.md` | 갱신 |
| `docs/history/README.md` | 갱신 |
| `docs/operations/tips.md` | 갱신 |
| `docs/history/documentation-refresh-20261001.md` | 추가 |

## 이후 파일 정리

같은 날 후속 요청으로 완료 검증 문서 32개를 통합하고 `.patch-backups` 전체 삭제를 승인받았다. 위의 “날짜별 문서 유지/삭제 없음”은 첫 문서 최신화 패치의 범위다. 후속 [파일 정리](../operations/project-cleanup.md) 이후 기존 패치 백업은 남기지 않는다. 원래 검증 판정과 실행 파일은 유지한다.
