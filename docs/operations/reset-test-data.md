# 모델 플랫폼 테스트 데이터 초기화

이 절차는 의도적으로 폐기할 모델 플랫폼 테스트 데이터에만 사용한다. **prepare는 서비스를 중지하고, apply는 계획된 데이터를 삭제한다.** 프로젝트의 문서·결과 파일 정리에는 실행하지 않는다. 2026-09-10의 완료 기록은 [검증 요약](../testing/results-summary-20260916.md)에 있으며 당시 계획·보고서는 삭제됐다.

## 정리하는 범위

| 대상 | 동작 |
|---|---|
| 제어 DB 네 개 | 현재 public 테이블과, 존재하는 경우 model_management.public.operations를 DB별 트랜잭션으로 비움. DB·schema·role은 유지 |
| 모델·데이터셋·Job 로그 객체 | 설정된 세 버킷에서 DB의 리소스 ID와 연결된 객체·미완료 multipart 업로드 제거. 버킷 유지 |
| training-outputs·serving-cache | 해당 학습/배포 UUID의 파일을 물리 삭제. named volume 유지 |
| gpu-admission | 플랫폼 작업과 연결된 예약·취소 기록 삭제. 다른 GPU 서비스는 유지 |
| Job·서빙 컨테이너 | 이름·소유 label·Operation·노드가 일치하는 컨테이너만 중지·제거 |
| Kafka | 플랫폼 topic의 기존 끝 offset까지 레코드를 읽을 수 없게 정리하고 지정 consumer group을 끝 offset으로 이동. topic·브로커 유지 |
| MLflow | TrainingRun·Operation 두 ID의 tag가 일치하는 실행만 삭제 상태로 전환. 공유 DB·experiment·삭제 메타데이터 유지 |

