# U01b2 소스 독립 정적 검토

판정: **정적 검토 PASS — 수정이 필요한 제품 결함이나 범위 밖 접근 경로를 발견하지 않았다.** 실제 검사·변이·DB 보존 검증이나 최종 독립 검토 PASS는 아니다.

- 대상: `var/unattended-20261015/correlation-development/src/ops/tools/codex_unattended_queue_observer.py` 전체 235줄.
- 소스 SHA-256: `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a`
- `transition-evidence/correlation-source-ready.json`의 frozen source pin과 실제 파일 해시가 일치한다.
- 기준 v2 계획 SHA-256: `25c798b43efd07b01c8c9030c0de7ff41a04b94cab8dd62dc4462ed13025403f`
- 기존 독립 사전 검토 SHA-256: `29709c6c34feaf5fa673f325e43855b9dc44ab761beb1d815e9ef01997fdb5ac`
- 재사용 저널 SHA-256: `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8` — 고정 PR18 `780d0755378b9f95f0282ec766ca99300b2b0b53`의 기존 계약과 같다. collector/ledger 해시도 사전 검토 기준과 일치한다.

확인한 정적 경로:

1. **입력·hash·JSON 경계:** 36–124, 196–203행. builtin tuple/bytes/str와 key를 먼저 검사하고 page 수·개별/합계 bytes 상한을 확인한 뒤 모든 원문 pin을 검증한다. 그 후에만 JSON을 decode한다. 중복 key/BOM/float·비유한 수/깊이 초과를 거부하며 single-text·빈 text_elements·UTF-8 text 길이를 제한한다. pin 실패 전에 binding을 반환하거나 canonicalization으로 원문 hash 불일치를 숨기는 경로가 없다. manifest에는 검증된 hash와 마지막 응답 유실의 null만 들어간다. canonical helper에는 float를 전달하지 않으므로 기본 allow_nan 값이 실제 manifest 의미를 바꾸지 않는다.
2. **불완전·유실·순환 pagination:** 127–168행. 첫 cursor=null, 동일 thread, strict int limit=20과 이전 nextCursor를 검사한다. 요청에 사용한 cursor를 기록하고 순환·재사용을 거부한다. 빈 pages/마지막 응답 유실/생략된 nextCursor/자료 끝의 non-null cursor는 EVIDENCE_INCOMPLETE다. EOF 뒤 추가 page와 chain 오류는 OBSERVATION_INVALID다. 네 page·각 20 items로 총 80 상한이 도출된다. 후보를 모아도 모든 page와 마지막 명시적 null이 검증되기 전에는 반환하지 않는다.
3. **client·prompt·모호성:** 149–176, 223–227행. 제출 client는 원래 저널 client와, 제출 text UTF-8 hash는 B.prompt_sha256과 묶인다. 다른 client의 같은 prompt는 후보가 아니다. 모든 item의 지원 형태를 검증하고 중복 queue ID는 동일 내용이어도 거부한다. 원래 client 후보가 정확히 하나일 때만 그 text bytes를 제출 text bytes와 비교한다. 다른 내용은 CONTENT_MISMATCH, 서로 다른 ID의 다중 후보는 CORRELATION_AMBIGUOUS다. 계획된 M1/M2가 겨냥하는 비교와 유일성 분기가 분리돼 있다.
4. **공개 read 한 번·UNKNOWN 보존:** 204–226행. `read_intent` 호출 지점은 205행 하나이며 정상 외곽 입력만 도달한다. 기존 확정 INTENT_OBSERVED만 소비하고 BLOCKED/불명/호출 예외를 구분한다. 사전 key 검증은 기존 순수 helper를 사용한다. 기존 저널이 검증한 canonical B를 다시 decode하여 prompt digest를 읽으며, 새 SQL·연결 생성·initialize/reserve/claim·재조회·재연결·상태 변경은 없다. PREPARED는 차단하고 어떤 분기도 UNKNOWN을 해제하거나 revision/raw/hash를 쓰지 않는다. 실제 SQLite 읽기/commit/오류 cleanup은 기존 API에 맡긴다.
5. **sanitized 결과·비권위 범위:** 187–235행. 반환은 report/binding/candidate뿐이다. journal_reason은 기존 고정 reason만 허용하고 state/revision은 확정 read 후에만 채운다. 모든 실패는 candidate=None으로 끝나며 원문 prompt/pages/cursor/다른 item ID/DB 경로/exception을 반환·로그·파일·전역 캐시에 보관하는 코드가 없다. 허용 ID 세 개는 관측 후보일 때만 반환한다. origin/acceptance/remote IO/원격 변경/replay/native-ready flag는 전 분기에서 false다. socket/process/clock/settings/worker/scheduler/native 호출도 없다.

미검증 경계: 새 네 검사와 두 의미 변이, 실제 stdlib SQLite의 trace·파일 bytes·close/reopen 보존·IO 차단·출력 없음은 실행 근거가 아직 필요하다. 이 검토에서는 검사 파일이나 다른 agent를 조회하지 않았다. caller가 제공한 thread/params와 list 응답의 출처, RPC ID 대응, native 접수·현재 상태·원자적 snapshot은 이 부품이 인증하지 않는다. 후속 live 연결과 owner/provenance 인수는 별도다.

runtime D18 HELD와 scheduler/root/user PAUSE, vms 제외·기존 조사 상한을 유지했다. 테스트/import/DB/native/runtime 실행·권한 probe·위임·통지는 하지 않았다. 이 보고서 외의 제품·검사·계획·기존 결과·최초 오류·원본11에는 쓰지 않았다. 공유 보수적 마감 2026-10-16 15:45:08Z를 유지하며 예산을 재설정하지 않는다.
