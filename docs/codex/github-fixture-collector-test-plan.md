# 고정 fixture assignment 수집 사전 계획

계획 v1 / 2026-10-15. 부모 `859b401af70b74e9417f7c0fab8f26b2985fcee3`, branch `codex/github-fixture-collector-20261015`의 새 격리 worktree다. [공통 검사 기준](common/TESTING.md)의 D29/D30/D32/D35/D38, [GitHub 기준](common/GITHUB_GATES.md)의 G04/G05/G06과 기존 보류를 유지한다. root는 source/guide/status, test_agent는 이 계획·새 test·소형 offline JSON, reviewer는 정확 계획 SHA의 사전 검토와 최종 독립 검토를 맡는다. source/test 구현·실행은 사전 PASS 뒤다.

정확 6파일: 새 `src/ops/tools/github_fixture_collector.py`, `src/ops/tools/tests/test_github_fixture_collector.py`, 이 계획, [guide](github-fixture-collector.md), `docs/testing/github-fixture-collector-offline-20261015.json`과 기존 `docs/status.md` 준비 안내행 1개다. 기존 source/test/guide·공통 정책·과거 근거는 불변이다. 실제 key/sign/App/API·native worker·head 코드·보류 sandbox·과거 suite/변이 실행은 0이다.

## 완료선과 고정 입력

공개 API는 `collect_fixture_assignment(state_dir_fd, expected_assignment_sha256)` 하나이며 반환 exact3키는 `{report, artifact, binding}`이다. CLI·경로·임의 argv/code/env/launcher/callback·이미 실행한 Popen 객체를 입력받지 않는다. Linux Python3.10 stdlib와 변경 없는 `github_check_audit.canonical_binding_digest`만 사용한다. 이는 **고정 진단 collector의 실제 생산 component**이며 실제 head collector/보호된 controller 설치·ledger·issuer 연결 완료가 아니다.

| 경계 | 고정 계약 |
|---|---|
| FD 입력 | strict int0 이상(bool 거부)의 열린 directory FD와 lower hex64 기대 digest. 현재 euid 소유·정확 mode0700인 디렉터리만 허용. caller FD/offset/FD flags는 보존하고 자기 CLOEXEC duplicate만 관리한다. low FD0/1/2도 caller를 닫지 않고 >=3으로 예약 |
| 파일 로드 | directory FD 기준 고정 `assignment.json`만 `O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`로 연다. regular/euid 소유/정확 mode0600/nlink1/size1..16384를 확인하고 cap+1 읽기와 SHA256 대조. symlink/FIFO/다중 link/권한·타입 오류를 거부하고 Popen0. own FD close 오류 뒤 같은 번호 재close0; 남은 own FD 정리 시도. 모든 설정 FD 정리 성공 뒤에만 spawn |
| 엄격 JSON | UTF-8 strict/BOM 거부, duplicate key/NaN/Infinity 거부, int 문자열20자 상한, 중첩 깊이8 상한. bytes의 기대 SHA가 맞더라도 JSON/shape/type 오류는 거부. builtin dict/str/int exact type과 필드 허용목록을 검증하며 bool을 숫자로 수용하지 않음 |
| assignment | exact7키 `{schema,mode,job_id,target,fixture_profile,timeout_ms,max_stream_bytes}`. schema strict int1, job_id ASCII `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`, timeout_ms strict int100..2000, max_stream_bytes strict int1..65536. mode=`fixture`; 형식상 정상 `head` 요청은 Popen 전 REAL_EXECUTION_BLOCKED. 그 밖의 mode/unknown profile/추가 실행 옵션은 ASSIGNMENT_INVALID |
| target6 | exact `repository,pr_number,base_sha,head_sha,release_sha256,plan_sha256`. 임시 evidence=`0`64자를 붙여 기존 canonical validator로 검증하고 임시값은 산출물에 저장하지 않음. release는 공통 규칙 digest이며 collector 설치 코드 pin과 다름 |
| 고정 실행 | `/usr/bin/python3 -I -B -c <source의 고정 literal>`만 Popen1회. cwd=`/`, env exact `{LC_ALL:'C',PATH:'/usr/bin:/bin'}`, stdin DEVNULL, stdout/stderr PIPE, bufsize0, shell=False, close_fds=True, pass_fds=(), start_new_session=True. 재시도·다른 interpreter fallback0 |
| 시간·수집 | spawn 직전 monotonic 기준 deadline을 기록하고 selectors로 두 pipe를 동시 drain. pipe EOF와 child 종료/reap을 분리하여 EOF만으로 반환하지 않음. timeout은 수집 deadline이며 Popen 자체나 전체 API의 엄밀한 hardwall 보장이 아님. 직접 child kill+최대1초 wait/reap과 pipe/selector 정리를 시도하며 descendant/group 전체 cleanup을 주장하지 않음 |
| 출력 cap | stdout/stderr 각각 cap까지 retained bytes만 hash. 다음1byte로 overflow를 관찰하고 중단하므로 captured_bytes<=cap, observed_bytes<=cap+1. observed는 전체 생성량이 아님. raw stdout/stderr 및 raw prefix는 report/artifact/로그에 저장·출력0 |

