D29 계획 `MESSAGE-CORRELATION-v1`입니다. **독립 사전 검토 전이며, 소스 구현은 이 계획의 reviewer PASS 뒤에 시작합니다.** 모델 요구값은 `gpt-6.1-sol / xhigh`로 고정합니다.

지정 파일과 기존 근거를 읽었습니다. 현재 checkout HEAD는 `2659a34386580256808880437e30828cb7b386d4`이며, controller SHA-256은 PR25 최종 검토에 기록된 `20299714f909746a5a1622b2a6445af004213f2eac6450f9fc5b4dec42767434`와 일치합니다. 추적 파일의 미커밋 변경은 없었습니다. 제품·테스트 실행/import, socket/RPC/network, 파일 쓰기, agent 생성, credential 조회는 수행하지 않았습니다.

**승인 범위와 보존 계약**

기준은 [공통 TESTING.md](docs/codex/common/TESTING.md)의 D28~D30·D32·D35·D38과 PR25의 기존 gates입니다.

- 기존 `prepare/submit/observe/record_delivery`와 저장된 intent/request/receipt/binding/terminal은 유지합니다. 자동 이행이나 과거 레코드 재작성은 없습니다.
- 새 메시지 correlation과 단계 결과 기록은 명시적 opt-in API로 추가합니다. legacy 레코드는 기존 수동 `observe`를 계속 사용할 수 있어야 합니다.
- PR25의 기본 submission disabled, 신규 durable claim 확인, lock, CLI pin, scope, schedule, PAUSE, admission, 재제출 금지를 재사용합니다.
- controller는 초기화된 기존 readonly completion client를 빌립니다. 새 연결·initialize·resume·설치·서비스 전환·PAUSE 변경은 없습니다.
- 로컬 검사에서는 명시적 fixture만 사용합니다. 실제 queue·owner RPC는 각각 0회입니다.

[기존 acceptance-summary](docs/testing/codex-owner-cli-controller-20261017/acceptance-summary.json)의 **6 PASS / FAIL0 / ERROR0 / SKIP0**와 기존 exact-prompt mutant 검출을 재사용합니다. 새 구현의 PASS로 계산하지 않으며 반복 실행하지 않습니다. 공통 helper의 실제 변경으로 과거 계약이 영향을 받으면 해당 assertion만 선정하고 이유를 기록합니다.

**메시지·단계·이력 fence 계약**

새 opt-in 요청은 다음 정확한 문자열을 queue의 단일 `--message` 인자로 사용합니다.

```text
[agent-project-task:v1:<task_id>]
<task_body>
```

새 요청의 `task_id`는 canonical UUIDv4이며 기존 intent의 task ID와 같습니다. 첫 줄 뒤에는 LF 하나만 사용하고, `task_body`는 trim·개행 정규화·Unicode 정규화 없이 보존합니다. intent의 `prompt_sha256`은 **marker를 포함한 전체 메시지 UTF-8**의 해시입니다. 이미 해시가 고정된 prompt 뒤에 marker를 몰래 붙이지 않습니다. 전체 메시지의 기존 16,384-byte 상한을 유지합니다.

제출 전 다음 사항을 durable하게 결합합니다.

- task ID 예약, intent key/hash, request hash, role/thread, exact message/hash, settings/source/plan pin.
- 완결된 pre-submit history baseline, 페이지 해시, 기존 turn/item 식별자와 사용자 입력 projection.
- baseline 기록의 해시와 신규 claim의 연결.
- 확인된 신규 claim 뒤, queue adapter 진입 전의 별도 dispatch-attempt 기록.

동일 task ID의 다른 context 예약은 충돌입니다. 동일 context 재진입은 기존 기록을 읽으며 두 번째 제출을 만들지 않습니다. 기존 레코드에 correlation sidecar가 없으면 opt-in 자동 관측은 거부하고 수동 API를 유지합니다.

baseline은 claim 전에 완결돼야 하며, 완료 후 5초 이내에 신규 claim 단계로 진입해야 합니다. baseline 실패·저장 실패·이미 존재하는 marker는 queue adapter 진입 0회입니다. claim 확인 유실은 queue 진입을 허용하지 않습니다. claim 뒤 기록 실패나 응답 유실은 UNKNOWN을 보존하고 자동 재제출하지 않습니다.

post-submit matcher는 다음 조건을 모두 확인합니다.

