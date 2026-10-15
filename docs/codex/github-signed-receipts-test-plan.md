# tests·reviewer 서명 receipt 검증 사전 계획

계획 v1 / 2026-10-15. 부모 `4e8eeb5db1abd847ba4d1bfe40ddbbf1f9106e17`, branch `codex/github-signed-receipts-20261015`, 격리 worktree 시작 clean. [공통 검사 기준](common/TESTING.md)의 D29/D30/D35/D38, [GitHub 기준](common/GITHUB_GATES.md)의 G04/G05/G06과 [준비 계획](common/PREPARATION_TEST_PLAN.md)을 유지한다. root는 source/guide/status, test_agent는 새 검사·소형 offline 근거, reviewer는 정확한 계획 SHA의 사전 검토와 최종 독립 검토를 맡는다. 사전 PASS 전 source/test 구현·실행은 0이다.

정확 6파일: 새 `src/ops/tools/github_signed_receipts.py`, `src/ops/tools/tests/test_github_signed_receipts.py`, 이 계획, `docs/codex/github-signed-receipts.md`, `docs/testing/github-signed-receipts-offline-20261015.json`과 기존 `docs/status.md` 준비 안내행 1개다. 기존 소스/검사·공통 정책·과거 근거는 불변이며 과거 suite/변이·App/API·운영키·native·sandbox 재실행은 0이다.

## producer 계약과 이번 완료선

[생산·controller 계약](github-signed-receipts.md)을 먼저 고정한다. owner는 PR 밖 0700 state와 0600 roster에 reviewed evaluator release·계획·기대 7binding·actor/key·assignment ledger를 보관한다. tests supervisor가 비신뢰 head child의 실제 exit/source/build/필수 실행 artifact를 직접 수집하고, 별도 reviewer invocation을 owner ledger의 job/role로 배정한다. worker에는 서명/App 키가 없고 임의 author PASS·임의 bytes signing 요청을 받지 않는 모델이다. 이 실제 설치·권한·상위경로·issuer·수집/재개·독립성은 **후속 미구현/미인수**이며 기존 sandbox 보류를 재개하지 않는다.

이번 완료선은 **주어진 roster/pin 아래 실제 Ed25519 서명·binding 필드·실제 artifact bytes hash를 대조하는 component**다. tests artifact는 향후 supervisor가 수집한 필수 실행 근거의 원래 bytes이며 binding의 evidence_sha256가 가리킨다. reviewer artifact는 별도 실행/판정 근거이며 같은 tests binding과 자신의 artifact hash를 서명한다. artifact가 receipt 자신을 hash하지 않아 순환하지 않는다. 현재 verifier는 opaque bytes의 의미·PASS·필수 명령 실행·실제 reviewer 참여를 증명하지 않는다. 실제 controller/issuer가 그 근거를 공급하는 연결이 다음 단계다.

## 고정 API·입력·판정

