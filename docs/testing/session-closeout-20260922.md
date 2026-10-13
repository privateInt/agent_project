# 전체 프로젝트 검토·세션 마감 — 2026-09-22

사용자가 올린 `agent_project(3).tar`를 기준으로 검토했다. Knowledge v1의 합의한 핵심 구현과 앞선 사용자 서버 검증은 유지한다. 이번 검토에서는 문서/테스트의 오래된 설명과 브라우저 요청 보관 실패 처리를 수정했다. 서버에 직접 접속하거나 서비스를 교체하지 않았다. 검토 당시 패치 서버 적용은 확인 전이었으며, 이후 사용자 로그로 확인한 문서 통폐합 적용 범위는 아래 기록에 반영했다.

## 검토 대상과 방법

- 원본 업로드 984개 파일을 검사하고 별도 사본에 추출했다. 경로 이탈·링크 항목 없이 소스/구성/문서를 읽었다. 원본 첨부는 유지했다.
- 직전 전달·검증 완료본에 있던 소스 파일과 SHA-256을 대조해 변경·누락이 없음을 확인했다. 추가 사용자 환경 설정은 보존했다. 현재 실행 이미지와 소스의 직접 비교는 수행하지 않았다.
- Model/Data/Knowledge Python 단위 검사, Knowledge 화면 대역 검사, 언어별 문법·문서 링크·Compose include·Docker COPY 경로·플랫폼 간 계약 사본을 확인했다.
- 문서의 책임 경계·실제 API 경로·코드 위치·현재 완료 상태를 구현/인수 기록과 대조했다. 모든 코드 줄의 의미·성능·운영 보안을 완전 검증한 결과는 아니다.

## 발견한 문제와 반영한 수정

| 항목 | 영향 | 조치 |
|---|---|---|
| 브라우저 요청 보관 오류 무시 | 청크 미리보기·인덱스 관리에서 localStorage 저장 실패 뒤에도 요청 전송. 응답 유실/새로고침 후 동일 키 재확인이 불가능할 수 있음 | 저장이 성공한 뒤에만 상태 확정·요청 전송. 응답 이후 보관 기록 제거 실패도 원래 키/본문 유지. 두 경로 모두 기존 코드에서 재현, 수정 후 회귀 통과 |
| 아키텍처의 오래된 초안 | 존재하지 않는 nested version/activate·removals API, 계획 단계 디렉터리, Job의 OpenSearch 직접 쓰기 도식이 실제 구현과 혼재 | 현재 라우트·내부 패키지·Service의 CommitBatch/쓰기 경계로 정정. 세부 계약 연결 유지 |
| 완료 상태·다음 작업 불일치 | reranker/HNSW/청커 비교가 완료 기록과 달리 대기로 표시되고 같은 이력이 여러 문서에 반복 | 현재 상태와 월간 이력 분리. 최신 test1·실패/복구 근거·남은 한계·Agent 후속 작업을 인수인계에 정리 |
| 오래된 UI 테스트 | 기존 문구와 `run-a` 같은 잘못된 UUID 때문에 현재 UI 규칙에서 실패. 브라우저 fixture에 추가 스크립트와 단계 이동 누락 | 현재 문구·유효 UUID·전체 UI 자산·자료 선택/미리보기 진입 흐름으로 갱신. Node 회귀 통과, Chromium 렌더링 검사는 아래 환경 제약 유지 |

실행 코드 변경은 `src/platform/knowledge/gateway/web/app.js`, `indexing.js`와 자산 버전 문자열을 갱신한 `index.html`이다. API·DB·청킹/색인/검색·Model/Data 실행 코드와 설정은 변경하지 않았다. 기존 지식 생성의 두 단계 저장 규칙은 그대로 유지한다.

## 이번 환경에서 실행한 검증

