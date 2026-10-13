# Surya OCR 검사·진단

모델 플랫폼의 Surya 배포와 데이터 플랫폼의 선택적 OCR을 함께 확인한다. HF import·GPU ready·PNG/스캔 PDF, 한국어와 회전/JPEG 검사는 서버에서 통과했다. [현재 상태](status.md)와 [OCR 계약](api-ocr.md)을 기준으로 필요한 범위만 재검증한다.

## 기존 배포로 확인

아래 명령은 기존 ready 배포를 사용해 테스트 PNG/PDF를 업로드·정제한다. 새 환경에서는 ID를 실제 Surya 배포로 바꾼다. 문서만 변경한 경우 실행할 필요가 없다.

```bash
cd ~/workspace/agent_project/data_platform
python tests/live_ocr_smoke.py \
  --deployment-id 29aac028-c0ad-49f6-a3fa-c2b3b67feade
```

## 코드 변경 후 빌드·회귀

```bash
cd ~/workspace/agent_project/data_platform
python ops/verify_ocr.py
```

도구는 Model/Data Compose의 DB 연결·네트워크·계약을 확인하고 parser-tests·builder와 관련 Go/실제 PostgreSQL 검사를 실행한다. DB 테스트는 임시 schema를 사용하며 FAIL/SKIP이 있으면 실패한다. 상시 서비스를 교체하지 않는다.

