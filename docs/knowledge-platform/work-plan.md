# Knowledge Platform 단계별 상태와 후속

2026-10-01 코드 기준으로 청킹→Model embedding→OpenSearch 색인→벡터 검색→선택적 rerank→평가의 v1 경로가 구현돼 있다. Agent의 자연어 검색 Tool/MCP도 후속 플랫폼에 이미 구현됐다.

| 단계 | 코드 구현 | 검증과 남은 범위 |
|---|---|---|
| K01 | Data canonical·출처/hash·입력 버전 고정 | 계약과 처리 코드 있음 |
| K02 | `recursive_character_v1`, `structure_v1`·청크 미리보기/비교·표/코드 구조 분할 | 서버 청킹/비교. 파싱/OCR/제목 판별은 Data 소유 |
| K03 | Model 문서 embedding·profile/차원/공간 검증 | 기존 한국어 embedding 배포의 API/연동 근거 |
| K04 | HNSW 색인·배치/부분 진행·버전·active CAS·참조 | 실제 색인/활성화·삭제/참조 해제 근거 |
| K05 | 질문 벡터와 고정 공간/버전 검색·필터/출처·오류 구분 | 실제 검색 및 격리 DB/실패 검사 |
| K06 | Model 한국어 reranker profile/API | 실제 추론/점수 API 확인 |
| K07 | 선택적 rerank·후보/최종 순위·timing·UI | 실제 ON/OFF 기능. 품질 일반화는 별도 |
| K08 | 자료/정답 검토·고정 snapshot·hit/recall/MRR/nDCG·비교·후보 진단 | 12문항 제한 평가 있음. recursive ON 최초 후보 불일치 1건은 미해결 |
| 운영 보완 | custom HNSW·실패/복구·요청 저장·UI stale 응답 처리 | 격리 서버 Go/DB 검증 있음. 마지막 Gateway 수정 후 이미지/실제 브라우저 확인 일부 미확인 |

## 현재 검색 경계

Knowledge 검색 API는 `query_embedding`과 `embedding_space_id`, `index_version_id`를 받는다. Agent의 자연어 Tool이 Model 질문 임베딩을 먼저 수행한다. rerank를 켠 경우 Knowledge가 Model reranker를 호출한다. 현재 OpenSearch 질의는 kNN이며 BM25/RRF 혼합 검색은 포함하지 않는다.

## 남은 항목

- 최종 요청 보관 수정의 배포 이미지/사용자 화면 인수는 로컬 Node 결과와 구분한다.
- 최초 `CANDIDATE_IDENTITY_CHANGED`의 원인은 미확정이다. 재발 시 저장된 후보 fingerprint/순위·고정 버전으로 조사하고 비교 기준을 완화하지 않는다.
- 한 문서 12문항의 결과를 일반 검색 품질 보증으로 확대하지 않는다. 새 자료/사용자 요구가 생기면 기존 평가 도구로 대표 범위를 확장한다.
- BM25/RRF, 실제 OpenSearch 노드 장애/부하, 전체 백업/복구는 별도 후속이다. Data OCR 개선·PDF 제목 추정·LiteLLM 도입을 현 기능의 선행 조건으로 추가하지 않는다.

[구조](architecture.md), [청킹](api-chunking.md), [색인](api-indexing.md), [검색](api-search.md), [평가](evaluation.md), [서버 인수](acceptance.md), [후보 진단 결과](../../knowledge_platform/tests/results/evaluation-candidate-diagnostics-20260921.json), [실패/복구](testing-failure-recovery-20260922.md), [세션 검토](../testing/session-closeout-20260922.md)를 참고한다.