| 항목 | 계약 |
|---|---|
| 공개 API | `verify_receipt_pair(expected_binding, expected_signers, receipts, artifacts)->dict` 한 개. CLI/JSON parser/서명 API/파일 경로·URL 입력·network/App/status/worker 실행 없음. builtin dict/str/int/bytes의 `type(...) is ...`를 먼저 확인해 subclass get/equality/iteration hook을 배제한다. 입력을 수정하지 않음 |
| binding 재사용 | 기존 `github_check_audit.canonical_binding_digest`를 import해 exact 7필드 유효성을 확인한다. expected의 None을 먼저 차단하여 None==None 수용0. payload binding도 엄격히 유효하고 expected의 repository/pr/base/head/release/plan/evidence 전부와 같아야 함. 기존 모듈 bytes SHA=`d2b8deb1b9a652d396c63f42d2c581af51c7c19aa9bfa7785f2e740580b3515e`, 기존 두 test bytes도 보존 |
| roster | exact roles `author/tests/reviewer`. author exact `{actor,key_sha256}`, tests/reviewer exact `{actor,public_key_hex}`. actor ASCII `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`; author fingerprint는 lower hex64. public_key_hex는 raw Ed25519 32bytes의 lower hex64. fingerprint는 DER/PEM이 아닌 **raw32의 SHA256**. 세 actor와 세 fingerprint 모두 다름. receipt가 내장 key/algorithm/author PASS로 roster를 대체하지 못함 |
| receipt | exact roles tests/reviewer, 각 exact `{payload,signature_hex}`. signature_hex는 64bytes의 lower hex128. payload exact `{schema,role,actor,key_sha256,binding,artifact_sha256}`. schema는 strict int1(bool 거부), role/actor/key fingerprint는 해당 expected role과 같음. hash들은 lower hex64. 알고리즘 selector·추가 필드·키/경로 참조는 거부 |
| artifact | exact roles tests/reviewer의 strict bytes, 각각 1..1,048,576bytes. 각 실제 bytes SHA256=해당 payload artifact_sha256. tests 실제 bytes SHA는 expected_binding.evidence_sha256와도 같음. reviewer hash는 별도이며 공통 binding.evidence_sha256를 reviewer hash로 바꾸지 않음. artifact 재직렬화/JSON 의미 해석/실행/외부 파일 읽기0 |
| 메시지 | payload 전체에 `json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')`를 적용하고 정확 `b'agent-project:signed-receipt:v1\n'`을 앞에 붙임. domain은 일반 메시지 prefix이며 Ed25519ctx/ph가 아님. 두 role의 shape/pin/binding/artifact 검증을 모두 끝낸 뒤 tests→reviewer 순서로 crypto 최대2회. invalid second role에도 subprocess0 |
| backend | `_verify_ed25519(public_key,message,signature)->bool`: raw32 공개키/메시지/64bytes signature만 받는 내부 함수. 공개키 SPKI는 DER prefix `302a300506032b6570032100`+raw32. fixed `/usr/bin/openssl pkeyutl -verify -pubin -keyform DER -rawin -inkey /proc/self/fd/N -in /proc/self/fd/N -sigfile /proc/self/fd/N`. `-digest`/sign/URL/private-key 입력0 |
| FD·subprocess | Linux public-only memfd 3개에 DER/message/signature만 저장. partial write를 끝까지 처리하고 write0/seek 오류는 실패, seek0 뒤 크기를 아는 입력으로 전달. FD0/1/2면 F_DUPFD_CLOEXEC으로3이상 예약 후 원래FD close. 고정 argv·shell=False·close_fds=True·pass_fds 정확3·check=False·timeout5초·stdin/out/err DEVNULL. env 정확 `{LC_ALL:'C',PATH:'/usr/bin:/bin',OPENSSL_CONF:'/dev/null'}`. 정상/nonzero/missing/OSError/TimeoutExpired/SubprocessError/FD실패 모두 cleanup. close예외도 남은 FD close를 시도하고 backend False; 오류시 커널 close 성공을 보장한다고 하지 않음 |
| 오류·상한 | no memfd/openssl/backend 미지원·nonzero·timeout·FD 실패는 모두 False→SIGNATURE_NOT_VERIFIED. 환경 원인과 잘못된 서명을 stderr 없이 구별했다고 주장하지 않음. backend stdout/stderr는 폐기. 두 verify의 timeout 각5초이며 엄밀한 전체 API hardwall/kill 보장은 아님. 새 fallback·일반 tempfile/다른 알고리즘으로 product 우회0 |

반환은 exact **15키**: `scope,status,reason,signature_pair_verified,artifact_hashes_verified,binding_fields_satisfied,input_origin_verified,signer_roster_origin_verified,issuer_environment_verified,runtime_isolation_verified,independent_review_verified,trusted_gate_verified,binding_verified,native_ready,merge_authorized`.

scope=`RECEIPT_PAIR_VERIFICATION_ONLY`, status=`CRYPTOGRAPHIC_MATCH/BLOCKED`. 세 부분 flag는 **둘의 서명·모든 hash·binding이 모두 맞을 때만 함께 true**, 실패 시 함께 false. 마지막 9 flag는 언제나 false다. caller가 roster와 receipts를 함께 바꾸면 새 pair도 수학적으로 맞을 수 있어 roster 출처·실제 issuer/독립성·최신 GitHub binding은 인증하지 않는다. report/stdout/stderr에 actor/key/signature/binding/artifact/예외 원문은 내보내지 않으며 이 모듈은 출력하지 않는다.