새 환경의 최초 OCR 설치 또는 관련 코드 변경 때는 검사 통과 후 아래 이미지와 서비스를 준비한다. 기존 PostgreSQL·Kafka·MinIO와 Model/Data 서비스가 기동된 환경을 전제로 한다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/model_platform
  docker buildx bake --builder default -f docker-bake.hcl --load management-import-job
  docker pull vllm/vllm-openai:v0.20.1
  docker compose build deployment gateway
  docker compose up -d --no-deps deployment gateway
  cd ../data_platform
  docker compose --profile jobs build data-refinement data-gateway data-dispatcher refinement-job
  docker compose up -d --no-deps data-refinement data-gateway data-dispatcher
)
```

vLLM 이미지를 재정의한 환경에서는 pull 대상도 `MODEL_SURYA_VLLM_IMAGE`와 일치시킨다. 기존 ready 배포가 없을 때만 `python tests/live_ocr_smoke.py --gpu-id 0`으로 새 import·배포를 만든다. GPU 점유가 있으면 기존 예약 정책에 따라 대기하며 메모리 비율 설정이 GPU 공유 허용을 의미하지 않는다.

## 실제 smoke가 수행하는 일

1. `/api/models`에 Surya OCR 모델을 등록하고 고정 HF revision을 import한다. 처음 실행하면 모델 다운로드와 모델 저장 공간이 필요하다.
2. 해당 snapshot을 GPU 0의 `surya-ocr-v2` profile로 배포한다. 기존 승인·GPU 예약·런타임 준비 경로를 사용한다.
3. 포함된 PNG와 텍스트 레이어 없는 PDF를 원본으로 업로드하고 OCR 배포 ID를 지정해 정제한다.
4. 원본·결과 SHA-256, 실행·모델·image ID, 멱등 재요청, CPU Job 종료를 확인한다.
5. 실제 결과에 `OCR TEST 12345`가 인식됐는지, 한 페이지 OCR이 완료됐는지, 원본 범위 안의 bbox와 `needs_review` 상태가 있는지 검사한다.

최종 성공 문구는 다음이다.

```text
PASS: managed Surya deployment -> PNG and scanned PDF -> pinned OCR provenance, text, coordinates and review state.
```

중간 CPU 정제 PASS나 Operation succeeded만으로 이 OCR 검증을 완료 처리하지 않는다. fixture 문구 미인식·미완료 OCR·모델 식별 불일치는 최종 실패다. 단순 fixture의 텍스트·좌표 검사가 통과해도 복잡한 표의 내용/병합, 한국어, 다단 문서 품질까지 검증한 것으로 확대하지 않는다.

성공 후 **Surya 배포는 실행 상태로 유지되어 GPU를 계속 사용한다.** 이후 정제 요청의 `ocr_deployment_id`로 출력된 배포 ID를 사용한다. 중지할 때는 Model Platform의 기존 해당 배포 중지 API/관리 화면을 사용한다.


## 기록과 재개

처음 출력되는 `[record] .../tests/results/ocr/<UUID>.json`이 주 보고서다. 모든 생성 요청의 키·본문을 POST 전에 저장한다. import는 최대 45분, GPU ready는 최대 30분 기다린다. API의 초기 기동도 읽기 요청으로 기다린다. 응답 유실이나 대기 시간 초과 시 같은 주 보고서로 재개한다.

```bash
cd ~/workspace/agent_project/data_platform
python tests/live_ocr_smoke.py --resume tests/results/ocr/실제-UUID.json
```

이미 ready인 새 Surya profile 배포가 있으면 모델 import·배포 생성을 생략할 수 있다.

```bash
python tests/live_ocr_smoke.py --deployment-id 실제-ready-Surya-deployment-ID
```

`--resume`는 보고서의 API·GPU·배포·키를 그대로 사용한다. 완료한 요청의 키를 새 배포나 다른 원본에 재사용하지 않는다. terminal failed 실행은 재개 명령으로 재실행되지 않는다. 원인 수정 후 새 smoke를 시작하면 새 키와 실행이 생긴다.

| 파일 | 용도 |
|---|---|
| `<UUID>.json` | 모델 import·배포·최종 OCR 판정과 재개 키 |
| `<UUID>-refinement.json` | 원본 2개와 정제 실행·Operation·결과 식별자 |
| `<UUID>-canonical-0.json`, `-1.json` | 실제 결과 전체; 생성 HTML도 데이터로 포함 |
| `model-go-tests.jsonl`, `data-go-tests.jsonl` | 서버 Go/DB 검사 결과 |

실패 시 터미널 마지막 오류와 주 보고서의 상태를 먼저 확인한다. 배포가 ready가 되지 않으면 Model Deployment의 `runtime_error_code`·`runtime_error_message`와 해당 런타임 로그를 본다. 정제가 실패하면 하위 보고서의 Operation을 조회한다. Operation은 성공했는데 최종 OCR 검사가 실패하면 저장한 canonical JSON의 `ocr.pages[].status/reason`과 `quality.issues`를 확인한다. 원인 코드만으로 추측하여 기존 실패 이력을 변경하지 않는다.


## import-errors

- 고정 Surya snapshot `3b3d4cdf88d6928b0acdc75181b13206ea67c4a3`의 확인된 42개 파일 중 모델 실행 파일과 LICENSE·README 11개를 받는다. 예제 assets·평가 자료·로고 등 31개를 제외하고 manifest에 선택 정책과 제외 목록을 기록한다. 다른 저장소·revision은 파일을 제외하지 않는다.
- HF 정보 조회와 파일 GET의 명시적 429에만 재시도한다. `Retry-After`와 `RateLimit`의 reset 시간 중 더 긴 값에 1초 여유를 더해 기다린다. 헤더가 없거나 해석할 수 없으면 301초 대기한다. HTTP 요청 하나에 재시도 최대 3회, 누적 대기 최대 20분이다. 더 긴 대기를 요구하면 그 전에 재요청하지 않고 실패로 남긴다.
- 다른 HTTP 오류·전송 도중 오류·MinIO 업로드·실패한 Operation은 자동 재시도하지 않는다. 실행 중 이미 성공한 파일은 반복 다운로드하지 않는다.
- `--retry-import`는 서버에서 이전 모델 버전과 Operation이 모두 failed인지 확인한 뒤 **같은 모델에 새 버전·Operation·키·보고서**를 만든다. 이전 실패 버전·부분 업로드는 자동 삭제하지 않는다. 재개가 필요한 보고서는 분석 중 유지한다.
- 선택적 `--hf-token`은 터미널에서 토큰을 숨겨 입력받아 기존 Management 자격 증명 경로로 전달한다. 값을 명령행·보고서에 저장하지 않는다. 수락 응답이 저장된 뒤에는 토큰 없이 재개할 수 있다. 수락 응답을 잃었다면 같은 토큰이 필요하다.
- 모델 실패 시 Operation의 오류 코드·메시지도 조회하여 출력하고 보고서에 기록한다.


이 처리는 고정 snapshot의 선택 정책과 429에 대한 제한된 대기다. 실제 수정 후 import/OCR은 성공했지만 그 실행에서 429 대기 분기를 탔는지는 확인하지 못했다. 실패한 Operation을 자동으로 다시 실행하지 않는다.

재시도 import는 **이번 실패의 보관 중인 보고서**로 `python tests/live_ocr_smoke.py --retry-import <실패-보고서.json> --gpu-id 0`을 실행한다. 이전 모델 버전·Operation이 모두 failed인지 확인하고 새 버전·키·보고서를 만든다. 응답 유실·진행 중 대기는 `--retry-import` 대신 원래 보고서의 `--resume`을 사용한다. 토큰은 `--hf-token`의 숨김 입력으로 전달하며 명령행에 값을 넣지 않는다.

Operation이 failed이면 컨테이너 종료 코드가 0이어도 실패다. 모델 오류는 runtime_error/Operation을, 정제 오류는 하위 Operation을, 품질 오류는 canonical의 `ocr.pages[].status/reason`과 `quality.issues`를 확인한다.

## 품질 검사 연결

[한국어·표·읽기 순서](testing-ocr-quality.md), [회전·JPEG·좌표 보호](testing-ocr-geometry.md)는 별도 suite다. sample2와 대표 운영 문서의 실제 결과는 [문제 PDF 검사](testing-ocr.md#pdf-fallback) 및 현재 상태에 따로 기록한다. 단순 PNG/PDF 성공으로 이 범위를 완료 처리하지 않는다.


<a id="pdf-fallback"></a>

## 원본 PDF 파싱 실패와 OCR fallback

2026-09-15. 사용자는 일부러 문제가 있는 `AI_Engineer_sample2.pdf`를 준비했고, 파일을 복구하려는 것이 아니라 **원본 그대로 업로드한 후 Parsing에서 텍스트를 읽지 못하면 OCR까지 진행하는지** 확인하려 했다. 앞서 제공한 repaired PDF는 이 테스트에 사용하지 않는다.

### 동작 기준

| 단계 | 처리 |
|---|---|
| 업로드 | PDF 형식을 식별하고 원본 바이트·해시를 그대로 저장한다. 텍스트 추출 가능 여부나 완전한 종료 표식을 요구하지 않는다. |
| 일반 파싱 | 파일을 열어 글자·표·페이지 구조를 읽는다. 정상적으로 읽은 페이지는 보존한다. |
| 글자가 없는 페이지 | 기존 선택적 OCR 대상으로 지정한다. |
| 파싱 예외 | 별도 PDF 렌더러로 페이지 정보를 얻고, 파싱하지 못한 페이지를 OCR 대상으로 지정한다. |
| OCR | 기존 배포로 렌더링한 페이지의 글자를 읽는다. 성공·실패·미처리를 구분한다. |
| 렌더링도 불가능 | 성공으로 처리하지 않는다. 페이지 정보를 얻지 못하면 작업 실패, 개별 렌더링/OCR 실패는 needs_ocr로 남는다. |

수집과 정제는 독립 요청이다. 업로드는 원본 등록까지 하고, 사용자가 **내용 정리**를 눌렀을 때 정제가 실행된다. 기존 모델 import나 배포를 새로 만들지 않는다.

PDF 등록은 제한된 앞부분의 공백/UTF-8 BOM 뒤 `%PDF-1.x` 또는 `%PDF-2.0` 식별자를 확인한다. 앞 공백은 최대 1,024바이트이며 인식되지 않는 다른 형식, 빈 파일, 32 MiB 초과, 잘못된 파일명/전송은 계속 거절한다. EOF·xref 완전성 검사와 문서 해석은 정제 단계의 책임이다. 새 등록 형식 버전은 `source-format-v2`다. 기존 v1 원본·응답·해시는 수정하지 않는다.

### 코드 변경 후 검사·빌드

현재 프로젝트 소스에는 관련 수정이 포함되어 있다. 필요한 코드 변경 뒤 아래 검사·빌드 절차만 실행한다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/data_platform
  bash ops/apply_pdf_ocr_fallback.sh
)
```

