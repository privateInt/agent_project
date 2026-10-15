# C01-P2 수정 계획 독립 검토

판정: **PASS** — 아래 정확한 delta에 한해 구현·새 회귀 실행을 승인한다. 최종 수정 소스의 인수 승인은 별도다.

- 수정 계획: `review/fix-plan.md`
- 정확한 delta SHA-256: `498c6eabc3be27a37b7e693d55976ba80f10900e8db1ee593b36b648510aa279`
- 기존 계획 SHA-256: `227df66ea111e1d29e5982290630deac9b3b40b9e034e064a08283540142962e`
- 수정 전 controller SHA-256: `e96e97fa47c190b5340b3989694f41ee29804d3cf60790e54af2aa13bdcac5be`
- 수정 전 새 tests SHA-256: `fa293ea0686d4dd14b70c73b7c580b61f42d02c0746c65aa28af930fb529d818`
- 보존할 `review/final-review.md` SHA-256: `a5e81fd8dd524a32b01f44172d43122082265d12e3e67bf65a853e19475ea8f6`

공통 비공개 reason 집합 세 개(`ASSIGNMENT_PIN_MISMATCH`, `ASSIGNMENT_CONFLICT`, `STATE_CONFLICT`)를 기존 `_result`와 기록 후 artifact 복원 분기에서 함께 사용하는 수정은 P2에 직접 대응한다. 정상 저장·조회, claim/terminal 상태, SQL, API 및 불명 상태에서의 원래 bytes 보존 계약은 변경하지 않는다.

계획의 새 method 하나·세 subcase는 변경 경계에 필요한 범위다. 기록 성공 뒤 실제 control pin 충돌과 terminal 실패/저장 artifact 모순을 각각 만들어 최종 응답의 BLOCKED reason·record/artifact 비공개·양쪽 DB 행 보존을 확인한다. 마지막 조회의 control COMMIT 응답 오류는 기록 단계가 성공한 후에만 주입해 DATABASE_UNCONFIRMED·저장 확인 false·원래 수집 bytes 보존을 직접 확인한다. 오류를 제거한 별도 재개방 조회에서는 저장된 bytes/hash를 대조한다. fault가 앞선 초기화·claim·기록 단계에 잘못 걸려 변경 분기를 우회하면 이 검사의 근거가 되지 않는다.

검사 실행 순서는 승인된 계획 그대로다. 새 method만 수정 전 source에서 한 번 실행해 P2의 의미 assertion 실패(환경/import ERROR와 구분)를 보존하고, 승인된 guard 수정 후 실패한 새 target만 재검증한다. 기존 method 내용은 바꾸지 않는다. 재현·수정 전후 source/test SHA, 정확한 diff, 원래 artifact 비교, 두 실행 로그 및 보존 비교를 별도로 연결한다.

사용자의 통과 검사 재실행 금지가 기존 최종 검토의 C09 재실행 제안보다 우선한다. 마지막 조회 불명 subcase가 수정된 bytes 복원 분기를 직접 검증하므로 C09의 이미 통과한 record-COMMIT 사례를 반복할 필요가 없다. C01-C12의 기존 13 methods, R03/R04/R15, 전체 suite 및 M1/M2도 재실행하지 않는다. 기존 결과는 원래 pin의 근거로 보존·재사용하며 변경된 source에서 다시 실행했다고 표시하지 않는다. M1/M2 대상인 fresh-claim gate·준비 실패 분류가 그대로인지는 최종 diff 검토로 대조한다.

이 delta는 추가 권한·운영 변경·배포·서명/발행·head 실행·sandbox 조사를 승인하지 않는다. 기존 계획/최종 검토/초기 실행/변이/최초 오류 기록은 불변으로 유지한다. 검토자는 테스트 실행, 제품/검사 수정, credentials/settings/network 접근, 추가 작업 생성 또는 MCP 통지를 수행하지 않았고 이 계획 검토 파일만 작성했다.
