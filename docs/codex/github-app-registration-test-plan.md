# GitHub App 등록 준비 문서 검사 계획

계획 v1 / 2026-10-15. 부모 `c9bfb1e8e506c09e09394a49a49420e0e7539a12`, branch `codex/github-app-registration-20261015`. 새 worktree의 시작 clean만 확인했다. [GitHub 기준](common/GITHUB_GATES.md), [준비 계획](common/PREPARATION_TEST_PLAN.md)의 G03/G05/G06, [검사 기준](common/TESTING.md)의 D29/D38을 유지한다.

범위는 새 `docs/codex/github-app-registration-test-plan.md`, `github-app-registration.md`, `github-app-registration-spec.json`의 3문서다. 이 계획의 독립 사전 검토 후 guide/spec을 작성한다. source/기존 정책·검사·근거 변경, 새 검사 코드·네 번째 보고서는 없다. 결과와 3파일 SHA는 작업/PR 기록에 남기고 독립 최종 검토를 받는다.

완료선은 사용자가 후속 수동 등록·첫 설치에 참고할 **문서 준비**다. 이번에는 등록/설치·key/token 생성/전송/저장·check 발행·보호/auto-merge 변경·worker 활성화를 하지 않는다. spec은 자체 검토용이며 Manifest/API/import용이 아니다.

## 고정 제안과 미확인 상태

기존 connector의 repository owner 조회 `privateInt`, owner_type=`User`, public을 준비 입력으로 사용한다. 현재 브라우저 로그인이나 App 소유자 확인을 대신하지 않는다. 개인 등록 URL은 `https://github.com/settings/apps/new`, 이름 제안은 `privateint-codex-checks`(가용성 UNKNOWN), Homepage는 `https://github.com/privateInt/agent_project`다.

기존 전용 App의 등록 설정과 실제 installation grant를 **별도로** 확인하는 순서를 생성보다 앞에 둔다. 불일치는 보고하고 권한 확대/중복 생성으로 해결하지 않는다. 현재 connector App을 emitter로 임의 재사용하지 않는다. 요청 권한과 실제 승인 권한은 다르며, 첫 Selected repositories는 `privateInt/agent_project` 하나다. 이는 설치 grant 범위로, 공개 자료 일반 읽기까지 차단한다는 뜻이 아니다. 최종 `agent_project`+`vms_project` 목표는 유지한다.

실제 App ID/installation ID는 null, 실제 grant는 미확인이다. 사용자에게 공유를 요청할 값은 비밀 아닌 두 ID뿐이며 인증 readback 전까지 주장값이다. App ID는 App General에서, installation ID는 설치 설정 URL에 `/settings/installations/<number>`가 보일 때만 읽는다. 안 보이면 UNKNOWN으로 남기며 token/비밀을 읽거나 만들지 않는다.

## 고정 자체 검토 spec

각 객체의 키는 아래 목록과 **정확히 일치**해야 한다. 값·타입·배열 원소/순서도 고정한다. `1`은 bool이 아닌 정수, false는 boolean, null은 null이다. 빈 callback URL은 빈 배열로 표현한다.

| 객체 | 정확한 키/값 |
|---|---|
| root | `schema_version:1`, `kind:"manual_registration_self_review"`, `not_for:["github_app_manifest","github_api","import"]`, `proposal:{…}`, `actual:{…}`, `scope:{…}` |
| proposal | `owner:{login:"privateInt",type:"User"}`, `name:"privateint-codex-checks"`, `homepage_url:"https://github.com/privateInt/agent_project"`, `registration_url:"https://github.com/settings/apps/new"`, `registration_scope:"only_this_account"`, `permissions:{…}`, `webhook:{…}`, `auth:{…}`, `installation:{…}` |
| proposal.permissions | `repository:{checks:"write",contents:"read",pull_requests:"read",metadata:"read"}`, `account:{}`, `organization:{}` |
| proposal.webhook | `active:false`, `events:[]` |
| proposal.auth | `callback_urls:[]`, `setup_url:""`, `request_oauth_on_install:false`, `device_flow:false` |
| proposal.installation | `repository_selection:"selected"`, `repositories:["privateInt/agent_project"]` |
| actual | `app_id:null`, `installation_id:null`, `suggested_name_availability:"UNKNOWN"`, `existing_dedicated_app:"UNKNOWN"`, `registration_verified:false`, `installation_verified:false`, `approved_permissions:null`, `approved_repository_selection:null`, `ids_authenticated:false`, `trusted_emitter_verified:false` |
| scope | `final_target_repositories:["privateInt/agent_project","privateInt/vms_project"]`, `secrets_requested:false`, `performed_by_this_step:{…}` |
| scope.performed_by_this_step | `registration:false`, `installation:false`, `key_token_creation:false`, `check_emission:false`, `protection_change:false`, `auto_merge_activation:false`, `worker_activation:false` |

