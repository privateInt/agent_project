# ConfigLayer wire 형식 보정 — D29 추가 계획

작성: test_agent / 2026-10-14 / 버전: config-layer-wire-addendum-1 / 사전 검토 대기

Root가 확인한 공식 Rust 0.162.1 `ConfigLayer.disabled_reason: Option<String>`에는 `#[serde(skip_serializing_if="Option::is_none")]`이 적용돼 활성 layer의 None이 wire에서 생략된다. 기존 검사는 TypeScript의 nullable/mandatory 선언만 기준으로 필드 부재를 거부했으므로 정상 native 응답에서 `USER_LAYER_UNUSABLE`, write 0건이 발생했다. 원래 요구사항인 활성 layer만 허용하는 정책을 유지하며 `disabledReason`의 부재 또는 명시적 null만 같은 활성 상태로 인정한다. 실제 reason 문자열과 모든 다른 non-null 값은 계속 차단한다. 이번 보정은 wire 표현의 동등성 근거를 적용하는 것이며 disabled layer 허용·계정/경로/CAS/version/origin/비밀 기준 변경이 아니다. reviewer 사전 승인 전 소스와 테스트는 수정하지 않으며 고정 read-only helper 및 원본 39개 검사도 그대로 둔다.

| 회귀 사례 | 입력과 관찰 가능한 통과 기준 |
|---|---|
| W01 정상 native wire | 기본 fake layer에서 `disabledReason`을 생략. 기존 정상 적용·no-op 검사가 설정 항목 PASS, 전체 BLOCKED를 유지하며 허용된 쓰기 횟수와 CAS/readback/origin 기준은 동일 |
| W02 null 호환 | 동일 layer에 `disabledReason:null` 명시. 생략형과 같은 활성 판정·설정 결과를 얻음 |
| W03 실제 비활성/잘못된 값 | `"managed"`, `false`, `0`, `""`, `[]`, `{}`를 각각 제공. 모두 `USER_LAYER_UNUSABLE`, batch write 0건. truthiness로 비활성을 통과시키지 않음 |
| W04 config 형식 오류 | `config`가 누락/null/배열/문자열. 생략된 disabledReason 여부와 무관하게 계속 차단, batch write 0건. 기존 layer 유일성·version·target/profile 오류 기대값 유지 |

승인 후 fake의 기본 wire를 W01로 맞추고, 기존 음성 사례 중 disabledReason 생략만 정상 사례로 교정한다. W02~W04의 focused 회귀를 추가한 뒤 변경된 helper의 focused suite만 실행한다. native 적용 완료·atomicity/CAS 실제 인수나 worker 준비 완료로 확대하지 않는다.
