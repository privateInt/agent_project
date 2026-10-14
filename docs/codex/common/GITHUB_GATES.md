# 독립 검토와 GitHub 병합 조건

공통 배포판: 1.3-preparation. 현재는 검토 가능한 적용 명세이며 GitHub 관리 설정이나 자동 병합이 적용됐다는 보고서가 아니다.

## 검토 책임

구현과 별개인 reviewer가 변경·검사 계획·실행 근거를 확인한다. 사용자가 매번 코드를 직접 검토할 필요는 없다. 중요한 통과 기준 변경·권한/비용 정책 변경·중요도 판단이 모호한 사안은 사용자에게 넘긴다. 동등한 사소 변경은 reviewer가 판단하고 이유를 기록한다. 대기 중인 중요 결정과 독립적인 작업은 계속할 수 있다.

GitHub의 사람 승인 1명을 모든 PR에 필수로 두면, 사용자와 작성자가 같은 계정인 현재 구성에서 밤마다 작업이 멈출 수 있다. 독립 에이전트의 판정은 아래 전용 필수 검사로 연결한다. 작성자 계정의 APPROVE나 PR에 적힌 PASS 문장을 독립 검토로 세지 않는다. 보호 설정과 신뢰된 발행기를 실제 검증하기 전에는 자동 병합하지 않는다.

## 필수 검사 명세

| 검사 이름 | 성공에 필요한 근거 | 실패·누락 처리 |
|---|---|---|
| `codex/required-tests` | 사전 승인 계획의 적용 대상 필수 검사 전부 성공, 동일 source/build 식별자 | FAIL·SKIP·미실행·환경 미준비·알 수 없는 결과는 차단 |
| `codex/independent-review` | 구현에 참여하지 않은 reviewer의 판정, 최신 대상·규칙·계획·검사 근거와 결합 | author PASS·자체 승인·오래된 판정은 거부 |
| `codex/policy` | 규칙 배포 해시, 보호 대상 유지, 중요 기준 변경의 사용자 결정 증거 | 필수 기준 완화·결정 미확인·규칙 변조는 차단 |

위 이름은 새 발행기의 계약이다. 현재 CI 검사나 GitHub App이 존재한다고 가정하지 않는다. 각 검사는 확인한 신뢰 App ID로 고정한다. ID 미확인·누락·임의 앱 허용 `-1`은 적용 불가다. 연결 도구의 App을 검사 발행기로 임의 지정하지 않는다.

증거 결합 키는 `repository`, `pr_number`, `base_sha`, `head_sha`, 공통 release digest, 검사 계획 digest, 필수 실행 근거 digest다. 어느 하나라도 바뀌면 재검증하고 이전 성공을 폐기한다. 공개 PR comment는 설명이며 강제 gate 자체가 아니다.

## 신뢰 경계

발행기는 protected base 또는 별도의 보호된 실행 환경에서 고정된 evaluator로 동작한다. PR head가 수정한 PASS JSON·workflow·helper를 그대로 신뢰하지 않는다. head 코드에는 status/admin credential을 주지 않는다. privileged `pull_request_target`에서 head 코드를 실행하지 않는다. 관리자 설정 변경 권한과 일반 검사 실행 권한을 분리한다.

현재 원격 main의 보호가 없는 상태는 신뢰된 base가 확보됐다는 뜻이 아니다. 발행기의 설치·실행 모델과 격리, 권한·독립성 증명은 다음 단계의 사전 검사 계획과 실제 거부 인수로 검증한다. GitHub App 생성·credential 생성·worker 활성화는 이번 문서에 포함되지 않는다.

## 적용 값과 순서

대상은 `privateInt/agent_project`, `privateInt/vms_project`의 `main`이다. 현재 보호·ruleset·base/head·관리권한을 다시 조회하고, 기존 보호가 있으면 보존하며 이번 차이만 적용한다. 아래는 GitHub REST 보호 API의 필드로 연결할 명세이며 즉시 전송할 요청 본문은 아니다.

| 보호 필드 | 적용할 값 |
|---|---|
| `required_status_checks.strict` | `true`: base 최신 상태의 검사 요구 |
| `required_status_checks.checks` | 위 3개 `{context, app_id}`; 실제 확인한 정수 App ID만 사용 |
| `enforce_admins` | `true` |
| `required_pull_request_reviews.required_approving_review_count` | 신규 구성은 `0`: 사람 승인을 상시 요구하지 않고 독립 reviewer 필수 검사로 통제. 기존 사람 승인 규칙은 확인 없이 제거하지 않음 |
| `required_pull_request_reviews.bypass_pull_request_allowances` | 신규 우회 허용 없음 |
| `allow_force_pushes`, `allow_deletions` | `false` |
| `required_conversation_resolution` | `true` |
| `restrictions` | 개인 저장소에 조직 전용 push 제한 객체를 적용하지 않음 |

1. trusted 발행기와 최신 커밋의 실제 검사 성공·실패·위조 거부를 확인한다.
2. 관리 권한 있는 경로에서 보호를 적용하고 readback으로 필드와 App ID를 대조한다. 일부 성공이나 403은 전체 완료가 아니다. 불명확한 응답은 조회로 확인한 뒤 재개한다.
3. 격리 PR에서 필수 검사 실패·누락·동명 다른 앱·stale base/head·author PASS가 병합을 막는지 확인한다. 신규 head push와 base 이동 후 이전 검토가 재사용되지 않는지도 검사한다.
4. 모두 확인한 뒤 저장소 `allow_auto_merge`와 해당 PR의 auto-merge를 순서대로 활성화한다. 최신 head 조건으로 병합하고 실제 remote main commit을 검증한다. 현재 draft PR을 이 문서만으로 ready/merge 전환하지 않는다.

현재 GitHub 연결은 콘텐츠/PR 작업이 가능하지만 관리 setter가 없고 branch-protection 상세 조회는 403이다. branch 요약과 ruleset 조회로 보호 없음은 관찰했으나 403을 상세 보호 설정의 성공 조회로 기록하지 않는다. 브라우저 관리 화면도 별도 로그인이 필요한 상태다. 이번에는 관리 설정을 변경하지 않았다.

공식 API: [보호 브랜치](https://docs.github.com/en/rest/branches/branch-protection), [자동 병합](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/automatically-merging-a-pull-request), 확인 2026-10-14. 설계의 실패·중복·원자성·권한 검사는 [사전 계획](PREPARATION_TEST_PLAN.md)의 G01~G10을 따른다. 발행기·적용기 구현 및 실제 인수는 후속 범위다.
