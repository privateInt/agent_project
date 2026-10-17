**REQUEST_CHANGES**

7개 파일의 핀은 모두 일치합니다. 다음은 정적 검토에서 확인한 필수 수정 사항입니다.

- **[P1] M07의 `missing-review`가 ERROR로 끝납니다.** [test_codex_owner_message_correlation.py:780](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/message-correlation-development/src/ops/tools/tests/test_codex_owner_message_correlation.py:780): 없는 reviewer 파일을 읽으면 `FileNotFoundError`가 발생하지만 `rejected()`의 236행은 이를 처리하지 않습니다. 해당 부재를 명시적으로 검사하거나 제품 API에서 도메인 오류로 변환해야 합니다.

- **[P1] 비후보의 malformed 사용자 입력을 허용합니다.** [codex_owner_turns.py:150](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/message-correlation-development/src/ops/tools/codex_owner_turns.py:150): `content=[{"type":"text"}]`도 완전한 이력으로 인정됩니다. 다른 페이지에 정상 후보가 있으면 binding·terminal까지 생성할 수 있습니다. 모든 사용자 입력의 필수 구조를 검증하고 불완전 입력에서는 `UNCONFIRMED`여야 합니다.

- **[P1] 실행 소스 pin의 필수 범위가 없습니다.** [codex_owner_stage_results.py:119](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/message-correlation-development/src/ops/tools/codex_owner_stage_results.py:119): manifest에 테스트 파일 하나만 있어도 허용합니다. 실행되는 제품 모듈·legacy helper·fixture 코드가 누락되면 변경을 검사하지 않은 채 `TESTS_PASSED`와 게시 소스 일치를 기록할 수 있습니다. 실행 의존 파일의 필수 목록을 강제해야 합니다.

- **[P2] 5초 fence가 claim 시점에 보장되지 않습니다.** [codex_owner_cli_controller.py:241](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/message-correlation-development/src/ops/tools/codex_owner_cli_controller.py:241): 검사 후 admission 저장과 claim commit이 지연돼도 재검증 없이 dispatch합니다. claim 경계에서 만료를 다시 확인하고, 이미 claim됐다면 UNKNOWN을 유지하며 queue 진입을 차단해야 합니다.

- **[P2] Draft JSON의 비유한 수·잘못된 타입을 거부하지 않습니다.** [codex_owner_stage_results.py:227](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/message-correlation-development/src/ops/tools/codex_owner_stage_results.py:227): 예를 들어 정상 응답에 `"additions":1e9999`를 추가하면 decoder는 무한대로 변환하고, 후속 검사는 이를 무시하여 `DRAFT_RECORDED`로 인정합니다. recorder에서 비유한 수와 허용 필드의 비정상 타입을 거부해야 합니다.

제품·테스트·mutant 실행/import, DB·socket·network·RPC 접근, 파일 쓰기, agent 호출은 수행하지 않았습니다.