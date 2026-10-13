# K04 적용과 검증

2026-09-21 갱신. 컴파일·bulk 수정 후 서버 Go·Python 검사와 서비스 교체가 통과했고, 실제 결과에서 48개 임베딩·OpenSearch 저장·버전 활성화를 확인했다. 후속 검색·평가까지의 근거는 [통합 검증 기록](acceptance.md)에 있다. 아래 0~4번은 최초 설치/일반 검증 절차이며 이번 문서 갱신 때문에 반복하지 않는다. 이전 bulk 오류와 수정 절차는 [별도 기록](testing-indexing.md#bulk-recovery)에 보존한다.

로컬에서 실행한 범위: Python 도구 검사 3개, Node 청킹 요청/구조 미리보기 검사, Node 인덱스 요청 재전송·CAS 보존·진행률·부분집합 검사, Python/JS/Compose 문법과 계약 사본 대조. Go·Docker·실제 브라우저 렌더링은 이 환경에 없다.

## 0. 초기 컴파일 실패 기록

첫 K04 빌드는 `set -euo pipefail`의 첫 Docker tests 단계에서 중단됐다. 일부 패키지의 ok를 전체 빌드·DB 성공으로 처리하지 않았다. 컴파일·bulk 오류는 후속 사용자 서버 검사와 실제 색인 성공으로 해결을 확인했다. 현재 통합 소스에는 수정이 포함돼 있으므로 과거 증분 패치를 순서대로 다시 적용하지 않는다. 새 설치나 관련 코드 변경에만 아래 절차를 사용한다.

## 1. 코드 검사와 이미지 준비

서버에 증분 패치를 적용한 뒤 실행한다. Go module/protobuf 생성은 Docker builder 안에서만 수행한다. 중간 명령 실패 시 멈춘다. 이 단계는 기존 실행 서비스를 교체하지 않는다.

```bash
cd ~/workspace/agent_project
(
  set -euo pipefail
  docker build --target tests -t knowledge-platform-tests:indexing ./knowledge_platform
  docker build --target builder -t data-platform-builder:indexing ./data_platform
  docker run --rm data-platform-builder:indexing go test -count=1 ./refinement/app
  python3 src/platform/knowledge/tests/run_indexing_db_checks.py
  python3 -m unittest discover -s src/platform/knowledge/tests -p test_indexing_tools.py -v
  cd knowledge_platform
  docker compose --profile jobs build knowledge-operation knowledge-service knowledge-gateway knowledge-dispatcher knowledge-reconciler chunking-job indexing-job
  cd ../data_platform
  docker compose build data-refinement
)
```

`run_indexing_db_checks.py`는 렌더링된 각 Compose에서 DB 연결/네트워크를 읽고 값을 출력하지 않는다. 기존 PostgreSQL에 **임시 test schema**를 만들어 Knowledge catalog와 Data reference 통합 검사를 실행하고 해당 schema만 정리한다. 실제 원본·인덱스를 테스트 대상으로 지우지 않는다. 별도 test DB 사용 시 각 builder에서 `KNOWLEDGE_TEST_POSTGRES_URL`/`DATA_TEST_POSTGRES_URL`을 지정해 동일 패키지를 실행할 수 있다. 환경 변수 없이 이미지 build의 unit만 통과하면 DB 통합 통과가 아니다.

추가 Node 환경이 있다면 `node src/platform/knowledge/tests/request_state_test.cjs`와 `node src/platform/knowledge/tests/indexing_ui_test.cjs`로 화면의 요청 상태를 검사할 수 있다. Node는 서버 런타임 필수 의존성이 아니다.

새 Go 검사는 벡터/profile 불일치·비유한값·부분 bulk·소유권·최종 count·TLS 설정 거절, DB에서는 같은 생성 요청·같은 배치 재전송·벡터 교체 거절·stale writer·미완료 seal 차단·동시 활성화·실패 빌드 독립·삭제/참조 해제 유실·공유 ChunkSet 보존·Data purge 경쟁을 다룬다.

## 2. 기존 OpenSearch 연결 설정

```bash
cd ~/workspace/agent_project/knowledge_platform
python3 ops/configure_indexing.py --opensearch ../src/infra/opensearch
```

이 도구는 OpenSearch Compose의 기존 접속 암호·공개 root CA·네트워크를 읽고 별도 private 설정 파일을 0600으로 생성한다. CA 개인키는 읽지 않는다. 기존 설정 파일이 있으면 덮어쓰지 않는다. 다른 사용자/암호를 운영하는 환경이면 이 연결 값을 해당 계정으로 맞춘다. 설정 출력 전체를 대화에 붙여넣지 않는다.

src/infra/OpenSearch 노드·볼륨을 생성하거나 다시 시작하지 않는다. 기존 `opensearch-net`, 두 노드, `rootCA.pem`이 준비되어 있어야 한다. 노드 이름에 대한 TLS 검증을 끄지 않는다. 설정 도구가 마지막에 실제 Compose 해석을 확인하므로 오류가 나면 서비스 교체 단계로 넘어가지 않는다.

Compose include 뒤의 전역 override를 사용한다. [Docker 공식 include/override 설명](https://docs.docker.com/compose/how-tos/multiple-compose-files/include/). 이후 Knowledge 기동에는 아래 두 Compose 파일과 두 env 파일을 함께 사용한다. 기본 파일만으로 재생성하면 인덱싱 연결 설정이 빠질 수 있다.

## 3. 영향 서비스 교체

실행 중인 청킹/인덱싱 작업이 없는 시점에 수행한다. Model Platform과 이미 배포된 임베딩 runtime은 재배포하지 않는다.

```bash
cd ~/workspace/agent_project
(
  set -euo pipefail
  cd data_platform
  docker compose up -d --no-deps data-refinement
  cd ../knowledge_platform
  docker compose --env-file .env --env-file .env.indexing \
    -f docker-compose.yml -f indexing.compose.yml \
    up -d --no-deps knowledge-operation knowledge-service knowledge-gateway knowledge-dispatcher knowledge-reconciler
)
```

새 Data 참조 table·Knowledge catalog/batch table은 각 서비스 시작 시 `CREATE TABLE IF NOT EXISTS`로 생성한다. 기존 청크·모델·canonical 내용을 수정하지 않는다. 참조 보호를 사용한 이후 Data의 이전 이미지로 단독 rollback하면 보호 기능이 사라지므로 새 reference 구현을 유지해야 한다.

화면은 `http://192.168.0.119:15100`이다. 4번 인덱스 관리에서 이름과 임베딩 배포 ID를 입력하고, 3번에서 저장된 청킹 결과를 연 뒤 **새 버전 빌드**를 누른다. ready가 되면 버전의 **활성화**를 선택한다. 삭제는 버튼 확인 후 실행하며, deleting은 물리 삭제/참조 해제 확인 중이라는 뜻이다.

## 4. 실제 48개 청크로 빌드·활성화 확인

아래 명령은 기존 ChunkSet과 임베딩 배포를 조회하여 **새 논리 인덱스와 버전 하나를 만들고 그 인덱스에 활성화**한다. 기존 활성 인덱스는 바꾸지 않는다. document 임베딩 GPU 추론이 발생한다. OCR·청킹을 다시 실행하지 않는다.

```bash
cd ~/workspace/agent_project
python3 src/platform/knowledge/tests/live_indexing.py \
  --chunking-run-id 6e3eaf24-c2dc-4756-87bb-83468a96c827 \
  --deployment-id 44247297-5184-4a36-9225-7791c80d3add \
  --state-file "$HOME/workspace/indexing_check_20260918.json"
```

정상 기대값: `status: passed`, `chunks/indexed: 48`, `references: held`, `dimension: 1024`, 생성된 버전과 active_version_id 일치. profile 공간과 runtime identity도 요약한다. 조회 시점에 자원이 없어졌으면 오류를 그대로 확인하며 과거 UUID를 대체하지 않는다.

접수 응답이 유실되거나 대기가 중단되면 같은 명령·같은 state 파일로 재실행한다. 저장된 동일 키/본문 또는 기존 버전 관찰을 사용한다. **이 파일을 지운 뒤 재실행하면 새 인덱스 요청**이 된다. 실패 버전의 추론을 자동 재시작하지 않는다. 소스와 벡터 전문은 journal에 저장하지 않는다.

실제 Data purge 차단·참조 해제와 OpenSearch 삭제는 우선 임시 schema의 통합 검사로 확인한다. 실제 사용자 원본 purge는 자동 acceptance에 포함하지 않는다. UI에서 불필요한 **이번 테스트 인덱스만** 삭제하면 해당 인덱스의 물리 삭제와 reference release 상태를 확인할 수 있다. 사용자 원본·공유 청크는 남는다.

## 5. 실패 시 확인할 내용

- `INDEXING_NOT_CONFIGURED`: 두 env/Compose 파일로 Knowledge service가 재생성됐는지 확인.
- `REFERENCE_ACQUIRE_PENDING`: 새 Data Refinement 실행 여부, 선택 canonical 존재/purge 상태 확인.
- `OPENSEARCH_HTTP_401/403`: 실제 사용자/암호·index 권한 확인. secret 값은 출력하지 않음.
- TLS/접속 오류: 기존 CA·노드 hostname·opensearch-net 연결 확인.
- `OPENSEARCH_BULK_INCOMPLETE`: 두 노드·shard 상태, 항목 오류 검사. HTTP 200도 부분 실패일 수 있음.
- Model 토큰 초과: 더 작은 chunk 설정으로 **새 ChunkSet/버전**을 만들거나 적절한 새 Model 배포를 선택. 현재 입력을 조용히 절단하지 않음.
- `INDEX_BATCH_CONFLICT`: 같은 offset에 다른 벡터가 제출됨. 동일 요청 재전송만 허용하며 강제로 덮어쓰지 않음.
- `INDEX_ACTIVATION_CONFLICT`: 인덱스 상세를 새로 읽고 사용자가 현재 버전을 기준으로 다시 선택.
- `REFERENCE_RELEASE_PENDING`: 물리 인덱스 삭제 후 Data 응답 대기. 유지보수 루프가 재시도하며 삭제 완료로 표시하지 않음.

사용자 서버 빌드/DB 검사와 최종 live_indexing 성공 요약을 확인했으므로 다음은 K05 질문 벡터 검색이다. K03의 이미 통과한 GPU/배포·OCR·GC 검사를 문서 갱신 목적으로 반복하지 않는다.


<a id="bulk-recovery"></a>

## Bulk 오류 진단과 실패 기록 재사용

사용자 서버에서 OpenSearch 설정과 Knowledge 서비스 5개 교체가 완료됐고, 실행은 `protecting → queued(48) → indexing → failed`로 진행됐다. 오류는 `INDEX_BATCH_UNCONFIRMED OPENSEARCH_HTTP_400`, 확인된 저장 수는 0개다. 무문서 bulk 검사 결과는 다음과 같다.

| 검사 | 실제 서버 응답 |
|---|---|
| query `require_alias=true` | `no requests added` — 빈 요청 검증까지 진행 |
| metadata `_require_alias=true` | `unknown parameter [_require_alias]` — 파싱 거절 |
| metadata `require_alias=true` | `no requests added` — 빈 요청 검증까지 진행 |

이 결과는 옵션 파싱을 확인하며 문서 저장 성공을 뜻하지 않는다. 클라이언트 action metadata를 `require_alias`로 고쳤다. URL의 `require_alias=true`, alias 대상, shard 확인, 응답 항목별 성공 검사와 동일 벡터 재전송 규칙은 유지한다. 회귀 검사는 각 action JSON을 파싱하여 허용 필드와 값을 검사한다. 이전의 잘못된 필드명을 기대하던 대역 검사도 수정했다.

HTTP 오류에는 제한된 오류 type과 unknown parameter 이름을 추가한다. arbitrary reason·문서·벡터·전체 응답은 출력하지 않는다. 이번과 같은 오류는 `OPENSEARCH_HTTP_400: illegal_argument_exception; unknown parameter [_require_alias]`로 구분할 수 있다.

컴파일 수정 뒤 서버 Go 전체·Data refinement·Knowledge/Data PostgreSQL 통합 검사·Python 3개·이미지 8개 빌드가 통과했다. 이번 bulk 수정도 사용자 서버에서 Python 도구 6개와 Go 전체 검사·Knowledge service 빌드·기동이 통과했다. 이후 실제 48개 저장·활성화가 성공했다. 작성 환경에서 Go/Docker 또는 서버를 직접 실행한 결과와는 구분한다.

### 실패 후 새 버전 검증

**현재 사용자의 완료 경로:** 이전 실패 journal을 삭제해 아래 `--retry-failed-from` 경로는 파일 읽기에서 중단됐다. 해당 옵션을 빼고 별도 `--state-file`로 실행하여 새 논리 인덱스의 48개 저장·활성화를 확인했다. 아래 재시도 예시는 이전 journal이 남아 있는 경우에만 해당한다. 성공 journal은 보관하고 다시 색인할 필요는 없다.

기존 `indexing_check_20260918.json`은 failed 버전을 가리킨다. 같은 파일만 재실행하면 해당 실패 상태를 다시 조회한다. DB 상태를 강제로 바꾸거나 그 파일을 지우지 않는다.

새 `--retry-failed-from` 옵션은 원래 journal과 서버의 실패 버전을 대조하고, 같은 논리 인덱스에 **새 버전 하나**를 만든다. 원래 실패 버전·물리 인덱스·Data 참조와 journal은 보존한다. `--state-file`에는 아래 별도 파일을 사용한다. 기존 ChunkSet과 배포를 재사용하지만 문서 임베딩 추론은 새 버전에서 다시 실행된다.

```bash
cd ~/workspace/agent_project
python3 src/platform/knowledge/tests/live_indexing.py \
  --chunking-run-id 6e3eaf24-c2dc-4756-87bb-83468a96c827 \
  --deployment-id 44247297-5184-4a36-9225-7791c80d3add \
  --retry-failed-from "$HOME/workspace/indexing_check_20260918.json" \
  --state-file "$HOME/workspace/indexing_check_bulk_fix_20260921.json"
```

접수 응답 유실이나 대기 중단 시 **위 명령과 두 파일 경로를 그대로 재사용**한다. 새 파일에 저장된 동일 키·본문을 재전송하거나 이미 생성된 버전만 관찰한다. 명령을 반복할 때마다 버전을 추가하지 않는다. 새 버전도 failed가 되면 그 상태를 출력하고 멈춘다.

보호 조건: 원래 버전이 failed이며 동일 index/ChunkSet/배포인지 확인한다. 도구는 아직 활성 버전이 없는 테스트 인덱스에서만 새 재검증을 시작하고, 빌드 중 다른 버전이 활성화되면 자동 교체를 중단한다. 운영 인덱스의 활성 버전 선택은 UI에서 수행한다.

성공 기대값: `status: passed`, `chunks: 48`, `indexed: 48`, `references: held`, `dimension: 1024`, 생성 버전과 `active_version_id` 일치. 오류가 나면 마지막 상태와 오류 문자열을 확인한다. 실제 성공 전 K04 전체 완료나 검색 구현 완료로 표시하지 않는다.

### 보관

검증이 끝난 패치 tar.gz와 해제 폴더는 삭제할 수 있다. 실제 프로젝트, 두 journal, 인프라 볼륨은 유지한다. 원래 failed 버전의 Data 참조는 기록 보존 정책에 따라 계속 held다. 새 버전 성공 확인 뒤 필요 없는 failed 버전을 UI에서 명시적으로 삭제하면 물리 인덱스 제거 확인 후 참조를 해제한다. 공유 청크·원본은 해당 버전 삭제로 제거하지 않는다.


<a id="hnsw"></a>

## HNSW 검증 범위

새 인덱스 버전의 빌드 입력으로 m과 ef_construction을 받는다. 현재 상태는 소스 구현·로컬 순수 Go 2개·평가 Python 40개·Node UI 스크립트 3개 통과다. 이후 사용자 서버에서 32개 파일 적용, Python 40개, 전체 Go/gRPC 빌드·PostgreSQL 통합, service/Gateway 기동과 고급 옵션 표시를 확인했다. [서버 요약](../../src/platform/knowledge/tests/results/hnsw-server-20260922.json). 이후 사용자 제공 버전 JSON과 새 화면에서 custom 32/256 설정의 42/42 ready·기본 버전 지정·검색을 확인했다. [확인 기록](../../src/platform/knowledge/tests/results/console-ui-server-20260922.json). OpenSearch 원시 매핑을 따로 조회한 결과는 아니다. 기존 검색 UI의 ON/OFF·잘못된 후보 수 안내·관리 선택 전환은 사용자 확인을 받았다.


- 로컬 Go 2개: 실제 config.go와 기존 chunking JSON 검증 소스를 독립 임시 module에서 실행했다. 기본/부분 입력, 경계, 잘못된 형식, 저장 설정 검증이다. 임시 module을 프로젝트에 추가하지 않았다.
- 로컬 Python 40개: 기존 평가 37개와 HNSW 기록·추론 전 거절·기존 보고서 비교/동일 버전 변조 거절 3개.
- 로컬 Node 3개 스크립트: HNSW 기본/부분/사용자 정의 입력, 범위 거절, 보류 요청 고정, 기존/신규 버전 표시와 기존 관리·검색 회귀.
- 서버 tests target에서 실행할 Go 검사: REST 전달/거절, Version 구형 호환, custom 매핑·불일치 시 삭제 차단, 전체 코드 빌드.
- 서버 PostgreSQL 검사: 구형 응답/키 재전송, 설정 저장·충돌, 직접 RPC 검증, 부모 미상속·비자동 활성화. 기존 배치/봉인/활성화/삭제 통합 흐름도 custom 32/256으로 검사한다. Model/Data/OpenSearch는 이 DB 검사의 대역이다.

구형 service는 custom 설정을 이해하지 못한다. 관련 코드 반영 시 service를 먼저, gateway를 다음에 교체하며 전환 중 새 빌드 요청을 보내지 않는다. custom 버전 생성 뒤 service만 구형으로 되돌리면 검색/관리 호환성을 보장하지 않는다. OpenSearch 설정을 적용하는 주체는 service이고 Job은 임베딩·배치 등록을 맡는다.

기존 활성 버전을 자동 전환하지 않는다. 42개/48개 자료의 성공은 HNSW 튜닝의 품질·성능 개선 근거가 아니다. [로컬 요약](../../src/platform/knowledge/tests/results/hnsw-local-20260922.json)과 서버 기록을 구분한다.
