# OWNER PREFLIGHT / DURABLE PREBINDING 최종 독립 검토

판정: **PASS** — 아래 고정 코드와 로컬 검사·문서 범위에서 남은 차단 finding 없음. native/owner 인수 승인은 아니다.

- Q source SHA256: `699f3a6e8993f28d88d7413ac51197a144b6d8b69a01f321da11a17398a4a23c`
- B source SHA256: `1663ab454e0f66a5e7f23382b9cf4c047068a0c86f10d895c4ad19a4d57f2a3c`
- Q test SHA256: `7e57cdcef6f3ef48c975e9de9d494b34018e97f14b90478a273ae1820b8fb476`
- B test SHA256: `63e1b380519df8f78d6e143c8824150049b73c4cf024a437442501ba9fd281d6`

검토 시각: 2026-10-17 UTC. 대상은 `var/unattended-20261015/binding-development`, branch `codex/agent-project-owner-preflight-binding-20261017`, 실제 HEAD/base PR19 `ddec1931eed8828cdd84ae783845729edf617fc1`이다. 근거 디렉터리는 원본 저장소의 `var/unattended-20261015/transition-evidence/owner-binding-20261017`이다. 제품·검사·문서 수정, import/검사/DB 조회·runtime 호출 없이 파일과 기존 근거만 읽었다.

## 사전 검토와 고정 구현의 재사용

- D29 frozen plan: `4ef0459db19aa24a806f1eae5701abc04dca6994220a7df67deff889a2b2d265`.
- 구현 기준 D29/D06 PASS: `14183f1e79baceed04bb3c656a252d67382ec65209409e005e741e002c289499`.
- 최초 source static 기록: `8b41f16772a5866aad404247a1ba63953af48fbbdcd584acb44457f3ad2f39bd`; F1 수정 확인 PASS: `691d0951b71ba57717f53e4ebca5a52837f2796d6a5b9d1ac0c4746951f1f872`.
- D06 bytes-path 실행기 보정 PASS: `d001780c82603d4ce6690b181e911c7198952e38bb38989e6ed9847279440990`.
- D06 authorizer fixture/실행기 보정 PASS: `f9b9c8a941d1341ba1708b592a35b5b192f4bf788aa483c8a08602c37eb66015`.

현재 네 파일과 실행 직전 snapshot의 전체 hash가 위 pin과 일치한다. 기존 정적 판단을 재사용하고 최종 test의 공개 계약·주입 경계·실제 assertion 및 실행기/실행 기록을 대조했다. None intent의 bind/read는 SQL0·record=None·closed=False를 B3가 실제 검증하고, initialize만 closed=None인 F1 계약을 유지한다. 두 D06 보정은 각각 audit 경로의 bytes 정규화와 제품 호출 후 fault authorizer 정리에 한정된다. B1/B2·helper·기대 결과는 불변이다.

Q는 caller-owned client에서 initialize→initialized notification→account/rateLimits/read, 최대 2 RPC/notification 1/retries 0을 관찰한다. strict boolean 및 유효한 bounded snapshot만 투영하고 unknown/invalid는 닫는다. 예외·속성·알 수 없는 metadata를 원문으로 출력하지 않는다. call_entered/response_observed는 local client 경계이며 wire-send·transport pin·full admission·native/remote authority를 확인한 것으로 보고하지 않는다. 실제 연결·기한·pin·exclusive attempt·종료 책임은 caller에 남는다.

B는 borrowed 전용 file SQLite만 사용한다. 두 연결의 구조·caller active transaction을 SQL 전에 검사하고 profile 확인 후 intent BEGIN IMMEDIATE 안에서 고정 기존 schema/pinned helper를 재사용한다. PREPARED revision 0에서 불변 canonical thread/client/intent/원문 params SHA binding을 저장하고, 같은 bytes만 reuse·다르면 conflict로 거부한다. 기존 intent/observer/ledger/claim 소스는 pin과 일치하며 수정하지 않았다. UNKNOWN은 새 bind/reuse를 거부하고 기존 binding 조회만 허용한다. binding commit 확인까지 intent lock을 유지하며, SQL/commit/release 불명은 확정 실패나 성공으로 승격하지 않는다. 소유 transaction만 정리하고 borrowed 연결을 열거나 닫지 않는다. 두 DB의 영구 atomicity·반환 뒤 claim/send 직렬화·경로 alias 식별·신뢰 pin은 외부 책임이다.

