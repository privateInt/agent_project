# App diagnostic check 발행·재조회 사전 검사 계획

계획 v1 / 2026-10-15. 부모 `03f4587b99526f99f47b359f232ddf3134d41eeb`, branch `codex/github-check-smoke-20261015`, 격리 worktree 시작 clean. [GitHub 기준](common/GITHUB_GATES.md)과 [준비 계획](common/PREPARATION_TEST_PLAN.md)의 G03/G05/G06, [검사 기준](common/TESTING.md)의 D29/D30/D32/D35/D38을 유지한다. reviewer가 이 파일의 정확한 SHA를 사전 확인한 뒤 구현한다. root는 source/guide/status, test_agent는 새 검사/소형 근거, reviewer는 사전·최종 검토를 맡는다.

변경은 정확 6파일이다. 새 `src/ops/tools/github_app_check_smoke.py`, `src/ops/tools/tests/test_github_app_check_smoke.py`, 이 계획, `docs/codex/github-app-check-smoke.md`, `docs/testing/github-app-check-smoke-offline-20261015.json`과 기존 `docs/status.md` 준비 안내행 1개다. 공통 정책·과거 근거와 기존 소스/검사는 바꾸지 않는다. 기존 App readback/키 경계/28·46검사 및 과거 변이는 재실행하지 않는다.

## 좁은 완료선과 재사용

고정 이름 `codex/emitter-smoke`의 **completed/failure diagnostic**을 App으로 만들거나 기존 항목을 찾아 재조회하는 수동 도구다. required 3개 context의 이름을 받거나 성공을 발행하는 기능은 없다. 기본 lookup도 installation token 발급 POST와 폐기 DELETE가 있을 수 있어 전역 read-only가 아니다. 개발 환경에서는 실제 App 키·GitHub API·사용자 서버·보호/worker/병합에 접근하지 않는다. 실제 GitHub 생성/재조회 인수는 별도 native NOT_RUN으로 남긴다.

`from src.ops.tools import github_app_readback as auth`로 기존 pin/key/JWT/metadata helper를 재사용한다. 기존 키 안전성·실제 RSA/검증·metadata 읽기 근거는 [이전 계획](github-app-readback-test-plan.md)과 [이전 검사 근거](../testing/github-app-readback-offline-20261015.json)를 재사용한다. 새 suite에서 과거 crypto 전체를 반복하지 않고 새 호출 경계와 실패 전파·FD close를 확인한다. 재사용 source/test는 다음 SHA와 부모 bytes 모두 보존한다.

| 기존 파일 | SHA-256 |
|---|---|
| `src/ops/tools/github_app_readback.py` | `97e8fdd2675a6d08c14d9f70da06dfb360165002bf99ec93f92ebac2c1f7f1b3` |
| `src/ops/tools/tests/test_github_app_readback.py` | `c2ac4580c6b9dc0d5ecdeceb44a6e138795b0e920811236270d736ac4b23551b` |

## 고정 계약

