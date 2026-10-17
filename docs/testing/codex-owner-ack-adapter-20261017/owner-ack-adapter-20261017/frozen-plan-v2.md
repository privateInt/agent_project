# OWNER-ACK-ADAPTER-20261017 · D29 frozen v2
상태 TEST_PLAN_READY / 독립 D29 사전 검토 전. 이번에는 이 계획만 작성했으며 제품·검사 작성/import/실행·SQL·native 조회는0이다.
ROOT=`/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015`, work=`ROOT/owner-ack-adapter-development`다.
실제 clean HEAD/base PR21=`7806daca6bf48c220f7bcd228d1140ef79c2b230`, branch=`codex/agent-project-owner-ack-adapter-20261017`를 확인했다.
근거는 `ROOT/transition-evidence/owner-ack-adapter-20261017` 아래에만 둔다. 시작02:32:19.107024Z→마감03:32:19.107024Z, 총60분에 분석/계획/review/구현/검증/정리 포함.
같은 실패 수정≤3, 최초 오류 보존·해당 단계만 재검증·PASS 반복0·시계 재설정0. old native D18 HELD·scheduler/root/user PAUSE·원본11개·과거 코드/PASS/오류는 불변이다.
새 client/socket/RPC/call/retry/clock/DB 연결·설정/credential/운영/main/GitHub 보호/VMS/native 조사·조회는0이다. 기존 client/transport 변경·live queue 연결로 확대하지 않는다.

## 고정 재사용·변경 파일
새 제품은 `src/ops/tools/codex_unattended_owner_ack_adapter.py`, 새 검사는 `src/ops/tools/tests/test_codex_unattended_owner_ack_adapter.py`만이다.
PR21 receipt SHA=`24812dc20aa9e20f52ed7c920a301800c00f7568e950f1ed4a9782c1d44495af`의 record_queue_receipt/read_queue_receipt를 검사 fixture에서만 호출한다.
quota SHA=`699f3a6e8993f28d88d7413ac51197a144b6d8b69a01f321da11a17398a4a23c`의 _error_code/_ERRORS, collector SHA=`4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0`의 _canonical을 재사용한다.
parser SHA=`0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a`의 _sha/_identifier를 재사용한다. 기존 _text는 독립 byte cap을 포함하므로 adapter에서 중복 호출해 M_UTF8CAP을 가리지 않는다.
SQLite fixture는 binding `1663ab454e0f66a5e7f23382b9cf4c047068a0c86f10d895c4ad19a4d57f2a3c`, journal `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8`, ledger `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4` 그대로다.
고정 helper source: PR21 test `730b8118103263175fbee94d30fde2fff5b5ae98d6b653e93f8da93db688fbf3`, intent test `a7ed452257d098cd0003d25675722d83a73fb70affc999e0ea269efc14c49efe`다.
strict_io source=`7e57cdcef6f3ef48c975e9de9d494b34018e97f14b90478a273ae1820b8fb476`, binding helpers=`63e1b380519df8f78d6e143c8824150049b73c4cf024a437442501ba9fd281d6`다.
module-level connect/profile/canonical/sha/snapshot/receipt_rows/receipt_snapshot/strict_io/no_external_io 함수만 재사용하며 기존 TestCase method/setup/classmethod는 호출·상속하지 않는다.
기존 제품/helper/schema/operator/초기 receipt/검사 근거는 수정하지 않는다. 새 DB schema/migration·새 protocol decoder는 필요 없다.

## 단일 공개 API·입력 모드
`prepare_queue_ack(*, decoded_result=MISSING, call_error=MISSING) -> dict`
MISSING은 모듈의 단일 object sentinel이며 presence는 identity로만 판정한다. sentinel 자체를 명시하면 omission과 같다. 입력 객체의 equality/bool/coercion은 사용하지 않는다.
두 인자가 모두 명시됐으면 단일 상호배타 guard에서 즉시 REJECTED/INPUTS_MUTUALLY_EXCLUSIVE다. type·shape·code 조사나 serialization 전에 거부한다.
그 뒤 분기 순서는 result→error→absence다. 상호배타 guard를 다른 분기에 중복하지 않는다. 두 인자가 모두 MISSING일 때만 명시적 absence를 준비한다.
explicit decoded_result=None은 absence가 아니며 RESULT_INVALID, explicit call_error=None은 ERROR_INVALID다. 둘 다 명시한 None도 먼저 INPUTS_MUTUALLY_EXCLUSIVE다.
함수는 client/callable/path/connection/clock/options를 받지 않고 caller-owned client의 결과/오류를 로컬 인자로 옮길 뿐이다.

