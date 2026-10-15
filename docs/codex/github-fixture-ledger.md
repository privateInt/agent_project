# 고정 진단 assignment·artifact 원장

이 단위는 [수집기](github-fixture-collector.md)의 실제 artifact bytes를 SQLite에 한 번 결합하고, 응답 유실 뒤 같은 job을 조회·재전송하는 저장 component다. [서명 계약](github-signed-receipts.md)의 보호된 controller 설치·tests/reviewer 역할 배정·issuer를 구현한 상태는 아니다. [검사 계획](github-fixture-ledger-test-plan.md)과 [개발 근거](../testing/github-fixture-ledger-offline-20261015.json)를 연결한다.

## 호출 계약과 상태

공개 API는 `initialize_fixture_ledger(connection)`, `assign_fixture_job(connection, expected_assignment_sha256, assignment_bytes)`, `read_fixture_job(connection, job_id)`, `record_fixture_artifact(connection, job_id, expected_assignment_sha256, expected_revision, artifact_bytes)`다. DB 경로·SQL·argv·callback·서명키를 인수로 받거나 connection을 직접 열지 않는다. 기존 수집기의 `_assignment`·canonical JSON·고정 profile literal을 그 소스 pin의 내부 계약으로 재사용하고 기존 파일은 변경하지 않는다.

borrowed connection은 정확히 builtin `sqlite3.Connection`이고 `isolation_level=None`, `row_factory=None`, `text_factory=str`, 호출 진입 시 `in_transaction=false`여야 한다. `busy_timeout`은 0..1000ms, `synchronous`는 FULL(2) 또는 EXTRA(3), journal은 `delete` 또는 `memory`다. main 외 attached DB와 temp schema object를 거부한다. caller connection과 설정을 닫거나 바꾸지 않는다. 이미 진행 중인 caller transaction은 SQL 실행·commit·rollback 없이 거부하고 보존한다. 같은 connection을 여러 스레드가 동시에 쓰는 것은 지원하지 않는다. 경합 검사는 각 프로세스가 자기 connection을 연다.

이 API는 신뢰된 controller가 별도 전용 DB에 연 connection을 공급한다는 계약이다. 기존 callback·converter·collation·extension·connection 출처를 인증하거나 제거하지 않는다. 임의 PR worker가 connection을 공급하게 해서는 안 된다. DB 경로·owner·상위 anchor·파일 mode·journal 권한·설치 pin과 전원 장애 내구성은 이 모듈의 증명이 아니다. 파일/운영 DB를 임의로 열거나 복사하지 않으며 개발 검사는 자체 임시 DB만 쓴다.

| 상태 | 허용 동작 |
|---|---|
| DB 없음 | caller가 별도로 connection을 연다. 이 모듈은 경로를 받지 않음 |
| 빈 전용 DB | schema/application ID가 0이고 object가 없을 때만 고정 schema1을 생성 |
| ASSIGNED, revision0 | 원래 assignment bytes·SHA를 보존. artifact/hash/binding은 None |
| RECORDED, revision1 | 해당 assignment의 artifact bytes·직접 계산한 SHA·최종 binding을 보존. 이후 교체 불가 |

job ID는 기존 assignment의 ASCII1..128자이고 `mode=fixture`만 허용한다. assignment bytes는 strict bytes1..16384, 기대 digest는 소문자 hex64다. 원래 bytes를 SHA와 함께 저장하며 같은 job/동일 bytes 재전송은 기존 record를 반환한다. 의미가 같아도 공백 등 bytes가 다르면 `ASSIGNMENT_CONFLICT`다. 실패 job을 새 성공으로 교체하거나 job ID를 자동 생성·재배정하지 않는다.

`record_fixture_artifact`의 expected assignment SHA는 저장된 고정값과 일치해야 하며 expected revision은 bool이 아닌 정수0이어야 한다. schema/row를 같은 transaction에서 검증하고 revision0→1을 조건부 UPDATE한다. 이미 sealed면 원래 expected0과 완전히 같은 artifact bytes만 `ARTIFACT_REPLAYED`로 복구한다. 다른 artifact는 `ARTIFACT_CONFLICT`, 다른 expected pin/revision은 각각 `ASSIGNMENT_PIN_MISMATCH`/`REVISION_CONFLICT`다. 서로 다른 writer의 최초 결합은 한 개만 성공하며 losing writer는 winner bytes를 덮어쓰지 않는다.

원장은 실행 착수·running 상태나 exactly-once process execution을 보장하지 않는다. caller는 먼저 조회한 뒤 수집·결합한다. 저장 전 프로세스가 끊겼다면 재실행 여부는 별도 controller가 결정해야 한다. collector 준비 실패로 artifact=None인 job의 terminal 실패 저장도 후속이며, artifact를 받은 실패/부분 수집 결과는 그대로 RECORDED할 수 있다. RECORDED는 PASS나 성공 실행을 뜻하지 않는다.

## 저장·검증과 복구

