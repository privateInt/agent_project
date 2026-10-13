# Q06b-1 질문 대상 저장과 호환 migration

2026-10-08. 기존 자기 SQLite에 질문 head/revision과 변경 불가능한 대상 참조를 추가한다. 서버 코드 적용·관련 검사·migrate/status까지 확인했다. 완료 수치는 [상태표](../status.md), 다음 Agent adapter/CAS 단계는 [작업계획서](../plans/document-qa-work-plan-20261002.md)를 따른다.

## revision의 기준과 구버전 호환

기존 업로드 코드는 `PRAGMA user_version=1`과 `library.question_revision=0`을 전제로 한다. 기존 테이블을 재작성하거나 전역 버전을 올리면 구버전 업로드/Flow 코드가 저장소를 거부할 수 있다. 이번 migration은 기존 테이블·제약·데이터를 유지하고 별도 확장 테이블을 추가한다.

| 저장 값 | 역할 |
|---|---|
| `PRAGMA user_version=1` | 기존 업로드 저장 schema. 변경하지 않는다 |
| `library.question_revision=0` | 구버전 API의 호환 값. 실제 질문 revision으로 사용하지 않는다 |
| `question_metadata.version=1` | 질문 저장 확장의 schema 버전과 migration 정의 hash |
| `question_head.revision` | 새 질문 코드가 사용할 실제 revision. 최초 0/대상 없음 |
| `question_targets` | 고정된 문서·색인·모델·Connection·Tool/Agent 참조와 내용 hash |

Q05의 `indexing_generation`은 질문 revision과 독립적이다. 기존 `/api/library`는 계속 revision 0·question_ready=false를 반환한다. 후속 질문 코드는 `QuestionStore.head()`를 읽어야 하며 구버전 API의 호환 값을 질문 대상 전환에 사용하면 안 된다. 이번에는 기존 API/Flow를 수정하거나 질문 가능 상태를 활성화하지 않는다.

## migration과 중단 복구

`QuestionStore.migrate(uploads)`는 Q06a까지 초기화된 기존 `UploadStore`를 요구한다. 저장소가 없으면 새 DB/키를 만들지 않는다. 기존 전역 schema·자료함 신원·호환 revision을 확인한 뒤 전용 파일 잠금과 SQLite `BEGIN IMMEDIATE`를 사용한다.

세 테이블·세 trigger 생성, metadata와 빈 head 삽입을 한 트랜잭션에서 수행한다. `executescript`의 중간 commit을 사용하지 않는다. 오류·KeyboardInterrupt·프로세스 강제 종료가 commit 전에 발생하면 새 schema 전체가 되돌아간다. commit 후 출력이 유실됐으면 재실행으로 이미 완성된 schema를 확인한다. schema/metadata 정의가 다르거나 일부만 존재하면 초기화·수선·삭제하지 않고 거부한다.

재실행은 기존 head/revision·후보·이력을 덮어쓰지 않는다. migration과 후보 저장은 같은 잠금을 사용하며 경쟁 호출은 `QUESTION_STORE_BUSY`로 끝난다. 기존 업로드의 쓰기는 SQLite 트랜잭션으로 직렬화되어 migration 뒤에도 저장된다. migration 중 조회는 commit된 상태만 본다.

기존 library/job·원본 재시도 BLOB·요청 별칭·intent/key·receipt·Flow setup·전용 키를 보존한다. Model·Data·Knowledge·Agent·n8n을 호출하지 않으며 모델 기동/추론·서비스 재시작은 없다.

## 변경 불가능한 대상 참조

`save_target(body)`는 `question-target-v1`의 형식만 검증해 후보 참조를 저장한다. body에는 자료함/질문 revision·색인 generation·전체 canonical snapshot·index/version·embedding 배포/공간·Tool/Agent 버전과 고정 pin을 둔다. pin은 embedding/chat 모델 버전·profile·배포, Knowledge/embedding/chat Connection 버전과 Tool/Agent 정의 hash다. Q05의 문서 snapshot과 Q01의 `QuestionHead` 계약을 함께 검증한다.

