# K03 적용·검증

## 2026-09-21 K04 색인·활성화 확인 (최신)

사용자 서버 로그로 K03 패치 53개 적용, Deployment/Gateway Go 빌드 검사, 이미지 3개 빌드, Python/UI 78개 검사와 공식 임베딩 API를 확인했다. 기존 배포 `44247297-5184-4a36-9225-7791c80d3add`를 재사용했으며 query 1개·document 2개 모두 1,024차원·유한값·L2 norm 1.0이다. 공간 불일치·길이 초과 거절도 확인했다. 등록 유형은 legacy `llm`, 검증 capability는 `embedding`이다. DB 통합 검사와 새 profile GPU 배포는 이 로그의 검증 범위가 아니다.

K02 저장 실행 `6e3eaf24-c2dc-4756-87bb-83468a96c827`은 문서 2개, `structure_v1 / 2026-09-18.2`, 최대 1,000자·겹침 100자, 48개(text 38/table 10), 최대 997자다. b90은 992+495자로 나뉘며 기록된 출처 범위의 합집합은 0..1352, 반복 문맥 135자의 동일성을 확인했다. 원본의 needs_ocr와 미인식 이미지 4개 제외는 유지한다. 같은 입력의 두 청커 비교는 남아 있다.

K04 핵심 빌드·저장·활성화를 사용자 서버에서 확인했다. 컴파일 수정 후 Knowledge 전체 Go·Data refinement·Knowledge/Data PostgreSQL 통합 검사가 통과했고, bulk 수정 후 Go 전체·Python 도구 6개와 Knowledge service 교체가 통과했다. 실제 결과는 `ready`, 48/48개 저장, 1,024차원, `references: held`, 생성 버전과 활성 버전 일치다. 기존 Model 임베딩 runtime을 재사용했다. [확인 근거와 식별자](../knowledge-platform/acceptance.md#k04)를 보존하고 다음은 K05 외부 질문 벡터 검색이다. 실제 OpenSearch 버전 삭제·참조 해제와 검색 품질은 별도 미검증 범위다.

최초 작성 시점(2026-09-18)의 작성 환경에서는 Python 34개 검사·문법 검사만 실행했고 Go·Docker·Streamlit 검사는 미실행이었다. 이후 사용자 서버에서 K03 공식 API와 관련 검사·이미지 빌드를 확인했으며, K04 실제 문서 임베딩 소비도 성공했다. 최신 근거는 위 상태와 [K04 확인 기록](../knowledge-platform/acceptance.md#k04)을 따른다. 아래 절차는 최초 설치/일반 검증용이다. [API 계약](../reference/embedding.md)과 [직접 호출 당시 근거](embedding.md#runtime-check)를 구분해 보존한다.

## 1. 필요한 이미지 빌드와 검사

증분 패키지를 프로젝트에 적용한 뒤 실행한다. 기존 Model 등록·학습·배포 시작 작업이 끝난 시점에 서비스 교체를 진행한다. GPU runtime을 새로 배포하거나 모델을 다시 내려받을 필요는 없다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build deployment gateway
  docker compose --profile playground build playground
  for suite in tests/test_playground.py tests/test_model_management_ui.py tests/test_playground_ui.py; do
    docker run --rm -i -e PLAYGROUND_SOURCE_DIR=/app \
      model-platform-playground:dev python - -v < "$suite"
  done
  python3 -m unittest discover -s tests -p test_live_embedding.py -v
)
```

deployment 빌드는 `./deployment ./deployment/runtime ./deployment/app ./internal/embedding`, gateway 빌드는 `./gateway/app ./internal/embedding`의 Go 검사를 실행한다. 기존 삭제 fetcher 검사도 유지한다. DB 환경이 없는 이미지 빌드에서는 DB 통합 검사가 SKIP된다. 이 단계는 DB/GPU 통과 기록이 아니다. AppTest는 기존 UI 회귀와 새 임베딩 버튼·명시적 호출을 검사하며 API는 대역이다.

Go 대역 검사 범위: query/document 전처리, 토큰 ID 입력 재사용, 순서 변경, 개수·중복·누락·index·null·차원·유한값·norm, 공간 변경, 초과 입력, tokenizer 한도 불일치/미지원, timeout, artifact/metadata 변경, 잘못된 경로·symlink, 신규 profile과 기존 계획 보존, 준비/중지/삭제 상태, REST 경계·오류 변환, 기존 blank-profile 임베딩의 chat 거절.

새 profile의 실제 신규 배포 검증은 별도다. 이번 적용의 주 경로는 이미 실행 중인 배포 재사용이다. 원하면 기존 [Go/DB 실행 방식](model-management-update.md)에 따라 builder에서 `./deployment/app`의 DB 회귀를 실행한다. 정상 삭제·OCR·GC를 이 작업 때문에 다시 실동작시키지 않는다.

## 2. 세 서비스만 교체

위 검사가 통과했을 때 실행한다.

```bash
cd ~/workspace/agent_project/model_platform
docker compose --profile playground up -d --no-deps deployment gateway playground
```

`management`, Data, Knowledge, 실행 중인 `model-serving-*`은 이 명령의 교체 대상이 아니다. 배포 서비스 재기동은 기존 저장 계획을 읽으며 blank-profile runtime의 계획 해시·컨테이너·캐시를 변경하지 않는다. 기존의 준비/실패 상태와 삭제 동작을 그대로 유지한다. UI 주소는 `http://192.168.0.119:13100`이다.

## 3. 기존 배포로 공식 API 확인

```bash
cd ~/workspace/agent_project
python3 src/platform/model/tests/live_embedding.py \
  --deployment-id 44247297-5184-4a36-9225-7791c80d3add
```

검사는 profile을 조회하고 다른 공간·너무 긴 입력의 거절을 확인한 뒤 질문 1개와 문서 2개를 요청한다. 배포 생성/중지/삭제는 하지 않는다. 성공 출력은 `status: passed`, 실제 `registered_model_type`, 고정 artifact/tokenizer/image/space, 역할별 1,024차원·토큰 수·norm 요약이다. 원문·전체 벡터·토큰 배열을 결과 파일로 저장하지 않는다. 실제 원본과 구성이 달라 `EMBEDDING_UNSUPPORTED_PROFILE`이면 지원 검사를 우회하지 말고 아래 profile 오류를 확인한다.

profile GET의 진단용 조회:

```bash
python3 - <<'PY'
import json
from urllib.request import urlopen
from urllib.error import HTTPError
url = 'http://127.0.0.1:13000/api/deployments/44247297-5184-4a36-9225-7791c80d3add/embedding-profile'
try:
    with urlopen(url, timeout=30) as response:
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
except HTTPError as error:
    print('HTTP', error.code)
    print(error.read(4096).decode('utf-8', errors='replace'))
PY
```

UI에서는 배포 목록의 `임베딩` → 상세의 `임베딩 실행`을 누른다. 화면을 열거나 새로고침하는 것만으로 추론하지 않는다. 등록 이름에 embedding이라는 단어가 있는지로 판단하지 않고, 저장된 profile·모델 종류·지원하는 원본을 사용한다. 실제 artifact 검증은 API가 맡는다. 이미 멈춘 배포는 ready로 취급하지 않는다.

## 4. K02 저장 결과 조회

새 청킹·OCR 없이 실제 실행 옵션과 표 b90의 출처를 조회한다.

```bash
cd ~/workspace/agent_project
python3 src/platform/knowledge/tests/inspect_chunking.py \
  --run-id 6e3eaf24-c2dc-4756-87bb-83468a96c827
```

필요하면 `--run-id`를 추가해 기존 14개 결과와 비교할 수 있다. 입력 snapshot과 options가 다른 결과를 알고리즘만의 차이로 해석하지 않는다. 이 도구는 출처 범위·구조·부분 텍스트와 기존 quality를 보여주며 원본 coverage 완전성을 자동 판정하지 않는다. 결과 요약은 실행당 128 KiB 상한이며 모든 보존 테스트 결과 합계 1 MB 정책을 유지한다.

## 5. 다음 작업과 임시 파일 정리

공식 API와 K02 저장 결과를 확인했고 K04에서 실제 문서 임베딩·OpenSearch 저장·활성화를 확인했다. 다음은 K05의 외부 query embedding과 검색 연결이다. Knowledge에는 추론 컨테이너를 추가하지 않는다.

패치 적용 성공과 위 서비스 적용 확인 후 `~/workspace/`의 **전달용 압축파일과 같은 이름의 압축 해제 폴더**는 지워도 된다. 실제 `~/workspace/agent_project`, 모델 캐시·Docker volume·환경 설정은 보존한다. 패치 폴더를 프로젝트 대신 Compose 작업 디렉터리로 사용하지 않는다. 증분 적용 도구는 이름이 같은 실제 프로젝트나 runtime 데이터를 삭제하지 않는다.


<a id="runtime-check"></a>

## 2026-09-18 기존 runtime 직접 호출 기록

사용자 배포 상세·Python 출력에 근거한 당시 직접 호출 기록이다. 공식 Model API 검증과 구분한다. 이후 공식 API를 통한 실제 색인·검색은 [K04/K05](../knowledge-platform/acceptance.md)에 보존한다.

| 항목 | 사용자 제공 값 |
|---|---|
| 모델 | `dragonkue/snowflake-arctic-embed-l-v2.0-ko` |
| 관리 이름 / 버전 | 한국어 embedding / base-v1 |
| 요청 revision / 확정 revision 접두사 | `main` / `55ec6e9358a5` |
| 배포 ID | `44247297-5184-4a36-9225-7791c80d3add` |
| 모델 버전 ID | `65c3f2ff-b1fd-4a39-8dd7-aaf34c34579e` |
| 상태 | status/runtime_state=`ready`, desired_state=`running` |
| 실행 설정 | engine=`vllm`, profile=`""`, max_model_len=`2048`, GPU ID=`0` |
| 당시 호스트 주소 | `http://127.0.0.1:32782/v1` |
| 내부 주소 | `http://model-serving-44247297-5184-4a36-9225-7791c80d3add:8000/v1` |
| served_model_name | `deployment-44247297-5184-4a36-9225-7791c80d3add` |

위 포트·준비 상태는 당시 관찰값이다. 후속 작업에서 선택한 배포를 조회하고 사용하며, 현재도 존재·ready라고 가정하거나 Knowledge에 포트를 하드코딩하지 않는다. 실제 이미지 버전은 sha256 이미지 ID만으로 추정하지 않는다.

### 입력과 결과

사용자는 simsys4090에서 해당 배포의 `/v1/embeddings`에 `encoding_format: float`와 아래 입력 두 개를 한 번에 전달했다.

```json
[
  "query: 대한민국의 수도는 어디인가?",
  "대한민국의 수도는 서울이다."
]
```

응답의 model은 요청한 served_model_name과 같았고, 출력은 다음과 같았다.

| 입력 번호 | 차원 | 모든 값이 유한함 | 출력된 L2 norm |
|---|---|---|---|
| 0 | 1024 | True | 1.0 |
| 1 | 1024 | True | 1.0 |

응답 개수는 2개였다. L2 norm은 검사 스크립트가 소수점 6자리로 반올림한 값이며 수학적으로 오차가 정확히 0이라는 뜻은 아니다.

확인한 범위는 **현재 배포의 짧은 query/document 텍스트에 대한 실제 벡터 반환과 응답 형태**다. 새로운 모델 컨테이너를 반드시 만들어야 한다는 선행 조건은 해소됐다. 모델 안내는 1,024차원·질문 접두사·CLS pooling과 정규화를 설명하지만, 이번 출력만으로 실제 서버의 모든 전처리/pooling 설정을 검증한 것은 아니다. [모델 안내](https://huggingface.co/dragonkue/snowflake-arctic-embed-l-v2.0-ko)

### 해석 한계

이 직접 호출만으로 query/document 전처리, tokenizer·pooling·정규화·불변 profile, 2,048토큰 경계·배치·부분 응답·중지/timeout 처리를 검증한 것은 아니다. 현재 계약은 [embedding](../reference/embedding.md)을 따른다. 배포 ID를 조회해 공식 API로 요청하며 동적 포트를 Knowledge에 고정하지 않는다. 기존 정상 배포를 검사 목적으로 삭제·재생성하지 않는다. LiteLLM 도입은 [별도 검토](../reference/litellm-integration-review.md)로 남아 있다.
