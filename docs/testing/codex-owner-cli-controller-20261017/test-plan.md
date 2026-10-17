D29 계획 v1입니다. 기준 HEAD는 PR24의 `02724c5f7fbc0aadecebe2befdaf9f8990374869`이며 미커밋 변경은 없습니다. **계획만 작성했으며 파일 수정·import·검사·실제 CLI queue·RPC·network·설치·추가 agent 호출은 하지 않았습니다.**

기존 PR24의 7그룹·P08·독립 PASS·발행 readback은 [저장된 근거](../transition-evidence/owner-cli-controller-20261017/start-state.json)를 재사용합니다. 신규 구현에 대한 PASS로 확대 해석하지 않습니다.

**구현·검사 대상 계약**

- 새 `actualOwnerCLIController`는 기존 `reserve_intent`·`read_intent`·`claim_intent`와 caller-owned SQLite connection을 사용합니다. controller는 connection을 생성·종료하거나 기존 DB를 초기화하지 않습니다.
- 실행 조건은 **이번 호출에서 확인된 신규 claim**입니다. 기존 API의 `PREPARED → SUBMISSION_UNKNOWN` durable commit과 `new_claim=true`를 확인한 뒤에만 adapter에 진입합니다. 이미 UNKNOWN인 intent는 실행하지 않습니다. claim 직후 중단되어 실제 전송 여부가 불명확해도 재전송하지 않습니다.
- adapter의 argv는 다음 형태로 고정합니다. 경로·thread·prompt는 검증된 입력을 사용하며 shell을 거치지 않습니다.

  ```text
  [codex_executable, "queue", "--approve-for-me",
   "-C", dev_root, "--thread", role_thread_id, "--message", exact_prompt]
  ```

- `--json`·caller client-ID 옵션·queueID→turnID 변환을 추가하지 않습니다. 로컬 `clientUserMessageId`의 `LOCAL_ONLY` 표시는 별도 control record에 두고 기존 intent schema는 유지합니다. 가상 native params를 기존 ACK/binding API에 전달하지 않습니다.
- 원문과 실행 결과는 PR24 fixture state 밖의 전용 `private_control_records`에 저장합니다. 디렉터리 0700, 원문 파일 0600, 기존 파일 덮어쓰기 없이 저장·fsync합니다. stdout은 원문·SHA-256·byte count·exit 관찰값을 보존하고, stderr는 SHA-256·byte count만 보존합니다. exit 0도 native ACK나 turn 완료가 아닙니다.
- 완료 조회는 초기화된 기존 owner client를 빌립니다. owner-confirmed 실제 thread/turn/native client-ID binding이 없으면 `UNBOUND`에서 멈춥니다. **연결 대안은 owner가 해당 binding을 한 번 명시적으로 확인하는 방식 하나**로 한정합니다.

기존 [intent API](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/owner-cli-controller-development/src/ops/tools/codex_unattended_intent_journal.py:247)와 [transport 제한](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/owner-cli-controller-development/src/ops/tools/codex_daemon_websocket.py:174)을 재사용합니다. 현재 metadata scope는 `notLoaded/limit=1`만 허용하므로, full 조회가 필요하면 **기본값이 꺼진 별도 completion-read scope**를 추가합니다. 기존 default 권한은 유지합니다.

**최소 신규 검사: 6그룹**

아래는 향후 실행할 검사 기준이며 현재 결과는 모두 `NOT_RUN`입니다.

| 그룹 | 필수 입력·상황 | 기대 결과와 확인할 근거 |
|---|---|---|
| **C01 — intent와 실행 선점** | 새 intent, 동일 intent 재진입, hash/revision 충돌, 두 실제 SQLite connection의 claim 경쟁, claim 저장 확인 실패 | 정확히 신규 claim을 확인한 호출만 stub adapter에 1회 진입. 나머지는 0회. 재개방한 DB에서 intent bytes/hash·UNKNOWN·revision 보존. borrowed connection은 caller가 계속 사용 가능해야 함. |
| **C02 — CLI 결과와 private 저장** | 정확한 argv, 공백·개행·shell 문자가 있는 prompt, exit 0/비정상 exit, timeout·응답 유실, stdout/결과 파일 저장 실패, claim 후 중단·재진입 | argv 요소와 prompt bytes가 정확히 일치하고 shell 사용 0. 허용된 첫 시도만 stub 호출 1회, 재진입 추가 호출 0. exit 0도 ACK/완료로 승격하지 않음. 실패·유실은 UNKNOWN 보존. 파일 권한·hash·count·재개방 보존 확인. stdout/stderr/예외의 secret sentinel은 공개 출력에 없어야 함. |
| **C03 — completion 조회 권한과 요청** | 명시적 completion scope, scope 없는 기존 metadata client, 다른 thread·추가 params·잘못된 cursor/limit, 금지 method | completion scope에서만 고정 thread의 `thread/turns/list`와 필요한 `thread/items/list` 허용. fake transport의 직렬화된 요청을 확인. 기존 metadata scope에서 full/items 요청은 계속 거부. resume·settings·queue mutation·재연결·controller의 initialize 호출은 0. |
| **C04 — 실제 turn 식별** | binding 없음, 정상 binding, thread/turn/client-ID 각각 불일치, clientId null/누락, prompt 한 글자·공백·개행 차이, 복수 matching userMessage | binding 없으면 UNBOUND이며 조회 0. 정상 사례만 완료 후보가 됨. `userMessage.clientId`·exact prompt·turnID·thread-bound request 중 하나라도 불일치하면 완료 불수락. 로컬 ID나 queueID는 대체 식별자가 될 수 없음. |
| **C05 — pagination·상태·최소 결과** | 여러 page의 정상 종료, 미완결 page, cursor 반복, page/시간/byte 상한, malformed 응답, 중복·충돌 turn, `summary/notLoaded`, 네 가지 turn status, 저장 실패 | 완결된 유일 matching turn의 `completed`만 `TURN_COMPLETED`로 기록. timeout·partial·ambiguous·`inProgress`·`failed`·`interrupted`를 구분하며 성공으로 합치지 않음. 최소 결과의 private 저장까지 확인되지 않으면 결과 저장 완료를 주장하지 않음. UNKNOWN intent는 그대로 보존. |
| **C06 — tests/reviewer/Draft PR 반영** | completed만 존재, 검사 FAIL/누락, reviewer 누락·독립성 미확인, 오래된 head·다른 repo/PR·non-Draft, artifact/binding 불일치, 일치하는 명시적 fixture | turn 완료와 tests PASS를 별도 기록. 외부 검증 근거가 부족하면 반영 대기 상태. 유효 fixture는 연결·비교 동작만 검증하며 `gate/integration complete`, native ready, merge 권한을 만들지 않음. 기존 PASS를 새 head의 검증으로 사용하지 않음. |

