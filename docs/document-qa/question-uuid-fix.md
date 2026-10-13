# head 활성화 뒤 질문 화면의 UUID 호환 수정

2026-10-12. 서버는 첫 질문 head를 정상 활성화했고 인증된 정책도 질문 가능으로 보고했다. PC 화면은 `질문 연결을 확인할 수 없습니다`·`확인 필요`를 표시하며 질문하기를 비활성으로 유지했다. 사용자 화면 관찰은 11:39:31 KST 스크린샷이고 접속 키나 새 질문 실행 실패의 근거는 아니다. [관찰·검사 근거](../testing/document-qa-question-uuid-fix-20261012.json), [질문 화면 계약](question-screen.md)을 따른다.

## 원인과 변경 범위

Q06b의 target_id와 question_id는 같은 참조/요청을 안정적으로 유지하는 UUID v5다. 화면은 업로드용 UUID v4 검사기를 모든 질문 참조에 재사용했다. 실제 target `01851866-e7bd-5c5f-a405-2f949c22c60d`와 정상 활성 정책을 넣으면 기존 화면에서 QUESTION_POLICY_UNAVAILABLE가 재현된다. 기존 DOM 대역의 target/question을 모두 v4로 작성해 개발 검사에서 이 차이를 놓쳤다.

`web/questions.js`에 서버의 `contracts.uid`와 같은 소문자 표준 UUID 형식·nonzero 검사를 적용했다. 정책의 head target, 질문/대상/Agent/Run, 검증된 출처, 탭 복구의 서버 참조가 UUID v5를 읽는다. 브라우저가 새 접수에 만드는 client key는 기존 UUID v4 생성/검사를 유지한다. 정책의 준비 상태·추론 옵션·revision 관계, 출처/인용·상태·한도 검증도 유지한다.

런타임 변경은 질문 화면 한 파일이다. API/Model/Knowledge/Agent 구현·DB/schema·head·키·기존 job/Flow/Run/intent/receipt는 이 패치의 쓰기 대상이 아니다. 새 모델/색인/Tool/Agent 생성이나 head 재활성화 없이 기존 상태를 읽는다. 정적 파일은 요청 때 읽으므로 이 패치를 적용한 뒤 브라우저를 새로고침하면 새 JS를 받는다. 서버의 질문 추론 옵션이 이미 켜져 있다면 QA API 재실행이 필요 없다. 화면 버전 표시는 q08.1을 유지한다.

## 확인된 현재 운영 결과

| 단계 | 실제 사용자 출력으로 확인한 결과 |
| --- | --- |
| GPU 색인 | 기존 TXT 1개, Flow generation=3/execution=10, indexing_activated, local indexing_generation=1, indexed/chunk_count=1 |
| 검색 | 실제 ToolCall 1회, 해당 문서 조각 1개 반환·검증, ToolVersion 발행·readback 확인 |
| 고정 QA Agent | 실제 AgentRun 시험 1회, `문서 QA CPU 연결 확인입니다. [S1]`, 원문/출처 검증·AgentVersion 발행 확인 |
| 첫 head | expected_revision=0 → head_revision=1, activation receipt applied, 인증된 질문 정책 question_ready=true |
| PC 질문 화면 | LAN 연결·기존 문서 검색 준비 완료 표시, UUID 차이로 질문 가능 표시가 차단됨; 화면 질문 접수/답변/원문 인수는 미완료 |

고정 참조:

- job: `430b0e7b-7b4c-4ae7-9c57-6c979ca94343`
- index/index_version: `a09f8482-6e10-44dc-aefe-ce0ad554841d` / `fab90f1d-9d92-4923-8550-8c5cbcbe620f`
- ToolVersion: `ecc5cc0b-fb25-4578-9521-3760dfd9747f`
- QA plan/AgentVersion: `b743f343-8b82-5a5e-a027-57003e9e2184` / `41330cd4-618f-429e-949e-a8ef3b1337ab`
- 현재 target: `01851866-e7bd-5c5f-a405-2f949c22c60d`, head_revision=1
- head 실제 활성 시각: `2026-10-12T02:33:49Z` (11:33:49 KST)

static production_verified/next_step 필드는 이번 외부 운영 관찰로 덮어쓰지 않는다. 과거 GPU 보류 generation=1, INDEXING_PROFILE_INVALID generation=2, 10월 8일 빈 head/오프라인 검사는 역사적 근거로 유지한다. 첫 문서의 연결 시험·인용 검증은 넓은 의미 품질 인수나 새 문서 누적 인수를 뜻하지 않는다.

## 적용 후 한 단계 확인

1. 배포 패키지의 SHA-256을 대조하고 `apply_patch.py --apply`를 실행한다. 기존 파일/고정 소스 충돌은 쓰기 전에 차단한다.
2. 기존 QA 서버를 실행한 채 PC의 `http://192.168.0.119:17110/console`에서 Ctrl+F5를 누른다. 기존 접속 키를 다시 입력한다. head 활성화·Flow 재개·init/migrate/configure는 반복하지 않는다.
3. 질문 가능을 확인하고 `연결 검증 코드는 무엇인가요?`와 `점검 담당 부서는 어디인가요?`를 각각 질문한다. 기대 값은 `QA-CPU-20261007`, `플랫폼 개발팀`이며 `[S1]`의 실제 원문도 확인한다.
4. 답변 준비 중이면 답변 진행 확인을 선택해 기존 Run 결과만 조회한다. 자동 5초 갱신은 로컬 GET만 수행한다. 응답 유실/새로고침에서도 같은 질문과 키/참조를 유지한다.

명령 검사는 `python3 -B -m unittest discover -s src/apps/document_qa/tests -p 'test_screen*.py' -q`다. 새 HTTP→실제 JS 검사 5개는 Node 18 이상과 fetch·structuredClone·Web Crypto 등 필수 기능을 확인한 뒤 실행한다. Node가 없거나 호환되지 않으면 JS 파일을 읽기 전에 해당 검사 클래스만 이유를 표시하고 skip한다. 개발 환경의 Node 24.19.0에서는 5개를 모두 실행했다. 구형 Node 경로의 skip을 JS 검사의 통과로 기록하지 않는다. QA 서버·브라우저 화면은 서버 Node를 사용하지 않는다. 브라우저 렌더나 실제 GPU 호출을 이 대역 검사로 대체하지 않는다.

사용자 서버의 UUID 패치는 checksum OK·applied/written=8로 적용됐다. 화면 관련 56개 검사는 신규 JS 5개가 optional chaining 구문에서 SyntaxError로 실패했고, 나머지 51개는 실패가 없었다. 기존 게이트가 Node 실행 파일의 존재만 확인한 것이 원인이다. 실제 Node 버전은 사용자 출력에 없으므로 추정해 기록하지 않는다. [검사 실행 환경 보완 근거](../testing/document-qa-question-node-gate-20261012.json)를 따른다. 화면 코드·기존 head는 이 보완 패치에서 다시 변경하지 않으며 변경 후 PC 질문 결과는 아직 미확인이다.

패키지는 `.patch-backups`를 만들지 않는다. `--apply` 생략은 검토, `--rollback`은 이번 파일만 원복한다. 원복 후 화면 새로고침이 필요하며 DB/head/질문 이력/키는 유지한다. PC 질문/원문 인수 다음에는 새 두 번째 문서의 누적 색인과 Tool/Agent/head 버전 전환을 확인한다. 전체 업로드만으로 자동 질의 준비가 끝났다고 보고하지 않는다.