| 대상 | 결과 | 해석 |
|---|---|---|
| Knowledge Python | 76개 통과 | 기존 도구·평가 단위 검사. 실제 모델/검색 품질 실행 아님 |
| Model Python | 269개 중 217개 통과·52개 skip | Streamlit 미설치로 Playground AppTest 52개 건너뜀. 전체 UI 인수로 처리하지 않음 |
| Data Python | 220개 통과, UI 모듈 2개 로딩 오류 | test_data_console/test_data_tables는 Streamlit 미설치. 전체 discover 명령은 실패 종료했으며 이를 전체 통과로 집계하지 않음 |
| Knowledge Node | 5개 스크립트 통과 | 요청 보관 쓰기 실패·정리 실패·동일 키 재확인, 검색/생성/선택 전환 회귀. 실제 렌더링은 아님 |
| Playwright console.spec.cjs | 미실행 | Chromium 실행 파일 없음. fixture는 갱신하고 JS 문법만 확인 |
| 정적 검사 | Go 483개·Python 161개 문법, JS/CJS·TOML·shell 문법 통과 | Go 타입 검사·빌드 성공을 뜻하지 않음 |
| 문서/구성 | Markdown 상대 파일 링크, JSON/YAML 파싱, Compose include 27개 경로, 일반 Dockerfile COPY 74개 소스 경로 확인 | Compose 실행·환경 변수 치환·이미지 다운로드/빌드는 미실행. Model 학습 Job Dockerfile의 패키지 설치는 이 경로 검사 범위 밖 |
| 플랫폼 간 계약 | Model embedding/reranking 및 Data collection/refinement 4개 proto 사본 일치 | go_package 차이 제외. protobuf 코드 생성/컴파일은 이번 환경에서 미실행 |

로컬에는 Docker/protoc/PostgreSQL이 없어 전체 Go/DB/실제 모델 검사를 새로 실행하지 않았다. 앞선 사용자 서버의 전체 Go 15개 테스트 패키지·PostgreSQL service/app 최상위 17개 통과 근거는 [실패·복구 기록](../knowledge-platform/testing-failure-recovery-20260922.md)에 유지한다. 이번 로컬 검사와 과거 서버 검사를 합산하지 않는다.

작은 판정 요약은 [JSON](../../src/platform/knowledge/tests/results/session-closeout-20260922.json)에 보존한다. 결과 파일 총량은 기존 합계 1 MB 미만 정책을 따른다.

## 앞선 코드 수정 패치의 적용 범위

앞선 `project_session_closeout_20260922` 적용기는 전체 업로드의 이전 파일 해시를 검사하고 변경 파일만 반영한다. 충돌 시 쓰기 전에 중단하며 재적용 시 이미 적용된 파일은 건너뛴다. 환경 설정·인증 자산·운영 데이터는 패치에 넣지 않고 파일 삭제나 서비스 명령을 자동 실행하지 않는다.

이번 수정은 정적 화면 자산을 포함하므로 적용 후 **knowledge-gateway만 빌드·교체**해야 한다. Model/Data·Knowledge Service/Job·DB·OpenSearch·기존 배포/인덱스는 교체하거나 재생성하지 않는다. 웹 페이지를 새로고침해 새 자산을 읽는다. 이후 문서 통폐합 적용기의 전제 검사가 통과해 요청 보관 수정의 소스 해시는 확인됐다. Gateway 이미지 교체와 수정 후 실제 브라우저 확인은 아직 미수령이다.

## 다음 세션

[인수인계](../HANDOFF_NEXT_SESSION.md)와 [인프라 확장 검토](../reference/infrastructure-expansion-review.md)의 합의에 따라 Agent 소유 MCP 검색 Tool 계약부터 진행한다. Agent MVP 완료 후 전체 플랫폼 통합 모니터링을 구축한다. 현재 인프라 설치는 미수행이며 기존 평가 자료·기준 인덱스·원본·실패 보고서를 보존한다.

recursive ON 최초 후보 불일치 원인은 미확정이다. 새 콘솔 OFF·전체 레이아웃/접근성·Model 재배포 실사용·OpenSearch custom mapping 독립 조회는 별도 미확인이다. Data PDF 구조 판별/OCR 개선, 실제 노드 장애·다중 사용자 권한·백업 복원 등은 별도 범위다. 이를 숨기거나 Knowledge v1을 모든 운영 조건에서 검증 완료로 표시하지 않는다.


