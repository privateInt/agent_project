# K06 Model reranker 적용·검증

2026-09-21. `Dongjin-kr/ko-reranker`의 배포 profile, 후보별 점수 API와 화면을 구현했다. **Python 42개, profile 수정 후 Deployment/Gateway Go·PostgreSQL 두 통합 검사·세 서비스 교체를 사용자 서버에서 확인했다. 후속 실제 점수 화면과 `live_reranking.py` 통과도 확인해 K06 Model 인수를 완료했다.** Go 문법 검사는 컴파일 검증과 구분한다. [로컬 확인 요약](../../src/platform/model/tests/results/reranking-k06-20260921.json), [API 계약](../reference/reranking.md), [작업 목록](../knowledge-platform/work-plan.md)을 함께 따른다.

현재 서버의 [K06 실제 확인](../knowledge-platform/acceptance.md#k06)을 보존한다. 최신 통합 소스에는 profile·Playground import 수정이 포함된다. 아래 빌드/추론 명령은 새 설치나 관련 코드 변경 후에만 사용하며 과거 증분 패치를 순서대로 다시 적용하지 않는다.

Knowledge의 rerank on/off 연결은 K07이다. 이번에는 Model이 질문·후보 ID/text를 받아 모든 후보의 점수를 반환하는 기반을 추가했다. 점수는 원래의 단일 logit이며 확률이 아니다. 기존 48개 인덱스와 embedding 배포는 재사용한다.

## 1. 확인 전제

기존 준비된 reranker 배포가 있으면 재사용한다. [재배포](model-management-update.md#redeploy)는 필요한 중지 배포에만 명시적으로 수행하고, 실제 성공 여부를 별도로 기록한다.

## 2. Python·Go·DB 검사와 이미지 빌드

Python은 표준 라이브러리만 사용한다. 새 검사 8개와 기존 관리·임베딩 경계 34개를 실행한다.

```bash
cd ~/workspace/agent_project
(
  set -euo pipefail
  python3 -m unittest discover -s src/platform/model/tests -p 'test_live_reranking.py' -v
  python3 -m unittest discover -s src/platform/model/tests -p 'test_playground.py' -v
  python3 -m unittest discover -s src/platform/model/tests -p 'test_model_management_ui.py' -v
  python3 -m unittest discover -s src/platform/model/tests -p 'test_live_embedding.py' -v
  docker build --target build-deployment \
    -t model-platform-reranker-tests:check ./model_platform
  python3 src/platform/model/tests/run_reranking_db_checks.py
)
```

`build-deployment`는 컨테이너 안에서 protobuf/Go module을 생성하고 Deployment·runtime·embedding/reranking Go 검사를 실행한다. DB 주소가 없는 빌드 단계에서는 DB 통합 검사가 skip된다. 이어지는 `run_reranking_db_checks.py`가 Model Compose의 기존 PostgreSQL 연결·네트워크를 사용해 **격리된 임시 schema에서 두 통합 검사를 실제 실행**한다. 실제 모델/컨테이너 대신 대역으로 ready 상태·identity·추론 중 중지·tokenizer 변조를 검사하며 종료 시 schema를 정리한다. 운영 배포·GPU·Knowledge 인덱스를 만들지 않는다. 비밀번호는 명령 출력에 표시하지 않는다.

그다음 Gateway·Deployment·Playground만 빌드한다. Gateway 빌드에도 새 파서·HTTP 오류/응답 검사와 내부 추론 검사가 포함된다. 빌드 실패 시 아래 서비스 교체는 실행하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose --profile playground build deployment gateway playground
  docker compose --profile playground up -d --no-deps deployment gateway playground
)
```

호스트의 `go.mod`, `go.sum`, 생성된 protobuf 파일은 추가하지 않는다. 별도 inference 서비스나 Compose 설정은 추가하지 않았다. `deployment`는 제어 서비스이며 기존 embedding의 `model-serving-*` 컨테이너를 자동 재생성하지 않는다.

## 3. 등록된 한국어 reranker 배포

Model 화면 `http://192.168.0.119:13100`에서 모델 목록을 새로 조회한다.

1. Hugging Face 원본이 `Dongjin-kr/ko-reranker`인 등록 모델의 준비된 원본 버전을 선택한다. 기존 등록 종류가 `llm`이어도 이 원본은 재정렬용으로 인식한다. 다시 가져오거나 등록 이름을 바꿀 필요 없다.
2. **배포**를 열어 사용할 GPU와 메모리 비율을 선택한다. 질문+후보 길이는 이 모델의 한도인 512토큰으로 고정되어 있다. 기존 embedding과 같은 GPU를 쓰면 현재 여유 메모리에 맞게 선택한다.
3. 배포가 ready가 되면 상태가 **재정렬용**으로 표시된다. **재정렬 → 점수 계산**에서 짧은 질문과 두 후보로 확인한다. 페이지 조회만으로 추론하지 않는다.
4. 새 배포 UUID를 복사한다. embedding 배포 ID를 이 검사에 넣으면 안 된다.

이 버전의 전용 배포 profile은 `ko-reranker-v1`이다. 기존에 profile 없이 배포했던 reranker는 자동 전환하지 않으므로 해당 버전으로 새 배포를 선택해야 한다. 새 프로필은 현재 기본 이미지 `vllm/vllm-openai:v0.15.0`에서 pooling runner와 activation 비활성화를 사용한다. 사용자가 `SERVING_IMAGE`를 다른 버전으로 설정했다면 API 호환성은 실제 검사로 확인한다. 실패 시 모델 ID·배포 상태·오류 코드를 보존하고 실패한 배포를 반복 생성하지 않는다.

## 4. 실제 API 인수

아래 `RERANKER_DEPLOYMENT_ID` 값을 3단계의 실제 UUID로 바꾼다.

```bash
cd ~/workspace/agent_project
RERANKER_DEPLOYMENT_ID='여기에-재정렬-배포-UUID'
python3 src/platform/model/tests/live_reranking.py \
  --deployment-id "$RERANKER_DEPLOYMENT_ID" \
  --summary-file "$HOME/workspace/reranking_check_20260921.json"
```

도구는 선택한 배포의 profile을 조회하고 정상 score 요청 2회, profile 불일치·빈 후보·중복 ID·알 수 없는 필드·길이 초과 거절을 검사한다. ID/index/실행 identity·유한한 단일 점수·pair 토큰 수를 검증하고 후보 순서를 바꿔도 ID별 점수가 유지되는지 확인한다. 짧은 서울/사과 예제의 상대 순위는 API smoke이며 전체 검색 품질 평가가 아니다. 긴 입력은 tokenize까지 호출하며 score 실행 전에 거절되어야 한다. 도구는 새 배포·중지·색인·삭제를 수행하지 않는다.

정상 결과는 `status: passed`, `valid_scoring_requests: 2`, `lifecycle_changes: 0`이다. 요약 파일은 0600 권한으로 저장하며 질문·후보 본문이나 원시 토큰 배열을 넣지 않는다. 동시 중지 거절은 앞선 DB 통합 검사에서 확인하고 이 도구가 실제 공유 배포를 중지하지는 않는다. 실제 브라우저 동작과 GPU 결과는 서버 실행 로그로 별도 확인한다.

## 5. 확인 후 이어갈 작업

K06 Go·DB와 실제 API·화면 결과를 수령해 [확인 기록](../knowledge-platform/acceptance.md#k06), 프로젝트 상태와 작업 목록에 반영했다. 후속 [K07](../knowledge-platform/acceptance.md#k07)에서 Knowledge의 선택적 rerank, candidate_k/top_k 분리, 후보 ID 대조·최종 순위·실패 시 오류 반환을 확인했다. rerank off의 Model 호출은 0회로 유지한다.

소스 적용을 마쳤으면 업로드한 패치 압축파일과 해제 폴더는 지워도 된다. 실제 `agent_project` 폴더와 색인 journal·검색/재정렬 확인 JSON은 보관한다. 소스 전체 압축파일은 DB·MinIO·볼륨·이미지 백업이 아니다.


<a id="resolved-issues"></a>

## 해결된 profile·화면 모듈 오류

### 배포 RPC profile 경계

`CreateServingDeployment`의 이전 허용 목록에 `ko-reranker-v1`이 빠져 있었다. runtime의 profile 검증은 통과하지만 RPC 입구에서 OCR 요청으로 거절한 구현 누락이다. 요청이 DB 생성 경로에 들어가기 전에 실패했으므로 해당 두 검사는 이후 추론·중지 검증까지 도달하지 못했다. 빌드 단계의 DB 검사는 연결 설정이 없어 skip되므로 이 누락을 잡지 못했다.

지원 profile 검증은 기존 `Normalize`에 맡기고, 일반 Serving RPC는 vLLM을 요구하며 Surya만 전용 OCR RPC로 구분하도록 수정했다. 알 수 없는 profile은 계속 거절한다. DB 없이 RPC 입구를 검사하는 회귀 테스트 1개(LLM·embedding·reranker·Surya·unknown·demo 6개 사례)를 추가했다. 실제 DB 두 통합 검사는 그대로 유지했다.

후속 사용자 로그에서 이 패치 10개 적용·재적용 0개, 수정본 Deployment/Gateway Go 검사·빌드, PostgreSQL 두 통합 검사와 세 서비스 교체를 확인했다. 이 문서의 절차는 현재 서버에서 완료했다. 이어서 보고된 화면 import 오류의 [Playground 수정](reranking.md#resolved-issues) 후 정상 점수 화면과 [실제 API 성공](../knowledge-platform/acceptance.md#k06)도 확인했다.

### Playground 모듈 누락

Playground Dockerfile이 Python 파일을 개별 나열해 복사하면서 `reranking_view.py`와 그 의존 파일 `reranking_validation.py`를 누락했다. 기존 빌드 검사는 일부 모듈만 import하고 화면 파일은 문법 검사만 하므로 빌드가 통과했다.

복사를 `COPY --chmod=0644 playground/*.py /app/`로 통합하고, 기존 비root 빌드 검사에서 진입점 `app`을 실제 import하도록 변경했다. `app`의 `main()`은 직접 실행할 때만 호출하므로 import 검사가 Gateway를 조회하거나 배포를 시작하지 않는다. 새 파일 누락도 앱이 의존하면 빌드 단계에서 실패한다. 패키지 추가나 버전 변경은 없다.

고정 의존성 Streamlit 1.63.0을 사용해 Dockerfile이 복사할 파일을 별도 경로에 구성하고 앱 import·초기 화면 AppTest를 확인했다. 두 파일을 각각 빼면 진입점 import가 실패하는 것도 확인했다. 후속 사용자 출력에서 정상 재정렬 점수 화면과 `live_reranking.py` 통과를 [확인](../knowledge-platform/acceptance.md#k06)했다. 이 수정의 Docker 빌드 원문 로그는 별도로 제공되지 않았다.

완료된 개별 패치 설치 명령은 제거했다. 현재 코드 변경 후 검증·빌드는 이 문서의 공통 절차를 사용한다.