## result의 bounded builtin 검증·복사·직렬화
exact builtin dict root는 {queuedSubmission} 한 key, queuedSubmission은 exact builtin dict {id,clientUserMessageId,input} 세 keys다.
각 dict는 type·cardinality를 먼저 검사하고 key를 순회해 모두 builtin str·길이≤32인지 확인한 뒤에만 keyset 비교/lookup한다. hostile key/subclass의 hash/equality/custom keys hook을 호출하지 않는다.
queued id/client는 builtin str·문자수1..128을 먼저 확인하고 기존 _identifier의 ASCII [A-Za-z0-9_.:-]만 허용한다. 큰 문자열을 encode/복사해 확인하지 않는다.
input은 exact builtin list 길이1, item은 exact builtin dict {type,text} 또는 {type,text,text_elements}, type은 builtin str 값 text다.
text_elements가 있으면 exact builtin list 길이0만 허용한다. presence를 보존해 새 []를 복사한다. tuple/subclass/extra key/image/nonempty spans는 RESULT_INVALID다.
text는 builtin str·문자수1..16384를 encoding 전에 확인한다. 문자열 안의 U+FEFF는 일반 텍스트로 보존하고 strict UTF-8로 surrogate만 거부한다. BOM이 붙은 raw bytes는 decoded dict가 아니므로 거부한다. 정규화/strip/개행 변환은 없다.
문자 cap 통과 후에만 _encode_text(text)의 strict UTF-8 encode를 한 번 수행한다; 이 시점 할당은 최대65536bytes로 제한된다.
그 다음 단일 byte guard `len(text_utf8) <= 16384`가 필수다. 초과는 REJECTED/TEXT_UTF8_LIMIT이며 copy/serialization/hash로 진행하지 않는다.
정해진 유한 shape만 고정 깊이로 확인하므로 cycle·nested/custom object·float/NaN/Infinity/bool/숫자/None 값은 수용 위치가 없어 RESULT_INVALID다. 재귀 walk/deepcopy/json default hook은 없다.
검증을 마친 뒤에만 새 builtin dict/list의 제한된 복사본을 만든다. immutable builtin str만 공유하며 입력 key 순서·값·중첩 객체를 변경하거나 반환 dict에 원본 참조를 남기지 않는다.
새 copy를 기존 _canonical(sorted/compact/ensure_ascii=True, ASCII bytes)로 직렬화하고 길이1..262144를 확인한 뒤 기존 _sha로 lower64hex를 만든다.
유한성은 copy의 허용 type으로 보장한다. 반환 bytes에는 BOM이 없고 optional empty text_elements presence를 보존한다. key order가 달라도 동일 shape/value면 같은 bytes/hash다.
문자/shape 선검증 없이 원본 전체를 serialize·encode·deepcopy하지 않는다. test seams는 _encode_text(text), _canonical(value), _sha(raw)의 작은 함수/고정 alias뿐이며 운영 설정이 아니다.

## error·absence 변환
error만 명시하면 `issubclass(type(call_error), Exception)`으로 실제 타입을 검사한다; 인스턴스 __class__/__getattribute__/code property/str/repr/args를 조회하지 않는다.
Exception subclass만 허용하며 str/object/None/Exception 클래스 자체와 KeyboardInterrupt/SystemExit/GeneratorExit 등 control BaseException은 ERROR_INVALID다.
허용 Exception은 기존 quota._error_code를 한 번 호출해 _ERRORS의 고정 RPC code 또는 UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR만 받는다. 원본 message/unknown code/exception typename은 보관하지 않는다.
_error_code는 기존 BaseException instance-dict descriptor 경로를 그대로 재사용한다. adapter는 독자 exception decoder·동적 code/property·retry를 만들지 않는다.
error 변환과 noargs absence는 PR21에 UNKNOWN 관찰을 저장할 수 있는 인자 준비다. 확정 실행 실패나 request 전송·미전송의 증명이 아니다.
일반적인 preparation Exception은 REJECTED/PREPARATION_UNCONFIRMED·payload 세 필드 None으로 닫고 raw exception을 출력하지 않는다; BaseException을 catch해 IO guard/취소를 숨기지 않는다.

