판정: **REQUEST_CHANGES — 현재 실행 허용 불가**

기존 지적 1·2·4는 해소됐습니다. 3은 아래 경로 결합 문제가 남았습니다. 고정 API와 새 검사 호출의 불일치는 발견하지 않았습니다.

1. **P2 — 잘못된 installation 경로를 최종 위치에 공개할 수 있습니다.**
   [launcher:201](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/execution-package-development/src/ops/tools/agent_execution_package.py:201)은 `installation_root`의 부모만 확인합니다. 따라서 runtime의 값을 `<target>/other`로 바꾸고 재고정하면 controller 설정과 달라도 `located=False` 검증을 통과합니다. [stage:380](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/execution-package-development/src/ops/tools/agent_execution_package.py:380)은 이를 rename한 뒤 최종 검증에서 거부하므로 잘못된 target이 남습니다. **rename 전에 두 설정의 installation 경로를 정확히 결합해 검증해야 합니다.**

2. **P2 — runner의 전용 `/tmp` 경계가 강제되지 않습니다.**
   [검사:54](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/execution-package-development/src/ops/tools/tests/test_agent_execution_package.py:54)의 `mkdtemp()`는 `TMPDIR` 등의 영향을 받습니다. 또한 `PACKAGE_OBSERVATIONS`, `MUTANT_WITNESS`, `MUTANT_RESULT`는 지정 경로를 제한 없이 덮어씁니다. **임시 root를 `/tmp`에 고정하고 결과 파일도 해당 전용 root 내부의 새 파일로 제한해야 합니다.**

3. **P2 — mutant의 필수 witness 조건을 판정하지 않습니다.**
   [mutant:23](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/execution-package-development/src/ops/tools/tests/test_agent_execution_package_mutant.py:23)은 child 종료값·응답을 기록하지만, `0`과 `BUNDLE_VERIFIED`를 확인하지 않습니다. 종료값 `1`이나 `3`도 마지막 assertion에서 **FAIL 1 / ERROR 0**을 만들 수 있습니다. **이 두 성공 조건과 지정 assertion의 실패임을 확인해야 하며, 선행 조건 실패를 기대된 mutant 검출로 인정하면 안 됩니다.** predicate 한 곳만 우회하는 변경은 확인했습니다.

실제 읽은 SHA-256은 다음과 같으며, 소스 4개는 `preexecution-source-pins.json`과 일치합니다.

```text
agent_execution_package.py
eca4d580e0fcd60bb36f001e417aa40678d786dc86face3c504ce0a1be701ba2
build_agent_execution_package.py
992fcbfa94449e983ce21dd47acc3eaa821ad6100cf90b00043941283e81e585
test_agent_execution_package.py
a2366a84e546e63effb4b99df6427937df6d9270c1255da9a1faabbcb878b477
test_agent_execution_package_mutant.py
fe36dcab04d8dd659481e043c2bc6a7c0e7cdde8621a9bf98038a91c47e6a919

plan-review-v2.md
4b0eef2c732f24ba9dd579bc53ecf0df932d32306f4f75e83e88f294b942e105
plan-refinements-v2.md
4af3d60f188559f59e93bac3da432f1b32a96bf2ea772088e29c35a78bc5052f
source-review.md
0797e4919d94484810bc6d6d4ef1aa52a2c3a3ceff6a4aabb59accc9cee48e7b
preexecution-source-pins.json
08942178d5e7f5678cb915e37b0defd3d1d20d78fce46aee200392d56f5c0b74
```

API 대조는 Git commit `314622f0ef1abcb6821ae7cd504cc3d793021f4d`의 필요한 부분만 읽었습니다. **파일 변경, 제품·검사 실행, 모듈 import, RPC/socket/network, 인증정보 접근은 수행하지 않았습니다. 과거 검사도 반복하지 않았습니다.**
