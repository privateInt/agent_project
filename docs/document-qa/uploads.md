# Q03a — 문서 업로드와 작업 접수

2026-10-07: 접수 API·자기 소유 저장·Data 업로드 adapter와 GPU 없는 검사 41개를 구현했다.
[검증 결과](../testing/document-qa-q03a-offline-20261007.json)는 임시 DB와 loopback 대역 결과다.
서버 applied/written=18·41개 검사(5.882초)·동일 자료함 init/status는
[사용자 출력](../testing/document-qa-q03a-server-observed-20261007.json)으로 확인했다.
Q03a 최초 적용 때 실제 Data 업로드는 미확인이었다. 이후 기존 TXT job의 Data HTTP 200·실제 CPU 정제 인수는 [현재 상태](../status.md)에서 확인한다. 신규 업로드 전체 경로 인수와 구분한다.

업로드 접수와 검색 준비를 구분한다. 원본 등록 후에도 `preparation_pending`이며
`question_ready=false`다. Q03a 단계의 workflow_started는 false이며
[Q03b 전달 adapter](workflows.md)는 확정된 진입 receipt를 별도로 표시한다.
GPU가 없어도 업로드를 접수할 수 있고, 모델 준비 확인·실제 추론은 사용자 결정에 따라 보류한다.

## 실행 범위

Python 3.10 이상/표준 라이브러리를 사용한다. 패치 설치와 검사만으로 서버가 기동하지 않는다.

```bash
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_uploads.py' -q
python3 -B -m apps.document_qa.api init
python3 -B -m apps.document_qa.api status
```

init은 자기 저장만 만든다. 기본 위치는
`$HOME/.local/state/agent-project-document-qa/uploads/`이며 프로젝트 밖에 둔다.
자료함 하나·schema 1 DB `uploads.sqlite3`·API bearer `api-token`·lock 파일을 생성한다.
디렉터리는 0700, DB/키/lock은 0600이다. 키 값은 출력하지 않는다. 기존 플랫폼/Ops 설정이나
외부 키를 수정하지 않는다. init 재실행은 기존 자료함·키·작업을 유지하고 이름을 바꾸지 않는다.
status는 로컬 조회만 수행하며, 저장이 없으면 임의로 새 자료함을 만들지 않는다.

내부 API를 수동으로 확인할 때만 다음 명령을 별도 터미널에서 실행한다.

```bash
python3 -B -m apps.document_qa.api serve
```

기본 bind는 127.0.0.1:17110이다. 기존 Ops의 17100 포트와 분리했다. `--port`로 포트, `--state-dir`로 자기 저장 위치를
지정한다. 외부 공개/서비스 등록/Compose 추가는 이번 단계에 없다. 기존 플랫폼 서비스의
재시작은 필요 없다. 준비 상태가 false여도 Data 연결 설정만 있으면 업로드는 접수한다.

serve는 Q02와 같은 `--settings`/`--connections-from`을 사용한다. 설정의 접속 참조만
읽고, Data 요청에 필요한 경우 해당 bearer를 읽는다. `/api/readiness`를 실제 호출할 때에만
Q02 GET 조회를 수행한다. 서버 시작 자체로 모델/플랫폼 조회·추론·workflow를 실행하지 않는다.
캡처한 접속 설정은 해당 서버 실행 동안 고정되며, 운영 설정 변경은 별도로 반영한다.

## API와 표시 상태

업로드/운영 API는 QA 전용 bearer 인증이 필요하다. Q03b callback은 별도 scoped bearer를 사용한다. 일반 사용자의 입력은 향후 Q07 화면에서
문서와 이름으로 받는다. 요청 UUID·자료함/job ID는 화면 내부에서 다루며 사용자 필수 입력으로
노출하지 않는다. multipart는 `name`과 `file` 두 필드만 받는다.

| API | 역할 |
|---|---|
| POST /api/uploads | 파일·이름·Idempotency-Key UUID를 받아 자기 job 저장 후 Data에 한 번 전달. 자기 접수의 202 반환 |
| GET /api/upload-requests/{request_key} | 최초 응답 유실 후 원래 사용자 요청 key로 job 확인 |
| GET /api/jobs, GET /api/jobs/{job_id} | 목록과 단계/원본 ID/오류 조회. 문서 본문·키·접속 origin은 제외 |
| GET /api/library | 자료함 ID·접수 sequence·예약 문서 수·임시 원본 byte 수 확인 |
| POST /api/jobs/{job_id}/retry-upload | JSON `{}`로 명시적 한 번 재확인. 기존 Data key/이름/filename/bytes 유지 |
| GET /api/readiness | Q02 inspect를 재사용. defaults_ready와 질문 가능을 구분 |

| 상태 | 의미 | 다음 동작 |
|---|---|---|
| upload_accepted | 자기 job·원본·Data 요청 의도가 저장됐고 아직 Data 전달 전 | 최초 전달. 중단 후 같은 job으로 확인 |
| upload_confirmation_required | 전달 시도 이후 등록 결과가 불확실함 | 원래 intent 보존. 완료된 전송/접수를 모른 채 새 key를 만들지 않고 명시적 재확인 |
| preparation_pending | 일치하는 Data ready 원본 receipt를 확인했고 임시 원본을 비움 | Q03b workflow 전달 대기. 아직 검색/질문 준비 완료가 아님 |

