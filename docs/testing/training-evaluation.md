# 학습·평가 분리와 비교 검증

작성 환경 Python **119개(기존 115개 + MLflow 연결 회귀 4개) PASS**, 평가 관련 20개 PASS. 사용자 서버에서 **Go 테스트 106개·하위 사례 103개, 실제 PostgreSQL·Compose/Bake 구성 검사까지 PASS**했다. RUN/PASS 209개의 이름이 일치하고 실패·SKIP은 없으며 새 평가 테스트 15개를 포함한다. 이 Go 테스트의 Docker·Management·MLflow·객체 저장소 관찰은 대역을 사용했다.

MLflow ID 보존 수정 후 첨부 `붙여넣은 텍스트 (1)(20260910-045004).txt`에서 **실제 GPU 1 step/2 step 학습·평가, 실제 MLflow 지표 일치·FINISHED, checkpoint를 켠 두 번째 학습, 읽기 전용 비교까지 PASS**를 확인했다. 학습별 TrainingRun·Operation은 succeeded이고 새 adapter 2개는 ready다. 이후 두 번째 adapter의 **실제 vLLM ready·채팅 응답·멱등 재요청·중지까지 PASS**했다. 평가부터 산출물 서빙까지 이 단계의 기능 연결 검증을 완료했으며 Streamlit 관리/채팅도 구현·서버 검사를 완료했으며 현재 남은 범위는 [프로젝트 상태](../status.md)를 따른다. 이번 문서 갱신 때문에 빌드·이미지 교체·완료한 테스트를 반복할 필요 없다.

| 실행 | 평가 loss | 조건부 perplexity | 평가 레코드 / 채점 토큰 |
|---|---:|---:|---:|
| 1 step | 6.34745979309082 | 571.0403045636771 | 2 / 6 |
| 2 step | 5.107982158660889 | 165.33639543489562 | 2 / 6 |

같은 평가 버전·base·tokenized/compatibility digest를 사용했고 comparable=true, 두 번째 loss 차이는 -1.2394776344299316이다. 성공 보고서는 `tests/results/evaluation/f248d855-cd23-4c7c-bc4d-ee3900ab9853.json`, 두 번째 adapter는 `19d02f67-9568-4802-aa0a-1495f2e3b28b`다. 별도 서빙 보고서 `tests/results/serving/6b0fd84d-28a6-46bf-9081-e36032a3674f.json`에서 같은 adapter의 추론·중지까지 검증했다.

첫 실패 요청 `6927676d-457e-42d3-af48-fdbb2a5b6a7c`는 그대로 보존한다. MLflow 실행 ID를 학습 전에 보존하는 수정은 새 요청에서 검증됐다. 과거 오류와 수정 경위는 [개발 이력](../history/model-platform-202609.md#2026-09-10-04-training-evaluation)에 남겼다.

동작·지표·입력 한도는 [평가 계약](../reference/training-evaluation.md)에 있다. 이번 smoke의 작은 산술 데이터는 연결 검증용이다. loss가 줄어야 PASS라는 조건은 없고, 일반 모델 품질 향상을 입증한 것은 아니다. 아래 1~5번은 처음 설치하거나 재검증이 필요할 때 사용하는 절차다.

## 1. 처음 설치할 때 적용과 Go/DB 검사

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail

  python -m unittest discover -s tests -p 'test_*evaluation*.py' -v
  docker build --target builder -t model-platform-check:training-evaluation .

  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL

  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:training-evaluation \
    go test -count=1 -v \
      ./internal/trainingevaluation ./internal/trainingcheckpoint \
      ./dispatcher/... ./training/... ./gateway/... \
      ./jobs/training/axolotl ./tests/creation

  python tests/check_build_layout.py
)
```

Go/DB 테스트는 실제 PostgreSQL의 고유 schema를 사용한다. Docker·Management·객체 저장소는 대역이다. RUN/PASS·FAIL·SKIP을 확인한다. 체크포인트 재개 시 평가 pin 복사·변경된 receipt 거절, 누락 평가의 완료/복구 차단, 복구 평가와 원래 이력 보존, 로컬 release 후 평가 조회를 포함한다.

## 2. Job과 서비스 이미지 교체

진행 중인 학습·취소가 끝난 뒤 실행한다. 먼저 새 학습 Job을 빌드하고 Training·Gateway·Dispatcher를 바꾼다. 호스트에 새 Python/MLflow 패키지를 설치할 필요는 없다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail

  docker buildx bake --builder default -f docker-bake.hcl --load training-job
  docker compose build training gateway dispatcher

  docker image inspect model-platform-training-axolotl:launch-v1 \
    --format '{{index .Config.Labels "model-platform.training-evaluation"}}' |
    grep -x '1'

  docker compose stop dispatcher
  docker compose up -d --no-build --no-deps --force-recreate training
  docker compose up -d --no-build --no-deps --force-recreate gateway
  docker compose up -d --no-build --no-deps --force-recreate dispatcher
  docker compose ps training gateway dispatcher
  docker compose logs --tail=40 training gateway dispatcher
)
```