| 대상 | 계약 |
|---|---|
| API/CLI | `smoke(pins,key_path,pr_number,base_sha,head_sha,emit_once=False)->report`, `main(argv=None,output_stream=None)->2`. pins는 auth의 정확 5키/`pins_checked` 계약. pr은 bool 아닌 int 1..2^63-1, base/head는 lower hex40, emit은 정확 bool. CLI 필수 9 flag/value 쌍은 `--app-id/--installation-id/--owner/--repository/--slug/--private-key/--pr-number/--base-sha/--head-sha`; 선택 bare `--emit-once`는 1회만. 순서 자유, 중복/`=`/축약/누락/extra/`--help` 차단. 실제 ID·개인 경로 하드코딩0 |
| 인증 전처리 | 입력이 틀리면 key/sign/network0. auth key open/sign/close 뒤 auth.GitHubReader의 정확 3 GET과 App/두 installation metadata 대조를 재사용한다. 실패 시 token POST0. 키/JWT/응답/예외 원문 비노출, 임시 secret 파일0. 기존 helper의 Linux/FD/서명 안전 계약을 약화하지 않음 |
| token 발급 | JWT로 `POST /app/installations/{id}/access_tokens` 최대1. body 정확 `{repositories:[repo-name],permissions:{checks:write,pull_requests:read,metadata:read}}`; contents 권한 없음. 응답 token은 ASCII graphic 1..4096자, expires_at은 정확 `YYYY-MM-DDTHH:MM:SSZ`이며 미래. permissions는 정확 3map, repository_selection=selected, repositories 정확1의 full_name/양의 strict id 확인. 정상 형식의 token을 얻으면 뒤 grant 실패에도 finally 폐기 시도. malformed/유실로 token을 얻지 못하면 발급/폐기 상태를 미확정으로 두고 재발급0 |
| token scope 조회 | installation token으로 `GET /installation/repositories?per_page=100&page=1` 최대1. total_count는 bool 아닌 int1, repositories 길이1·같은 id/full_name. next Link 거부. 이는 이 token의 범위 대조이며 App 전체 설치 선택목록 인증으로 확대하지 않음 |
| PR pins | `GET /repos/{repo}/pulls/{pr}` 최대3. number/open/draft=true/merged=false를 strict 타입으로 확인. base/head SHA와 각 repo.full_name/id는 예상 repo 및 token repo id와 동일. fork/다른 repo·이동한 SHA·closed/ready/merged는 차단. emit 직전 및 record GET 뒤 PR을 다시 조회하며 이전 PR 결과를 재사용하지 않음 |
| 목록 | `GET /repos/{repo}/commits/{head}/check-runs?check_name=codex%2Femitter-smoke&filter=all&per_page=100&page=1&app_id={id}` 최대1. strict total_count==len(check_runs)<=100, next Link 차단. 해당 name/App/head/external_id의 정확한 failure record 0/1만 허용. 잘못된 App·external_id·head, 이미 success인 record·중복·불명확 row를 무시하고 새 POST하지 않음 |
| lookup/create | 기존1개면 양의 id로 GET→PR 재확인, check POST0. 0개+기본 lookup이면 CHECK_NOT_FOUND/BLOCKED. 0개+emit_once면 PR 재확인→check POST 최대1→GET→PR 최종 재확인. GET `/repos/{repo}/check-runs/{id}` 최대1. POST response나 GET 실패·유실에 같은 invocation의 추가 check POST0. DB/journal/서버 idempotency·동시 invocation 원자성은 제공하지 않음 |
| 고정 check body | POST `/repos/{repo}/check-runs`는 정확 name/head_sha/status/conclusion/external_id/completed_at/output 7키. name=`codex/emitter-smoke`, status=`completed`, conclusion=`failure`; completed_at은 현재 canonical UTC. output은 정확 title/summary 2키. title=`Diagnostic smoke only`; summary=`Intentional failure. This diagnostic does not approve required checks, independent review, policy, binding, runtime isolation, or merge.` 자유 text/이름/conclusion/URL 입력0 |
| record 대조 | id는 양의 strict int, name/app.id+slug/head_sha/status/conclusion/external_id와 output.title/summary를 대조한다. GET id는 목록/정상 생성응답의 같은 id. server의 다른 metadata는 무시하고 출력하지 않으며 response URL을 따라가지 않는다. 목록과 POST 응답이 GET의 실제 저장 상태 대조를 대신하지 않음 |
| 폐기 | 유효 형식 token을 얻었으면 finally `DELETE /installation/token` 최대1, HTTP204만 token_revoked_verified=true. 실패 시 기존 실패 stage/reason을 보존하고, 원래 성공 경로면 REVOKE/TOKEN_REVOKE_UNCONFIRMED/BLOCKED. work deadline 종료에도 폐기 1회는 시도할 수 있으나 외곽 kill·메모리 zero·폐기 성공을 보장하지 않음 |
| transport/상한 | 신규 Client는 api.github.com 고정, 인증서/hostname 검증, 환경 proxy·redirect·retry·endpoint override0. JWT는 token 발급에만, installation token은 나머지 신규 endpoint에만. 메서드/경로별 상한과 순서를 강제한다. 신규 GET<=6/token POST<=1/check POST<=1/DELETE<=1, 기존 metadata GET<=3. counters는 connect/요청 시도이며 wire 성공 수가 아님. 신규 I/O idle timeout5초, body 읽는 중128KiB cap, strict UTF-8/duplicate key/NaN/Infinity/정수 token20자/depth32 거부. 재사용 metadata는 기존 idle10초/depth64를 유지 |
| 시간/실행 | 시작 기준 내부70초는 요청 전 확인하는 협력적 deadline이며 hard wall-clock이 아님. 폐기 시도는 별도 best effort. 사용자 명령의 `timeout --kill-after=2s 90s`가 전체 외곽 제한이며 kill 뒤 폐기를 주장하지 않음. native는 검토·hash 고정한 두 source 파일만 빈 src/ops/tools package로 구성하고 고정 launcher를 `python3 -I -B`로 실행. PR head 전체나 workflow/helper 임의 실행0 |