MLflow의 `runs/delete`는 삭제 표시이며 물리적인 전체 DB 제거가 아니다. 다른 사용자 데이터가 있는 공용 MLflow에서 전체 gc를 실행하지 않는다. [MLflow REST API](https://mlflow.org/docs/latest/api_reference/rest-api.html#delete-run)

Kafka consumer가 남아 있으면 offset 초기화를 거절한다. 레코드 정리 뒤 시작/끝 offset과 group 결과를 확인한다. Kafka 디스크 파일의 실제 공간 회수는 브로커의 정리 시점에 따라 달라진다. [Kafka consumer offset 관리](https://kafka.apache.org/41/operations/basic-kafka-operations/)

소유권을 증명할 수 없는 객체·컨테이너·볼륨 내용, 예상하지 않은 DB 테이블이나 trigger, 원격 노드 설정, versioning 버킷이 있으면 중단한다. 일반적인 orphan 데이터 삭제나 공유 인프라 초기화 도구로 사용하지 않는다. 진행 중인 초기화의 준비 목록·결과 보고서는 복구를 위해 유지한다. 과거 완료 결과 파일의 정리는 별도 정책을 따른다.


## 1. 사전 확인

현재 데이터의 폐기 범위와 백업 여부를 먼저 정한다. 보존할 데이터는 [백업/복원](backup-restore.md)을 따른다. 기존 `(sh_test)` Python과 설치된 boto3를 사용하며 새 venv를 요구하지 않는다. 필요한 경우 현재 환경에 `python -m pip install boto3`로 설치한다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/test_reset_test_data.py -v
python -c 'import boto3; print(boto3.__version__)'
```

최근 모델 API 코드를 변경했다면 [모델 Go/DB 검사](../testing/README.md)도 완료한다. 아래 예시의 보고서 이름이 이미 존재하면 새 이름으로 바꿔 전체 단계에 일관되게 사용한다. 이전 실패 계획을 덮어쓰지 않는다.

`model_management.public.operations` 잔여 테이블은 정확한 DB/schema 조합에서만 선택적으로 허용한다. 계획에 존재 여부·행 수·fingerprint를 고정하며 준비 후 집합이 바뀌면 거절한다. 별도 Operation DB가 실행 소유권 기준이고 잔여 테이블 ID는 삭제 권한으로 사용하지 않는다. 테이블 DROP/CASCADE나 임의 추가 테이블 허용은 하지 않는다.

## 2. 쓰기 중지와 삭제 대상 준비

```bash
cd ~/workspace/agent_project/model_platform
python ops/reset_test_data.py prepare \
  --plan tests/results/operations/reset-plan.json
```

prepare는 플랫폼 9개 서비스를 중지하고, DB/label/이름이 일치하는 Job·서빙 런타임을 중지한다. 진행 중인 작업도 테스트 폐기 대상으로 종료하며 데이터 파일은 아직 삭제하지 않는다. 공용 PostgreSQL·MinIO·Kafka·MLflow와 다른 프로젝트 컨테이너는 실행 상태로 유지한다.

출력에서 네 DB의 테이블별 행 수, 버킷별 객체 수·용량, 세 볼륨 크기, Job 수·MLflow 실행 수를 확인할 수 있다. 실제 컨테이너 ID·구성 fingerprint·객체 식별자·파일 목록·DB 행 fingerprint·Kafka offset을 준비 파일에 기록한다. credential·모델 가중치는 포함하지 않는다. 두 개의 초기화 명령을 동시에 실행하지 못하게 잠금을 사용한다.

Kafka group이 아직 active라는 오류는 Dispatcher 종료 뒤 membership 만료가 반영되기 전일 수 있다. 다른 Dispatcher가 실행 중인지 확인하고 만료 뒤 같은 prepare 명령을 다시 실행한다. 이미 계획 파일이 생성됐다면 덮어쓰지 않는다. 생성 이후 서비스를 다시 켰거나 설정을 바꿨다면 기존 계획을 apply하지 말고 새 이름으로 준비한다.

## 3. 준비한 테스트 데이터 삭제

prepare의 대상이 현재 모델 플랫폼 테스트 리소스와 일치하면 실행한다. 과거 초기화의 승인을 현재 데이터 삭제에 재사용하지 않는다. 이번에 폐기할 테스트 데이터와 백업 필요 여부가 확정된 경우에만 적용한다.

```bash
cd ~/workspace/agent_project/model_platform
python ops/reset_test_data.py apply \
  --plan tests/results/operations/reset-plan.json \
  --report tests/results/operations/reset-apply.json
```

apply는 대상 변경과 실행 중 writer를 다시 검사한 뒤 Kafka → MLflow → 객체 → 런타임 → 세 볼륨 → 네 DB 순서로 처리한다. S3 일괄 삭제 응답의 `Errors`도 검사하므로 HTTP 요청 성공만으로 객체 삭제 성공을 판정하지 않는다. [S3 DeleteObjects 응답](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/delete_objects.html)

DB는 한 DB의 전체 대상 테이블을 하나의 TRUNCATE 트랜잭션으로 비운다. 네 DB와 S3를 하나의 원자적 트랜잭션으로 묶지는 않는다. 단계별 결과를 보고서에 기록하고 마지막 검사까지 끝나야 `passed=true`, `phase=complete`다.

**중간 실패 후에는 플랫폼을 켜지 않는다.** 실패 원인을 해결하고 같은 계획에 새 report 이름을 지정해 apply를 재실행한다. 이미 삭제된 객체/파일과 비워진 DB는 허용하지만 새로 추가되거나 바뀐 데이터를 삭제 범위에 넣지는 않는다. 부분 초기화 뒤에는 ID 연결이 사라질 수 있으므로 새 prepare로 대체하지 않는다. 서비스가 재기동되거나 소유권이 달라졌다면 원래 계획·실패 보고서로 상태를 확인한다.

apply 성공 뒤에도 플랫폼 서비스는 중지 상태다. 실패했을 때 무조건 start하는 trap은 넣지 않는다. prepare만 했고 apply를 한 번도 실행하지 않았다면 데이터를 보존한 상태로 서비스를 다시 시작할 수 있다.

## 4. 기동·빈 상태·공간과 재기동 확인

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  jq -e '.passed == true and .phase == "complete"' \
    tests/results/operations/reset-apply.json

  docker compose --profile playground start \
    operation management training deployment gateway playground reconciler gc dispatcher

  python ops/verify_reset.py \
    --plan tests/results/operations/reset-plan.json \
    --reset-report tests/results/operations/reset-apply.json \
    --report tests/results/operations/reset-started.json \
    --timeout 600

  docker compose --profile playground restart \
    playground gateway dispatcher reconciler gc deployment training management operation

  python ops/verify_reset.py \
    --plan tests/results/operations/reset-plan.json \
    --reset-report tests/results/operations/reset-apply.json \
    --report tests/results/operations/reset-restarted.json \
    --timeout 600
)
```

두 검증 사이에 모델·데이터셋·작업을 새로 추가하지 않는다. 브라우저를 새로고침하고 열려 있는 추가/학습/배포 팝업은 닫는다. 이전 URL이나 팝업에 남은 UUID는 초기화로 사라진 과거 ID다.

검증은 서비스 ready 대기를 최대 600초로 설정하고, 여섯 목록·DB 테이블이 비어 있는지, S3 객체·학습/서빙 파일·소유 Job이 없는지, GPU 예약이 비었는지, Kafka에 새/이전 이벤트가 없는지, 대상 MLflow 실행이 삭제 표시됐는지와 저장 공간을 확인한다. Playground 이외의 Docker healthcheck 미설정은 별도 probe 성공으로 표시하지 않는다.

| 결과 | 의미와 후속 행동 |
|---|---|
| reset_passed=true, capacity_passed=true, passed=true | 초기화·기동 및 공간 기준 통과 |
| reset_passed=true, capacity_passed=false | 초기화 완료. 공유 디스크의 용량 경고가 남음. 데이터 삭제를 반복하지 않고 남은 공간 원인 확인 |
| 명령 오류 또는 reset_passed=false | 보고서·Compose 로그로 미완료 항목 확인. 실제 확인 전 전체 완료 표시 금지 |

첫 검증이 공간 경고로 종료 코드 2를 반환하면 괄호 블록은 재기동 전에 멈춘다. `reset-started` 보고서에서 초기화 성공을 확인하고 공간 문제를 정리한 뒤 새 보고서 이름으로 검증·재기동 단계를 이어간다. 이력 보존이 필요 없는 빈 상태의 재기동 검사이며, 이전 GPU 학습·서빙 검증을 대체하거나 반복하지 않는다.
