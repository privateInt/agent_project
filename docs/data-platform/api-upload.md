# 파일 업로드·원본 등록 API

`POST /api/source-documents`는 파일 1개를 받아 원본 버전으로 등록한다. 기본 주소는 `http://127.0.0.1:14000`이다. 정상 업로드에는 Kafka·Operation·one-shot 컨테이너가 필요하지 않다. 응답의 `document_id`가 이후 정제에서 고정해서 사용할 원본 버전 ID다.

## 요청과 응답

multipart/form-data에 `name` 텍스트 필드와 `file` 파일 필드를 각각 한 번 보낸다. 순서는 무관하며 다른 필드·중복 필드를 거절한다. `Idempotency-Key` 헤더가 필수다.

```bash
curl -fsS 'http://127.0.0.1:14000/api/source-documents' \
  -H 'Idempotency-Key: upload-document-example-001' \
  --form-string 'name=회의 문서' \
  -F 'file=@/absolute/path/meeting.docx' |
  jq
```

위 파일 경로는 실제 파일로 바꾼다. 등록 이름은 앞뒤 공백을 제거한 1~200자, 파일명은 경로 구분자·제어문자가 없는 1~255 UTF-8 바이트다. 파일명은 표시용 메타데이터로만 저장하고 로컬 경로나 객체 키로 사용하지 않는다.

처음 등록과 동일 요청 재전송 모두 **HTTP 200**이다. 업로드 완료 응답은 기존 원본 상세 응답과 같은 형태다.

```json
{
  "document_id": "UUID",
  "name": "회의 문서",
  "source_type": "upload",
  "original_filename": "meeting.docx",
  "format": "docx",
  "format_version": "source-format-v2",
  "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "state": "ready",
  "size_bytes": 12345,
  "sha256": "64자리 소문자 SHA-256",
  "artifact_uri": "s3://data-sources/uploads/키해시/sha256/내용해시/original",
  "crawl_run_id": "",
  "source_url": "",
  "final_url": "",
  "processor_version": "upload-source-v1",
  "processor_image_id": "",
  "created_at": "2026-09-11T00:00:00Z"
}
```

값은 응답 형태를 설명하는 예시다. `ready`는 원본 저장·검증 완료를 뜻하며, 정제나 OCR 완료를 뜻하지 않는다.

## 지원 형식과 검사 범위

파일은 **1바이트~32 MiB**, 전체 HTTP 요청은 파일 최대 크기 + 64 KiB까지다. Gateway와 Collection은 각각 업로드를 동시에 2개까지 처리하고 초과하면 429를 반환한다. multipart가 선언한 MIME 대신 확장자와 파일 내용을 검사하여 `content_type`을 기록한다. 원본의 공백·줄바꿈·압축 내용은 바꾸지 않는다.

| 확장자 | format | 등록 시 확인 |
|---|---|---|
| `.txt` | text | UTF-8, 바이너리 제어문자 제외 |
| `.md`, `.markdown` | markdown | UTF-8 텍스트 |
| `.html`, `.htm` | html | UTF-8, 기본 마크업 표시 존재 |
| `.pdf` | pdf | 최대 1,024바이트의 공백/BOM 뒤 PDF 식별자. EOF/xref 완전성은 정제에서 처리 |
| `.png` | png | 이미지 헤더·크기·끝 표시 |
| `.jpg`, `.jpeg` | jpeg | 이미지 헤더·크기·끝 표시 |
| `.docx` | docx | ZIP CRC·크기, OOXML 문서 타입·본문 루트·패키지 관계 |
| `.xlsx` | xlsx | ZIP CRC·크기, OOXML 통합문서 타입·본문 루트·패키지 관계 |

