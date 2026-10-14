# Native 준비 설정 적용의 구현 전 검사 계획

작성: test_agent / 2026-10-14 / 버전: config-apply-plan-3
상태: 독립 reviewer 사전 검토 대기. 구현·저장소 수정·native 호출·모델 실행은 하지 않았다.

## 범위와 고정 입력

D23·D24·D29와 현재 준비 단계에 연결한다. Native CLI 0.162.1에서 계정·목표 모델 카탈로그·quota의 조회가 PASS지만 전역 설정은 `CONFIG_FIELDS_UNKNOWN`인 상태를 입력으로 삼는다. 이번 결과는 비활성 준비 기본값의 설정 적용과 확인이며 기능 개발·무인 실행·크레딧 제어 인수가 아니다.

읽기 전용 `codex_readiness.py`는 SHA-256 `5b84534a6e0b3bb19fdd6a2bd6e3c38f23ea89edc205b461751b5c0e857ee399`로 고정한다. 그 파일과 원래 module의 allowlist는 변경하지 않는다. 새 apply 도구는 확인한 같은 bytes를 private module instance로 로드하고 그 instance에서만 `config/batchWrite` 한 메서드를 허용해 bounded `RpcClient`의 기존 전송·응답 파서·종료를 재사용할 수 있다. 공유 module monkeypatch와 저수준 파서 복제는 하지 않는다. read-only probe 자체는 계속 batchWrite를 거부해야 한다.

구현 범위는 Agent `src/ops/tools/codex_prepare_config.py`, 인접 `tests/test_codex_prepare_config.py`, `docs/codex/config-preparation*.md`다. 공통 v1.2 배포판·VMS·기존 metadata probe는 수정하지 않는다.

비공개 실행 전제는 사전에 확인한 실행 UID·HOME, 그 HOME의 사용자 설정 `~/.codex/config.toml`, 확인된 `~/.local/bin/codex` launcher다. 대상은 `Path.home()/'.codex/config.toml'`에서 유도하고 예상 경로·계정과 정확히 대조한다. `CODEX_HOME` override, symlink 대상/조상, 다른 UID/HOME, 예상 밖 target/launcher는 쓰기 전에 차단한다. 공개 결과에는 계정 식별자·절대 경로·원문을 넣지 않는다.

## 설정 계약과 적용 순서

