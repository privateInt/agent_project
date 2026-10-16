# U01b1 로컬 SQLite intent journal 최종 독립 검토

판정: **PASS — 고정 제품과 범위별 실제 SQLite 검사 근거에 한정**.
미해소 finding은 발견하지 못했다. 전체5개를 최종 pin에서 다시 실행한 PASS가 아니다.

| 검토 대상 | 전체 SHA-256 |
|---|---|
| `intent-development/src/ops/tools/codex_unattended_intent_journal.py` | `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8` |
| 같은 사본 `src/ops/tools/tests/test_codex_unattended_intent_journal.py` | `a7ed452257d098cd0003d25675722d83a73fb70affc999e0ea269efc14c49efe` |
| 같은 test 디렉터리 `fixtures/codex_unattended_intent_journal/synthetic.json` | `3932939c84c22a0875e5fa8141a70869738132f32b8b68f0bccce47a80074a66` |
| `intent-development/docs/testing/codex-unattended-intent-observed-20261016.json` — review PENDING인 원문 | `839e748aa5e4c670cc97096ab478b97ff3512572f09f82cdc5b615fa87a52286` |
| 재사용 `/tmp/agent-unattended-intent-postfix-static-review.md` | `9e079c40ced11a94f7a92d0c4b2328bb58d3b5c0e988ed6b9829c69fafa0af31` |

개발 경로와 아래 실행 근거는 원 repo의 `var/unattended-20261015/` 아래다.
계획4c005dd7와 사전 PASS f1fb9dad를 유지한다. 제품은 postfix 정적 감사와 정확히 같은 pin이므로
그 소스 검토를 재사용하고, 실제 검사 추가·두 호출 수정 diff·raw log/result·runner 및 보존 기록을 읽었다.

**D06 소규모 수정으로 분류한다.** 최초 실패는 새 preflight helper가 기존 `_one(connection, sql)`에
세 인자를 전달한 검사 결함이다. 제품이 이를 불명으로 처리해 public result에는 TypeError 원문이
없다. 원인은 실제 signature와 call 대조로 판단했다. 최초 raw 실패는 main.synchronous 주입의
attempts가 비었다는 assertion이다. 초기 busy_timeout/response_after도 의도한 주입이 아닌
TypeError로 UNCERTAIN이 될 수 있으므로 그 부분을 통과 근거로 재사용하지 않는다.

실제 before8ae7→finala7ed diff는 다섯 번째 method 안의 `actual_one(target, sql, parameters)`
두 호출을 `actual_one(target, sql)`로 바꾼 것뿐이다. assertion·기대 reason·입력·제품은 그대로다.
초기 실행 test snapshot은 before-arity snapshot과 byte 일치한다. 첫4개와 두 정상 mutant target은
이 수정의 영향을 받지 않으므로 최초 PASS 재사용이 타당하다. 별도 전체 계획 변경은 필요 없다.

| 실제 실행 근거 | 관찰·재사용 범위 | result SHA-256 |
|---|---|---|
| `intent-run-1/normal-result.json` | 최초5개 중4 PASS, 다섯 번째 FAIL1/ERROR0/SKIP0. 실패 후 중단, 변이 미실행. 첫4개 PASS만 재사용한다. | `b0029abfdffc47132ae99b9e23a3ff949390d9bf784cc8708fdc342f2ab56135` |
| `intent-run-2/result.json` | coordinator가 기존 test_agent runner로 **다섯 번째만 PASS1/FAIL0/ERROR0/SKIP0**, 이어 정확2개 변이를 최초 실행했다. | `4d54222162084ab4de0504b022c84ce0c3739741c0c4b6d49261b38dee544d3b` |
| `intent-run-2/M1/result.json` | revision predicate와 대응 parameter만 제거. 지정 stale target에서 semantic FAIL1/ERROR0, 실제 잘못된 UNKNOWN/rev1 commit과 new_claim=true 확인. | `3c4f9db72be50dc7fd14cd148ac815a5a28d3eb1afb9fac1e82dcb341cb2fb16` |
| `intent-run-2/M2/result.json` | state predicate만 제거. 지정 UNKNOWN target에서 semantic FAIL1/ERROR0, 실제 잘못된 UNKNOWN/rev2 commit과 new_claim=true 확인. | `484e881b663e10aa92c7fcd659f7c3d913d0b946a8d8006605d72fec5cefef15` |

runner의 commit observer는 real COMMIT 뒤 SQL로 row를 읽고 transaction 종료를 확인했다.
두 변이는 schema/인자 오류에 가려진 실패가 아니다. 정상 단일 target PASS를 다시 실행하지 않고
최초 결과에서 재사용했다. M1/M2 밖 변이·옛 suite·전체5개 반복은 없다.

최종 다섯 번째 PASS는 preflight busy_timeout/main.synchronous의 query-before/response-after 네
하위 사례와 기존 commit 경계·두 connection 경쟁·SQL/COMMIT/rollback 오류 사례 전체를 포함한다.
잘못된 profile은 BLOCKED, SQL 응답 불명은 UNCERTAIN이며 새 claim을 보고하지 않는다.
실제 COMMIT 뒤 simulated 응답 유실의 UNKNOWN/rev1 재open 보존과 재claim 금지도 검증한다.
hostile key/equality 거부는 초기 두 번째 method의 PASS에 포함된다. 두 추가는 기존5개 안에
있으며 assertion을 약화하거나 M1/M2의 단일 목표를 바꾸지 않는다.

초기 preservation manifest의 파일 hash 검사를 읽기 전용으로 수행해 모두 일치함을 확인했다.
최초 로그·실패 원문·제품/fixture snapshot은 보존돼 있다. 기록은 기존 근거126개·원본 변경11개
보존, 첫 실패 뒤 추가 실행0을 명시한다. run-2는 source/test pin과 한정 runner diff를 유지한다.
현재 제품·최종 검사·fixture와 요약의 실행 result SHA도 실제 파일과 일치한다.

네 문서의 실제 변경분과 요약은 최초4PASS/1FAIL, helper 두 호출 수정, 해당 fifth만 재검증,
두 변이의 의미 실패 및 미완료 통합을 구분한다. 요약의 final review PENDING은 검토 전 상태다.
판정 metadata만 갱신할 수 있으며 새 요약 SHA를 기록해야 한다. 위 pin은 검토 시점 원문으로
남기고 source/test/결과·실행 범위를 바꾼 문서에 이 PASS를 소급하지 않는다.

완료 범위는 전용 임시 파일 SQLite의 borrowed connection, 명시적 빈 DB 초기화·불변 bytes/
digest/correlation·재조회/close-reopen·실제 UPDATE CAS·UNKNOWN no-replay다. 제품은 원격 IO나
실행 권한을 부여하지 않는다. 합성 데이터의 owner/provenance/서명·보호된 controller 연결·
native ACK/원격 exactly-once·전원 장애/rollback 저항·실제 두 native 개발 작업은 미인수다.
요약의 runtime 상태는 coordinator 기록이며 이번 reviewer가 runtime 조회로 재검증하지 않았다.

시간은 prior active1902초와 13:28:48 UTC 마감을 유지했고 reset하지 않았다. 기존 helper 수정은
1회로 보존한다. runtime D18 HELD·두 PAUSE·vms 제외·운영 보호는 유지하며 live 실행을 승인하지 않는다.
이번 reviewer는 이 검토 파일만 작성했다. 제품/검사/문서 수정·검사/import/DB/runtime/권한 probe·
다른 agent/result polling/wait·위임·통지는 수행하지 않았다. 다음은 coordinator의 근거 복사·게시·readback이다.
