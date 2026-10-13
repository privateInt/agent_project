# 학습 checkpoint 보존·재개

작성일: 2026-09-10. Go 100개·하위 사례 116개와 실제 PostgreSQL·Compose/Bake·이미지 전환, 단일 GPU 실패/취소 후 재개·새 adapter 등록, 취소 재개 adapter의 vLLM 추론·중지를 검증했다. GPU 검증은 Gemma 3 1B·synthetic 데이터·총 4 step 중 2 step 재개 범위다. [검증 기록과 재검증 절차](../testing/training-checkpoints.md)를 참고한다. 기존 완료 출력의 [업로드 전용 복구](training-output.md)와 달리, 이 기능은 새 학습 Job을 생성해 남은 optimizer step을 수행한다.

## 지원 범위와 API

| 경로 | 요청·응답 |
|---|---|
| POST /api/training-runs | 기존 training에 `checkpoint_steps` 추가. 0/생략은 기존 동작. 양수는 저장 간격이며 max_steps 이하여야 한다. |
| GET /api/training-runs/{id}/checkpoints | 완료된 checkpoint 목록. step, max_steps, checkpoint_sha256, size_bytes, file_count, resumable을 반환한다. 본문·쿼리 없음. |
| POST /api/training-runs/{id}/resume | `Idempotency-Key` 필수. 본문은 `{"checkpoint_step":2}`만 허용한다. 새 training_run_id·operation_id를 202로 반환한다. |
| GET /api/training-runs/{id}, 학습 목록 | 새 학습의 `resume`에 source_training_run_id·checkpoint_step·checkpoint_sha256을 반환한다. 원래 학습은 null이다. |

첫 지원은 로컬 단일 GPU QLoRA, synthetic 또는 등록된 dataset version, sample_packing=false다. node_name을 지정하면 로컬 노드여야 한다. DDP·원격 노드·FSDP·DeepSpeed·외부 checkpoint 업로드·학습 설정 변경·총 step 연장은 지원하지 않는다. 기존 checkpoint 미사용 학습·DDP 경로는 유지한다.

재개는 원래 TrainingRun과 Operation이 모두 failed 또는 모두 cancelled이고, 실행된 원래 소유 Job이 exited 또는 이미 제거됐을 때 허용한다. 저장 완료 step이 max_steps보다 작아야 한다. checkpoint 생성 전 실패·미시작 취소·저장 중 깨진 출력은 재개할 수 없다. succeeded 학습에서 추가 학습을 시작하는 기능은 별도 범위다.

새 학습은 원본 snapshot ID, dataset pin, GPU 요청, LR·batch·LoRA 등 정규화된 명세와 **총 목표 max_steps**를 그대로 복사한다. 예를 들어 목표 4에서 2 step checkpoint를 재개하면 남은 2 step을 실행한다. 원래 TrainingRun·Operation·MLflow 종료 상태는 유지하고 새 MLflow run을 연결한다.

같은 재개 키는 같은 새 학습·Operation을 반환한다. 같은 키에 다른 step을 지정하면 충돌이다. 확정된 재개 응답은 checkpoint가 이후 없어지거나 서비스가 재생성돼도 재현한다. 다른 키는 별도 재개 학습을 만들 수 있으므로 응답 유실·시간 초과에는 키와 보고서를 재사용한다.

## 파일 저장과 검증

새 학습 Job은 기존 `model-training-outputs` 볼륨에 결과와 checkpoint를 저장한다. checkpoint는 실행별 디렉터리의 `checkpoints/checkpoint-{step}/data`에 두며, 완료 출력 `output`과 분리한다. REST에는 파일 경로나 실행 승인 토큰을 노출하지 않는다.

Trainer의 on_save 이후 adapter 가중치·설정, optimizer.pt, scheduler.pt, rng_state.pth, trainer_state.json, training_args.bin을 검사한다. 임시 사본의 SHA-256·크기가 원본과 같은지 확인하고 파일·디렉터리를 fsync한 뒤 완료 디렉터리를 원자적으로 공개한다. 저장 실패 또는 강제 종료로 남은 임시 디렉터리는 목록에 포함하지 않는다.

최근 **2개 완료 사본**을 유지한다. 새 저장을 확정한 뒤 오래된 사본을 정리하므로 저장 도중 실패해도 이전 완료 사본은 남는다. Trainer 자체의 작업용 checkpoint도 최대 2개이므로 학습 중에는 보존용 2개 외 작업용·저장 중 사본이 추가로 필요하다. 성공 후에는 작업용 checkpoint 디렉터리를 최종 adapter 업로드에서 제외한다. 실패·취소된 실행의 작업용·미완료 파일은 자동 정리하지 않는다.

API의 목록·재개 접수는 완료 receipt와 원본 실행 식별자를 검사한다. 파일 전체 hash와 실제 모델·데이터·엔진 호환성은 새 Job에서 학습 전에 검증한다. 따라서 `resumable=true`는 실행 중 GPU가 비어 있거나 파일 전체 readback까지 완료됐다는 뜻은 아니다.

실행 시에는 선택한 checkpoint를 새 임시 작업 디렉터리로 복사하고 전체 바이트를 다시 검사한다. 원본 checkpoint는 수정하지 않는다. 실제 base snapshot·데이터 파일, 정규화된 학습 명세, axolotl/transformers/torch/peft/bitsandbytes/accelerate 버전과 변환·복원 코드 hash가 동일해야 한다. 코드나 엔진을 변경한 뒤 이전 checkpoint를 강제로 허용하는 옵션은 제공하지 않는다.

새 학습 완료에는 `.result.checkpoint`의 시작 step·최종 step·실제 수행 step과 모델/optimizer/scheduler/RNG 복원 증거가 필요하다. Trainer가 지정 경로를 전달받았는지, 복원 메서드가 실행됐는지, optimizer 상태와 scheduler 진행 step이 있는지 검사한다. 0부터 재학습해 성공했거나 복원 일부가 빠졌으면 완료로 인정하지 않는다. GPU 부동소수점 연산까지 중단 없는 실행과 bitwise 동일하다고 보장하지는 않는다.

`output/release`는 완료된 최종 output 사본만 정리한다. checkpoint 사본은 삭제하지 않는다. checkpoint의 원격 복제·노드 이동·볼륨 손실 복원·삭제/TTL API는 이번 범위에 없다. 완료 출력의 release 실환경 검증도 직전 단계의 미검증 항목으로 유지한다.

## 엔진 근거

Axolotl은 `resume_from_checkpoint`, `save_steps`, `save_total_limit`, `save_only_model`을 제공한다. optimizer 저장을 생략하면 checkpoint 재개를 할 수 없으므로 이 경로는 save_only_model=false로 고정한다. [Axolotl 설정 문서](https://docs.axolotl.ai/docs/config-reference.html)

Trainer callback으로 학습 시작·step 종료·저장 완료를 관찰하고 재개 진행 증거를 기록한다. [Transformers Trainer](https://huggingface.co/docs/transformers/main/en/main_classes/trainer), [Callbacks](https://huggingface.co/docs/transformers/main/en/main_classes/callback)

위 문서는 확인 시점의 공개 문서다. 사용 중인 Axolotl 0.18.0 기반 이미지에서 모델·optimizer·scheduler·RNG 복원과 남은 step 수행을 실제 GPU smoke로 확인했다. 다른 엔진 버전·모델의 호환성까지 검증한 것은 아니다. 런타임 버전 변경 시 복원 검사나 입력 검사를 완화하지 않고 실제 차이를 확인한다.
