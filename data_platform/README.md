# Data Platform

웹페이지·첨부·업로드 원본을 저장하고, 별도 정제 요청에서 parsing/OCR을 수행해 canonical JSON과 Markdown을 제공한다. 수집과 정제는 독립 요청이다. 자료 그룹과 원본/결과 조회·다운로드·삭제를 제공한다.

OCR 연결과 일부 문서 인식은 확인됐지만 어려운 표/그림의 품질 한계는 남는다. `needs_review`·`needs_ocr`와 읽힌 내용을 보존한다. 최신 판단은 [Data 상태](../docs/data-platform/status.md)를 따른다.

- [화면/기동](../docs/data-platform/testing-console.md)
- [자료 그룹](../docs/data-platform/api-groups.md)
- [구조·API·검증 목차](../docs/data-platform/README.md)
- [대표 샘플 품질 점검](../docs/data-platform/testing-acceptance-20260917.md)

접속 호스트와 포트는 기존 Compose/환경 설정을 따른다. 과거 서버 IP를 모든 설치의 기본 주소로 사용하지 않는다.