directory FD의 원래 경로·상위경로·PR 밖 anchor·owner 의도·기대 digest 출처는 인증하지 않는다. 같은 UID의 trusted literal은 worker 권한 격리 검사가 아니다. 실제 보호된 loader/launcher·caps/ptrace/proc/network·동시 ledger/CAS·서명은 후속이며 fixture를 head로 전환하는 옵션은 없다.

## Profile·report·artifact 계약

| Profile | 고정 동작·독립 기대값 |
|---|---|
| `ok` | stdin이 `/dev/null`이고 `/proc/self/fd` 스캔 후 살아있는 fd>=3이 없음을 고정 코드로 확인한 뒤 stdout `fixture-ok\n`, stderr `fixture-stderr\n`, exit0. FD/DEVNULL selfcheck 실패는 nonzero. 외부 FD 번호·코드를 입력받지 않음 |
| `nonzero` | stdout `PASS\n`, stderr 빈 bytes, exit7. PASS 문구는 판정에 사용하지 않음 |
| `dual-output` | stdout `O`32768bytes를 먼저, stderr `E`32768bytes를 이어 기록. 두 pipe 실제 수집과 small-cap overflow를 확인 |
| `wait` | 10초 대기. 실제100~200ms assignment로 timeout/kill/reap을 관찰 |
| `closed-pipes-wait` | stdout/stderr를 닫고 10초 대기. EOF가 먼저 와도 실제 timeout/kill/reap 필요 |
| `signal` | 자신에게 SIGTERM. 완전 수집이면 returncode=-SIGTERM이며 성공 exit0과 다름 |

report는 exact **26키**: `scope,status,reason,assignment_bytes_verified,collection_complete,exit_zero_observed,returncode,child_reaped,stdout_captured_bytes,stderr_captured_bytes,stdout_eof,stderr_eof,output_complete,output_limit_exceeded,timed_out,fixture_only,head_code_executed,input_origin_verified,signer_roster_origin_verified,issuer_environment_verified,runtime_isolation_verified,independent_review_verified,trusted_gate_verified,binding_verified,native_ready,merge_authorized`.

scope=`FIXTURE_ASSIGNMENT_COLLECTION_ONLY`; status=`OBSERVED/BLOCKED`. count는 strict int0..cap, returncode는 실제 wait/poll로 관찰한 strict int 또는 None이며 spawn 전은 None이다. 나머지는 strict bool. `assignment_bytes_verified`는 metadata/bytes/hash/shape 검증만, `exit_zero_observed`는 reaped child의 실제 returncode==0만 뜻한다. `output_complete`는 두 EOF와 cap/IO/timeout 불명 없음, `collection_complete`는 여기에 reap/cleanup 확인까지 성립한 경우다. 완전 수집이면 OBSERVED·EXIT_ZERO/EXIT_NONZERO/SIGNALED이고 어떤 값도 tests PASS 판정이 아니다. `fixture_only=true`, `head_code_executed=false` 및 마지막9 trust flag는 모든 경로에서 false다. report에는 job/target/hash/path/raw exception을 넣지 않으며 모듈 stdout/stderr 출력0이다.

고정 reason은 `ARGUMENTS_INVALID`, `STATE_FD_INVALID`, `ASSIGNMENT_FILE_INVALID`, `ASSIGNMENT_HASH_MISMATCH`, `ASSIGNMENT_INVALID`, `REAL_EXECUTION_BLOCKED`, `SPAWN_FAILED`, `TIMEOUT`, `OUTPUT_LIMIT_EXCEEDED`, `IO_UNCONFIRMED`, `CLEANUP_UNCONFIRMED`, `UNEXPECTED_FAILURE`, `EXIT_ZERO`, `EXIT_NONZERO`, `SIGNALED`다. 준비 오류는 child0. 수집 중 최초 stop reason을 보존하고 cleanup 오류가 유일한 오류일 때 CLEANUP_UNCONFIRMED다. cleanup 불명은 collection_complete=false이며 원래 실패를 성공으로 바꾸지 않는다. timeout/overflow 뒤 returncode는 강제 종료 후 관찰값이지 원래 job의 자연 종료 결과가 아니다.

