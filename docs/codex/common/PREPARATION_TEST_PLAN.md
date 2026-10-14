# 다음 준비 단계의 구현 전 검사 계획

작성: test_agent / 2026-10-14 / 기준 버전: preparation-test-plan-2
상태: 독립 reviewer의 사전 검토 대기. 계획 작성만 완료했으며 구현·서버 조회·GitHub 관리 변경·모델 호출은 실행하지 않았다.

개정 이유: 독립 reviewer의 P2-1/P2-2 보완 요구에 따라 공식 app-server 계약, 명시적인 실행 상한, 프로토콜 실패 fixture, D38의 네 가지 의미 오류, 현재 구현과 후속 관리 자동화의 경계를 확정했다.

## 요구사항과 범위

기준은 Agent `AGENTS.md`, `docs/codex/common/{RULES,TESTING,DECISIONS}.md`의 1.1-docs 배포판과 VMS `AGENTS.md`, `docs/codex/project.md`다. D04~D09, D15~D20, D22~D24, D28~D30, D35, D38을 연결한다. 프로젝트의 기존 제품 인수 기준과 운영 보류는 유지한다.

이번 계획은 두 준비 산출물만 대상으로 한다.

1. simsys4090의 개발 환경에서 기존 Codex CLI·유효 설정·계정·모델 카탈로그·사용량·크레딧 미사용 전제를 읽는 Python 3.10 이상 표준 라이브러리 전용 readiness probe. 예정 소스는 Agent `src/ops/tools/codex_readiness.py`의 공용 단일 도구다. VMS에 구현을 복제하지 않고 공개된 고정 버전의 같은 도구를 사용한다. 설치·로그인·모델 호출·worker 활성화·인증 저장소 이동을 수행하지 않는다.
2. 두 GitHub 저장소의 필수 검사·독립 reviewer 증거·자동 병합을 연결하는 관리 설정 명세. 현재는 문서 설계와 읽기 조회 결과만 준비한다. full worker, status emitter, admin apply helper 구현 및 활성화는 후속 단계다.

문서 준비 단계에서 제품 전체 Go/DB/GPU/LLM/브라우저 검사는 적용 대상 없음으로 기록한다. 필요한 native 조회나 GitHub 실제 인수를 환경 부족 때문에 실행하지 못한 경우에는 적용 대상 없음이 아니라 `BLOCKED`다. 현재 문서 PR #1은 각각 draft이며 두 main의 보호·필수 검사·자동 병합이 구성되지 않은 상태를 준비 입력으로 삼는다. 실제 적용 직전에 다시 조회한다.

## 판정과 근거 형식

| 판정 | 관찰 조건 | 준비 완료에 미치는 영향 |
|---|---|---|
| PASS | 해당 요구사항을 지정 환경의 최신 근거로 확인 | 그 항목만 충족. 합성 검사는 native 인수로 승격하지 않음 |
| FAIL | 확인한 상태가 요구사항과 충돌하거나 probe가 금지 동작을 수행 | 준비/병합 차단 |
| BLOCKED | 필요한 권한·CLI·native 접근·공식 계약·계정/과금/정책 근거 없음 | 준비/병합 차단 |
| SKIP / 적용 대상 없음 | 이번 변경 영향이 없는 검사와 이유 | 요구사항 자체가 있는 항목의 미실행을 가리키는 데 사용 금지 |

프로브에는 전체 판정과 항목별 판정·이유 코드·확인 시각·CLI 버전·근거 출처·환경 범위를 기록한다. native 환경을 검증하지 못하면 `native_environment=unverified`, CLI 미존재는 `cli=missing_prerequisite`로 보고하며 전체 PASS로 처리하지 않는다. 이 값은 내부 보고서 설계이며 공식 Codex 응답 필드로 주장하지 않는다. 계정이나 API 인증 경로가 미확인인 경우에도 `BLOCKED`다.

근거는 값의 허용 목록으로 작성한다. 이메일·계정 식별자·token·API key·인증 파일 내용·전체 환경 변수/설정·운영 경로는 출력하지 않는다. 원문 응답을 파일이나 stderr에 보존한 뒤 사후 삭제하는 방식도 허용하지 않는다. JSONRPC에서 알 수 없는 필드는 버리고, 필요한 필드의 부재/형식 변경은 안전하게 차단한다.

