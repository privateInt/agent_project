# C01-P2-v2 수정 계획 독립 검토

판정: **PASS**. 승인 대상은 `review/fix-plan.md`의 정확한 SHA-256 `68831d156cf633d4aacf1d77a4d54d54fc7a138ada19d06dbac547b4b5c4208b`이다. v1 검토 기록은 별도로 보존한다. 이번 승인은 method 분리와 실행 순서에 관한 v2 delta이며, 코드의 세 reason 공통 비공개 guard와 나머지 동결 계약은 동일하다.

세 개의 새 focused method로 분리하면 통과한 positive subcase도 반복하지 않고 변경 분기를 검증할 수 있다. 승인 순서는 다음과 같다.

1. 수정 전 source에서 새 control pin 충돌 target과 terminal 모순 target만 각각 한 번 실행한다. 두 target이 실제 최종 조회의 `STATE_CONFLICT` 뒤 artifact 재부착으로 의미 assertion FAIL을 내야 하며, 환경/import ERROR는 재현 성공이 아니다.
2. 승인된 공통 guard만 수정한다. 실패한 두 target과 **처음 실행하는** 마지막 조회 불명 target만 실행한다. 불명 target은 기록 성공 후 control read COMMIT 응답 오류를 주입해 원래 bytes 보존·저장 확인 false를 확인하고, fault 제거 후 별도 재개방 조회로 원본 bytes/hash를 대조한다.
3. 새 fixture helper 하나와 세 method만 추가하고 기존 13개 method 본문은 byte 단위로 보존한다. 수정 전 source/test 복사본 `evidence/before-p2`의 SHA는 현재 수정 전 원본과 일치한다. 초기 재현과 수정 후 실행 로그·정확한 source/test pin·보존 비교를 각각 남긴다.

기존 C09와 나머지 통과 methods, R03/R04/R15, 전체 suite 및 M1/M2는 재실행하지 않는다. 이전 근거는 이전 pin에 귀속해 재사용하며, 공통 guard 외 claim/분류/SQL 변경이 없다는 최종 diff 대조로 범위를 확인한다. 최종 source 인수는 실행 근거를 받은 뒤 별도 독립 검토한다.

검토 시 대조한 SHA-256:

- 수정 전 controller와 `evidence/before-p2/github_fixture_controller.py`: `e96e97fa47c190b5340b3989694f41ee29804d3cf60790e54af2aa13bdcac5be`
- 수정 전 tests와 `evidence/before-p2/test_github_fixture_controller.py`: `fa293ea0686d4dd14b70c73b7c580b61f42d02c0746c65aa28af930fb529d818`
- 불변 `review/final-review.md`: `a5e81fd8dd524a32b01f44172d43122082265d12e3e67bf65a853e19475ea8f6`
- 보존한 v1 `review/fix-plan-review.md`: `af4cde36ed10b0bd3324406dadbf23ab568ecd6750bebdee0da672eb33371da2`

이 검토는 테스트·제품을 실행/수정하거나 credentials/settings/network·MCP 통지를 사용하지 않았다. v1 기록 앞에 임시로 붙였던 v2 승인은 이 별도 파일로 분리했으며, v1은 위 원래 해시로 복원했다. 최초 최종 검토와 실행/오류/변이 근거는 수정하지 않았다.