## 실제 실행 근거와 제한된 재사용

최종 실행기 SHA256: `025372067ccd97fdd8f1a5f7070fe2ea5161d6e99903403abad624ffb4670238`.
`run-3/result.json` SHA256: `09fbe7e8585185322f53ba45f6820fa906d0dbf1a1b8c586272113fa5dce67b8`.
실행 시작 `01:40:50.985788Z`, 완료 `01:40:51.734703Z`이며 원래 공유 마감 `01:57:31Z` 이전이다. 실행기와 snapshot pin·command·raw log·개별 result hash·mutation diff/AST audit가 서로 일치한다.

| 근거 | 실제 최초/재개 결과 | 최종 채택 범위 |
|---|---|---|
| run-1 normal | 4실행, 3 PASS / setup ERROR1, FAIL0/SKIP0 | Q1/Q2/Q3 PASS 재사용 |
| run-2 normal | 3실행, 2 PASS / fault fixture ERROR1, FAIL0/SKIP0 | B1/B2 및 정상 UNKNOWN guard helper PASS 재사용 |
| run-3 normal | B3만 1실행, PASS / FAIL0/ERROR0/SKIP0 | 오류난 B3 전체 메서드 재검증 |

필수 정상 메서드 6개가 모두 PASS다. 최종 여섯 개를 한꺼번에 재실행했다고 주장하지 않는다. 정상 누적 실행 8회에는 보존된 두 ERROR가 포함되며, 통과 메서드는 반복하지 않았다. run-1은 B 제품 API 전 audit bytes/str TypeError였고, run-2는 제품 호출 후 fixture profile PRAGMA의 authorizer 정리 호환성 오류였다. 두 원본 log/result/snapshot과 최초 source finding, coordinator metadata 관찰 오류·그 정정, 실행 전 runner 생성 SyntaxError를 별도로 보존했다. 검사 fixture/runner 보정 2회, 원래 예산과 실패 상한을 유지했다.

정상 result pin은 run-1 `cfee9e4c24162d8154ce64bdfbd68e668e28f37dbdcc2904ba71ed003273d5d3`, run-2 `3556762620e0634a645969a0396f972ae5607c30171453b926d5f21bdafaf43d`, run-3 `9967f8fbeec13c5808da12a635f7f76782040d3b32a5b0bc2dd3db8b73c37b64`다.

정확히 두 변이는 정상 6개 결과가 모인 뒤 원래 지정 target을 각각 한 번 실행했다. 다른 AST·builder·검사 assertion은 변경하지 않았으며 정상 target PASS를 재사용했다.

- **M_Q:** `ordinary is True`만 `ordinary is not False`로 변경했다. ordinary=null·유효한 14% snapshot에서 실제 `OBSERVED_ALLOWED`/allowed=true가 나왔고 Q3의 expected_status assertion(243행)에서 **semantic FAIL1/ERROR0/SKIP0**이었다. result SHA256 `ad900ae6dab0189204af6164cf0650bf92873edef7e30b48131086654cb7019b`.
- **M_B:** PREPARED/revision0 guard 전체만 True로 바꿨다. 지정 UNKNOWN helper의 reason assertion(259행)에서 **semantic FAIL1/ERROR0/SKIP0**이었다. 실제 UNKNOWN revision 1에서 BINDING_STORED/new_binding=true/commit confirmed와 peer-visible row 1개를 캡처했다. 저장된 canonical C의 intent_revision=0·bytes/hash·기대 binding도 맞았다. result SHA256 `667cfac87d4ed1b3b093678809faec14f19a9f9a501df7af29c66d87b8cd7b4a`. 보존된 wrong-commit-witness.sqlite를 SQL 없이 파일 hash로 확인했고 `4885bf5c82ff752514fe717dcae83129914c771c30b0b73f82520b9f8e431204`와 일치한다.

Python 3.10.12 / SQLite 3.37.2에서 새 이름의 최소 source copy를 `-I -S -B`로 실행한 기록이다. 상대 fixture/generated schema pin, package·기존 helper 및 dependency pin을 확인했다. Q는 fake client 검사이고, B는 실제 전용 임시 file DB의 bytes/hash/reuse/conflict·close/reopen·UNKNOWN 보존·동시 BEGIN IMMEDIATE 경합·commit/release 전후 유실·privacy/소유 cleanup 검증이다. runtime 호출 0·경계 위반 0이며 원본 checkout·/opt·operator 파일에 의존하지 않는다. 프로토콜과 입력은 synthetic fixture다. OS crash·두 DB 영구 원자성이나 실제 native client와의 인수를 증명하지 않는다.