같은 정규화 입력은 같은 target ID/hash와 최초 생성 시각을 재사용한다. 입력 dict를 나중에 바꿔도 저장된 값은 변하지 않는다. 문서 순서만 다른 입력도 같은 대상을 사용한다. 내용 변경은 새 대상이며 기존 대상을 UPDATE/DELETE하는 SQL은 trigger가 거부한다. 읽을 때 형식·자료함·ID·hash·정규화 내용과 head 관계를 다시 확인한다.

대상 하나는 최대 128KiB, 보존 대상은 자료함당 최대 100개다. 본문·hit text·벡터·credential을 넣지 않는다. 한도에 도달해도 기존 대상 재사용은 가능하며 새 대상 접수는 거부한다. 자동 삭제/이력 정리 정책은 이번에 추가하지 않는다.

**후보 저장은 운영 검증이나 head 활성화가 아니다.** 이번 API에는 head 전환/질문 실행 함수가 없고 CLI에도 후보 입력 명령이 없다. head는 최초 revision 0·target 없음, question_ready=false다. 양수 revision을 읽는 호환 검사는 테스트 전용 SQL fixture로만 만들었다. 운영 대상 검증과 원자 CAS 전환은 Q06b-3에서 별도 구현한다.

## 서버 로컬 확인 명령

패키지 코드 적용 뒤 기존 `sh_test` 환경과 자기 저장소에서 아래 관련 검사·migrate/status를 확인했다. 문서 변경 때문에 이를 반복하지 않는다.

```bash
python3 -B -m unittest discover -s src/apps/document_qa/tests -p 'test_question_store.py' -q
python3 -B -m src.apps.document_qa.question_store migrate
python3 -B -m src.apps.document_qa.question_store status
```

최초 출력은 `question_storage_schema=1`, `upload_schema=1`, `head_revision=0`, `head_target_id=null`, `target_count=0`, `question_ready=false`다. `production_api_requests=0`, `GPU_requested=false`를 유지한다. `status`는 schema를 생성하지 않는다. 사용 가능한 Connection/Agent/실제 색인이 없어도 이 로컬 migration 인수는 가능하다.

| 오류 | 의미 |
|---|---|
| `STORE_NOT_INITIALIZED` / `SEARCH_TOOL_NOT_INITIALIZED` | 앞 단계의 기존 저장 초기화가 없음. 새 저장소로 대체하지 않는다 |
| `QUESTION_NOT_INITIALIZED` | 질문 확장 migration 전의 조회 |
| `QUESTION_STORE_BUSY` | migration/후보 저장이 진행 중. 완료 뒤 명시적으로 재실행 |
| `QUESTION_SCHEMA_INVALID` / `QUESTION_LEGACY_SCHEMA_INVALID` | 다른 버전·부분 schema·잘못된 기존 상태. 원문 검토 필요 |
| `QUESTION_HEAD_INVALID` / `QUESTION_TARGET_CORRUPT` | 저장된 신원/관계/hash 불일치. 삭제·reset으로 해결하지 않는다 |

## 코드 복귀 범위

전달 패키지의 `apply_patch.py --rollback`은 코드/문서만 원복하고 질문 테이블·대상·이력·DB·키는 삭제하지 않는다. 구버전 Upload/Workflow/Refinement/Indexing/SearchTool 코드는 추가 schema를 무시하며 기존 저장 값 0/false로 동작한다. migration 전후와 테스트 전용 양수 head 상태에서도 구버전 초기화·업로드/receipt 저장·HTTP 자료함 조회가 유지되는 것을 검사했다.

이는 후속 질문 API/Agent/CAS 버전까지 임의로 원복할 수 있다는 뜻이 아니다. 이후 DB/코드 계약이 바뀌면 해당 단계에서 복귀 경계를 다시 인수한다. 재적용한 Q06b-1 migration은 보존된 질문 저장을 재사용한다.

근거: [서버 로컬 인수](../testing/document-qa-q06b1-server-observed-20261008.json), [개발 환경 검사](../testing/document-qa-q06b1-offline-20261008.json). Q06b-2 Agent adapter·Q06b-3 CAS·운영 색인/Tool/Agent·질문 품질은 별도 인수다.
