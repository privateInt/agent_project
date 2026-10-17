# Native metadata 고정 runner·wrapper 실행 전 독립 검토

판정: **PASS** — 아래 exact pins의 오프라인 새4→지정변이2 최초1회 실행 gate에 대한 정적 판정이다. reviewer는 import/검사/helper/진단을 실행하지 않았다.

- source SHA256: `6f704fc2d308406eb27e614e88cfb0bb4c317f8f6c695afa48319bdc5d402432`
- test SHA256: `e6c485f1e1852ca365c70cdc5986478fee3f8619182dd00a19b8ed1d1589d3ac`
- run-once.py SHA256: `d1437cbbe0733a65968c49461e62f9e99cd13397cb5ed3cc3d781d7a6a83778c`
- tests-ready.json SHA256: `0340218b94bc743e263289ccc7087de66ca3d794579b93d90c11783699f2eaec`
- execute-and-checkpoint-once.py SHA256: `7666553fce9fc3347e7b4815e9cb8e2ffe41f945ec34e7371627a775ee1fb9a7`
- 고정 단위 마감: **2026-10-17 04:00:00Z**. 조사 CLOSED·native0·scheduler PAUSE를 유지하며 시간/시도 재기산은 없다.

**재사용과 exact pin:** 기존 source-review, test-static-review, packaging-review와 packaging-pin-correction-review의 판정을 재사용했고 본문 코드 검토를 반복하지 않았다. 선언된 workspace source/test/package init/fixture8개 및 복사된 websocket dependency31개를 파일 hash만으로 대조해 모두 일치함을 확인했다. ready의 plan/addendum/review/source-ready/isolation/원본/diff/audit/environment 등 evidence13개 pin도 실제 파일과 일치한다. fixture·primitive hash는 앞선 정적 검토본과 동일하다. 실제 Python executor 환경 대조는 runner의 sys.version/sys.executable gate가 실행 전에 수행하며 이번 reviewer가 환경 probe로 재확인하지 않았다.

**runner gate/실행 순서:** 명시 --source-ready와 exact source CLI pin, runner 자신의 hash, 결합 계획 및 source-review PASS, source-ready/guard 위치, 정확4 methods AST와 모든 복사 bytes를 확인한다. 실행-attempt exclusive marker와 initial-snapshot을 만든 뒤 매 stage마다 별도 fresh renamed minimal-source 및 pinned vendor를 사용한다. worker는 -I -S -B이며 원본/site/dist-packages 경로를 제거하고 새 fixture 상대 경로 및 vendor 실제 위치를 확인한다. explicit METHODS 순서의 정상4만 failfast로1회 수행하고 FAIL/ERROR/SKIP이면 변이에 진입하지 않는다. helper2 정상 PASS/각1회/send0도 요구한다. 이전 TestCase·구 suite를 호출하거나 discover하지 않는다.

**변이2 의미 gate:** exact Compare 하나를 True로 치환하고 다른 AST/bytes 동등성과 고정 guard 위치를 검사한다. 각 isolated mutant에서는 지정 helper 하나만1회 실행한다. actual masked wire를 다시 decode하고 bytes/hash·method·params·ID1/next_id2·request1/connection1을 대조한다. 지정 _blocked_mutation_target의 정확 assertion 위치에서 AssertionError FAIL1/ERROR0/SKIP0 및 실제 includeTurns=True 또는 builtin int limit2의 잘못된 송신이 함께 있어야 검출 성공이다. import/다른 assertion/fixture ERROR나 정상4 반복으로 대체하지 않는다. 원본 정상 helper PASS는 재사용한다.

**IO guard:** worker의 BaseException audit가 socket/subprocess/exec/spawn/fork/DB/환경 변경을 차단한다. open/listdir/scandir와 metadata 접근은 fresh copy·정해진 stage evidence·stdlib 범위만 허용하며 원본 checkout·/opt·그 밖의 credential/운영 경로를 거부한다. 복사 source는 읽기만 허용한다. 파일 변형 계열도 차단하고 fixture의 fake AF_UNIX superclass 및 thread/sleep 금지와 결합한다. 위반은 오류 근거로 보존하며 PASS가 되지 않는다. fixture cleanup과 모든 fake socket close도 정상 통과 조건이다. 실제 product WebSocket encode/decode를 쓰는 fake 검사이며 native socket/RPC는0이다.

**최초 실패·마감:** exclusive execution marker/새 stage 디렉터리/first.log 및 initial source/test/fixture/dependency/review snapshot을 사용한다. harness/controller 최초 오류는 별도 log/JSON으로 보존하고 실패 stage에서 중단한다. 각 stage 결과와 누적 result를 저장하며 자동 수정·재실행은 없다. 부모 remaining timeout과 worker SIGALRM은 같은04:00Z 절대 마감이며 재기산하지 않는다. 실패 시 기존 raw 근거와 정상 PASS를 보존하고 별도 원인 검토 전 계속 실행하지 않는다.

**wrapper:** CLI로 받은 이 보고서의 full SHA와 첫12줄 PASS를 확인한 뒤 source/test/run-once/tests-ready 실제 full SHA가 보고서에 있는지 대조한다. ready command는 고정 source pin의 run-once 명령과 정확히 일치해야 한다. 남은65초 초과·기존 execution-attempt/result 없음·exclusive coordinator marker와 stdout/stderr 조건에서만 명령을1회 실행한다. timeout은 최대60초이며 retry 경로는 없다. 먼저 관측 summary 원본을 exclusive 보존한다. 종료 후 result hash와 정상4/변이2 gate를 이용해 execution-checkpoint를 저장하고, 같은 invocation 안에서 관측 JSON과 ROOT의 로컬 state/action-required를 갱신한다. 현재 summary는 PENDING이고 ROOT state의 native_metadata_unit은 object라서 필요한 입력 shape도 맞는다. final_independent_review=PENDING과 native 미검증 상태를 PASS로 바꾸지는 않는다. 이 절차의 실제 저장 성공 여부는 실행 후 근거로 확인해야 한다.

정확한 최종 실행 후보는 아래이며 coordinator가 보고서 저장 후 계산한 실제 full SHA를 마지막 인자에 넣는다. report 자체 hash를 report 본문에 넣는 순환 참조는 만들지 않는다. reviewer는 이 명령을 실행하지 않았다.

```sh
python3 -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/transition-evidence/native-metadata-run-1/execute-and-checkpoint-once.py --preexecution-review-sha256 <이_보고서의_실제_SHA256>
```

미해결 실행 전 정적 finding은 없다. 이 PASS는 실제 검사 결과·제품 최종 독립 검토·발행/readback·수정본 native 인수의 PASS가 아니다. 실행 뒤 정상4·변이2/실제 wrong-wire·최초 오류/원본 보존·문서/상태의 독립 대조가 별도로 필요하다. 기존 native v3 현장 성공을 수정본 native acceptance로 표시하지 않는다. 이번에는 정적 읽기·파일 hash 대조와 이 보고서 작성만 수행했으며 기존 변경/실행·최초 정적 finding·원본11/운영/auth 보호는 그대로다.
