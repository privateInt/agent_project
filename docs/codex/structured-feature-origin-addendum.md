# Structured feature origin wire 보정 — D29 추가 계획
작성: test_agent / 2026-10-14 / 버전: structured-feature-origin-addendum-1 / 사전 검토 대기

Root가 확인한 공식 Rust 0.162.1 `fingerprint.rs`는 structured feature의 boolean origin을 `path+'.enabled'`로 기록하며 `merge.rs`는 `multi_agent_v2`를 structured key로 분류한다. 따라서 승인된 scalar `features.multi_agent_v2=false`의 origin은 정확히 `features.multi_agent_v2.enabled`로 검증한다. 이 한 key의 동등 wire 경로만 보정하며 다른 key의 기존 prefix 규칙은 유지한다. canonical child metadata는 exact user target·profile null·현재 layer version을 충족해야 하고 누락 시 ordinary key/parent metadata로 대체하지 않는다. D06의 wire 동등성 보정으로 reviewer 사전 승인을 요청하며 승인 전 소스·테스트는 수정하지 않는다. native 후속 확인은 읽기 전용으로 `--apply` 없이 수행하며 역사 계획/receipt·readiness·common·VMS·기존 39개는 유지한다.

| 회귀 사례 | 통과 기준 |
|---|---|
| S01 실제 wire/no-op | 기본 fake의 v2 bool origin을 canonical `.enabled`로 제공. 이미 적용된 9 defaults의 읽기 확인은 설정 PASS·전체 BLOCKED, write 0건 |
| S02 같은 false/상위 layer | canonical child가 다른 layer metadata이면 값이 false여도 `EFFECTIVE_CONFIGURATION_OVERRIDE`, write 0건 |
| S03 child 누락 | canonical child가 없고 ordinary key/parent가 유효해도 `CONFIGURATION_ORIGINS_UNKNOWN`, write 0건 |
| S04 child 우선 | 유효 child+다른 parent는 child로 판정, 다른 child+유효 parent는 거부. v2 외 기존 prefix 기대값은 유지 |
| S05 metadata 오류 | canonical child의 version/path/type이 잘못되면 계속 거부, write 0건. config·계정·CAS·비밀·활성화 기준 불변 |

승인 후 위 focused 회귀와 변경된 helper suite만 검증한다. scalar false 설정값이나 병합/원자 적용 기능을 바꾸지 않으며 기존 39개 재실행·native 쓰기·모델 호출은 하지 않는다.
