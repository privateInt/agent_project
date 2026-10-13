# 질문 벡터 검색·선택적 rerank 계약

2026-09-21. 이 계약을 먼저 확정한 뒤 K05 소스를 구현했다. K04의 기존 인덱스와 mapping을 사용하며 질문 임베딩은 호출자가 Model Platform에 요청한다. rerank off 검색은 모델 추론을 호출하지 않는다. K07은 [선택적 rerank 계약](api-search.md#rerank)에 따라 질문 원문과 후보를 Model에 전달하며 모델 배포·재색인은 실행하지 않는다. 로컬 검사에 이어 서버 Go·PostgreSQL 통합·실제 Model query embedding→OpenSearch 검색을 사용자 로그로 확인했다. [K05 확인 기록](acceptance.md#k05)과 [K07 재정렬 검색 확인](acceptance.md#k07)을 기준으로 하며, [적용·검증](testing-search.md) 절차의 불필요한 재실행은 하지 않는다. 이후 [화면 확인](console-ux-20260922.md)과 [제한된 청커 비교](acceptance.md#k08)도 기록했으며 일반 품질 보장은 별도다.

## 요청과 버전 고정

`POST /api/indexes/{index_id}/search`는 동기 읽기 API다. 내부는 기존 `IndexingService.Search`를 사용한다. 요청은 JSON 객체 하나이며 중복 키·알 수 없는 필드·null·쿼리 문자열을 거절한다. 본문 상한은 64 KiB다.

| 필드 | 규칙 |
|---|---|
| `index_version_id` | 필수 UUID. 해당 논리 인덱스의 ready·참조 held 버전 |
| `embedding_space_id` | 필수. 선택 버전의 공간 ID와 일치해야 함 |
| `query_embedding` | 필수 단일 숫자 배열. 현재 1,024차원, 유한값, L2 norm 오차 0.001 이내 |
| `top_k` | 생략 시 5, 정수 1..50 |
| `candidate_k` | 정수 top_k..100. off는 생략 시 top_k, on은 명시 필수 |
| `filters` | 생략 또는 객체. 아래 명시한 필드만 허용 |
| `rerank` | 생략 또는 `{"enabled":false}`는 벡터 검색. on은 enabled/deployment_id/expected_reranker_profile_id 필수 |
| `query_text` | rerank on에서만 필수. raw UTF-8 1..8KiB, 공백만/NUL 거절 |

K07부터 `query_text`는 rerank on에서만 받는다. 이 필드로 질문 임베딩을 생성하지 않으며 기존 query_embedding도 필수다. 다른 공간의 같은 차원 벡터, 0 벡터, 문자열·null·NaN/Infinity, 여러 벡터, 원문만 보낸 요청은 거절한다. 서버가 질문 prefix를 붙이거나 벡터를 정규화하지 않는다. 공간 ID는 호환성 계약이며 입력 벡터의 실제 생성 모델을 증명하는 인증 수단은 아니다.

호출자는 `GET /api/indexes/{id}`로 active_version_id를 읽고 `GET /api/index-versions/{version_id}`의 embedding_profile을 고정한다. 그 profile의 배포에 Model 공식 `GET /api/deployments/{id}/embedding-profile`, `POST /api/deployments/{id}/embeddings` (`role=query`, raw text, expected_embedding_space_id)로 요청한다. 응답 공간·차원·역할·입력 ID를 대조한 뒤 위 검색 API를 호출한다. 같은 공간의 재배포는 허용하지만 다른 공간으로 자동 전환하지 않는다.

active가 바뀌어도 명시한 이전 ready 버전을 검색한다. 새 활성 버전을 다시 해석하거나 fallback하지 않는다. 논리 인덱스가 deleting/deleted이거나 지정 버전이 ready가 아니면 거절한다. 검색은 부모→버전 순서의 PostgreSQL 공유 row lock을 유지하며 실행하므로 물리 삭제·상태 전환과 직렬화된다. 이미 시작된 검색이 끝난 뒤 삭제가 진행될 수 있으며, 삭제 접수 완료 후 새 검색은 거절한다.

## 검색 범위

`filters.source_document_ids`, `filters.canonical_ids`는 각각 중복 없는 UUID 1..20개다. 선택 버전에 포함된 문서만 지정할 수 있다. `filters.kinds`는 `text`, `table`, `code` 중 중복 없는 1..3개다. 같은 배열 안은 OR, 다른 필드 사이는 AND다. 빈 배열·null·알 수 없는 필터·버전 밖 문서를 거절한다. 문서 조건의 교집합이 비면 `SEARCH_EMPTY_SCOPE` 오류다. `filters` 생략/빈 객체는 해당 버전 전체를 뜻한다. 현재 filter는 문서 범위 선택이며 사용자 권한 시스템을 대신하지 않는다.

조건은 Lucene k-NN의 `filter`에 넣어 후보 검색부터 적용한다. 전체 후보를 얻은 뒤 일부를 버리는 방식으로 대체하지 않는다. 유효한 문서 범위에 해당 kind 청크가 없는 경우는 정상 빈 결과이며 `empty_reason=no_matches`다. [OpenSearch k-NN query](https://docs.opensearch.org/latest/query-dsl/specialized/k-nn/index/).

## 응답과 실패

응답은 실제 index/version/space, candidate_k/top_k·실제 후보/반환 수, 적용한 filters, rerank 적용 여부·단계별 시간을 포함한다. off는 `rerank={requested:false,applied:false,status:"disabled"}`를 유지한다. on은 profile identity와 실제 적용/후보 없음 상태를 반환한다. `results`는 rank·retrieval_rank·retrieval_score와 원래 청크의 ID/본문/source_document_id/canonical_id/source_spans/quality/structure를 반환한다. on은 `rerank_score`·`rerank_input_tokens`도 반환하며 최종 rank와 retrieval_rank는 다를 수 있다. 질문 벡터와 저장 벡터는 반환하지 않는다.

점수는 현재 mapping의 **OpenSearch Lucene cosinesimil 점수**, `(1 + cosine_similarity) / 2`이며 클수록 가깝다. 확률이나 reranker 점수가 아니다. 점수 내림차순, 동점은 chunk_id 오름차순으로 정렬한다. 근사 검색이므로 동일 점수 후보 집합 전체의 완전성이나 정확 검색과 동일한 Recall을 보장하지 않는다. [점수 의미](https://docs.opensearch.org/latest/mappings/supported-field-types/knn-spaces/).

검색 전체 deadline은 off 15초, on 60초이며 Model profile 상한 5초·rerank 상한 45초를 부모 deadline 안에서 적용한다. OpenSearch query timeout은 8초다. 동시 처리 슬롯은 서비스당 8개이며 대기도 전체 deadline에 포함한다. backend/최종 JSON 응답은 각각 2 MiB로 제한한다. 초과 시 개수를 조용히 줄이지 않고 오류를 반환하며 호출자가 candidate_k/top_k를 낮춰 재요청한다. timeout·부분 shard 실패·조기 종료·누락/중복/범위 밖 hit·본문 해시/출처 불일치는 성공으로 반환하지 않는다. 물리 mapping 소유권도 확인한다. OpenSearch 요청은 명시적 물리 인덱스 한 개와 `allow_partial_search_results=false`를 사용한다. [Search API](https://docs.opensearch.org/latest/api-reference/search-apis/search/).

| HTTP | 의미 |
|---|---|
| 400 | 요청 형식·벡터·차원·필터/빈 범위 오류, 재정렬 입력/모델 토큰 한도 초과 |
| 404 | 논리 인덱스/버전이 없거나 다른 인덱스 소속 |
| 409 | 공간 불일치, 인덱스 삭제 중/삭제됨, 버전 미준비/삭제됨 |
| 413 | 요청 또는 응답 크기 상한 초과 |
| 502 | 검색 결과·카탈로그 정합성 위반 |
| 503 | backend 미설정·접속 실패·물리 인덱스 누락·부분 결과 |
| 504 | 전체 deadline 또는 backend timeout |

## 검색 확인 화면

화면은 버전 선택 → Model query embedding → 공개 Knowledge search API를 각각 호출한다. 브라우저의 다른 포트 CORS 문제를 피하기 위해 Gateway에 `/console/model/deployments/{id}/embedding-profile`, `/console/model/deployments/{id}/embeddings`, `/console/model/deployments/{id}/reranker-profile` 전달 경로와 이름 기반 선택용 배포 목록·모델 버전·등록 모델 GET을 둔다. 고정된 Model Gateway의 공식 REST API로 전달하며 query 1개만 허용한다. 임의 URL·배포 생성·문서 임베딩·검색 결합 endpoint는 제공하지 않는다. Knowledge 도메인 검색은 이 화면 경로를 호출하지 않으며 on일 때 Model RerankingService gRPC를 호출한다.

화면 열기/목록 조회/버전 선택은 추론을 실행하지 않는다. 사용자가 검색을 누를 때만 질문을 보내며 prefix는 Model이 적용한다. 질문·벡터·검색 결과는 localStorage나 검사 요약 파일에 저장하지 않는다. 원문·출처는 텍스트로 렌더링하고 기존 원본 품질 경고를 표시한다.


<a id="rerank"></a>

## 선택적 재정렬

기존 검색 API의 `rerank` 옵션이며 Model은 관련성 점수를, Knowledge는 후보 선택과 최종 순위를 담당한다. 질문 임베딩은 호출자가 제공한다.

### 요청 예시와 처리

기존 `POST /api/indexes/{index_id}/search`를 사용한다. rerank 생략 또는 `{"enabled":false}`는 기존 벡터 검색이다. 켠 요청은 다음 필드를 추가한다.

```json
{
  "query_text": "이 자료에서 가죽 생산의 환경 영향을 어떻게 평가했나요?",
  "candidate_k": 20,
  "top_k": 5,
  "rerank": {
    "enabled": true,
    "deployment_id": "cc64a683-a69f-48a5-836e-9f52f0255cc6",
    "expected_reranker_profile_id": "rp_2717f05094bb6068f3edef2c93025792da740785f956b97dc07d06c29f228ff1"
  }
}
```

위는 추가 필드의 예시이며 기존 version/space/query_embedding도 필수다. `query_text`는 trim/접두어 변환 없이 UTF-8 원문 1..8KiB이며 공백만인 입력·NUL은 거절한다. off 요청에는 query_text를 보내지 않는다. on에서는 candidate_k를 명시하고 `1 <= top_k <= 50`, `top_k <= candidate_k <= 100`을 지킨다. 프로필 ID는 Model의 `GET /api/deployments/{id}/reranker-profile`로 조회한다. false에 배포/profile 등 추가 옵션을 넣으면 오류다. unknown/null/중복 JSON 키는 기존과 같이 거절한다.

### 순위와 응답 검증

1. 인덱스/고정 버전·공간·필터를 검증한다.
2. on일 때만 지정 Model의 profile을 조회하고 expected ID·ready·단일 logit·큰 점수 우선·입력 한도를 확인한다.
3. 기존 OpenSearch 필터 검색에서 candidate_k개까지 가져온다. 벡터 점수 내림차순, 동점 chunk_id 오름차순으로 retrieval_rank를 고정한다.
4. 후보가 있으면 원문 질문과 전체 후보 ID/text를 Model RerankingService에 한 번 요청한다. 재시도·후보 분할/제외·자동 truncation은 없다. profile 전체 identity·후보 ID/index/개수·유한 점수·토큰 수를 검사한다.
5. rerank_score 내림차순, 동점 retrieval_rank 오름차순으로 최종 top_k를 반환한다. 원래 retrieval_rank·retrieval_score·본문·출처·품질을 유지한다.

`rerank`는 requested/applied/status, deployment_id/reranker_profile_id와 모델 버전·runtime image/container, score_kind/score_direction·입력 한도를 담은 profile을 반환한다. `results[].rerank_score`와 `rerank_input_tokens`는 적용한 경우에만 제공한다. 점수는 확률이 아니며 벡터 점수와 합산하지 않는다. timings에 profile_ms/rerank_ms를 추가한다.

off는 Model profile/추론 모두 0회이고 기존 disabled 응답을 유지한다. 정상 빈 후보는 profile 검증 후 추론 0회, `requested:true, applied:false, status:skipped_no_candidates`로 반환한다. 입력과 profile이 잘못된 요청을 빈 검색 결과로 숨기지 않는다.

### 추가 실패 조건

Model 계약의 질문+후보 합계 128 KiB와 각 pair 512토큰 초과는 오류다. Knowledge는 바이트를, Model은 토큰을 검사한다. 부모 인덱스→버전 공유 잠금을 응답 검증까지 유지하며 삭제·활성화는 검색 deadline까지 기다릴 수 있다. 취소/deadline은 Model gRPC에 전파한다.

요청한 rerank가 실패하면 오류를 반환한다. 벡터 결과로 성공을 대신하지 않으며 후보 분할·제외·자동 truncation·재시도를 하지 않는다. 입력/길이는 400, 배포 없음은 404, 미준비/profile 변경은 409, 응답 이상은 502, 연결 실패는 503, timeout은 504다. 알려진 코드와 고정 설명만 반환하며 질문·후보·원시 backend 오류를 로그/요약에 남기지 않는다.