고정 application ID `0x4150464C`(1095779916)와 user_version1, 정확한 단일 `fixture_jobs` table DDL만 허용한다. table은 `WITHOUT ROWID`이며 job_id primary key, assignment bytes/SHA, revision0/1과 nullable artifact bytes/SHA를 저장한다. revision0은 artifact/hash 둘 다 NULL, revision1은 둘 다 NOT NULL인 CHECK를 둔다. trigger/view/추가 index·다른 schema·혼합 application을 거부하며 기존 DB를 자동 migration/수정/삭제하지 않는다. writes는 `BEGIN IMMEDIATE`, read는 `BEGIN` snapshot 안에서 schema와 row를 확인한다. 실제 transaction을 소유한 호출만 commit/rollback하고 각 cursor를 닫는다. SQL은 소스의 고정 문장과 parameter binding만 사용한다. retry·대기 loop·연결 fallback은 없다.

schema object 조회는 최대2행, 각 type/name/table 이름은128자와 SQL4096자 이내만 반환하며 추가 object 또는 상한 초과는 거부한다. temp object 조회는1행, database 목록은3행까지만 읽어 main/temp 외 연결을 거부한다. row는 읽을 때마다 strict type/길이와 원래 assignment SHA·shape를 다시 확인한다. stored blob은 SQL에서 먼저 길이를 제한해 큰 위조 blob을 그대로 로드하지 않는다. artifact가 있으면 원래 bytes SHA와 assignment 연결을 다시 확인한다. 이 일관성 검사는 직접 DB를 쓸 수 있는 공격자의 재작성·삭제·rollback을 인증하거나 막지 않는다. 신뢰된 상태 anchor와 최신성 ledger는 별도 인수다.

artifact는 strict bytes1..16384이고 수집기의 canonical ASCII JSON exact11키 및 nested outcome8/stream6/limits2를 요구한다. job/target6/assignment SHA/profile/limits/고정 literal digest는 assignment와 일치해야 한다. strict bool/int·cap/cap+1·EOF/prefix·실제 exit 표기와 reason의 내부 일관성을 확인한다. 완전 수집은 두 EOF·reap·cap/timeout 없음과 일치해야 하고, 부분 수집은 prefix로 표시한다. empty prefix digest는 빈 bytes SHA와 대조하지만 그 외 raw stream은 없어 count/hash를 재구성하지 않는다. 입력 artifact 자체의 출처·실제 invocation·내용 진실성은 인증하지 않는다.

DB 오류나 commit 결과 불명은 `DATABASE_UNCONFIRMED`/record=None으로 닫는다. commit이 실제로 끝난 뒤 응답만 유실될 수 있으므로 오류를 rollback 성공 또는 쓰기 없음으로 단정하지 않는다. 다음 독립 호출에서 같은 job을 조회하고 원래 bytes로 재전송해 결과를 확인한다. 자동으로 새 job을 만들거나 성공으로 바꾸지 않는다. rollback 실패 등으로 `transaction_closed=false`면 caller가 해당 connection을 폐기·재개 판단해야 한다. report를 내기 위해 caller transaction을 추가 정리하지 않는다.

## 반환과 한계

반환 exact2는 `{report, record}`다. record는 None 또는 exact7키 `job_id,revision,assignment_bytes,assignment_sha256,artifact_bytes,artifact_sha256,binding`이다. final binding은 assignment target6에 실제 stored artifact bytes의 SHA를 붙인다. raw assignment/artifact는 caller의 저장/복구용 반환이며 stdout/stderr로 출력하지 않는다.

report exact19키는 `scope,status,reason,operation_confirmed,assignment_digest_verified,artifact_digest_verified,revision,transaction_closed,fixture_only,head_code_executed`와 `input_origin_verified,signer_roster_origin_verified,issuer_environment_verified,runtime_isolation_verified,independent_review_verified,trusted_gate_verified,binding_verified,native_ready,merge_authorized`다. scope=`FIXTURE_LEDGER_ONLY`, status=`OBSERVED/BLOCKED`, revision은 strict int0/1 또는 None이다. 성공하는 저장/조회만 operation_confirmed=true이며 마지막9 신뢰 flag는 항상 false, fixture_only=true/head_code_executed=false다. hash 검증은 bytes 일관성일 뿐 출처 인증이 아니다. report에 job/target/hash/path/SQL/raw exception을 노출하지 않는다.

reason은 `LEDGER_READY`, `ASSIGNED`, `ASSIGNMENT_REPLAYED`, `ARTIFACT_RECORDED`, `ARTIFACT_REPLAYED`, `JOB_OBSERVED`, `ARGUMENTS_INVALID`, `CONNECTION_INVALID`, `SCHEMA_INVALID`, `ASSIGNMENT_INVALID`, `ASSIGNMENT_HASH_MISMATCH`, `REAL_EXECUTION_BLOCKED`, `JOB_NOT_FOUND`, `ASSIGNMENT_CONFLICT`, `ASSIGNMENT_PIN_MISMATCH`, `REVISION_CONFLICT`, `ARTIFACT_INVALID`, `ARTIFACT_CONFLICT`, `ROW_INVALID`, `DATABASE_UNCONFIRMED`로 제한한다.

현재 unit은 CaaS의 실제 SQLite 저장·경합·복구 component 검증이며 사용자 서버의 보호된 설치/전체 Python3.10 인수·실제 head worker·trusted issuer·필수 검사/병합 적용 완료가 아니다. 이전 고정 ok의 [서버 관찰](../testing/github-fixture-collector-native-observed-20261015.json)과 새 SQLite 연동 검사를 구분한다. 기존 sandbox 세 실패·D17/D18 상한·연장 소진에 따른 보류는 유지하며 이를 재개하는 launcher·head 옵션을 만들지 않는다.