스크립트는 Go 원본 식별 검사, 파서/OCR/좌표 검사, UI 검사를 끝낸 뒤 Collection·refinement-job·UI 이미지를 빌드한다. 실행 중인 서비스는 **data-collection과 data-console만 재생성**한다. Dispatcher는 다음 정제 요청부터 새 refinement-job 이미지 ID를 확인해 사용한다. 기존 API/Gateway·OCR 배포·저장소와 요청 복구 volume은 유지한다. 파일을 수정하거나 새 데이터 처리 요청을 자동으로 보내지 않는다.

검사 출력은 `tests/results/pdf-ocr-fallback/server-*.log`에 저장된다. 지정된 Go/파서/OCR/좌표/UI 검사를 모두 통과하고 DB 검사가 SKIP되지 않았는지 확인한다. 테스트 수는 코드 버전에 따라 달라지므로 예전 개수를 현재 통과 조건으로 고정하지 않는다. 당시 기록은 Go 최상위 4개·하위 43개, 파서 계열 Python 46개·UI 91개였다. 과거 PDF 패치 당시 UI 게이트는 77개였으며 이후 화면 검사 추가로 83개, 2026-09-16 결과 검증 추가로 91개가 됐다. 호스트에 Python·Go 의존성을 설치하지 않는다.

### 화면에서 원본으로 확인

