# U01b1 intent journal 소스 독립 정적 검토

판정: **REQUEST_CHANGES — SQL 응답 불명의 분류에 관한 P2 finding 1건**.
소스만 검토했으며 검사·SQLite 실행 PASS가 아니다. 병렬 작성 중인 검사 파일·결과는
조회하거나 polling/wait하지 않았다.

| 실제 읽은 대상 | SHA-256 |
|---|---|
| 원 repo `var/unattended-20261015/intent-development/src/ops/tools/codex_unattended_intent_journal.py` | `c4c6f602df8c24657f1f9c7d582c15c2a7bb265bdec38352559c731962f7bfd8` |
| 보존 계획 `var/unattended-20261015/transition-evidence/agent-unattended-intent-plan-v1.md` | `4c005dd7c2a7703f0639ef71aed968c5a1fc5cf8b6ce404364debb779097d575` |
| 사전 PASS `/tmp/agent-unattended-intent-plan-review.md` | `f1fb9dad4822a897f13239cc5fc5fdae4734b7000240a21f056f78767cd476ee` |
| intent-development `github_fixture_ledger.py` | `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4` |
| intent-development `github_fixture_collector.py` | `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0` |

고정 소스 298행과 재사용 helper의 관련 부분을 텍스트로 읽었으며 실행하지 않았다.

## F1 · P2 · preflight SQL 오류가 CONNECTION_INVALID로 확정 분류됨

위치: journal **127–139행**, **214–228행**, report **195–208행**.
재사용 ledger의 `_connection` **144–154행**에도 같은 예외 축약이 있다.
기준 계획 **134–135행**, **155–159행**은 SQL/응답 불명을
CHECKPOINT_UNCONFIRMED/UNCERTAIN으로 구분한다.

구체적 정적 경로: 타입·연결 설정이 올바른 borrowed connection에서
`_one(connection, "PRAGMA main.synchronous")`가 SQLite I/O/조회 예외를 내면,
journal 138–139행은 그 원인에 관계없이 `_Failure("CONNECTION_INVALID")`로 바꾼다.
`_run`의 225–226행은 이를 확정된 차단 reason으로 받고 `_result`는
`status=BLOCKED, reason=CONNECTION_INVALID`를 반환한다.
busy_timeout 조회의 예외도 재사용 ledger helper에서 먼저 같은 방식으로 축약된다.

new_claim=false와 거부는 유지된다. 다만 실제로 확인한 잘못된 연결 profile과 SQL 응답을
확인하지 못한 상태가 같은 결과가 되어 승인 계획의 UNCERTAIN 분류가 사라진다.
이 경로는 BEGIN 전이며, 이미 commit한 데이터를 잃었다는 finding이 아니다.
위 입력을 실행해 관찰한 결과가 아니라 소스의 예외 경로로 도출한 것이다.

최소 보정은 로컬 journal의 preflight에서 두 원인을 구분하는 것이다. 닫힌/다른 타입/
기존 transaction/확인된 부적합 profile은 CONNECTION_INVALID로 유지하고, 실제 SQL 조회·
cursor 응답의 불명은 CHECKPOINT_UNCONFIRMED로 전달한다. 원인을 이미 축약하는 기존
`_sqlite._connection` 재사용만으로는 둘을 구분할 수 없다. 기존 ledger를 바꾸지 않고
borrowed 연결의 구조 검사와 재사용 SQL primitives를 조합해 보정할 수 있다.
필요한 검증은 기존 다섯 번째 `test_claim_commit_boundary_reopen_and_failures`의 SQL 실패
범위에서 preflight 조회 실패도 확인하는 것이다. 새 method·추가 mutant·옛 검사 반복은 요구하지 않는다.

## 그 밖에 확인한 구현

- 공개 네 API와 입력 B의 필드·canonical bytes/전체 hash/project 부분 hash, strict ID/hash/path
  검증이 계획과 맞는다. raw prompt/settings를 받지 않고 다른 원문 digest의 출처는 외부 책임이다.
  collector의 JSON helper와 ledger의 bounded query/execute/one을 재사용하며 공용 파일은 기존 pin이다.
- 연결을 열고 닫거나 파일 path를 받는 API는 없다. 초기화는 BEGIN IMMEDIATE 안에서 빈 object/
  zero marker만 받아 전용 schema/app ID/version을 만든다. 기존 정상 DB 재initialize와
  예상 밖 schema는 거부하고 나머지 API에는 migration·자동 초기화가 없다.
- reserve는 같은 key의 정확한 bytes/hash만 재사용하고 다른 결합과 correlation 재사용을
  거부한다. UNKNOWN의 reserve 재요청은 원래 상태를 그대로 반환한다. read는 원문 hash/
  canonical·키/correlation/state/revision을 재검증하며 고정 결과만 반환한다.
- claim **287–291행**의 실제 단일 UPDATE에 key+intent_sha256+expected revision+
  state=PREPARED 조건이 모두 있다. rowcount 불일치 이유는 같은 transaction에서 판별한다.
  정상 PREPARED/0→SUBMISSION_UNKNOWN/1과 UNKNOWN 재claim 금지가 보존된다.
  지정 M1/M2가 다른 선행 중복 guard에 가려지는 경로도 보이지 않는다.
- 본 transaction의 SQL/COMMIT/정리 예외는 CHECKPOINT_UNCONFIRMED로 처리한다.
  실제 COMMIT 뒤 예외가 나면 connection의 실제 in_transaction을 확인하고 new_claim=false/
  record=None으로 끝난다. 자신의 transaction만 정리하며 caller transaction을 종료하지 않는다.
  이 정적 확인은 F1의 preflight 분류와 구분한다.
- 자동 재시도·재연결·삭제·expiry·PREPARED 복귀·queue/transport/worker/settings/PAUSE
  호출은 없다. remote_io_performed/remote_mutation_authorized/input_origin_verified/native_ready는
  항상 false다. DB claim은 실제 실행 권한이나 서버 idempotency 증명이 아니다.

F1 외에 이 고정 소스에서 구체적 추가 위반은 발견하지 못했다. 검사 5개·변이 2개와 close/reopen/
경쟁·COMMIT 유실의 실제 실행 근거, 수정 pin의 최종 독립 검토는 아직 미검증이다.
실제 native/owner/서명/보호된 controller 연결·두 작업은 이번 정적 검토의 PASS 대상이 아니다.

이번 쓰기는 이 검토 파일뿐이다. 제품·검사·계획·기존 실행 근거·최초 오류·원본11개 변경을
수정하지 않았고 import/검사·DB/SQL/runtime 조회·권한 probe·socket/credential 접근·위임·통지는
수행하지 않았다. parent/child policy를 조회하거나 자신의 policy를 바꾸지 않았다.
D18 HELD·root/user PAUSE·vms 제외와 기존 운영 보호를 유지했다.
