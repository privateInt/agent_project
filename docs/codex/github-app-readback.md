# GitHub App 인증 metadata 수동 조회

2026-10-15. [등록 안내](github-app-registration.md) 다음의 **한 번 실행하는 읽기 도구**다. [사전 검사 계획](github-app-readback-test-plan.md), [격리 검사 근거](../testing/github-app-readback-offline-20261015.json)를 함께 읽는다. 실제 App 조회는 사용자 로컬 키와 서버 실행 결과가 필요하며 이번 개발 환경에서는 NOT_RUN이다.

## 받은 입력과 UI 관찰

사용자가 App ID `5323842`, installation ID `171552314`와 화면 9장을 제공했다. 화면에서 다음을 읽었다. 원본 이미지·Client ID·키·토큰은 저장소에 복제하지 않는다.

| 항목 | 화면에서 확인한 범위 |
|---|---|
| App 신원 | About에 Owned by `privateInt`, App ID `5323842` |
| 등록 입력 | 이름 `privateint-codex-checks`, Homepage `https://github.com/privateInt/agent_project` |
| 연결 옵션 | 빈 Redirect/Setup/Webhook URL·secret, Webhook Active·OAuth·Device Flow 해제, Only on this account 선택 |
| 요청 권한 | Checks 읽기·쓰기, Contents/Pull requests/Metadata 읽기. Metadata 화면은 Mandatory·Read-only 표시 |
| 설치 확인 화면 | **Install을 누르기 전** Only select repositories, `privateInt/agent_project` 1개와 위 네 권한 표시 |
| 설치 설정 URL | `/settings/installations/171552314` 숫자 경로 |

이는 사용자 제공 UI의 관찰이다. 저장된 설치 권한·전체 선택 저장소 목록의 인증 조회나 독립된 발행기 신뢰 근거가 아니다. 기존 [등록 spec](github-app-registration-spec.json)의 null/미확인 값은 당시 준비 snapshot으로 보존한다.

## 조회 범위

단일 파일 [github_app_readback.py](../../src/ops/tools/github_app_readback.py)는 Linux/Python 3.10 이상과 기존 `/usr/bin/openssl`을 사용한다. 키는 서버 안에서 RSA 서명에만 사용하며 JWT는 메모리에만 둔다. 다음 세 endpoint에 순서대로 HTTPS GET을 한 번씩 보낸다.

| 순서 | 고정 endpoint | 대조 대상 |
|---|---|---|
| 1 | `/app` | App ID·owner/User·slug, 요청 권한 네 항목 exact, events 빈 배열 |
| 2 | `/app/installations/171552314` | 설치 ID·App ID·account/User·승인 권한 exact·selected·정지 상태 없음 |
| 3 | `/repos/privateInt/agent_project/installation` | 해당 저장소의 설치가 같은 신원·권한 조건을 만족함 |

일반 코드는 실제 ID를 하드코딩하지 않고 CLI pin을 검증한다. HTTPS host는 `api.github.com` 고정, 인증서 검증 사용, proxy 환경·redirect·retry·endpoint override는 지원하지 않는다. 어떤 단계가 실패하면 뒤 GET을 보내지 않는다. installation access token 생성 POST, check 발행, 저장소/보호 설정 변경은 없다.

`repository_selection=selected`와 대상 저장소 포함을 확인해도 **다른 선택 저장소가 추가로 없다는 증명은 아니다.** 이 도구는 전체 repository list나 권한의 실제 행사, App 설치 제한/웹훅 설정 전체를 검증하지 않는다. 성공해도 [공통 GitHub 조건](common/GITHUB_GATES.md)의 trusted 실행·독립 reviewer·binding·필수 검사·보호/병합 인수는 남는다.

## 로컬 키 준비

실제 App 키 생성·사용은 이번 격리 검사에서 실행하지 않았다. 아래는 사용자 서버에서 인증 조회를 할 때의 절차다. 이미 신뢰된 로컬 키가 있으면 재사용하며 새 키를 반복 생성하지 않는다.

1. 저장소 밖의 사용자 전용 디렉터리 `~/.local/state/agent-project-github-app`을 mode `0700`으로 준비한다. 기존 파일을 덮어쓰지 않는다.
2. 해당 App의 GitHub 설정에서 Edit → Credentials → Key pairs를 확인한다. 사용할 키가 없다면 New key를 한 번 눌러 PEM을 내려받는다. 화면이 이 공식 경로와 다르면 해당 단계에서 멈추고 확인한다. 키 본문이나 파일을 이 대화·Git·PR·공유 자료에 업로드하지 않는다.
3. 기존에 신뢰한 SSH/SFTP 경로로 사용자 서버의 위 전용 디렉터리에 전달한다. 예시 실행 경로는 `~/.local/state/agent-project-github-app/private-key.pem`이다. 파일 mode는 `0600` 또는 `0400`, 소유자는 실행 사용자여야 한다. 다른 경로에 이미 안전하게 보관한 키는 옮기지 않고 `--private-key` 경로만 바꿀 수 있다.
4. 심볼릭 링크·하드 링크·빈/과대 파일·디렉터리·다른 사용자 소유·group/other 권한은 도구가 거부한다. 최종 파일 요소를 O_NOFOLLOW로 열며 부모 경로 전체의 nonsymlink 여부까지 증명하지 않는다.