Data 업로드는 동기 200이며 receipt는 `document_id`다. QA의 202/job과 혼동하지 않는다.
name·original_filename·SHA256·size·source_type=upload·ready와 유효한 원본 ID를 검사한다.
불일치/잘못된 응답/timeout/권한 오류는 원본을 비우거나 검색 성공으로 처리하지 않는다.
원격 오류 본문과 키 값은 출력하지 않는다.

## 중복·중단·용량

SQLite 트랜잭션에서 예약과 job/key를 함께 저장한다. 준비 완료와 처리 중 자료를 합해
최대 20문서를 예약한다. 21번째는 저장/전달 전에 거부한다. 하나의 파일은 최대 32MiB이며
multipart 전체는 32MiB+64KiB 이내다. 전체 파싱과 초기 형식/UTF-8 검사 후 접수하므로
잘못된 뒤쪽 필드가 원본 등록을 일으키지 않는다. PDF signature는 추후 본문 추출 성공의
보장이 아니다. 정제 본문/전체 canonical 32MiB와 청크 한도는 Q04/Q05에서 검사한다.

같은 사용자 key와 같은 정규화된 이름/filename/SHA/size는 같은 job이다. 같은 key의
내용 변경은 409다. 새로운 사용자 key여도 같은 이름/filename/내용이면 기존 job의 alias로
저장해 중복 자료를 만들지 않는다. 이름 또는 filename이 다르면 별도 자료로 접수한다.
alias는 job당 100개까지이며 기존 key 조회/재전송은 계속 허용한다.

Data 요청 key는 job/sequence/단계의 Q01 intent에서 만들고 저장한다. 동일 job의 전송은
OS file lock으로 직렬화한다. 원격 I/O 동안 DB 트랜잭션은 잡지 않는다. 프로세스가 종료되면
lock은 해제되고, 이미 저장한 원본·key·불확실 상태로 확인할 수 있다. 기본 중복 POST는
불확실했던 Data 전송을 자동 반복하지 않는다. retry-upload는 같은 intent를 명시적으로
한 번 전송하며 100번을 넘으면 별도 검토를 요구한다. exactly-once 실행을 주장하지 않는다.

Data origin은 최초 intent에 고정한다. 연결 주소가 달라졌을 때 미확인 요청을 다른 Data로
재전송하지 않고 UPLOAD_TARGET_CHANGED로 거부한다. 이식/주소 변경 전에 미확인 접수의
receipt와 원래 대상을 확인해야 하며 자동 새 key/새 원본 생성으로 해소하지 않는다.

Data receipt가 확정되기 전의 원본만 retry envelope로 SQLite에 임시 보관한다. 최악의
미확인 원본 합계는 640MiB이며 DB metadata와 쓰기 중 rollback journal 공간은 별도다.
확정 receipt와 BLOB 비우기를 한 트랜잭션으로 처리하고 FULL auto-vacuum으로 DB 페이지를
반환한다. 이미 등록된 원본의 장기 복제·프로젝트 내 업로드 파일/패치 백업은 만들지 않는다.
이를 디스크/백업에서의 물리적 완전 삭제 보장으로 해석하지 않는다.

LOCAL_STATE_UNCERTAIN이면 원래 사용자 key로 조회한 뒤 상태를 판단한다. 이미 commit됐을
가능성을 무시하고 다른 key로 처음부터 접수하지 않는다. 초기화 중 남은 init.sqlite3는
STORE_INIT_INCOMPLETE로 보존한다. 실행 중인 init과 파일을 확인한 뒤 미완료 자기 초기화만
처리하며 기존 uploads.sqlite3/작업/키를 일괄 삭제하지 않는다.

## 검증과 배포 경계

41개 검사는 실제 임시 SQLite·동시 접수·자식 프로세스 강제 종료·loopback Data/QA HTTP로
접수/receipt/파일 보존·메모리 외 작업 보존을 확인한다. 32MiB 등록 확인 후 DB 페이지가
반환되는 사례도 포함한다. 임시 파일은 검사 종료 후 지운다. 운영 API·모델·GPU·Ops·workflow는
호출하지 않는다. 이미 통과한 Q01/Q02/플랫폼 전체 검사는 반복하지 않는다.

HTTP adapter의 operator 요청은 4개다. Q03b 연결 시 callback을 포함한 최대 연결은 8개다. 본문 idle timeout 30초/읽기 budget 60초·Data 요청 timeout 125초·
receipt 64KiB로 제한한다. 이 한도는 OS DNS까지 강제로 중단하는 실행기를 뜻하지 않는다.
운영 LAN UI/TLS/reverse proxy/서비스 등록은 Q07 배포 때 별도로 인수한다.

자기 SQLite와 file lock은 현재 Linux 단일 호스트·단일 저장 볼륨용 adapter다. Kubernetes에
핵심 API/workflow 계약을 유지해서 옮길 수 있지만, 이번에 Kubernetes 인수를 완료한 것은
아니다. 초기 이식은 단일 replica와 전용 지속 볼륨을 확인해야 한다. 공유 filesystem이나
여러 replica의 동시 사용은 지원으로 표시하지 않으며 필요할 때 저장/실행권 adapter를
교체한다. Data/Knowledge/Agent DB와 MinIO/OpenSearch 직접 접근은 없다.

[코드](../../apps/document_qa/README.md), [API 계약과 코드 hash](../../apps/document_qa/upload-contract.json),
[전체 작업계획서](../plans/document-qa-work-plan-20261002.md)를 따른다.
