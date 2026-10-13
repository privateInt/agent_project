# 실제 문서 품질 점검 정답

`ai-engineer-sample.json`은 사용자가 제공한 `AI_Engineer_sample.pdf`의 렌더링 화면을 대조해 작성한 작은 정답 fixture다. PDF/실행 결과 사본은 이 폴더에 저장하지 않는다. 원본 SHA-256은 fixture에 고정되어 있다.

- 물리 3페이지: 그림 안 라벨 24개. 화살표 관계/의미 순서는 평가하지 않는다.
- 물리 4페이지: 합쳐진 10열 표에서 공정 구분 2개와 항목·수량·단위 관계 11개를 골랐다. 전체 표 정확도 점수가 아니다.
- 물리 6페이지: Table 2의 머리글과 데이터 25행, 셀 85개 전체의 값/위치/병합. 화학식 하첨자는 일반 텍스트 숫자로 전사했다.

정답은 OCR 결과에서 자동 생성하지 않는다. 원본의 오탈자를 임의 교정하지 않고 `Toogling`, `Shaving` 등 실제 표기를 따른다. 숫자 지수 부호를 보존한다. 공백과 NFC만 정규화한다. 다른 PDF나 변경된 원본은 같은 fixture로 평가하지 않는다.

실행: `python -B tests/test_document_quality_audit.py -v`, 실제 파일 검사는 [최종 점검](../../../../docs/data-platform/testing-acceptance-20260917.md)의 명령을 따른다. 알려진 결함이 있는 현재 결과의 품질 검사 종료 코드는 1이며 검사 도구의 단위 테스트 실패와 다르다.

## 영역 실험

`regions.json`은 같은 원본 SHA에만 쓰는 수동 지정 페이지/영역 좌표다. 원본 픽셀의 잘라내기/배치를 시험하기 위한 벤치마크이며 자동 영역 탐지 기능이 아니다. 공정명·셀 정답을 모델 입력에 넣지 않는다. `live_document_regions.py`는 파생 PNG 세 개를 기존 정제 API로 처리한다. 실행·재개·결과 범위는 `docs/data-platform/testing-ocr-quality.md#region-experiments`에 설명한다. 원본/실제 결과·이미지를 이 fixture 디렉터리에 저장하지 않는다.


`raster-reference.json`은 물리 4페이지의 데이터 행 41개를 원본 픽셀에서 작성한 공정/투입·산출/항목/수량/단위 정답이다. 영역 좌표는 담지 않는다. 오른쪽 선행 3행에는 번호가 보이지 않아 빈 공정으로 두며, 자동 탐지나 모델 입력에는 이 정답 문구를 쓰지 않는다. `raster_quality.py`가 구조·중복·추가 공정까지 검사한다. 자동 영역 구현/한계는 docs/data-platform/testing-ocr-quality.md#region-experiments를 따른다.
