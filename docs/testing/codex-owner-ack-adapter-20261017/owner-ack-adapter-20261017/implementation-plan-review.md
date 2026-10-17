# OWNER-ACK-ADAPTER-20261017 D29/D06 독립 사전 검토

판정: **PASS** — 아래 정본 v2의 로컬 순수 adapter 구현·검사 계획을 승인한다. 제품 정적 검토·실행·최종 인수 PASS는 아니다. 현재 차단 finding은 없다.

- review-plan.md = frozen-plan-v2.md SHA256: `52144e10edda70a96a25989a13bdc96d0069a5bce1d0cfab35e667a903a6d995` (15,583 bytes / 93 lines).
- 실제 clean HEAD/base PR21: `7806daca6bf48c220f7bcd228d1140ef79c2b230`; branch `codex/agent-project-owner-ack-adapter-20261017`.
- unit-budget.json SHA256: `3e2ee7e6e0c0466a7e8cebdd2da2a80460d74658aaad64e4f74ca15a26302db8`.
- 공유 예산: **2026-10-17 02:32:19.107024Z→03:32:19.107024Z**. 최초 분석부터 계획·검토·구현·검증·정리까지 포함하며 같은 실패 수정≤3, 연장/재설정은 없다.

**D06 교정 수용:** 보존 v1 SHA256 `483a2ffcedbf6cb12ad524496dd018768e735e64d189b6f48e25d56876bd97bc`와 v2의 실제 diff 및 preimplementation-plan-correction.json(`5d7d2280c89d38efccf1f4edce7d7e68afcfce73ec9427b7b8ceb18806ab7ed4`)을 대조했다. 변경은 버전 표기와 U+FEFF/검사 설명에 한정된다. v1의 미승인 작성안에 있던 추가 내용 금지를 제거해 기존 PR21 계약과 이번 사용자의 명시 지시를 복원하는 경미한 정합성 보정으로 수용한다. v1 기대값을 그대로 유지했다는 뜻은 아니다. 승인된 기존 계약의 판정·필수 범위를 완화하지 않는다. builtin str 내부 U+FEFF는 위치와 무관하게 일반 텍스트로 보존하고 UTF-8 byte 수에 포함한다. raw BOM bytes는 decoded dict 입력이 아니므로 RESULT_INVALID다. surrogate 거부·정규화 금지·16,384 UTF-8 byte 상한은 유지한다. 아직 제품/검사 실행이 없고 v1·교정 근거를 보존한다.

**공개 API와 실패 경계:** prepare_queue_ack의 단일 MISSING identity sentinel, 조사·직렬화 전 단일 상호배타 guard, result→error→absence 분기가 구체적이다. explicit None은 absence가 아니며 두 None도 먼저 INPUTS_MUTUALLY_EXCLUSIVE로 거부한다. 일반 preparation Exception은 payload 모두 None의 REJECTED/PREPARATION_UNCONFIRMED로 닫고 BaseException은 삼키지 않는다. client/call/path/connection/clock 인자나 실행 경로가 없어 독립 로컬 구현에 native 계약이 선행 조건이 되지 않는다.

**bounded serialization·hostile 입력:** builtin dict/cardinality→builtin str key/길이→keyset/lookup 순서로 hostile key의 hash/equality 및 subclass hook을 피한다. 고정 유한 dict/list shape와 builtin 값만 허용하므로 cycle·임의 nested object·float/비유한수·동적 serialization/default/deepcopy가 필요 없다. ID/client 128자 및 text 16,384자 선검증 뒤 strict UTF-8 encode를 한 번 수행해 임시 할당을 최대 65,536 bytes로 제한한다. 이어지는 단일 UTF-8 byte guard를 통과해야 copy/canonical/hash로 진행한다. optional empty text_elements presence·원래 문자열·입력 불변을 유지하며 새 bounded builtin copy만 기존 canonical로 직렬화한다. 이 조건이면 serialization 전 할당도 유한하고 최종 bytes cap 262,144를 별도로 확인할 수 있다. PR21 _text를 중복 호출해 새 byte guard 변이를 가리지 않는 범위가 적절하다.

**안전한 오류 변환·반환:** 실제 Exception 타입만 허용하고 control BaseException/None/문자열/클래스 객체를 거부한다. 고정 quota._error_code를 한 번 재사용해 instance-dict descriptor의 기존 안전 경로와 enum/fallback을 유지한다. 새 exception decoder나 property/str/repr/args 조회가 필요 없다. exact 반환 keys/types·ready iff PREPARED·상태별 payload 조합과 모든 IO/신뢰/권한 flag=false가 명확하다. 원문은 성공한 준비 결과의 transient bytes에만 있고 로그·파일·DB·global cache에는 남기지 않는다. caller는 ready=True인 경우에만 세 payload 필드를 PR21에 전달해야 한다. REJECTED의 None을 absence로 저장하는 연결은 승인 범위가 아니다.

