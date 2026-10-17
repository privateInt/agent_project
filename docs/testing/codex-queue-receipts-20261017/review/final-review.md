# QUEUE-ACK-20261017 최종 독립 검토

판정: **PASS** — 고정 ACK 로컬 단위의 검사·실행 근거·문서·보존 대조 완료. 남은 차단 finding 없음. native/owner 인수 승인은 아니다.

- Source SHA256: `24812dc20aa9e20f52ed7c920a301800c00f7568e950f1ed4a9782c1d44495af`
- Test SHA256: `730b8118103263175fbee94d30fde2fff5b5ae98d6b653e93f8da93db688fbf3`
- run-1/result.json SHA256: `3e58dc7d72443b95e0c2649a07a8c7086e47a489128519bd0c879ef54b7db3b6`

검토 대상은 `var/unattended-20261015/ack-development`, branch `codex/agent-project-queue-ack-20261017`, 실제 HEAD/base PR20 `aef3af98d1bbc9037f1b75906e0217844bb5bd1d`다. 근거는 `var/unattended-20261015/transition-evidence/queue-ack-20261017`에 있다. 이번 reviewer는 고정 source 정적 검토를 재수행하지 않고 기존 PASS를 재사용했다. 새 검사 코드·이미 완료한 실행 기록·문서·보존 및 게시 helper의 관련 경계만 정적으로 읽었다.

## 고정 기준과 실행 일치

review-plan.md와 frozen-plan-v2.md는 모두 `027cd5bda32eac67e91246b410d2177afb8a108830bde951ffeab7a0351521f7`, D29/D06 implementation-plan-review는 `bdfb9175adb149a5295adbca21a32f336d527ecc6903a2ef241d44139a12fc74`, source-static-review PASS는 `6c273a12051049e91cc6b6234c57d52dd0dccac442789a51f99e4a7511286db1`다. 현재 source/test와 실행 직전 snapshot의 hash가 위 전체 pin에 일치한다. 실행 copy manifest의 기존 dependency·support·package·상대 fixture 17개 pin을 현재 파일과 모두 대조했다. 기존 product/support 변경은 없다.

고정 response schema `c11b9772788427b19c9f9decf0765e2b41cbdead1aa9c3d39f42fdd70b339314`, synthetic fixture `6524918db5f70e3d7e3ecef2121ffcbbe6effe03200eeb37b370b9fe3075b04d`, fixture manifest `6a5a3117f704fdebf54305b9ce7529ce85470ce30387de9c004b41645009e6d0`가 실행 입력에 포함됐다. 실행기 SHA256 `aa17d7ed3547024ba816295a95dcd412d05153fde53304b7a48c786461ea6ff4`, readiness SHA256 `74a2f1e2013c97aa5c79c95634254d5f54b5bfb4bb4b9fd0e9f18c42f18077b1`도 기록과 일치한다.

## 실제 정상 4개와 검사 단언

첫 normal 실행은 **4 PASS / FAIL0 / ERROR0 / SKIP0**다. 세 번째 method 안에서 두 mutant target helper가 각각 정상 1회 PASS·peer/reopen row0였음을 marker와 코드로 확인했다. 정상 target은 변이 전 별도로 재실행하지 않았다. 이번 ACK 검사에는 새 product/test 실행 실패가 없고 기존 PASS/suite 반복도 없다. 실행 전 작성 보정은 snapshot/diff에 보존돼 있으며 old contextmanager wrapper의 원래 함수 식별, source 보존 witness 및 경계 사례 보강이다. 기대 의미나 필수 범위의 완화는 없다.

- `test_ack_candidate_store_reuse_and_reopen`(344–377행): 실제 전용 임시 intent/binding/receipt file DB의 준비·claim UNKNOWN1 후 ACK projection을 저장한다. canonical P/bytes/hash·peer row·schema markers·동일 reuse, 다른 직렬화/empty text_elements/최대 ACK bytes/queued ID conflict 뒤 원래 file과 source 보존, 세 DB close/reopen 후 동일 record를 단언한다. receipt 파일에 원문 submission/ACK/prompt·dev_root가 없음을 확인한다.
- `test_unknown_receipt_is_immutable`(379–397행): ACK 없음·허용 timeout·UNCLASSIFIED의 UNKNOWN을 각각 저장/reuse/reopen하고, 늦은 ACK나 다른 허용 code가 최초 관찰을 교체하지 못함을 확인한다. 정상 UNKNOWN 저장의 CONFIRMED는 원격 결과 확정과 구분된다.
- `test_receipt_validation_conflict_and_privacy`(399–467행): 입력 조합·builtin type/code·원문 hash·caps·strict JSON/shape/ID/text·source pins·UNKNOWN2 거부·문맥 pin/row 손상과 conflict를 검증한다. Unicode 및 줄바꿈 정규화를 허용하지 않는다. source/receipt 원본 보존도 단언한다.
- `test_receipt_database_faults_and_profiles`(469–653행): empty-only init, foreign schema/markers, 세 연결의 None/closed/active transaction 및 SQL0·caller 미커밋 데이터 보존, profile/동일 객체·main filename/memory 거부를 검증한다. D06대로 journal._one(connection, sql)의 실제 두 인자 경계에서 preflight 실패 hit를 확인한다. BEGIN/INSERT 전후·실제 readback authorizer 거부·COMMIT 전후·소유 cleanup 전후 실패를 실제 hit와 phase로 검증한다. COMMIT 이후 응답 유실은 UNCERTAIN/record=None/new_receipt=false이며 peer row1·close/reopen 조회로 P를 확인한다. cleanup 실패는 RECEIPT_CLEANUP으로 남고 fixture가 자기 transaction을 정리한다.