artifact는 strict bytes의 canonical ASCII JSON(`sort_keys=True,separators=(',',':'),ensure_ascii=True`)이다. exact11 root는 `schema,kind,job_id,target,assignment_sha256,fixture_profile,fixture_code_sha256,limits,outcome,stdout,stderr`; schema=1, kind=`FIXTURE_DIAGNOSTIC`. limits exact2는 `timeout_ms,max_stream_bytes`; outcome exact8은 `reason,returncode,child_reaped,collection_complete,exit_zero_observed,output_complete,timed_out,forced_termination_requested`; 각 stream exact6은 `captured_bytes,observed_bytes,sha256,prefix_only,eof,limit_exceeded`다. hash는 retained prefix/실제 전달한 `-c` literal의 UTF-8 bytes SHA256이며 원문을 포함하지 않는다. 중단/불명은 output_complete=false·prefix_only=true로 명시한다. artifact 안에는 자기 hash/evidence/최종 binding/receipt가 없어 순환하지 않는다.

준비/설정/spawn 실패는 artifact=binding=None. child spawn 뒤 관찰은 불완전/실패도 위 shape의 artifact를 만들고, 원래 target6+SHA256(정확 artifact bytes)로 exact7 binding을 반환한다. binding 필드가 채워져도 `binding_verified=false`; 실제 origin/최신성/의미/서명은 인증하지 않는다.

## 선정 검사

R01~15는 최대15개 methods와 좁은 subtests, R16은 재사용 unit 밖 감사다. 실제 임시 FD/file과 실제 고정 fixture Popen을 사용하며 fault 대역은 구분한다. parent Python과 고정 `/usr/bin/python3` child 버전도 별도 기록한다. 실제3.10이 없으면 AST3.10과 NOT_RUN을 분리하고 다른 interpreter 성공을 3.10 인수로 부르지 않는다.

| ID | 필수 입력·상황 | 기대 결과·근거 |
|---|---|---|
| R01 | temp0700 directory/0600 assignment의 정상 ok 실제 전체 API | OBSERVED/EXIT_ZERO·collection/output/exit0/reaped true, exact26·9false·raw output0, marker의 독립 bytes count/hash, exact artifact/binding |
| R02 | actual nonzero profile, stdout PASS 문구 | OBSERVED/EXIT_NONZERO·actual returncode7·exit_zero_observed=false·collection_complete=true. **M1 target**, 문구에 의해 성공 오표기0 |
| R03 | actual signal profile | OBSERVED/SIGNALED·actual -SIGTERM·reaped true·exit0false·신뢰false |
| R04 | actual dual-output, cap65536와 exact32768 경계 | 각32768 count/hash·EOF·complete true·deadlock0. stdout 뒤 stderr 순서여도 둘 모두 drain |
| R05 | actual dual-output, smallcap64 | OUTPUT_LIMIT_EXCEEDED/BLOCKED·output_complete=false·prefix_only true, cap/cap+1·정확 captured-prefix hash·추가 Popen0·reap. **M2 target**은 output_complete를 직접 assert |
| R06 | actual wait/closed-pipes-wait, timeout100~200ms | TIMEOUT/BLOCKED·timed_out true·output_complete/collection_complete false·kill/reap 확인. closed pipes EOFtrue라도 조기완료0. 각 API wall elapsed<3초와 직접 child 잔존0 확인; 환경 지연 실패는 숨기지 않음 |
| R07 | strict FD/hash args, temp file을 directory로, bad dir mode/owner 대역, caller lowFD | ARGUMENTS_INVALID/STATE_FD_INVALID·spawn0. caller FD/offset/flags 보존. directory owner negative 대역과 실제 metadata 검사를 구분 |
| R08 | actual assignment missing/symlink/FIFO/link2/mode wrong/empty/16385, owner negative 대역 | ASSIGNMENT_FILE_INVALID·읽기전 regular 확인·FIFO hang0·spawn0·own FD cleanup·caller보존 |
| R09 | expected SHA mismatch와 strictJSON/shape/limits/target6 각단일오류 | ASSIGNMENT_HASH_MISMATCH 또는 ASSIGNMENT_INVALID·spawn0. dupe/nonfinite/BOM/UTF8/int21/depth9/unknown/bool/size 경계·empty/extra 대상·정상한도 수용을 구분 |
| R10 | 정상 hash/shape의 mode=head, 임의 argv/env/code/unknown profile/extra 옵션 | head만 REAL_EXECUTION_BLOCKED, 나머지 ASSIGNMENT_INVALID, 모두 Popen0. 실제 head/source/worker 실행0 |
| R11 | 실제 ok와 inheritable caller dirFD+추가 public canary FD, Popen argv/kwargs spy; 격리 child의 low FD 입력 | 고정 selfcheck 양성·config/canary 비상속·DEVNULL·fixedargv/env/cwd/PIPE/session/close_fds/pass_fds 확인. caller lowFD를 닫지 않으며 own pipe lowFD도 안전 정리. spawn은 실제, 임의 launcher 주입0 |
| R12 | spawn OSError·read/selector/clock/kill/wait/pipe-close·config duplicate/close 오류 대역 | 고정 실패 reason·원문0·후속spawn0/추가spawn0·own resource 정리 시도. close 오류 후 동일번호 재시도0, caller fd보존. reap 불명은 BLOCKED·child_reapedfalse, 실제 성공으로 합산0 |
| R13 | artifact canonical bytes·binding 비순환·assignment/job/target/profile/limits 변경 | 독립 SHA/artifact exactshape/final7 대조. evidence는 실제 artifact SHA, target6의 임시 evidence는 미포함. 실제 전달 literal digest와 상한/prefix/forced outcome 명시·입력 불변 |
| R14 | private sentinel·malformed subclass args·unknown exception·opaque PASS/FAIL 출력 | stdout/stderr/raw path/config/예외0, exact allowlist·9false·fixturetrue/headfalse 전 경로. fixture exit0도 required tests/independence/gate PASS 아님 |
| R15 | AST3.10/import·stdlib+기존validator·noCLI/임의 실행/서명/network/영속쓰기·기존 source/test SHA | import 부수효과0, public entry 하나와 fixed subprocess만. product가 상태/keys/App/ledger를 쓰지 않고 기존 bytes 보존; receiver를 구분해 list.remove를 os.remove로 오탐하지 않음 |
| R16 | guide/status1행/offlineJSON/정확6파일·links/whitespace/public payload/SHA | controller 설치·source origin/head/native/issuer/required gate 미인수와 과거 보류 유지. 실제 vs 대역/NOT_RUN·실패 이력·snapshot/cleanup·해시 귀속을 대조. 후속 pinned 두 source+임시0700 state/0600 assignment의 고정 ok 명령은 reviewer 정적 검토만 하며 실제 사용자 Python3.10 실행은 NOT_RUN; cleanup은 그 명령이 만든 임시 경로만 대상 |

