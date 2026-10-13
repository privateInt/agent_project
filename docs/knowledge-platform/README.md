# Knowledge Platform 문서

선택형 청킹·Model embedding·HNSW 색인/버전 관리·벡터 검색·optional rerank·평가 CLI가 구현돼 있다. Agent 소유 자연어 검색 Tool/MCP도 구현됐다. 현재 범위와 남은 인수는 [작업표](work-plan.md)를 따른다.

| 주제 | 문서 |
|---|---|
| 구조/소유권 | [아키텍처](architecture.md) |
| 청킹 | [API](api-chunking.md), [검증](testing-chunking.md) |
| 색인/버전 | [API](api-indexing.md), [검증](testing-indexing.md), [수명주기](testing-lifecycle-20260921.md) |
| 검색/rerank | [API](api-search.md), [검증](testing-search.md) |
| 평가 | [설계](evaluation.md), [실행/근거](testing-evaluation.md), [CLI](../../src/platform/knowledge/evaluation/README.md) |
| 서버 인수 | [인수 기록](acceptance.md), [실패/복구](testing-failure-recovery-20260922.md) |
| 화면 | [UI 범위](console-ux-20260922.md), [마지막 수정/검증 한계](../testing/session-closeout-20260922.md) |

현재 검색은 벡터+선택적 rerank이며 BM25/RRF는 별도 확장이다. 12문항 평가와 최초 recursive 후보 불일치 기록을 함께 유지한다. 날짜별 문서의 “다음 단계”는 해당 시점의 이력이며 [전체 로드맵](../roadmap.md)이 현재 작업 순서다.
