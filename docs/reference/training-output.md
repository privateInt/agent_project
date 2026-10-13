# 완료 출력 보존과 업로드 재시도

학습 계산이 성공한 뒤 저장소 부족·통신 오류로 업로드가 실패하면, 같은 GPU 학습을 다시 실행하지 않고 완료된 파일을 업로드한다. 적용 이후 생성하는 로컬 Docker 학습이 대상이다. Kubernetes와 실제 원격 노드 전환은 보류한 상태로 유지한다.

## 보존 범위

`model-training-outputs` named volume을 Training 서비스와 로컬 학습 Job이 공유한다. 경로는 `/training-outputs/{training_run_id}/{dispatch_token}/output`이며 외부 요청으로 지정할 수 없다. base 다운로드와 전처리는 임시 폴더를 사용하므로 이 볼륨에 복제하지 않는다. 완료 출력의 checkpoint·embedding 등 manifest에 들어간 파일은 모두 보존한다.

Job은 실행 승인 후 output 폴더의 잠금을 잡고, 학습 종료 코드 0·필수 adapter 파일·DDP 완료 증거를 확인한 뒤 manifest와 결과 정보를 `completed.json`에 기록한다. 파일과 디렉터리를 sync하고 receipt를 atomic rename한다. receipt에는 실행 승인 식별자가 있으므로 REST 응답이나 일반 로그로 출력하지 않는다.

완료 receipt가 없는 파일은 학습 성공의 증거로 쓰지 않는다. 컨테이너 강제 종료 시 남을 수 있는 미완료 출력은 checkpoint 자동 재개의 대상이 아니며, 운영자가 상태와 필요성을 확인해 별도 정리한다. 과거 버전에서 임시 폴더가 이미 지워진 실패 학습은 이 패치로 복원할 수 없다.

## REST API

| 메서드·경로 | 동작 |
|---|---|
| `GET /api/training-runs/{id}/output` | 보존 상태, manifest 요약, 가장 최근 복구 상태 |
| `POST /api/training-runs/{id}/output/retry` | 업로드 전용 복구 요청. `Idempotency-Key` 필수. body/query 없음 |
| `POST /api/training-runs/{id}/output/release` | 원격 파일 전체 검증 후 해당 로컬 output 폴더 삭제. body/query 없음 |

복구 수락은 HTTP 202, terminal 상태의 동일 키 재요청은 HTTP 200이다. 같은 학습에 진행 중인 다른 복구가 있거나 Job 종료가 확인되지 않으면 HTTP 409다. 실패한 요청을 같은 키로 반복해도 새 작업을 만들지 않는다. 원인을 해결한 뒤 새 키로 요청한다. 키는 DB에 SHA-256으로 저장한다.

GET은 가장 최근 시도를, retry의 동일 키 재요청은 그 키에 해당하는 시도를 반환한다. 오래된 실패 키를 다시 보내면 최신 시도가 성공했어도 오래된 실패 응답을 받는다. 반환 필드는 `training_run_id`, `operation_id`, `availability`, `manifest_sha256`, `file_count`, `size_bytes`, `recovery_id`, `recovery_status`, `error_code`, `output_model_version_id`, `requested_at`, `updated_at`이다. `operation_id`는 원래 학습 Operation이다.

`availability`는 `not_retained`·`unsealed`·`invalid`·`retained`·`released`다. `retained`는 유효한 receipt가 있다는 의미이며 GET마다 대용량 가중치 전체를 해싱하지 않는다. 실제 복구는 모든 로컬 파일을 다시 해싱한다. `file_count`·`size_bytes`는 완료 시점 manifest의 값이며 manifest 자체를 제외한다.

## 실행 기록과 복구 기록

| 항목 | 복구 성공 후 |
|---|---|
| 원래 TrainingRun `status`·`result_json` | 기존 실패 또는 성공 그대로 |
| 원래 Operation·실행 세대·시작 시각 | 그대로 유지, 새 Operation 없음 |
| MLflow run | 기존 ID·종료 상태 그대로 유지 |
| TrainingRun `output_model_version_id` | 기존 완료 요청이 저장한 값 그대로 |
| TrainingRun `output_recovery_status` | 가장 최근 복구 상태 |
| TrainingRun `recovered_model_version_id` | 최근 복구가 성공한 경우 그 ready adapter ID |
| Management model version | 기존 run·manifest 예약을 사용해 ready 확정 |