- 원장이 `SUBMISSION_UNKNOWN/revision=1`이고 해당 요청의 확인된 claim·dispatch-attempt·baseline 결합이 존재함.
- 지정 thread의 모든 bounded page를 끝까지 확인함. 첫 후보 발견으로 반환하지 않음.
- baseline에 없었던 turn/item에 정확한 전체 메시지가 유일하게 존재함.
- candidate는 full `userMessage`이며 단일 text content, 허용된 정확한 필드 집합, 빈 `text_elements` 조건을 만족함.
- marker를 공유하는 다른 메시지, 다른 본문, 중복 item/turn, 기존 history의 marker는 모두 충돌 또는 모호함.
- `clientId`의 null·누락·임의 literal은 메시지 토큰과 동일하다고 가정하지 않음.
- 완료 판정에는 PR25의 status/error/final-answer 검증을 추가로 적용함.

모든 페이지는 full items를 제공해야 합니다. candidate가 없는 페이지의 partial/malformed history도 유일성을 깨뜨리므로 `UNCONFIRMED`입니다. pre/post의 기존 사용자 입력 projection이 바뀌거나 baseline 항목을 재확인할 수 없으면 확인하지 않습니다. 기존 turn의 정상 status 변화와 agent output 추가는 사용자 입력 변조와 구분합니다.

**신뢰 경계:** 같은 UID는 history와 로컬 기록을 위조·재생할 수 있습니다. 페이지 해시와 pre/post 비교는 변화를 검출하는 보조 근거이며 원자적 snapshot이나 출처 인증이 아닙니다. 성공 결과는 로컬 메시지 상관관계와 turn 관측만 나타냅니다. native ACK, client-ID 동일성, authenticity, provenance를 주장하지 않습니다. 완료를 관측해도 원장의 UNKNOWN은 유지합니다.

**좁은 단계 결과 recorder 계약**

기존 `github_signed_receipts.py`의 실제 함수는 `verify_receipt_pair`입니다. 서명·artifact 해시 비교도 실행이나 독립성을 증명하지 않습니다. `github_check_audit.py` 역시 supplied snapshot의 구조 비교입니다. failure-only `github_app_check_smoke`를 production required check로 사용하거나 수정하지 않습니다.

새 recorder는 caller가 시작한 한 단계의 결과만 immutable하게 기록합니다. worker·서명키·native publisher는 만들지 않습니다.

각 기록의 필수 context는 다음으로 고정합니다.

```text
schema, stage, attempt_id, task_id,
intent_sha256, request_sha256,
repository, base_sha, head_sha, plan_sha256,
source_manifest_sha256,
origin, artifact hashes/byte counts,
process-or-tool outcome, previous-stage digest
```

- artifact 해시는 실제 받은 bytes에서 계산합니다. 선언된 해시만 저장하거나 신뢰하지 않습니다.
- 실제 test capture는 고정된 기존 runner·명시된 신규 test ID·소스 pin을 사용하는 좁은 실행 경로에 연결합니다. argv, cwd, 시작/종료, timeout/signal/returncode, 완결 여부와 raw 결과 bytes를 함께 기록합니다.
- fixture, 일반 caller-supplied 결과, 직접 포착한 로컬 process 결과, caller가 전달한 실제 tool response를 구분합니다. caller가 붙인 origin 문자열만으로 `LOCALLY_CAPTURED`를 인정하지 않습니다.
- reviewer와 Draft publisher는 실제 tool response bytes·call 참조·대상 context를 함께 보존합니다. 임의 callback의 반환값을 trusted issuer나 독립 reviewer로 승격하지 않습니다.
- 실제 reviewer의 author와 다른 실행 참조, 검토 source/head/plan, 범위와 findings를 기록합니다. `verdict=PASS`만으로 독립성을 인정하지 않습니다.
- Draft 결과는 실제 응답의 PR 번호, open/Draft, base/head SHA, 양쪽 repository를 기대 context와 대조합니다. snapshot을 이후 현재 상태로 취급하지 않습니다.
- native 역할 workflow는 계속 `NOT_RUN`입니다. 실제 standalone review, 로컬 검사, Draft 게시 근거와 별도로 표시합니다.

JSON은 duplicate key·unknown/missing key·비정상 타입·bool-as-int·비유한 숫자·과대 입력·잘림·trailing data를 거부합니다. 기대 context는 결과 artifact와 별도 인자로 받고 정확히 대조합니다.

**프로세스 종료 0이나 보고 JSON 생성은 tests PASS가 아닙니다.** 기존 runner의 실제 결과와 요청한 test ID/count가 일치하고 failures/errors/skips가 모두 0이어야 합니다. fixture의 PASS는 fixture 처리 검증으로만 기록합니다.

**필수 신규 검사: 7개 그룹**

