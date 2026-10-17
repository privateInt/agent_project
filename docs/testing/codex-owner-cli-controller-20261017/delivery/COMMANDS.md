# 실행 패키지 명령서 (실제 설치/운영 변경 미실행)

소스 commit: 37fb6b9e1f1492776ab84eff6fc4f7f5d614d2a9. Launcher hash: 54ed5d524d1dd6acc470a2bbc52671079cd77ee4579d40605ab0ca1de477f4cc. Manifest hash: a6f8cfef7cc5e3f1a11676b314e2abc3515fddbdfef3608f7dc7a72abe5cfe2d. UID: 1011.
모든 설치 파일과 DB 계약은 bundle/manifest.json, schema-contracts.json에 있다.
새 코드/설정의 기대값은 이 검토 산출물에서 외부로 고정한 것이며 소유자 승인값이 아니다.
기존 var 후보의0775 상위경로를 보존하고 target은 /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14로 제안한다.
bootstrap/source 부모는0700이어야 한다. 기존 /opt·automation_mvp·service/timer/PAUSE는 수정하지 않는다.

실행 계정은 codex_shshin이다. 이미 해당 계정으로 로그인했으면 그대로 진행한다.
다른 관리 계정에서는 `sudo -iu codex_shshin`으로 전환한다. HOME을 수동 변경하거나 root로 실행하지 않는다.
`id -un`이 codex_shshin, `id -u`가 1011인지 확인하고 `cd /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project`로 이동한다.
아래 명령은 승인 후에만 실행한다. 이번 작업은 temp fixture 대상에서만 같은 명령을 검사한다.

```sh
set -eu
cd /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project
printf '%s  %s\n' '54ed5d524d1dd6acc470a2bbc52671079cd77ee4579d40605ab0ca1de477f4cc' '/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/agent-execution-deliveries/build/20261017/package-37fb6b9e1f14/bundle/installation/launch.py' | sha256sum -c -
/usr/bin/python3 -I -S -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/agent-execution-deliveries/build/20261017/package-37fb6b9e1f14/bundle/installation/launch.py verify-bundle --expected-manifest-sha256 a6f8cfef7cc5e3f1a11676b314e2abc3515fddbdfef3608f7dc7a72abe5cfe2d --expected-code-version 37fb6b9e1f1492776ab84eff6fc4f7f5d614d2a9 --expected-uid 1011
/usr/bin/python3 -I -S -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/agent-execution-deliveries/build/20261017/package-37fb6b9e1f14/bundle/installation/launch.py stage --expected-manifest-sha256 a6f8cfef7cc5e3f1a11676b314e2abc3515fddbdfef3608f7dc7a72abe5cfe2d --expected-code-version 37fb6b9e1f1492776ab84eff6fc4f7f5d614d2a9 --expected-uid 1011
printf '%s  %s\n' '54ed5d524d1dd6acc470a2bbc52671079cd77ee4579d40605ab0ca1de477f4cc' '/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14/installation/launch.py' | sha256sum -c -
/usr/bin/python3 -I -S -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14/installation/launch.py init-new --expected-manifest-sha256 a6f8cfef7cc5e3f1a11676b314e2abc3515fddbdfef3608f7dc7a72abe5cfe2d --expected-code-version 37fb6b9e1f1492776ab84eff6fc4f7f5d614d2a9 --expected-uid 1011
/usr/bin/python3 -I -S -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14/installation/launch.py verify --expected-manifest-sha256 a6f8cfef7cc5e3f1a11676b314e2abc3515fddbdfef3608f7dc7a72abe5cfe2d --expected-code-version 37fb6b9e1f1492776ab84eff6fc4f7f5d614d2a9 --expected-uid 1011
/usr/bin/python3 -I -S -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14/installation/launch.py status --expected-manifest-sha256 a6f8cfef7cc5e3f1a11676b314e2abc3515fddbdfef3608f7dc7a72abe5cfe2d --expected-code-version 37fb6b9e1f1492776ab84eff6fc4f7f5d614d2a9 --expected-uid 1011
```

현재 PAUSE를 유지한 run 확인은 별도 동작이다. 설치 후 현재 PAUSE가 유지되면 REJECTED/PAUSED exit2가 기대값이다. 임시 fixture 인수의 exit1과 구분한다.

```sh
set -eu
printf '%s  %s\n' '54ed5d524d1dd6acc470a2bbc52671079cd77ee4579d40605ab0ca1de477f4cc' '/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14/installation/launch.py' | sha256sum -c -
/usr/bin/python3 -I -S -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14/installation/launch.py run --expected-manifest-sha256 a6f8cfef7cc5e3f1a11676b314e2abc3515fddbdfef3608f7dc7a72abe5cfe2d --expected-code-version 37fb6b9e1f1492776ab84eff6fc4f7f5d614d2a9 --expected-uid 1011
```

복귀는 아래 별도 명령으로 수행한다. 현재 candidate를 보존하여 퇴역시키며 기존 서비스는 전환하지 않는다.

```sh
set -eu
printf '%s  %s\n' '54ed5d524d1dd6acc470a2bbc52671079cd77ee4579d40605ab0ca1de477f4cc' '/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14/installation/launch.py' | sha256sum -c -
/usr/bin/python3 -I -S -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14/installation/launch.py rollback --expected-manifest-sha256 a6f8cfef7cc5e3f1a11676b314e2abc3515fddbdfef3608f7dc7a72abe5cfe2d --expected-code-version 37fb6b9e1f1492776ab84eff6fc4f7f5d614d2a9 --expected-uid 1011 --rollback-to /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/runtime-candidate/pr25-37fb6b9e1f14.retired
```

init-new는 없는state에만, 이미있으면거부하며 자동migration0이다. run은 기존root/user/package PAUSE와23–07KST/최대3 시작을 유지한다.
고정 fixture 정상과exit7의 실제 artifact를 저장하므로 두번째 결과는 FAILED이며 run exit1이 정상 인수 기대값이다. UNKNOWN은재실행하지않고exit3/원래claim을보존한다.
rollback은 exclusive lock 뒤 이번 candidate 전체를 같은parent 새이름으로rename하며 DB/근거를삭제하지않는다. 충돌은덮지않는다. 서비스가활성화된환경의전환명령은포함하지않는다.

## 지원되는 native 연결 최소 대안 (미실행)

queue/list handler의 부작용 계약을확인하지못해가상adapter/직접RPC를추가하지않았다.
공식 CLI reference의codex queue를 기존소유client 경로로사용할수있다. 아래는승인된task1건의명령형태이며자동재전송이나완료확인계약은별도필수다.

```sh
/home/codex_shshin/.local/bin/codex queue --approve-for-me -C /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project --thread 01a13fb0-1721-7af3-82e6-747eac4de411 --message '<승인된 단일 작업>'
```

이명령은이번작업에서실행하지않았다. CLI 문서 https://learn.chatgpt.com/docs/cli/reference#codex-queue .
전체controller native dispatch·자동작업두건·설치·서비스전환·PAUSE해제는NOT_RUN이다.
