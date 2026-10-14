# Codex 비활성 준비 설정 적용

설정 준비 단계이며 기능 개발·모델 실행·worker 설치가 아니다. 단일 구현은 `src/ops/tools/codex_prepare_config.py`에 둔다. VMS는 별도 코드를 복제하지 않는다. 공통 1.2-preparation 정책은 그대로 사용한다.

## 확인된 입력과 남은 조건

2026-10-14 사용자가 제공한 서버 조회 결과에서 CLI 0.162.1, 실행 계정 일치, ChatGPT 인증 방식, `gpt-6.1-sol`/`xhigh` 카탈로그, 관찰 시점의 quota를 확인했다. CLI 0.139.0에는 준비 설정 key가 부족했고 모델 카탈로그에도 목표 모델이 없었다. 업데이트 뒤 모델 항목은 PASS, 전체는 BLOCKED였다. 이는 계정별 실제 추론 성공이나 서버 전체 인수가 아니다. 개인 인증·절대 경로·사용량 원문은 공개 문서에 저장하지 않는다.

기본 설정은 아직 native 적용하지 않았다. 크레딧 차감 금지·실제 역할 설정·전체 동시 실행 상한·GitHub 필수 검사/독립 검토 강제는 별도 미완료다. 이 도구를 실행해도 전체 판정은 BLOCKED다.

## 적용 범위

모델/effort/login 방식과 `agents`의 비활성 상태·세션별 상한 2·기본 모델/effort만 적용한다. `features.multi_agent`와 `features.multi_agent_v2`도 false로 고정해 활성화 우회를 막는다. 역할 파일은 설치하지 않는다. sandbox는 기본적으로 기존 값을 보존하며 `--workspace-write`를 명시한 경우에만 같은 batch에서 승인된 template 값을 적용한다.

UID·HOME·passwd 등록 HOME·launcher·helper 해시를 먼저 대조한다. 사용자 config 또는 조상의 symlink·외부 소유권·group/other 쓰기 가능 경로·CODEX_HOME override·API/외부 token 환경은 차단한다. 다른 계정의 인증은 복사하지 않는다.

기존 [조회 도구](common/READINESS.md)의 bytes를 고정 해시로 확인하고 격리 module에서 bounded RPC만 재사용한다. 기존 파일과 읽기 전용 module의 허용 목록은 변경하지 않는다. 원래 조회 도구에는 설정 쓰기 기능이 없다.

`config/read`의 정확한 base user layer 하나와 nonempty version을 사용한다. 승인 key만 `config/batchWrite`의 CAS·upsert로 한 번 적용하고 `reloadUserConfig=false`를 사용한다. 다른 layer override, 예상 경로/version 불일치, legacy cap alias 충돌은 차단한다. 응답 유실은 새 연결에서 조회하고 재쓰기하지 않는다. user layer의 비대상 JSON 값과 유효 설정을 대조한다. 순서·서식이 아니라 값의 형식과 의미를 보존 기준으로 삼는다. 인증 파일은 내부 해시만 비교하며 keyring/cache 전체 무변경을 주장하지 않는다.

## 실행과 판정

기본 모드는 조회/변경 key 요약이다. 정확히 확인한 UID·HOME·CLI 경로를 명시하고, 적용에는 `--apply`를 추가한다.

```text
python3 -I -B src/ops/tools/codex_prepare_config.py --expected-uid VERIFIED_UID --expected-home VERIFIED_HOME --codex VERIFIED_LAUNCHER --apply
```

고정된 sibling `codex_readiness.py`도 함께 있어야 한다. 기준 SHA-256은 `5b84534a6e0b3bb19fdd6a2bd6e3c38f23ea89edc205b461751b5c0e857ee399`다. argv/전체 config/오류 응답 원문을 붙이지 않고 요약 JSON만 확인한다.

`configuration_prepared=true`는 이 비활성 기본값의 readback 성공이다. 무인 실행 승인이 아니며 역할·비용·합산 상한·병합 강제의 검증을 대체하지 않는다. `write_attempted=true`이지만 확인이 실패한 경우 실제 적용 여부는 미확인으로 남기고, 다음 실행은 먼저 다시 읽는다. 파일 직접 rewrite, CAS 생략, single-write 반복으로 우회하지 않는다. 합성 fixture는 upstream CAS/원자성의 native 인수가 아니다.

