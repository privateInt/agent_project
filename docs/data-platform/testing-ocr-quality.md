# 한국어 OCR 품질 검증 — 2026-09-15

기존 Surya 배포를 사용해 한국어 본문, 병합 표, 2단 문서, 텍스트·스캔 혼합 PDF를 평가한다. 4개 파일·5개 페이지 중 OCR 대상은 4페이지다. **2026-09-15 사용자 서버에서 네 문서 모두 품질 PASS**, 5페이지 공백 제외 CER 0%, 서버 도구 테스트 15개 OK를 확인했다. [검증 범위와 결과 이력](../history/data-platform-202609.md#2026-09-15-04-ocr-quality-verified). 근거는 공유된 콘솔이며 report.json·canonical 전체를 직접 받은 것은 아니다.

완료 보고서는 `tests/results/ocr-quality/a6883b64-99e0-4a48-87ae-f1f36e990a9b/report.json`이다. 같은 검사를 반복할 필요는 없다. 아래 실행 절차는 향후 회귀 검사에 사용한다. 현재 다음 작업은 별도 패치의 [회전·crop PDF와 JPEG 검증](testing-ocr-geometry.md)이다. 이 새 패치는 CPU Job 이미지 빌드가 필요하다.

## 적용·실행

현재 프로젝트 소스에는 관련 수정이 포함되어 있다. 필요한 코드 변경 뒤 아래 검사·빌드 절차만 실행한다.

```bash
cd ~/workspace/agent_project/data_platform

python tests/test_ocr_quality_tools.py -v
python tests/live_ocr_quality.py \
  --deployment-id 29aac028-c0ad-49f6-a3fa-c2b3b67feade
```

Model API 기본 주소는 `http://127.0.0.1:13000`, Data API는 `http://127.0.0.1:14000`이다. 각각 `--model-api`, `--api`로 변경할 수 있다. `--timeout`은 정제 실행별 대기 시간으로 기본 1500초다.

처음에 `[record] .../tests/results/ocr-quality/<실행-ID>/report.json`과 `[html]` 경로가 출력된다. 기존 배포가 ready인지 확인한 뒤 원본 4개 업로드·정제 요청을 순차 실행한다. 각 CPU Job은 기존 Surya 서비스에 OCR을 요청한다. 완료 후에도 배포는 유지된다.

준비된 배포가 없으면 요청을 중단한다. 이 도구는 모델을 자동으로 가져오거나 배포하지 않는다. 배포 ID는 직전 서버 로그에서 확인한 값이며, 현재 ready 상태는 실행 시 다시 확인한다.

## 입력과 판정

| 파일 | 구성 | 확인할 내용 |
|---|---|---|
| `korean-body.pdf` | 스캔 1페이지 | 한국어, 문서번호, 날짜, 금액 |
| `korean-table.pdf` | 스캔 1페이지, 5행·4열의 논리 격자와 실제 셀 14개 | 셀 위치·값, 가로/세로 병합 |
| `two-columns.pdf` | 스캔 1페이지 | 왼쪽 단 전체 → 오른쪽 단 전체 순서, 각 단의 위치 |
| `mixed-pages.pdf` | 검색 가능한 텍스트 1페이지 + 스캔 1페이지 | 1페이지 CPU 텍스트 유지, 2페이지만 OCR |

정답은 직접 작성한 텍스트와 표 구조다. OCR 응답에서 정답을 만들지 않았다. A4 크기는 595.28×841.89 PDF point이고 스캔은 180 DPI로 제작했다. 중복 미리보기 `preview.pdf`는 배포본에서 제외했다. 위의 개별 PDF에서 페이지를 확인한다. 테스트 실행에는 별도 TTF가 필요하지 않다. 샘플 재생성 시에만 [외부 폰트 복원 조건](../testing/artifact-retention.md#입력자료-정리)을 따른다.

판정 기준 `ocr-quality-v1`은 이 프로젝트의 회귀 검사 기준이다. 공인 OCR 벤치마크의 합격선은 아니다.

| 항목 | 통과 기준 |
|---|---|
| 본문 CER | NFC 정규화 후 모든 공백을 제외한 Levenshtein 문자 오류율 ≤ 2%, 페이지마다 판정. 표 셀 텍스트는 본문에서 제외 |
| 읽기 순서 | 지정한 본문 표지 문구가 각각 한 번 나타나고 정답 순서대로 위치. 2단 문서는 두 단을 번갈아 읽으면 실패 |
| 중요 문구·영역 | 제목·대표 문구·번호·금액·날짜가 정확히 한 번 나타나고 해당 블록들의 합집합 bbox 중심이 정답 영역 내에 위치. 허용 여유 8 PDF point |
| 좌표 범위 | 모든 블록 bbox가 유한한 값이고 페이지 안에 위치. 영역 검사는 픽셀 단위 bbox 정확도나 IoU 측정이 아님 |
| 표 | 표 개수와 모든 셀의 행·열·정규화 텍스트·row_span·column_span 정확 일치. 누락·추가·중복 셀은 실패 |
| OCR 선택 | 예상한 페이지만 OCR 기록에 있고 모두 completed. 혼합 PDF는 `[2]`, native 페이지 블록에는 OCR extraction 표시가 없어야 함 |
| 출처·상태 | 원본/결과 해시, 모델·이미지·컨테이너, 처리기·선택 정책 일치. needs_review와 검토 이슈 유지, requires_ocr 미해결 이슈 없음 |

공백·줄바꿈의 정확도는 CER에서 측정하지 않는다. 표 셀마다 실제 좌표가 없는 기존 제한도 그대로 남으며 셀 bbox를 추정하여 채우지 않는다. `needs_review` 자체는 이번 검사에서 정상 상태이고, 모든 측정 항목을 통과해도 검토 이슈를 제거하지 않는다.

전체 PASS는 네 파일이 모두 평가되고 모든 항목이 통과했을 때만 출력한다. 각 정제 단계에서 출력하는 기존 `PASS: immutable source ...`는 원본·작업·저장 경로 검사다. 한국어 품질의 최종 판정은 `[quality]`, `[quality-summary]` 및 마지막 품질 PASS/FAIL을 확인한다.

## 중단·실패·재평가

연결이 끊기거나 대기 시간이 끝나면 출력된 **같은 report.json**으로 재개한다. 아래 `<실행-ID>`를 실제 경로로 바꾼다.

```bash
python tests/live_ocr_quality.py \
  --resume tests/results/ocr-quality/실제-실행-ID/report.json
```

모든 업로드·정제 요청 키를 최초 POST 전에 저장한다. 재개할 때 전체 PDF와 정답, 요청 키, 완료 결과의 해시를 먼저 확인한다. 완료한 파일은 다시 OCR하지 않는다. 아직 진행 중인 요청은 같은 키로 조회·재전송하며 새 Operation으로 바꾸지 않는다. 보고서와 실행 도중 원본 PDF·suite.json을 수정하지 않는다.

품질 FAIL이어도 나머지 문서 측정은 계속한다. 실제 terminal failed/cancelled 정제는 실패 이력을 유지하고 나머지를 실행한다. 응답 유실·blocked·출처 불일치처럼 상태를 확인해야 하는 경우는 중단하며 같은 기록으로 재개한다. 실행 도중 Surya 컨테이너나 모델·이미지 식별자가 달라지면 서로 다른 실행 조건을 섞지 않고 중단한다.

모든 결과가 저장돼 있으면 서비스가 내려간 뒤에도 다시 평가할 수 있다.

```bash
python tests/live_ocr_quality.py \
  --evaluate tests/results/ocr-quality/실제-실행-ID/report.json
```

`--evaluate`는 네트워크·Docker 호출 없이 저장한 canonical 바이트와 정답으로 평가한다. 미완료·실패 파일을 성공으로 바꾸지 않는다. 진행 중인 기록은 원래 서버 경로에서 재개하고, 오프라인 평가용 복사본은 결과 디렉터리 전체를 유지한다.

## 결과 공유

실행별 디렉터리에는 다음이 남는다.

- `report.json`: 정답·출처·요청 키·파일별/페이지별 판정과 오류.
- `report.html`: 브라우저로 여는 판정표, 정답과 인식 본문, 좌표·표 차이. 모델 텍스트는 HTML 이스케이프하여 표시.
- `refinement/*.json`: 각 원본의 요청·Operation·Job·canonical 메타데이터.
- `canonical/*.json`: 해시 확인한 실제 canonical 원문. 원시 OCR 응답과 native 보존 정보도 기존 계약대로 포함.

문제 분석 시 `[quality-summary]` 출력과 해당 실행 디렉터리를 함께 전달한다. 결과를 묶을 때에는 다음처럼 **해당 실행-ID만** 지정한다.

```bash
tar -czvf ocr-quality-results.tar.gz \
  -C tests/results/ocr-quality 실제-실행-ID
```

## 로컬 검증과 범위

이번 변경 후 Python 전체 72개 테스트 PASS. 이 중 새 품질 도구 검사 15개, 실제 PDF 텍스트층·CPU 페이지 선택 검사 1개다. Python 3.10 구문 호환을 확인했고 실제 로컬 테스트 런타임은 Python 3.12다. 기록: `tests/results/ocr-quality/local-python-tests.log`.

5페이지를 렌더링하여 한글 누락·잘림·표 병합·2단 배치를 확인했다. 도구 테스트의 OCR 응답은 정답으로 만든 모의 데이터다. 이후 사용자가 공유한 실제 서버 실행에서 한국어 CER·표 값/병합·2단 순서·선택적 OCR 모두 PASS를 확인했다. 서버 도구 테스트 15개는 로컬 전체 72개에 포함되므로 합산하지 않는다. 이번 변경은 서버 Go 코드·DB·실행 이미지를 바꾸지 않아 해당 검사를 반복하지 않았다.

이 합성 문서는 한국어·표·읽기 순서·선택적 OCR의 첫 품질 점검이다. JPEG, 회전/crop, 기울어짐·노이즈·손글씨, 긴 문서, 실제 운영 문서의 정확도는 후속 범위다. 네 문서에서 실패한 항목은 없으며 회전/crop·JPEG와 대표 운영 문서 확인을 거쳐 브라우저 수집·첨부 확장으로 이어간다.


<a id="region-experiments"></a>

## 영역별 OCR 실험과 판정

기준일: 2026-09-18. 수동 3입력과 픽셀 기반 9입력 실험은 모두 끝났다. 추가 튜닝은 보류다. Data의 실험이며 Knowledge에 PDF 구조 보정 책임을 넘기지 않는다. 아래 실행법은 이후 의도적으로 새 실험을 할 때만 사용한다. 현재 기능 상태는 [Data 상태](status.md), 상세 시간순 근거는 [품질 이력](testing-acceptance-20260917.md)에 둔다.

### 수동 영역 실험

`tests/live_document_regions.py`는 `fixtures/document-quality/regions.json`에 사람이 지정한 물리 3·4페이지의 원본 픽셀을 사용한다. 그림 라벨 모음 1개와 Table 1 좌/우 2개를 새 PNG 자료로 업로드하고 각각 정제한다. 원본 SHA가 기준과 다르면 요청 전에 거절한다. 정답 문자열은 모델 입력에 넣지 않는다.

초기 검사는 그림 라벨 24/24, 왼쪽 값 0/3, 오른쪽 값 11/11·공정명 2/2로 세 사례 중 둘을 통과시켰다. 이는 공정과 값의 연결 전체를 검증한 결과가 아니다. 강화된 공정/투입·산출/항목/수량/단위 검사에서는 같은 원본의 좌7/20·우2/21 관계만 맞아 두 표 모두 실패했다. 그림은 24/24지만 화살표 관계와 의미 읽기 순서를 검증하지 않았다.

### 픽셀 기반 영역 실험

`tests/live_raster_regions.py`는 PDFium 렌더링 픽셀의 선·반복 머리글·번호 열의 글자 위치로 상자 24개와 표 8구간을 찾는다. PDF 텍스트·parser bbox·baseline 텍스트는 탐지에 쓰지 않는다. 페이지 선택과 정답은 이 샘플에 고정되어 있어 일반 문서 자동 탐지 성능을 뜻하지 않는다.

- 그림 1개, 왼쪽 표 4개, 오른쪽 번호 없는 선행 구간 1개와 표 3개, 합계 9개 PNG를 새 자료로 업로드·정제한다.
- 원본 머리글 픽셀을 반복 배치하며 본문 행의 글자·수치를 옮기거나 공정 번호를 만들어 넣지 않는다.
- `raster-reference.json`은 사람이 원본을 보고 작성한 41개 데이터 행의 정답이다. 공정 번호/이름 → Inputs/Outputs → 항목 → 수량 → 단위, 행 병합·누락·추가·중복·머리글을 함께 검사한다.
- 결과 좌표는 `rendered-page-pixels-top-left`이다. PDF 텍스트 좌표와 같지 않으며 표 셀의 원래 위치 복원·원문 병합은 구현하지 않았다.

최종 실험은 9개 모두 평가됐지만 품질 0/9이며 그림과 왼쪽 표 두 번째 입력은 약 120초 timeout이었다. 모든 글자가 틀렸다는 의미는 아니다. 운영 기본 정제와 원래 canonical은 교체하지 않았다.

### 새 실험의 실행 조건

이 프로젝트에는 실험 코드와 Docker 검사 stage가 포함돼 있다. 과거 일회성 patch의 `apply.py`, `repair.sh`, `experiment.sh`는 필요하지 않다. 최신 코드에서 parser-tests만 빌드한다. 이 stage는 일반 사용자 권한으로 CPU 회귀와 CLI import를 검사한다. 운영 API·Job·OCR 배포를 새로 띄우거나 교체하는 명령이 아니다.

사용자 서버의 Linux/Docker에서 현재 원본·ready OCR·baseline을 확인한 뒤 실행한다. 아래 ID는 과거 기록 예시다. 서버의 실제 값을 지정해야 하며 baseline은 동일 원본과 모델 identity를 만족해야 한다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/data_platform
  REGION_SOURCE_ID='00140153-b226-462b-9a6e-b5f6647f9525'
  REGION_OCR_ID='29aac028-c0ad-49f6-a3fa-c2b3b67feade'
  REGION_BASELINE_ID='9a38f968-e035-49e3-acfd-740fa4614b86'
  REGION_RUNNER='live_raster_regions.py'
  REGION_RESULTS_DIR=$(mktemp -d "$HOME/workspace/agent-region-results.XXXXXX")

  docker build --target parser-tests -t data-platform-check:regions .
  docker run --rm --read-only --cap-drop ALL \
    --security-opt no-new-privileges --network host \
    --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,nosuid,nodev,size=256m \
    --mount "type=bind,src=${REGION_RESULTS_DIR},dst=/results" \
    data-platform-check:regions \
    python -B "tests/${REGION_RUNNER}" \
      --api http://127.0.0.1:14000 --model-api http://127.0.0.1:13000 \
      --source-id "$REGION_SOURCE_ID" --deployment-id "$REGION_OCR_ID" \
      --baseline-run-id "$REGION_BASELINE_ID" --output-root /results
)
```

수동 3입력이 목적일 때만 `REGION_RUNNER='live_document_regions.py'`로 바꾼다. 두 명령을 연속 실행할 필요는 없다. runner에는 Docker socket·GPU를 전달하지 않지만 공개 API 요청으로 서버의 정제 Job과 기존 GPU OCR 추론이 실행된다. 임시 검사 컨테이너는 `--rm`으로 제거되며 실제 Job은 기존 [GC 정책](../operations/gc.md)을 따른다.

입력·결과는 프로젝트 밖 `agent-region-results.*`에 남는다. 출력 경로를 자동 삭제하지 않으며 분석/재개가 끝난 뒤 [보관 정책](../testing/artifact-retention.md)에 맞게 요약한다. 원본 PDF·baseline의 서버 객체와 실험 PNG의 서버 등록 객체는 로컬 폴더 정리와 별개다.

### 결과·재개·오프라인 평가

`[record] /results/<ID>/report.json`은 위 바인드한 호스트 디렉터리 안의 `<ID>/report.json`이다. report에는 baseline·실행 identity·입력·키·최종 JSON·평가가, 같은 실행 폴더에는 입력 PNG·canonical 사본이 저장된다.

| 표시 | 의미 |
|---|---|
| `complete=true` | 모든 사례가 평가되거나 종료 실패로 기록됨 |
| `all_probes_passed=true` | 해당 샘플의 정해진 항목 통과; 전체 문서 품질 아님 |
| runner 종료 코드 0 | 모든 사례의 결과 수집·평가 완료; 품질 실패는 별도 필드에 남음 |
| `candidate_parser_ms` | 파생 입력 3개 또는 9개의 정제 구간 합계; 원본 1회 지연 아님 |

응답 유실·대기 만료 후에는 원래 호스트 디렉터리를 다시 같은 `/results`에 마운트하고, 실행 명령의 source/deployment/baseline/output 옵션을 `--resume /results/<ID>/report.json`으로 바꾼다. 새 임시 폴더나 새 요청 키로 대체하지 않는다. `--evaluate`는 저장 파일만 평가한다. 코드·입력·요청·모델/런타임 identity 변경, 파일 누락/손상이 있으면 거절한다.

2026-09-18 전달본에는 과거 전체 report와 입력 PNG를 남기지 않았다. [요약 JSON](../../src/platform/data/tests/results/retained-summary-20260918.json)과 대표 canonical은 기존 실행의 재개 입력이 아니다. 서버에 완전한 실행 폴더가 따로 있는 경우에만 재개할 수 있다. `raster_quality.py --report` 역시 온전한 수동 실험 report가 필요하므로 삭제된 경로를 기본 예제로 사용하지 않는다.

### 구현·검증 이력

2026-09-17 최초 helper import의 PermissionError를 검사 이미지 내 `a+rX`와 non-root 빌드 검사로 수정했고, 후속 서버 빌드와 수동 실험 실행을 확인했다. 자동 후보 추가 당시 관련 129개 로컬 검사·CLI import 및 후속 서버 검사 stage 빌드가 통과했다. 이는 GPU 인식 품질 통과가 아니다. 당시 image ID·실험 ID와 판정은 [품질 이력](testing-acceptance-20260917.md)에 보존한다.

이번 문서 검토에서는 API/모델에 요청하지 않았다. 표·그림 비교는 보존 canonical 3개로 읽기 전용 재확인했다. 자동 후보는 운영 기본 경로에 넣지 않는다. 향후 채택하려면 대표 문서에서 출처·좌표·중복·품질·시간 상한을 별도 검증해야 한다.