따라서 원래 학습이 `failed`이면서 복구가 `succeeded`인 응답은 정상이다. 이 경우 `recovered_model_version_id`로 서빙한다. 이후 또 다른 복구 시도가 실패하면 최근 상태는 failed가 되며, 과거 복구 이력·이미 등록된 ready 모델은 지우지 않는다.

## 복구와 장애 처리

복구 worker는 DB의 queued/running 요청을 읽고 파일 잠금을 잡는다. 원래 TrainingRun과 Operation이 terminal이고 취소 요청이 없으며, 소유 Job이 exited 또는 제거된 상태인지 확인한다. Operation·실행 token·instance·노드·base·MLflow ID·데이터셋 pin·DDP 증거와 완료 receipt를 비교한다. 원래 Job이 멈췄다는 확인 없이 업로드를 시작하지 않는다.

공통 publisher는 같은 TrainingRun의 같은 manifest만 예약한다. 기존 원격 객체는 내용이 같을 때 재사용하고, 다른 내용은 덮어쓰지 않는다. manifest를 마지막에 올리고 원격 readback을 수행한다. Management도 다시 검증한 뒤 ready로 등록한다. ready 응답 유실 뒤에도 같은 버전을 조회·검증한다.

`training_output_recoveries` 테이블은 요청별 상태·결과를 보관한다. 원격 등록 후 DB commit이 실패하면 running 상태를 이어 처리한다. 파일 잠금은 여러 worker와 정리를 직렬화한다. 복구당 처리 제한은 20분이며, 알려진 timeout은 실패로 기록하여 새 키로만 재시도한다. CLI 기본 600초는 관찰 시간으로, 실행 자체를 취소하는 시간이 아니다. 서비스 중단이나 DB 응답 유실로 결과가 미확정이면 같은 요청을 이어 처리한다.

| 오류 코드 | 확인할 사항 |
|---|---|
| `SAVED_OUTPUT_UNAVAILABLE` | 완료 출력 볼륨·경로 보존 여부 |
| `SAVED_OUTPUT_INVALID` | receipt와 원래 실행·입력의 일치 여부 |
| `SAVED_OUTPUT_CHANGED` | 완료 뒤 파일 삭제·변경·손상 여부. 자동 덮어쓰기 없음 |
| `OUTPUT_PRODUCER_NOT_STOPPED` | 원래 Job의 종료 및 취소 여부 |
| `OUTPUT_STORE_UNAVAILABLE` | MinIO 연결·버킷·설정 |
| `OUTPUT_PUBLICATION_FAILED` | 저장 공간, 업로드/readback, Management 등록 오류 |
| `OUTPUT_PUBLICATION_TIMEOUT` | 처리 제한 초과. 기존 원격 객체는 보존 |

원래 실행이 계속 running인 경우, receipt만 보고 terminal 성공/실패를 만들어내지 않는다. 현재 단계는 실행 상태를 임의 변경하는 수동 복구 API가 아니다.

## 로컬 정리와 운영

release 요청은 완료 receipt와 ready 모델의 lineage·URI·manifest, 원격 파일 전체를 검증한다. 진행 중인 복구 또는 미확인 원격 데이터가 있으면 로컬 파일을 남긴다. 삭제는 해당 `output` 폴더만 대상으로 하며 receipt·복구 DB 이력·MinIO 모델·Job 로그를 유지한다. 응답 유실 시 같은 release 요청을 반복할 수 있다. 완료 후 `availability=released`와 완료 시점 파일 수/크기를 반환한다.

기존 컨테이너/로그 GC는 이 볼륨을 삭제하지 않는다. 자동 TTL도 없다. 영속 보존으로 디스크 사용량이 늘어나므로 필요 없는 완료 사본은 검증 후 release한다. 검증되지 않은 결과의 일괄 삭제나 `docker compose down -v`를 정리 방법으로 사용하지 않는다. 백업에는 기존 DB·MinIO와 함께 이 볼륨의 아직 게시되지 않은 완료 출력을 포함한다. 한 호스트의 컨테이너 제거·서비스 재시작에 대한 보존이며 디스크 자체의 장애를 복제하는 기능은 아니다.

[검증 절차](../testing/training-output.md), [개발 이력](../history/model-platform-202609.md#2026-09-10-02-training-output)
