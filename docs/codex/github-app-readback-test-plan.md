# GitHub App 인증 metadata 읽기 사전 검사 계획

계획 v1 / 2026-10-15. 부모 `5e91e4bff2460e33fff95ec99ea32899d911a973`, branch `codex/github-app-readback-20261015`. 새 worktree 시작 clean과 기존 AGENTS 동일성을 확인했다. [GitHub 기준](common/GITHUB_GATES.md), [준비 계획](common/PREPARATION_TEST_PLAN.md)의 G03/G05/G06, [검사 기준](common/TESTING.md)의 D29/D30/D38을 유지한다. 독립 사전 검토 후 구현하며 실제 App 인수를 대신하는 offline API를 추가하지 않는다.

변경은 새 5파일만: `src/ops/tools/github_app_readback.py`, `src/ops/tools/tests/test_github_app_readback.py`, 이 계획, `docs/codex/github-app-readback.md`, `docs/testing/github-app-readback-offline-20261015.json`. root는 소스/가이드, test_agent는 검사/소형 근거, reviewer는 사전·최종 검토를 맡는다. 기존 소스·정책·46검사·실행 근거는 변경/재실행하지 않는다.

## 관찰과 완료선

사용자 제공 ID와 9장 UI를 root가 읽은 결과는 입력 근거다. App About의 소유자/App ID와 등록 제안 설정은 보였지만, 설치 권한/선택 1개 화면은 **Install 전 확인 화면**이다. 마지막 installation URL 관찰도 저장된 grant 본문이나 인증 App readback을 대신하지 않는다. guide에서 UI 관찰·사용자 주장·후속 인증 조회를 분리한다.

제품은 Python 3.10 stdlib와 기존 `/usr/bin/openssl`로 사용자가 수동 실행하는 단일 파일 도구다. 이번 개발 환경에서 실제 App 키 생성·사용, GitHub 인증 조회, token 생성 POST, 쓰기/발행/보호/worker/merge는 0건이다. 후속 native 실행은 검토된 안내를 따라 사용자 로컬 키로 수행하고 새 결과를 받아야 한다. 성공 범위도 **인증된 세 metadata 응답의 pin 대조**뿐이다. 선택 저장소 전체 목록·grant에 추가 repo가 없는지는 세 GET으로 증명하지 않는다.

## 고정 API와 경계

| 대상 | 확정 계약 |
|---|---|
| CLI/pin | 필수 `--app-id`, `--installation-id`, `--owner`, `--repository owner/repo`, `--slug`, `--private-key` 로컬 경로. 실제 ID를 일반 코드에 하드코딩하지 않음. ID는 bool 아닌 int 1..2^63-1, CLI는 canonical 양의 십진수. owner는 ASCII `[A-Za-z0-9-]{1,39}`, repo token은 `[A-Za-z0-9_.-]{1,100}`이며 `.`/`..` 금지, owner/repo-owner 정확히 일치. slug는 `[a-z0-9-]{1,100}`. trim/case 보정 없음 |
| API | `ProbeError(stage,reason)`, `pins_checked(app_id,installation_id,owner,repository,slug)->dict`(정확히 해당 5키), `open_key_checked(path)->fd`, `sign_jwt(fd,app_id,now=None)->str`, 내부 `GitHubReader(jwt,pins).get_json(path)`, `probe(pins,key_path)->report`, `main(argv=None,output_stream=None)->2`. probe가 Reader를 내부 생성한다. 외부 evidence/transport/endpoint 입력 없음; 검사는 monkeypatch만 사용 |
| 키 | `os.open`은 readonly+O_NOFOLLOW+O_NONBLOCK+O_CLOEXEC 후 fstat. 최종 경로 요소 nonsymlink, regular/euid 소유, mode 정확0400/0600, nlink1, 크기1..16384bytes. 부모 디렉터리 nonsymlink까지 증명한다고 하지 않음. 최초 fd가0/1/2면 fcntl.F_DUPFD_CLOEXEC으로3이상에 복제 후 원본 close, 반환 fd>=3으로 subprocess stdio 충돌 방지. 실패·성공 모두 FD close, 경로 재열기/Python key본문 읽기 없음 |
| 서명 | 고정 `/usr/bin/openssl dgst -sha256 -sign /proc/self/fd/<fd> -passin pass: -sigopt rsa_padding_mode:pkcs1`, shell=False, pass_fds로 FD 유지. stdin은 signing payload만, 실제 key/JWT/경로를 openssl argv·로그에 넣지 않음. stderr 폐기, timeout5초·child 회수. env 정확 `{PATH:'/usr/bin:/bin',LANG:'C',LC_ALL:'C',OPENSSL_CONF:'/dev/null'}`. 임시 secret 파일 없음. malformed/암호 없이는 열 수 없는 key/non-RSA는 비대화형 차단 |
| JWT | header alg=RS256, typ=JWT, iss=App ID, iat=now-60, exp=now+300. 생성 시각 기준 미래 유효300초이며 두 claim 간격은360초다. 호출당 JWT 하나, 출력/저장/자동갱신 없음 |
| HTTP | 인증서 검증을 켠 `http.client.HTTPSConnection`의 host는 api.github.com 고정. 순서대로 GET `/app`, `/app/installations/{id}`, `/repos/{owner}/{repo}/installation`만 1회씩. endpoint/인증서 우회 CLI 없음. proxy 환경을 사용하지 않고 redirect/retry/POST 없음. 허용 경로·중복·최대3 attempt guard. `get_attempts`는 connect 실패도 포함한 시도 수이며 실제 전송 성공 수가 아님 |
| 응답/상한 | HTTP200을 body 읽기 전에 확인. GET I/O idle timeout10초, body는 읽는 중128KiB 제한. UTF-8 strict JSON, 중첩 duplicate key/NaN/Infinity·정수 token20자 초과·depth64 초과/RecursionError 거부. unknown metadata는 무시하되 pin 필드는 strict 타입. 실패 시 뒤 GET0. idle timeout은 요청 hard wall-clock이 아니며 native 명령의 외곽 `timeout --kill-after=2s 45s`가 전체 제한이다. 단순3×10+5로 hardwall을 주장하지 않음 |
| App 대조 | id=App pin, owner.login/type=expected owner/User, slug pin, requested permissions 정확 `{checks:write,contents:read,pull_requests:read,metadata:read}`, events 정확[] |
| 두 installation 대조 | 각각 id=installation pin, app_id=App pin, account.login/type=owner/User, permissions 정확 같은4개, repository_selection=selected, suspended_at가 **존재하며 null**. repository-install 응답도 동일 identity/rights 조건. app_slug/target_type 등 비선정 metadata를 새 인증 근거로 세지 않음 |
| 출력 | 출력 가능하면 stdout JSON 1개, stderr 원문0, CLI 항상 exit2(OBSERVED도 CI 성공 아님). argparse/예외/출력 I/O 오류에 raw argv·key/path/JWT/response/ID/owner/repo/slug·traceback 비출력. fixed enum 외 reason/stage 금지. 고정 host/인증/키 안전 기준을 완화하는 fallback 없음 |

