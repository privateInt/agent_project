# CPU 정제 검사·진단

원본 ID → 독립 정제 요청 → CPU Job → 검증된 canonical JSON의 경로를 확인한다. 기존 TXT·DOCX·XLSX 서버 smoke와 UID 65532 자체 검사는 통과했다. 완료 범위는 [Data 상태](status.md), 형식·품질 계약은 [정제 API](api-refinement.md)를 따른다. 현재 소스에 런타임 수정이 포함되어 있으므로 예전 패치를 다시 적용하지 않는다.

## Python 도구 검사

```bash
cd ~/workspace/agent_project/data_platform
python tests/test_collection_tools.py -v
python tests/test_upload_tools.py -v
python tests/test_refinement_tools.py -v
```

## 파서·Go·PostgreSQL 검증

```bash
cd ~/workspace/agent_project/data_platform
python ops/verify_refinement.py
```

이 도구는 다음을 수행한다.

1. 기존 Data Compose에서 연결 설정을 읽는다. 비밀 값을 출력하지 않는다.
2. Docker `parser-tests` target에서 TXT/MD/HTML/DOCX/XLSX/PDF/이미지 파서 테스트를 실행한다.
3. Docker `builder` target으로 전체 Go 코드를 빌드하고 `go test -count=1 -json ./...`를 실행한다.
4. PostgreSQL 테스트는 기존 `DATA_POSTGRES_URL`이 가리키는 서버의 임시 schema를 만들고 테스트 후 제거한다. 실제 데이터 테이블을 초기화하지 않는다.
5. 테스트 FAIL 또는 테스트 SKIP이 하나라도 있으면 실패로 종료한다. 로그는 `tests/results/refinement/go-tests.jsonl`에 저장한다.

PASS를 확인한 뒤에만 다음 단계로 진행한다. 이 도구는 상시 서비스 이미지를 전환하지 않는다. 사용 중인 PostgreSQL 역할에 임시 schema 생성 권한이 없거나 서버에 연결하지 못하면 먼저 해당 원인을 해결한다.


## 이미지 빌드와 서비스 전환

```bash
cd ~/workspace/agent_project/data_platform
(
  set -euo pipefail
  docker compose --profile jobs build refinement-job \
    data-refinement data-operation data-dispatcher data-reconciler data-gateway

  docker compose up -d --no-build --no-deps --force-recreate data-operation
  docker compose up -d --no-build --no-deps --force-recreate data-refinement
  docker compose up -d --no-build --no-deps --force-recreate \
    data-dispatcher data-reconciler data-gateway

  docker compose ps
  docker compose logs --tail=50 data-refinement data-operation data-dispatcher data-reconciler data-gateway
)
```

Collection 서비스는 기존 실행을 사용한다. `data-refinement`가 `data_refinement` DB와 `data-canonical` 버킷을 생성한다. 정제 Job 이미지는 빌드만 하며, 실제 실행은 Dispatcher가 승인된 Operation에 맞춰 생성한다. 기존 `data-collection`, 수집 Job, 모델 플랫폼을 내리거나 원본을 지우지 않는다.

새 정제 Operation이 생긴 후 구버전 Operation/Dispatcher로 일부만 되돌리면 새 실행 종류를 처리할 수 없다. 장애가 있으면 Job·결과를 보존하고 먼저 로그를 확인한다.


## runtime

Job은 `/usr/local/bin/python -I -B -u`로 격리 파서를 실행한다. Docker 빌드 중 실제 사용자 65532로 Go → Python CLI → TXT 결과 검사를 수행하며 실패하면 빌드를 완료하지 않는다. parser.py 읽기 권한과 바이너리 실행 권한을 명시한다.

파서/런타임 변경 시 빌드한 Job을 실제 제한 조건에서도 검사할 수 있다. 이 검사는 DB·Operation·MinIO에 요청하지 않는다.

```bash
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=134217728 \
  --cap-drop=ALL --security-opt no-new-privileges \
  --cpus=1 --memory=1g --pids-limit=128 \
  --entrypoint /app/refinement-job \
  data-platform-refinement-job:dev --self-test
```

stderr는 최대 8 KiB로 수집하고 고정된 reason/stage/exception/errno/line/exit_code만 보고한다. 원문·경로·환경변수·예외 메시지는 출력하지 않는다.

### 실행 실패 진단

| reason | 의미 |
|---|---|
| interpreter_missing / interpreter_permission / process_start_failed | Python 프로세스 시작 실패 |
| script_missing / script_permission / script_syntax_error | 파서 스크립트 접근·구문 문제 |
| dependency_unavailable | 모듈 로드 실패 |
| permission_denied / file_missing / output_exists | stage가 가리키는 로컬 입출력 문제 |
| invalid_spec / invalid_utf8 / parse_rejected | 입력 명세·UTF-8·파서 형식/한도 검증 실패 |
| timeout / cancelled / memory_error | 시간·취소·메모리 예외 |
| internal_error / process_failed | 그 외 실패. exception과 line으로 해당 코드를 확인 |

자체 검사에서 실패하면 해당 출력을 먼저 확인한다. 실제 smoke만 실패하면 새 보고서의 Operation 상세와 `parser_process_failed` 로그를 확인한다. 기존처럼 모든 실패가 일반적인 원본 형식 오류 문구 하나로 사라지지 않는다.


## 실제 정제 smoke

다음 명령은 작은 TXT·DOCX·XLSX를 새로 업로드하고 정제한다. 이미 있는 원본을 쓰려면 실제 원본 ID로 `--document-id`를 지정한다. 과거 문서에 있던 UUID가 현재 DB에 남아 있다고 가정하지 않는다.

```bash
cd ~/workspace/agent_project/data_platform
python tests/live_refinement_smoke.py
```

원본/결과 SHA-256·크기·출처·블록 수, 멱등 재전송·변경 입력 409, 실행 image ID와 CPU Job 종료를 확인한다. 기본 TXT/XLSX는 ready, DOCX는 페이지 정보 제한으로 needs_review다. needs_ocr는 미완료 OCR을 뜻한다.

응답 유실·대기 초과는 이번 실행이 출력한 보고서로 재개한다. terminal failed를 재개해도 새 실행을 만들지 않으며 원인 해결 후 새 요청이 필요하다. 삭제한 과거 결과 경로는 재개에 사용할 수 없다.

```bash
python tests/live_refinement_smoke.py \
  --resume tests/results/refinement/실제-record-id.json
```