모든 그룹은 새 private 임시 디렉터리·새 fixture DB·fake owner history를 사용합니다. 기존 DB의 init/migrate, 실제 CLI/socket·credential 접근은 금지합니다.

| 그룹 | 상황·입력 | 기대 결과와 확인할 데이터 |
|---|---|---|
| **M01 — exact message와 durable task ID** | 정상 UUID, 공백·LF·Unicode·shell 문자, byte 상한, 동일 예약 재진입, 다른 context의 ID 재사용, 예약/request 저장 실패 | queue 인자의 전체 문자열과 prompt hash가 정확히 일치. durable ID/request가 queue 진입보다 먼저 존재. 충돌·부분 저장은 제출 0회. 재개방 뒤 동일 bytes/hash 확인. |
| **M02 — claim·fence·유실·호환성** | PREPARED 관측, claim 전후 확인 유실, baseline 누락/오래됨, dispatch 기록 실패, 경쟁 호출, queue 응답 유실, legacy 레코드 | 허용된 신규 claim만 fixture adapter 최대 1회. 나머지 0회. 유실 뒤 UNKNOWN/revision/raw bytes 보존, 재진입 추가 제출 0회. 기존 수동 observe 사용 가능. |
| **M03 — 유일한 실제 메시지 매칭** | 후보가 마지막 페이지에 위치, null/누락/무관한 clientId, 본문·공백·Unicode 차이, 복수 content·추가 필드·nonempty text elements, 오래된 후보, 동일 marker/다른 본문 | 전체 scan 뒤 exact candidate 하나만 매칭. 나머지는 UNCONFIRMED이고 성공 binding/terminal 없음. clientId를 로컬 토큰으로 대체하지 않음. |
| **M04 — completeness와 mutable history** | 뒤 페이지의 두 번째 후보, 중복 turn/item, cursor cycle, malformed/partial 비후보 페이지, 4페이지 뒤 cursor 잔존, 시간/byte 초과, baseline 삭제·사용자 입력 변경 | 첫 후보로 조기 성공하지 않음. 모두 UNCONFIRMED, reason 구분, cap 준수. queue 재시도 0회. 정상 status 변화와 입력 변경을 구분. |
| **M05 — recorder schema·내구성·origin** | actual bytes와 선언 hash 불일치, context별 단일 불일치, strict JSON 오류, fixture의 local-capture 주장, callback PASS, write/fsync 실패·충돌·재개방 | 잘못된 입력을 accepted evidence로 기록하지 않음. identical 재진입만 idempotent. 기존 기록 불변. fixture/supplied/local capture 구분과 모든 trust 제한 유지. |
| **M06 — 실제 runner·review 근거 연결** | 정상 runner result, returncode 0+실패/skip/부족한 count, JSON 없음·잘림·stale artifact, reviewer PASS지만 실행/검토 context 누락·다른 head·author와 동일 | test 결과와 process 결과를 따로 판정. 명시된 7개 test ID/count·raw bytes·source/head/plan 결합 필요. reviewer PASS 단독으로 독립성·전체 완료를 만들지 않음. |
| **M07 — Draft와 단계 결합** | 정확한 Draft fixture, non-Draft/closed, 다른 repo/PR/base/head, malformed response, fixture로 바꾼 publisher 응답, 이전 단계 digest 불일치 | 정확한 snapshot만 구조적으로 기록. supplied fixture를 실제 게시로 표시하지 않음. 실제 standalone 근거가 있더라도 native workflow NOT_RUN, native/trusted-gate/merge flags false. |

M01~M05는 실제 임시 SQLite/filesystem의 저장·재개방을 검증합니다. history·publisher 응답은 fixture입니다. M06/M07 fixture 성공과 별개로, 구현 후 실행한 **실제 신규 검사 결과·실제 standalone 독립 검토·실제 Draft tool response**가 recorder에 연결됐는지 최종 reviewer가 확인해야 합니다. 실제 게시 권한은 해당 단계의 기존 사용자 지시로 판단하며 이 계획은 게시를 실행하지 않습니다.

**새 semantic mutant: 정확히 1개**

정상 7그룹 통과 뒤 격리된 source copy에서 matcher의 **`전체 scan 후 후보 수 == 1` 조건만 `>= 1`로 우회**합니다. pagination과 나머지 검증은 유지합니다.

M04의 fixture는 첫 페이지와 뒤 페이지에 서로 다른 turn/item ID를 가진 동일 exact 메시지 두 개를 제공합니다.

