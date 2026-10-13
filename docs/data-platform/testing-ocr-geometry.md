# 회전·crop PDF와 JPEG 검증 — 2026-09-15

기존 한국어 품질 4문서는 서버 PASS했다. 이번 단계는 PDF 회전·좌표 원점과 JPEG의 픽셀 좌표를 검증한다. **2026-09-15 사용자 서버에서 OCR 품질 5개 PASS + 좌표 불일치 보존 2개 GUARD_PASS를 확인했다.** OCR 5개의 공백 제외 CER은 모두 0%다. [검증 완료 이력](../history/data-platform-202609.md#2026-09-15-06-ocr-geometry-verified).

완료 보고서는 `tests/results/ocr-quality/7dfba596-ba5d-4440-9b70-447f593d3b19/report.json`이다. 같은 검사를 다시 실행할 필요는 없다. 아래 적용·실행 명령은 향후 회귀 검사용으로 보존하며 다음은 대표 운영 문서의 품질 확인이다.

## 이번 수정

PDF 렌더러가 크기만 비교하던 검사에 회전값과 실제 표시 영역의 원점·경계를 추가했다. 크기가 같은 상태에서 MediaBox 원점만 이동한 PDF는 이전 검사로 걸러지지 않아 OCR 좌표가 어긋날 수 있었다. PDFium의 상속된 MediaBox/CropBox 교집합을 읽어 canonical의 전체 회전 페이지 좌표 기준과 비교한다.

좌표가 맞지 않으면 추론 전에 `pdf_coordinate_mismatch`를 기록한다. native 블록·이슈와 `needs_ocr`를 유지하고, 잘못된 좌표의 OCR 결과로 교체하지 않는다. 이번 변경은 crop 좌표 변환의 완전한 지원을 추가한 것이 아니다.

JPEG는 기존대로 EXIF 자동 회전 없이 저장된 픽셀을 사용한다. PDF 90/180/270도는 파일의 회전 메타데이터를 반영한 페이지 좌표를 사용한다. API·DB·Go 실행 계약과 OCR 정책 식별자는 유지하며 코드 차이는 CPU Job image ID에 남는다.

## 서버 적용

현재 프로젝트 소스에는 관련 수정이 포함되어 있다. 필요한 코드 변경 뒤 아래 검사·빌드 절차만 실행한다.

아래 블록 전체를 실행한다. `set -euo pipefail`을 괄호 안에서 사용하므로 파일이 없거나 빌드·검사에 실패하면 다음 명령으로 넘어가지 않고, 현재 터미널 세션은 유지된다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/data_platform

  python -m unittest discover -s tests -p 'test_ocr*tools.py' -v
  docker build --target parser-tests -t data-platform-check:ocr-geometry .
  docker compose --profile jobs build refinement-job

  python tests/live_ocr_quality.py \
    --suite tests/fixtures/ocr-geometry/suite.json \
    --deployment-id 29aac028-c0ad-49f6-a3fa-c2b3b67feade
)
```

호스트는 기존 `(sh_test)` Python 3.10과 Docker를 사용한다. 표준 라이브러리 도구 26개를 실행하고, PDF/JPEG 렌더링 테스트는 `parser-tests` 이미지 빌드에서 수행한다. 새 Python 환경이나 호스트 PDF 패키지 설치는 필요 없다.

이번에는 `ocr.py`를 수정했으므로 **refinement-job 이미지 빌드가 필요하다.** 빌드에는 기존 Go → isolated Python → canonical 자체 검사도 포함된다. Model import·Surya 배포·상시 Model/Data 서비스 재시작은 수행하지 않는다. 기존 Surya 배포는 각 미완료 파일 처리 전에 ready 상태와 고정 식별자를 재확인한다.

## 7개 입력과 기대 결과

| 입력 | 검증 내용 | 기대 결과 |
|---|---|---|
| `rotate-90.pdf` | /Rotate=90과 원본 페이지 좌표 | OCR 품질 PASS, needs_review |
| `rotate-180.pdf` | 크기가 바뀌지 않는 180도 회전 | OCR 품질 PASS, needs_review |
| `rotate-270.pdf` | /Rotate=270과 원본 페이지 좌표 | OCR 품질 PASS, needs_review |
| `cropped.pdf` | 표시 영역과 전체 페이지 크기가 다름 | GUARD_PASS, needs_ocr |
| `shifted-origin.pdf` | 크기는 같지만 원점이 (20,30)으로 이동 | GUARD_PASS, needs_ocr |
| `jpeg.jpg` | 일반 JPEG의 원본 pixel 좌표 | OCR 품질 PASS, needs_review |
| `jpeg-exif-6.jpg` | EXIF 방향값 6을 자동 적용하지 않는 정책 | OCR 품질 PASS, needs_review |

회전 PDF는 저장된 그림을 반대 방향으로 회전시켜 PDF 뷰어와 모델 입력에서는 정방향으로 보인다. 따라서 이 샘플은 **PDF 페이지 회전 변환**을 검증하며 실제 옆으로 누운 글자를 모델이 얼마나 잘 읽는지 평가하는 샘플은 아니다. EXIF 6 JPEG도 원시 픽셀은 정방향이며, EXIF 자동 회전을 적용하면 서로 달라지는 비대칭 문구로 정책을 검사한다.

중복 미리보기 `fixtures/ocr-geometry/preview.pdf`는 배포본에서 제외했다. 실행에는 위 개별 원본 7개와 로컬 좌표 비교용 `reference.png`를 사용한다. 별도 TTF는 테스트 실행에 필요하지 않으며, 샘플 재생성 시에만 [외부 폰트 복원 조건](../testing/artifact-retention.md#입력자료-정리)을 따른다.

## 판정 해석

`ocr-geometry-v1` 기준은 OCR 품질과 좌표 불일치 보존을 별개로 센다.

- OCR 5개: 공백 제외 NFC CER ≤ 2%, 지정 문구와 읽기 순서, 각 문구의 영역, 모든 bbox 범위, 페이지 좌표계·회전값·출처·정책·완료 상태를 검사한다. 영역 여유 8단위는 PDF point 또는 이미지 pixel이다.
- 보존 2개: 정확히 `failed/pdf_coordinate_mismatch`, native 블록·기존 이슈의 보존, `ocr_pdf_coordinate_mismatch`의 requires_ocr=true, 최종 needs_ocr를 검사한다. 렌더 결과·원시 모델 응답 등 추론 기록이 생기면 실패한다. 문자·표 점수는 N/A다.

**GUARD_PASS는 해당 문서의 OCR 성공을 뜻하지 않는다.** 지원하지 않는 좌표를 표시하고 추론 전 원본 결과를 보존한 검사의 통과다. 다른 오류(timeout 등), Operation 자체 실패, 원본 유실은 이 통과 조건에 해당하지 않는다.

사용자가 공유한 서버 실행 로그의 전체 결과는 다음과 같다.

```json
{
  "passed": true,
  "evaluated_cases": 7,
  "passed_cases": 7,
  "ocr_passed_cases": 5,
  "guard_passed_cases": 2
}
```

정제 경로의 `PASS: immutable source ...`와 품질 검사의 `[quality]`를 구분한다. 좌표를 처리할 수 없는 두 문서도 결과 등록 Operation은 succeeded가 될 수 있지만 canonical은 needs_ocr다.

## 중단·결과 공유

실행 시작 시 기존 도구가 다음 경로를 출력한다.

`tests/results/ocr-quality/<새 실행-ID>/report.json`

새 suite도 동일한 보고서 디렉터리 규칙을 사용하며 내부 `rubric_version=ocr-geometry-v1`로 구분한다. 품질 FAIL 뒤에도 다른 파일을 측정한다. 응답 유실·진행 중 시간 초과에는 새 실행을 만들지 않고 같은 보고서를 재개한다.

```bash
python tests/live_ocr_quality.py \
  --resume tests/results/ocr-quality/실제-실행-ID/report.json
