# Knowledge 실패·복구 보완 — 2026-09-22

사용자가 확인한 새 화면 검색 다음의 2번 작업이다. 기존 실패 검사를 대조해 빠진 실행 경로를 추가했다. **테스트·실행 도구·문서만 변경했다. 서비스 동작/API/스키마 변경이나 이미지 교체는 없다. 사용자 서버에서 패치 18개 적용, 전체 Go 검사와 PostgreSQL service/app 검사 17개가 통과했다.**

## 기존 범위와 추가한 검사

| 실패 경로 | 기존 검사 | 이번 추가 |
|---|---|---|
| 문서 임베딩 장애 | profile/벡터 계약, 생성 요청 재확인 | 실제 Job `run()`과 loopback gRPC: 첫/두 번째 배치의 Model 오류, 잘못된 결과 거절, 추론 자동 재시도 없음 |
| 저장 응답 유실 | 단일 배치 대역 실패·동일 receipt 재확인 | Job에서 동일 CommitBatch 요청으로만 최대 3회 재확인, 실패 예산 소진 후 추가 임베딩 중단 |
| bulk 부분 성공 | 모든 응답 항목·ID·상태 검사 | 실제 HTTP client 앞에서 일부 항목 저장 후 오류, 전체 저장 후 연결 종료, 동일 NDJSON 재전송과 중복 문서 방지 |
| 여러 배치의 부분 진행 | 단일 배치 DB 통합 | PostgreSQL 진행률·보류 receipt 보존, 서비스 객체 재생성 후 벡터 변경 거절·원래 결과 재확인, 저장된 batch 재확인 시 blob 재조회/중복 bulk 없음 |
| 마지막 쓰기 차단/완료 응답 유실 | 미완료 seal 거절·완료 재호출 | 외부 쓰기 차단만 완료하고 응답 유실 → DB는 indexing 유지 → 재확인 후 ready. Job 최종 응답 유실 시 실패 보고로 ready를 덮어쓰지 않음 |
| 작업 성공 보고 유실 | lifecycle report의 재시도 | Reconciler가 일치하는 ready/개수/참조 receipt로 성공 조정, 부분 진행·미확정 참조·다른 작업 식별자는 성공 처리하지 않음. 조정 응답 유실 후 같은 관찰 조건 유지 |
| 실패한 새 버전 | SQL로 failed 상태를 만든 활성 버전 보존 검사 | 실제 첫 batch 저장 후 실패한 Operation을 유지보수 루프가 관찰. 진행률과 Data 참조 보존·실패 버전 활성화 거절·기존 활성 버전 보존 |
| 재정렬 모델 장애 | 입력 초과·누락 점수·취소·시간 초과 | profile/점수 요청 각각 Unavailable → 결과 fallback/원문 오류 노출/자동 추론 재시도 없음 → 명시적 새 검색 성공 → DB 잠금 해제 확인 |
| 동시 버전 변경 | 활성화 두 요청의 CAS 경쟁, 검색 중 삭제 대기 | 같은 버전의 활성화와 삭제 경쟁에서 한 요청만 성공. 삭제 중인 버전을 활성으로 남기지 않음 |

Job gRPC 7개, Bulk HTTP 2개, Reconciler 5개 시나리오를 추가했다. 새 DB 테스트는 4개 함수이며 재정렬의 profile/점수 실패 2개 하위 경우를 포함한다. 이후 전체 Go 검사와 아래 PostgreSQL 실행 결과로 해당 추가 검사의 통과를 확인했다. 시나리오 수와 최상위 테스트 함수 수는 구분한다.

소스: [Job](../../src/platform/knowledge/jobs/indexing/main_test.go), [HTTP bulk](../../src/platform/knowledge/internal/searchstore/recovery_test.go), [Reconciler](../../src/platform/knowledge/reconciler/app/index_recovery_test.go), [DB 복구·경쟁](../../src/platform/knowledge/service/app/failure_recovery_integration_test.go).

## 격리와 복구의 의미

Job 테스트는 테스트 프로세스의 127.0.0.1 임시 gRPC 서버로 세 연결 주소를 모두 지정한다. HTTP 테스트도 임시 서버를 사용한다. 임베딩 벡터는 테스트가 생성하며 실제 배포를 조회하거나 GPU를 사용하지 않는다. Reconciler의 Docker 관찰도 대역이다.

DB 검사는 기존 testsupport.Postgres를 사용한다. 설정된 PostgreSQL에 임시 schema를 생성하고 해당 search_path에서만 테스트 catalog를 만든 뒤 정리한다. Model/Data/OpenSearch는 대역이다. 운영 인덱스·원본·평가 정답셋을 변경하지 않는다. 단, 같은 PostgreSQL 서버에 테스트 연결과 임시 schema 생성 부하는 발생한다. 운영 서비스 시작·중지·재빌드는 수행하지 않는다.

