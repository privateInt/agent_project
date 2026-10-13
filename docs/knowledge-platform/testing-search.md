# 검색·선택적 재정렬 검증

계약은 [검색 API](api-search.md)를 따른다. 사용자 서버의 벡터 검색과 선택적 재정렬 연동은 [K05](acceptance.md#k05)·[K07](acceptance.md#k07), 제한된 품질 비교는 [K08](acceptance.md#k08)에 보존한다. 아래 명령은 관련 코드를 변경했거나 의도적으로 다시 검증할 때만 사용한다. 문서 통합 때문에 서비스나 추론을 다시 실행하지 않는다.

## 코드 변경 후 회귀·서비스 반영

기존 `.env.indexing`·CA·인증 설정을 유지한다. DB 검사에 Model/OpenSearch는 대역을 사용하며 실제 연동은 아래 live 도구로 구분한다.

```bash
cd ~/workspace/agent_project
(
  set -euo pipefail
  python3 -m unittest discover -s src/platform/knowledge/tests -p 'test_*search_tools.py' -v
  docker build --target tests -t knowledge-platform-tests:search ./knowledge_platform
  python3 src/platform/knowledge/tests/run_rerank_db_checks.py
  cd knowledge_platform
  docker compose --env-file .env --env-file .env.indexing \
    -f docker-compose.yml -f indexing.compose.yml build knowledge-service knowledge-gateway
  docker compose --env-file .env --env-file .env.indexing \
    -f docker-compose.yml -f indexing.compose.yml up -d --no-deps knowledge-service knowledge-gateway
)
```

Go tests target만 실행하면 DB 설정이 없는 검사가 skip될 수 있다. `run_rerank_db_checks.py`는 격리 PostgreSQL schema에서 K05 검색/잠금과 K07 재정렬을 실행하고 정리한다. 벡터 검색만 따로 진단할 때는 `run_search_db_checks.py`를 쓸 수 있다. 실패하면 서비스 교체 전에 중단한다. 내부 rerank 주소는 `KNOWLEDGE_MODEL_RERANKING_GRPC_ADDRESS`(기본 model-deployment:9000), 화면 Model origin은 `KNOWLEDGE_MODEL_GATEWAY_URL`(기본 http://model-gateway:8000)이다. vLLM 동적 포트를 이 설정에 넣지 않는다.

## 벡터 검색 실제 확인

아래 명령은 기존 버전을 읽고 **raw 질문 한 건만 Model에서 임베딩**한다. 같은 벡터를 재사용해 전체 검색·선택 문서/kind 필터·다른 공간/차원/빈 필터/알 수 없는 필터 거절을 확인한다. 인덱스 생성·빌드·활성화·삭제는 실행하지 않는다.

```bash
cd ~/workspace/agent_project
python3 src/platform/knowledge/tests/live_search.py \
  --index-id aafe22b7-852b-424e-afaa-58fa8c1d739f \
  --index-version-id 150826c2-1f4c-495c-8e07-54aacaa1a245 \
  --question '이 연구에서 가죽 제품의 생애주기 환경 영향을 어떻게 평가했나요?' \
  --top-k 5 \
  --summary-file "$HOME/workspace/search_check_20260921.json"
```

Knowledge 기본 주소는 `http://127.0.0.1:15100`, Model 기본 주소는 `http://127.0.0.1:13000`이다. 필요하면 `--knowledge-url`, `--model-url`로 바꾼다. 이 예제는 Model 공식 REST API를 직접 호출하며 화면 전용 전달 경로에 의존하지 않는다.

성공 기준: `status: passed`, 요청 버전·공간 일치, `dimension: 1024`, `indexed_chunks: 48`, `query_embedding_calls: 1`, `index_mutations: 0`, 비어 있지 않은 검색/필터 결과와 네 가지 거절 검사 통과. 질문·벡터·본문·출처 metadata 전문은 요약 파일에 저장하지 않고 ID·개수·품질 상태·판정만 기록한다. 이 결과는 API 연동 확인이며 Recall/MRR/nDCG 등의 검색 품질 통과가 아니다.

원래 배포가 종료돼도 호환 공간의 다른 ready 배포를 명시적으로 선택할 수 있다: `--deployment-id <배포 UUID>`. 공간이 다르면 재색인이나 모델 교체를 자동 수행하지 않고 실패한다. 버전/원본이 삭제됐으면 현재 상태를 확인하며 과거 UUID를 임의 대체하지 않는다.

## 화면 확인

지식 검색에서 지식과 기본 검색 버전을 선택하고 질문을 입력한 뒤 검색한다. 보존 버전은 검색 버전 변경에서 선택한다. 재정렬을 켤 때는 배포 이름으로 모델을 고르고 후보 수를 지정한다. 결과에서 원문·출처·품질 경고, 원래/최종 순위, 점수·처리 시간을 확인한다. 화면 사용법은 [콘솔](console-ux-20260922.md)을 따른다.

조회·버전 선택·profile 조회는 추론하지 않는다. 검색 버튼에서 Model 질문 임베딩과 Knowledge 검색을 수행한다. 입력/버전/조건을 바꾸면 이전 요청을 취소하고 오래된 결과를 표시하지 않는다. 원문의 needs_ocr는 검색 성공 뒤에도 유지한다.

## 검색 오류

| 결과 | 확인할 사항 |
|---|---|
| 검색 404/405, Search Unimplemented | 새 Gateway·service 이미지가 둘 다 교체됐는지 확인 |
| SEARCH_NOT_CONFIGURED | 기존 두 env/Compose 파일로 service를 기동했는지 확인 |
| SEARCH_SPACE_MISMATCH | 선택 버전의 profile로 query embedding을 생성했는지 확인 |
| SEARCH_VERSION_UNAVAILABLE / SEARCH_INDEX_UNAVAILABLE | 명시한 버전·인덱스의 ready/삭제 상태 확인 |
| SEARCH_INVALID_FILTER / SEARCH_EMPTY_SCOPE | 비어 있거나 서로 교차하지 않는 문서 범위 확인 |
| SEARCH_RESPONSE_TOO_LARGE | candidate_k/top_k를 낮춰 재요청. 서버는 결과를 조용히 잘라 성공 처리하지 않음 |
| SEARCH_BACKEND_UNCONFIRMED / SEARCH_RESPONSE_INVALID / SEARCH_TIMEOUT | 마지막 오류 코드와 서비스 상태 확인. 부분 결과를 정상으로 사용하지 않음 |
| 화면 Model 연결 실패 | Model Gateway의 준비 상태·공유 네트워크·고정 origin 확인 |


<a id="rerank"></a>

## 재정렬 실제 확인

2026-09-21 실행은 `status: passed`다. 후보 20개·반환 5개이며 원래 벡터 순위 1·4·2·7·10이 최종 1~5위가 됐다. Knowledge on 검색 310 ms 중 rerank 구간은 194 ms였다. 질문 임베딩은 검색 시간 밖이며 단일 요청 시간은 성능 보장이나 품질 개선 근거가 아니다.

```bash
cd ~/workspace/agent_project
python3 src/platform/knowledge/tests/live_rerank_search.py \
  --index-id aafe22b7-852b-424e-afaa-58fa8c1d739f \
  --index-version-id 150826c2-1f4c-495c-8e07-54aacaa1a245 \
  --reranker-deployment-id cc64a683-a69f-48a5-836e-9f52f0255cc6 \
  --question '이 연구에서 가죽 제품의 생애주기 환경 영향을 어떻게 평가했나요?' \
  --candidate-k 20 --top-k 5 \
  --summary-file "$HOME/workspace/knowledge_rerank_check_20260921.json"
```

질문 embedding은 1회만 실행하고 같은 벡터로 후보 20개를 반환하는 off 검색과 최종 5개를 반환하는 on 검색을 수행한다. 실제 후보 수는 색인/필터에 따라 작을 수 있다. ID·원문/출처/품질·기존 벡터 점수/순위 보존, 재정렬 점수/토큰/최종 순위, 빈 질문과 다른 profile 거절을 대조한다. 인덱스/배포 수명주기 변경은 0회다. 도구는 모든 off 후보와 비교하기 위해 candidate_k를 50 이하로 제한하며 공개 API는 100까지 지원한다.

`status: passed`는 API 연동 확인이다. 순위가 off와 같아도 정상일 수 있으며 품질 향상을 뜻하지 않는다. 근사 검색에서 두 요청의 후보/벡터 순위가 달라지면 도구는 비교 불확실 오류로 중단한다. 실제 대표 질문의 품질 평가는 K08이다.

기본 Gateway 주소는 Knowledge `http://127.0.0.1:15100`, Model `http://127.0.0.1:13000`이다. 필요하면 `--knowledge-url`/`--model-url`을 지정한다. 같은 공간의 새 embedding 배포는 `--embedding-deployment-id`로 명시할 수 있다. 현재 정상 배포를 검사 때문에 중지·재배포하지 않는다.

### 재정렬 오류

| 오류 | 확인할 내용 |
|---|---|
| RERANK_INPUT_TOO_LONG | 질문+후보 중 하나가 512토큰을 초과했다. 문자 청킹의 1,000자와 토큰 수는 다르다. 문제 입력을 검토하며 성공할 때까지 자동 제외/잘라내기를 하지 않는다. |
| RERANK_INPUT_TOO_LARGE | 질문+후보 합계가 Model 바이트 한도를 넘었다. candidate_k와 입력을 검토한다. |
| RERANK_PROFILE_MISMATCH | 사용자가 선택한 profile과 현재 배포가 다르다. profile을 다시 조회하고 의도한 모델인지 확인한다. |
| RERANK_NOT_READY / SEARCH_RERANK_UNAVAILABLE | 지정한 배포의 준비·중지 상태를 확인한다. |
| SEARCH_RERANK_RESPONSE_INVALID | 점수·후보 ID·runtime identity 응답을 대조하지 못했다. 결과를 성공으로 사용하지 않는다. |
| RERANK_UNAVAILABLE / RERANK_TIMEOUT | Model gRPC 연결·모델 준비 상태·검색 전체 60초와 추론 상한 45초를 확인한다. |


<a id="verification-history"></a>

## 구현 당시 회귀 범위

질문 임베딩은 Model 공식 API를 사용한다. Knowledge 검색은 명시적 버전·공간·벡터·필터를 검증하고 기존 OpenSearch 인덱스를 읽는다. 기존 서비스 두 개(`knowledge-service`, `knowledge-gateway`)를 교체하며 새 상시 서비스·Job·GPU runtime·DB schema·mapping은 추가하지 않는다. Model/Data/인프라와 문서 임베딩·색인 빌드는 이번 적용으로 재실행하지 않는다.

| 검사 | 확인한 범위 |
|---|---|
| Python | 검색 도구 8개 + 기존 색인 도구 6개 = 14개 통과. 요청 순서·단일 query 추론·버전 고정·응답/필터·거절·요약 보관 대역 검사 |
| JavaScript | 검색·기존 색인·청킹 요청 흐름 3개 suite 통과. DOM/fetch 대역이며 실제 브라우저 렌더링과 구분 |
| 정적 확인 | 변경 Python AST·JS 문법·Compose YAML, 변경 Go 15개 파일의 tree-sitter 문법 확인 |
| 서버 후속 확인 | 사용자 로그로 Go 전체 검사·PostgreSQL 검색 통합·두 서비스 빌드/기동·실제 검색 통과를 확인. 작성 환경의 Go 문법 확인과 별도 근거 |

Knowledge service가 Model Deployment의 공식 `RerankingService` gRPC를 호출한다. Gateway는 기존 검색 요청 전달과 화면용 reranker-profile GET을 지원한다. 새 추론 컨테이너·인덱스·DB schema·mapping·학습/배포 작업은 추가하지 않는다. `rerank` 생략/false는 기존 벡터 검색이고 on 요청은 후보 점수를 대조해 최종 순위를 만든다. 질문 임베딩은 호출자가 Model에 요청한다.

| 검사 | 이번 확인 |
|---|---|
| Python | 새 K07 도구 6개 + 기존 검색 도구 8개, 총 14개 통과 |
| JavaScript | 검색·색인·청킹 요청 흐름 3개 suite 통과. 새 off/on/profile·점수·오류·옵션 변경 검사 포함 |
| 정적 확인 | Knowledge Go 87개 tree-sitter 문법 확인, Python AST·JS 문법·Compose 구조·Model 원본 protobuf와 메시지 일치 확인 |
| Go·DB·GPU | 로컬에는 Go/Docker가 없어 미실행. 이후 사용자 서버의 Go·PostgreSQL 통합과 별도 실제 API 검증 통과를 확인 |

Go 검사 소스에는 입력/바이트/profile 제한, 결정적 동점 처리, 원본 출처 보존, 누락/중복/index/NaN/Inf/token/runtime 변경 거절, off 호출 0회, empty 추론 생략, 오류 시 fallback 금지, 취소/deadline 전파와 rerank 중 삭제 잠금 검사를 추가했다. 작성된 검사와 실제 통과 기록은 구분한다. [로컬·서버 요약](../../src/platform/knowledge/tests/results/rerank-k07-20260921.json), [실제 API 결과](../../src/platform/knowledge/tests/results/rerank-search-live-20260921.json).