- 정상: `UNCONFIRMED/AMBIGUOUS`, binding·terminal 없음.
- mutant: 잘못된 매칭/binding을 실제 생성한 witness를 먼저 확인한 뒤 지정 assertion에서 **FAIL1 / ERROR0**.
- syntax/import 오류, fixture 오류, 다른 guard에 의한 차단은 검출 성공이 아닙니다.

기존 PR25 prompt mutant는 재실행하지 않습니다.

**runner와 최소 변경 파일**

기존 `test_codex_owner_cli_controller.py`의 `__main__` runner와 socket/subprocess guards를 재사용합니다. 신규 `MessageCorrelationAcceptance`의 M01~M07만 명시적으로 실행하며 discovery나 기존 `ControllerAcceptance` 실행은 하지 않습니다. Python 3.10 호환·`-B`, 정상 실행 60초 상한, mutant 15초 상한으로 계획합니다. 최초 실패와 수정 이력을 보존하고 변경 영향이 있는 실패 대상만 재검증합니다.

최소 코드 변경 후보는 다음 5개입니다.

- `codex_owner_cli_controller.py`: opt-in 준비·제출·관측과 durable sidecar 연결.
- `codex_owner_turns.py`: 별도 baseline/matcher 함수. 기존 수동 함수 계약 유지.
- 새 `codex_owner_stage_results.py`: 엄격한 caller-driven recorder와 고정 runner capture 연결.
- 기존 `test_codex_owner_cli_controller.py`: 신규 7그룹과 opt-in capture metadata 연결.
- 새 `test_codex_owner_message_correlation_mutant.py`: 지정 mutant 하나.

transport·journal·signed receipts·check audit·App smoke·설치 패키지는 변경하지 않는 계획입니다. 문서는 기존 운영 계약에 새 API·신뢰 경계를 추가하고, 새 D29/결과 근거를 보존하며 status/roadmap/handoff에는 짧은 참조만 추가합니다.

**향후 live acceptance 명세 — 실행하지 않음**

별도 live 승인과 실제 대상 pin 확보 뒤 아래 한 역할·한 요청만 사용합니다.

```text
role=test_agent
thread=01a13fb0-1721-7af3-82e6-747eac4de411
model=gpt-6.1-sol
reasoning=xhigh
repository=privateInt/agent_project
workspace=/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project
cli=/home/codex_shshin/.local/bin/codex
endpoint=/home/codex_shshin/.codex/app-server-control/app-server-control.sock
```

최종 message의 문법은 다음과 같습니다. `<task_id>`에는 새 durable UUIDv4를 한 번 대입하고, 전체 bytes/hash를 제출 전에 고정합니다.

```text
[agent-project-task:v1:<task_id>]
D29 bounded correlation acceptance. Reply exactly: D29_CORRELATION_OBSERVED
Do not use tools, edit files, run commands, or contact services.
```

queue argv는 기존 PR25의 다음 배열을 그대로 사용합니다.

```text
[cli, "queue", "--remote", "unix://" + endpoint,
 "--approve-for-me", "-C", workspace,
 "--thread", thread, "--message", exact_message]
```

- 제출: 최대 1회, 5초와 기존 cleanup 상한. 자동 resubmit 0회.
- history: pre 최대 4페이지 + post 최대 4페이지, **합계 최대 8회**.
- 요청 params: `threadId`, `cursor`, `limit=20`, `itemsView="full"`, `sortDirection="desc"`만.
- scan별 최대 20초·1MiB, 페이지당 최대 20 turns, 전체 acceptance 60초 상한.
- 새 연결/initialize/resume/poll/retry 0회. 기존 borrowed client의 deadline도 이 한도 안에 있어야 함.
- post scan은 한 번만 수행합니다. inProgress·missing·partial·ambiguous·timeout이면 인수 미통과로 종료하며 추가 조회하지 않습니다.
- 예상 최종 답변은 외부 acceptance assertion으로 대조합니다. 동일 prompt 안의 지시 이행은 일반 모델 품질이나 native 역할 workflow 통과가 아닙니다.
- 실제 head/base/plan/source pins가 없거나 PR25 runtime gates가 충족되지 않으면 실행하지 않습니다. 기존 PAUSE를 해제해 조건을 만들지 않습니다.

완료 기준은 독립 D29 prereview, 신규 7그룹 PASS/FAIL0·ERROR0·SKIP0, 지정 mutant 검출, 기존 기록 불변, 실제 standalone 단계 근거의 정확한 연결, 최종 독립 검토입니다. 로컬 완료와 live 사용 가능 판정을 구분하며, 현재 신규 검사·mutant·live·native 역할 workflow 상태는 모두 **NOT_RUN**입니다.