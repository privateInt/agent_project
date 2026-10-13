# Knowledge 검색 평가 도구

프로젝트 루트에서 `python3 -m knowledge_platform.evaluation --help`로 실행한다. Python 표준 라이브러리를 사용하며 기존 배포와 인덱스만 읽고 추론한다.

- [평가 입력·매칭·지표 계약](../../docs/knowledge-platform/evaluation.md)
- [서버 적용·자료 준비·평가 실행](../../docs/knowledge-platform/testing-evaluation.md)

먼저 `prepare`로 현재 canonical 자료와 빈 dataset을 준비한다. `add-case`로 실제 질문과 정확한 원문 근거를 등록하고 검토 후 `review`를 실행한다. `run`은 같은 질문 임베딩을 최대 네 variant에 재사용한다. `compare`는 호환된 완료 보고서의 두 variant를 비교한다.

K08 구현과 합성 검사 통과는 실제 문서의 검색 품질 검증을 뜻하지 않는다. 서비스 재빌드나 재시작은 필요 없다.