## Native readiness probe 필수 검사

| ID / 요구사항 | 상황과 입력 | 관찰 가능한 통과 기준 | 환경·근거 |
|---|---|---|---|
| N01 / D22·원본 보존 | CLI 없는 PATH, 실행 권한 없음, CaaS에서 native 대상 접근 없음 | 설치나 우회 실행 없이 각각 missing prerequisite 또는 BLOCKED. 사용자 서버에도 CLI가 없다고 단정하지 않음 | 격리 fixture + native 별도 |
| N02 / D28·stdlib | 지원 Python 3.10, 비어 있는 외부 패키지 경로 | probe의 의존성은 stdlib만 사용. subprocess는 문서로 확인한 읽기 명령의 고정 인자 목록을 사용하며 shell을 호출하지 않음 | 소스/import 검토 + 최소 실행 |
| N03 / D22·범위 보존 | 정상 CLI, timeout, 비정상 종료, 손상 JSON, 종료하지 않는 가짜 프로세스 | 부록 A의 전체 60초·요청 5초·version 3초·종료 1+1초와 byte/notification/page 상한을 지킴. 중단 후 자식 종료/회수. 에러가 PASS로 바뀌지 않고 원문 비밀 없이 이유 출력 | 가짜 CLI/JSONRPC 프로세스 |
| N04 / D23·D35 | ChatGPT, API key, external token, 미인증, 계정 조회 실패, 알 수 없는 응답 | 공식 계약으로 확인한 ChatGPT 인증만 해당 항목 PASS. API 방식은 FAIL, 미확인은 BLOCKED. 로그인·refresh·token 교체·캐시 복사·키 출력 없음 | 계약 fixture + native 읽기 조회 |
| N05 / D15·D16 | 목표 모델/effort, 다른 모델, 목표 모델 미노출, effort 필드 없음, 전문 역할/합산 제어 unknown | 카탈로그에서 `gpt-6.1-sol`와 `xhigh`를 확인한 범위만 PASS. 모델 임의 대체 없음. 카탈로그만으로 실제 inference나 전문 역할 유효 설정을 증명하지 않음. 세션 상한 2와 전체 전문 역할 합산 상한 2를 구분하고 전문 역할·합산 제어 미확인은 BLOCKED | 공식 모델/설정 계약 + fixture + native |
| N06 / D15·설정 충돌 | layered 설정의 override 충돌, 읽을 수 없는 설정, 필요한 key 미제공 | `config/read`의 `result.config`로 확인한 유효 설정만 판단. 필요한 field가 없으면 BLOCKED. 템플릿 문자열이나 TOML 부분 추출을 유효 설정 증거로 쓰지 않음 | 설치 버전 계약 + fixture |
| N07 / D24 | 포함 한도 남음/소진, reset 시각 없음, 다중 bucket 중 하나 소진, 잘못된 percent/시각, endpoint 실패 | `rateLimitsByLimitId`가 있으면 유효 bucket 모두 확인하고 없으면 `rateLimits` 사용. 반환된 primary/secondary window와 소진 표식을 부록 A대로 판정. 소진은 대기 필요, 누락/불명확은 BLOCKED. 재개 시각 추정·worker/자동 재개 없음 | 공식 계약 fixture + native |
| N08 / D24·크레딧 | optional `credits`의 0/양수/unknown, `rateLimitResetCredits.availableCount`만 존재, 로컬 구매 설정만 존재 | workspace paid-credit 정보와 earned reset 수를 구분. 잔액 0·earned reset 존재·API key 부재·한도 조회 성공만으로 크레딧 차감 금지 PASS를 만들지 않음. 실제 계정/워크스페이스 강제 제어 미확인은 BLOCKED. 구매·소비/reset·설정 변경·모델 요청 없음 | 공식 정책 + 비밀 없는 실제 제어 근거 |
| N09 / D35·비밀 보호 | JSON/error/stderr/config에 canary key·token·email, 개행 포함 값, 예상치 않은 필드 | stdout·stderr·산출물에 canary 0건. 허용된 요약만 출력. 예외 traceback이나 raw 응답을 공개하지 않음 | canary fixture + 출력 전수 조회 |
| N10 / D22·D30 | simsys4090의 실제 CLI·개발 worktree/운영 원본 구분·현재 HEAD, 기존 로그인 | 읽기 조회 범위와 실행 계정/환경을 비공개 근거로 대조하고 native 항목만 기록. fixture 성공과 설치된 바이너리·인증·유효 설정의 실제 조회를 구분 | native 인수 필수, 현재 BLOCKED |
| N11 / 금지 호출·부수 효과 | 모든 정상/오류 경로, app-server 시작 전후의 인증 저장소 상태 | 부록 A의 allowlist 밖 호출 0건. 설치·복사·credential write 0건. 인증 파일 변경은 차단, 부수 효과 미확인도 BLOCKED. 부수 효과 근거는 native 실행 관찰만 인정 | 명령 spy + 격리 파일 감시 + native 필수 |
| N12 / JSONRPC 정확성 | id 불일치/중복, unknown server request, malformed JSON/UTF-8, byte 초과, notification 초과, cursor 누락/반복·page 초과 | 부록 A의 상한/순서/상관 규칙을 위반한 응답을 수용하지 않고 종료. 해당 fixture의 준비 판정이 PASS가 되지 않으며 에러 원문·cursor·비밀 출력 없음 | 프로토콜 fixture |

