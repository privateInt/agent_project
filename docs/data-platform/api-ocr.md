# Surya OCR 연결 계약

2026-09-15. 서버 Go/PostgreSQL 검사와 수정 후 HF import·Surya GPU 배포·PNG/스캔 PDF 실제 OCR을 모두 확인했다. [검증 이력](../history/data-platform-202609.md#2026-09-15-02-ocr-verified)의 범위로 연결 기능을 완료하며 대표 문서 품질은 별도로 확인한다.

## 배포와 요청

Model Platform에 `model_type=ocr`로 등록한 `datalab-to/surya-ocr-2`의 ready snapshot을 배포한다. 기존 배포 승인·GPU 예약·controller·중지 경로를 사용한다.

```json
{
  "model_version_id": "<등록한 Surya model_version_id>",
  "resources": {"gpu_ids": [0]},
  "serving": {
    "engine": "vllm",
    "profile": "surya-ocr-v2",
    "max_model_len": 18000,
    "gpu_memory_utilization": 0.5
  }
}
```

Model Gateway의 `POST /api/deployments`로 제출한다. 선택적 `Idempotency-Key`를 지원하며 검증 도구는 항상 사용한다. Surya profile은 `CreateOCRDeployment` gRPC로 전달한다. 구버전 서비스가 새 필드를 무시하고 다른 실행을 생성하지 않도록 새 RPC가 없으면 오류를 반환한다. DB 내부 engine은 `vllm-surya`, 공개 engine은 `vllm`이다. 기본 LLM profile의 기존 요청 직렬화·멱등 fingerprint는 유지한다.

Surya profile은 지정 저장소의 OCR snapshot만 받는다. manifest와 파일 해시를 검증하고 `config.json`의 `qwen3_5` / `Qwen3_5ForConditionalGeneration`, tokenizer·vision processor·chat template 파일을 확인한다. adapter나 다른 모델 종류는 거절한다. 모델 파일을 변환하거나 remote code를 활성화하지 않는다.

배포가 ready가 된 뒤 Data Gateway의 `POST /api/refinement-runs`에 `Idempotency-Key`와 다음 JSON을 보낸다.

```json
{
  "name": "스캔 문서 OCR 정제",
  "document_id": "<등록한 원본 document_id>",
  "ocr_deployment_id": "<ready Surya deployment_id>"
}
```

OCR 선택은 PNG·JPEG·PDF에만 허용한다. 필드를 생략하면 기존 CPU 정제를 수행한다. DOCX·XLSX·HTML의 포함 이미지 OCR은 아직 지원하지 않으며 해당 입력에 OCR 배포를 지정하면 요청을 거절한다. 기존 CPU 처리에서는 그 이미지의 `needs_ocr` 표시를 유지한다. `rules_version` 기본값은 `basic-cleaning-v1`이다.

같은 키의 이름·원본·규칙·OCR 배포 ID가 같으면 같은 실행을 반환한다. 하나라도 바꾸면 409다. 새 실행에만 Collection과 Model gRPC로 입력을 확인하고, 수락된 요청 재조회에는 원격 모델의 현재 상태가 필요하지 않다. 종료한 실행을 다시 처리하려면 새 키를 사용한다.

## 모델 식별과 서비스 경계

Refinement와 실행 Job은 `model.ocr.v1.OCRDeploymentService/ResolveDeployment`로 배포를 조회한다. Model/Data에 동일한 protobuf 계약을 두며 상대 DB를 직접 읽지 않는다. 모델 실행·중지·GPU 소유권은 Model Platform에 있다. Data Job은 GPU 없는 CPU 프로세스로 이미 배포된 모델에 요청한다.

요청 수락 시 다음 식별자를 고정하고 정제 실행의 `ocr` 필드와 결과 JSON의 `ocr.identity`에 보존한다.

| 필드 | 의미 |
|---|---|
| `deployment_id`, `model_version_id` | 선택한 배포와 불변 모델 버전 |
| `runtime_image_id`, `runtime_container_id` | 실제 vLLM Docker image ID와 컨테이너 ID |
| `source_manifest_sha256` | 배포가 검증한 모델 manifest 해시 |
| `internal_endpoint`, `served_model_name` | 해당 배포의 내부 endpoint와 모델 alias |
| `profile` | `surya-ocr-v2` |

조회는 running/ready 상태와 60초 이내 runtime 관찰을 요구한다. Job도 시작 후 동일 식별자가 여전히 ready인지 재확인한다. 이때 배포가 달라졌거나 조회할 수 없으면 `REFINEMENT_OCR_UNAVAILABLE`로 실패하며 다른 모델을 선택하지 않는다. 요청자가 임의 URL·모델명·image ID를 전달하는 API는 없다.

Model/Data는 같은 Docker 네트워크를 사용한다. 기본 모델 조회 주소는 `model-deployment:9000`이며 Data의 `DATA_MODEL_DEPLOYMENT_GRPC_ADDRESS`로 설정한다. 실제 이미지 추론은 검증된 `http://model-serving-<deployment_id>:8000/v1/chat/completions`에 보낸다. 제어·도메인 조회는 gRPC이며 추론 본문은 vLLM의 OpenAI 호환 HTTP 계약이다. 기존 텍스트 채팅 Gateway를 이미지 프록시로 사용하지 않는다.

## Surya 호환 기준

검증 도구는 HF revision `3b3d4cdf88d6928b0acdc75181b13206ea67c4a3`을 고정한다. Surya 전용 기본 이미지는 `vllm/vllm-openai:v0.20.1`이며 Model의 `MODEL_SURYA_VLLM_IMAGE`로 설정한다. 실제 결과에는 태그 대신 런타임 image ID가 남는다. 기본 LLM 서빙 이미지 설정은 별도로 유지한다.

공식 Surya v2는 페이지 이미지를 OpenAI 호환 요청으로 보내 HTML을 받고, SDK에서 이를 블록으로 변환한다. 이 구현은 동일 이미지 요청·프롬프트 형식을 사용하는 작은 CPU client와 HTML 변환기를 둔다. CPU Job에 Surya SDK·Torch를 설치하거나 SDK의 모델 자동 실행을 호출하지 않는다. 응답의 모델 alias·단일 assistant 응답·완전 종료·크기·JSON 숫자와 키 중복을 확인한다.

확인한 원문은 [공식 모델 snapshot](https://huggingface.co/datalab-to/surya-ocr-2/tree/3b3d4cdf88d6928b0acdc75181b13206ea67c4a3), [공식 OpenAI backend](https://github.com/datalab-to/surya/blob/c0377481c10527ec8c87bec5a10a6567fdf613fd/surya/inference/backends/openai_client.py), [공식 vLLM 설정](https://github.com/datalab-to/surya/blob/c0377481c10527ec8c87bec5a10a6567fdf613fd/surya/settings.py)이다. 이 소스 대조가 사용자 서버의 CUDA·GPU 실행 성공을 증명하지는 않는다.

## 페이지 선택과 결과 보존

먼저 기존 CPU 파서를 실행한다. 이미지 또는 추출 불가·의심스러운 텍스트·이미지 영역·표 구조 제한 때문에 OCR이 필요한 PDF 페이지를 선택한다. 선택되지 않은 페이지에는 추론을 요청하지 않는다. 한 페이지의 OCR이 완료되면 해당 페이지 블록을 문서 순서에 넣고 PDF 표는 [표 비교 규칙](table-comparison.md)에 따라 후보를 선택한다. 기존 블록과 품질 이슈는 `ocr.pages[].native_blocks`, `native_issues`에 보존한다.

| 결과 필드 | 내용 |
|---|---|
| `processor.version` | OCR 지정 실행은 `cpu-parser-surya-v1` |
| `processor.image_id` | CPU 정제 Job image ID; 모델 image ID는 `ocr.identity`에 별도 보존 |
| `ocr.client_version`, `policy` | `surya-openai-v1`, `selective-pages-v1` |
| `ocr.pages[]` | 페이지 번호, completed/partial/failed/skipped, 원인, CPU 원형, 추론 결과·렌더 정보 |
| `ocr.pages[].unresolved_visual_blocks` | 부분 인식 페이지에서 내용이 전사되지 않은 그림 블록 수 |
| `blocks[].recognition_status` | 미인식 그림에만 `unread`; 그림 위치와 읽힌 본문은 보존 |
| `blocks[].location.bbox` | PNG/JPEG는 원본 pixel, PDF는 원본 page point의 top-left 좌표 |
| `quality.issues` | 검토 필요·미완료 OCR과 그 위치 |

모델의 0–1000 좌표를 원본 페이지 크기로 변환한다. PDF는 회전 메타데이터를 반영한 전체 페이지의 top-left 좌표를 사용한다. JPEG는 EXIF 자동 회전을 적용하지 않는 원시 pixel 좌표다. PDF raster와 CPU 추출의 크기·회전값·표시 영역 원점/경계가 다르면 추론 전에 `pdf_coordinate_mismatch`로 해당 페이지를 미완료로 남긴다. 같은 크기의 원점 이동도 검사한다. crop·원점 이동의 모든 좌표 변환을 지원하는 것은 아니며 이때 native 결과·이슈와 needs_ocr를 유지한다. [좌표 검증 절차](testing-ocr-geometry.md).

HTML 블록 순서는 모델 응답 순서다. 표의 행·열 및 rowspan/colspan은 검증하되 모델이 주지 않은 셀별 bbox는 만들지 않는다. 셀 좌표 제한은 `ocr_table_cell_geometry_unavailable`로 표시한다. 코드 블록의 들여쓰기는 보존한다.

추론 성공 페이지도 `ocr_output_requires_review`를 기록한다. 따라서 OCR을 실제 수행한 문서는 최소 `needs_review`다. 토큰 확률은 페이지 평균의 보조 지표이며 교정된 OCR 정확도나 블록별 confidence로 표시하지 않는다. 평균이 없으면 검토 이슈를 추가하고, 0.5 미만이면 CPU 결과를 유지하며 `needs_ocr`로 남긴다.

잘린 생성·시간 초과·통신 오류·잘못된 구조/좌표·추출 한도 초과는 페이지별 이유와 함께 CPU 결과를 유지한다. 이러한 미완료 OCR이 하나라도 있으면 결과는 `needs_ocr`다. 결과 등록 자체는 성공할 수 있으므로 Operation succeeded만으로 OCR 완료를 판정하지 않는다. 반면 실행 전 모델 식별 불일치나 전체 파서/출력 제한 위반은 Operation 실패다.

2026-09-16부터 `Image`·`Figure`·`Diagram`의 빈 글자는 페이지 전체 실패로 처리하지 않는다. 읽을 수 있는 본문/표가 함께 있으면 그 결과와 그림 bbox를 보존하고 페이지를 `partial`, 사유를 `visual_content_unread`로 기록한다. 해당 그림 위치에는 `ocr_visual_content_unread`, `requires_ocr=true`를 남겨 문서 상태가 `needs_ocr`로 유지된다. 빈 일반 텍스트, 위험한 HTML, 잘못된 좌표, 낮은 토큰 확률은 기존 실패 처리를 유지한다. 그림만 있고 읽힌 글이 없으면 `no_readable_blocks`로 CPU 원형을 유지한다. `partial`을 모든 페이지 OCR 완료나 품질 PASS로 계산하지 않는다.

Go 등록 검증은 부분 인식 페이지의 읽힌 내용·그림 표식·페이지 번호·미인식 개수·품질 이슈가 일치하는지 확인한다. 정제 Job과 정제 API의 검증 코드를 함께 배포해야 한다. 기존 결과 JSON을 변경하거나 미인식 영역을 완전 인식으로 승격하지 않는다.

## PDF 표 선택·비교 검토 (2026-09-16)

현재 CPU 파서가 찾은 표의 `pdf_table_spans_and_borderless_tables_require_review` 이슈도 OCR 대상 선정에 사용한다. 따라서 감지된 표가 있는 PDF 페이지는 OCR 모델을 지정했을 때 페이지/시간/좌표 한도 내에서 이미 OCR을 시도한다. 무테 숫자 표 후보도 별도 감지한다. 표가 있는 모든 페이지를 완벽히 찾아내는 검출기는 아니다.

페이지 OCR의 구조/좌표/토큰 검사를 통과하면 해당 페이지 블록을 채택한 뒤 표별로 파싱 후보와 대응시킨다. 새 `ocr-priority-v1` 정책은 읽을 수 있는 OCR 표를 우선 사용하며, 값·행열·병합 차이는 검토 대상으로 남기고 두 후보를 보존한다. OCR 표가 없거나 비었으면 파싱 후보를 유지한다. 원본 PDF 바이트와 기존 native 기록도 유지한다. 자세한 비교 기준·한도는 [표 비교](table-comparison.md)를 따른다.

UI의 **빠른 작업** 또는 사용 가능한 OCR 미선택 시 `ocr_deployment_id` 없이 파서만 실행한다. 선택한 모델이 요청 직전 검사에서 중지·오류이면 파서 전용으로 제출한다. 접수된 OCR 요청의 identity 검증·재전송 정책은 그대로이며 실행 도중 임의 모델 변경이나 파서 전용 요청으로의 재작성은 하지 않는다.

새 결과의 `processing.mode`는 `ocr_priority` 또는 `parser_only`다. 요청한 방식이며 모든 표 탐지나 OCR 성공을 보증하지 않는다. 파서만 사용한 스캔/사진의 기존 미인식 품질 표시는 그대로 남긴다.

2026-09-17 첨부 sample의 4·5·6페이지는 OCR 완료로 기록됐으나 원본 대조에서 표의 누락·열 오류와 파서 fallback의 문단 중복을 확인했다. 기록된 OCR 약 178.968초 중 추가 그림 단계는 약 2.601초다. 페이지 완료 상태를 표 정확도 통과로 해석하지 않는다. 상세 근거와 이전 기록과의 구분은 [최신 점검](testing-acceptance-20260917.md)을 따른다.

## 그림 내부 글자 인식

페이지 OCR이 반환한 Image/Figure/Diagram 중 글자가 빈 영역은 검증된 페이지 raster에서 잘라 최대 3배 확대하고 같은 고정 OCR 배포에 한 번 더 요청한다. 훈련용 OCR 프롬프트·모델은 바꾸지 않는다. 모든 페이지 요청을 먼저 수행한 후 crop 단계를 실행한다. 문서당 최대 8개 요청, crop 단계 최대 120초, 요청당 최대 45초이며 기존 전체 OCR 540초 제한도 함께 적용한다. 실패한 crop을 재시도하거나 그 안의 그림에 재귀 요청하지 않는다.

`ocr.pages[].visual_regions[]`는 영역 ID, 원래 위치/라벨, 실제 crop 위치, 요청 이미지 해시·크기, 원문 HTML·사용량/확률, completed/partial/failed/skipped, 사유와 읽힌 결과 블록 수를 보관한다. 성공한 글자·표 블록은 `visual_region_id`, crop 기준 `model_bbox`와 원본 페이지 기준 location을 갖는다. Go는 영역 참조·블록 수·상태와 원본 좌표로의 환산을 확인한다. raw HTML을 UI에서 실행하지 않는다.

빈 그림만 반환·낮은 확률·위험 HTML·잘못된 좌표·시간/개수 한도는 기존 그림의 unread 상태와 본문을 보존한다. 페이지 전체가 그림뿐이었다면 성공한 crop이 있을 때만 읽힌 내용으로 전환한다. crop 성공 후도 OCR 검토 상태를 유지한다. 인식 성공은 그림 의미·관계에 대한 검증이나 설명 생성이 아니다. 이전 결과는 자동 재처리하지 않는다.

2026-09-17 보존 수정부터, 안전한 crop 응답에 읽힌 글자/표와 빈 그림 child가 함께 있으면 영역을 `partial`, 사유를 `visual_text_unread`로 기록한다. 읽힌 블록은 모델 순서대로 원래 위치에 넣고 원래 그림의 unread 블록도 정확한 source 위치에 하나 남긴다. 중첩된 빈 그림을 추가 삽입하거나 재귀 요청하지 않는다. `result_blocks`는 읽힌 블록만 센다. 페이지와 문서는 계속 `partial`/`needs_ocr`이며 제목 하나의 보존을 그림 전체의 인식 성공으로 계산하지 않는다.

Go 검증은 partial 영역에 원래 라벨·좌표의 unread 블록이 하나 있는지, 읽힌 child의 참조·개수·crop 좌표 환산이 맞는지, 페이지 partial과 미완료 품질 표시가 유지되는지 확인한다. 원래 unread 블록이 남은 completed 영역은 거절한다. 읽힌 내용이 없는 응답과 낮은 확률·위험 HTML·잘못된 좌표는 기존 실패 처리를 유지한다. 정제 API를 먼저 갱신한 후 새 Job 이미지를 활성화한다. 기존 completed/failed/skipped 결과도 계속 검증한다.

실제 응답 재생에서는 세 영역의 숫자·문구·그림 제목을 보존했지만 3페이지 내부 라벨은 여전히 0/24다. 서버의 새 추론 결과는 별도 확인해야 한다. [점검·후속 기준](testing-acceptance-20260917.md)을 따른다.

## 처리 시간 기록

새 정제 Job은 `measure_timings=true`로 파서를 호출한다. 원본/결과 JSON과 별도로 DB 구조를 추가하지 않으며, 시간은 정제 결과의 부가 정보다. 직접 파서 호출에서 이 옵션을 생략하면 기존 결정적 출력 동작을 유지한다.

| 위치 | 구간 | 의미 |
|---|---|---|
| `timings.native_parse_ms` | 기본 추출 | 입력 검사부터 CPU 추출까지 |
| `timings.ocr_ms` | OCR 처리 | 선택 페이지 렌더·요청·응답 검사·결과 병합 |
| `ocr.pages[].timings.visual_regions_ms` | 그림 내부 추가 인식 | 해당 페이지의 crop 준비·요청·응답 검사 합계; 페이지 total_ms에 포함 |
| `timings.parser_total_ms` | 추출·인식 합계 | 위 처리 전체, 결과 JSON 직렬화/파일 쓰기 제외 |
| `ocr.pages[].timings.render_ms` | 페이지 이미지 준비 | 렌더러 실행 |
| `ocr.pages[].timings.request_ms` | OCR 요청 | PNG 인코딩·전송·서버 대기·추론·응답 수신/JSON 검사 포함 |
| `ocr.pages[].timings.response_parse_ms` | 응답 정리 | HTML 블록 변환·검사 |
| `ocr.pages[].timings.total_ms` | 페이지 합계 | 해당 시도의 처리, 위 구간과 포함 관계 |

시간은 단조 시계로 측정하며 단위는 밀리초다. 실행한 실패 구간도 기록하고 실행하지 않은 구간은 생략한다. 요청 시간에서 GPU 연산만 분리해 알 수는 없다. 기존 결과에 시간 필드가 없으면 0으로 보충하지 않는다.

Operation의 `created_at`→`execution_started_at`은 **실행 전 대기·준비**이며 컨테이너 생성만의 시간은 아니다. `execution_started_at`→`completed_at`은 실행·결과 저장·등록·완료 보고를 포함한다. 이 타임스탬프와 정제 JSON의 시간은 종료 컨테이너가 삭제돼도 남는다.

추가 진단은 Dispatcher/Job 로그의 `data_job_timing_v1`을 사용한다. Dispatcher의 `container_launch`는 `docker run --detach` 호출 시간이며 작업 완료나 모델 로딩 시간이 아니다. Job은 `authorization`, `source_download`, `parser_process`, `canonical_validation`, `canonical_upload`, `canonical_registration`, `job_process`를 기록한다. 로그에는 작업 번호·단계·시간만 남기며 문서/모델 응답·설정값은 넣지 않는다. 업로드/등록 시간은 이미 저장한 JSON에 역기록하지 않고 로그로 남긴다. Data Job 로그는 컨테이너 자동 정리 후 소실될 수 있으므로 상세 조사 시 보존 기간 내 확인한다.

부분 인식 보존과 시간 계측은 지연 감소를 입증하지 않는다. 페이지 동시 요청·부분 영역 OCR·토큰/해상도 변경은 측정 후 품질과 함께 비교할 후속 작업이며 현재 순차 처리·시간/페이지 한도·자동 재요청 금지는 유지한다.

원본 MinIO 객체는 그대로 보존한다. 원시 생성 HTML은 결과 JSON 안의 데이터로만 보관하며 화면에서 직접 렌더하지 않는다. 블록의 별도 HTML은 허용 태그·속성으로 걸러낸다. 이후 UI에서도 raw HTML을 실행 가능한 HTML로 표시해서는 안 된다.

## 처리 한도

| 항목 | 현재 값 |
|---|---|
| Job / Python 파서 | 전체 20분; CPU 파서 7분, OCR 지정 파서 17분 |
| OCR 페이지 / 시간 | 최대 10페이지, 합계 540초, HTTP 요청당 최대 120초 |
| PDF raster | 192 DPI를 기준으로 최대 600만 pixel·긴 변 4096으로 축소 |
| 이미지 | 원본 4천만 pixel 초과는 OCR 보류; 전송 PNG 최대 8 MiB |
| 응답 | HTTP 2 MiB, 생성 HTML 1 MiB, 최대 생성 12,288 tokens |
| 결과·원본 | 각각 32 MiB; 기존 CPU 파서의 구조·전개 한도도 적용 |

HTTP proxy·redirect와 자동 추론 재시도는 사용하지 않는다. 시간이 남거나 CPU 품질이 나쁘다는 이유로 다른 GPU 모델을 자동 실행하지 않는다. GPU 부하·대표 한국어 문서·다단 읽기 순서·복잡한 표의 실제 품질 평가는 서버 smoke 이후의 별도 검증이다.

## 페이지·그림 중복 텍스트 병합 (2026-09-17 후속)

그림 crop의 읽힌 블록을 원래 위치에 넣은 뒤 페이지 OCR 문단과 비교한다. 동일 페이지·좌표 체계, 같은 paragraph 유형/label 및 Surya 추출이고, NFC·공백만 정규화한 글자가 완전히 같아야 한다. 두 bbox의 교집합이 작은 영역의 90% 이상이며 작은/큰 면적 비율이 0.5 이상인 일대일 대응만 합친다. 숫자·문장부호·대소문자 차이, 부분 문자열, 먼 위치, 표/코드, 일대다 대응은 보존한다.

crop 블록을 최종 본문에 남겨 `visual_region_id`, 모델 좌표와 `result_blocks` 검증을 유지한다. 제외한 페이지 블록은 ID/order를 뺀 사본으로 `ocr.pages[].deduplicated_page_blocks[]`에 보관한다. 항목은 `reason=same_text_and_region`, `removed`, `kept_visual_region_id`, `kept_model_bbox`를 가진다. 이는 병합 근거 데이터이며 본문으로 다시 인덱싱하지 않는다. 원래 페이지/crop raw 응답도 그대로다. `ocr_page_visual_duplicate_suppressed` 품질 이슈를 기록하며 부분 인식/미확인 표시는 올리지 않는다.

실제 응답 재생에서 `Register now!`의 겹치는 두 블록을 하나로 보존했고 기존 Go 검증을 통과했다. 새 추론 요청은 추가하지 않는다. 흐름도·복합 표 인식 개선은 기본 모드에 바로 넣지 않고 [영역별 실험](testing-ocr-quality.md#region-experiments)으로 효과부터 확인한다.