external_id는 prefix `agent-project-emitter-smoke-v1:` 뒤에 다음 SHA256 hex를 붙인다. domain은 정확 `b'agent-project:github-app-check-smoke:v1\n'`, body는 `{repository,pr_number,base_sha,head_sha,app_id,installation_id}` 6필드를 `json.dumps(...,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')`한 값이다. 같은 diagnostic scope 식별자이며 G05의 rules/plan/evidence binding이나 인증된 receipt가 아니다. external_id를 GitHub의 server idempotency key라고 부르지 않는다.

독립 고정 vector는 repository=`Example/demo`, pr=31, base=`a`×40, head=`b`×40, app=17, installation=29일 때 `agent-project-emitter-smoke-v1:7a1a37f6fba06620f43a7d6f7d6ee7eb1a50d01e0f6558fc6bcc5273312ec581`이다. 여섯 필드 각각의 변화가 식별자를 바꾸는지 검사한다.

## 출력과 실패 보존

report는 정확 22키다: `scope,status,stage,reason,identity_metadata_verified,metadata_get_attempts,api_get_attempts,token_post_attempts,check_post_attempts,token_delete_attempts,check_run_id,check_record_observed,pr_pins_rechecked,token_revoked_verified,create_outcome_unconfirmed,input_origin_verified,independent_review_verified,binding_verified,trusted_gate_verified,runtime_isolation_verified,native_ready,merge_authorized`.

scope=`APP_CHECK_SMOKE_ONLY`, status=OBSERVED/BLOCKED, counters는 strict int이며 위 상한 이내, check_run_id는 null/양의 strict int다. 나머지 값은 bool이며 마지막 7개 trust flag는 항상 false다. metadata 3개 대조 완료·record GET 완료·그 뒤 PR pins 재확인·token 폐기204가 모두 true인 경우만 COMPLETE/OBSERVED다. `pr_pins_rechecked`는 마지막 record 조회 뒤 재확인 완료만 뜻한다. 정상201 record/id를 받은 뒤 GET 실패라면 id를 보존하고 check_record_observed=false다. check POST 시도 후 유효한 생성응답을 받지 못하면 create_outcome_unconfirmed=true이며 실제 생성0이라고 단정하지 않는다. 생성 전 실패/기존 조회에는 false다.

stage 허용값은 ARGUMENTS/KEY/SIGN/APP/INSTALLATION/REPOSITORY/TOKEN/TOKEN_SCOPE/PR/CHECK_LIST/PR_RECHECK/CHECK_CREATE/CHECK_READBACK/REVOKE/COMPLETE/INTERNAL이다. reason 허용값은 OBSERVED/ARGUMENTS_INVALID/KEY_INVALID/SIGNING_FAILED/READ_NOT_ALLOWED/HTTP_STATUS_INVALID/HTTP_TRANSPORT_FAILED/RESPONSE_INVALID/APP_MISMATCH/INSTALLATION_MISMATCH/REPOSITORY_INSTALLATION_MISMATCH/UNEXPECTED_FAILURE/TOKEN_ISSUE_UNCONFIRMED/TOKEN_SCOPE_INVALID/PR_PINS_MISMATCH/CHECK_LIST_INVALID/CHECK_NOT_FOUND/CHECK_CREATE_UNCONFIRMED/CHECK_RECORD_MISMATCH/TOKEN_REVOKE_UNCONFIRMED/DEADLINE_EXCEEDED다. 허용 밖 예외 필드는 INTERNAL/UNEXPECTED_FAILURE로 가린다.

