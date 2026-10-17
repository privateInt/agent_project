# Owner quota reader · durable prebinding 독립 경계 사전 검토

상태: **PRELIMINARY_ONLY — D29 frozen plan 미검토, 구현 시작 승인 아님.** 최초 확인 2026-10-17 01:03:17 UTC에 지정 owner-binding-20261017 경로가 없었다. 계획 완료 여부를 반복 조회하거나 기다리지 않았다. 정확한 frozen plan/API/검사표/변이 target에 대한 별도 D29 PASS가 구현 전에 필요하다.

기준은 PR19 `ddec1931eed8828cdd84ae783845729edf617fc1`이다. binding-development의 실제 HEAD가 이 값이고 미커밋 변경이 없음을 읽기 전용으로 확인했다.

| 읽은 근거/소스 | SHA-256 |
|---|---|
| `owner-run-analysis-20261017/observed.json` | `b672de17eba0e4e38d2404dae1b0dbf02b997f679cb0772fe599b752e1f8756c` |
| `quota-read-once-20261017.json` | `57a25a332bf29dfe3eabaee2817a1be5d75dd8114177b6a82bac8d51a9e0825c` |
| 기존 intent journal | `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8` |
| PR19 queue observer/parser | `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a` |

오류 분석: 과거 owner 작업은 첫 세 RPC 응답 뒤 thread/resume에서 SESSION_WRITER_CONFLICT가 발생한 기록이다. 서버 수신 근거는 보존된 서버 로그 분석에 있고, receipt에는 완료 응답과 정확한 client/numeric error code가 없었다. excludeTurns=True라도 resume이 session writer 초기화를 시도했다는 실제 분석을 유지해야 한다. 이 과거 결과를 새 quota-only 성공으로 지우거나 재실행해서는 안 된다.

최신 보존 quota receipt는 initialize와 account/rateLimits/read 두 응답, initialized notification1, ordinaryUsageAllowed=true와 주간 사용14%를 기록한다. 이전 false/100%와 충돌 기록은 별도 시점의 과거 사실이다. 새 응답도 관찰 시점의 quota 정보이며 thread writer 충돌 해소·현재 실행 권한·native admission을 입증하지 않는다.

**A · injected existing-owner client reader: 로컬 구현 가능.** 구현이 직접 socket/transport 연결·credential·subprocess·thread/queue/service를 소유하지 않고, caller가 제공한 client의 initialize/initialized/account-rateLimits-read만 호출하는 한 독립 작업을 막는 외부 선행조건은 없다. binding-development에는 해당 transport 파일이 없으며 이를 이유로 transport 설치나 경로 의존을 추가할 필요가 없다. connection·실제 client/source pin·deadline·owner 승인과 연결 종료는 caller 책임이다.

D29에서 고정해야 할 A 계약:

- client는 해당 시도의 initialize가 아직 수행되지 않은 caller-owned 연결이어야 한다. 이미 초기화된 연결을 재초기화하거나 실패 후 재사용·재연결·재시도하지 않는다. 정상 최대2 RPC와 initialized notification1, 각 단계 실패 시 이후 호출0을 기대값으로 정한다.
- ordinaryUsageAllowed는 strict bool True만 허용 관찰이며 False와 누락/잘못된 type/불명 응답은 fail closed다. 관찰된 허용은 native/controller 실행 허가가 아니다. 전송·전체 deadline을 이 부품이 직접 검증했다고 표시하지 않는다.
- 고정 phase와 allowlist error code/기본 unknown code만 반환한다. 임의 exception 문자열·code 속성·account/response 원문을 복사하지 않는다. call_entered는 클라이언트 함수 진입 사실이고 실제 송신·서버 수신 증명이 아니다. 응답이 없으면 수신 여부 불명으로 유지한다.

