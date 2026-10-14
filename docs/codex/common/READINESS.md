# 모델 호출 없는 실행 준비 점검

공통 배포판: 1.2-preparation. CLI 메타데이터를 확인하는 도구의 사용 범위다. 기능 개발·무인 실행·설치·로그인·사용량 초기화·구매를 수행하지 않는다.

## 도구 원본과 검증

구현은 Agent 저장소의 `src/ops/tools/codex_readiness.py` 한 곳에서 관리한다. VMS에 같은 코드를 복제하지 않는다. 두 프로젝트는 검토한 동일 버전과 해시를 사용한다. [Agent 준비 PR](https://github.com/privateInt/agent_project/pull/1)의 해당 커밋과 `docs/testing/codex-readiness-20261014.json`의 파일 해시를 대조한다. PR 최신 head를 검토된 고정 버전으로 가정하지 않는다.

Python 3.10 이상의 stdlib만 사용한다. 공식 출처와 버전을 확인한 기존 Codex 실행 파일만 대상으로 한다. 이 도구는 CLI 설치나 로그인 대신 사용할 수 없다. 원본 운영 경로에 새 파일을 덮어쓰지 않고 검토한 개발 worktree에서 실행한다.

```bash
python3 -B -m unittest src.ops.tools.tests.test_codex_readiness
```

이 검사는 합성 CLI·계정 응답과 임시 파일을 사용한다. 실제 계정·서버·추론·크레딧 제어가 검증됐다는 뜻은 아니다.

## 서버에서 조회할 순서

1. 실행 계정, 개발 worktree의 remote/branch/HEAD와 미커밋 변경, 기존 공식 CLI의 경로·버전을 비공개 실행 기록에서 확인한다. 작업 소유 계정과 인증 소유 계정을 구분한다.
2. 해당 계정의 기존 인증을 그대로 사용한다. API 인증 환경이 있으면 조회가 차단되며 다른 계정의 인증을 복사하지 않는다. 어떤 환경 변수가 있는지만 기록하고 값은 출력하지 않는다.
3. 아래 `--expected-uid`에는 사전에 확인한 실행 계정의 UID를 직접 입력한다. 실행 중인 계정의 UID를 자동 대입해 계정 확인을 생략하지 않는다. `--codex`에는 확인한 실행 파일 경로, `--worktree`에는 확인한 개발 worktree를 입력한다.

```text
python3 -I -B src/ops/tools/codex_readiness.py --expected-uid VERIFIED_UID --codex VERIFIED_CLI_PATH --worktree VERIFIED_WORKTREE
```

4. 허용 목록으로 요약한 JSON만 확인한다. 인증 원문·전체 환경·CLI 원문 응답을 파일이나 대화에 붙이지 않는다. 실제 서버 관찰 근거와 이 환경의 합성 검사 근거를 구분한다.

## 판정 범위

| 항목 | 도구가 확인하는 범위 | 남는 확인 |
|---|---|---|
| 계정·설정 | 지정 UID, 조회한 ChatGPT 인증 방식, 유효 기본 설정 | 인증 부수 효과와 전문 역할별 실제 설정 |
| 모델·사용량 | 카탈로그의 목표 모델/effort, 그 시점의 반환된 한도 | 실제 모델 실행·사용량 초기화와 재개 정책 |
| 크레딧 | 잔액·reset 횟수로 미사용 제어를 추정하지 않음 | 계정에 적용되는 차감 금지의 강제 제어 |
| 프로세스 | 요청·출력·페이지 상한, 같은 process group 종료 | 별도 세션으로 이탈하는 신뢰할 수 없는 실행 파일은 대상 제외 |

전체 판정은 준비 미확인 항목 때문에 항상 `BLOCKED`, 확인한 정책 충돌이 있으면 `FAIL`이다. 개별 `PASS`를 worker 시작 승인으로 해석하지 않는다. `native_environment=unverified`는 호스트·운영 상태·인증 부수 효과를 이 도구만으로 인수하지 않았다는 뜻이다.

`auth.json`은 내부 해시 비교만 하고 복사·출력하지 않는다. 파일이 같아도 keyring·cache 등의 전체 무변경 증거로 취급하지 않는다. app-server의 `refreshToken:false`는 강제 refresh를 요청하지 않는 조건이며 모든 내부 인증 변경을 막는 보증이 아니다.

## 실행 상한과 후속 경계

전체 60초 안에서 version 3초, 요청 5초, 종료 1+1초를 사용한다. stdout 512 KiB·한 줄 128 KiB·stderr 64 KiB·notification 32개·모델 5페이지를 제한한다. 쓰기 대기도 deadline을 적용하고 stderr 원문은 버린다. 지원하지 않는 응답·필수 필드 누락/형식 오류·timeout은 차단하고 추가 미사용 필드는 폐기한다. 이 한계의 계약과 검사는 [사전 검사 계획](PREPARATION_TEST_PLAN.md)에 있다.

크레딧 미사용·역할별 설정·총 동시 실행·신뢰된 reviewer 검사 발행·GitHub 보호 인수는 [실행 준비](EXECUTION_SETUP.md)와 [병합 명세](GITHUB_GATES.md)의 별도 완료 조건이다. 이 도구는 worker, status emitter, 관리 설정 적용기를 포함하지 않는다.

공식 계약 확인: [app-server](https://learn.chatgpt.com/docs/app-server), [설정](https://learn.chatgpt.com/docs/config-file/config-reference), [사용량](https://learn.chatgpt.com/docs/pricing), 2026-10-14.
