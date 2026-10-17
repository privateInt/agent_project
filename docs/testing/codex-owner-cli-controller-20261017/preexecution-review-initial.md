**REQUEST_CHANGES**

- [controller:152](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/owner-cli-controller-development/src/ops/tools/codex_owner_cli_controller.py:152): fixture CLI·endpoint는 문자열 경로만 검사합니다. symlink로 실제 CLI/socket을 가리킬 수 있고, 216행의 CLI `resolve()` 결과도 전용 tmp 범위인지 재검사하지 않습니다. 실제 해석 경로의 범위 검증이 필요합니다.
- [runner:282](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/owner-cli-controller-development/src/ops/tools/tests/test_codex_owner_cli_controller.py:282): `socket.socket` 패치 전에 정의된 [기존 socket 하위 클래스](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/owner-cli-controller-development/src/ops/tools/codex_daemon_websocket.py:23)는 원래 생성자를 호출하므로 guard를 우회합니다. 일반·변이 runner 모두 `_DeadlineSocket` 생성도 차단해야 합니다.

SHA256: 지정 **7/7 일치**. 관련 prefix: controller `7d1b15a670f0`, runner `754bcdcdf6da`, transport `112a53e20996`, mutant `b46c8b47bc2e`.

읽기 검토만 수행했습니다. 테스트·import·쓰기·RPC·network·추가 agent **0**.