공식 [app-server 문서](https://learn.chatgpt.com/docs/app-server)에서 확인한 메서드·필드는 부록 A로 고정한다. [설정 참조](https://learn.chatgpt.com/docs/config-file/config-reference)의 `forced_login_method=chatgpt`, `model_reasoning_effort=xhigh`도 연결한다. 이 문서 계약은 native 설치 버전의 지원·실제 유효 설정·부수 효과 확인을 대신하지 않는다. 크레딧 차감 금지 강제 제어는 확인되지 않았다. 문서에 없는 필드를 만들어 넣거나 optional field 부재를 기본 안전값으로 바꾸지 않는다.

모델 목록 확인은 모델 실행 성공이나 응답 품질의 근거가 아니다. probe가 전체 PASS를 내더라도 기능 개발 시작 통보·작업별 D29 계획·최종 reviewer·필수 인수는 별도로 남는다.

## GitHub 관리 설정 명세의 요구사항 검사

현재는 아래 요구사항의 명세·권한·검증 설계를 검토한다. emitter/admin apply/worker에 대한 동작 fixture와 실제 인수는 후속 구현 단계의 필수 기준으로 유지하며 지금 실행 완료로 보고하지 않는다.

| ID / 요구사항 | 상황과 입력 | 관찰 가능한 통과 기준 | 환경·근거 |
|---|---|---|---|
| G01 / 권한 경계 | 콘텐츠/PR 읽기·쓰기만 가능한 connector, admin 권한 없음, 403/404 | API 미지원과 미설정 상태를 구분. 관리 setter가 없는 connector로 보호/자동 병합/필수 검사를 적용했다고 보고하지 않음. 읽기 결과와 검토 가능한 적용 명세만 생성 | 현재 connector + HTTP fixture |
| G02 / 단계별 적용 | 정상 저장소, 예상 repo/base 불일치, 조회 후 설정 변경 | 기본 모드는 읽기/dry-run. apply에는 정확한 repo/base·현재 상태 digest·명시 단계가 필요. 불일치면 쓰기 없이 중단. 보류 중인 draft PR을 ready/merge로 전환하지 않음 | fixture; 실제 apply는 후속 단계 |
| G03 / D20·필수 검사 | 검사/리뷰 없음, 필수 검사 실패·취소·SKIP·미보고·동명 다른 앱 | 모든 필수 검사의 성공과 허용 emitter identity가 있어야 통과. missing/unknown은 차단. 설정 명세에 검사 이름·App/source와 strict base 갱신 조건을 확인한 GitHub API 형식으로 기록 | 공식 GitHub 계약 + fixture |
| G04 / D07·독립 reviewer | 구현자의 PASS 파일/PR comment, 동일 GitHub 계정 APPROVED, 수정한 reviewer의 승인 | author가 쓴 PASS와 계정 자기 승인은 독립 검토 근거로 거부. 구현에 관여하지 않은 reviewer의 판정과 검토 대상·근거를 trusted emitter가 확인한 경우에만 reviewer check 성공 | 독립성 fixture + emitter 설계 검토 |
| G05 / D20·증거 결합 | head/base/rules/검사 근거 각각 변경, 오래된 승인 재전송 | reviewer status는 repo/PR, base SHA, head SHA, 규칙/계획 digest, 필수 검사 근거 digest에 결합. 하나라도 달라지면 기존 성공 무효. 생성 시각만으로 새 증거로 인정하지 않음 | canonical digest fixture + 실제 최신 SHA 조회 |
| G06 / D35·trusted context | PR이 workflow·helper·rules·review JSON을 변조, 악성 head 코드, fork PR | author가 수정한 실행 코드에 status/admin credential을 전달하지 않음. protected base 또는 별도 trusted runner/App의 evaluator가 고정 계약과 근거를 조회한 뒤 emit. `pull_request_target`에서 권한을 가진 채 head 코드를 실행하는 구성 거부 | 권한/워크플로 정적 검토 + 공격 fixture |
| G07 / D20·원자 병합 | 검사 후 head push, base 이동, merge 직전 조회 실패, 병합 가능 상태 unknown | 최신 base/head 및 유효한 결합 증거를 재확인. GitHub가 지원하는 expected-head 조건으로 다른 head 병합 거부. base 이동은 재검증까지 차단. 보호 규칙 우회·force push·direct main push 없이 실패 닫힘 | fixture + 격리 실제 PR 인수 |
| G08 / D32·중복/부분 실패 | 같은 apply 두 번, 일부 설정만 성공, 서버 반영 후 응답 유실, retry 중 권한 철회 | 재조회로 현재 상태를 확인하고 같은 설정을 중복 변경하지 않음. 부분 성공을 전체 PASS로 쓰지 않음. 보존된 전 상태·차이·후속 복구 항목을 기록하며 임의로 보호를 제거하지 않음 | 상태형 HTTP fixture + 후속 readback |
| G09 / D06·표준 변경 | 기준 완화 PR, 기준 변경 중요도 unknown, 오타만 수정한 PR | 중요한 변경/모호한 변경은 사용자 결정 증거 없으면 reviewer gate 차단. 동등한 사소 변경은 독립 reviewer 결정으로 진행. 모든 일반 PR에 사용자 수동 승인 요구를 추가하지 않음 | 요구사항별 판단 fixture |
| G10 / 실제 관리 인수 | 허용된 admin 경로, 두 저장소, 자동 병합+보호 readback | 공개 저장소에서 지원하는 실제 관리 계약·권한을 확인. 정확한 필수 검사·trusted reviewer check·우회 정책·자동 병합 허용을 읽기로 확인하고 실패/누락 검사 PR이 병합되지 않는 근거 필요. connector만으로 현재 인수 완료 불가 | 격리 저장소/PR 또는 사전 검토된 제한 대상, 현재 BLOCKED |

설정·status API payload와 endpoint는 최신 공식 GitHub 자료 및 실제 저장소 기능으로 확인한 필드만 쓴다. ruleset과 branch protection을 혼용해 동등하다고 가정하지 않는다. 독립 agent reviewer의 check와 GitHub의 사람 PR approval은 다른 계약이다. reviewer check를 믿을 수 있는 경로가 없는 상태에서 review count 0, 계정 자기 승인, author-controlled status로 대신하지 않는다. native helper의 GitHub 인증도 ChatGPT/Codex 인증과 분리하고 token 값을 출력하지 않는다.

## 문서·회귀·검증력 검사

필수 문서 검사는 변경 링크, 두 저장소 공통 배포판 본문 일치와 `release.json` SHA-256, project manifest, diff 범위·소스 불변, 미커밋 사용자 변경 보존이다. 공개 저장소에는 비밀·개인 절대 경로·native 원문을 넣지 않는다. 코드가 추가되는 후속 단계에서는 그 언어의 문법/import 검사와 위 변경 동작 fixture를 실행한다. 현재 계획 작성에 제품 전체 검사나 새 자동 백업은 필요하지 않으며 VMS의 백업 추가 개발 제외를 유지한다.

D38에서 reviewer가 선정한 의미 오류는 네 가지다. (1) unknown credit을 PASS로 바꾸면 N08이 검출, (2) base/head/rules/check-evidence 결합 검사를 제거하면 G05/G07이 stale 근거 수용을 검출, (3) author PASS를 수용하면 G04가 검출, (4) secret 원문을 출력하면 N09가 검출해야 한다. 현재 probe 구현 단계에서는 (1)/(4), 후속 emitter/admin helper 단계에서는 (2)/(3)의 격리 복사본을 검증한다. 정상 코드가 통과하고 오류 복사본이 목표한 의미 때문에 실패해야 한다. 문법 오류·환경 실패는 검증력 성공으로 세지 않는다.

## 부록 A: 공식 읽기 계약과 자원 상한

기준 출처: [Codex app-server](https://learn.chatgpt.com/docs/app-server), 확인일 2026-10-14. stdio 기본 전송은 JSONL이며 한 줄이 하나의 JSONRPC 메시지다. 요청 id는 각 요청에 유일하게 배정한다. 요청한 id와 정확히 일치하는 응답만 수용하며 중복 id 응답, 미발행 id 응답, unknown server request는 프로토콜 실패다. notification은 결과 응답으로 세지 않고 허용된 처리만 하며 최대 32개다.

| 순서 / 메서드 | 허용 params | 필요한 응답과 판정 |
|---|---|---|
| 1. `initialize` | `clientInfo: {name,title,version}` | 성공 응답을 확인하기 전 다른 조회 금지. 실패/timeout이면 종료 |
| 2. `initialized` notification | `{}` | initialize 성공 뒤 한 번만 발행 |
| 3. `config/read` | `{includeLayers:false}` | `result.config`의 유효 layered 설정에서 확인된 field만 사용. 필요한 field 부재는 BLOCKED |
| 4. `account/read` | `{refreshToken:false}` | `result.account.type='chatgpt'`, `result.requiresOpenaiAuth=true` 확인. 다른 인증 방식은 FAIL, null/필드 미확인은 BLOCKED. email/id는 즉시 버림 |
| 5. `model/list` | `{limit:20,includeHidden:false}`; 다음 요청에만 받은 `cursor` 추가 | `result.data[].model`/`id`, `supportedReasoningEfforts[].reasoningEffort`로 목표 모델/effort 노출 확인. `nextCursor`는 null이면 종료, string이면 opaque 값 그대로 다음 요청에만 전달. 누락/다른 타입/반복 cursor는 차단. 최대 5 page 안에 종료하지 못하면 BLOCKED |
| 6. `account/rateLimits/read` | `{}` | `rateLimitsByLimitId`가 있으면 유효 bucket을 모두 처리하고 없으면 `rateLimits` 처리. 관련 bucket의 primary/secondary window 중 반환된 항목을 검사. `usedPercent`는 유한한 수이며 0~100, `windowDurationMins`/`resetsAt`은 문서 타입을 지키고 reset은 Unix seconds로 처리. bool을 숫자로 허용하지 않음. 필요한 한도/시각 누락·null·잘못된 타입은 BLOCKED |

한도가 100%이거나 `rateLimitReachedType`이 제공되면 소진으로 판정한다. 이미 지난 reset 시각만으로 사용량이 초기화됐다고 가정하지 않는다. 유효 bucket 중 하나라도 소진/미확인이면 포함 한도 항목을 PASS로 만들지 않는다. `credits`는 optional workspace paid-credit 정보이며 `rateLimitResetCredits.availableCount`는 earned reset 수다. 둘을 유료 잔액으로 혼동하지 않으며 어느 값도 D24 강제 제어 증거로 인정하지 않는다.

전체 60초, 개별 JSONRPC 요청 5초, CLI version 조회 3초를 상한으로 고정한다. graceful 종료 1초 뒤 종료하지 않으면 kill하고 1초 안에 회수한다. 모든 경로에서 종료/회수 시간도 전체 상한에 포함한다. 읽은 stdout 전체는 512 KiB, 한 줄은 128 KiB, stderr는 64 KiB를 넘기지 않는다. stderr는 비밀을 포함할 수 있으므로 원문을 버리고 출력하지 않는다. stdout/stderr를 동시에 제한해 pipe deadlock을 막으며 초과하면 프로토콜 실패로 종료한다. notification 최대 32개, model page 최대 5개다. 상한 이후 재시도나 무한 pagination을 하지 않는다.

버전 조회와 이 표의 초기화/읽기 allowlist 외 실행은 금지한다. consume/reset/login/logout/config write/thread/turn/inference/구매·설치·Git/운영 변경 호출은 0건이어야 한다. raw 응답·stderr·opaque cursor·계정 email/id를 공개 로그/파일에 저장하지 않는다. 인증 파일 변경은 차단하며 의도된 credential write는 0건이다. app-server 시작/종료의 부수 효과는 native 관찰 전까지 unknown/BLOCKED로 남긴다. observer가 인증 저장소를 복사해 기준을 만들지 않는다.

## 부록 B: 단계 분할과 전달 경계

| 단계 | 이번 구현 가능 범위 | 완료 근거와 남는 차단 |
|---|---|---|
| 현재 D29 계획 | 이 v2 문서와 독립 사전 검토 | 계획 digest에 reviewer 확인을 결합. 구현 미시작 |
| 다음 준비 구현 | Agent의 stdlib 공용 read-only probe, 관련 fixture, 공개 고정 버전 전달 방법, GitHub 설정 명세 | N01~N12·문서 검사 및 N08/N09 의미 오류 검사. VMS는 같은 고정 버전 사용. native/credit/emitter/admin 인수는 별도 BLOCKED |
| Native 읽기 인수 | 기존 CLI/계정/환경에서 허용 조회와 부수 효과 관찰 | native 관찰 근거만 인정. D24 강제 제어 미확인은 유지 |
| 후속 관리 자동화 | trusted status emitter, admin apply helper, worker/재개 제어 | 별도 D29 계획·구현·G01~G10 fixture/실제 인수, G04/G05/G07 의미 오류 검사. 현재 구현하거나 활성화하지 않음 |

같은 도구를 고정 버전으로 사용하는 것은 native 서버에 지금 설치하거나 인증 캐시를 옮기는 지시가 아니다. 공개 버전과 hash 확인 방법은 비밀 없이 명세에 기록한다. helper 구현 경로·trusted emitter의 identity·관리 credential 전달은 후속 계획에서 확정하며 문서만으로 활성화된 것으로 보고하지 않는다.

## 종료 조건과 현재 남은 단계

1. reviewer가 이 v2 계획의 정확한 digest와 핵심 검증력 대상을 사전 확인한 뒤 현재 허용된 probe 구현·설정 명세 준비를 시작한다. 이 확인은 최종 구현 검토와 다르다.
2. 구현 후 관련 stdlib/fixture/문서 검사를 모두 통과하고 동일 소스·설정·근거 digest에 최종 독립 검토를 붙인다.
3. fixture 통과는 산출물 준비 근거다. native readiness, 크레딧 차감 금지 제어, trusted reviewer emitter, GitHub 실제 관리 readback/거부 인수가 없으면 실행·자동 병합 준비 완료로 선언하지 않는다. 후속 worker/emitter/admin helper 미구현은 현재 probe 코드 검사 실패로 혼동하지 않으며 전체 실행 준비에는 BLOCKED로 기록한다.
4. 현재 native 서버 접근·로컬 codex/gh·GitHub admin setter가 없으므로 N10/G10 및 실제 계정·과금 제어는 BLOCKED다. connector의 두 저장소 branch-protection 조회는 403이며 상세 보호 구성을 읽었다고 표시하지 않는다. 이 환경 부족은 기준 완화나 API/credits fallback의 근거가 아니다.
5. 모델/인증/동시성 정책을 바꾸거나 필수 검사를 SKIP 허용으로 바꾸는 선택은 D06의 사용자 결정 대상이다. 기존 요구사항을 그대로 구현하는 일반 준비 단계마다 사용자 승인을 요구하지 않는다.
