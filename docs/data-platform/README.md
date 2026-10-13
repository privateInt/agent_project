# 데이터 플랫폼 문서

> 2026-10-01 코드 검토: [현재 구현과 남은 작업](../status.md)을 함께 확인한다. 아래 날짜별 결과와 검증 한계는 당시 근거로 보존한다.

수집은 원본을 저장하고, 정제는 원본 ID로 문서 내용을 읽어 공통 JSON을 만든다. PDF·이미지에서 필요한 페이지는 모델 플랫폼의 기존 Surya 배포로 OCR한다. 화면은 **자료 목록 / 결과 확인**으로 구성한다. 자료 목록에서 웹/업로드 파일을 가져오고 각 행의 내용 정리·원본 다운로드를 사용한다.

| 목적 | 문서 |
|---|---|
| 완료 범위·다음 작업 | [현재 상태](status.md), [세션 인수인계](../HANDOFF_NEXT_SESSION.md) |
| 최신 실제 샘플의 그림·표·시간 | [2026-09-17 최종 점검](testing-acceptance-20260917.md) |
| 수동 crop·픽셀 후보·공정 연결 검사 | [영역 실험과 판정](testing-ocr-quality.md#region-experiments) |
| 테스트 파일 보관·정리 | [결과 예산·입력자료·재개 제한](../testing/artifact-retention.md) |
| 자료 그룹 생성·이름 변경·추가/제외 | [그룹 사용법·API](api-groups.md) |
| 사용자 화면·파일 업로드·다운로드 | [화면 사용법](testing-console.md) |
| 서비스 경계·저장·통신 | [구조](architecture.md), [기존 코드 재사용](architecture.md#reuse) |
| HTTP/Chromium 수집·원본/작업 조회 | [수집 API](api-collection.md) |
| 파일 업로드·형식 검사 | [업로드 API](api-upload.md) |
| 원본 ID 기반 parsing·공통 JSON·품질 상태 | [정제 API](api-refinement.md) |
| PDF 표 OCR 우선·내부 후보/근거 | [표 선택 규칙](table-comparison.md) |
| 모델 식별·선택적 OCR·좌표 | [OCR 계약](api-ocr.md) |
| 최초 연결·HTTP 수집 검사 | [수집 설치·검증](testing-collection.md) |
| 연결 페이지·첨부 수집 | [HTTP 범위 검사](testing-collection-scope.md) |
| 동적 페이지·초기 응답 보존 | [브라우저 수집 검사](testing-browser-collection.md) |
| 파일 업로드 검사 | [업로드 검사](testing-upload.md) |
| CPU 파서·Job 런타임·진단 | [정제 검사](testing-refinement.md) |
| Surya 배포·OCR·모델 다운로드 오류 | [OCR 검사·진단](testing-ocr.md) |
| 한국어·표·읽기 순서 | [OCR 품질 검사](testing-ocr-quality.md) |
| 회전·crop·JPEG | [좌표 검사](testing-ocr-geometry.md) |
| 의도적으로 문제가 있는 PDF | [원본 OCR 전환 검사](testing-ocr.md#pdf-fallback) |
| 이전 결과·변경 이유 | [검증 요약](../testing/results-summary-20260916.md), [변경 이력](../history/data-platform-202609.md) |

검증 절차는 현재 소스를 기준으로 필요한 범위만 실행한다. 예전 패치 압축파일을 순서대로 적용할 필요가 없다. 실제 검증 상태와 절차의 존재를 구분한다.