공통 invoke(159–301행)는 정확한 report/record keys, status/reason·commit/new_receipt/outcome 관계, API별 source closed의 null/false 구분, 모든 native/authority flag=false, profile 보존·출력 없음·privacy를 검증한다. public source read 순서와 source SQL의 DML/DDL/설정 쓰기 부재도 확인한다. old TestCase/기존 source mutation을 금지하는 guard와 old_case_calls=[] 기록을 대조했다. 기존 API의 fixture setup과 기존 support 함수 재사용을 기존 PASS 검사 재실행으로 계산하지 않았다.

## 정확히 두 semantic mutants

실제 mutation diff와 AST audit는 각각 지정 guard 한 곳만 True로 바꾸며 다른 AST가 같음을 기록한다. 정상 4개가 모두 PASS한 뒤 각 지정 helper만 1회 실행했고, **각 semantic FAIL1 / ERROR0 / SKIP0**이다. 두 raw traceback 모두 test invoke의 정확한 reason assertion(298행)에서 예상 거부와 실제 RECEIPT_STORED가 달라 실패했다. syntax/import/fixture ERROR로 성공 처리하지 않았다.

- **M_CLIENT:** 유효한 다른 client의 serialized ACK를 사용했다. source는 SUBMISSION_UNKNOWN revision1로 보존됐고, 실제 ACK_CANDIDATE/new_receipt=true/commit confirmed 및 잘못 결합된 P의 peer/reopen row가 각각 1개다. ACK bytes의 SHA 자체는 정확하다. P의 bound client·원문 ACK hash·receipt hash 관계가 helper의 expected P와 일치하고 source_unchanged=true다.
- **M_STATE:** 유효한 PREPARED revision0 binding/intent와 정상 serialized ACK를 사용했다. PREPARED0를 그대로 보존하면서 실제 ACK receipt를 잘못 commit한 peer/reopen row1과 P/hash를 확인했다. builder의 중복 state guard로 변이를 가리지 않았다.

| 단계 | 상세 result SHA256 | raw first.log SHA256 |
|---|---|---|
| normal | `08715a6b65651fdd291b9043a795bfedca2d8329b77c1c8e7c3f99e231b6bd31` | `644fd2c6fd7f767cd1448119b3c5a81ba597f202455e63edfd429264a8963e1c` |
| M_CLIENT | `f7f1a55b5375dc68ee91b0f96f48d98f09a932ed5fd144c0a6d6434cc16514ac` | `c8bc0b8770ba4ebef1fa19539960f107831c541ae06651ff6b9bfa90bac1b403` |
| M_STATE | `29cf346c081f8f7f8b9a070d75c1cdcbe53f8955ce7798fb3ea41ccbb6ce4d5f` | `dd9a3be94ae45e77d9ac179e3c1b27f405e289ebe20e3717581d0ae1c03c54a7` |

실제 보존 SQLite witness를 SQL 없이 파일 hash로 대조했다. M_CLIENT `12fa213390a85853fcac679ce3f4dc34c366be4124b61479b26d1a84928d9952`, M_STATE `02cbcf3a3b4a598c39d48ad3190293700cf31420daf94990984862ccc6e1ae8c`가 상세 JSON과 일치한다. peer/reopen의 일치·source 보존·실제 wrong commit을 요구하는 실행기 gate와 결과를 확인했다.

## 실제 범위·문서·보존

환경은 기록된 Python 3.10.12 / SQLite 3.37.2다. 세 단계 모두 새 이름의 최소 source copy에서 상대 fixture를 `-I -S -B`로 읽었다. actual dedicated temporary file SQLite·close/reopen·peer 및 주입 실패 검증이며 native protocol 입력은 synthetic serialized decoded result다. boundary_violations/old_case_calls는 빈 목록이고 보호된 원본·/opt·operator 파일 읽기 및 새 runtime 호출은 0으로 기록됐다. 실제 명령·IO guard·stage copy pins와 이 범위를 대조했다.

observed JSON과 네 문서의 새 문단은 최초 정상4 PASS·두 의미 변이 검출·기존 검사 미반복·native 미검증을 정확히 구분한다. 과거 상태 갱신 지연의 NOT_RUN 문서는 `observed-before-execution-reconciliation.json`으로 보존됐고 현재 기록은 실제 완료 결과를 반영한다. 이를 새 검사 실패나 재실행으로 세지 않는다. 검토한 문서 pin은 다음과 같다.

