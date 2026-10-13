# 청킹 빌드·검증·적용

## 2026-09-21 책임 경계 확정·평가 진단 서버 확인

개선 structure 48개·recursive 44개의 같은 입력 미리보기·색인과 12문항 비교는 [확인 기록](acceptance.md#k08)을 따른다. 개선 structure ON은 12/12이고 recursive ON의 원래 오류 1건은 보존한다. body-03 단일 재확인은 통과했지만 최초 불일치 원인은 미확정이다. [평가 진단 보완](evaluation.md#candidate-diagnostics)은 사용자 서버에서 17개 파일 적용과 테스트 37개 통과를 확인했다.

PDF 파싱·OCR·제목/본문/표 구조 판별은 Data가 소유한다. Knowledge는 확정 canonical의 구조·출처·품질을 보존하며 청킹·Model 임베딩 요청·색인·검색·선택적 rerank·인덱스 관리를 담당한다. 제목 추정 규칙 추가는 Data의 별도 보류 과제이며 Knowledge의 작업·완료 조건에서 제외한다. 다음은 Knowledge 관리·검색 화면 확인과 삭제·참조 해제 검증이다. 기존 canonical·평가 정답셋·기준 인덱스를 보존하고 완료한 전체 평가·GPU 검사를 반복하지 않는다.

## 2026-09-21 K04 색인·활성화 확인 (당시 기록)

사용자 서버 로그로 K03 패치 53개 적용, Deployment/Gateway Go 빌드 검사, 이미지 3개 빌드, Python/UI 78개 검사와 공식 임베딩 API를 확인했다. 기존 배포 `44247297-5184-4a36-9225-7791c80d3add`를 재사용했으며 query 1개·document 2개 모두 1,024차원·유한값·L2 norm 1.0이다. 공간 불일치·길이 초과 거절도 확인했다. 등록 유형은 legacy `llm`, 검증 capability는 `embedding`이다. DB 통합 검사와 새 profile GPU 배포는 이 로그의 검증 범위가 아니다.

K02 저장 실행 `6e3eaf24-c2dc-4756-87bb-83468a96c827`은 문서 2개, `structure_v1 / 2026-09-18.2`, 최대 1,000자·겹침 100자, 48개(text 38/table 10), 최대 997자다. b90은 992+495자로 나뉘며 기록된 출처 범위의 합집합은 0..1352, 반복 문맥 135자의 동일성을 확인했다. 원본의 needs_ocr와 미인식 이미지 4개 제외는 유지한다. 이후 같은 입력의 두 청커 미리보기·품질 비교를 확인했으며 최신 인수 기록을 따른다.

K04 핵심 빌드·저장·활성화를 사용자 서버에서 확인했다. 컴파일 수정 후 Knowledge 전체 Go·Data refinement·Knowledge/Data PostgreSQL 통합 검사가 통과했고, bulk 수정 후 Go 전체·Python 도구 6개와 Knowledge service 교체가 통과했다. 실제 결과는 `ready`, 48/48개 저장, 1,024차원, `references: held`, 생성 버전과 활성 버전 일치다. 기존 Model 임베딩 runtime을 재사용했다. [확인 근거와 식별자](acceptance.md#k04)를 보존하고 다음은 K05 외부 질문 벡터 검색이다. 실제 OpenSearch 버전 삭제·참조 해제와 검색 품질은 별도 미검증 범위다.

기준일: 2026-09-18. 첫 버전과 후속 구조 분할의 근거를 구분한다.

| 검사 | 확인 범위 |
|---|---|
| 첫 `.1` 소스 서버 적용 | 사용자 로그: 98개 파일 적용, Knowledge Go 검사 및 Data refinement/gateway 검사 성공 |
| 첫 `.1` 이미지·기동 | 사용자 로그: Job와 상시 이미지 빌드, Knowledge 5개 컨테이너 Up |
| 첫 `.1` 실제 입력 | 사용자 화면: 최대 8,000자에서 14개 청크 저장·조회. 1,352자 표 b90 제한 문제 확인 |
| 후속 `.2` Node 검사 | 요청 상태와 구조 분할 표시 대역 검사 통과, JS 문법 통과 |
| 후속 `.2` 표 자료 확인 | 기존 OCR 표 fixture의 원문-셀 대응 및 크기 충족 가능성 확인(Python 보조 확인; Go 실행 아님) |
| 후속 `.2` Go·Docker·실제 입력 | 사용자 로그: 증분 24개 적용, chunking 단위·전체 Go 검사와 빌드·기동 통과. 화면: 실행 `6e3eaf24-c2dc-4756-87bb-83468a96c827`, 48개 저장. 후속 결과 조회에서 실제 1,000/100 옵션·표 b90 spans와 반복 문맥까지 확인 |
| DB 통합·실제 브라우저 자동화 | 후속 K04에서 Knowledge service/app·Data refinement/app PostgreSQL 통합 검사를 확인. 브라우저 자동화는 미실행이며 사용자 저장/조회 화면과 구분 |

## 이미 첫 버전을 적용한 서버의 후속 반영

이 절은 아직 `.2`를 적용하지 않은 환경의 설치 절차다. 사용자 서버에서는 이미 검사·빌드·기동과 48개 저장을 확인했으므로 문서 갱신 때문에 반복하지 않는다. 미적용 환경에서 증분 적용 후 다음 묶음을 실행한다. 검사·빌드 실패 시 서비스 재기동 단계에 도달하지 않는다. Data/Model은 이번 패치에서 수정하지 않는다. 기존 DB·버킷·저장 청크를 초기화하지 않는다.

```bash
(
  set -eu
  cd ~/workspace/agent_project/knowledge_platform
  docker build --target core-tests -t knowledge-chunking-core-tests:dev .
  docker build --target tests -t knowledge-platform-tests:dev .
  docker compose --profile jobs build chunking-job
  docker compose build knowledge-service knowledge-gateway
  docker compose up -d --no-deps knowledge-service knowledge-gateway
  docker compose ps
)
```

`core-tests`는 외부 모듈 없이 청킹 패키지만 실행한다. `tests`는 전체 Go 검사이며 전용 DB URL이 없으면 DB 검사는 skip된다. 새 Job 이미지를 먼저 만든 뒤 새 작업을 받는 서비스와 Gateway를 갱신한다. 기존 `.1` 결과는 그대로 조회하며 이전 실패 실행의 옵션을 덮어쓰지 않는다.

화면을 새로고침하고 **같은 정제 버전 → 최대 1,000자/겹침 100자 → 새 청크 생성**으로 확인한다. 성공한 14개 결과는 비교 기준으로 남긴다. 표 조각의 본문 행·반복 문맥·출처와 최대 길이를 확인한다. 헤더/병합 문맥과 한 행만으로 한도를 넘으면 필요한 글자 수를 표시한다. 행 대응이 불가능하면 원문을 조용히 잘라내지 않는다.

아래는 처음 설치하는 경우의 절차다. 이미 첫 버전을 적용한 서버에서 Data 빌드·기동을 반복할 필요는 없다.

## 1. 소스 반영

서버 기준 경로는 `~/workspace/agent_project`다. 별도 증분 전달본의 `apply.py`는 변경 전 파일 해시를 확인해 예상과 다른 서버 변경이 있으면 쓰기 전에 중단한다. 전체 프로젝트 압축본은 별도 디렉터리에 풀어 비교할 수 있다. 현재 프로젝트 전체를 삭제하거나 DB/MinIO/볼륨을 초기화하지 않는다.

## 2. 빌드와 코드 검사

다음 명령 묶음은 실패하면 즉시 종료한다. 변경된 Data refinement/gateway와 신규 Knowledge만 대상으로 한다. 기존 Data가 처리 중인 작업이 없는 시점에 서비스 재기동 부분을 실행한다.

```bash
(
  set -eu
  cd ~/workspace/agent_project/knowledge_platform
  docker build --target tests -t knowledge-platform-tests:dev .

  cd ../data_platform
  docker build --target builder -t data-platform-builder:knowledge-chunking .
  docker run --rm data-platform-builder:knowledge-chunking \
    go test -count=1 ./refinement/app ./gateway/app
)
```

DB 통합 검사는 별도 접속 환경 변수가 없으면 skip된다. unit 통과를 DB 통합 통과로 표시하지 않는다. `src/platform/knowledge/internal/testsupport`는 `KNOWLEDGE_TEST_POSTGRES_URL`이 주어진 테스트 DB에서 임시 schema를 생성하고 해당 schema만 정리한다.

DB 검사까지 수행하려면 테스트 전용 PostgreSQL URL을 환경에 준비하고 다음을 실행한다. 명령 자체가 접속 값을 출력하지 않는다.

```bash
(
  set -eu
  cd ~/workspace/agent_project/knowledge_platform
  docker run --rm --network ops-infra \
    -e KNOWLEDGE_TEST_POSTGRES_URL \
    knowledge-platform-tests:dev go test -count=1 ./...
)
```

실제 네트워크 이름을 바꾼 환경이면 `ops-infra`도 맞춰야 한다. 운영 원본을 테스트 입력으로 자동 생성/삭제하지 않는다.

## 3. 영향 서비스와 Knowledge 기동

```bash
(
  set -eu
  cd ~/workspace/agent_project/data_platform
  docker compose build data-refinement data-gateway
  docker compose up -d --no-deps data-refinement data-gateway

  cd ../knowledge_platform
  if [ ! -f .env ]; then
    python3 ops/configure.py --model-platform ../model_platform
  fi
  docker compose --profile jobs build chunking-job
  docker compose build knowledge-operation knowledge-service knowledge-gateway knowledge-dispatcher knowledge-reconciler
  docker compose up -d knowledge-operation knowledge-service knowledge-gateway knowledge-dispatcher knowledge-reconciler
  docker compose ps
)
```

configure는 기존 Model Compose의 공유 인프라 연결값을 읽어 Knowledge 전용 설정 파일을 생성한다. 기존 설정이 있으면 덮어쓰지 않는다. Data Collection/Refinement는 같은 Docker 네트워크에서 실행 중이어야 한다. 기본 내부 주소는 `data-collection:9000`, `data-refinement:9000`이다.

기본 화면/API 주소는 `http://192.168.0.119:15100`, 서버 로컬 주소는 `http://127.0.0.1:15100`이다. 실제 서버 IP가 다르면 호스트만 변경한다. `/health`는 Gateway 자체 응답 확인이고 전체 서비스 검증이 아니다.

## 4. 실제 입력 검증

1. 기존 Data의 정제 완료 자료를 화면에서 조회한다. 우선 짧은 문장·일반 문단 자료부터 선택한다.
2. `structure_v1`으로 생성하고 전달 상태, Operation 상태, 저장된 청크·출처·경고를 확인한다.
3. 완료된 결과에서 같은 입력의 다른 청커를 준비해 `recursive_character_v1`을 실행한다. 제목 경계가 있는 입력으로 실제 차이를 확인한다.
4. 한글/emoji, 페이지 없는 문서, 실제 표·코드·읽힌 그림 글자·미인식 영역을 확인한다. 큰 표는 명시적 크기 오류가 날 수 있다.
5. 같은 key 재전송으로 새 실행이 중복 생성되지 않는지 검사한다. Job 결과 등록 후 Operation 보고가 유실된 경우 Reconciler 관찰도 통합 검사 대상이다.

API 검증 도구는 **이미 존재하는 canonical ID**를 인자로 받는다. 수집/OCR은 생성하지 않고 청킹 작업 두 개만 생성한다. 식별자는 현재 화면에서 확인한 값으로 바꾼다.

```bash
cd ~/workspace/agent_project/knowledge_platform
python3 -B tests/live_chunking.py --canonical-id '<현재 canonical ID>'
```

도구가 기록하는 snapshot hash가 두 전략에서 같아야 한다. 두 전략의 청크 개수가 항상 달라야 한다는 검사는 하지 않는다. 차이는 제목·블록 경계가 있는 대표 입력에서 확인한다. script의 기본 4,000자도 큰 표를 모두 수용한다는 보장은 없다.

## 5. 추가 로컬 검사와 문제 확인

Node가 있는 환경에서 요청 상태 검사:

```bash
node tests/request_state_test.cjs
```

Playwright와 Chromium이 있는 개발 환경에서는 `node tests/console.spec.cjs`로 실제 DOM 동작을 대역 API와 확인할 수 있다. 이 브라우저 검사도 Go/DB/Docker 통합 검사와 별개다.

실행 문제 확인:

```bash
docker compose logs --tail=100 knowledge-service knowledge-operation knowledge-dispatcher knowledge-reconciler
```

화면에 결과가 없다면 전달 상태/오류 → Operation phase/오류 → Job 컨테이너 로그 순서로 확인한다. `submission_status=blocked`를 완료로 표시하지 않는다. 큰 구조 오류는 최대 문자 수 조정 또는 해당 구조 처리 개선으로 해결하고 원문을 조용히 자르지 않는다.

후속 Model 임베딩·색인·검색·재정렬과 두 청커 비교는 [통합 검증 기록](acceptance.md)에 있다. 현재 다음 작업은 [작업 목록](work-plan.md)을 따른다.


<a id="structure-context"></a>

## 문맥 보존·구버전 호환 검사

Go 1.25.0에서 청킹 패키지 **26개 테스트(하위 사례 19개 별도)**가 통과했다. 그중 신규 상위 테스트는 9개다. 여러 짧은 블록의 0/1/10/49자 overlap, 한글·emoji 위치, 연속 제목과 긴 본문, 정확한 길이 한도·긴 제목·짧은 사실, 표/코드/제외/절/문서 경계, 원문 전체 보존, 결정성, 지원 버전 거절을 검사했다.

수정 전 `.2` 소스로 생성한 완전한 결과 해시 6개와 수정 후 결과를 대조했다. 두 청커의 `.1`·`.2` 결과 네 개와 `.2` 구조 분할 결과 두 개가 바이트 단위로 동일하고 저장 검증도 통과했다. recursive의 신규 접수 버전과 정책 정체성은 바뀌지 않았다.

Python 실행 도구 테스트 **4개**도 통과했다. 기존 실행에서 동일 canonical 목록·옵션·snapshot을 가져와 두 미리보기 실행을 만드는 흐름, 구버전 서비스의 사전 거절, 옵션 변경·잘못된 한도의 사전 거절, 입력 snapshot 변화 시 다음 실행 중단을 대역으로 확인했다. 실제 서버·Docker·DB·모델 추론은 이 로컬 검사의 범위가 아니다.

실제 서버의 같은 입력 비교는 [K08](acceptance.md#k08)에 보존한다. 다시 미리보기를 만들 필요가 있을 때 `tests/live_chunking.py --baseline-run-id <실제 기준 실행 UUID> --expected-structure-version 2026-09-21.1`을 사용한다. 기준 canonical 목록·snapshot·옵션을 고정하고 두 청커의 CPU 미리보기 작업을 새로 만든다. 접수 ID를 먼저 출력하므로 중단되면 해당 ID부터 조회한다. 재실행하면 새 작업이 생긴다. 임베딩·색인·검색·활성화·삭제는 호출하지 않는다.

청커 버전 코드를 변경해 반영할 때는 실행 중인 청킹 작업을 확인하고 chunking-job 이미지를 먼저, 접수 service를 다음에 갱신한다. 기존 완료 결과는 덮어쓰지 않는다. 100자 overlap이 모든 질문의 필요한 문맥을 보장하는 것은 아니다.