Training은 evaluation_spec 열을 추가한다. 이전 요청·결과는 평가 미사용으로 읽는다. 코드 생성물은 Docker builder 안에서 만든다. Dispatcher의 이미지 검사가 실패하면 새 Job이 실행되지 않으므로 라벨이 1인지 먼저 확인한다.

## 3. 실제 GPU 평가와 비교

GPU 0에서 다른 학습/서빙이 끝난 뒤 실행한다. 모델·출력·checkpoint 사본을 저장할 디스크 공간이 필요하다. 이 smoke는 새 Dataset에 train/eval 버전을 만들고 **학습 Job 2개를 순서대로 실행**한다. 첫 실행은 1 step, 두 번째는 2 step과 checkpoint_steps=1이다. 각 실행의 마지막 모델로 평가 2개 레코드 전체를 채점한다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_evaluation_smoke.py \
  --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
  --gpu-id 0 \
  --timeout 600
```

API 기본 주소는 http://127.0.0.1:13000, MLflow는 http://127.0.0.1:5000이다. 필요한 경우 --api와 --mlflow로 지정한다. 두 주소 모두 이 명령을 실행하는 호스트에서 접근할 수 있어야 한다.

PASS 조건:

- 각 생성 요청과 재요청이 같은 TrainingRun/Operation을 반환한다.
- 학습·평가 pin, 전체 레코드 수·마지막 step·채점 토큰 수·loss/perplexity 계산이 맞는다.
- TrainingRun과 Operation 모두 succeeded이고 출력이 해당 실행의 ready adapter다.
- 두 번째 실행에서 checkpoint wrapper도 start=0/final=2/executed=2를 기록한다.
- 기존 MLflow run이 FINISHED이고 heldout 지표·step·평가 pin tag가 REST 결과와 같다.
- 비교 GET이 같은 평가 조건을 확인하고 요청 순서·loss 차이를 보존한다. 원래 학습 상세는 바뀌지 않는다.

보고서는 tests/results/evaluation/{UUID}.json에 저장한다. 마지막 [output_model_version_id]는 두 번째 실행의 새 adapter다. 데이터셋·모델·checkpoint·로그를 자동 삭제하지 않는다. 이번 기본 smoke는 실패 후 checkpoint 재개를 실행하는 것은 아니다. 평가를 함께 요청한 checkpoint 재개는 Go/DB와 Python wrapper 대역 검증 범위이며 실제 GPU 재개 검증은 별도로 기록해야 한다.

## 4. 중단·오류 확인과 같은 요청 재관찰

600초는 각 관찰 구간의 제한이며 Job을 취소하는 제한이 아니다. GPU 대기나 첫 모델 로딩이 길어지면 보고서를 유지하고 원인을 확인한다. 실패한 요청을 새 키로 자동 제출하지 않는다.

[record]의 실제 경로를 아래 변수에 넣는다.

```bash
cd ~/workspace/agent_project/model_platform
EVALUATION_REPORT='tests/results/evaluation/<record UUID>.json'
python tests/live_evaluation_smoke.py --resume "$EVALUATION_REPORT" --timeout 600
```

--resume은 보고서에 저장된 API·MLflow·모델·GPU·요청 키를 사용한다. timeout 후 계속 실행 중이던 작업은 같은 ID로 관찰한다. 이미 failed/cancelled인 요청은 그대로 실패로 보고한다. 코드를 수정한 뒤 새 GPU 학습을 의도하면 원인을 해소하고 3번을 새로 실행한다.

오류 조사에는 보고서의 run1.created 또는 run2.created를 사용한다. 다음 예는 두 번째 요청이 생성됐으면 그것을, 아니면 첫 번째 요청을 조회한다.

```bash
(
  set -euo pipefail
  EVALUATION_RUN_ID=$(
    jq -er '.run2.created.training_run_id // .run1.created.training_run_id' "$EVALUATION_REPORT"
  )
  EVALUATION_OPERATION_ID=$(
    jq -er '.run2.created.operation_id // .run1.created.operation_id' "$EVALUATION_REPORT"
  )
  curl -fsS "http://127.0.0.1:13000/api/operations/$EVALUATION_OPERATION_ID" |
    jq '{status,phase,error_code,error_message}'
  curl -fsS "http://127.0.0.1:13000/api/training-runs/$EVALUATION_RUN_ID/logs?tail_lines=120&max_bytes=65536" |
    jq '{source,availability,stdout,stderr,truncated}'
)
```

| 표시 | 의미와 확인 |
|---|---|
| EVALUATION_INPUT_INVALID | 저장된 평가 파일·실행 조건 확인 실패. Operation error_message 확인 |
| AXOLOTL_CONFIG_RENDER_FAILED | 원본 대화 중복·구조·pin 등 평가 준비 또는 기존 설정 변환 실패 |
| AXOLOTL_TRAIN_FAILED | 학습뿐 아니라 같은 Python 프로세스의 토큰화·평가·MLflow 오류도 포함. 로그 마지막 traceback 확인 |
| EVALUATION_RESULT_INVALID | Python 성공 종료 뒤 필요한 평가 결과가 없거나 pin·범위·수치가 불일치 |
| comparable=false | ready 여부·평가 버전·base·protocol/토큰 digest가 다른지 reason 확인 |

평가 구간은 Operation의 axolotl_training phase 안에서 실행하며 로그에 [model-platform-evaluation]과 완료 마커를 남긴다. 모델 참조 답변을 전부 출력하지 않는다. 원래 학습의 완료 receipt가 존재하고 업로드만 실패했다면 [출력 복구 절차](training-output.md)를 사용할 수 있다.

## 5. 새 adapter 서빙 연결 확인

성공 smoke의 두 번째 adapter로 이 검증을 완료했다. Deployment `d8be0fc3-6231-4a89-9360-78fc7404deaa`가 ready가 됐고 Operation `8cd5b0ac-4e04-4da1-8e4c-c56e0db53f6a`는 completed/succeeded였다. adapter 이름 `deployment-d8be0fc3-6231-4a89-9360-78fc7404deaa`로 Hello!를 응답했고 멱등 재요청·stopped까지 PASS했다. 현재 해당 추론 런타임은 중지됐으며 로그·캐시는 보존한다.

재검증이 필요할 때만 GPU 0에서 다른 학습·서빙이 실행 중이지 않은지 확인한 뒤 다음을 실행한다. 새 이미지 빌드는 필요 없다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_serving_smoke.py \
  --model-version-id 19d02f67-9568-4802-aa0a-1495f2e3b28b \
  --gpu-id 0
```

다른 평가 smoke를 실행했다면 마지막 [output_model_version_id] 또는 보고서 `.run2.run.output_model_version_id`의 값을 사용한다. 이번 성공으로 평가 경로의 새 산출물도 서빙에 연결됨을 확인했다. 준비 중 RUNTIME_CREATE_UNCONFIRMED가 나타났지만 이후 오류가 비워지고 ready로 진행했다. 이 출력만으로 그 중간 상태의 원인을 특정하지 않는다. Streamlit 관리/채팅도 구현·서버 검사를 완료했으며 현재 남은 범위는 [프로젝트 상태](../status.md)를 따른다. 자동으로 주입하는 장애 fixture는 이번 기본 평가 smoke에 없다.