동일 배치 재전송은 **현재 실행에서 보유한 같은 Model 응답**으로만 가능하다. 서비스 객체를 재생성하는 테스트는 DB receipt가 메모리에 의존하지 않는지 확인하는 것이며, 죽은 Job을 자동 복구하거나 벡터 전문을 DB에서 복원한다는 뜻이 아니다. Job이 결과를 잃고 종료되면 추론을 자동으로 다시 실행하지 않는다. 종료된 프로세스에 최종 receipt가 없으면 기존 정책대로 확인 후 새 버전을 명시적으로 만든다.

Reconciler 대역 테스트는 요청의 관찰 조건 보존을 검사하며 Operation DB의 CAS 구현 전체를 다시 검증하는 테스트가 아니다. HTTP/저장 대역 결과는 실제 OpenSearch 노드 장애 주입의 결과와 구분한다.

## 실행 상태

- 로컬 확인: 변경 Go 테스트 6개 파일의 파싱/gofmt, Python 실행 도구 문법·대역 호출 인자, 합성 청킹 입력 12/14개가 여러 배치 조건을 충족함을 확인했다.
- 당시 로컬에는 Docker·protoc·PostgreSQL이 없어 새 전체 Go/DB 검사는 미실행이었다. [사전 확인 기록](../../src/platform/knowledge/tests/results/failure-recovery-preflight-20260922.json)은 그 시점의 기록으로 보존한다. 이후 사용자 서버 로그로 실행 완료를 확인했다.
- 서버 절차: `docker build --target tests -t knowledge-platform-tests:failure-recovery ./knowledge_platform` 다음 `python3 src/platform/knowledge/tests/run_failure_recovery_checks.py`. 첫 명령은 DB를 제외한 Go 테스트를 실행하고, 두 번째는 Compose에서 DB 연결을 읽어 임시 schema의 service/app 테스트를 실행한다. 비밀 연결 값은 출력하지 않는다.
- 사용자 서버의 두 명령이 모두 완료됐다. tests 이미지의 `go test -count=1 ./...`에서 테스트가 있는 15개 패키지가 ok이며, 별도 PostgreSQL 연결 실행에서 service/app 최상위 17개와 재정렬 profile/scores 하위 경우가 통과했다. DB 실행 로그에 skip/실패는 없고 runner의 최종 PASS를 확인했다. [서버 결과 요약](../../src/platform/knowledge/tests/results/failure-recovery-server-20260922.json). 일반 tests target의 DB skip만으로 판정하지 않았다.
- 새 Job/HTTP/Reconciler 및 DB 복구 경로는 이번 격리 검증 범위에서 완료다. Gateway/app도 전체 검사에 포함되어 새 콘솔 전달 경로의 Go 검사 통과를 확인했다. 운영 서비스 이미지는 교체하지 않았으며 테스트 이미지 빌드만 수행했다.

## 새 사용자 화면 확인

사용자 제공 버전 JSON에서 test1의 42/42 ready·참조 held·HNSW m=32/ef_construction=256을 확인했다. 이어진 화면에서는 같은 버전을 기본 검색 버전으로 선택했고, 이름으로 선택한 reranker를 사용해 후보 20개→결과 5개·검색 처리 295ms·원문/출처 표시를 확인했다. [정확한 식별자·확인 한계](../../src/platform/knowledge/tests/results/console-ui-server-20260922.json).

이는 custom 설정의 버전 저장·빌드·활성화·검색을 확인한 근거다. OpenSearch 원시 매핑을 따로 조회한 결과, 새 콘솔 OFF 화면, 전체 결과의 정답 여부를 뜻하지 않는다. 전체 Go 검사 로그는 이번 실패·복구 실행에서 추가 확인했다. 기존 API·이전 화면 OFF 인수는 유지한다.

## 다음 단계

서버 Go와 임시 schema 검사는 통과했고 이 로그에서 추가 수정이 필요한 실패는 없다. 같은 검증을 문서 갱신 때문에 반복하지 않는다. 기존 recursive ON 최초 CANDIDATE_IDENTITY_CHANGED의 원인은 미확정으로 보존한다. 다음은 Agent 소유 MCP 검색 Tool 계약 설계다. 자연어 질문의 Model 임베딩 → Knowledge 검색 API 호출, 선택적 rerank 옵션 전달, 결과·출처·오류 반환을 정의한다. PDF 파싱·OCR·제목 구조 판별은 Data 영역이며 이번 작업에 포함하지 않는다.
