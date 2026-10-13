# Knowledge Platform 검증 기록

2026-09-22 통합. 아래 K04~K08은 사용자 서버 로그·전달 JSON에 근거한 2026-09-21 기록이다. 당시 식별자와 실패를 보존하며 현재도 해당 객체가 존재한다고 가정하지 않는다. API 계약·재현 절차는 각각 기능별 api/testing 문서를 따른다.

| 확인 범위 | 기록 |
|---|---|
| 색인·활성화 | [K04](#k04) |
| 외부 질문 벡터 검색 | [K05](#k05) |
| Model reranker API | [K06](#k06) |
| Knowledge 선택적 재정렬 검색 | [K07](#k07) |
| 같은 입력의 청커 비교 | [K08](#k08), [기존 평가 진단](#baseline-evaluation) |
| 이후 화면·삭제·참조 해제 | [관리 검증](testing-lifecycle-20260921.md), [새 콘솔](console-ux-20260922.md) |
| 이후 실패·복구 | [격리 검증](testing-failure-recovery-20260922.md) |

이 성공은 일반 검색 품질·모든 운영 장애의 검증 완료를 뜻하지 않는다. recursive 최초 후보 불일치 원인은 미확정이며 Data PDF 구조 추정은 Knowledge 범위 밖이다. 다음 개발은 Agent 소유 MCP 검색 Tool 계약이다.


<a id="k04"></a>

## K04 색인·활성화

확인일: 2026-09-21. 사용자 서버 실행 로그와 `live_indexing.py`의 최종 응답을 근거로 **K04 핵심 빌드·저장·활성화 성공**을 기록한다. 작성 환경에서 서버를 직접 조회한 결과는 아니다. 후속 벡터 검색 결과는 [K05](#k05)에 있다.

### 확인한 결과

`protecting → indexing → ready`로 진행했고, 48개 중 48개가 저장됐다. 기존 ChunkSet을 Model Platform의 배포 모델로 문서 임베딩하고 OpenSearch에 저장한 뒤 해당 버전을 활성화했다. `ready` 전환은 구현상 배치 완료·쓰기 차단·refresh/count 검사를 통과한 뒤 이루어진다. 별도로 OpenSearch 노드의 원본 응답을 수집한 것은 아니다.

| 항목 | 확인값 |
|---|---|
| 결과 | `status: passed`, `ready`, 48/48개 |
| 논리 인덱스 | `aafe22b7-852b-424e-afaa-58fa8c1d739f` |
| 생성 버전 = 활성 버전 | `150826c2-1f4c-495c-8e07-54aacaa1a245` |
| 기존 청킹 실행 | `6e3eaf24-c2dc-4756-87bb-83468a96c827` |
| 청커 | `structure_v1 / 2026-09-18.2`, 최대 1,000자·겹침 100자 |
| Model 배포 | `44247297-5184-4a36-9225-7791c80d3add` |
| 임베딩 차원 | 1,024 |
| 임베딩 공간 | `es_00cb26c80fff352a4cc494bef5b90d75a83584caf4d7e3b525f8516c2dcab76e` |
| 기존 runtime 재사용 | `36b3c91fbdc8bb7670dac3dd16da71055ebd40bd294960187254f3d5a9c452f9` |
| Data 참조 상태 | `held` |
| 실행 journal | `~/workspace/indexing_check_bulk_fix_20260921.json` |

ChunkSet/policy hash와 두 canonical ID를 포함한 최종 응답은 [검증 요약 JSON](../../knowledge_platform/tests/results/indexing-k04-20260918.json)의 `live_receipt`에 보존한다. 현재 상태와 과거 실패 시도를 별도 필드로 기록한다.

### 수정과 재검증 경위

컴파일 수정 이후 Knowledge 전체 Go 검사·Data refinement unit·Knowledge/Data의 PostgreSQL 통합 suite와 이미지 빌드가 사용자 서버에서 통과했다. 이 DB 통합 검사의 외부 Model/OpenSearch 호출은 대역이며 실제 추론·저장 성공 근거와 구분한다.

첫 실제 bulk는 action metadata `_require_alias` 거절로 실패했다. [bulk 수정](testing-indexing.md#bulk-recovery) 11개 파일 적용 후 Python 도구 6개, Docker tests target의 `go test -count=1 ./...`, Knowledge service 빌드·기동이 통과했다. 후속 실제 실행에서 위 48개 저장·활성화를 확인했다.

사용자는 이전 실패 journal `indexing_check_20260918.json`을 삭제했다. `--retry-failed-from`을 사용한 첫 재시도는 파일을 읽는 단계에서 중단됐다. 이후 해당 옵션을 빼고 별도 `--state-file`로 실행했으므로 **이번 성공은 새 논리 인덱스**다. 기존 실패 인덱스 안에 새 버전을 만든 사례로 기록하지 않는다. 이전 실패 인덱스의 삭제·참조 해제 여부는 이번 결과로 확인할 수 없다.

### 남은 검증과 다음 작업

이 실행에서 확인한 held는 참조 획득 상태이며 삭제·해제 성공을 뜻하지 않는다. 이후 전용 삭제 검증은 [관리 기록](testing-lifecycle-20260921.md), 두 청커 비교와 품질 한계는 [K08](#k08)을 따른다. 원본 needs_ocr·미인식 영역 제외는 유지한다. 이전 실패 인덱스의 정리 여부는 이 성공으로 추정하지 않는다. 성공 journal은 보존한다.


<a id="k05"></a>

## K05 벡터 검색

사용자가 제공한 `붙여넣은 텍스트 (1)(20260921-013327).txt`의 실행 결과를 근거로 작성했다. 서버에 직접 접속한 관찰이 아니다. 로그 SHA-256: `1a5826b4d580a0047a42f58dfa4de252cf636e350d0a4a29273dbc409d1f5272`.

### 확인한 결과

| 항목 | 사용자 로그의 근거 |
|---|---|
| 소스 적용 | `knowledge_search_update_20260921`: `APPLIED: 38 files` |
| Python | 검색 도구 8개 전부 `OK` |
| Go | Docker tests target의 `go test -count=1 ./...` 성공 |
| PostgreSQL | `run_search_db_checks.py` 성공. 버전/공간·진행 중 검색과 삭제·취소 검사, 임시 schema 정리. Model/OpenSearch는 대역 |
| 서비스 | Knowledge service·Gateway 이미지 빌드 및 두 컨테이너 `Started` |
| 실제 API 검색 | `live_search.py`의 `status: passed`, 후보 5개·반환 5개·필터 결과 5개 |
| 기존 자료 재사용 | 색인 청크 48개, 질문 임베딩 1회, 인덱스 변경 0회 |
| 정상 응답 검사 | query/document 공간 일치·명시적 버전·결과 무결성·후보 필터·점수 순서 |
| 거절 검사 | 다른 공간·차원 불일치·빈 필터·알 수 없는 필터 거절 |

기존 Model 배포와 runtime을 그대로 사용했다. 문서를 다시 임베딩하거나 인덱스를 새로 만들지 않았다. 이번 확인 범위는 K05 벡터 검색 API의 연동이며 rerank는 사용하지 않았다.

### 고정 식별자와 보존 기록

인덱스·버전·임베딩 배포·공간·차원·runtime은 [K04 기준](#k04)과 같다. 반환 canonical은 `157c335f-5436-4338-8078-edb040ebe1ca`이며 원본 품질 `needs_ocr`를 유지했다.

간결한 전체 receipt와 빌드 식별자는 [검증 요약 JSON](../../knowledge_platform/tests/results/search-k05-20260921.json)에 보존한다. 사용자 서버의 `~/workspace/search_check_20260921.json`과 K04 journal `~/workspace/indexing_check_bulk_fix_20260921.json`을 유지한다. 원본 빌드 로그·벡터·본문·이미지를 프로젝트 테스트 결과에 추가 복제하지 않는다. 기존 [1 MB 미만 보관 정책](../testing/artifact-retention.md)을 따른다.

### 검증의 한계

- `elapsed_seconds: 0.246`은 도구 한 번의 전체 시간이며 순수 검색·p95·처리량 지표가 아니다.
- 반환 5개와 무결성 확인은 정답률 검증이 아니다. 후속 12문항 품질 비교는 [K08](#k08)이다.
- needs_ocr는 저장 원본의 경고이며 이 검색에서 OCR이 실패했다는 뜻이 아니다.
- 이 K05 실행에는 rerank·Agent MCP·LiteLLM이 포함되지 않았다. 화면·삭제 확인은 이후 별도 기록으로 남겼다.


### 다음 구현

K06 Model 점수 API와 K07 Knowledge 선택적 재정렬은 아래 후속 기록에서 확인한다. 현재 구현 순서는 [작업 목록](work-plan.md)을 따른다. 과거 성공을 확인하려고 문서 임베딩·색인·OCR을 반복하지 않는다.


<a id="k06"></a>

## K06 Model 재정렬 API

사용자가 제공한 Playground 점수 화면과 `model_platform/tests/live_reranking.py` 결과를 근거로 **Model reranker API와 짧은 관련성 예제의 실제 추론을 확인했다.** 검색 품질 평가와 Knowledge 검색의 rerank 연결은 포함하지 않는다.

### 실행 근거

```bash
python3 model_platform/tests/live_reranking.py \
  --deployment-id cc64a683-a69f-48a5-836e-9f52f0255cc6 \
  --summary-file "$HOME/workspace/reranking_check_20260921.json"
```

출력은 `status: passed`, 유효 점수 요청 2회, 수명주기 변경 0회다. 원본 요약은 사용자 서버 경로에 있으며, 전달된 결과의 사본은 [live 요약](../../model_platform/tests/results/reranking-live-20260921.json)에 보존한다.

| 항목 | 확인 값 |
|---|---|
| 배포 ID | `cc64a683-a69f-48a5-836e-9f52f0255cc6` |
| 모델 버전 ID | `68f1c0db-da9d-4195-b266-4cc7249916f1` |
| 원본 | `Dongjin-kr/ko-reranker` |
| 확정 revision | `8c2ab1a833734491fd3158164dd2b7d8b28e071c` |
| profile | `ko-reranker-v1` |
| profile ID | `rp_2717f05094bb6068f3edef2c93025792da740785f956b97dc07d06c29f228ff1` |
| runtime container | `b25cca9eed0324b1778a34ae4ebf8668515ea56bb4ea2e4717ff25a0dc264018` |
| runtime image | `sha256:59c018a60d965607819387096449351ab9d7ca3134b0a6f7e3f239f33dad9f4b` |
| 등록 종류 / capability | `llm` / `reranker` |
| 입력 정책 | `xlmr-pair-special4-no-prefix-v1`, 512토큰, 길이 초과 거절 |
| 점수 의미 | 단일 `logit`, 큰 점수 우선, 확률 아님 |

| 후보 | 점수 | 질문+후보 토큰 |
|---|---:|---:|
| relevant | 9.869013786315918 | 18 |
| unrelated | -9.824690818786621 | 18 |

실제 API에서 profile identity, ID/입력 순서, 유한 단일 logit, pair 토큰 한도, 짧은 관련성 쌍의 상대 순위, 후보 순서 교환, profile 불일치·빈 후보·중복 ID·알 수 없는 필드·길이 초과 거절을 확인했다. 화면에도 후보 2개와 같은 점수·18토큰이 표시됐다. 이전 `reranking_view` import 오류 이후 정상 재정렬 화면이 확인됐다. Playground 수정 Docker 빌드 명령의 원문 로그 자체는 별도로 제공되지 않았다.

### 완료와 남은 범위

K06 Model capability는 실제 API·화면 확인까지 완료했다. 앞서 profile 수정 후 Deployment/Gateway Go 빌드·PostgreSQL 두 통합 검사·서비스 교체를 확인한 기록은 [누적 요약](../../model_platform/tests/results/reranking-k06-20260921.json)에 보존한다. DB의 중지 경합 검사는 runtime 대역을 사용했고, live 도구는 공유 모델을 실제 중지하지 않았다.

후속 Knowledge 연결은 [K07](#k07)에서 확인했다. [중지 배포 재배포 UI](../testing/model-management-update.md#redeploy)는 로컬 검사까지 확인했으며 실제 서버 재배포 성공은 별도 미확인이다.

현재 결과로 대표 문서의 검색 Recall/MRR/nDCG, 긴 청크의 512토큰 적합성, 장시간 부하나 모든 질문의 관련성을 검증했다고 주장하지 않는다. 실제 48개 인덱스의 `needs_ocr` 경고는 유지하며 두 청커 비교는 [K08](#k08)에 별도로 기록했다.


<a id="k07"></a>

## K07 재정렬 검색

확인일: 2026-09-21. 사용자가 제공한 서버 실행 로그에서 **K07 적용·Go·PostgreSQL 통합·실제 Model/OpenSearch 검색 연동 통과**를 확인했다. 서버를 직접 조회한 기록은 아니다. 실제 브라우저 화면과 검색 품질은 이번 확인 범위에 포함하지 않는다.

### 근거와 검증 범위

| 항목 | 확인 결과 |
|---|---|
| 패치 | `knowledge_rerank_update_20260921` check/apply, 37개 파일 적용 |
| Python | K07 도구 6개 + 기존 검색 도구 8개, 총 14개 통과 |
| Go | Docker tests target의 `go test -count=1 ./...` 통과 |
| PostgreSQL | `run_rerank_db_checks.py` 통과, service/app 1.148초. 격리 임시 schema의 off/on/empty/error/cancel/deadline/read-delete 검사; Model/OpenSearch는 대역 |
| 실행 서비스 | Knowledge service·Gateway 빌드 성공과 두 컨테이너 Started |
| 실제 API | `live_rerank_search.py`의 `status: passed`; 기존 인덱스와 준비된 두 모델 배포 사용 |
| 호출·변경 | 질문 임베딩 1회, 정상 rerank on 검색 1회, 인덱스·배포 수명주기 변경 0회 |

원본 첨부는 `붙여넣은 텍스트 (1)(20260921-035918).txt`, 27,245 bytes, SHA-256 `2444be51f41ce19b5e3b47806621dbcbad931c56ba11bdba16e86eef1a3b83da`다. 전체 빌드 로그를 중복 보관하지 않고 [검증 요약](../../knowledge_platform/tests/results/rerank-k07-20260921.json)에 근거와 서비스 이미지 ID를 보존한다. 로그의 최종 JSON은 [실제 API 결과](../../knowledge_platform/tests/results/rerank-search-live-20260921.json)에 그대로 추출했다. 서버 원본 요약 경로는 `~/workspace/knowledge_rerank_check_20260921.json`이다.

Go tests target과 별도 PostgreSQL 검사를 구분한다. DB 검사의 장애·빈 후보·timeout·동시 삭제 검사는 대역을 사용한다. 실제 모델의 중지·장애를 이번 서버에서 유발했다는 뜻이 아니다. 이번 문서 갱신에서는 완료한 코드·DB·GPU 검사를 반복하지 않았다.

### 실제 사용 대상

[K04 기준](#k04)의 48개 청크 인덱스·명시적 버전·임베딩 배포/공간과 [K06](#k06)의 reranker 배포·profile을 재사용했다. 질문/후보 pair는 최대 512토큰, 무접두어, 초과 거절, raw logit 정책이다.

재정렬 runtime image/container, tokenizer·artifact hash, 전체 chunk ID는 실제 API JSON에 보존한다. 등록 모델 유형이 legacy `llm`이어도 이번 검증 capability는 `reranker`다. 새로운 모델 컨테이너를 만들거나 기존 인덱스를 재색인하지 않았다.

### 검색 결과

질문: “이 연구에서 가죽 제품의 생애주기 환경 영향을 어떻게 평가했나요?”

같은 질문 임베딩으로 off 검색 후보 20개와 on 검색 최종 5개를 대조했다. 후보의 ID·원문·출처·품질·벡터 점수/순위 보존, profile identity, 단일 유한 점수·토큰·정렬 규칙, off/on 적용 표시, 빈 질문과 다른 profile의 거절 검사를 통과했다. 동점 처리 검사가 포함돼도 이 실제 결과에 동점 후보가 있다는 뜻은 아니다.

| 최종 순위 | 원래 벡터 순위 | 벡터 점수 | 재정렬 점수 | 질문+후보 토큰 |
|---:|---:|---:|---:|---:|
| 1 | 1 | 0.761916 | 2.382285 | 146 |
| 2 | 4 | 0.750416 | 1.708795 | 254 |
| 3 | 2 | 0.760610 | 1.029120 | 283 |
| 4 | 7 | 0.736247 | 0.396487 | 281 |
| 5 | 10 | 0.728227 | -0.238298 | 206 |

벡터 7위와 10위가 최종 top 5에 포함됐다. 이는 재정렬 적용 확인이며 정답률 향상 근거는 아니다. logit은 확률이 아니므로 음수 점수도 정상이다. 벡터 점수와 logit을 직접 비교하거나 더하지 않는다.

| 시간 항목 | 값 | 범위 |
|---|---:|---|
| Knowledge on 검색 전체 | 310 ms | 질문 임베딩 제외 |
| Model profile 조회 | 103 ms | 검색 전체에 포함 |
| 후보 검색 | 11 ms | OpenSearch backend 7 ms를 포함 |
| Model rerank 구간 | 194 ms | 검색 전체에 포함 |
| 도구 전체 | 0.678초 | 질문 임베딩·off/on·거절 검사 등을 포함 |

단일 요청의 관찰값이다. 개별 시간의 합이 전체 시간과 일치할 필요는 없으며 backend 7 ms를 후보 검색 11 ms에 다시 더하지 않는다. 부하 시험·지연 보장·질문 임베딩 단독 시간은 이 결과로 판단하지 않는다.

### 남은 작업과 문서 적용

이 연동 확인 이후 브라우저 ON/OFF·관리 선택 전환·삭제/참조 해제를 [별도 검증](testing-lifecycle-20260921.md)했고, 같은 snapshot의 두 청커 비교는 [K08](#k08)에 기록했다. Model 재배포 실사용은 미확인이다. 기존 원본 품질 경고는 유지하며 단일 검색 성공으로 품질을 일반화하지 않는다.


<a id="k08"></a>

## K08 청커 비교

2026-09-21. 사용자 실행 로그와 첨부 JSON을 근거로 기록했다. 서버에 직접 접속해 수행한 검사는 아니다. 원본 보고서는 외부 평가 폴더에 보존하고 프로젝트에는 [작은 결과 요약](../../knowledge_platform/tests/results/evaluation-candidate-diagnostics-20260921.json)만 남긴다.

### 입력과 색인

snapshot `8abb4c67b353d892fc3d7f625dff8060c512b584895c44da9afcb3385a6c3fb2`, reviewed dataset SHA-256 `7bea41064fb64e957dcc8915a8fb6e8ac8ca5b747e3c505efa8e9101cd6e4ae3`를 고정했다. 한 논문과 그 표 발췌의 12개 한국어 질문이며 영어 근거를 사용한다. 두 문서의 needs_ocr/needs_review 품질 한계는 그대로다. max_characters 1000·overlap_characters 100·후보 20·상위 5개 조건이다.

| 전략 | 청킹 실행 ID | 별도 index ID | version ID | 저장 |
|---|---|---|---|---:|
| 개선 structure 2026-09-21.1 | 32c1c351-4136-44f2-9ece-435157b64037 | dab75094-38a6-47ec-9dee-d016397a0bce | a1334595-583e-490c-8ad9-7ec279f188b3 | 48/48 |
| recursive 2026-09-18.2 | 75c518e9-0a63-4c72-8dee-39d1a0e09908 | 6d393e0d-bf9a-416e-98d1-0a7a34237aec | 61df22bc-36e3-460d-95dc-20059878e9fe | 44/44 |

두 버전 모두 ready·references held·자신의 active version 일치를 확인했다. live_indexing 도구는 각각 새 논리 인덱스를 만들고 그 버전을 활성화했다. 기존 기준 index aafe22b7-852b-424e-afaa-58fa8c1d739f / version 150826c2-1f4c-495c-8e07-54aacaa1a245의 활성 전환은 하지 않았다. 기존 Model embedding 배포 44247297-5184-4a36-9225-7791c80d3add, reranker cc64a683-a69f-48a5-836e-9f52f0255cc6을 재사용한다. 임베딩은 같은 1024차원 공간이다.

청크 조회 자료의 전체 입력 명세·snapshot·옵션과 실제 source span 텍스트를 대조했다. b191 정답 구절은 새 두 청커에서 앞의 대체 공정 설명과 함께 저장됐고 인접 대상 청크의 100자 overlap이 일치했다. b66은 기존 26자 제목 단독에서 개선 structure의 642자 제목+본문 청크로 묶였다. 표 청크 수 10개는 유지됐으나 모든 표의 바이트 동일성을 검사했다고 확대 해석하지 않는다. b160·b192가 text라는 Data 구조 판별의 한계는 남아 있다. 번호/서식 기반 제목 추정은 별도 보류이며 Knowledge에서 보정하지 않는다.

### 같은 12문항 결과

| 전략 | rerank | Hit@5 | MRR@5 | nDCG@5 | 실패 |
|---|---|---:|---:|---:|---:|
| 기존 structure | off | 10/12 | 0.750000 | 0.771822 | 0 |
| 기존 structure | on | 11/12 | 0.833333 | 0.855155 | 0 |
| 개선 structure | off | 11/12 | 0.819444 | 0.844244 | 0 |
| 개선 structure | on | 12/12 | 0.875000 | 0.907732 | 0 |
| recursive | off | 12/12 | 0.805556 | 0.855155 | 0 |
| recursive | on | 11/12 | 0.791667 | 0.824399 | 1 |

고정 분모 12이며 실패는 0점이다. 원래 보고서와 새 보고서의 집계를 재계산하고 dataset·source/snapshot·query/reranker profile·budget을 대조했다. 새 평가는 질문 임베딩 12회·검색 48회·수명주기 변경 0회다. 후보 Recall@20은 네 설정 모두 1.0이며 recursive ON 실패 문항도 OFF 후보에서 근거를 확보했다.

body-07은 기존 검색 7위→rerank 17위에서 새 두 청커 모두 3위→2위로 개선됐다. 개선 structure의 다른 문항은 기존 대비 정답 순위가 나빠지지 않았고 body-08 OFF는 2위→1위였다. crop-01/02는 각 평가에서 OFF 1위→ON 2위로 재정렬 시 순위가 내려간다. rerank는 선택 기능을 유지하며 이 작은 자료만으로 청커/모델의 일반적 우열이나 지연 성능을 보장하지 않는다.

### recursive body-03 오류와 재확인

원래 report-chunkers-context-v1.json은 completed_with_errors다. recursive-on / body-03은 CANDIDATE_IDENTITY_CHANGED이며 후보 ID 집합은 같았으나 비교 대상 필드 하나 이상이 달랐다. 당시 보고서에는 변경 필드가 없으므로 원인을 특정할 수 없다.

inspection-recursive-body-03.json은 단일 문항 재확인이다. 질문 임베딩 1회·검색 2회·수명주기 변경 0회였고 OFF/ON 모두 1위, 20개 후보의 ID·내용·metadata·검색 순위·점수가 모두 일치했다. 이전 보고서의 OFF 상위 5개 청크와 순서도 같다. 그러나 전체 OFF 후보 해시는 이전과 다르다. 이 해시는 ID·검색 순위·점수의 조합이고 재확인에서 질문을 다시 임베딩했으므로 어느 시스템이 원인인지 단정할 수 없다. 오류 재현 없음과 근본 원인 해결을 구분한다. 원래 실패를 재확인 성공으로 대체하지 않는다.

후보 진단 도구의 17개 파일 적용과 서버 테스트 37개 통과는 확인했다. 이는 최초 오류의 재현이나 원인 해결을 뜻하지 않는다. 후속 화면·삭제 검증은 이 문서 상단의 관리·콘솔 기록을 따르며 Model 재배포 실사용은 별도 미확인이다.


<a id="baseline-evaluation"></a>

## 기존 12문항 평가와 body-07 진단

사용자가 전달한 `report-leather-lca-v1.json`, `comparison-leather-lca-v1.json`, `inspection-body-07.json`을 확인했다. 기존 structure 청커의 실제 12문항 평가이며 한 논문과 그 표 발췌이므로 독립된 두 연구의 평가로 해석하지 않는다. 질문은 한국어이고 근거 본문은 영어다.

| 지표 | rerank off | rerank on |
|---|---:|---:|
| 후보 Recall@20 | 1.0 | 1.0 |
| Hit@5 | 10/12 | 11/12 |
| MRR@5 | 0.75 | 0.8333333 |
| nDCG@5 | 0.7718216 | 0.8551550 |
| 실패 | 0 | 0 |

평가는 질문 임베딩 12회·검색 24회·수명주기 변경 0회였다. `body-07` 추가 진단은 질문 임베딩 1회·검색 2회·수명주기 변경 0회이며 원래 보고서와 후보 집합·상위 결과가 같았다. 정답 b191[27:101]을 온전히 담은 청크는 검색 7위→rerank 17위였다. 입력은 245토큰으로 512토큰 제한 이내였다. 26자짜리 `2. Materials and methods` 제목 청크는 16위→3위였다.

확인한 문제는 세 가지다. `.2` 구조 청커는 짧은 블록들을 길이 제한 전에 내보낼 때 overlap을 이어받지 않았다. 연속된 heading은 각각 경계로 처리돼 앞 제목이 단독 청크가 됐다. canonical의 b160(3.3절)과 b192(결론)는 text여서 절 경계가 아니며, 정답 문장은 앞 설명과 분리되고 결론과 섞였다. 앞의 두 가지를 이번 Knowledge 수정 범위로 삼는다. 마지막 항목은 Data 구조 판별의 한계다. 후속 논의에서 번호/서식 기반 제목 추정의 오인 위험을 확인해 Data의 별도 보류 과제로 분리했으며 Knowledge 다음 작업에서 제외했다. 이 문제가 reranker 순위 하락의 유일한 원인이라고 단정하지 않는다.

| 근거 파일 | 원본 파일 SHA-256 |
|---|---|
| report-leather-lca-v1.json | `a79c7cec005a8d55f9e90bcfdf4ca0ead145e6cbfab4adb317976e19060642f3` |
| comparison-leather-lca-v1.json | `1ddfa3bdcf7de1f337ecd899b1f0e3b25ada9fe249fc829c83173e8ebb893ff2` |
| inspection-body-07.json | `fd5abaf26078eec03a36c5807cf528fc77b87064027c317a271396039a05de9f` |

큰 원본 진단 파일은 프로젝트 tests/results로 복제하지 않고 위 요약과 해시를 보존한다.
