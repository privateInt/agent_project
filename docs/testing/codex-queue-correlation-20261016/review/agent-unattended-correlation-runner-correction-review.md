# U01b2 D06 runner 수정 범위 독립 검토

판정: **PASS — 아래로 한정한 AST selector 보정은 D06 경미 harness 수정이다.** 승인된 M1의 의미·검사·통과 기준을 바꾸지 않는다. 실제 수정 파일이나 실행 결과를 승인하는 판정은 아니다.

- 보존 입력: `transition-evidence/correlation-runner-before-m1-index-review.py.txt`
- 입력 SHA-256: `274067174deb402afe9f193e65a0748681fb620f34d97b217e78cb79cd493823`
- 동결 제품 SHA-256: `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a` — 일치 확인, 수정 없음.
- 승인된 v2 계획 SHA-256: `25c798b43efd07b01c8c9030c0de7ff41a04b94cab8dd62dc4462ed13025403f`
- 기존 검사 구성 수정 범위 검토 SHA-256: `f75cb20fd91d507f2eb74a73719f69cdd3515d72c63fe29c6c4b14582e28c665` — 보존.

**관찰 사실:** 이 보존 runner의 131–133행은 Compare의 직접 피연산자가 Constant인 경우와 `len()` Call을 거부한다. 모든 하위 Constant를 거부하는 코드는 없으므로, 이 pin에서는 `matches[0][1] != text`의 Subscript 인덱스 0/1이 거부된다고 결론 내릴 수 없다. 또한 현 스냅샷의 Call 거부는 `len()`에만 한정돼 있다. 요청된 “모든 Call 거부”는 유지라고 가정하지 말고 최종 diff에서 실제 보장 여부를 확인해야 한다. 다른 파일이나 실행을 조사해 이 차이의 경위를 추정하지 않았다.

승인하는 정확한 수정 계약:

1. M1 입력은 고정 제품의 정확한 `matches[0][1] != text` 비교식이다. AST 동일 비교가 정확히 한 개여야 하고 단일 operator/단일 comparator여야 한다는 기존 검증을 유지한다.
2. Compare 내부의 Constant 중 허용 대상은 **즉시 부모 Subscript의 `slice` 자체인 정수 Constant만**이다. `type(value) is int`로 판정하여 bool을 제외한다. 이 고정식에서는 0과 1이다. 단순히 Subscript 하위에 있다는 이유로 value 쪽 literal, 문자열·bool·float, slice 범위의 literal 등을 허용하지 않는다. Compare의 직접 literal 피연산자와 모든 `ast.Call`은 계속 거부해야 한다.
3. 원래 text bytes 비교만 우회한다. 현재 NotEq 분기는 False로 교체하여 CONTENT_MISMATCH 거부를 건너뛰되 client/thread/prompt digest/hash/schema/pagination/유일성 검사는 유지한다. 원래 소스의 한 구간만 교체하고, 그 한 Compare를 바꾼 의도 AST와 결과 AST의 정확한 동등성 검증을 유지한다. 다른 AST·원문 구간이나 M2 selector/변이에는 손대지 않는다.
4. 최초 네 검사 PASS/FAIL0/ERROR0/SKIP0 뒤에만 지정 두 mutant target을 각각 한 번 실행한다. 정상 target PASS는 재사용한다. M1 성공 판정은 지정 semantic reason assertion에서 **FAIL1/ERROR0/SKIP0**이고 실제 잘못된 `OBSERVED/CANDIDATE_OBSERVED` 및 원래 fixture queue ID candidate가 반환된 경우뿐이다. source pin·유일 비교·다른 AST 동등성·semantic gate의 거부를 무시하거나 warning으로 낮추면 안 된다. import/SQL/AST/harness 오류나 단순 flag 오류는 변이 검출 성공이 아니다.

이 범위는 정상 제품의 기대 결과나 필수 검사 강도를 바꾸지 않고, 승인된 nested index 표현을 선택하도록 검증기를 정리하는 작업이다. literal/Call 제한은 오히려 더 명확해진다. D06·D07의 독립 검토에 따라 경미 수정으로 분류하며 사용자 판단이 필요한 기준 완화는 포함하지 않는다. 테스트 assertion·fixture·제품·계획 변경, case 삭제·SKIP·재실행 허용은 승인하지 않는다.

최종 독립 검토에서는 실제 최종 runner 전체 SHA, 이 보존 입력 대비 diff, 정확한 M1/M2 실행 인자·변이 diff와 실제 잘못된 candidate 근거를 확인해야 한다. 현재 보고서는 정밀한 수정 범위 승인만이며, 설명된 최초 결함의 재현이나 수정 완료 확인이 아니다. 수정 완료 파일을 조회·대기하지 않았고 검사/import/AST 실행/DB/native/runtime/probe·다른 agent 접근·위임·통지도 하지 않았다. 이 보고서 외에는 쓰지 않았다.

기존 결과·최초 오류·원본11과 이전 검토는 보존했다. runtime D18 HELD, scheduler/root/user PAUSE, 기존 조사 상한을 유지한다. 공유 보수적 마감 2026-10-16 15:45:08Z와 기존 누적 시간은 그대로이며 재설정하지 않는다.