PDF는 불완전한 trailer나 텍스트 파싱 불가를 업로드 거절 사유로 삼지 않는다. 해당 원본은 그대로 저장하고 정제에서 파싱·페이지 렌더링·OCR 여부를 판단한다. [문제 PDF의 원본 OCR 전환](testing-ocr.md#pdf-fallback). 기존 v1 원본과 재전송 응답은 유지한다.

PDF·이미지의 전체 디코딩, 암호화 PDF의 복호화, HTML 파싱 품질은 이 등록 단계에서 보장하지 않는다. 이미지 헤더의 가로×세로는 1억 픽셀까지다. TXT/MD/HTML은 UTF-8만 지원한다. Office 패키지의 필수 XML은 Go 기본 XML 디코더가 처리하는 UTF-8/ASCII 범위다.

DOCX/XLSX는 압축된 원본 32 MiB 외에 압축 해제 합계 128 MiB·개별 항목 32 MiB·항목 4096개 제한이 있다. `[Content_Types].xml`과 `_rels/.rels`는 각각 1 MiB까지다. 패키지 중복/상위 경로·비정상 파일 항목·암호화 ZIP·매크로 형식·필수 XML의 DTD/과도한 중첩을 거절한다. 디스크에 압축을 풀거나 외부 관계 URL을 따라가지 않는다. 암호화 Office, `.doc`, `.xls`, `.docm`, `.xlsm`, HWP는 이번 업로드 범위에 없다.

## 재요청과 실패

같은 키에 정규화된 이름·원래 파일명·원본 바이트가 같으면 저장된 응답을 반환한다. 내용이 같아도 이름이나 파일명을 바꾸면 같은 키를 재사용할 수 없다. 별도 등록을 의도한 경우 새 키를 사용한다.

| HTTP | 의미 | 다음 행동 |
|---|---|---|
| 200 | 원본 등록 또는 동일 요청 재전송 완료 | document_id로 조회·다운로드 |
| 400 | 필드/키/형식/파일명 오류, 크기 초과, 전송 잘림 | 입력 수정; 응답 내용을 확인 |
| 409 | 같은 키에 다른 이름·파일명·내용 | 원래 입력으로 확인하거나 별도 등록에는 새 키 사용 |
| 429 | 업로드 동시 처리 제한 | 잠시 뒤 같은 키·같은 파일로 재요청 |
| 502 | 저장된 바이트와 해시·크기 불일치 | 스토리지 상태 확인 후 같은 요청 재전송 |
| 503/504 | 의존 서비스 접근 또는 처리 시간 문제 | 같은 요청으로 완료 여부 확인 |

서버가 응답하기 전에 연결이 끊겨도 DB 커밋이 끝났을 수 있다. 이때 새 키를 생성하지 말고 같은 요청을 재전송한다. 메타데이터 예약·문서 행·재전송 응답은 한 DB 트랜잭션으로 저장한다. MinIO 쓰기 후 DB 실패 시 미등록 객체가 남을 수 있지만, 같은 키·같은 내용의 재시도는 같은 객체 키를 사용한다. 자동 삭제·GC는 후속 범위다.

## 수집 문서와 함께 사용

- 목록: `GET /api/source-documents?q=...`; 이름·URL·원래 파일명 부분 검색과 기존 cursor 사용.
- 상세: `GET /api/source-documents/{document_id}`.
- 다운로드: `GET /api/source-documents/{document_id}/content`; 원본 해시 검증 후 attachment로 반환.
- 업로드는 원래 파일명을 다운로드 이름으로 제공하며, 수집 문서는 기존 UUID 기반 `.bin` 이름을 유지한다.
- 기존 수집 문서는 `source_type=crawl`, 기존 URL·crawl_run_id·Job 이미지 ID를 유지한다. 새 `original_filename`, `format`, `format_version`은 빈 문자열이며 HTTP Content-Type을 형식 검사 결과로 바꾸지 않는다.

정제 API가 원본을 사용할 때는 `document_id`·해시를 입력으로 고정하고 파일을 읽는다. 이번 단계는 정제를 자동으로 시작하지 않는다.

구현 참고: [Go multipart](https://pkg.go.dev/mime/multipart), [Go ZIP](https://pkg.go.dev/archive/zip), [gRPC Go 클라이언트 스트리밍](https://grpc.io/docs/languages/go/generated-code/). ZIP 내부 검증은 Go 라이브러리를 사용하며, 경로·크기·중복·Office 문서 타입 제한은 프로젝트에서 별도로 적용한다.