reason은 `CRYPTOGRAPHIC_MATCH`, `EXPECTED_BINDING_INVALID`, `SIGNER_PINS_INVALID`, `RECEIPTS_INVALID`, `BINDING_MISMATCH`, `ARTIFACTS_INVALID`, `ARTIFACT_HASH_MISMATCH`, `SIGNATURE_NOT_VERIFIED`, `UNEXPECTED_FAILURE`만 허용한다. 단일 오류별 기준은 아래 표다. 복수 오류에 대해 임의 우선순위 계약을 추가하지 않되 모든 입력 검증은 crypto 전에 끝낸다.

## 선정 검사와 기대 reason

R01~R15는 최대15개 methods와 좁은 subtests로 묶고, R16은 재사용 unit 밖 일회성 감사다. 실제 crypto는 임시 fixture Ed25519 키/서명만 사용한다. 실제 App RSA 키/운영 signing key 생성·접근0, 과거 suite 재실행0. fixture prerequisites가 없으면 NOT_RUN/오류와 기능 BLOCKED를 기록하며 mock PASS로 대체하지 않는다.

| ID | 단일 오류/관찰 | 기대 결과·필수 근거 |
|---|---|---|
| R01 | 두 temp Ed25519 key로 독립 canonical 메시지 서명, 정상 pair | 실제 openssl2회·CRYPTOGRAPHIC_MATCH, exact15·부분3true/신뢰9false·입력불변 |
| R02 | 고정 canonical byte fixture, dict순서 변경, wrong domain/부분payload로 실제서명 | 순서변경은 MATCH, 정확 domain+전체payload 외 서명은 SIGNATURE_NOT_VERIFIED. fixture가 product canonical helper를 재사용해 같은 오류를 만들지 않음 |
| R03 | expected7 shape/None/bool PR/해시·repo 형식·누락/extra | EXPECTED_BINDING_INVALID, backend0. invalid expected/observed가 모두 None이어도 거부 |
| R04 | roster shape/actor/upper·길이hex, author=tests/reviewer actor/key, tests=reviewer actor/key | SIGNER_PINS_INVALID, backend0. author fingerprint와 raw32 SHA를 정확히 비교 |
| R05 | receipt exactshape/schemaTrue/role·actor·fingerprint·signature길이/extra, malformed payload binding | RECEIPTS_INVALID, backend0. 두번째 role만 잘못돼도 첫 crypto0 |
| R06 | **M1 target**: 나머지는 모두 정상, tests 64bytes signature의 1byte만 변경 | 실제 openssl nonzero→SIGNATURE_NOT_VERIFIED. 형식/role/binding/artifact guard가 대신 막지 않음 |
| R07 | payload pin은 정상인데 다른 role key/다른 Ed25519 key로 실제서명, author payload 혼입 | wrong signing key는 SIGNATURE_NOT_VERIFIED; author/role payload pin 불일치는 RECEIPTS_INVALID. 단순 미서명 PASS 거부 |
| R08 | repo/PR/base/head/release/plan/evidence 각각 stale인 pair를 **완전히 재서명** | 유효한 stale 7binding은 BINDING_MISMATCH, backend0. 서명만 깨진 fixture로 stale 검사를 가장하지 않음 |
| R09 | **M2 target**: 정상 signed pair/tests bytes/evidence pin, reviewer 실제 bytes만1byte변조 | ARTIFACT_HASH_MISMATCH, backend0. tests evidence 이중검사는 정상이어야 변이를 가리지 않음 |
| R10 | artifacts rolemap/bytearray/memoryview/subclass/빈값/1MiB+1, 양쪽1MiB 정상, tests hash↔evidence 불일치 | shape/size는 ARTIFACTS_INVALID; 실제 bytes/payload/test-evidence hash 불일치는 ARTIFACT_HASH_MISMATCH. 정상최대크기는 실제 MATCH |
| R11 | RFC8032 Test2 public vector(message72) 실제backend와 실제 verify argv capture | backend true, DERprefix/raw32·3 publicbuffers·seek0·FD>=3·pass_fds·fixedcmd/env/DEVNULL/5초 정확. artifact bytes/개인키를 subprocess에 넘기지 않음 |
| R12 | backend nonzero/missing/timeout/FD생성·부분write/write0/seek/close 실패, 두번째backend 실패 | 모두 SIGNATURE_NOT_VERIFIED/부분flag전부false, 최대2call·남은FD close·원문0. partial write 성공은 실제verify 성공. TimeoutExpired 주입은 실제5초 kill/reap 실측과 구분 |
| R13 | 격리 child가 std0/1/2를 닫은 뒤 public RFCvector 실제verify, 별도FD로 결과전달 | 처음 lowFD를>=3으로 예약해 실제true·FD정리. stdin/out/err 대체와 충돌0, 운영키0 |
| R14 | signed opaque FAIL/nonJSON artifact·caller의 다른정상roster/pair·private sentinel·예외·subclass hooks | 좁은 MATCH여도9trustfalse/의미PASS 주장0. hostile subclass는 올바른 입력오류 reason으로 hook실행0, unknown오류 UNEXPECTED_FAILURE, stdout/stderr/원문0 |
| R15 | AST3.10/import·noCLI/parser/network/sign/영속쓰기·기존canonical source/test bytes | stdlib+기존 canonical helper/고정 verify backend만, module import에 crypto/worker 부수효과0. 실제3.10 미실행은 syntax와 구분 |
| R16 | producer guide·status1행·offlineJSON·정확6파일·링크/공백/비노출·전후SHA | controller/producer 계약은 설치전 목표, 현issuer/roster origin/native/trust BLOCKED를 유지. 테스트별 실제/mocked/NOT_RUN·counts/time·기존파일 보존·snapshot/cleanup 근거 연결 |

