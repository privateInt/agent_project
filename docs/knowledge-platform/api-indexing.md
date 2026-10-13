# K04 인덱스 빌드와 관리 계약

2026-09-22 갱신. K04 Go·DB 검사와 실제 48개 저장·활성화는 [사용자 서버 결과](acceptance.md#k04)로 확인했다. 이 문서는 색인·관리 계약이며 새 [K05 검색 계약](api-search.md)과 [검색 적용·검증](testing-search.md)은 별도로 관리한다.

## 책임과 실행

Knowledge의 `IndexingService`가 논리 인덱스·버전·Data 참조·OpenSearch 쓰기를 소유한다. 기존 Operation/Outbox/Dispatcher가 CPU `indexing.build` Job을 한 번 승인한다. Job은 저장된 청크 배치를 받고 **Model EmbeddingService**에 `role=document`로 요청한 뒤 결과를 Knowledge에 등록한다. 동적 모델 endpoint나 OpenSearch 인증 정보는 Job에 전달하지 않는다. 임베딩 runtime은 기존 Model 배포를 사용한다.

상시 서비스 수는 유지한다. 일회성 indexing Job 이미지만 추가하며 모델 가중치를 로드하지 않는다. OpenSearch에 연결하는 서비스는 `knowledge-service` 하나다. Job과 Model은 기존 `ops-infra`, Knowledge 서비스는 추가로 기존 `opensearch-net`에 연결한다. TLS의 CA·호스트 이름을 검증한다.

## 저장 모델

| 자원 | 고정하거나 관리하는 값 |
|---|---|
| LogicalIndex | UUID, 표시 이름, 기본 임베딩 배포 ID, active_version_id, revision, active/deleting/deleted |
| IndexVersion | 부모 버전, ChunkSet ID/hash, 입력 snapshot·청커 정책, 포함할 canonical ID, Model profile, indexing_config, 상태·개수·참조 상태 |
| Index batch | 버전·offset, 응답 hash, 청크 수, 저장 확인 여부 |
| Data reference owner | 버전 UUID, 정렬된 canonical ID/hash, held/released, 해제 tombstone |
| 물리 인덱스 | 버전별 UUID 이름, 소유 metadata, 고정 mapping, 청크별 벡터·본문·원본 spans·품질 |

표시 이름·기본 배포는 생성 시 정한다. 이번 버전은 별도의 이름/기본 설정 편집 API를 제공하지 않으며, 빌드마다 다른 embedding_deployment_id를 명시할 수 있다. 활성화 시 기본 배포를 암묵적으로 바꾸지 않는다. 향후 검색 호출자는 **선택한 IndexVersion의 profile**을 사용한다.

최대 문서 20개·청크 20,000개의 기존 ChunkSet 계약을 유지한다. 처음 지원하는 profile은 `snowflake-ko-v1`, 1,024차원·CLS·L2다. 배치는 최대 16개이며 Model의 입력 바이트 제한과 1.5 MiB 청크 metadata 한도 중 먼저 도달한 곳에서 나눈다. 토큰 초과는 Model 오류로 빌드를 실패시키며 글자를 임의로 자르지 않는다. 청커의 글자 수와 모델 토큰 수는 다른 제한이다.

같은 공간이라도 이번 빌드 중 deployment/model version/runtime container 등 고정 profile 필드가 달라지면 실패한다. 다른 배포로 전환하려면 새 버전을 만든다. 원본의 needs_ocr/needs_review·구조·spans를 보존하고 잘못 인식된 내용을 자동 보정하지 않는다.

## 외부 REST

Knowledge Gateway 기본 포트는 15100이다. 목록은 limit 1..100과 필터에 묶인 cursor를 사용한다. 생성과 빌드는 `Idempotency-Key`가 필수다. 같은 키·본문은 같은 리소스를 반환하며, 같은 키의 다른 본문은 409다. HTTP 202는 완료가 아닌 접수다.

| 메서드·경로 | 의미 |
|---|---|
| POST /api/indexes | 이름·기본 임베딩 배포 ID로 논리 인덱스 생성 |
| GET /api/indexes | 목록, 삭제 상태 포함 |
| GET /api/indexes/{id} | 활성 버전·revision·기본 설정 |
| POST /api/indexes/{id}/builds | 새 버전 접수 |
| GET /api/indexes/{id}/versions | 버전 목록·처리 개수·실패 원인·profile·계보 |
| GET /api/index-versions/{id} | 불변 입력과 현재 상태 |
| POST /api/indexes/{id}/activate | ready 버전 활성화, expected_revision + expected_active_version_id 필수 |
| DELETE /api/index-versions/{id} | 비활성 버전 정리 접수 |
| DELETE /api/indexes/{id} | 전체 삭제 접수, 같은 CAS 조건 필수 |

생성 예:

```json
{"name":"연구 자료","embedding_deployment_id":"44247297-5184-4a36-9225-7791c80d3add"}
```

기존 결과를 재사용하는 빌드 예:

```json
{"chunking_run_id":"6e3eaf24-c2dc-4756-87bb-83468a96c827"}
```

빌드의 선택 입력 `hnsw`로 그래프 생성 설정을 지정한다.

```json
{"chunking_run_id":"32c1c351-4136-44f2-9ece-435157b64037","hnsw":{"m":32,"ef_construction":256}}
```

m은 정수 2~64(기본 16), ef_construction은 정수 16~1024(기본 128)다. 생략·빈 객체·부분 입력은 해당 기본값으로 채우며 부모 버전의 값을 상속하지 않는다. null/소수/알 수 없는 필드/중복 키/범위 초과는 400으로 거절한다. 확정값은 버전의 indexing_config에 저장하며 이후 매핑 검사·평가 기록에도 사용한다. 구형 버전의 필드 생략은 16/128로 해석하고 기존 물리 인덱스는 바꾸지 않는다. 같은 키의 기본값 생략/명시는 같은 요청이고 다른 설정은 409다. [HNSW 상세 계약](api-indexing.md#hnsw)을 따른다.

선택적으로 `canonical_ids`를 지정하면 해당 ChunkSet의 부분집합만 새 버전에 포함한다. 생략하면 전체다. `parent_version_id`는 같은 논리 인덱스의 ready 버전이어야 한다. 자료 제외는 원본/기존 인덱스를 수정하는 대신 남길 자료로 새 버전을 만들고 활성화한다.

새 청킹부터 실행하려면 chunking_run_id 대신 아래 `chunking`을 지정한다. 둘을 동시에 받을 수 없다. 새 청킹 요청에는 별도의 부분집합을 지정하지 않는다.

```json
{
  "chunking": {
    "name": "새 청킹과 색인",
    "canonical_ids": ["157c335f-5436-4338-8078-edb040ebe1ca"],
    "chunker": {"id": "structure_v1", "options": {"max_characters": 1000, "overlap_characters": 100}}
  }
}
```

인덱스 생성은 기본 배포 UUID 형식을 검증한다. **실제 Model ready/profile 확인은 빌드 접수 시** 수행한다. 현재 [화면](console-ux-20260922.md)에서는 이름으로 배포 모델을 선택하고 자료 선택·처리 설정·확인 단계를 거친다. 저장된 청크 재사용 또는 새 청킹을 선택한다. 화면 열기·새로고침은 임베딩을 실행하지 않는다.

## 준비·실패·완료

| 상태 | 의미 |
|---|---|
| protecting | Data 참조 획득·ChunkSet 대기/검증. 재시도 가능한 연결 오류는 유지하며 이유 표시 |
| queued | 입력·참조를 고정하고 Operation 전달을 요청함 |
| indexing | 승인된 프로세스가 배치 단위 임베딩·색인 수행 |
| ready | 모든 배치 확인, write block·refresh·최종 count 검증 성공 |
| failed | 입력 보호 또는 실행 실패. 부분 인덱스·Data 참조는 명시적 삭제까지 보존 |
| deleting | 즉시 새 쓰기/활성화 차단, 물리 인덱스 삭제·Data 참조 해제 재시도 |
| deleted | 물리 인덱스 삭제 확인·Data 참조 해제 확인. 이력은 남음 |

실패가 이전 활성 버전을 바꾸지 않는다. ready가 되어도 자동 활성화하지 않는다. 부모 index row lock와 revision CAS로 동시 활성화를 직렬화하며 ABA도 막는다. 활성 버전 단독 삭제는 거절한다. 전체 삭제는 활성 포인터부터 비우고 삭제 상태를 기록한다. K05 검색은 active/deleted 상태와 명시적 버전을 이 계약에 맞게 확인해야 한다.

벡터는 Model 응답의 ID·순서·개수·차원·유한 숫자·L2 norm·토큰 수·고정 profile을 검사한다. OpenSearch 요청 전에 응답 hash를 영속 기록한다. 같은 offset의 다른 벡터는 거절하며, 완료된 같은 배치는 추가 쓰기 없이 반환한다. Bulk는 명시적 alias를 요구하고 모든 항목의 ID·대상·상태·shard 실패 여부를 확인한다. HTTP 200만으로 완료 처리하지 않는다.

OpenSearch 3.x, Lucene HNSW·cosinesimil, shard 1/replica 1을 유지하고 m/ef_construction은 버전의 확정 설정을 사용한다. 현재 infra 소스는 3.8.0이며 실제 노드 버전은 빌드 준비 때 확인한다. 두 shard copy를 요구하므로 현재 2개 노드가 정상이어야 한다. ready 시 write block을 추가하여 진행 중 쓰기가 종료된 후 불변성을 확정한다. [벡터 mapping](https://docs.opensearch.org/latest/mappings/supported-field-types/knn-vector/), [Bulk 항목·alias](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/), [write block 보장](https://docs.opensearch.org/latest/api-reference/index-apis/blocks/).

재시도는 **같은 Knowledge 요청/같은 벡터 응답**에 한정한다. Model 추론을 자동 반복하거나 죽은 Job을 새 프로세스로 재실행하지 않는다. 최종 도메인 ready 후 Operation 보고가 유실되면 Reconciler가 보정한다. 최종 receipt 없이 프로세스가 종료됐으면 Operation의 재조정 메모로 확인하고 해당 버전을 정리하거나 새 버전을 만든다. 완료로 추정하지 않는다.

## Data 보호와 정리

AcquireKnowledgeReferences는 버전 owner와 canonical hash 목록을 고정한다. Data PurgeRun과 동일한 `purge-files:<refinement_run_id>` lock을 사용하므로 참조 조회와 영구 삭제 사이의 경쟁을 막는다. 공유 원본은 마지막 owner 해제 전까지 purge할 수 없다. 원본 source도 기존 Data의 refinement 참조 fence로 보호된다. 목록 숨김과 물리 purge는 구분한다.

물리 인덱스 삭제 확인 이후 ReleaseKnowledgeReferences를 호출한다. 해제가 실패하면 deleting을 유지하고 유지보수 루프가 다시 시도한다. 존재하지 않는 owner도 released tombstone을 남겨 늦은 Acquire가 보호를 되살리지 못하게 한다. Data가 내려가면 임의로 참조 해제 성공을 가정하지 않는다.

공유 ChunkSet 및 canonical/source 파일은 인덱스 삭제로 지우지 않는다. ChunkSet 자체의 TTL/GC는 별도 보존 정책으로 남긴다. 실패·이전 ready 버전도 자동 TTL 없이 명시적 삭제 대상이며, 이력 row는 보존한다. 참조 RPC는 현재 다른 내부 제어 RPC와 같은 신뢰된 사내망 전제다.

## 관리 화면·실제 삭제 검증 도구

2026-09-21 [화면 보완과 전용 검증](testing-lifecycle-20260921.md)을 추가했다. 인덱스를 바꾸면 상세·작업 대상을 해제하고 늦은 응답을 무시한다. 삭제 API/서비스 정책은 유지한다. 전용 인덱스·두 버전으로 활성 버전 삭제 거절, 비활성/전체 삭제와 공유 참조 보존을 검증하며 OpenSearch GET·Data 조회 RPC로 결과를 독립 확인한다. 실제 서버의 두 버전 삭제·참조 해제·기준 보존은 2026-09-22 사용자 결과로 확인했다. 원본 purge 호출은 이 검증에 포함하지 않았다.


<a id="hnsw"></a>

## HNSW 빌드 설정

POST /api/indexes/{id}/builds의 선택 필드:

```json
{
  "chunking_run_id": "32c1c351-4136-44f2-9ece-435157b64037",
  "hnsw": {"m": 32, "ef_construction": 256}
}
```

| 입력 | 생략 시 | 플랫폼 허용 범위 |
|---|---:|---:|
| hnsw.m | 16 | 정수 2~64 |
| hnsw.ef_construction | 128 | 정수 16~1024 |

범위는 플랫폼의 자원 제한 정책이며 OpenSearch 전체 지원 범위가 아니다. hnsw 생략·빈 객체·일부 필드 생략은 기본값으로 채운다. null·문자열·boolean·소수·알 수 없는 필드·중복 키·범위 초과는 거절한다. REST와 서비스의 직접 gRPC 입력에서 모두 검사하며 잘못된 옵션으로 Model 조회·청킹·버전 생성을 하지 않는다. hnsw는 빌드 입력에만 있으며 검색 요청이나 논리 인덱스 생성 입력이 아니다.

화면의 고급 색인 설정은 접혀 있다. 잘못된 HNSW 입력 안내는 작업 영역에 표시한다. 최신 화면은 [지식 만들기 흐름](console-ux-20260922.md)을 따른다. 빈 입력은 기본값을 사용하며 새 버전에만 적용한다. 저장된 청크 재사용과 새 청킹 입력 모두 지원한다. 부모 버전은 계보만 나타내므로 부모의 HNSW 설정을 암묵적으로 상속하지 않는다. 원하는 값을 이번 빌드에서 지정한다. ready 후 활성화는 기존 명시적 동작을 유지한다.

엔진 lucene, 방법 hnsw, 거리 cosinesimil과 shard 1/replica 1은 유지한다. 차원은 Model profile에서 받는다. ef_search, 엔진/거리 선택, 벡터 재사용·재색인 전용 실행은 이번 범위에 없다. 새 버전 빌드는 현재처럼 청크 문서 임베딩을 다시 요청한다. HNSW 설정만으로 embedding_space_id는 바뀌지 않는다.

[OpenSearch 방법/엔진 문서](https://docs.opensearch.org/latest/mappings/supported-field-types/knn-methods-engines/)에 따라 m·ef_construction은 생성 시 고정된다. 기존 물리 인덱스의 설정을 수정하지 않고 새로운 버전으로 비교한다. 검색 시의 ef_search는 [검색 요청 계약](https://docs.opensearch.org/latest/query-dsl/specialized/k-nn/index/)에 속하며 candidate_k와 구분한다.

### 버전 저장과 재시도

새 IndexVersion의 spec_json에는 확정된 indexing_config를 저장한다.

```json
{
  "indexing_config": {
    "schema_version": "indexing-config-v1",
    "method": "hnsw",
    "engine": "lucene",
    "space_type": "cosinesimil",
    "parameters": {"m": 32, "ef_construction": 256}
  }
}
```

같은 값이 접수 응답·버전 목록/상세·실행 입력에 전달되고 생성/재개/봉인/검색/삭제의 OpenSearch 매핑 검사에 사용된다. 실제 매핑이 저장된 설정과 다르면 소유/매핑 불일치로 중단한다. 기존의 엄격한 소유 확인을 느슨하게 하지 않는다.

필드가 없는 구형 버전만 정확히 16/128로 해석한다. 필드가 있는데 null이거나 불완전한 설정이면 오류다. 저장 데이터 일괄 수정이나 DB DDL 변경은 없다. 기존 준비된 인덱스는 재생성하지 않는다.

기본값으로 정규화되는 요청은 기존 index-build-v1 fingerprint를 그대로 유지한다. 따라서 이전 접수의 응답이 유실됐더라도 동일 키로 확인할 수 있다. 사용자 정의 값은 확정 설정을 fingerprint에 포함한다. 같은 키에 다른 HNSW 값은 409이고, JSON 키 순서만 달라도 같은 설정이면 재전송으로 처리한다. 브라우저 보류 요청은 당시 body와 key를 그대로 보존한다.

### 평가

신규 평가 보고서는 variant마다 indexing_config를 기록한다. compare 출력은 양쪽 설정을 보여주며 같은 index_version_id의 설정이 서로 다르면 거절한다. 다른 버전끼리는 설정 차이를 기록한 채 기존 정답셋·입력·후보 예산·query profile 통제 조건으로 비교한다. 예전 보고서에 필드가 없으면 당시 고정값 16/128로 해석하고 원본 파일을 수정하지 않는다. 이 변경으로 완료한 평가를 자동 반복하지 않는다.
