**REQUEST_CHANGES** — PR24 `02724c5f…` 기준, 두 문서를 함께 검토했습니다.

1. [owner-binding 계약](../transition-evidence/owner-cli-controller-20261017/plan-clarifications.md:2)에 필수 `request_sha256`이 빠져 있습니다. CLI receipt hash와 구분해 실제 요청의 해시 대상·인코딩을 고정하고, C04에 request hash·user-message item ID·role 불일치 및 **관측값 None이 일치하는 정상 사례**를 명시해야 합니다.

2. [C01](../transition-evidence/owner-cli-controller-20261017/test-plan.md:28)의 확인·실패 판정을 구체화해야 합니다. 임시 DB 초기화와 claim의 반환 report를 확인하고, COMMIT 전 rollback은 `PREPARED`, COMMIT 후 확인 유실은 `UNKNOWN`으로 구분하십시오. 미확인 호출의 adapter 진입은 0회, 저장된 UNKNOWN의 재진입도 추가 제출 0회여야 합니다.

3. [신규 controller 실행 제한](../transition-evidence/owner-cli-controller-20261017/plan-clarifications.md:4)을 C01/C02의 명시적 검사로 연결해야 합니다. default submission disabled, 각각의 임시 root/user/local PAUSE, schedule 밖, 시작 3회 소진, lock/admit 저장 실패에서 **adapter 0회**를 확인하십시오. 기존 PR24 PASS 재검사는 필요 없습니다.

쓰기·제품/검사 실행·import·RPC/network·추가 agent 호출은 모두 0이며, 05:50Z 상한을 유지했습니다.