<a id="documentation-consolidation"></a>

## 문서 파일 통합 — 2026-09-22

사용자 요청에 따라 완료된 패치별 문서를 실제로 통합·삭제했다. Markdown은 **121개에서 99개**가 됐다. 기존 문서 23개를 제거하고 K04~K08 검증 기록 1개를 만들었으며, 나머지 내용은 기존 기능별 문서에 합쳤다. archive 폴더로 옮겨 파일을 숨기는 방식은 사용하지 않았다.

| 통합 대상 | 현재 위치 | 보존한 내용 |
|---|---|---|
| Knowledge 검증 5개 | acceptance.md의 K04~K08 | UUID·해시·지표·실패와 검증 한계 보존 |
| Knowledge API/검사 2개 | api-search.md / testing-search.md | 선택적 rerank를 검색 문서에 통합 |
| Knowledge 패치 안내 5개 | 청킹·색인·평가의 API/testing 문서 | 문맥 보존, bulk 복구, HNSW, 집계·후보 진단 통합 |
| Model 보완 안내 5개 | testing/reranking.md / embedding.md / model-management-update.md | profile·import 오류, 직접 embedding 확인, 재배포·진행률 |
| Model 인수·제어 검사 3개 | testing/results-summary-20260916.md / request-creation.md | Model v1 범위와 CPU smoke·Dispatcher 장애 절차 |
| Data 안내 3개 | data-platform/architecture.md / testing-ocr.md / testing-ocr-quality.md | 재사용 경계·PDF fallback·영역 실험 |

계약·현재 사용법·검사 절차·당시 증거의 역할을 구분하고, 오래된 개별 tar/apply 안내와 이미 완료된 다음 단계 문구를 제거했다. 과거 실패를 성공으로 변경하지 않았고 PDF 구조 판별은 Data 책임으로 유지했다. 문서 상대 링크와 문서 경로를 가리키는 JSON 메타데이터 3곳도 새 위치로 고쳤다. 테스트 판정·측정값·원본 해시는 바꾸지 않았다.

이번 `project_docs_consolidation_20260922`는 문서와 위 문서 참조만 반영한다. 서비스 소스·테스트 코드·fixtures 데이터·설정·인증서·모델/인덱스/운영 데이터는 변경하지 않는다. 서비스 재빌드·재시작은 필요 없다. 앞선 코드 수정 패치를 대체하지 않으며 그 수정이 적용된 소스를 기준으로 검사한다. 사용자 로그로 `CHECK OK: 49 updates, 23 obsolete documents to delete`와 `APPLIED: 49 updates, 23 deletions`를 확인했다. 따라서 문서 통폐합의 서버 적용은 완료다. 적용기 전제 검사의 통과는 앞선 수정 소스의 해시 일치도 뒷받침하지만 Gateway 이미지 교체·수정 후 실제 브라우저 확인을 증명하지는 않는다.

적용기는 업데이트·삭제 대상 전체의 해시와 경로를 쓰기 전에 검사한다. 로컬 수정·예상치 않은 누락·심볼릭 링크가 있으면 중단한다. 통합된 문서를 먼저 저장하고 변경되지 않은 구문서만 삭제한다. 재적용은 변경 0개이며 실행 중 중단은 재실행으로 이어갈 수 있다. 여러 파일 전체의 원자적 교체는 아니므로 적용 중 다른 편집은 피한다. 전체 tar만 기존 폴더에 덮어쓰면 구문서가 남기 때문에 기존 서버에는 이 적용기를 사용한다.

검증: 문서 파일/절 링크, 삭제 문서의 남은 참조, 비문서 파일 변경 범위, 기존 측정 기록 보존을 검사한다. 적용기는 check 무변경·수정/삭제 충돌 차단·누락/링크 거절·정확 반영·재적용을 검증한다. 문서 정리 때문에 Go/DB/GPU/브라우저 검사를 반복하지 않는다.
