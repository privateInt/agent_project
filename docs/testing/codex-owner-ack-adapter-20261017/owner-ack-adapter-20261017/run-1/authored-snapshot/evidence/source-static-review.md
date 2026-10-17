# OWNER-ACK-ADAPTER 최초 source 독립 정적 검토

판정: **PASS (STATIC ONLY)** — 고정 source 134행 전체를 대조했으며 수정이 필요한 구체적 finding은 없다. 실제 검사·최종 실행·native/owner 인수 PASS는 아니다.

- Source: `owner-ack-adapter-development/src/ops/tools/codex_unattended_owner_ack_adapter.py`
- 전체 Source SHA256: `c471abd71fd34d8a2f6d037150ab4a642a0ec81c1c7cb7feb59759ffcdc01e05`
- 정본 review-plan.md = frozen-plan-v3.md SHA256: `896b50320eedce867dc2aee90b0744ff58b6307811e3fccf8cef55250e7b0849`
- 재사용 v2 사전 검토 SHA256: `7a37efe4f262cc81f234cc67b439b838ea63b2ef2bfb06756b0945fd672977de`
- 재사용 v3 delta 검토 SHA256: `8c761f48a19ab21fff8dc1b80c9b23753e8bc48eb244c44b78e8150cddd9cf63`
- 실제 HEAD/base PR21: `7806daca6bf48c220f7bcd228d1140ef79c2b230`.

기존 사전 판정은 반복하지 않고 고정 pin으로 재사용했다. source-ready의 source pin·두 mutation expression과 실제 파일이 일치한다. 다음은 새 구현의 최초 정적 확인이다.

| 범위 | 확인 결과 |
|---|---|
| 35–39, 46–67행 | exact builtin dict와 cardinality를 확인한 뒤 모든 key의 builtin str/type·길이를 검사하고 그 뒤 set 비교/lookup을 한다. subclass/hostile key를 lookup이나 hash/equality에 넣지 않는다. ID/client 128자, builtin list/item/empty spans·text type/문자 cap 검사가 고정 유한 shape를 만든다. |
| 42–43, 67–78행 | text 문자수 1..16,384를 encoding 전에 확인하고 strict UTF-8 encode를 한 번 수행한다. surrogate는 RESULT_INVALID, UTF-8 초과는 TEXT_UTF8_LIMIT다. U+FEFF나 개행/Unicode를 제거·정규화하는 경로가 없다. byte guard 통과 뒤에만 새 dict/list를 만들고 optional empty spans presence를 보존한다. 원본 nested 객체를 바꾸지 않는다. |
| 81–98행 | BaseException의 instance-dict descriptor로만 원본 dict를 얻는다. exact dict·항목≤32에서 builtin items를 순회하고 builtin str key·길이4를 먼저 확인한 뒤 literal code와 비교한다. 원본 get/lookup·property/str/repr/args는 없다. value도 builtin str·길이≤64를 먼저 확인한 뒤 고정 enum을 조회한다. 일반 Exception 시 candidate를 비우고 plain Exception() proxy에 고정 code만 넣는다. 기존 quota._error_code 호출은 **proxy로 정확히 한 번**이며 원본 exception을 넘기지 않는다. |
| 108–115행 | MISSING presence는 identity bool로만 판정한다. 단일 dual guard `not (has_result and has_error)`가 type·shape·error 조사·직렬화보다 앞선다. 두 explicit None도 여기서 거부한다. 다른 branch에 중복 dual guard는 없다. |
| 116–129행 | result→error→absence 순서다. result는 검증된 copy만 기존 canonical로 직렬화하고 bytes/cap 확인 뒤 SHA를 계산한다. error는 실제 Exception 타입만 허용해 안전한 proxy 변환을 쓴다. explicit None/controls를 absence로 취급하지 않는다. noargs만 세 payload None의 ABSENCE_PREPARED다. |
| 16–22, 108–134행 | 반환 keys·builtin ready/status·상태별 세 payload 조합과 고정 reason이 계획에 맞는다. 성공한 준비 결과만 ready=True/PREPARED다. 일반 preparation Exception은 REJECTED/PREPARATION_UNCONFIRMED로 닫고 BaseException은 catch하지 않는다. 모든 IO/신뢰/권한 flag는 False이며 raw error/입력 객체·normalized dict를 report에 넣지 않는다. |

value 길이≤64 선검사는 기존 고정 RPC code를 보존하면서 큰 unknown 문자열의 whitelist hash 계산을 막는다. 새로운 error enum/분류를 도입하지 않는다. 원본 attribute dict에 충돌 custom key가 있어도 type gate에서 건너뛰며 classifier에는 깨끗한 proxy만 전달한다. oversized dict는 순회 없이 기존 fallback으로 간다.

M_UTF8CAP의 단일 predicate는 **72행 `len(text_utf8) <= 16384`**, M_DUAL은 **115행 `not (has_result and has_error)`**다. parser._text나 추가 byte/dual guard로 같은 조건을 중복하지 않는다. 계획의 Unicode 4,097자/dual 정상-result fixture는 나머지 문자·shape·최종 serialized cap에 가려지지 않는 구조다. 실제 wrong-prepared bytes/hash와 지정 semantic FAIL1/ERROR0 검출은 아직 검사 근거가 필요하다.

호출 경로는 고정 identifier/canonical/SHA·quota enum/classifier뿐이다. client/call/socket·source/receipt API·DB connect/SQL·clock·경로/환경/credential·출력·retry·global raw cache 경로가 없다. transient 성공 bytes에는 ACK text가 포함될 수 있으며, docstring이 ready=True일 때만 PR21에 전달하고 REJECTED의 None을 absence로 저장하지 말라고 명시한다. ready와 모든 false flag는 native origin/owner 승인/dispatch/replay와 구분된다.

이번에는 해당 source와 pin/예산 기록·Git 상태만 읽고 이 보고서만 작성했다. 새 test 파일·다른 agent 진행 상태는 조회하지 않았다. 제품/test/계획 수정·import·AST/검사 실행·SQLite SQL·native query/RPC/socket/queue/credential/service·위임/통지는 없다. 기존 PASS/suite도 반복하지 않았다. 새4개/변이2개와 실제 PR21 임시 SQLite 결합 후 최종 독립 검토는 별도다. 공유 마감 **2026-10-17T03:32:19.107024Z**, 원래 누적 예산·old native D18 HELD·scheduler/root/user PAUSE·원본11개/과거 근거·운영 보호를 유지하며 연장/리셋하지 않는다.
