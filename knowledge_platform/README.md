# Knowledge Platform

Data canonical을 선택형 청킹하고 Model 임베딩으로 OpenSearch HNSW 인덱스를 만든다. 고정 버전/임베딩 공간의 벡터 검색과 선택적 Model rerank, 버전 활성화/삭제, 평가 CLI를 제공한다.

v1 핵심 경로와 제한된 12문항 비교, 격리 실패/복구 검증에는 서버 근거가 있다. Agent 소유 자연어 검색 Tool과 MCP도 이미 구현됐다. 이를 아직 설계 전인 다음 작업으로 보지 않는다. BM25/RRF 혼합 검색은 현재 구현 범위 밖이다.

- [현재 범위와 남은 항목](../docs/knowledge-platform/work-plan.md)
- [구조](../docs/knowledge-platform/architecture.md)
- [청킹](../docs/knowledge-platform/api-chunking.md), [색인](../docs/knowledge-platform/api-indexing.md), [검색/rerank](../docs/knowledge-platform/api-search.md)
- [평가 도구](evaluation/README.md), [평가 절차](../docs/knowledge-platform/testing-evaluation.md)
- [과거 서버 인수](../docs/knowledge-platform/acceptance.md), [실패/복구](../docs/knowledge-platform/testing-failure-recovery-20260922.md)

Go module/protobuf는 Docker builder에서 생성한다. `indexing.compose.yml`은 기존 OpenSearch 연결을 추가하는 override다. `tests/live_indexing.py`는 새 인덱스를 생성/활성화하므로 단순 상태 조회와 구분한다. 문서 갱신만으로 색인이나 모델을 다시 생성하지 않는다.