출력이 가능하면 stdout JSON 1개/항상 exit2, stderr 원문0이다. key/path/JWT/token/Authorization/response/예외/계정·repo·SHA·external_id를 report에 내보내지 않는다(check_run_id만 허용). 응답 유실 뒤에는 lookup만 준비하고 성공을 반복 발행하지 않는다. 원래 실패·부분 생성·폐기 불명확을 최신 성공으로 덮지 않는다.

## 선정 검사

아래 20주제를 약20 methods와 필요한 subtests로 묶는다. product key/JWT/token 대신 dummy값을 사용한다. 실제 TLS loopback은 임시 공개 canary RSA/인증서를 `/usr/bin/openssl`로 생성하고 격리 서버에만 연결한다. 제품의 고정 host/TLS context를 먼저 검사한 test-only factory가 loopback+fixture CA로 매핑하며 제품 CLI 우회는 없다. 기존 stdlib 전역을 patch하지 않고 module namespace proxy를 써서 원래 HTTPSConnection을 보존한다. 환경 미준비는 NOT_RUN/SKIP로 남겨 사용 가능 판정을 막으며 가짜 응답 PASS로 대체하지 않는다.

| ID | 입력/상황 | 필수 통과 기준 |
|---|---|---|
| S01 | API/CLI 정상·순서·9쌍·emit bare, bool/범위/대소문자/누락/중복/`=`/축약/help/extra | 잘못된 입력은 key/sign/HTTP0, raw argv0, 항상2 |
| S02 | auth key/sign/close 경계·실패·private 예외 | 기존 helper를 실제 호출 경계로 재사용, 실패 뒤 metadata/token0, FD close/예외 비노출. 과거 crypto suite 재실행0 |
| S03 | metadata 정상3GET와 App/설치/repo 불일치/중간실패 | 기존 identity 조건과 3시도 상한 유지, 실패 뒤 token/check POST0, metadata flag 정확 |
| S04 | token POST의 JWT/body, token/expiry/grant/repo 응답 오류 | 요청 exact1repo/3권한/contents0, strict bounded token·canonical 미래 expiry, 과다/부족/다른repo 차단 |
| S05 | token repo-list의 total_count/len/id/full_name/next Link | 정확1repo·동일id만 수용, malformed/bool/다른repo/pagination 차단, 전체App목록 인증 주장0 |
| S06 | 최초 PR의 number/open/draft/merged/base/head/repo id/full_name | 개별 strict 불일치 및 fork/ready/closed/merged 차단, check POST0 |
| S07 | 독립 literal external_id vector·각6필드 변경·동일입력 | canonical/domain 정확, 전필드 민감도, G05 전체binding/trust flag 승격0 |
| S08 | check 목록0/1·wrong App/head/external_id·동일scope 중복·이미success·invalid/count/Link | 잘못된 행을 무시해 재생성하지 않음, 모두 check POST0/BLOCKED |
| S09 | 목록0 + 기본 lookup | CHECK_NOT_FOUND/BLOCKED, token 발급/폐기 경계 관찰, check POST0 |
| S10 | 기존 정상failure 목록1→GET→PR 재확인, GET실패 | 기존 양의id 재사용·check POST0, 저장record/PR 후 검증 필수, 실패면 id/한계 보존 |
| S11 | emit 정상 실제 TLS 전체 순서 | tokenrepo/PR/list/prePR/create/GET/postPR/revoke 정확. 서버가 수신한 payload exact7/output2·failure 확인, 독립 고정응답으로 OBSERVED/exact22 |
| S12 | POST/GET id·App id/slug·head/name/external/status/conclusion/output 개별 오류 | 잘못된 response 차단. 정상생성 뒤 GET실패는 id 보존/recordfalse. M2는 정상POST 뒤 GET만 wrong app.id |
| S13 | create 직전 PR 이동, record GET 뒤 PR 이동/ready/closed | 전자는 create0, 후자는 create<=1이나 BLOCKED/pr_rechecked=false, stale 성공0 |
| S14 | 상태형 서버가 생성 후 response 연결종료/timeout/유실/invalid201 | 같은 invocation check POST<=1/retry0, create_outcome_unconfirmed 정확. 별도 lookup fixture가 기존record를 찾으면 재생성0; 동시성·crash 원자성 주장0 |
| S15 | token 추출 뒤 grant실패/중간실패/성공, revoke 비204·실패·deadline | finally DELETE<=1, 원래실패 보존·원래성공의revoke실패 차단. token POST유실은 미확정/재발급0, kill후폐기 보장0 |
| S16 | 실제 TLS/HTTP 401/403/404/5xx·302·socket오류, 두 TLS서버 redirect, JSON/cap/depth | 비허용 status 차단·후속중단·redirect target 요청/Auth0. valid JSON+공백으로 cap+1 경계, duplicate/nonfinite/int20/depth32 거부; raw0 |
| S17 | Client 허용method/path/방문횟수·토큰Auth분리·고정host/cert·proxy환경 | 허용 외 network0, 신규GET<=6/tokenPOST1/checkPOST1/DELETE1+metadataGET3, retries0·부적절 credential전송0 |
| S18 | 정상/오류/unknown ProbeError/비공개 sentinel·write/flush/broken pipe | exact22/허용enum·strict값·7false, checkid 외 비공개값0, stdout허용JSON·stderr원문0·exit2 |
| S19 | AST3.10/import와 pinned 두파일의 빈package+고정launcher | stdlib+auth만, 기존auth source/test/plan/근거 bytes불변. thin `python3 -I -B`의 invalidCLI도 sanitized2. 실제3.10/실제App 미실행 별도 |
| S20 | guide/status/소형receipt·6파일 scope/links/전후hash·실패이력 | 현재상태 한row, 기본lookup의tokenPOST/DELETE·고정failure·no retry/외곽timeout·nativeNOT_RUN·G06미인수 명시. 실제/대역/미실행·counts/time/source/test/plan SHA 기록 |