- docs/testing/codex-queue-receipts-observed-20261017.json: `87891ef6d2ccf3ba8609a2986b218ff73fddfd88b53f44ea7c180fdb5f3dfb84`.
- docs/HANDOFF_NEXT_SESSION.md: `b038ec56965c09ec256c324680b93b7efbc833d0063648f4ddeb1e2b1cc3cf54`.
- docs/status.md: `9eb5649b4d620b315df03969bf91867c5b7dee42c8a9c27dfb78f145d47955e8`.
- docs/roadmap.md: `cdf4a0c696a6f2b597aa01df2dcbd6c6f7a61c0d1fd69637c2afba882e335dd6`.
- docs/development.md: `fa2d996dccc5042bdbf90af3a54d6be006e77f7f1aa5aadb14954f8225b52dff`.

preservation-after-verification.json SHA256 `37c5aadeb407d7d7afc7b49f4f5f595679a931308dc1c609733114275afd927d`를 대조했다. 원본 11개 실제 파일 hash가 기존 baseline과 모두 같고 원본 root의 4 modified+7 untracked 상태·branch `codex/resume-preparation-20261015`·HEAD `b1fda4916041fde8438251b04db62e6ff702c24f`도 유지됐다. 기존 owner script `7ec43e502ed7a59e61c3abfd8949df059fb9dbb2f80657fdcd93dee08f83d259`, 보존 receipt `22e14deebb25db329fb0ebef6b22659167d2ffe06d3cbaeddd90b2fbc6a430bc`의 hash도 일치한다. 두 PAUSE와 기존 receipt의 현장 보존은 preservation 기록에 근거하며 추가 runtime 조회는 하지 않았다. 과거 PR20 및 그 이전 실패/검사/owner 관측을 수정하거나 재실행하지 않았다.

## 게시 helper의 메타데이터 반영 조건

읽은 package_unit.py SHA256는 `1e5938e8b6d8ff139c511f58e2130f3692aa75e26b3bd028eea2cee16e2cc31f`, persist_unit_publication.py는 `90dd809da52968c5a27b86c0fb6b74c0759c7b5fec7789918ebe9f5c8a4c5fa3`다. 실행하지 않았다. ack 설정은 base PR20와 전용 workspace/근거 prefix를 가리킨다. package helper 105–117행은 첫12줄의 실제 PASS·현재 source/test 전체 pin·현재 PENDING 상태를 요구한다. 이전 summary 원문을 먼저 보존한 뒤 165–168행에서 final_independent_review만 PASS/file/이 보고서 hash로 교체한다. 다른 실행 결과·code/test·fixture/native flags를 변경하는 것은 이 승인에 포함되지 않는다. 현재 네 문서는 고정 보고서를 참조하는 문구라 추가 동작 변경이 필요 없다.

이 메타데이터 반영은 검토한 code/test/fixture/실행 근거의 bytes가 유지되고 본 보고서가 그대로 보존되는 조건에서 적절하다. 새 summary hash는 publication manifest/readback으로 기록해야 한다. UTF-8 게시 묶음은 .sqlite 바이너리를 복사하지 않으므로 **원래 전용 근거 경로의 두 SQLite witness는 계속 보존**하고, 원격 묶음에 그 바이너리도 포함됐다고 주장하지 않는다. 상세 JSON의 witness hash와 위 실제 원본 hash가 연결 근거다. persist helper의 Draft/base/parent·파일 readback 및 원본 보존 gate도 후속 단계 조건이며 이번에 실행·게시한 것은 아니다.

완료 범위는 **로컬 ACK_CANDIDATE/UNKNOWN 최초 관찰의 불변 projection 검증·저장·재조회**다. decoded result의 SHA는 wire response hash가 아니며 native origin/client identity/ID demux/기한·3DB atomic freshness·owner/provenance·dispatch/replay 권한을 증명하지 않는다. live bounded serialization/error adapter·실제 send/ACK/turn/result/controller와 두 native 작업은 미구현 또는 NOT_RUN이다. 과거 true/주간14% owner quota 관측은 inherited historical evidence이며 이번 추가 조회는 0이다.

실행 시작 `2026-10-17T02:21:21.205750Z`, 완료 `02:21:22.708596Z`는 canonical 마감 **02:50:11.827731Z** 이전이며 budget/runner/environment/result 모두 clock_reset=false다. 최초 분석을 포함한 기존 예산을 연장·재설정하지 않았다. 이번 reviewer는 이 보고서만 작성했고 제품/검사 import·실행, SQLite SQL, native query/RPC/socket/queue/credential/service·다른 agent 조회/호출·게시·통지·운영/main 변경을 하지 않았다. 기존 R03/R04/R15/PR20 PASS/전체 suite 재실행 없이 old native D18 HELD·scheduler/root/user PAUSE 및 운영/VMS 보호와 조사 상한을 유지한다.