## exact 반환 계약·caller gate
plain dict exact keys={scope,ready,status,reason,ack_result_bytes,expected_ack_sha256,call_error_code,remote_io_performed,db_io_performed,input_origin_verified,client_identity_verified,transport_pin_verified,transport_id_matching_verified,deadline_verified,native_acceptance_verified,remote_mutation_authorized,dispatch_permitted,replay_permitted,native_ready}.
scope=OWNER_QUEUE_ACK_ADAPTER_ONLY; ready는 builtin bool, status=PREPARED|REJECTED이며 ready iff PREPARED다. ready는 receipt API 인자 준비만 뜻한다.
PREPARED/ACK_RESULT_PREPARED: ack_result_bytes=builtin bytes, expected_ack_sha256=그 bytes의 SHA, call_error_code=None이다.
PREPARED/ABSENCE_PREPARED: 세 payload 필드 모두 None. PREPARED/CALL_ERROR_PREPARED: bytes/hash=None, code=위 고정 code다.
REJECTED reasons={INPUTS_MUTUALLY_EXCLUSIVE,RESULT_INVALID,TEXT_UTF8_LIMIT,ERROR_INVALID,PREPARATION_UNCONFIRMED}; ready=false·세 payload 필드 모두 None이다.
remote_io_performed/db_io_performed/input_origin_verified/client_identity_verified/transport_pin_verified/transport_id_matching_verified/deadline_verified/native_acceptance_verified/remote_mutation_authorized/dispatch_permitted/replay_permitted/native_ready는 항상 false다.
원문은 ACK 준비 성공의 transient 반환 bytes에만 있다. normalized dict/error/source 객체·message·원문·주소·계정은 로그/DB/파일/global cache에 남기지 않는다.
caller는 ready=True를 확인한 경우에만 세 필드를 PR21 record_queue_receipt에 keyword로 전달한다. REJECTED의 None 필드를 absence로 전달해 UNKNOWN 저장으로 바꾸면 안 된다.
submission 원문 bytes/SHA·key/intent/binding pins·UNKNOWN1/source/DB 소유권 검증은 기존 PR21에 그대로 남는다. adapter는 이들을 받거나 확인했다고 표시하지 않는다.
실제 owner client 선택/source pin·transport ID/deadline/demux·call의 성공/유실·dispatch 직렬화·사용량/owner authorization·native acceptance는 외부 caller 책임이다.

## 정확4 새 검사 — 각 검사가 새 동작을 확인하는 이유
새 `unittest.TestCase`만 사용하며 explicit synthetic relative fixture만 허용한다. 모든 adapter 호출은 기존 strict_io/no_external_io(BaseException) 안에서 수행하고 stdout/stderr·IO 시도0을 확인한다.
`test_result_canonical_bounds_and_receipt_reopen`: 정상/텍스트 내부 U+FEFF 보존/다른 key order/optional empty spans·id/client128·4byte Unicode4096자=UTF8 16384bytes를 준비, canonical expected bytes/hash·원본 identity/값 불변을 확인한다.
같은 method에서 정상 및 최대 Unicode의 반환 세 필드를 PR21에 실제 전달한다. fixture가 새 temp file DB3개와 PREPARED binding→claim UNKNOWN1을 준비하고 receipt 저장/peer/reopen exact P/hash를 확인한다.
이 검사는 새 serialization이 기존 UTF-8/pin 계약에 맞고 실제 SQLite 재조회까지 연결되는지 검증한다; 기존 PR21 test method를 다시 실행하는 것이 아니다.
`test_invalid_result_shape_utf8_and_caps`: subclasses/hostile keys/customhooks/cycle/extra keys/float/raw-BOM-bytes/surrogate/ID·문자·byte 상한을 REJECTED로 확인하며 _encode_text/_canonical spy로 char-cap 전 encode0·거부 후 serialize0을 증명한다.
그 method가 `case_utf8_byte_cap_rejects_overlong_result` helper를 정확1회 호출한다. 이 검사는 큰 encoding 할당 방지와 문자수≠UTF8 byte수인 새 위험을 검증한다.
`test_safe_error_absence_and_unknown_receipt`: noargs·고정 code/unknown code·hostile code equality/property/getattribute/str/repr의 Exception을 변환하고 hook0·message privacy를 확인한다.
대표 허용 code/UNCLASSIFIED/absence 인자를 실제 PR21에 전달해 UNKNOWN reason 저장/peer/reopen·권한 false·원본 source 불변을 확인한다. Exception이 아닌 값/controls는 ERROR_INVALID다.
`test_dual_none_and_no_io`: `case_dual_arguments_are_rejected` helper 정확1회, dual 모드/explicit None 거부·absence 구분·고정 전체 keys/types/flags를 확인하고 모든 외부 IO/clock/connection/call/source API 접근을 차단한다.
같은 method에서 bounded _canonical의 일반 Exception을 한 번 주입해 PREPARATION_UNCONFIRMED·payload None·raw error 비노출을 확인한다. 별도 method/suite를 추가하지 않는다.
SQLite 생성/명시 init/claim/borrowed close는 fixture 소유이며 adapter 호출 밖에서만 한다. 재open된 현재 연결도 identity-dedup/alive guard로 닫고 caller 데이터/profile/schema/원본 보존을 확인한다.
Python3.10 호환·authorizer cleanup lambda *args: SQLITE_OK·SQLite audit path os.fsdecode(os.fspath(...))·stat으로 alias 실체 조사0을 기존 기준대로 유지한다.

