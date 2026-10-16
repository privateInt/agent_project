# U01b2 D06 검사 구성 수정 범위 독립 검토

판정: **PASS — 아래의 정확한 수정 범위는 D06 경미 수정으로 승인한다.** 승인된 v2 계약을 복원하는 검사 구성·기대값 정정이며, 최종 수정 파일의 승인이나 실행 PASS는 아니다. 수정 완료 파일을 기다리거나 조회하지 않았다.

- 수정 전 보존 입력: `transition-evidence/correlation-pretest-test-review-input.py.txt`
- 입력 SHA-256: `5eab928148a081e1f5ef5c27b0d8cc25b542988074c05c7b35e0b006a9caa048`
- 정확한 v2 계획 SHA-256: `25c798b43efd07b01c8c9030c0de7ff41a04b94cab8dd62dc4462ed13025403f`
- 동결 제품 SHA-256: `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a` — 재확인했으며 기존 정적 PASS는 이 pin에 한정해 재사용한다.
- 기존 정적 검토 SHA-256: `3ca50732bb902629f06d58273f2283933c5a9dee46cbc8dbdaaa8d9fccc7e723`

1. **독립 expanded_page의 첫 cursor:** v2는 첫 요청에 명시적 `cursor=null`을 요구한다. 단일 page의 정상 whitespace/key-order 검사에는 같은 thread/limit=20과 null cursor의 새 요청을 사용해야 하며, 두 번째 pagination 요청의 non-null cursor를 재사용하면 안 된다. 제출·요청·응답의 실제 원문 hash는 다시 계산하고, candidate 동일·원문 binding hash 차이·빈 text_elements 동등성 검사는 그대로 유지한다. 별도 `first-not-null` 음성 검사를 삭제하거나 통과로 바꾸지 않는다.

   **관찰 한계:** 제공된 스냅샷 260행은 이미 `self.page(db)[0]`를 사용하고, 128–131행 helper의 cursor 기본값은 None이다. 이 pin에서는 해당 cursor가 이미 올바르다. 따라서 최초 non-null 재사용 버그나 그 before/after diff를 이 스냅샷에서 확인했다고 주장할 수 없다. 현재 pin에 대한 불필요한 cursor 변경은 요구하지 않는다.

2. **malformed JSON 기대값:** 보존 입력 335–348행은 submission/request/response에 일괄 `INPUT_INVALID`, `reads=0`, `confirmed=False`를 적용한다. 이 공통 기대값을 입력 위치에 따라 나누는 것만 승인한다.

   | 오류 위치 | reason / status | public read 횟수 | journal_read_confirmed |
   |---|---|---|---|
   | 제출 params JSON | INPUT_INVALID / BLOCKED | 0 | false |
   | page 요청 JSON | OBSERVATION_INVALID / UNKNOWN | 1 | true |
   | page 응답 JSON | OBSERVATION_INVALID / UNKNOWN | 1 | true |

   전제는 기존 정상 UNKNOWN1 저널, 유효한 제출 결합, 원문 bytes에 맞는 모든 pin이다. page 오류에서는 원래 저널 read가 확정되므로 journal_reason=INTENT_OBSERVED와 원래 state/revision을 유지한다. 제출 JSON 오류에서는 저널을 읽지 않으므로 state/revision은 null이다. 모든 경우 candidate=None, 모든 권한·출처 flag=false, UNKNOWN/raw/hash/DB와 연결 보존은 유지한다. JSON만 잘못됐어도 원문 pin은 유효하므로 binding 검사를 제거하거나 binding=False로 우회하면 안 된다.

v2의 평가 순서 1–3과 4–6을 함께 적용하면 제출 JSON은 read 전, page JSON은 read 후 검증이다. 계획의 일반적인 “early byte 거부 read0” 문구를 모든 page JSON으로 확장하면 이 명시적 순서와 malformed page의 OBSERVATION_INVALID 규칙을 어긴다. 제품도 해당 순서대로 구현돼 있다. 따라서 잘못 작성된 미실행 검사 상수를 승인된 계약으로 되돌리는 것이며 제품 결과에 맞춘 기준 완화가 아니다.

공통 정책 DECISIONS.md의 D06·D07에 따라 독립 reviewer로 분류했다. 필수 범위·검사 수·판정 강도·사용자 승인 기준은 바뀌지 않는다. 정확히 네 method와 두 의미 변이 target, malformed 입력 목록, IO 차단·읽기 횟수·sanitization·보존 assertion을 유지해야 한다. 제품/계획 변경, case 삭제·SKIP·예외 무시·비교 완화·변이 target 변경은 이 승인에 포함되지 않는다.

최종 검토에서 실제 최종 test 전체 SHA와 보존 입력 대비 diff를 확인해야 한다. 이 범위 승인만으로 실제 수정 완료나 최초 네 검사·두 변이 성공을 표시할 수 없다. 최초 실행 근거와 오류 이력은 그대로 보존한다.

검사/import/DB/native/runtime 실행·권한 probe·다른 agent 조회·위임·통지 없이 이 보고서만 작성했다. runtime D18 HELD, scheduler/root/user PAUSE, 기존 원본11·모든 과거 근거·조사 상한을 유지했다. 공유 보수적 마감은 2026-10-16 15:45:08Z이며 시계를 재설정하지 않는다.
