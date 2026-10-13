# Knowledge Platform — 2026년 9월

각 절은 해당 시점의 이력이다. 당시의 ‘미확인/다음 작업’을 현재 상태로 해석하지 않는다. 현재 기준은 [상태](../status.md)와 [인수인계](../HANDOFF_NEXT_SESSION.md)다.

## 2026-09-18 — 경계 정리와 첫 청킹 구현

- 이전 두 프로젝트를 검토하고 선택형 chunker, Model 배포 모델 사용, Knowledge의 선택적 rerank, Agent의 MCP/Tool/flow 소유권을 정리했다.
- 프로젝트 내부 아키텍처·작업 목록·로드맵·인수인계를 먼저 갱신한 후 입력 snapshot·두 청커·비동기 Job·저장된 청크 미리보기 소스를 추가했다.
- 현재 그룹 version으로 멤버 구성을 고정할 수 없으므로 첫 구현은 명시적 canonical 선택 목록을 저장한다. Data에는 원본별 정제 실행 조회 필터를 추가했다.
- 짧은 사실·Unicode 출처·표/코드·읽힌 그림 글자·품질 경고를 보존하고, 미인식/빈 영역은 제외 근거로 기록한다. 첫 `.1` 버전에서 큰 표/코드는 자동 분할하지 않고 오류로 알렸다.
- 요청 상태 Node 대역 검사와 JS/Python/Compose 정적 확인은 통과했다. 최초 전달 시점의 Go/Docker/DB/실제 브라우저/서버 경로는 미검증이었다. 서비스 적용 완료나 K02 인수 완료로 해석하지 않는다.

현재 계약과 적용 순서는 [청킹 계약](../knowledge-platform/api-chunking.md), [빌드·검증](../knowledge-platform/testing-chunking.md)을 따른다. embedding·색인·검색·rerank·MCP 구현은 아직 시작하지 않았다.

## 2026-09-18 — 서버 실행 확인과 큰 구조 분할 후속 보완

- 사용자 실행 로그에서 98개 파일 적용, Knowledge `go test ./...`, Data refinement/gateway 검사, Job/상시 서비스 빌드와 5개 서비스 기동을 확인했다. 전용 DB 검사는 별도이며 모든 통합 검증의 통과를 뜻하지 않는다.
- canonical `157c335f-5436-4338-8078-edb040ebe1ca`의 `b90`은 1,352자 table이다. 첫 실행 `3569e4e3-cdc5-4fc1-9471-7fa37e772f78`은 구조 크기 제한으로 실패했다.
- 사용자가 최대 글자 수 8,000으로 새 실행 `3373638b-b2c1-448b-8f1d-627b748f779e`를 생성했고 14개 청크 저장·화면 조회를 확인했다. 전체 청크 내용이나 실제 선택한 청커의 상세 설정 JSON은 제공되지 않았으므로 추정하지 않는다.
- 후속 `.2`는 셀/Markdown 행 분할, 헤더·병합 셀의 기준 행 반복과 출처 보존, 코드 줄 분할, 원자 행/줄 초과 시 필요한 글자 수 표시를 추가한다. `.1` 결과 조회·실행 정책은 유지한다.
- 새 Go 회귀 검사에는 Unicode·CRLF·원문 coverage·헤더/병합 문맥·코드·오류·버전 호환·기존 OCR 표 fixture를 포함한다. 이 환경에서 새 Go 실행은 못 했다. Node 요청 상태/분할 표시 검사와 기존 fixture 원문 대응 확인은 통과했다. 실제 `b90` JSON 자체는 미수령이다.

## 2026-09-18 — 구조 분할 서버 로그·48개 저장 확인

사용자 첨부 로그에서 증분 24개 적용, Go chunking 및 전체 검사, 이미지 빌드·서비스 기동 성공을 확인했다. 화면의 실행 `6e3eaf24-c2dc-4756-87bb-83468a96c827`은 48개 청크를 저장했다. 본문 세 청크와 `needs_ocr` 표시는 확인했지만 실제 옵션 JSON과 표 분할 내용은 미확인이다. 48개라는 개수만으로 표 품질 완료로 판단하지 않는다. 사용자 요청으로 다음 구현은 Model 관리 수정부터 진행한다.

## 2026-09-18 — Model 선행 확인 완료와 다음 대화 재개점