C01·C02·C05는 **실제 임시 SQLite와 실제 filesystem**을 사용합니다. CLI 실행과 owner-client 응답은 **명시적 stub/fixture**입니다. 이를 실제 CLI/native 연동 PASS로 표시하지 않습니다.

C03~C05의 조회 기준은 고정된 0.162.1 cached schema와 기존 projection입니다.

- 기본 요청은 `thread/turns/list`에 `threadId`, `cursor`, 제한된 `limit`, `itemsView="full"`, 고정 `sortDirection`을 명시합니다.
- 응답의 `data/nextCursor`를 검증하고 완결 전에는 완료를 수락하지 않습니다. pagination 누락·반복·상한 초과는 불완전 조회입니다.
- `itemsView` 생략은 cached schema의 기본값 `full`에 따라 처리하고, 명시적 `summary/notLoaded`는 partial로 처리합니다.
- `thread/items/list`를 사용하면 요청의 `turnId`를 고정하고 모든 `ThreadItemEntry.turnId`와 item pagination을 검증합니다.
- 응답에 없는 thread echo 필드를 발명하지 않습니다. 기존 RPC 요청/응답 ID 연결과 검증된 요청 thread scope를 사용합니다.

C06은 우선 기존 [`audit_bound_checks`](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/owner-cli-controller-development/src/ops/tools/github_check_audit.py:173)를 재사용합니다. 서명 입력이 실제로 제공되는 경로에서는 기존 `verify_receipt_pair`를 재사용할 수 있습니다. 두 API 모두 입력 출처·실제 실행·reviewer 독립성·gate 신뢰를 자체 입증하지 않는다는 기존 의미를 유지합니다. Draft PR의 repo/head/base/Draft 여부는 별도 외부 검증값으로 받아 대조합니다.

**의미 변이 1개**

격리 소스 복사본에서 **exact prompt 비교만 제거**합니다. thread·turnID·native clientId·pagination·completed status는 모두 맞고 prompt만 다른 fixture를 C04에 제공합니다.

기대 결과는 지정 assertion의 `FAIL 1 / ERROR 0`입니다. 변이 코드가 실제로 잘못 완료를 수락한 control record를 남겼다는 증거까지 확인합니다. 단순 강제 실패·import 오류·fixture 미진입은 변이 검출로 인정하지 않습니다. 공개 근거에는 record hash와 판정만 남깁니다.

**향후 runner와 실패 보존**

- runner·소스 복사본·새 DB·private records는 전용 `/tmp/agent-owner-cli-controller-d29-<unique>/` 아래에만 둡니다. 결과는 존재하지 않는 새 파일로만 생성하며 repo·기존 evidence를 덮어쓰지 않습니다.
- Python 3.10 호환, `-B`, 새 6그룹의 명시적 목록과 변이 지정 대상만 실행합니다. discovery·전체 suite·기존 7/P08/R03/R04/R15 반복은 없습니다.
- 새 빈 임시 DB만 기존 설정/초기화 API로 준비합니다. 기존 두 DB의 init/migrate는 호출하지 않습니다.
- 실제 subprocess/CLI·socket/network 경계에는 금지 guard를 먼저 설치하고, 명시적 대역 이외 진입은 검사 실패로 처리합니다. 실제 credential·설치 경로·service·scheduler·PAUSE는 접근하지 않습니다.
- 최초 FAIL/ERROR·소스/계획 hash·환경·소형 실패 근거를 보존합니다. 보정 후에는 실패 대상과 영향받은 신규 검사만 재검증하고 이미 통과한 그룹은 반복하지 않습니다.
- 향후 통과 기준은 새 6그룹 모두 PASS, FAIL/ERROR/SKIP 0, 지정 변이 검출, live/native 호출 0입니다. 독립 사전 계획 검토와 최종 구현 검토는 별도 단계이며 이번 응답에서 수행됐다고 표시하지 않습니다.

현재 남은 CLI→turn 연결은 **미연결**입니다. 이 계획의 오프라인 검사가 통과하더라도 실제 CLI/native 인수와 tests/reviewer/Draft PR의 외부 검증이 끝나기 전에는 통합 완료로 판정하지 않습니다.