## D38·완료 기록

reviewer가 선택한 정확 2개만 실행한다. **M1**은 `_verify_ed25519`의 `return completed.returncode == 0`을 `return True`로 바꿔 **실제 openssl invalid-signature 실행 결과를 무시**한다. R06의 형식상 정상인 tampered signature가 잘못 MATCH되어 기대 SIGNATURE_NOT_VERIFIED assertion이 FAIL이어야 한다. **M2**는 payload artifact_sha256와 실제 artifact digest의 비교 guard를 제거한다. R09 reviewer-only 변조가 두 정상서명/tests binding 아래 잘못 MATCH되어 assertion FAIL이어야 한다.

정상 target PASS는 최초 새 suite의 같은 source/test/plan SHA 결과를 재사용한다. 추가 정상 target run은 하지 않고 각 격리 mutant target만1회 실행한다. FAIL1/ERROR0의 의미·mutant SHA·원본불변·copy삭제를 기록한다. 문법/import/환경 ERROR는 검출 성공이 아니다. 최초 source/test/plan 원문 snapshot과 실행별 SHA를 scratch에 보존해 보정 시 독립 byte/AST 대조가 가능해야 한다.

새 suite1회+위2mutants+R16감사 뒤 새 변경/실패/미해결 우려가 없으면 반복·확대하지 않는다. 실제 issuer/collector/roster 로딩·실제 역할 실행·native 검증·success check 발행·보호/worker/merge는 후속이며 이번 crypto MATCH로 통과시키지 않는다. 기존 sandbox EACCES의 누적 세 실패·D17/D18 상한/연장 소진·보류는 유지한다.

공식 근거(2026-10-15 열람): [OpenSSL3 pkeyutl](https://docs.openssl.org/3.0/man1/openssl-pkeyutl/)의 Ed25519 rawin/no-digest·크기를 아는 oneshot 입력, [RFC8410](https://www.rfc-editor.org/rfc/rfc8410.html)의 Ed25519 SPKI. Ed25519/RFC8032와 실제 controller 계약의 상세는 [guide](github-signed-receipts.md)를 참조한다.