공식 [app-server 문서](https://learn.chatgpt.com/docs/app-server)는 사용자 config의 원자적 `config/batchWrite`를 설명한다. Root가 확보한 0.162.1 생성 protocol의 계약: params는 `edits:[{keyPath,value,mergeStrategy:'upsert'}]`, `filePath`, `expectedVersion`, `reloadUserConfig`; 읽기 결과는 `config`, `origins`, `layers`; user layer는 `name.type='user'`, `name.file`, `name.profile`, `version`, `config`, `disabledReason`; 쓰기 결과는 `status:'ok'|'okOverridden'`, `version`, `filePath`, `overriddenMetadata`다. CAS 오류 코드는 추정하지 않는다. 문서 계약은 native 인수를 대신하지 않는다.

1. 계정/경로/고정 helper bytes/CLI 0.162.1을 검증한다. 기존 API 인증 환경이 있으면 차단한다. initialize→initialized 후 `account/read {refreshToken:false}`로 ChatGPT 인증을 다시 확인한다. login/refresh·인증 복사 없음.
2. `config/read {includeLayers:true}`에서 정확한 대상 file와 `profile:null`의 user layer 하나만 선택한다. 누락·중복·disabled·비어 있는 version은 BLOCKED이며 write는 0건이다. 해당 버전에서 승인된 key와 merge 전략의 지원을 확인하지 못하면 적용하지 않는다.
3. layer의 현재 값과 아래 scalar key만 비교한다. 전체 `agents` 테이블·문서 원문을 대체하지 않는다. 기본은 계획/차이 요약이며 apply 모드는 같은 실행에서 다시 읽은 version으로 단 한 번의 batch를 제출한다. 이미 모두 일치하면 write 0건으로 종료한다.
4. `filePath`는 검증한 유일 target, `expectedVersion`은 읽은 nonempty version, `reloadUserConfig:false`로 고정한다. CAS conflict·API 미지원·원자성 미확인은 BLOCKED/FAIL이며 직접 TOML 쓰기·버전 생략·분할 single write로 우회하지 않는다.
5. 응답 path/version/status와 새 `config/read`의 같은 user layer 및 유효 설정을 검증한다. `status='ok'`와 `overriddenMetadata=null`, 승인 값의 typed 일치와 unrelated JSON의 의미 불변만 설정 항목 PASS로 인정한다. `okOverridden`이나 다른 layer override는 BLOCKED다. 응답 유실/timeout은 재쓰기하지 않고 읽기로 적용 여부를 확인하며 unknown은 BLOCKED다.

| keyPath | 값 |
|---|---|
| `model` | `gpt-6.1-sol` |
| `model_reasoning_effort` | `xhigh` |
| `forced_login_method` | `chatgpt` |
| `agents.enabled` | `false` |
| `agents.max_concurrent_threads_per_session` | `2` |
| `agents.default_subagent_model` | `gpt-6.1-sol` |
| `agents.default_subagent_reasoning_effort` | `xhigh` |
| `features.multi_agent` | `false` |
| `features.multi_agent_v2` | `false` |

0.162.1에서 `features.multi_agent_v2`가 `agents.enabled`보다 우선하므로 두 feature도 batch에서 끄고 readback에서 bool false를 확인한다. `0`·문자열 `'false'`를 동등한 값으로 인정하지 않는다.

`sandbox_mode='workspace-write'`는 승인된 비활성 template에 있지만 최소 범위의 기본 적용에서는 기존 값을 보존한다. 이번 단계에서 명시적으로 포함한 경우에만 동일 batch에 그 scalar edit 하나를 추가한다. 역할별 유효 설정·전역 합산 cap·크레딧 차감 금지·GitHub gate는 계속 별도 BLOCKED다.

## 필수 fixture 검사

Python 3.10 stdlib unittest의 메모리 fake RPC와 임시 config 경로로 아래 변경 위험만 검사한다. 기존 RpcClient의 bounded 전송·프로세스 회수 검사를 재사용하며 helper bytes가 같으므로 기존 39개는 반복하지 않는다.

| ID | 상황 | 관찰 가능한 통과 기준 |
|---|---|---|
| C01 / 정상·보존 | unrelated key/profiles/agents 필드가 있는 TOML, multi_agent_v2=true | 한 번의 CAS upsert batch가 승인 scalar만 변경. unrelated JSON 의미 불변, TOML 전체 rewrite 없음. 세 활성화 flag를 typed false로 readback. auth·프로젝트 config·다른 파일 변경 0건. target/version/reload=false 고정 |
| C02 / 적용 전 차단 | UID/HOME/launcher/hash/CODEX_HOME/조상 symlink 불일치, API 인증, layer 누락·중복·disabled·profile/version 오류 | `config/batchWrite` 호출 0건. 인증/key/경로 원문 출력 0건. 계정이 다른 경우 login이나 fallback 없음 |
| C03 / CAS·원자성 | 읽은 뒤 concurrent writer, 여러 edit 중 하나 거부, 지원되지 않는 key/endpoint | stale version으로 unrelated 변경을 덮지 않음. 거부된 batch는 부분 반영 0건. version 생략·개별 write·수동 파일 rewrite·자동 재시도 없음 |
| C04 / 재확인·반복 | 이미 일치, 다른 path, okOverridden/metadata nonnull, higher layer override, 유실 후 반영/미반영/unknown | 이미 일치이면 write 0건. ok/no override와 정확한 readback만 설정 항목 PASS. path/override 불일치는 차단. 유실 후 조회하며 같은 batch 재쓰기 0건 |
| C05 / 고정 transport | helper hash 불일치, private instance의 allowlist 확장, 정상 batch | hash 불일치면 import/write 0건. 검토된 RpcClient를 그대로 사용하며 private instance만 batch 허용. 원본 module/file은 불변. 응답 파서/전송/종료를 새로 복제하지 않음 |
| C06 / 비밀·범위 | error/config/account/version suffix의 canary, 예상 밖 write key/method, workspace 선택 없음/있음 | stdout·stderr·결과에 canary/계정 id/절대 경로 0건. initialize/read/한 번의 batch 외 mutating RPC 0건, thread/turn/inference·login·worker·GitHub 호출 0건. base helper는 계속 batchWrite 금지 |

C01/C03의 fake 서비스는 upstream 원자성의 실제 증거가 아니다. native 적용의 실제 인수는 별도 제한 단계에서 expected-version conflict와 정상 readback, 비대상 설정 보존을 확인해야 한다. 그 전에는 도구/fixture 준비와 native 적용 완료를 구분한다. 새 전체 백업은 만들지 않고 기존 설정을 출력하거나 공개 저장소에 복사하지 않는다.

## 완료선

계획 버전·digest의 독립 사전 검토 후 새 apply 도구만 구현한다. C01~C06의 관련 검사를 통과하고 최종 reviewer를 거친 뒤 native 명령을 전달한다. 결과는 변경 key 이름·판정 이유·설정 readback 일치 여부·비활성 상태만 보고한다. 설정 항목 PASS여도 전체 상태는 항상 BLOCKED로 둔다. D24 크레딧 강제나 무인 worker 활성화를 선언하지 않으며 중요한 기준 변경은 D06을 유지한다.