**B · 별도 전용 file SQLite prebinding: 로컬 구현 가능.** 기존 공개 read_intent는 PREPARED와 SUBMISSION_UNKNOWN 양쪽을 확정 조회하고, journal 자체는 변경 없이 재사용할 수 있다. PR19의 strict bytes/hash/JSON/single-text helper를 재사용하되, 전체 observe_queue_correlation은 UNKNOWN을 요구하므로 PREPARED 단계 parser 대용으로 호출하면 안 된다.

D29에서 고정해야 할 B 계약:

- caller-owned 별도 연결의 명시 초기화만 허용한다. 빈 전용 file DB 확인, 별도 schema/app marker, bounded SQLite helper·transaction cleanup을 사용하며 기존 intent DB init/migration/ATTACH/schema/claim은 바꾸지 않는다. SQL·commit·cleanup 예외의 불명과 알려진 invalid profile을 구분한다.
- 신규 저장은 public read_intent의 확정 PREPARED/revision0을 전제로 한다. 원래 intent key/pin·client, intended thread와 실제 params bytes의 SHA를 strict PR19 parser로 결합한다. params의 client와 text UTF-8 hash도 원래 intent에 일치해야 한다. canonical binding bytes/hash를 저장하고 원문 pin을 canonicalization으로 대체하지 않는다.
- 원래 intent에 binding 한 개만 허용한다. thread/params hash 등을 key에 넣어 다른 binding을 새 row로 우회 저장할 수 없어야 한다. 같은 bytes/hash는 재사용, 다른 유효 binding은 conflict이며 원래 bytes/hash 보존을 재조회로 확인한다. 이후 UNKNOWN intent의 readback은 허용하되 새 binding 생성·변경·claim 재개·replay를 허용하지 않는다. UNKNOWN에서 동일 reserve를 허용할지 조회만 허용할지는 API별 기대 결과로 고정해야 한다.
- intent read와 별도 binding DB commit은 두 DB 원자 작업이 아니다. PREPARED snapshot 검사는 관찰 사실이며 동시 claim 방지 증거가 아니다. **prebinding commit 확정 전에 claim하지 않도록 직렬화하는 책임은 caller**에 남긴다. 이 순서를 지키는 실제 adapter/보호 journal 연결은 후속 조건이며 로컬 부품 구현을 막지 않는다.

필수 새 검증의 최소 방향: A는 injected fake client로 정확한 호출 순서·상한·false/unknown fail-closed·단계별 오류 privacy와 call-entered/수신 불명을 확인한다. B는 실제 전용 임시 SQLite의 초기화·불변 저장/reuse/conflict·원래 raw/hash 보존·UNKNOWN readback·close/reopen을 확인한다. 첫 실행부터 새 경로의 portable copy와 상대 fixture를 사용한다. 기존 PASS/전체 suite는 반복하지 않는다. 정확히 두 의미 변이는 예를 들어 A의 false 허용 오판과 B의 다른 유효 binding 수락을 겨냥해 실제 잘못된 결과와 semantic FAIL1/ERROR0을 확인할 수 있다. 이는 frozen 계획의 target 선정·정상 PASS gate·횟수·기준을 검토한 판정이 아니므로 계획에 정확히 고정해야 한다.

실제 send/ACK·controller 통합·두 DB 직렬화 인수·owner/provenance·native authorization은 미완료다. quota-only 재조회가 가능했다는 사실로 기존 six-RPC/thread-resume 또는 다른 runtime 조사를 재승인하지 않는다. scheduler/root/user PAUSE와 old native D18 HELD·기존 조사 상한은 유지된다.

이 검토는 제품/검사/plan을 작성·실행하거나 다른 agent를 조회·위임하지 않았다. socket/RPC/network/credential/service/queue/DB 호출·기존 R03/R04/R15/old suite 실행도 없다. 지정 분석/소스/공통 D29 규칙을 읽고 이 필수 독립 검토 기록만 새로 작성했다. 기존 성공·실패 근거와 원본 변경에는 쓰지 않았다. 새 단위 시작은 2026-10-17 00:57:31Z, 보수적 마감은 01:57:31Z이며 기존 분석 시간을 포함하고 재설정하지 않는다.