**정확히 네 새 method의 범위가 충분하다.** 첫 method는 canonical bytes/hash·U+FEFF/순서/empty spans·ID/Unicode 한계 및 실제 임시 SQLite PR21 저장/peer/reopen을 담당한다. 두 번째는 hostile shape/key/hook·raw BOM·surrogate·문자/byte cap과 encode/serialize 미도달을 확인한다. 세 번째는 고정/unknown/hostile Exception·absence/controls 및 실제 UNKNOWN 저장·재개방·privacy/source 보존을 확인한다. 네 번째는 dual/None/absence·report/권한 flags·no IO와 일반 preparation 예외를 확인한다. source API·clock·connection 접근 차단은 adapter 호출 중 적용하고, fixture의 DB 생성/init/claim/receipt API/close는 그 밖에서 수행한다. 기존 TestCase method/setup/classmethod를 호출·상속하지 않고 고정 module-level support만 재사용한다. 새 schema·기존 source 변경·추가 method/suite는 필요 없다.

**정확히 두 semantic mutants:** 정상 네 method가 모두 PASS/FAIL0/ERROR0/SKIP0인 뒤에만 지정 helper를 각 한 번 실행한다. helper 정상 PASS는 두 번째/네 번째 method 내부 각 1회 결과를 재사용한다.

- M_UTF8CAP: byte guard 한 곳만 True로 바꾸고 유효한 4-byte Unicode 4,097자(16,388 UTF-8 bytes)를 사용한다. 실제 잘못된 PREPARED/ready=True·ACK bytes·정확 hash·초과 UTF-8 길이·입력 불변·권한 false를 캡처하고 지정 semantic assertion FAIL1/ERROR0/SKIP0을 요구한다. 이 helper에서 PR21 DB로 전달해 기존 cap에 가려서는 안 된다.
- M_DUAL: 상호배타 guard 한 곳만 True로 바꾸고 정상 decoded result와 안전한 Exception을 함께 준다. result 우선 경로에서 실제 잘못 준비된 ACK bytes/hash·code=None·dual presence를 확인하고 지정 semantic assertion FAIL1/ERROR0/SKIP0을 요구한다. 다른 분기에서 상호배타 검사를 반복하지 않는다.

두 변이는 **wrong prepared 결과**를 검출하는 것이며 DB wrong commit을 요구하지 않는다. 나머지 AST·파서/직렬화/hash·원본 source는 불변이어야 한다. artifact는 synthetic fixture를 제외한 runtime 원문을 기록하지 않고 hash·길이·presence·고정 상태와 정확 assertion 위치만 보존한다. syntax/import/다른 guard/fixture ERROR는 검출 성공이 아니다. 이 구분은 새 순수 경계의 결함을 직접 검증하며 기존 PR21 통과 검사를 반복하지 않는다.

최소 source copy·상대 고정 fixture·Python3.10 `-I -S -B`·실제 temp file SQLite 결합과 no IO guard가 D30/D32/D35의 이번 범위에 적절하다. native/UI/LLM/부하/운영 migration·복원 검사를 추가할 이유는 없다. 제품 SOURCE_READY 뒤 별도 정적 검토와 최초 정상4/지정2 실행 근거, 최종 독립 검토·PR21 대상 Draft/readback을 거쳐야 완료다.

실제 owner client 선택·신뢰 pin·ID demux/기한·call의 성공/유실·claim/dispatch 직렬화·사용량/owner 승인·live ACK/controller/native 인수는 후속 연결의 별도 조건이다. 이번 로컬 인자 준비의 ready가 이를 확인하거나 실행을 허가하지 않는다. publication helper의 이번 cfg 확장은 계획 단계에서 기존 helper를 반복 검토/실행할 대상이 아니므로 추가 조사하지 않았다.

이번에는 정본·v1 delta·교정/예산 근거와 Git 상태만 읽고 이 보고서만 작성했다. 기존 통과 source/schema/검사/검토를 재수행하지 않았고 제품/검사 작성·import/실행·SQLite SQL·native query/RPC/socket/call/queue/credential/service·다른 agent 조회/호출·통지·설정 변경은 없었다. 원본11개·과거 근거·old native D18 HELD·scheduler/root/user PAUSE·운영/main/VMS 보호와 조사 상한을 유지한다.
