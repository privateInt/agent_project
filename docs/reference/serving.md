# Deployment와 vLLM 서빙

기본 서빙은 단일 호스트·단일 GPU·Hugging Face LLM snapshot과 해당 snapshot에서 학습한 adapter이며, 명시적 OCR·임베딩 profile도 제공한다. 실제 서버 검증 완료 여부는 [현재 상태](../status.md), 실행 순서는 [서빙 테스트](../testing/serving.md)를 따른다.

2026-09-11: `model_type=ocr`인 Surya v2 snapshot용 `serving.profile=surya-ocr-v2`를 추가했다. 기존 LLM 기본값과 분리된 vLLM 이미지·vision 설정·모델 식별 gRPC를 사용한다. 2026-09-15에 실제 GPU ready와 PNG·스캔 PDF OCR까지 PASS했다. [검증 범위](../history/data-platform-202609.md#2026-09-15-02-ocr-verified)를 따른다. 아래 표는 기본 LLM profile의 계약이며 OCR의 별도 범위는 [Surya 계약](../data-platform/api-ocr.md), 실행은 [OCR 검증](../data-platform/testing-ocr.md)을 따른다.

## CPU demo의 의미

기존 `deployment.apply` Job은 실행 승인 후 약 2초 기다리고 Deployment를 ready로 기록하는 제어 경로 테스트다. 모델을 메모리나 GPU에 올리지 않고 추론 endpoint도 만들지 않는다. REST → 도메인 전달 → Operation/Outbox → Kafka → Dispatcher → Job → 완료 보고를 검증한다. 과거 Gemma QLoRA 1 step 검증은 별도로 실제 GPU 학습을 수행한 결과다.

기존 요청과 CPU smoke를 유지하기 위해 `serving`을 생략하면 demo로 처리한다. 실제 추론은 `serving.engine=vllm`을 명시한다.

## 생성 계약

`POST /api/deployments`, 선택적 `Idempotency-Key` 헤더:

```json
{
  "model_version_id": "cceae526-7b7c-47d4-870b-117372b51885",
  "resources": {"gpu_ids": [0]},
  "serving": {
    "engine": "vllm",
    "max_model_len": 2048,
    "gpu_memory_utilization": 0.5
  }
}
```

202 응답의 deployment_id·operation_id를 보관한다. 수락 직후 실행 완료를 의미하지 않는다. 같은 키·정규화 입력은 같은 ID를 반환하고 입력 충돌은 409다. 같은 키를 재전송해도 중지된 서버를 다시 시작하지 않는다. 다른 배포를 만들 때만 새 키를 사용한다. [생성 요청 계약](request-creation.md)의 도메인 트랜잭션과 재전달 규칙을 그대로 사용한다.

| 입력 | 허용 범위 |
|---|---|
| 모델 버전 | ready Hugging Face LLM snapshot 또는 [검증된 학습 adapter](adapter-serving.md) |
| GPU | `gpu_ids`에 정확히 한 개 지정. gpu_count만으로 자동 선택하지 않음 |
| node_name | 생략 또는 Deployment 서비스의 설정 노드와 일치 |
| max_model_len | 기본 2048, 128..32768. 모델 자체 한도를 초과하면 vLLM 기동 실패 가능 |
| gpu_memory_utilization | 기본 0.5, 0.1..0.95. GPU 예약·충돌 방지 기능은 아님 |

adapter 입력은 source_model_version_id로 정확한 base snapshot을 찾고 원본 연결·가중치·tokenizer를 검사한다. [adapter 계약](adapter-serving.md)에 지원 옵션과 서빙용 사본 규칙을 정리했다. `--trust-remote-code`를 켜지 않으므로 사용자 코드를 요구하는 모델은 지원하지 않는다.

Gateway는 실제 서빙 생성에 별도 `CreateServingDeployment` gRPC를 사용한다. 구버전 Deployment가 새 필드를 무시하고 demo 성공을 반환하는 상황을 방지하며, 새 RPC가 없으면 오류를 반환한다. 기존 demo의 v1 fingerprint와 RPC는 유지한다.

## 실행과 데이터 소유권

| 구성 | 역할과 수명 |
|---|---|
| `deployment.serve` Job | CPU에서 BeginExecution 승인 → StartServing → 준비 결과 관찰·Operation 보고 후 종료 |
| Deployment controller | 승인된 의도를 DB에 보관하고 snapshot 준비·Docker 생성·상태 조회·중지 담당 |
| `model-serving-<deployment_id>` | 실제 GPU 추론 서버. 준비 Job이 끝나도 계속 실행 |
| `model-serving-cache` 볼륨 | 배포별 snapshot 사본. Deployment가 쓰고 vLLM은 읽기 전용으로 사용 |

StartServing은 Operation ID·현재 dispatch token·execution instance·실행 승인 기록을 확인한다. 같은 실행 주체의 응답 유실은 재전송으로 회복한다. 승인이 DB에 저장되기 전에는 controller가 모델 다운로드나 GPU 컨테이너 생성을 하지 않는다.

요청 시 source URI·manifest SHA-256·크기를 고정한다. MinIO의 `_manifest.json`과 파일별 SHA-256·크기를 검사하고, 경로 이탈·중복·symlink·예상 밖 캐시 파일을 거절한다. 임시 디렉터리 준비를 끝낸 뒤 최종 디렉터리로 바꾼다. vLLM은 오프라인 모델 경로를 사용하며 HF token·MinIO 자격 증명·Docker 소켓을 받지 않는다.

Deployment 서비스가 Docker CLI·소켓을 사용한다. vLLM 컨테이너는 owner=`deployment-serving`, 시작 Job은 owner=`operation-dispatcher`다. GC는 종료된 시작 Job의 로그만 기존 정책으로 수집하며 vLLM 컨테이너와 모델 캐시는 수집하지 않는다. GC 모드는 이 변경으로 바뀌지 않는다.

## 준비 결과와 현재 상태

`GET /api/deployments/:id`:

| 필드 | 의미 |
|---|---|
| status | created / applying / ready / failed / unhealthy / stopping / stopped |
| apply_status | 최초 서빙 준비의 결과. ready 확정 후 중지·런타임 장애가 나도 ready 보존 |
| desired_state | running 또는 stopped |
| runtime_state | queued / preparing / starting / ready / unhealthy / exited / unknown / failed / stopping / stopped |
| endpoint | 정상 상태일 때 호스트 loopback의 OpenAI API 주소. 포트는 Docker가 할당 |
| internal_endpoint | 정상 상태일 때 공통 Docker 네트워크에서 사용할 API 주소 |
| served_model_name | 추론 요청의 model 값. `deployment-<deployment_id>` |
| artifact_kind / base_model_version_id / adapter_model_version_id | snapshot 또는 adapter와 고정된 원본 버전 연결 |
| base_served_model_name | base 모델 이름. adapter 요청에서는 served_model_name과 구분 |
| runtime_container_id / runtime_image_id | 확인된 Docker 컨테이너와 실제 이미지 ID |
| runtime_error_code / runtime_error_message | 현재 준비·관찰·중지 오류 |

실행 중이라는 Docker 응답만으로 ready를 기록하지 않는다. `/health` 200과 `/v1/models`의 기대 모델명을 모두 확인한다. adapter 요청은 모델 목록의 adapter parent·root까지 검사한다. 준비 제한 시간은 base/adapter 다운로드·검사 최대 30분, DB에 기록한 첫 start 시각부터 readiness 15분이다. 초기 환경에서는 다운로드·모델 로드 시간이 길 수 있다.

Reconciler는 `deployment.serve`의 apply_status와 원래 Operation ID를 사용한다. 이미 준비를 성공한 서버를 중지해도 과거 Operation succeeded는 유지된다. ready 이후 health 실패는 endpoint를 숨기고 unhealthy로 표시하며 다시 정상 응답을 받으면 ready가 된다. 이미 종료되거나 사라진 런타임은 자동 재시작·재생성하지 않는다.

Docker create/start 응답 유실은 결정된 이름·소유권·이미지·컨테이너 ID를 재관찰한다. 다른 소유자나 교체된 ID는 자동 인수·중지하지 않는다. 동일 배포의 controller 작업은 PostgreSQL session advisory lock으로 직렬화한다. controller의 일반 관찰은 순차 처리이므로 다른 모델의 긴 다운로드가 health 갱신을 늦출 수 있다. Stop API는 별도 처리와 다운로드 취소 경로를 가진다.

## 추론 경로와 임베딩 확인

현재 LiteLLM은 미통합이다. 채팅은 Model Gateway가 배포 ID를 조회해 전달하고, Data OCR은 Model의 ResolveDeployment 응답을 고정한 뒤 Job이 runtime에 직접 요청한다. [현재 경로·LiteLLM 도입 검토](litellm-integration-review.md)를 따른다. 사용자 기존 snowflake 배포의 직접 `/v1/embeddings` 응답은 성공했으나 공식 Model 임베딩 API·profile/공간 identity·용도 표시 소스는 K03에서 추가했다. 서버 빌드·적용·실호출 인수는 남아 있다. [확인 결과](../testing/embedding.md#runtime-check).

## 추론과 중지

호스트에서 `endpoint + /chat/completions`로 OpenAI 호환 JSON을 보낸다. 같은 Docker 네트워크의 클라이언트는 internal_endpoint를 사용한다. 브라우저/외부 클라이언트는 Gateway의 `POST /api/deployments/:id/chat`으로 텍스트 채팅을 요청할 수 있다. Gateway가 ready 배포와 모델 식별자를 검증해 내부 런타임으로 전달한다. LiteLLM·범용 추론 proxy는 통합하지 않았다. vLLM 자체 호스트 포트는 127.0.0.1 바인딩이며 직접 접근에는 별도 연결이 필요하다. 내부 네트워크의 클라이언트 인증은 아직 없다.

`POST /api/deployments/:id/stop`은 desired_state=stopped를 저장하고 202와 현재 상태를 반환한다. 이후 GET의 status=stopped를 확인해야 중지가 확정된다. 중복 요청은 안전하며 새 Operation을 만들지 않는다. 다운로드 중이면 취소하고, 실행 중이면 해당 소유 컨테이너 ID에 graceful stop을 요청한다. ready 이전 중지는 apply_status=failed로 종료한다.

Stop만 수행하면 중지된 컨테이너·로그·캐시는 진단용으로 남는다. 이후 명시적 [배포 삭제](model-management-update.md)를 수행하면 해당 컨테이너와 전용 cache를 제거하며, 이 기능은 fetcher 후속 수정 뒤 사용자 성공 확인을 받았다. 한 번도 시작하지 않은 created 컨테이너는 stopped 소유권을 확인하고 force 없이 제거해 GPU 예약을 해제한다. readiness timeout도 자동 삭제나 강제 종료를 하지 않으므로 Stop을 호출한다. [GPU smoke](../testing/serving.md)는 실패 시에도 중지를 시도한다. Compose의 stop/down은 동적으로 만든 서빙 컨테이너를 멈추지 않으므로 **플랫폼을 내리기 전에 배포 Stop API를 호출하고 stopped를 확인한다.**

## 런타임 기준과 제약

기본 이미지는 `vllm/vllm-openai:v0.15.0`으로 고정했다. 최신 버전이라는 의미는 아니며, 준비된 기준 버전이다. 생성 전에 명시적으로 pull하고 controller는 로컬 이미지의 sha256 ID를 고정해 실행한다. latest·태그 없는 참조는 거절한다. 실제 사용자 드라이버·GPU에서의 로드와 추론은 GPU smoke로 검증한다.

공식 vLLM 지원 목록에는 Gemma3ForCausalLM과 google/gemma-3-1b-it가 있다. Docker 런타임과 OpenAI 호환 서빙 옵션은 해당 버전 문서를 기준으로 한다. [지원 모델](https://docs.vllm.ai/en/v0.15.0/models/supported_models/), [Docker 실행](https://docs.vllm.ai/en/v0.15.0/deployment/docker/), [OpenAI 호환 API](https://docs.vllm.ai/en/v0.15.0/serving/openai_compatible_server/)

서빙은 단일 GPU를 유지한다. Training·Serving은 같은 [로컬 GPU 예약 장부](multi-gpu.md)를 사용한다. 점유 충돌에 따른 waiting_resources/GPU_BUSY는 학습 Job의 대기 정책이며, 서빙은 아래 공유 로딩 정책을 따른다. 원격 노드·호스트 직접 CUDA 프로세스의 스케줄링은 없다. 배포별 모델 사본·종료 컨테이너·비정상 종료로 남은 임시 캐시에 자동 정리·용량 제한은 아직 없다. 실행 중인 배포의 cache 볼륨·네트워크·노드 설정을 변경하지 않는다. 새 버전 이미지로 교체하려면 새 배포를 생성·검증한 뒤 이전 배포를 중지한다.

## 같은 GPU의 여러 서빙 모델

서빙 컨테이너 create/start는 다른 GPU 사용자가 있어도 로딩을 시도한다. 기존 GPU 예약 장부의 소유권·중복 생성·취소 방어는 유지하며, 학습 Job의 단독 GPU 사용 대기는 유지한다. 이는 GPU 메모리를 보장하거나 기존 작업의 메모리를 회수하는 기능이 아니다. `gpu_memory_utilization`은 각 vLLM 프로세스의 설정으로 전달하며 기존 배포 설정을 자동 변경하지 않는다.

로딩 중 종료된 프로세스의 Docker 상태와 제한된 로그를 확인하여 CUDA OOM·vLLM 가용 메모리 부족을 `GPU_OUT_OF_MEMORY`로 구분한다. 호스트 OOMKilled는 `RUNTIME_HOST_OOM`이며 다른 종료는 `RUNTIME_NOT_RUNNING`이다. 로그 원문을 공개 오류에 복사하지 않는다. 실패한 배포를 자동 재생성·재시작하지 않고 기존 배포는 유지한다. UI는 메모리 비율 조정 또는 다른 배포 중지 후 새 배포를 안내한다. 이전 `waiting_resources` 배포는 새 Deployment 컨트롤러의 다음 처리 주기에서 로딩을 시도한다.

서버의 실제 동시 로딩과 GPU OOM은 아직 검증하지 않았다. 로컬에서는 admission·오류 분류·DB 기반 실패 상태 테스트 코드를 추가했으며, DB 검사는 적용 전 서버 검사에서 통과해야 한다.

## 중지된 배포 삭제

중지와 삭제는 별도 요청이다. `DELETE /api/deployments/:id`는 중지된 배포의 검증된 컨테이너·전용 cache를 정리하고 `deleted_at`을 남긴다. 등록 모델은 보존한다. [상세 계약](model-management-update.md)을 따른다.

## K03 임베딩 profile

`snowflake-ko-v1`과 해당 snowflake snapshot의 신규 배포를 지원하는 소스를 추가했다. 기존 blank-profile 배포는 저장 계획을 수정하지 않고 [임베딩 API](embedding.md)에서 artifact·tokenizer·실행 모델을 검증해 재사용한다. 해당 원본의 legacy 등록도 chat API에서 거절한다. 기존 snapshot/adapter/OCR 및 삭제 fetcher 동작을 보존한다. 서버 적용·신규 profile 실배포 검증 여부는 [K03 검사](../testing/embedding.md)를 따른다.
