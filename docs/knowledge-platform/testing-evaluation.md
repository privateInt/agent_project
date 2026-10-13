# K08 평가 도구 적용·실행

2026-09-21. [평가 계약](evaluation.md)을 외부 Python 도구로 구현했다. **로컬 검사 25개와 로컬 HTTP 대역을 이용한 CLI 전체 흐름이 통과**했다. 이후 실제 12문항 평가와 body-07 진단을 완료했다. [청커 비교 결과](acceptance.md#k08)를 따른다. 후보 진단 보완 포함 로컬 검사 37개에 이어 사용자 서버의 17개 파일 적용·검사 37개 통과도 확인했다. 아래 prepare/정답셋 절은 최초 준비 절차이며 현재 평가 폴더가 있으면 반복하지 않는다. K07의 Go·DB·실제 검색 성공은 [기존 기록](acceptance.md#k07)을 유지한다.

새 도구는 Python 표준 라이브러리만 사용한다. 기존 `live_search.py`의 API 응답 검증 함수를 재사용하며 운영 Go 서비스가 평가 코드에 의존하지 않는다. Go/protobuf/DB/Docker/이미지 변경이나 재빌드·재시작은 없다. 정상 모델을 중지·재배포하거나 기존 색인을 다시 만들지 않는다.

## 1. 도구 회귀 검사

관련 Python 도구를 수정했을 때 실행한다. 합성 입력·HTTP 대역을 사용하며 GPU 요청이 없다. 현재 테스트 수는 코드에 따라 달라지므로 당시 통과 개수를 오늘의 기준으로 고정하지 않는다.

```bash
cd ~/workspace/agent_project
python3 -m unittest discover -s knowledge_platform/tests -p 'test_evaluation*.py' -v
```

기존 Go/DB/실제 검색을 문서 정리 때문에 반복하지 않는다.

## 2. 현재 문서와 평가 명세 준비 — 최초 준비 절차(완료)

```bash
cd ~/workspace/agent_project
python3 -m knowledge_platform.evaluation prepare \
  --index-id aafe22b7-852b-424e-afaa-58fa8c1d739f \
  --index-version-id 150826c2-1f4c-495c-8e07-54aacaa1a245 \
  --reranker-deployment-id cc64a683-a69f-48a5-836e-9f52f0255cc6 \
  --output-dir "$HOME/workspace/knowledge_eval_20260921"
```

기본 Gateway는 Knowledge `127.0.0.1:15100`, Data `127.0.0.1:14000`, Model `127.0.0.1:13000`이다. 원격에서 실행할 때 `--knowledge-url`, `--data-url`, `--model-url`에 실제 호스트를 지정한다. vLLM 동적 포트나 OpenSearch·DB 인증 정보를 넣지 않는다.

이 명령은 기존 버전과 canonical 본문, reranker profile만 읽는다. 실제 본문 바이트 hash·크기를 버전의 고정 값과 대조하며 **GPU 추론·수집·OCR·청킹·색인·활성화·삭제 요청은 0회**다. 아래 세 파일을 만든다. 기존 폴더가 있으면 덮어쓰지 않고 중단하므로 다시 준비할 때는 새 폴더를 지정한다.

| 파일 | 내용 |
|---|---|
| `sources.json` | 고정 source/canonical hash, 블록 원문·위치·품질 경고. 512 KiB 이하 |
| `dataset.json` | 같은 입력에 묶인 빈 정답셋. `draft`, 질문 0개이므로 평가 불가 |
| `variants.json` | 현재 버전의 off/on 두 설정, candidate_k 20·top_k 5, 실제 조회한 reranker profile |

현재 sources.json과 검토된 dataset-leather-lca-v1.json을 수령했고 기존 structure의 실제 12문항 평가·body-07 진단도 완료했다. 새 청커 비교에는 같은 자료·정답셋을 사용한다. 자료 본문과 큰 진단 파일은 tests/results에 복제하지 않고 평가 입력으로 보관한다.

처음에는 8~12문항을 작성한다. 두 문서의 본문 질문, 표의 수치/단위/조건, 여러 근거가 필요한 질문을 포함한다. 구체적인 질문은 실제 내용을 읽고 정하며 표 머리글만을 답으로 지정하지 않는다. OCR 오류가 있는 근거는 원본과 확인하거나 현재 평가에서 제외한 이유를 별도 기록한다. 현재 도구는 답이 있는 질문의 검색 평가만 지원한다.

## 3. 정답셋 작성과 검토

sources의 `documents[].blocks[]`를 읽고 질문과 답을 뒷받침하는 짧고 정확한 원문 구절을 선택한다. `add-case`는 구절을 직접 찾아 Unicode 문자 범위·구절 hash·source ID를 기록한다. 같은 case ID와 질문을 다시 지정하면 해당 질문에 정답 근거를 하나 더 추가한다. 동일하거나 겹치는 범위는 거절한다. 구절이 여러 번 나오면 `--start`에 0부터 시작하는 code point 위치를 지정한다.

다음은 명령 형식이다. `실제_...` 부분은 sources에서 확인한 값으로 바꾸어 실행한다. 질문에 `query: ` 접두어를 직접 붙이지 않는다.

```bash
cd ~/workspace/agent_project
python3 -m knowledge_platform.evaluation add-case \
  --sources "$HOME/workspace/knowledge_eval_20260921/sources.json" \
  --dataset "$HOME/workspace/knowledge_eval_20260921/dataset.json" \
  --case-id body-01 \
  --question '실제_문서를_읽고_작성한_질문' \
  --canonical-id '실제_canonical_UUID' \
  --block-id '실제_블록_ID' \
  --quote '실제_블록의_정확한_원문_구절' \
  --grade 3
```

등급 3은 핵심 정답, 2는 직접적인 보조 근거, 1은 부분적인 관련 근거로 일관되게 사용한다. 단일 정답 문항은 모두 3으로 두어도 된다. 함수는 구절의 존재·범위를 검증하지만 질문과 관련 있는지는 판단하지 않는다. 사람이 모든 질문/정답을 검토한 뒤 표시한다. `review`는 검토 이력을 기록하는 명령이며 LLM이나 도구가 의미상 정답을 인증한다는 뜻이 아니다.

```bash
cd ~/workspace/agent_project
python3 -m knowledge_platform.evaluation review \
  --sources "$HOME/workspace/knowledge_eval_20260921/sources.json" \
  --dataset "$HOME/workspace/knowledge_eval_20260921/dataset.json" \
  --reviewer shshin
python3 -m knowledge_platform.evaluation validate \
  --sources "$HOME/workspace/knowledge_eval_20260921/sources.json" \
  --dataset "$HOME/workspace/knowledge_eval_20260921/dataset.json"
```

`add-case`로 수정하면 검토 상태가 draft로 돌아간다. 직접 JSON을 수정하는 경우에도 review를 draft로 되돌리고 다시 검토한다. 자료 패킷이 바뀌면 dataset의 hash 검증이 실패한다. 단순 파일명 변경은 입력 hash를 바꾸지 않는다.

## 4. 같은 버전에서 off/on 평가

정답셋 작성·검토 후에 실행한다. `prepare`가 만든 빈 파일 그대로는 실행할 수 없다.

```bash
cd ~/workspace/agent_project
python3 -m knowledge_platform.evaluation run \
  --sources "$HOME/workspace/knowledge_eval_20260921/sources.json" \
  --dataset "$HOME/workspace/knowledge_eval_20260921/dataset.json" \
  --variants "$HOME/workspace/knowledge_eval_20260921/variants.json" \
  --output "$HOME/workspace/knowledge_eval_20260921/report.json"
```

N문항·현재 off/on 두 설정이면 질문 임베딩 N회, 검색 기본 2N회다. API 실패가 있으면 후속 의존 호출이 생략될 수 있다. 재시도/자동 fallback은 하지 않는다. on 요청도 전체 후보를 받아 top_k 밖의 후보까지 대조하므로 candidate_k는 도구에서 최대 50이다. 같은 질문의 벡터는 모든 청커/옵션에 재사용하고 별도로 저장하지 않는다. on 전체 후보 순위에서 최종 top_k를 로컬 평가하며 실제 서비스 top_k만 반환할 때와 전송량·처리 시간이 다르다.

보고서는 질문 단위로 갱신하며 완료 시 `completed`, 요청 실패가 있으면 `completed_with_errors`(종료 코드 2), Ctrl+C이면 `interrupted`(2)다. 미완료 보고서는 비교를 거절한다. 재실행은 새 출력 경로를 사용하며 자동 이어받기·중복 추론 방지 캐시는 제공하지 않는다. 준비 검증 실패는 추론 전에 중단하고 점수 없이 `preflight_failed` 기록을 남긴다. 잘못된 dataset 자체는 평가 진입 전에 거절한다.

주 지표 `all_questions_metrics`는 모든 문항을 분모로 하고 요청 실패를 0점으로 넣는다. 성공 요청 평균은 `successful_only_metrics`, 실패율은 `failure_rate`다. 정상 빈 검색은 성공한 0점 결과다. `candidate_recall`은 재정렬 전 후보 검색 지표이며 top_k 지표와 다르다. 후보 검색 성공 뒤 rerank가 실패해도 후보 Recall은 보존하고 최종 지표만 0으로 기록한다. `candidate_observed`는 후보 조회 결과를 확보한 문항 수다. 각 단계의 오류와 품질 경고를 확인하고, 성공 요청 평균만으로 모델을 선정하지 않는다.

```bash
cd ~/workspace/agent_project
python3 -m knowledge_platform.evaluation compare \
  --left "$HOME/workspace/knowledge_eval_20260921/report.json" \
  --left-variant baseline-off \
  --right "$HOME/workspace/knowledge_eval_20260921/report.json" \
  --right-variant baseline-on \
  --output "$HOME/workspace/knowledge_eval_20260921/comparison.json"
```

비교값은 오른쪽−왼쪽이다. 양수여도 이 작은 정답셋을 넘어선 품질 개선이나 통계적 유의성을 주장하지 않는다. 실패율과 개별 문항을 함께 본다. 서로 다른 실행 보고서도 같은 dataset·snapshot·문서 집합·후보/최종 수·query profile일 때 비교할 수 있다. 같은 인덱스 버전의 off/on이면 후보 집합도 같아야 한다.

## 5. 두 청커 비교

동일 snapshot·1,000/100 옵션의 개선 structure 48개와 recursive 44개는 모두 별도 인덱스 ready를 확인했다. 기존 48개 기준 인덱스도 보존했다. [인수 기록](acceptance.md#k08)에 실행·인덱스·버전 ID와 12문항 지표를 기록한다.

`variants-chunkers-context-v1.json`의 네 설정은 structure-new-off/on, recursive-off/on이다. 후보 20개·상위 5개를 고정했고 질문 임베딩 12회·검색 48회였다. 개선 structure ON은 12/12이며 recursive ON의 body-03 실패 1건 때문에 전체 보고서는 completed_with_errors다. 단일 문항 재확인 성공은 원래 보고서를 대체하지 않는다.

이 상태 확인을 위해 prepare·청킹·색인·전체 평가를 다시 실행할 필요는 없다. 향후 Data의 독립적인 변경으로 canonical이 바뀌면 새 자료 패킷과 정답 근거를 별도로 검토하며 서로 다른 snapshot을 동일 입력 비교로 취급하지 않는다. PDF/제목 인식 보완은 이번 Knowledge 평가의 후속 작업이 아니다.

## 6. 후보 불일치 자동 진단

[진단 보완](evaluation.md#candidate-diagnostics) 적용 후 다음 평가부터 오류 outcome의 `candidate_difference`에서 달라진 후보와 필드를 확인한다. 검색 점수는 off/on/on_minus_off를 기록한다. 임의 허용 오차나 자동 재시도는 추가하지 않는다. 질문당 `query_embedding_sha256`는 같은 실행에 공유한 실제 벡터의 normalized JSON SHA-256이며 원래 벡터는 저장하지 않는다. 구형 보고서에 이 필드가 없어도 비교할 수 있다. 이 해시는 진단용이며 비교 승인 조건을 새로 추가하지 않는다.

상세 기록은 한 건 4 KiB·보고서 상세 합계 48 KiB로 제한한다(각 객체의 encoded 바이트 기준). 후보 추가·제거 ID는 각각 최대 5개, 필드별 상세 후보는 최대 3개이며 용량에 따라 더 적게 저장한다. 전체 변경 개수·필드 목록과 생략 개수·truncated를 기록한다. 보고서 상세 예산을 넘으면 `candidate_difference_omitted: report_detail_budget_exhausted`를 남기고 해당 실패를 계속 집계한다. 보고서 전체 384 KiB 한도는 별도로 유지한다.

## 검증·보관·남은 범위

[로컬 확인 기록](../../knowledge_platform/tests/results/evaluation-k08-20260921.json)에 실제 실행한 25개 검사 범위를 기록한다. 한글/emoji code point·구절 hash·중복/겹침 거절·범위 합집합·손계산 지표·중복 점수 방지·실패 분모·전체 후보 변동·runtime 변경·두 청커 임베딩 공유·중단 기록·호환 비교를 확인했다. 실제 서버 Go/DB/GPU 테스트는 이번 변경에서 수행하지 않았다.

정답셋 최대 100문항·문항당 근거 20개·variant 4개이며 저장 상세량을 제한한다: `문항 수 × variant 수 × (top_k + 5) <= 1000`. 예를 들어 4개 variant·top_k 5이면 최대 25문항이다. 자료 패킷 512 KiB, dataset 256 KiB, 보고서 384 KiB의 상한을 넘으면 자르지 않고 오류로 종료한다. 운영 원본·완전한 청크 응답·벡터·큰 로그를 프로젝트의 테스트 결과로 추가하지 않는다. 기존 1 MB 미만 기록 정책을 유지한다.

실제 질문/정답 확정·prepare/run·두 청커 비교 결과는 확인했다. recursive ON 최초 오류 원인은 미확정이고 평가 진단 보완의 서버 적용·37개 검사 통과는 확인했다. 다음은 Knowledge 관리·검색 화면과 삭제·참조 해제 검증이며 품질 일반화는 별도다. K07 브라우저 표시 확인, OpenSearch 버전 삭제·참조 해제, Model 재배포 UI의 실제 서버 동작도 별도 미확인이다. 이번 패치 적용 후 압축파일·해제 폴더는 지워도 되며 실제 프로젝트와 작성한 dataset/sources/평가 보고서는 보관한다.


<a id="candidate-diagnostics"></a>

## 후보 진단 회귀 범위

Python 평가 검사 총 37개(기존 25 + 집계 4 + 신규 진단 8)가 로컬에서 통과했다. 실제 GPU 추론은 수행하지 않았다. 신규 검사는 미세 점수 차이의 엄격 거절·체크포인트 저장, text/metadata/rank 구분과 원문 비노출, 후보 집합 오류 우선순위, 정상 rerank·두 청커의 벡터 공유, API/응답 오류 구분, 50개 변경의 한도와 결정성, 100문항 실패에서 예산/분모/보고서 크기 보존, 구형 보고서 호환을 확인한다.

사용자가 올린 기존 두 전체 보고서의 비교와 단일 진단의 parent identity도 오프라인으로 확인했다. 테스트 결과 원문·소스/벡터·큰 로그는 복제하지 않고 작은 확인 요약만 프로젝트에 보존한다. 신규 진단의 서버 적용과 37개 검사 통과는 아래 사용자 로그로 확인했다. 이 문서 갱신 때문에 전체 평가나 GPU 검사를 다시 수행할 필요는 없다.

사용자 로그에서 --check 후 **APPLIED: 17 files**, 이어 `test_evaluation*.py` **37개 / 0.657초 / OK**를 확인했다. 외부 Python 평가 도구·검증 기록의 적용 확인이며 서비스 재빌드·재시작을 요구하지 않는다. 실제 모델 추론·새 검색 평가·후보 불일치 재현 결과는 이 로그에 포함되지 않는다. 기존 recursive-on / body-03 실패의 근본 원인은 여전히 미확정이다.


<a id="aggregate-comparison"></a>

## 집계 반올림 회귀 검사

평가 도구 기존 25개 검사와 추가 회귀 검사 4개, 총 29개가 로컬에서 통과했다. 순차 합산으로 생성된 보고서 수용, 작은 지표 반올림 차이 수용, 실제 지표·시간·건수 변경/필드 누락/NaN/Infinity 거절, dataset·후보 정체성 유지, 원본 보고서 바이트 불변을 확인했다. 대역 검사이며 GPU 호출은 없다.

실제 기존 평가와 body-07 진단은 [인수 기록](acceptance.md#baseline-evaluation)에 유지한다.