S20의 Git working-state/문서 범위 감사는 reusable unit 밖 일회성 검사다. 최초·최종 검사 원문을 scratch에 보존해 test 보정이 생겨도 실행별 SHA/결과를 소급 연결하지 않는다. 새 관련 suite 1회와 아래 2변이만 수행하고 새 변경/실패/미해결 우려 없이는 반복·확대하지 않는다.

## D38·남는 인수

reviewer 제안 M1은 **실제 create payload의 conclusion failure→success**다. S11의 loopback 서버가 받은 body assertion이 정상 PASS→변이 assertion FAIL/ERROR0이어야 한다. 응답을 요청에서 파생하지 않아 잘못된 payload가 정상처럼 반사되는 fixture를 쓰지 않는다. M2는 **readback app.id guard 제거**다. token/PR/POST 응답은 정상, GET record만 well-formed wrong app.id이고 나머지 모든 guard는 정상인 S12 target이 정상 BLOCKED/PASS→변이 잘못 OBSERVED/assertion FAIL·ERROR0을 검출해야 한다. 최종 reviewer가 정확 target/오류를 확인한다. mutation 전후 원본 source/test/plan SHA, mutant SHA·원인·counts와 복사본 삭제를 기록한다. 문법/import/환경 ERROR는 검출 성공이 아니다.

실제 GitHub의 한정 diagnostic 발행/GET/폐기 인수는 후속 사용자 실행 결과가 있어야 하며 현재 NOT_RUN이다. TLS loopback 성공은 App의 실제 권한·GitHub 저장상태 인수를 대신하지 않는다. 그 native 관찰도 evaluator 신뢰환경·author 격리·독립 reviewer·G05 전체binding·세 required check 성공·보호/자동병합·두repo 최종목표의 완료가 아니다. 현재 키에 관한 사용자 설명만 유지하며 별도키 존재/교체를 추정하지 않는다. key/Library 접근0, 기존 sandbox EACCES 보류와 D17/D18 누적·연장 소진 유지.

공식 계약은 root가 2026-10-15 확인한 [check runs](https://docs.github.com/en/rest/checks/runs), [App token](https://docs.github.com/en/rest/apps/apps), [installation list/revoke](https://docs.github.com/en/rest/apps/installations), [PR 조회](https://docs.github.com/en/rest/pulls/pulls)를 참조한다. API version header는 `2026-03-10`, create201/GET200/revoke204이며 실제 실행/비밀을 문서에 포함하지 않는다.
