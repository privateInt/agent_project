# GitHub 검사 발행용 App 등록·첫 설치

2026-10-15. [권한 제안 spec](github-app-registration-spec.json)과 [사전 검사 계획](github-app-registration-test-plan.md)에 따른 수동 등록 안내다. **이번 변경은 문서 준비만 완료한다.** 실제 App·설치·승인 권한은 미확인(UNVERIFIED), 두 ID는 null이다. spec은 자체 검토용이며 GitHub App Manifest·API 요청·import 파일이 아니다.

현재 연결 도구에는 App 등록·설치나 Checks 쓰기·보호 설정 변경 기능이 없다. 아래 등록·설치는 소유자가 GitHub 설정 화면에서 수행할 단계다. 자동 실행용 payload나 명령은 제공하지 않는다.

## 1. 소유 계정과 기존 App 확인

기존 repository 조회에서 `privateInt/agent_project`의 소유자는 개인 계정 `privateInt`(type=User), 저장소는 public으로 확인됐다. 현재 로그인한 계정을 뜻하지 않으므로 먼저 GitHub 계정이 `privateInt`인지 확인한다. 조직 계정 경로를 대신 사용하지 않는다.

[개인 계정 GitHub Apps](https://github.com/settings/apps)에서 기존 **전용 검사 발행 App**이 있는지 먼저 확인한다. 존재 여부는 아직 UNKNOWN이다. 있다면 아래의 등록 설정과 해당 App의 **설치 시 실제 승인 권한·선택 저장소**를 따로 대조한다. 등록 요청 권한과 설치에 승인된 권한은 같다고 가정하지 않는다. 불일치는 보고하고 이 단계에서 권한 확대나 중복 생성으로 해결하지 않는다. 현재 연결 도구의 App을 검사 발행기로 임의 지정하지 않는다.

기존 전용 App이 없다면 [새 GitHub App 등록](https://github.com/settings/apps/new)으로 이동해 다음 제안값을 입력한다. 불명확한 UI나 이름 충돌은 해당 항목을 미확인으로 남기고 알린다. 이름 가용성은 UNKNOWN이다.

## 2. 등록 화면의 제안값

| 항목 | 값 |
|---|---|
| 소유 계정 | `privateInt` 개인 계정 |
| GitHub App name | `privateint-codex-checks` |
| Homepage URL | `https://github.com/privateInt/agent_project` |
| Callback URL / Setup URL | 비움 |
| Request user authorization (OAuth) during installation | 해제 |
| Enable Device Flow | 해제 |
| Webhook Active | 해제; Webhook URL·secret 비움 |
| Subscribe to events | 선택 없음 |
| Where can this GitHub App be installed? | Only on this account |

Repository permissions는 다음 네 항목만 요청한다. 표시되는 Metadata 값도 Read-only인지 확인한다.

| 화면 항목 | 수준 | 이번 제안의 용도 |
|---|---|---|
| Checks | Read and write | 후속 발행기의 check run 기록 |
| Contents | Read-only | 대상 코드·문서·근거 읽기 |
| Pull requests | Read-only | PR의 대상·base/head 확인 |
| Metadata | Read-only | 저장소 식별 정보 읽기 |

그 외 repository 권한과 account·organization·enterprise 권한은 모두 No access다. Administration, Contents 쓰기, Actions 쓰기, Workflows 쓰기를 추가하지 않는다. 저장 직전 소유 계정·네 권한·Webhook 해제·설치 제한을 다시 대조한 뒤 Create GitHub App을 누른다. 등록·설치 화면의 계정·권한·설치 대상이 이 제안과 정확히 일치할 때만 Create/Install을 진행하고, 다르면 중단 후 알린다. 여기서는 private key나 token을 생성·다운로드·업로드·전달·저장하는 작업을 진행하지 않는다.

이 값은 프로젝트의 요청 제안이다. GitHub가 저장한 등록 설정이나 실제 installation grant를 확인한 결과가 아니다.

## 3. 첫 설치

해당 App의 설정에서 Install App을 열고 `privateInt`를 선택한다. 설치 화면의 요청 권한을 다시 확인하고 **Only select repositories → agent_project 하나**를 선택한 뒤 Install을 누른다. All repositories를 선택하지 않는다. 기존 설치가 있으면 새 설치를 만들기 전에 현재 승인 권한과 선택 저장소부터 확인한다.

선택 저장소는 이 설치의 권한 부여 범위다. 공개 저장소의 일반 읽기까지 차단한다는 뜻은 아니다. 첫 설치에 `vms_project`를 추가하지 않으며, [공통 GitHub 기준](common/GITHUB_GATES.md)의 최종 두 저장소 목표(`privateInt/agent_project`, `privateInt/vms_project`)는 유지한다. VMS 설치 확대와 인수는 후속 단계다.

## 4. 공유할 확인값과 후속 검증

공유할 값은 비밀이 아닌 **App ID와 installation ID 두 개**다.

- App ID: 해당 App의 General 설정에서 확인한다. Client ID와 구분한다.
- installation ID: [설치 목록](https://github.com/settings/installations)에서 해당 App의 설치 설정을 열었을 때 URL에 `/settings/installations/<number>`가 실제로 보이면 그 숫자를 확인한다. 보이지 않으면 UNKNOWN으로 남긴다. 이를 찾기 위해 token을 만들거나 비밀을 읽지 않는다.

사용자가 알려 준 ID는 인증 readback 전까지 주장값이다. 이후 App 소유자·identity, installation의 대상 계정·선택 저장소·실제 승인 권한을 인증된 조회로 대조해야 한다. 지금은 이름 가용성·기존 App 존재·실제 ID·승인 권한이 모두 미확인이다. 이 문서 변경에서 등록·설치·key/token 생성·check 발행·보호/auto-merge 변경·worker 활성화는 각각 0건이다.

등록·설치 후에도 신뢰된 실행 환경과 독립 reviewer 근거, 최신 base/head와 artifact 결합, 위조·누락·실패 거부 인수가 남는다. [기존 구조 검사](github-check-audit-test-plan.md)와 [binding 필드 검사](github-evidence-binding-test-plan.md)의 성공도 실제 발행기 신뢰나 병합 허가를 뜻하지 않는다. 공통 필수 검사 3개와 G03/G05/G06 실제 인수는 그대로 적용한다. 기존 sandbox EACCES 조사 보류와 소진된 재시도·연장 허용량도 이 단계로 해제하지 않는다.

## 공식 근거

2026-10-15 열람. UI 절차·권한 근거이며 실제 계정/App 상태의 조회 근거는 아니다.

- [GitHub App 등록](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
- [요청 권한과 설치 승인 권한](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)
- [자신의 App 설치·선택 저장소와 public 읽기](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)
- [Check run 생성의 Checks 쓰기 권한](https://docs.github.com/en/rest/checks/runs#create-a-check-run)
- [App ID·installation ID 구분](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