Model 배포 삭제는 사용자 성공 확인을 받았으며 기존 snowflake 배포에서 실제 임베딩 벡터 반환까지 확인했다. K03 전체 완료는 아니고 공식 API/profile·한도·공간 identity·용도 표시와 Knowledge 연결은 남아 있다. K02는 48개 저장 결과의 실제 옵션·표·출처·두 청커 비교가 남아 있다.

사용자 요청에 따라 LiteLLM 검토와 프로젝트 상태·작업 목록·인수인계를 먼저 문서화했다. LiteLLM은 Knowledge 선행 조건에 포함하지 않는다. 이번 대화에서 기능 구현을 시작하지 않으며 다음 대화는 K02 잔여 확인과 K03 기존 배포 연결부터 진행한다.

## 2026-09-18 — K03 Model 계약과 K02 기존 결과 조회

K03 임베딩 API/profile은 Model에 구현했다. Knowledge는 모델을 로드하지 않는다. 읽기 전용 `knowledge_platform/tests/inspect_chunking.py`로 저장된 실행의 실제 옵션·표 b90 출처를 조회할 수 있다. 사용자 서버 결과와 K03 공식 API 인수는 아직 수령하지 않았으며 K04 색인 구현을 완료 처리하지 않는다. 다음은 [K03 서버 적용·검사](../testing/embedding.md) 후 색인·버전·참조 관리다.

## 2026-09-18 — K02 상세 확인과 K04 색인 구현

K04 소스 구현을 추가했다: 논리 인덱스·새/기존 청킹 입력·Model 문서 임베딩·OpenSearch 저장·불변 버전·CAS 활성화·삭제와 Data 참조 보호. JS 요청 상태 검사와 Python 도구 3개는 통과했다. Go/DB/OpenSearch 서버 검증은 아직 실행하지 않았으며, 다음은 K04 적용·인수 후 K05 질문 벡터 검색이다. 실제 K02 저장 옵션·표 b90 spans/반복 문맥을 사용자 로그로 확인했다. [K04 계약](../knowledge-platform/api-indexing.md)과 [검증](../knowledge-platform/testing-indexing.md)을 따른다.

## 2026-09-21 — K04 첫 빌드 오류 수정

- 사용자 로그에서 K04 패치 64개가 정상 적용됐고, 첫 Knowledge 검사 이미지의 Go 컴파일이 `service/app/indexes.go:116`에서 중단됐음을 확인했다.
- 원인은 `chunking.Verify`의 반환형 `*chunking.Result`와 `indexing.Select` 인수형 `chunking.Result`의 불일치다. 검증 오류 처리 뒤 `*result`를 전달하도록 호출부 한 곳을 수정했다. 검증 함수는 성공 시 non-nil 결과를 반환한다.
- 같은 로그에서 9개 패키지의 `ok` 출력을 확인했다. 전체 Go 빌드·DB·OpenSearch 성공으로 집계하지 않는다. `set -e`에 따라 이후 Data 검사와 서비스 교체는 실행되지 않았다.
- 수정 후 Go 실행은 작성 환경에 Go/Docker가 없어 미실행이다. 후속 패치 적용 후 [K04 검증 1번](../knowledge-platform/testing-indexing.md)부터 재개한다. 기존 임베딩 runtime·ChunkSet을 재생성하지 않는다.

## 2026-09-21 — Go/DB 통과, 실제 bulk 파싱 오류와 후속 수정

