# OWNER-ACK-ADAPTER v3 delta 독립 사전 검토

판정: **PASS** — 구체적인 exception attribute key 충돌 gap을 닫는 v3 delta를 승인한다. 나머지 v2 사전 검토는 재사용하며 제품 정적·실행·최종 PASS는 아니다.

- frozen-plan-v3.md 전체 SHA256: `896b50320eedce867dc2aee90b0744ff58b6307811e3fccf8cef55250e7b0849` (16,415 bytes / 94 lines).
- 보존 v2 plan SHA256: `52144e10edda70a96a25989a13bdc96d0069a5bce1d0cfab35e667a903a6d995`.
- 보존 v2 implementation-plan-review.md SHA256: `7a37efe4f262cc81f234cc67b439b838ea63b2ef2bfb06756b0945fd672977de`.

preimplementation-error-key-finding.json과 v2→v3 실제 diff만 대조했다. v2 보고서는 수정하지 않았다. 변경은 버전 표기, 오류 metadata의 bounded 안전 추출/proxy 위임, 기존 오류 method의 두 추가 사례에 한정된다. 새로운 차단 finding은 없다.

**finding의 원인:** BaseException descriptor로 원본 builtin dict를 얻는 것만으로 `attributes.get("code")`의 안전성이 보장되지 않는다. 원본 dict의 custom key가 "code"와 같은 hash를 가지면 lookup 과정에서 저장된 key의 임의 equality hook이 호출될 수 있다. v2의 original Exception 직접 위임 경로에는 이 gap이 있다. 이 기록을 보존하고 해당 부분을 v3로 대체해야 한다.

**수정 경계가 적절하다.** descriptor로 얻은 exact dict만 사용하고 len≤32일 때만 builtin `dict.items`를 순회한다. `type(key) is str`을 먼저 확인한 다음 literal `key == "code"`를 비교해야 한다. 원본 dict의 get/lookup·keyset 변환·비교를 위한 hash 생성·code property·str/repr/args는 사용하지 않는다. 선택한 value도 `type(value) is str` 확인 후 기존 고정 _ERRORS membership을 검사한다. 이 순서에서는 custom key/str subclass/value hook에 도달하지 않는다.

원본 dict가 oversized이거나 유효한 code를 선택하지 못했거나 descriptor/순회에서 일반 Exception이 발생하면 선택 code를 비우고 plain builtin `Exception()` proxy를 만든다. proxy에는 원본 message/args/typename/metadata/key 객체를 넣지 않고 고정 whitelist code만 담는다. 이 깨끗한 dict의 기존 quota._error_code(proxy) 호출은 충돌 key를 포함하지 않으며 정확히 한 번으로 분류를 재사용할 수 있다. code가 없으면 UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR다. control BaseException/IO guard를 catch해 숨기지 않는 v2 계약은 유지한다.

이는 기존 quota pin/PASS를 수정하거나 재실행하는 작업이 아니다. 기존 classifier 앞의 새 untrusted 입력 경계를 닫는 D06 보정이며 성공 기준 완화가 없다. 새 protocol/error enum이나 독립 원문 decoder도 만들지 않는다. source 정적 검토에서는 **원본 exception을 기존 helper에 직접 넘기지 않는지**, scan/type/항목 cap의 순서, 예외 후 candidate 초기화, plain proxy·decoder 1회 위임을 확인해야 한다.

기존 `test_safe_error_absence_and_unknown_receipt` 안에 (1) 실제 "code"와 hash가 충돌하는 custom key만으로 lookup을 유발할 수 있는 원본 instance dict, (2) 33개 이상 attribute 사례를 추가해 hook0/UNCLASSIFIED를 확인하는 것으로 충분하다. fixture 생성 중 발생 가능한 key hash 호출과 adapter 호출 중 hook 관찰은 구분해야 한다. 기존 정상 whitelist code·unknown/hostile value·property/getattribute·message privacy·UNKNOWN SQLite 결합 기대는 유지한다. 추가 method나 mutation target은 필요 없다.

정상 method **4개**, M_UTF8CAP/M_DUAL **2개**, wrong-prepared bytes/hash의 실제 semantic FAIL1/ERROR0/SKIP0 기준과 helper 정상 PASS 재사용은 불변이다. 두 변이를 DB wrong commit 검증으로 바꾸지 않는다. v2의 U+FEFF 처리·bounded serialization·dual/None/absence·caller ready gate·순수/no IO·SQLite fixture/native 구분과 나머지 사전 판정은 재검토하지 않고 재사용한다.

이 PASS 이후 제품/검사 구현 기준과 readiness/실행 snapshot의 plan pin은 **위 v3 전체 SHA** 및 두 독립 보고서로 결합해야 한다. 현재 review-plan.md(v2)는 이 reviewer가 변경하지 않았으며 미검토 producer 후속 변경이 자동 적용되는 것은 아니다. source/검사 작성·실행 근거와 최종 독립 판정은 별도다.

이번에는 finding과 plan delta를 읽고 이 보고서만 작성했다. 코드/검사 수정·import·검사·SQLite·native query/RPC/socket/queue/credential/service·다른 agent 조회/호출은 없다. 기존 source/PASS/suite를 반복하지 않았다. 원본11개·과거 결과·v2 문서와 **마감 2026-10-17T03:32:19.107024Z**, 최초 분석을 포함한 원래 예산·old native D18 HELD·PAUSE·운영 보호를 유지하며 연장/리셋하지 않는다.
