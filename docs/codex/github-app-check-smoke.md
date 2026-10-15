# GitHub App 진단 검사 발행·재조회

2026-10-15. [App 인증 조회](github-app-readback.md)와 [worker의 키 경로 거부 관찰](github-app-key-boundary.md) 다음의 작은 준비 단계다. [도구](../../src/ops/tools/github_app_check_smoke.py)는 지정 Draft PR의 고정 head에 `codex/emitter-smoke` 진단 검사를 만들거나 기존 항목을 찾아 재조회한다. [사전 검사 계획](github-app-check-smoke-test-plan.md)과 [개발 검사 근거](../testing/github-app-check-smoke-offline-20261015.json)를 따른다. 사용자 실행 보고와 별도 GitHub 저장 record 조회는 [발행 관찰](../testing/github-app-check-smoke-native-observed-20261015.json)로 구분한다. 현재 준비 판정은 [상태표](../status.md)에서 관리한다.

검사 결과는 항상 **completed/failure**다. GitHub의 실패 표시는 의도한 진단 결과이며 제품 검사를 실행한 결과가 아니다. 필수 검사 3개의 이름·성공 결과·자유 본문을 입력받지 않는다. 이 도구로 발행 경로를 확인해도 trusted emitter·독립 reviewer·G05 전체 binding·필수 검사/보호·무인개발/병합은 인수되지 않는다.

## 사용자 실행과 저장 기록 관찰

2026-10-15 13:28:17 KST 제출 출력은 Python3.10.12·SOURCE_SHA256_OK·COMPLETE/OBSERVED·smoke_exit2다. metadata3 GET·신규6 GET, token POST1·check POST1·폐기 DELETE1과 record 조회·PR 재확인·token 폐기 확인이 보고됐고 일곱 신뢰 flag는 false다. 출처는 사용자 터미널 출력이며 실제 실행 시각과 독립된 실행 source attestation은 미확인이다. 직전 검토 명령의 commit/source/auth SHA는 기대 pin이고 최신 출력 자체에는 commit이 인쇄되지 않았다. 이 실행 보고를 실제 Python3.10 전체 suite 통과로 합산하지 않는다.

별도 connector GET1회에서 같은 검사 id `116247156813`의 App·head·external_id·completed/failure·고정 output이 일치했다. 이는 저장 record의 보강 근거이며 사용자 명령의 실행 source·키/runtime 격리·token 폐기까지 독립 증명하지 않는다. 조회 시각 자체는 제공되지 않았다. 관찰 target은 당시 `a6d7351519cf0599a6f319500cc1b07176a02988` head로 보존한다. 이후 문서 commit의 새 PR head에 재결합하거나 최신 gate로 재사용·재발행하지 않는다. 기존 개발 검사와 실패/보류 근거는 그대로 유지한다.

## 실행과 권한 경계

Linux/Python3.10 이상, stdlib와 기존 `github_app_readback.py`의 키/서명/인증 helper를 사용한다. 기존 helper는 수정하지 않는다. 현재 키를 재사용하며 다른 키의 존재·교체를 추정하거나 키를 공유하지 않는다. 키 FD로 고정 OpenSSL 서명을 마친 뒤 닫고 App/설치/저장소 metadata 3 GET을 대조한다. PR head 전체·workflow·임의 실행 파일을 credential 보관 계정에서 실행하지 않는다.

**기본 lookup도 전역 read-only가 아니다.** 기본값의 check 발행은 0회지만 installation token 발급 POST와 폐기 DELETE가 있을 수 있다. token 요청은 대상 저장소 하나와 `checks:write`, `pull_requests:read`, `metadata:read`로 제한한다. 받은 grant·미래 UTC 만료·저장소 하나의 id/full_name과 token의 저장소 목록을 대조한다. App 전체 설치 저장소 목록이나 다른 저장소의 실제 접근 거부를 인수하는 검사는 아니다. token을 파일·stdout·stderr에 저장하지 않는다.

호스트는 `api.github.com` 고정이며 인증서/hostname 검증을 사용한다. proxy·redirect·자동 retry·endpoint override는 없다. 재사용 metadata는 최대3 GET, 신규 호출은 최대6 GET·token POST1·check POST1·폐기 DELETE1이다. JWT는 기존 metadata 3 GET과 token 발급에만, installation token은 신규 조회·check 발행·자기 폐기에만 사용한다. 신규 응답은128KiB·depth32 제한과 strict JSON 검사를 적용하고 재사용 metadata의 기존 제한은 유지한다.

## 대조와 부분 실패

