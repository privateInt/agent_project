# U01b2 D29 독립 사전 검토

판정: **PASS — 지정 v2 계획의 제한된 오프라인 구현은 가능하다.** 구현을 막는 계획 결함은 발견하지 않았다. 제품·검사 실행 PASS나 최종 독립 검토를 대신하지 않는다.

- 대상: `var/unattended-20261015/transition-evidence/agent-unattended-correlation-plan-v2.md`
- 정확한 SHA-256: `25c798b43efd07b01c8c9030c0de7ff41a04b94cab8dd62dc4462ed13025403f`
- 고정 PR18: `780d0755378b9f95f0282ec766ca99300b2b0b53`
- 검토 시각: 2026-10-16 15:02 UTC. 공유 시작 14:45:08Z와 보수적 마감 15:45:08Z를 유지하며 시계를 재설정하지 않는다.

`intent-development` HEAD와 아래 세 worktree/commit 원문 해시의 일치를 읽기 전용으로 확인했다.

| 소스 | SHA-256 |
|---|---|
| `codex_unattended_intent_journal.py` | `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8` |
| `github_fixture_ledger.py` | `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4` |
| `github_fixture_collector.py` | `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0` |

보존된 generated schema 다섯 개의 해시도 계획과 일치한다. 실제 정의에서 add의 thread/client/input, list 요청의 thread, 응답의 data와 선택 nextCursor를 확인했다. QueuedSubmission에는 thread가 없고, add/list의 UserInput 및 ACK/list의 QueuedSubmission 정의는 같다. 명시적 끝 cursor와 single-text/빈 text_elements만 허용하는 엄격한 부분집합은 이 schema와 양립한다.

검토 결과:

1. 기존 공개 `read_intent`의 확정 read/commit 결과를 한 번만 소비할 수 있다. 전용 borrowed 연결의 기존 읽기 transaction 외에는 직접 SQL·연결 생성·경로 접근이 필요 없다. 원래 B 검증과 SQL 불명 처리는 재사용하며, BLOCKED와 JOURNAL_UNCONFIRMED를 구분한다. PREPARED는 차단하고 어떤 결과에서도 UNKNOWN/revision/raw/hash를 변경하거나 claim을 재개하지 않는다.
2. builtin type 선검사, 원문 hash 후 decode, 중복 key/BOM/float 거부, bounded JSON과 byte 제한이 구체적이다. 원래 client 및 prompt UTF-8 hash를 B에 묶고, 각 요청의 thread/limit/cursor를 순서대로 검사한 뒤 완결 목록의 유일 후보만 반환한다. 응답 유실·생략된 nextCursor·중복/다중 후보·불완전 pagination에서 부분 후보를 버리며, 빈 목록도 확정 실패로 승격하지 않는다.
3. sanitized hash manifest는 원문 순서와 유실을 보존한다. candidate는 제공된 자료의 후보일 뿐이다. 저널에 없는 thread/params의 사전 영구 결합이나 응답 출처를 인증하지 않으며, origin/acceptance/native-ready/원격 변경/replay 권한은 계속 false다. queue/add/list/start 실행이나 durable observation 저장도 범위 밖이다.
4. 정확히 네 새 method에 정상·오류 mapping·읽기 전용 보존·close/reopen·경계·pagination·sanitization이 배정돼 있다. 전체 80 items는 4×20의 도출 상한이므로 81 입력이 page/item 제한에서 먼저 거부되는 것도 정상이다. 별도 독립 80 초과 guard나 추가 method가 필요하지 않다. M1은 유효 pin/client/thread를 유지한 다른 prompt, M2는 정상 두 page의 서로 다른 두 queue ID로 다른 guard의 가림을 피한다. 첫 네 검사 PASS 뒤 지정 target만 각각 한 번 실행하여 잘못 반환된 실제 candidate를 semantic FAIL1/ERROR0으로 확인한다는 계획이 타당하다. 이 검토에서는 어느 검사도 실행하지 않았다.

외부 선행조건은 후속 live 연결에 남는다. 승인된 runtime 담당자가 대상 thread·현재 protocol·허용된 읽기 범위를 확인하고, caller/controller 담당자가 원래 intent/pin과 제출 params 및 RPC ID로 대응된 list 요청/응답의 출처를 입증해야 한다. 실제 인수 담당자와 reviewer는 consumed/유실/변동 pagination의 한계를 별도 평가해야 한다. 이러한 native/owner/provenance 근거의 부재는 이 비권위적 로컬 관찰기 구현을 막지 않으며 live 인수를 통과시켜 주지도 않는다.

버전 설명 주석: 현재 보존된 v1 SHA-256은 `13a71687cf79b0fb42d2abd3b7a245e48ac621cfe11490d08fc43a923e88d6f7`이다. 현재 v1↔v2 diff에는 정책·시간 외에 외곽 key/JSON 오류 분류 및 도출 상한 검사 명확화 문구의 차이도 있다. 따라서 두 줄만 바뀌었다고 전제하지 않고 지정 v2 전체를 검토했다. 이 차이는 위 오프라인 범위의 구현 차단 사유가 아니다.

정책 사실은 제공된 부모 검증 결과로 취급했으며 재조사하지 않았다. runtime D18 HELD, scheduler/root/user PAUSE, vms 제외와 기존 조사 상한은 유지된다. 제품·검사·계획 수정, import/검사/DB/native/runtime 실행, 권한 probe, 다른 agent 조회·위임·통지는 수행하지 않았다. 이 검토 보고서만 새로 작성했으며 기존 결과·최초 오류·원본11에는 쓰지 않았다.