## D38와 실행·근거 규칙

reviewer가 선정할 정확2개만 실행한다. **M1**은 실제 returncode7에도 `exit_zero_observed`를 true로 만드는 의미 오류다. 최초 R02의 actual nonzero/PASS-text fixture가 그 flag의 false assertion으로 FAIL이어야 한다. **M2**는 overflow 시 `output_complete`를 true로 오표기하는 의미 오류다. 최초 R05의 actual dual-output/smallcap fixture가 그 flag의 false assertion으로 FAIL이어야 한다. 다른 reason/status/EOF guard가 여전히 BLOCKED해도 해당 오표기는 실패해야 하므로 generic status만 검사하지 않는다. 각 mutant는 원본과 같은 검사의 정상 PASS를 최초 suite에서 재사용하고 mutant target만1회 실행한다. FAIL1/ERROR0, 실제 잘못된 관찰값·mutant SHA·원본불변·격리copy 삭제를 남긴다.

최초 새 suite1회·각 mutant1회·R16감사만 수행하며 통과한 정상 target/과거 suite를 반복하지 않는다. 새 실패/변경/미해결 우려가 있을 때만 reviewer와 관련 범위 보정을 기록한다. source/test/plan 실행 전후 SHA·원문 snapshot·환경·counts/PASS/FAIL/ERROR/SKIP·elapsed·stdout/stderr 비노출·actual/mocked 경계와 cleanup을 scratch 원문 및 소형 offline JSON에 귀속한다. mock kill/wait는 실제 timeout/reap 근거와 구분하며 실제 연동 미실행은 BLOCKED/NOT_RUN으로 남긴다.

서명 검증기의 기존 crypto/과거 tests를 재실행하지 않는다. 이 artifact를 서명·required check 발행·보호설정·worker 활성화에 연결하지 않는다. native head launcher와 보호된 owner state/roster/assignment ledger·실제 tests/reviewer/issuer 연결은 후속이며 기존 sandbox EACCES 누적 세 실패·D17/D18 상한/연장 소진·보류를 재개하지 않는다.