1. [데이터 관리](http://192.168.0.119:14100)를 새로고침한다.
2. 이전 업로드 오류가 남아 있으면 **입력 수정**을 누른다.
3. **AI_Engineer_sample2.pdf 원본**을 선택하고 **가져오기**를 누른다. `AI_Engineer_sample2_repaired.pdf`는 선택하지 않는다.
4. 파일 미리보기의 **내용 정리**를 누른다.
5. **결과 확인**에서 **글자 인식 결과: 2페이지 완료**와 실제 추출된 본문·표를 확인한다.

OCR 실패 시 완료 페이지 수와 읽기 실패/미처리 페이지 수가 따로 표시된다. 결과가 `내용 확인 필요`인 것은 OCR 이후에도 원본과 정확도를 대조해야 함을 뜻한다. `일부 내용 미확인`은 완료되지 않은 부분이 남아 있다는 뜻이다. 원본/정리 결과를 전환해 표와 숫자를 비교한다.

이번 원본의 기대 식별자는 **420,275바이트**, SHA-256 **dbd0a127e8b865ffac35c12484f85389a9e996bbf1287372699d44b95123342f**다. 정제는 이 해시를 유지하고 결과를 별도 저장한다. 코드에 이 파일명이나 해시를 특별 처리하는 분기는 없다.

### 결과를 읽기 전용으로 검증

화면에서 실행한 정리 결과 주소의 `id=` 값을 사용한다. 해당 화면은 `page=refine`이어야 한다. 기존 원본으로 이미 완료된 결과가 있다면 새 업로드나 정리 없이 그 결과를 확인할 수 있다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/data_platform
  read -r -p '정리 결과 주소의 id 값: ' PDF_REFINEMENT_RUN_ID
  test -n "$PDF_REFINEMENT_RUN_ID"
  docker compose --profile ui exec -T data-console \
    python check_connection.py \
    --refinement-run-id "$PDF_REFINEMENT_RUN_ID" \
    --expect-source-sha256 dbd0a127e8b865ffac35c12484f85389a9e996bbf1287372699d44b95123342f \
    --expect-ocr-pages 2
)
```

검사는 GET·다운로드만 사용한다. 원본·결과 크기와 해시, 요청/결과 식별자, 처리기·OCR 모델 정보, 두 페이지 각각의 완료 상태와 읽을 수 있는 본문·표를 확인한다. 실패·미처리·중복/누락 페이지, `needs_ocr`, 다른 원본 해시를 성공으로 처리하지 않는다. 읽을 수 있는 글이 없는 빈 페이지는 이 기대 조건을 통과하지 못한다.

이 명령의 통과는 저장된 처리 결과에 대한 확인이다. 글자 정확도·표 배치의 검수나 직접 GPU 이용률 측정을 대신하지 않는다. 일반 텍스트/선택적 OCR 혼합 문서는 `--expect-ocr-pages`를 생략한다.

### 검증 결과와 한계

- 실제 첨부 원본: 변경된 **Go Recognize 실행 PASS**, 동일 바이트/해시 보존 확인.
- 일반 파싱: 원본 **2페이지·추출 글자 0자·needs_ocr** 확인.
- 실제 PDFium 렌더링: 두 페이지 모두 **1587×2245 이미지** 생성.
- 실제 Surya HTTP 클라이언트: `/v1/chat/completions` 요청이 **2회** 나가는 것을 확인. 응답은 로컬 테스트 서버에서 제공했다.
- Go canonical 검증: 원본의 글자 없음 경로와 강제로 주입한 파싱 예외 fallback 결과 모두 PASS.
- 회귀 검사: Python **112개 PASS**(기본 파서 20, OCR 21, 좌표 5, 업로드 도구 8, UI 58). Go 원본 식별 최상위 **4개 PASS**, 하위 **43개 PASS**.
- 일반 텍스트 PDF의 불필요한 OCR 방지, 성공한 앞 페이지 보존, 파싱 예외, 손상 PDF 렌더링 실패, OCR 실패, 페이지/시간/메모리 제한, crop/회전 좌표 보호를 검사했다.

**이 로컬 검사만으로 실제 서버 업로드·Docker Job·GPU 품질을 검증한 것은 아니다.** 후속 사용자 확인은 아래 별도 기록을 따른다. 테스트 대역의 완료 응답을 실제 OCR 성공으로 취급하지 않는다. 파서 의존성은 프로젝트 핀과 같은 lxml 6.1.1, pdfplumber 0.11.8, Pillow 12.3.0, pypdfium2 5.3.0을 사용했다.

기록: `src/platform/data/tests/results/pdf-ocr-fallback/local-summary.json`, `sample2-original.json`, 각 검사 로그. [구현 이력](../history/data-platform-202609.md#2026-09-15-20-pdf-ocr-fallback).

서버 후속 기록에서는 Go 원본 검사 4개/하위43개, 당시 Python 123개(파서46+UI77), runtime 자체 검사·이미지 빌드·Collection/UI 재생성을 확인했다. 2026-09-16 후속 대화에서 사용자가 sample2 원본의 실제 OCR 동작을 확인했다. 새로 첨부한 `AI_Engineer_sample.pdf`는 다른 9페이지 문서이며, 그 문서의 표·그림 품질 문제와 구분한다. 과거 원본 로그는 [검증 요약](../testing/results-summary-20260916.md)으로 정리했다.