## 선정한 정적 검사

| ID | 방법 | 통과 기준 |
|---|---|---|
| R01 / strict JSON | 일회성 stdlib의 object_pairs_hook/parse_constant로 UTF-8 읽기, 위 shape와 재귀 대조 | 중첩 duplicate key·NaN/Infinity 거부, root/nested exact 키·타입·값 일치. 의미 항목을 자유 텍스트로 대체하지 않음 |
| R02 / 권한·옵션 | 고정 proposal을 완전 대조 | repository 4권한만, Metadata Read-only 확인. account/org 빈 객체, Enterprise 등 그 외 No access. Administration·Contents write·Actions write 없음. only_this_account, inactive webhook/구독0, guide의 webhook URL/secret 비움, callback/setup 비움, OAuth/device false, selected repo 정확1개 |
| R03 / guide-spec | UI 입력·계정·App/설치 점검 순서·ID 위치를 교차 확인 | 이름/URL/owner/권한/옵션 동일. 로그인/기존 전용 App 확인이 생성보다 앞섬. 등록 설정과 실제 설치 grant 별도 점검·불일치 보고. ID 미표시 UNKNOWN, 선택 grant/public read 구분, 최종2repo 보존 |
| R04 / 제안·실제 | 본문과 actual/scope의 완료·신뢰 주장 대조 | 이름 가용성 UNKNOWN, 실제 ID null/grant UNVERIFIED. 요청과 승인 권한 구분. 이번 등록/설치 실행0, 사용자 ID는 인증 readback 전 claim. 신뢰/발행/보호/auto-merge/worker 활성화 완료 주장 없음 |
| R05 / 비밀·금지 행동 | 세 문서 전체를 읽고 비밀 literal·생성/업로드/전달/저장·자동 등록/import 지시 확인 | 비밀0·비밀 생성/전달 요청0. 공유 대상은 두 ID뿐. spec을 제출할 명령이나 credential/서비스 활성화를 후속 절차로 끼워넣지 않음 |
| R06 / 링크·보존 | 상대 링크·공식 근거·whitespace·부모 대비 Git diff/새 파일 목록 대조 | 변경 정확3문서, source/기존 test/common/과거 근거 byte 불변. 깨진 링크·네 번째 보고서 없음. 준비 단계의 문서 결과는 실제 App/설치 readback과 구분 |
| R07 / 독립 검토 | 사전 계획 SHA, 최종 3파일 SHA와 R01~06 결과 연결 | 필수 문서 검사 PASS·지적 해소. 실제 등록/설치·인증 readback·trusted emitter/G03/G05/G06 실제 인수는 NOT_RUN/UNVERIFIED 유지. 문서 PASS를 실행 준비/병합 허가로 승격하지 않음 |

공식 근거는 root가 열람한 [수동 등록](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app), [check run 생성](https://docs.github.com/en/rest/checks/runs#create-a-check-run)과 guide를 대조한다. 불명확한 UI는 미확인으로 남기고 권한을 넓히지 않는다.

소스/실행 로직 변경이 없어 기존 46개 suite·새 검사 코드/fixture·변이/D38·Go/DB/GPU/LLM/브라우저 E2E는 적용 대상이 아니다. 기존 필수 기준이나 실제 인수를 면제하지 않는다. 사전 PASS는 guide/spec 작성의 시작 조건뿐이다. 기존 sandbox EACCES 보류·D17/D18 누적 미확인·재시도/연장 소진과 제품 실패 이력도 유지한다.