결과는 정확히 14키: `scope,status,stage,reason,get_attempts,identity_metadata_verified,selected_repository_list_verified,input_origin_verified,trusted_gate_verified,binding_verified,native_ready,merge_authorized,independent_review_verified,ui_observations_are_authenticated_evidence`.

`scope=AUTHENTICATED_APP_METADATA_ONLY`, status는 OBSERVED/BLOCKED, attempts는0..3. identity_metadata_verified는 세 GET 대조가 모두 끝났을 때만 true, 나머지 여덟 boolean은 항상 false다. input_origin_verified=false는 report/pin의 provenance attestation 미검증이며 좁은 metadata 대조와 구분한다. stage는 ARGUMENTS/KEY/SIGN/APP/INSTALLATION/REPOSITORY/COMPLETE/INTERNAL, reason은 OBSERVED/ARGUMENTS_INVALID/KEY_INVALID/SIGNING_FAILED/READ_NOT_ALLOWED/HTTP_STATUS_INVALID/HTTP_TRANSPORT_FAILED/RESPONSE_INVALID/APP_MISMATCH/INSTALLATION_MISMATCH/REPOSITORY_INSTALLATION_MISMATCH/UNEXPECTED_FAILURE만 허용한다.

## 선정 검사

아래 20주제를 methods/subtests로 작게 구현한다. 제품은 파일/서비스를 쓰지 않지만 검사 fixture의 임시 RSA/인증서·HTTP 서버는 격리 생성/정리한다. production 키가 아니며 실제 App/API에 연결하지 않는다.