[사전 계획 공개판](config-preparation-test-plan.md)은 비공개 실행 UID/경로 한 문장만 일반화했다. 구현 전 원본 계획 v3 해시는 `735683d70ddcab7106458328271d66e72d55f8f73b75ff1701cca0cce3b7beba`이며 독립 reviewer가 사전 PASS를 부여했다. 기존 sandbox 보존, UID·HOME 고정 및 대상 소유권 확인, 계정/카탈로그 미확인 시 쓰기 0건을 사전 해석으로 확정했다. 초기 최종 검토·focused 검사 근거는 `docs/testing/codex-config-preparation-20261014.json`에서 보존한다. 응답 형식 보정 근거는 `docs/testing/codex-config-wire-20261014.json`에서 보존하고, 현재 출처 경로 보정의 근거는 `docs/testing/codex-config-origins-20261014.json`에서 확인한다.

공식 계약: [app-server](https://learn.chatgpt.com/docs/app-server), [설정 참조](https://learn.chatgpt.com/docs/config-file/config-reference), [사용량과 요금](https://learn.chatgpt.com/docs/pricing). 확인일 2026-10-14. 실제 서버에서 도구의 정상 적용과 CAS 충돌 인수는 별도 단계다.

## CLI 응답 형식 보정

CLI 0.162.1의 공식 Rust `ConfigLayer`는 `disabled_reason: Option<String>`에 `skip_serializing_if=Option::is_none`을 적용한다. 활성 user layer는 JSON의 `disabledReason`이 생략되는 것이 정상이다. 초기 구현은 생성된 TypeScript의 nullable/필수 선언만 기준으로 그 생략을 거부했고, 사용자 실행에서 `USER_LAYER_UNUSABLE`로 설정 쓰기 전에 중단됐다.

[추가 계획](config-layer-wire-addendum.md)에 따라 생략 또는 명시적 null을 같은 활성 상태로 처리한다. 문자열·false·0·빈 문자열·배열·객체 등 모든 non-null 비활성 사유와 잘못된 config 형식은 계속 차단한다. 계정·경로·필수 version/profile·origins·CAS·비대상 값 보존 기준은 그대로다. D06의 사소한 동등 표현 보정으로 독립 reviewer가 분류했고, 추가 계획 해시 `a7ed90be9dfc0e206a675f5879a7cb04e9c84a901d2b2a74e850ab7a18538748`를 D29에 따라 수정 전에 승인했다.

출처는 [고정 CLI 0.162.1의 Rust 응답 코드](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/config.rs)다. `ConfigWriteResponse.overridden_metadata`에는 해당 생략 속성이 없으므로 쓰기 receipt는 여전히 명시적 null을 요구한다. 다른 누락 필드를 임의로 정상화하지 않는다. 원래 조회 도구의 해시와 전체 BLOCKED 판정은 바꾸지 않는다. 이 보정 자체는 native 설정 적용이나 CAS/원자성 인수 완료가 아니다.

## 구조화된 feature의 출처 경로

2026-10-14 사용자 출력에서 승인 기본값 9개의 native batch 쓰기는 ACK됐고 별도 조회 도구의 configuration도 PASS였다. 준비 도구는 추가 출처 확인에서 `CONFIGURATION_ORIGINS_UNKNOWN`으로 중단됐다. 이는 적용 완료와 검증 완료를 구분해야 하는 부분 결과다.

고정 CLI 0.162.1의 [fingerprint.rs](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/config/src/fingerprint.rs)는 구조화된 boolean feature의 출처를 enabled leaf에 기록한다. [merge.rs](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/config/src/merge.rs)는 multi_agent_v2를 이 구조화된 feature로 분류한다. 따라서 `features.multi_agent_v2=false` 값의 출처는 `features.multi_agent_v2.enabled`에서 확인한다.

[추가 계획](structured-feature-origin-addendum.md)에 따라 이 한 key에만 canonical child를 필수로 적용한다. 원래 scalar key나 상위 parent에 유효한 metadata가 있어도 child가 없으면 차단한다. exact user file·profile null·현재 layer version 및 같은 값의 다른 layer 거부 기준은 그대로다. 다른 key의 prefix 규칙은 변경하지 않는다. 독립 reviewer는 D06의 동등 출처 표현 보정으로 분류했고 추가 계획 해시 `b5eac6cd2c1c893a750c882f09f022009d8c643232296a0e4aed42bfa1733d68`를 D29에 따라 구현 전에 승인했다.

이미 ACK된 설정에 대한 다음 native 확인은 `--apply` 없이 수행한다. 현재 기본값이 일치하면 쓰기 0건으로 출처를 확인하고, 변경이 관찰되면 적용 대기로 남긴다. 설정을 다시 쓰거나 출처 확인을 생략해 통과시키지 않는다. 수정판의 출처 조회·CAS 충돌/원자성 인수와 크레딧·역할·합산 상한·GitHub gate의 완료는 각각 실제 근거가 필요하다.