## 정확2 semantic mutants · 정상4 전부 PASS 뒤 지정 helper만 ONCE
M_UTF8CAP은 단일 text byte guard만 True로 치환한다. target=case_utf8_byte_cap_rejects_overlong_result, fixture=유효 shape/ID/문자 cap 안의 U+1F600 4097자=UTF8 16388bytes다.
정상은 REJECTED/TEXT_UTF8_LIMIT이다. mutant는 실제 ready=True/status PREPARED/ACK_RESULT_PREPARED·nonnull bytes·정확 hash·decoded text UTF8>16384를 반환해야 지정 semantic assertion FAIL1/ERROR0이다.
이 helper는 PR21에 변이 결과를 전달하지 않는다(PR21의 기존 byte cap으로 새 adapter의 잘못 준비됨을 가리면 안 됨). input 불변·serialized bytes≤262144·authority false를 함께 증명한다.
M_DUAL은 단일 상호배타 guard만 True로 치환한다. target=case_dual_arguments_are_rejected, fixture=정상 decoded result와 안전한 Exception을 동시에 명시한다.
정상은 REJECTED/INPUTS_MUTUALLY_EXCLUSIVE다. mutant는 result 우선 경로의 실제 잘못된 PREPARED ACK bytes/hash를 반환해야 semantic FAIL1/ERROR0이며 error code는 None이다.
두 target의 정상 PASS/marker는 위 정상 methods 안에서 각1회만 기록하고 재사용한다. 변이는 isolated source copy·exact1 AST predicate 치환·나머지 AST/원본 source 불변이다.
잘못 준비된 transient bytes/hash/UTF8 byte count 또는 dual presence·sanitized 관찰·정확 assertion 위치를 캡처하고 원문을 로그/DB에 남기지 않는다. syntax/import/다른 guard/fixture ERROR는 검출 성공이 아니다.

## 실행·근거·완료선
지금 TEST_PLAN_READY/SHA 후 멈춤→총괄 frozen 사본/독립 D29 PASS→제품/새 검사 병행 작성→SOURCE_READY 소스 hash/guard 위치 고정→독립 source review 순서다.
runner는 실제 승인된 source/test/fixture/plan/review/environment hash를 확인하고 최소 source copy -I -S -B에서 최초 정상4 failfast ONCE, 전부 PASS일 때만 지정 helper 변이2를 각각 ONCE 실행한다.
FAIL/ERROR/SKIP은 최초 raw log/입력 pins/source/test/cleanup 관찰을 보존하고 중단한다. 수정은 같은 실패≤3·필요한 해당 target만, 정상 PASS/old suite/R03/R04/R15/이전4·6 반복0이다.
모든 새 snapshot/runner/result는 이 evidence의 별도 run에 보존한다. fixture 원문은 명시된 synthetic만, adapter의 runtime 원문은 로그/DB에 저장하지 않는다.
D30/D32/D35는 새 pure boundary와 실제 PR21 fileSQLite 결합/UNKNOWN/격리만 검증한다; native/UI/LLM/부하/운영 migration·복원 인수는 영향 없어 새로 실행하지 않는다.
최초4+지정2 실제 근거 후 독립 final review→이번 단위만 commit→PR21 대상 Draft PR→게시된 파일 exact hash readback 순서이며 main 병합/상시 worker 활성화는 없다.
완료선은 작은 adapter+새4검사+의미 변이2+portable/실제 SQLite 근거+최종 독립 PASS·commit/Draft/readback이다. 실제 owner call/live ACK/dispatch 연결 완료나 native 권한을 뜻하지 않는다.