- 사용자 로그에서 컴파일 수정 7개 적용, Knowledge 전체 Go 검사, Data refinement unit, Knowledge catalog/Data reference PostgreSQL 통합 suite, Python 3개, Knowledge 이미지 7개와 Data Refinement 이미지 1개 빌드를 확인했다.
- 처음 서비스 교체는 `.env.indexing` 누락으로 Knowledge만 중단됐다. 설정 도구 실행과 Knowledge 서비스 5개 기동 뒤 실제 빌드는 48개 입력을 확인하고 첫 bulk에서 `INDEX_BATCH_UNCONFIRMED OPENSEARCH_HTTP_400`으로 실패했다.
- 무문서 요청에서 query `require_alias=true`와 action `require_alias=true`는 `no requests added`, `_require_alias=true`는 unknown parameter로 거절됐다. 문서 저장 없이 필드 호환성을 확인한 근거다.
- action metadata의 잘못된 접두사를 수정하고 계약 대역 검사를 강화했다. HTTP 오류 type/거절 필드의 제한된 진단을 추가했다. 원래 failed 기록을 보존하고 같은 논리 인덱스에 새 버전을 만드는 명시적 재검증 옵션을 추가했다.
- Python 도구 6개는 로컬 통과다. 이번 수정의 Go·실제 bulk·seal·활성화 성공은 대기 중이다. [재개 절차](../knowledge-platform/testing-indexing.md#bulk-recovery)를 따른다.

## 2026-09-21 — bulk 수정 적용 후 실제 48개 색인·활성화 확인

- 사용자 로그에서 bulk 패치 11개 적용, Python 도구 6개, Docker tests target의 Knowledge 전체 Go 검사, Knowledge service 빌드·기동을 확인했다.
- 이전 실패 journal이 삭제되어 `--retry-failed-from` 시도는 파일 읽기에서 중단됐다. 해당 옵션 없이 새 journal로 실행했으므로 이번 성공은 같은 실패 인덱스의 새 버전이 아닌 새 논리 인덱스다.
- `protecting → indexing → ready`, `status: passed`, 48/48개, 1,024차원, Data 참조 `held`, 생성 버전과 활성 버전 일치를 확인했다. 논리 인덱스는 `aafe22b7-852b-424e-afaa-58fa8c1d739f`, 활성 버전은 `150826c2-1f4c-495c-8e07-54aacaa1a245`다.
- 기존 Model 배포 `44247297-5184-4a36-9225-7791c80d3add`와 runtime을 재사용했다. 성공 journal `~/workspace/indexing_check_bulk_fix_20260921.json`을 보관한다.
- [확인 기록](../knowledge-platform/acceptance.md#k04)과 결과 JSON에 receipt·계보·검증 한계를 기록했다. 다음은 K05 외부 질문 벡터 검색이다. 실제 삭제·참조 해제, 두 청커 비교·검색 품질은 별도로 남긴다. 이번 갱신은 문서·검증 기록만 바꾸며 서비스 재빌드·재기동은 필요 없다.

## 2026-09-21 — K04 문서 적용 확인, K05 검색 소스 구현

- 사용자 로그에서 K04 성공 기록 문서 패치 `APPLIED: 18 files`를 확인했다. K04 실제 48개 저장·활성화와 원래 임베딩 runtime 재사용 근거를 유지했다.
- K05 계약 문서를 먼저 확정하고 기존 IndexingService에 동기 검색 RPC/REST를 추가했다. 명시적 버전·공간·차원·정규화·필터·개수 검증과 후보 단계 Lucene filter, 출처·품질·점수 반환을 구현했다.
- 검색은 공유 row lock을 부모→버전 순서로 유지하여 물리 삭제와 조율한다. timeout·부분/잘못된 응답은 오류이며 다른 버전·공간·전체 범위로 fallback하지 않는다.
- 화면에서 고정 버전 조회, Model query embedding, Knowledge search를 별도로 호출한다. Gateway의 화면용 Model 전달은 공식 API 두 경로와 query 한 개만 허용한다. 도메인 검색에는 Model 호출이 없다.
- Python 검색 도구 8개·기존 색인 도구 6개, JS 검색/색인/청킹 요청 흐름 3개 suite와 문법 확인이 통과했다. 변경 Go 15개 파일의 문법 파싱은 타입 검사·컴파일을 뜻하지 않는다. Go/Docker가 없는 작성 환경에서 서버 Go·DB·실제 검색·브라우저 렌더링은 미실행이다.
- [서버 적용·검색 확인](../knowledge-platform/testing-search.md)은 service·Gateway 두 개 교체와 기존 인덱스 재사용이다. 질문 한 건의 벡터를 검색/거절 검사에 재사용하고 문서 임베딩·재색인은 하지 않는다. 실제 검색 인수 후 K06~K07로 이어간다.

## 2026-09-21 — K05 서버 적용·Go/DB·실제 검색 확인

- 사용자 로그에서 K05 패치 38개 적용, Python 검색 도구 8개, Knowledge 전체 Go 검사와 PostgreSQL 검색 통합 suite를 확인했다. 두 서비스 이미지 빌드·기동도 성공했다.
- 실제 검색은 기존 48개 청크에서 후보/반환/필터 결과 각 5개, 1,024차원, query 추론 1회·인덱스 변경 0회다. 명시적 버전·공간·무결성·순서·후보 필터와 네 가지 거절 검사를 통과했다.
- 원래 임베딩 배포/runtime과 인덱스를 재사용했다. 기존 needs_ocr는 유지한다. 도구 한 번의 전체 경과 0.246초는 검색 지연 벤치마크나 품질 판정이 아니다.
- [K05 확인 기록](../knowledge-platform/acceptance.md#k05)과 간결한 receipt를 보존했다. 실제 브라우저 확인·검색 품질·실제 삭제/참조 해제·두 청커 비교는 별도로 남긴다.
- 다음 구현은 K06 Model reranker profile/추론 API, 이후 K07 Knowledge 선택적 rerank다. 이번 후속 패치는 문서·검증 요약만 갱신하며 서비스 재빌드·재기동은 필요 없다. 이 문서 패치의 서버 적용은 아직 확인하지 않았다.

## 2026-09-21 — Model reranker·Knowledge 선택적 재정렬·실제 평가

- K06 Model profile/점수 API·화면과 K07 Knowledge 선택적 rerank의 Go·DB·실제 20→5 검색을 확인했다. [Model 인수](../knowledge-platform/acceptance.md#k06), [Knowledge 인수](../knowledge-platform/acceptance.md#k07).
- K08 기존 structure와 개선 structure/recursive의 같은 원문·12문항 비교를 확인했다. 개선 structure ON 12/12, recursive ON 최초 오류 1건은 유지했다. 단일 재확인 성공을 최초 오류의 원인 해결로 처리하지 않았다. [평가 기록](../knowledge-platform/acceptance.md#k08).
- 평가기 합계 비교·후보 동일성 진단을 보완했고 진단 패치 17개 파일/서버 37개 검사를 확인했다. PDF/OCR/제목 구조 판별은 Data 소유로 확정했으며 Knowledge에는 제목 추정 규칙을 추가하지 않았다.
- Model 중지 배포 재배포 UI를 구현·로컬 검사했으나 실제 재배포 성공 확인은 별도다.

## 2026-09-22 — 수명주기·HNSW·화면·실패 복구 확인

- 전용 두 버전의 물리 인덱스 삭제·Data 참조 해제·기준 보존을 실제 확인했다. 기존 검색 화면 ON/OFF·입력 오류·관리 선택 전환도 사용자 확인했다. [수명주기 기록](../knowledge-platform/testing-lifecycle-20260921.md).
- HNSW m/ef_construction 선택 입력과 확정 설정 저장·평가 기록을 추가했고 서버 Go/DB·서비스 교체를 확인했다. 개편 화면에서 test1의 custom 32/256·42/42 ready·활성화·ON 검색 20→5·295ms를 확인했다. 원시 mapping 독립 조회와 새 콘솔 OFF는 별도다. [화면 기록](../knowledge-platform/console-ux-20260922.md).
- 격리 Job/HTTP/Reconciler/DB 실패·복구·경쟁 검사를 추가하고 사용자 서버에서 전체 Go 15개 테스트 패키지, PostgreSQL service/app 최상위 17개·skip 0을 확인했다. 실제 노드 장애 주입과는 구분한다. [검사 기록](../knowledge-platform/testing-failure-recovery-20260922.md).

## 2026-09-22 — 전체 업로드 검토·세션 마감

사용자 전체 업로드의 기존 전달 소스는 직전 검증 완료본과 변경·누락 없이 일치했다. 현재 문서에 반복되던 과거 단계 설명과 오래된 API 초안/가상 디렉터리를 구현 상태에 맞춰 정리했다. 구형 UI 테스트의 문구·UUID·진입 경로도 갱신했다.

청크 미리보기·인덱스 관리의 localStorage 쓰기 오류가 무시되는 문제를 대역 테스트로 재현하고 수정했다. 저장 실패 시 요청을 보내지 않고, 응답 이후 보관 기록 제거가 실패해도 원래 요청 키/본문을 유지한다. [마감 검토](../testing/session-closeout-20260922.md)에 통과·건너뜀·환경 제약을 분리했다. 이 수정의 서버 적용은 아직 확인 전이며 Gateway만 교체하면 된다. 후속 개발은 Agent MCP 검색 Tool 계약 설계다.


## 2026-09-22 — 문서 파일 통합

완료된 패치별 안내와 중복 계약/검사를 기능별 문서로 합쳤다. 프로젝트 Markdown 121→99개, 기존 23개 삭제·검증 기록 1개 추가다. 서버 증거·실패·한계를 보존하고 상대 링크 및 JSON의 문서 참조만 수정했다. 실행 코드는 이번에 바꾸지 않았다. [통합 기록](../testing/session-closeout-20260922.md#documentation-consolidation). 삭제 보호 적용기와 최신 전체 소스를 제공하며 서버 반영은 아직 확인 전이다.