| ID | 입력/관찰 | 통과 기준 |
|---|---|---|
| A01 | CLI/pin 정상·누락/초장문·bool/범위·owner/repo 불일치·path/control 삽입·미지 argv | 정확한 pin만 수용, 잘못되면 signing/HTTP0, argv echo0 |
| A02 | mode0400/0600, 빈/과대·group/other·wrong owner·symlink/hardlink·dir/FIFO | 정해진 regular 파일만 수용, FIFO 비차단·invalid key HTTP0, FD leak0 |
| A03 | 격리 subprocess의 stdin 닫힘→최초fd0, open 뒤 경로 교체·실패 중 FD 상태 | fd>=3 예약 후 실제 sign/verify 성공(HTTP0), 같은 열린 파일 사용, 경로 재열기0·항상 close |
| A04 | 임시 RSA 실제 서명과 독립 openssl verify, 고정 now | signature 검증 PASS, header/iss/iat/exp 정확, 출력 JWT0 |
| A05 | malformed/비어 있지 않은 암호로 보호한 RSA/EC key, openssl 부재/nonzero/timeout | prompt/HTTP0, child/FD 회수, generic BLOCKED. EC fixture 생성 불가면 이유와 미실행을 분리 |
| A06 | 서명 subprocess 호출/환경 capture | 고정 실행파일·RSA sigopt·passin·pass_fds·timeout·env·stdin payload·stderr 폐기, secret argv/temp파일0 |
| A07 | fixed Reader의 경로·중복·attempt cap·요청 순서 | 정확3 GET·고정host·Authorization/JSON 헤더·cert 검증, 다른 경로/추가 attempt/network0 |
| A08 | 실제 TLS loopback 정상3응답, dummy JWT | 정상 OBSERVED. 테스트만 HTTPSConnection factory를 loopback+fixture CA로 매핑하고 제품 요청 host/cert context 검증. CLI 우회 없음 |
| A09 | 두 TLS 서버와302 Location, 정상처럼 보이는302 metadata body | 정상 BLOCKED, redirect target 요청/Authorization 전달0. M2는 별도로 비200 오수용을 검출 |
| A10 | 401/403/404/500·TLS/cert 오류·socket timeout·중간 실패 | 고정 stage/reason, 실패 후 GET0·retry0, 실제 시도 수 정확 |
| A11 | cap 경계/초과·malformed UTF-8/JSON·duplicate/NaN/Infinity·거대 정수/깊이 | body 읽기 중 cap, strict 거부·원문0·후속 GET0 |
| A12 | App id/owner/User/slug/events 개별 오류·bool id | pin/type 불일치 차단, unknown/private metadata는 신뢰/출력하지 않음 |
| A13 | installation2응답의 id/app/account/selected/suspended 누락·비null | 각 응답 독립 대조, identity/rights 불일치 시 metadata_verified=false |
| A14 | App/installation permissions 추가·누락·상향/오타/타입 | 정확4map만 수용. App 정상·두 installation만 같은 추가/상향 권한도 차단(M1) |
| A15 | 정상/모든 오류 결과 | exact14키·enum·attempt·상태 분리, 여덟 false 고정·전체 selected repo 검증 주장0 |
| A16 | private sentinel을 argv/key path/응답/예외에 삽입·broken output | stdout 허용 JSON 외0, stderr/raw/traceback0, 항상 exit2 |
| A17 | argument/key/signing 실패 및 첫/둘째/셋째 조회 실패 | 전자 HTTP0, 후자는 실패 뒤 요청0, FD/child/connection 정리 |
| A18 | 실제 loopback 인증서 검증과 proxy 환경·단일 파일 실행 | 신뢰하지 않는 인증서 거부, env proxy 사용0, `python3 -I -B` standalone 지원·추가 의존성 없음 |
| A19 | guide의9장 UI관찰/수동 로컬 key·외곽timeout·pin·공유범위 | pre-Install 화면을 saved grant로 승격0, 사용자 key/JWT 공유/업로드 요청0, whole selected list·신뢰/게이트/병합 한계 명시 |
| A20 | 소스/검사 AST3.10·import·링크·5파일 diff·소형 receipt | stdlib+기존 openssl만, 제품 write/POST/설치/서비스 변경0. 실제3.10 실행 여부 별도, 기존46suite 재실행0. 실행 전후 source/test/계획 SHA·횟수/시간·PASS/FAIL/ERROR/SKIP 기록 |

## D38와 종료

reviewer 선정 M1은 **installation permissions exact-map 차단 제거**다. App 요청4권한은 정상이고 두 installation에만 동일한 추가/상향 권한을 넣어 다른 일관성 guard가 대신 차단하지 않게 한다. M2는 **HTTP200 status guard 제거**다. 302에 정상 metadata JSON을 넣으면 잘못 OBSERVED가 되어 BLOCKED 기대 assertion이 실패해야 한다. 이는 비200 수용 검출이며 Authorization 전달 검출로 부르지 않는다. 두 서버 nofollow/전달0은 A09의 별도 필수 검사다.

각 target 정상 PASS→격리 복사본 assertion FAIL·ERROR0, mutant SHA·원인·원본 불변·복사본 삭제를 기록한다. 문법/import 실패는 검출 성공이 아니다. 관련 새 suite와 두 변이만 실행하고 새 변경/실패 없이 확대·반복하지 않는다. 소형 근거와 최종 독립 검토를 연결한 뒤 native는 NOT_RUN으로 남긴다. 실제 readback 성공도 전체 저장소 grant·trusted execution·독립 reviewer·binding·필수 check·보호/merge 인수를 끝내지 않는다. 기존 sandbox EACCES 보류와 D17/D18 누적/소진 이력은 유지한다.

공식 근거: [JWT/RS256과 App ID issuer](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app), [OpenSSL dgst](https://docs.openssl.org/3.0/man1/openssl-dgst/), 2026-10-15 열람. guide는 조회 endpoint의 최신 공식 계약과도 대조한다.
