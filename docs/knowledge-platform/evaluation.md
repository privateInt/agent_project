# K08 검색 평가 계약

2026-09-21. K07 실제 검색 20→5 확인 이후의 외부 평가 도구다. Knowledge·Model·Data의 API를 이용하며 서비스·DB·컨테이너·색인 수명주기는 변경하지 않는다. 이 문서를 먼저 작성한 뒤 소스에 반영했다. [실제 청커 비교](acceptance.md#k08)를 확인했다. [후보 진단 보완](evaluation.md#candidate-diagnostics)까지 로컬 평가 검사 37개가 통과했고 사용자 서버의 17개 파일 적용·검사 37개 통과도 확인했다. 이 성공은 기존 실제 평가의 recursive 후보 불일치 원인 해결을 의미하지 않는다.

## 고정 입력과 정답

기존 ready 인덱스 버전의 snapshot·선택 canonical ID/hash·source ID/hash를 고정한다. Data content의 실제 바이트 hash/크기를 확인한 뒤 문서 블록 텍스트와 품질 경고를 평가 입력으로 내보낸다. 원본 전체 파일·벡터·응답 본문을 결과 보고서에 복제하지 않는다. 질문 벡터는 재현 비교를 위한 normalized JSON SHA-256만 추가 기록하며 구형 보고서에 해시를 소급 생성하지 않는다. 자료 패킷은 512 KiB, 정답셋은 256 KiB, 결과는 384 KiB 이하이며 초과 시 조용히 자르지 않고 중단한다.

정답은 질문별 source_document_id/canonical_id/block_id와 Unicode code point `[start,end)` 범위, 해당 구절의 SHA-256 및 관련성 등급 1~3이다. 실제 블록에서 확인한 구절만 등록한다. draft 자료는 평가를 거절하고 reviewed 표시·검토자를 요구한다. 처음 지원 범위는 답이 있는 질문 1~100개, 질문당 정답 1~20개다. 답 없음 판정·답변 생성 품질 평가는 포함하지 않는다.

같은 snapshot과 canonical 부분집합만 비교한다. 청커 변경으로 달라지는 chunk ID는 정답으로 사용하지 않는다. 정답 구절은 가능한 한 짧고 독립적인 근거로 작성한다. 중복/겹치는 정답 구간은 거절한다. 검색 결과를 보고 유리한 정답만 고르는 것을 피하며 표·본문·여러 문서·한글 질문을 포함한다. needs_ocr 경고를 정답 확정으로 해석하지 않는다.

## 매칭과 지표

이전 프로젝트의 DCG 계산 및 순위순 one-to-one 매칭 개념을 가져오고 재귀 metadata 탐색·문자열 포함 추측은 제거한다. hit의 실제 source spans와 정답 범위를 대조한다. 같은 hit 안의 겹치는 spans는 합집합으로 계산해 반복 표 머리글이 coverage를 부풀리지 않게 한다. 기본 정답 범위 coverage는 100%이며 dataset에서 0.5~1.0을 명시할 수 있다.

순위순으로 아직 배정하지 않은 정답 중 등급이 높은 것, 동점이면 정답 ID 순으로 하나를 배정한다. 한 hit와 정답은 각각 한 번만 인정한다. Hit@k, Recall@k, MRR@k, nDCG@k를 계산하며 DCG gain은 `2^grade-1`, discount는 `log2(rank+1)`이다. 이상 DCG는 전체 정답 등급의 내림차순이다. 후보 전체에 대한 Recall과 최종 top_k 지표를 구분한다. 후보 조회 후 rerank가 실패해도 이미 확인한 후보 Recall을 보존하고 최종 결과만 실패로 계산한다.

이는 고정된 정답셋에 대한 보수적인 근거 단위 지표다. 한 청크가 여러 정답을 담아도 한 개만 인정하고, 여러 청크를 합쳐야만 정답 범위를 덮는 경우는 인정하지 않는다. 미판정 결과는 0으로 계산한다. 이 정의와 정답 범위 폭에 따라 점수가 달라지므로 일반 검색 품질의 절대값으로 해석하지 않는다.

## 실행과 비교

평가 manifest는 기존 index/version ID, 선택적 embedding 배포, reranker 배포/profile, candidate_k/top_k를 고정한다. 최대 네 variant로 두 청커×off/on을 비교한다. 출력 상세량은 `문항 수 × variant 수 × (top_k + 5) <= 1000`으로 제한한다. `1 <= top_k <= candidate_k <= 50`이며 공개 검색 API의 최대 후보 100과 다르다. 모든 후보를 반환받아 off/on의 전체 후보 identity와 출처/벡터 순위를 대조하기 위한 도구 한도다.

질문당 임베딩을 한 번 생성해 모든 variant에 재사용한다. 임베딩 공간과 실제 query profile은 동일해야 한다. 인덱스별 off 후보는 한 번 읽고 on 요청도 전체 후보를 반환받아 집합·본문·출처·품질·원래 순위를 대조한 뒤 로컬에서 top_k를 평가한다. 후보 변동·runtime/profile 변화·응답 불일치는 실패다. 재시도·fallback·후보 잘라내기를 하지 않는다.

오류는 질문/variant별로 남기고 후보 불일치의 크기 제한·생략 표시는 [진단 계약](#candidate-diagnostics)을 따른다. 전체 질문 수를 분모로 한 실패 포함 평균(실패=0)을 주 지표로 제공하며 성공한 요청만의 평균과 실패율을 별도 제공한다. 준비 단계의 identity/자료 검증 오류는 추론 전에 중단하며 점수를 만들지 않는다. 실행 중간마다 보고서를 갱신하고 중단/미실행 항목은 완료와 구분한다. 완료된 보고서만 비교한다.

시간은 공유 query embedding, variant별 검색 왕복, API 내부 시간을 구분한다. on 검색 왕복에는 재검색/profile/rerank가 포함된다. 전체 후보를 받는 검증용 호출의 시간이며 일반 top_k 서비스 지연·반복 성능 평가가 아니다. 비교는 같은 dataset hash·snapshot·canonical 집합·매칭 정책·후보/결과 수·query profile을 요구한다. 정답셋 검토는 사람이 수행하고 품질 개선 결론은 실제 평가 결과를 받은 뒤 기록한다.

## 2026-09-22 색인 설정 기록

각 variant에 버전의 indexing_config를 보존한다. compare 결과에 양쪽 설정을 포함하며 같은 index_version_id의 설정 불일치는 거절한다. 필드가 없는 구형 보고서는 당시 고정된 Lucene/HNSW/cosinesimil·m16/ef_construction128로 해석하며 파일을 덮어쓰지 않는다. 다른 인덱스 버전의 HNSW 차이는 비교 기록에 남긴다. 평가 도구는 새 인덱스를 생성하거나 기존 인덱스를 변경하지 않는다. [HNSW 계약](api-indexing.md#hnsw)과 [로컬·서버 검증](testing-indexing.md#hnsw)을 구분한다. 서버 Go/DB 및 custom 버전의 빌드·활성화·검색은 사용자 기록으로 확인했다.


<a id="candidate-diagnostics"></a>

## 후보 불일치 진단

- 외부 Python 평가기의 diagnostics.py를 추가하고 runner.py에 연결했다. 후보 ID 집합과 10개 identity 필드의 기존 정확 비교·오류 코드 우선순위를 유지한다. 아주 작은 검색 점수 차이도 계속 실패다.
- 실패 outcome의 candidate_difference에 OFF/ON 후보 해시, 추가/제거 ID, 달라진 필드와 전체 변경 개수, 제한된 후보 상세를 저장한다. retrieval_rank와 retrieval_score는 이전/이후 값, 점수는 on_minus_off까지 기록한다. 나머지 필드는 normalized JSON SHA-256만 저장한다.
- 후보 해시는 retrieval_rank 순서의 ID·순위·점수 목록이다. 재정렬 후 출력 순서가 달라졌다는 이유만으로 해시가 변하지 않는다. 최종 rank·rerank_score·rerank_input_tokens는 원래와 같이 identity 비교 대상이 아니다.
- 질문 벡터는 query_embedding_sha256만 기록한다. 실제로 생성해 해당 문항의 모든 variant에 공유한 벡터의 normalized JSON SHA-256이다. 구형 보고서의 벡터를 추정·복원하지 않으며 compare의 승인 조건을 바꾸지 않는다.
- 상세 한 건은 독립 encoded 기준 최대 4 KiB, 보고서 상세 합계는 48 KiB다. 추가·제거 ID 각각 최대 5개, 변경 상세 후보 최대 3개를 결정적 ID 순서로 샘플링하고 한도에 맞춰 줄인다. 생략 개수·truncated를 기록한다. 보고서 상세 예산이 소진되면 candidate_difference_omitted를 남기며 실패 집계는 유지한다. 보고서 전체 384 KiB 상한도 유지한다.
- retrieval-report-v1에 선택 필드만 추가한다. 과거 보고서는 수정 없이 읽고 비교하며 현재 성공으로 과거 실패를 덮어쓰지 않는다. API 오류나 유효하지 않은 응답은 candidate 비교가 완료된 것처럼 표시하지 않는다.

추가 API 요청·자동 재시도·fallback은 없다. 최초 recursive-on/body-03 실패를 후속 성공으로 바꾸지 않는다.


<a id="aggregate-comparison"></a>

## 집계 실수 비교와 원본 보존

기존 `runner.compare`는 저장한 summary와 문항별 결과를 다시 집계한 값을 딕셔너리 완전 일치로 비교했다. 전달받은 실제 보고서의 off 검색 시간 12개를 순차 합산하면 평균이 `26.433249999999997`이 되고, 저장된 값은 `26.43325`다. 차이는 약 `3.55e-15 ms`이며 이 차이만으로 같은 오류를 재현했다.

Python 3.12에서 부동소수점 `sum` 알고리즘이 변경됐다. [Python 공식 문서](https://docs.python.org/3/library/functions.html#sum)를 참고한다. 따라서 서로 다른 Python 환경에서 생성·검증하면 이런 차이가 생길 수 있다. 사용자의 정확한 Python 버전 조합은 확인하지 않았으며, 재현한 반올림 문제와 서버 환경 원인 추정을 구분한다.

- DCG, 지표 평균, 후보 Recall 평균, 검색 시간 평균의 실수 합산을 `math.fsum`으로 변경했다.
- 재계산한 집계의 실수 값은 유한값 여부와 `rel_tol=1e-12`, `abs_tol=1e-12`로 비교한다.
- 문항 수·성공/실패 수·필드 구조·null은 타입과 값의 완전 일치를 유지한다. boolean을 숫자로 인정하지 않는다.
- dataset/snapshot/모델 profile/후보 집합의 동일성 검증은 변경하지 않는다. 실제 집계값 변경은 계속 거절한다.
- 기존 보고서 파일과 비교 파일을 수정하지 않는다. 진단 스크립트는 프로젝트 모듈을 사용하므로 스크립트를 교체할 필요가 없다.