## 문서와 보존

기존 네 문서의 각 1문단 추가 및 observed JSON을 대조했다. 원래 3+2+1 PASS 재사용·두 최초 ERROR·두 변이 검출·fake/실제 SQLite 구분을 정확히 기록한다. `docs/status.md`의 검토 진행 중 문구와 observed의 final_independent_review=PENDING은 이번 판정 이후 메타데이터로 갱신할 수 있다. 해당 상태를 제품/검사 변경이나 추가 실행 근거로 해석하지 않는다.

검토한 문서 SHA256:

- observed JSON: `6ce3ab97076629f08074c38d51307384e8333596483f6aeaaba3aefd3f04b8d0`.
- HANDOFF_NEXT_SESSION.md: `2dfb2f9919f8a59588e32d9fc63bc8c18cb2e4c8c95ca3bcf838a1cff00af35a`.
- status.md: `ac00d0ebed5fc9faf96409ebf02358371e01c8db4d7df909458082cdee120b10`.
- roadmap.md: `772585087660753587304ea9af53ecaa40de7c8204627681a03804ed818dbc71`.
- development.md: `f879de88a42a8dddf63b0aea5078d5a29d8cd016974ea13352f488354e867898`.

`preservation-after-verification.json` SHA256 `ecb655abf4d4c109c5e47f7f42543b12bde63e04632b00c54f64f26ebed8dd36`를 읽고, 보존 baseline의 기존 로컬 변경 11개 실제 hash를 모두 대조했다. 원본 root의 4 modified+7 untracked 상태, branch `codex/resume-preparation-20261015`, HEAD `b1fda4916041fde8438251b04db62e6ff702c24f`도 유지됐다. 기존 owner utility source `7ec43e502ed7a59e61c3abfd8949df059fb9dbb2f80657fdcd93dee08f83d259`와 보존 receipt `22e14deebb25db329fb0ebef6b22659167d2ffe06d3cbaeddd90b2fbc6a430bc`는 기존 observed pin과 일치한다. 과거 ordinary=false/100% 및 thread/resume writer 충돌 기록도 보존됐다. 서비스나 runtime 상태는 추가 조회하지 않았고 PAUSE/D18 유지 근거는 보존 기록과 고정 경계로 한정한다.

별도 owner 실제 quota 조회 1회는 고정 PR16 transport `7f70d9eb3402fddd97f2a8c0f448b2b3336db28512d0c8e004adc7dc76e4b415`로 initialize/read 2 RPC·notification 1·retries0, true/주간14%를 관찰한 것이다. 해당 근거 SHA256 `57a25a332bf29dfe3eabaee2817a1be5d75dd8114177b6a82bac8d51a9e0825c`와 문서가 일치한다. **새 Q reader의 실제 native 실행·인수는 NOT_RUN**이다. 과거 six-RPC/thread-resume를 반복하지 않았으며 이 별도 quota 관찰은 PAUSE/D18·사용량 신선도·모든 quota bucket·owner/provenance/권한을 승인하지 않는다.

## 남은 경계와 다음 작업

실제 send/ACK/turn/result/controller 연결·두 native 작업은 미구현 또는 NOT_RUN이다. 기존 PAUSE와 old native D18 HELD, 운영 원본/DB/Docker/인증정보/main/VMS 보호 및 조사 상한을 유지한다. caller의 실제 owner client·pins·전용 상태 경로·직렬화·기한·신선도 및 명시적 native 실행 권한은 후속 연결의 별도 조건이다.

다음은 coordinator가 이 고정 코드·필수 근거를 보존해 **PR19 대상 별도 브랜치의 Draft PR20**으로 게시하고 게시 commit의 파일을 readback하는 단계다. 이번 reviewer는 게시·통지·다른 agent 조회/위임·실행·운영 변경을 하지 않았으며, 검토서를 제외한 파일을 수정하지 않았다. 기존 R03/R04/R15·old PASS·전체 suite도 실행하지 않았다. 공유 마감 `2026-10-17T01:57:31Z`와 최초 분석 시간을 포함한 원래 예산은 재설정하지 않았다.