```

재개 시 `--suite`와 `--deployment-id`를 추가하지 않는다. 기록된 정답·원본 해시·요청 키·모델 식별자를 사용한다. 완료 결과는 해시 검증 후 재평가하며 다시 OCR하지 않는다. terminal failed Operation은 이력을 보존한다. 기존 한국어 4문서 보고서도 기존 기준 `ocr-quality-v1`로 읽고 평가한다.

서비스 없이 저장된 결과를 재평가할 때에는 `--resume` 대신 `--evaluate`를 사용한다. 보고서 디렉터리에는 JSON/HTML 판정과 `refinement/*.json`, `canonical/*.json`이 남는다. 완료 후 `[quality-summary]`와 해당 실행 디렉터리를 전달한다.

```bash
tar -czvf ocr-geometry-results.tar.gz \
  -C tests/results/ocr-quality 실제-실행-ID
```

## 확인한 범위

- 로컬 Python 전체 84개 PASS: 기존 72개와 새 도구 7개·실제 렌더/변환 5개. 실제 로컬 런타임은 Python 3.12이고 새 코드의 Python 3.10 구문을 별도로 확인했다.
- PDF 3개 회전 렌더를 독립된 기준 PNG와 비교했다. 일반/EXIF JPEG는 저장된 픽셀과 렌더 결과가 일치했다.
- crop·원점 이동 입력은 native 결과가 유지되고 모의 추론 함수가 호출되지 않는 것을 확인했다. 모의 HTML → 실제 canonical 좌표 변환도 검사했다.
- 샘플을 다시 생성해 바이트가 같은지 확인했고, 8페이지 미리보기의 한글·배치를 확인했다.

로컬 기록은 `tests/results/ocr-geometry/local-python-tests.log`다. 당시 실제 GPU 추론·Docker 빌드는 수행하지 않았다. 이후 서버에서 도구 26개 OK, parser-tests 빌드와 refinement-job 자체 검사·빌드, OCR 5개 및 보존 2개 통과를 확인했다. 첨부 원문은 `tests/results/ocr-geometry/server-geometry-20260915-010951.log`에 보존했다. 새 CPU Job 이미지 `sha256:6b59f315dc97a0838d09225bb22bca4b4c68b4cc1f7217e917464a4d41b1544d`가 7개 canonical 메타데이터와 모두 일치한다.

이번 판단은 업로드한 콘솔 로그에 근거하며 서버 report.json·canonical 전체를 직접 읽은 것은 아니다. 서버 도구 26개와 이전 로컬 84개는 중복되므로 합산하지 않는다. 회전/crop 검사가 전체 운영 문서의 인식 품질이나 crop 지원 완료를 뜻하지 않는다. 이후 대표 운영 문서의 품질 점검과 필요한 좌표 지원 보완을 진행한다.