1. 지정 PR의 number·open/draft/unmerged·base/head SHA와 같은 저장소 id/full_name을 확인한다. fork·ready·이동한 SHA는 차단한다.
2. 고정 이름/App/head의 목록을 `filter=all`로 한 번 조회한다. 100개 상한·불완전 count·추가 page·중복·다른 external_id·이미 success인 진단 record를 무시하고 새로 발행하지 않는다. 정확한 기존 failure record 하나면 그 id로 재조회한다.
3. 목록이 비어 있으면 기본 lookup은 CHECK_NOT_FOUND/BLOCKED다. 명시적인 `--emit-once`에서만 PR을 다시 확인하고 completed/failure check를 최대1회 POST한다.
4. 생성 응답과 별개로 같은 id의 저장 record를 GET해 App id/slug·head·이름·external_id·결과·고정 output을 대조한다. 그 뒤 PR pins를 다시 조회한다. 조회 사이의 PR 변경을 원자적으로 막는 기능은 없으며 최종 불일치는 BLOCKED다.
5. 형식이 유효한 token을 얻었으면 중간 실패에도 자기 폐기 DELETE를 한 번 시도한다. HTTP204만 폐기 확인이다. 폐기 실패는 전체 OBSERVED를 막고 기존 실패 이유도 보존한다.

external_id는 repository/PR/base/head/App/installation의 diagnostic scope hash다. 서버 idempotency key·서명·G05의 rules/plan/evidence binding이 아니다. 동시 invocation과 crash 사이의 중복을 원자적으로 막는 journal은 없다. check POST 응답 유실·손상에는 같은 invocation에서 추가 POST를 하지 않고 `create_outcome_unconfirmed=true`로 남긴다. 이후에는 lookup으로 기존 상태만 확인한다. 정상 생성 id 뒤 GET이 실패하면 id와 미확인 상태를 보존한다. 성공한 진단 발행을 반복하지 않는다.

token 발급 응답 유실·복구할 수 없는 JSON에서는 token 존재와 폐기를 확인할 수 없다. 외곽 timeout/kill 뒤 finally 실행이나 token 폐기를 보장하지 않는다. 메모리 zero를 증명하지도 않는다. 내부70초 deadline은 요청 전 협력적 검사, 신규5초 timeout은 I/O idle 제한이다. 전달 명령의 `timeout --kill-after=2s 90s`가 최종 외곽 제한이며 종료/출력 유실은 성공으로 해석하지 않는다.

## 고정 버전으로 실행할 절차

게시 후 실제 commit·두 source SHA와 대상 Draft PR/base/head를 넣은 명령을 전달한다. 임의 PR head checkout 대신 검토된 새 도구와 기존 auth helper **두 파일만** 임시 빈 `src/ops/tools` package에 놓고 hash를 확인한다. 고정 launcher를 `python3 -I -B`로 실행하며 현재 키·계정·권한·보호·worker 설정은 변경하지 않는다. 원본 key의 복사본이나 공유 파일을 만들지 않는다.

CLI 필수 인자는 `--app-id`, `--installation-id`, `--owner`, `--repository`, `--slug`, `--private-key`, `--pr-number`, `--base-sha`, `--head-sha`의 flag/value 9쌍이다. 선택 bare `--emit-once` 외 mode/name/conclusion/URL 옵션은 없다. 중복·`=`·축약·누락·help·추가 인자는 차단하며 실제 ID를 일반 코드에 하드코딩하지 않는다.

stdout은 허용된22필드 JSON 하나, 정상 관찰도 종료 코드는 **2**다. OBSERVED에는 metadata 대조·record GET·그 뒤 PR pins 재확인·token 폐기 확인이 모두 필요하다. `check_record_observed`만 true이거나 exit2만 나왔다고 전체 관찰 성공으로 처리하지 않는다. report는 check_run_id 외 계정·저장소·SHA·external_id·키 경로·credential·응답/예외 원문을 내보내지 않는다.

`input_origin_verified`, `independent_review_verified`, `binding_verified`, `trusted_gate_verified`, `runtime_isolation_verified`, `native_ready`, `merge_authorized`는 항상 false다. 사용자 실행 결과는 별도 출처로 기록해야 하며 로컬 TLS fixture 성공을 실제 GitHub/App 인수로 승격하지 않는다. 기존 sandbox EACCES 조사 보류와 D17/D18 누적·연장 소진도 유지한다.

공식 계약(2026-10-15 확인): [check runs](https://docs.github.com/en/rest/checks/runs), [token 발급·범위](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app), [token list/폐기](https://docs.github.com/en/rest/apps/installations), [PR 조회](https://docs.github.com/en/rest/pulls/pulls). API header는2026-03-10, create201/GET200/revoke204를 사용한다.
