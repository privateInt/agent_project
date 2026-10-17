# D06 byte-path runner 수정 독립 검토

판정: **PASS — D06 경미 harness 보정이며 B3와 지정 두 변이만 계속 실행하는 범위가 적절하다.** 검사 기대값·제품·테스트 소스·IO 허용 범위의 완화는 없다. 이 판정은 수정 실행기 범위 승인이고 B 검사/변이 실행 PASS나 최종 독립 검토가 아니다.

| 확인 대상 | SHA-256 |
|---|---|
| 정확한 run-2/run-once.py | `ba73c78dc87001825fc84c41f40e032efb2f004f17ee042ebd1f909a0ff87d38` |
| 최초 run-1/run-once.py | `065cc786bd77d96c8c119053441c6e3e3a2bbcafb404e3e4b7e2644123b92fa3` |
| 최초 normal/result.json | `cfee9e4c24162d8154ce64bdfbd68e668e28f37dbdcc2904ba71ed003273d5d3` |
| 최초 normal/first.log | `7ed9fef9b1d6579126a08e8cf1cd9b128a2fd1b423724c8586fe2ca0011791cc` |
| 제공된 runner-byte-path-fix.diff | `ad556f5d25cbbec99261026b7ba8acc711104a2de70c043050013ca4d31693b4` |
| quota 제품 / 불변 | `699f3a6e8993f28d88d7413ac51197a144b6d8b69a01f321da11a17398a4a23c` |
| binding 제품 / 불변 | `1663ab454e0f66a5e7f23382b9cf4c047068a0c86f10d895c4ad19a4d57f2a3c` |
| quota 검사 / 불변 | `7e57cdcef6f3ef48c975e9de9d494b34018e97f14b90478a273ae1820b8fb476` |
| binding 검사 / 불변 | `4190ad0152637cb06d5276bd94682a767def9ee8da9f7dbc86ee31930569a117` |
| 최초 tests-ready.json | `c6f8baccc419386453025532d674b1abfbfd5c2dead9267153d45699c08cac6c` |

처음 normal raw log와 JSON은 quota 세 method PASS 후 B1 database fixture의 sqlite3.connect audit에서 bytes/str startswith TypeError가 발생했음을 함께 보여준다. binding API에 도달하지 않았고 binding_observations와 sqlite_connections 기록도 비어 있다. 최초 결과는 testsRun4/FAIL0/ERROR1/SKIP0이며 성공한 전체6개로 바꾸지 않는다. 위 cfee pin은 run-1/normal/result.json이고 상위 run-1/result.json의 요약과 구분한다. 별도 coordinator-runner-generation-first-error.json의 SyntaxError도 생성 단계의 오류로 보존돼 있으며 그 생성 시도의 제품/검사 실행0과 최초 normal 실행 결과를 혼동하지 않는다.

실제 run-1→run-2 diff와 새 실행기 경로를 확인했다.

1. **표현 정규화:** sqlite3.connect audit target을 os.fsdecode(os.fspath(args[0]))로 바꾸는 한 줄이 bytes와 str을 같은 filesystem 문자열 경계로 맞춘다. abspath·temporary DB prefix 검사와 기존 :memory: invalid-case 허용은 그대로다. 경로를 무조건 허용하거나 검사 예외를 무시하는 변경이 아니다. 원본 checkout·/opt·operator 파일·socket/process 거부와 BaseException boundary도 유지된다.
2. **재실행 방지:** 실행 전 최초 testsRun4/FAIL0/ERROR1/SKIP0/경계 위반0, PASS/PASS/PASS/ERROR 순서 및 현재 두 제품·두 검사 pin의 최초 copy pin 일치를 요구한다. 실제 최초 method ID도 Q1/Q2/Q3/B1과 일치한다. 현재 pin은 보존 readiness/amendments-1 기록과 맞는다. 성공한 quota 세 method는 normal suite에서 제거해 재사용하고, B 세 method는 동일 assertions로 실행한다. 실패 B1 재시도와 아직 수행하지 못한 B2/B3의 최초 실행을 구분한다.
3. **B normal gate:** cases가 B3만이므로 기대 count/record를3으로, unknown helper marker 위치를 cases[1]의 B2로 바꾼 것은 정확하다. B2 helper는 UNKNOWN1 거부·row0·원래 snapshot 보존 assertion을 모두 지난 뒤에만 marker를 true로 만든다. B3 전체 PASS와 이 marker 전에는 변이를 진행하지 않는다. 최종 정상 범위는 최초 quota3 PASS+새 binding3 PASS의 서로 다른6개이며 전체6개 재실행이 아니다.
4. **지정 변이 유지:** M_Q는 최초 Q3 PASS를 재사용하고 변이 Q3만 한 번 실행한다. M_B는 새 B2 내부 helper의 정상 PASS 뒤 지정 helper만 한 번 실행한다. 유일한 AST target·다른 AST 동등성·실제 잘못된 allowed 또는 committed binding witness·정확한 semantic assertion의 FAIL1/ERROR0/SKIP0 gate는 변경되지 않았다. report flag/import/SQL/harness ERROR를 검출 성공으로 낮추지 않는다.
5. **한 번의 계속 실행:** run-2 exclusive attempt marker와 별도 snapshot/log/result를 사용하며 원본 run-1을 덮지 않는다. 정상 또는 변이의 비예상 FAIL/ERROR/SKIP 시 중단하고 자동 수정·반복은 없다. source copy·상대 fixture·Python -I/-S/-B worker와 원래 마감의 남은 시간 제한도 그대로다.

이 조정은 첫 정상6개의 검증 범위를 삭제하거나 선택으로 낮추지 않고, 변경 없는 quota PASS를 재사용해 미완료 단계만 계속하는 것이다. D06·D07의 독립 경미 수정으로 분류한다. 총괄의 다음 범위는 위 exact runner/pins로 B3 및 gate 이후 정확한 두 변이를 각 한 번 수행하는 것으로 한정된다. run-2 readiness의 일부 최초 작성 설명에 “first six”가 남아 있어도 실제 suite는 B3이며 최종 근거는 3+3 재사용과 최초 ERROR를 분리해 기록해야 한다.

검토자는 실행기·제품/검사를 import/실행하거나 SQLite/RPC/queue/service/다른 agent를 호출하지 않았다. 읽기 전용 code/diff/log/pin 검토와 이 보고서 작성만 수행했다. 이번 판정은 B 실행 결과·최종 인수 PASS를 뜻하지 않으며 native 호출은 범위 밖이다. 실제 결과 후 최종 독립 검토가 별도로 필요하다. 기존 실패·PASS·원본 변경·PAUSE·native D18 HELD·조사 상한과 원래 마감 2026-10-17 01:57:31Z를 유지하며 예산을 재설정하지 않는다.