키 파일은 읽기 전용 FD로 열고 `/usr/bin/openssl`에 전달한다. Python이 키 본문을 읽거나 임시 키 파일을 만들지 않는다. OpenSSL 실행 환경은 고정하며 config를 비활성화하고 빈 passin을 지정해 대화형 암호 입력을 막는다. 비어 있지 않은 암호 없이는 열 수 없는 키는 실패한다. 빈 암호로 암호화한 파일까지 일괄 거부한다고 주장하지 않는다. JWT는 RS256, iat=생성 시각-60초, exp=생성 시각+300초이며 출력하지 않는다.

이 사용자 전용 파일은 **PR head 실행과 격리된 worker credential store를 입증한 구성이 아니다.** 이번에는 고정된 검토 파일 하나를 수동 실행한다. 같은 UID의 미검토 코드에 키를 노출할 수 있는 구성으로 무인 worker를 활성화하지 않는다. 기존 sandbox EACCES 보류·재시도/연장 소진은 유지하며 이 조회 때문에 추가 조사를 시작하지 않는다.

## 검토된 파일 실행

게시된 실행 명령은 PR의 검토 commit과 source SHA-256을 고정한다. branch tip을 그대로 내려받아 실행하지 않는다. 키 파일은 다운로드한 probe의 임시 디렉터리에 넣지 않는다. Python은 `-I -B`로 실행하며 project import/PYTHONPATH 설정은 필요 없다.

CLI 필수 값은 다음과 같다. 아래는 인자 표이며 단독 실행 명령이 아니다.

| 인자 | 이번 사용자 입력 |
|---|---|
| `--app-id` | `5323842` |
| `--installation-id` | `171552314` |
| `--owner` | `privateInt` |
| `--repository` | `privateInt/agent_project` |
| `--slug` | `privateint-codex-checks` |
| `--private-key` | 서버에 보관한 로컬 PEM 경로 |

외곽 실행에는 `timeout --kill-after=2s 45s`를 사용한다. GET의 10초는 I/O idle timeout이며 전체 요청의 hard deadline이 아니다. OpenSSL 서명은 5초 timeout으로 제한한다. DNS·응답 지연을 포함한 전체 종료선은 외곽 timeout이며 단순 합계 35초 보장을 하지 않는다. 시간 초과 시 중단된 실행을 성공으로 보지 않는다.

표준 출력에는 허용된 JSON 요약 하나만 나온다. 비밀·경로·JWT·응답 원문·예외 traceback은 출력하지 않는다. 공유할 결과는 이 JSON뿐이다. **항상 exit 2**이며 OBSERVED도 CI gate 성공이 아니다. `--help`와 잘못된 인자도 sanitized BLOCKED JSON/exit 2다.

정상 결과는 `scope=AUTHENTICATED_APP_METADATA_ONLY`, `status=OBSERVED`, `stage=COMPLETE`, `get_attempts=3`, `identity_metadata_verified=true`다. 이 값은 세 인증 metadata 응답이 pin과 일치했다는 뜻이다. 나머지 여덟 범위/신뢰 boolean은 false로 유지한다. `input_origin_verified=false`는 이 출력 자체의 provenance attestation이나 pin의 출처를 인증한 signed receipt가 아니라는 뜻이다. native_ready·merge_authorized를 true로 바꾸지 않는다.

실패하면 stage/reason과 시도 수를 확인한다. `KEY_INVALID`는 소유·권한·파일 조건, `SIGNING_FAILED`는 키·OpenSSL·서명 조건, HTTP/metadata 오류는 해당 읽기 단계의 미완료다. 원문 비밀을 출력하거나 인증서/권한 검사를 완화해 재시도하지 않는다. 새 결과는 기존 실패와 구분해 기록한다.

## 공식 근거

2026-10-15 열람. 격리 검사와 실제 App 인수를 구분한다.

- [App JWT 인증 endpoint](https://docs.github.com/en/rest/apps/apps): `/app`, `/app/installations/{installation_id}`, `/repos/{owner}/{repo}/installation`
- [JWT RS256·issuer·시간 claim](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
- [App private key 관리](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)
- [OpenSSL dgst](https://docs.openssl.org/3.0/man1/openssl-dgst/)
- [Python 3.10 http.client](https://docs.python.org/3.10/library/http.client.html)
