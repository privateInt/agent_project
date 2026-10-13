# 문서 QA 연결 계층

업로드·자기 작업 저장·진입/CPU/색인 workflow와 Q06a 검색 Tool adapter, Q06b-1 질문 대상 저장/migration·Q06b-2 고정 QA Agent/질문 adapter·Q06b-3 검증된 질문 head CAS를 구현했다. Q07a/Q07b 서버 로컬 인수와 결과 문서 적용에 이어 Q08 질문·답변·출처 화면을 구현·대역 검사했다. 10월 12일 첫 TXT의 GPU 색인·검색 Tool·QA Agent smoke/발행·head1과 PC 질문 접수를 확인했다. PC 코드 질문과 prompt-only 초안은 인용 누락으로 실패했으며 답변/출처·품질 인수는 남았다. 최신 question_ready는 실제 HTTP 정책으로 확인한다. 구현/검증 수치는 [상태표](../../../docs/status.md), 후속 단계는 [계획서](../../../docs/plans/document-qa-work-plan-20261002.md)에 한 번만 기록한다.

| 파일 | 역할 |
|---|---|
| [contracts.py](contracts.py), [API 계약](api-contract.json) | Q01 receipt/intent/단계·질문 대상 gate |
| [readiness.py](readiness.py), [조회 계약](readiness-contract.json) | Q02 GET 준비 조회·운영 기본값 |
| [intake.py](intake.py), [uploads.py](uploads.py), [api.py](api.py) | Q03a 파일 접수·job·Data 업로드·인증 HTTP API |
| [workflows.py](workflows.py), [고정 Flow](workflow_template.py) | Q03b durable handoff·scoped callback·명시 등록 |
| [refinement.py](refinement.py), [결과 검증](canonical_results.py) | Q04a CPU 정제 intent·receipt·canonical 검증 |
| [refinement_flow.py](refinement_flow.py), [고정 Flow](refinement_flow_template.py) | Q04b 별도 실행권·40 step/600초·명시 resume |
| [indexing.py](indexing.py) | Q05a additive 저장·전체 문서 snapshot·직렬화·intent/receipt·CAS 활성화 |
| [indexing_results.py](indexing_results.py) | canonical/hash·Model profile·청커·입력/참조/개수 검증 |
| [indexing_transport.py](indexing_transport.py) | Knowledge 전용 제한 HTTP·쓰기/추론의 별도 opt-in |
| [색인 검사](tests/test_indexing.py) | 새 35개: 임시 상태·독립 Knowledge 대역·실제 loopback HTTP |
| [indexing_flow.py](indexing_flow.py), [고정 Flow](indexing_flow_template.py) | Q05b 별도 실행권·80 step/1200초·명시 resume·paused_check |
| [indexing_runtime.py](indexing_runtime.py), [연결 절차](indexing_connection.py) | 기본 추론 금지·선택 모델/청커 GET 검증·명시 보류 연결 인수 |
| [workflow 검사](tests/test_indexing_flow.py) | 새 48개: 별도 실행권·추론 보류·정제 완료 연결·HTTP callback·게시 정의 |
| [search_tool.py](search_tool.py), [계약](search_tool_results.py) | Q06a additive 저장·고정 검색 Tool·intent/receipt·시험/발행 조회 gate |
| [search_tool_transport.py](search_tool_transport.py), [연결 안내](../../../docs/document-qa/search-tool.md) | Agent 제한 HTTP·별도 관리/추론 opt-in·로컬 CLI |
| [question_store.py](question_store.py), [schema](question_schema.py), [참조 계약](question_targets.py) | Q06b-1 atomic additive migration·질문 head/revision 조회·변경 불가능한 후보 저장 |
| [질문 저장 안내](../../../docs/document-qa/question-storage.md), [검사](tests/test_question_store.py) | 기존 데이터/구버전 호환·프로세스 중단·재실행·동시 업로드 검증 |
| [qa_agent.py](qa_agent.py), [schema](qa_agent_schema.py), [검증](qa_agent_results.py), [HTTP](qa_agent_transport.py) | Q06b-2 고정 Agent·chat/Tool/Connection pin·시험/발행·질문 요청/결과·원문 인용 gate |
| [Agent/질문 안내](../../../docs/document-qa/qa-agent.md), [검사](tests/test_qa_agent.py) | 중단/동시 실행·같은 키 복구·head 고정·인증 질문 HTTP·기존 저장 보존 |
| [question_head.py](question_head.py), [schema](question_head_schema.py), [검증](question_head_results.py) | Q06b-3 읽기 전용 색인/Tool/Agent/시험 재검증·원자 CAS·불변 전환 receipt |
| [head 안내](../../../docs/document-qa/question-head.md), [검사](tests/test_question_head.py) | 동시 전환·commit/응답 유실·실제 프로세스 중단·과거 질문 보존·기본 serve 연결 |
| [화면](web/index.html), [화면 동작](web/app.js), [파일 해시/요청](web/upload.js), [정적 경로](web_assets.py) | Q07a 같은 서버 `/console`·기존 bearer 인증·업로드/조회·탭 단위 접수 확인 |
| [화면 안내](../../../docs/document-qa/screen.md), [HTTP 검사](tests/test_screen.py), [화면 대역](tests/screen_ui_test.cjs) | GPU 없이 기존 저장 보존·Host/Origin·접수/응답 유실/파일 변경/재연결 검사. 실제 브라우저 인수는 후속 |
| [recovery.py](recovery.py), [재개 화면 계약](web/recovery.js), [재개 안내](../../../docs/document-qa/error-recovery.md) | Q07b 서버 상태별 동작·실행 전 revision 재검증·동일 요청 유지·CPU만 명시 재개 |
| [재개 HTTP 검사](tests/test_screen_actions.py), [재개 화면 대역](tests/screen_actions_ui_test.cjs) | 기존 GPU 실행권 보존·중복/유실·같은 요청/세대·취소·새로고침·401/상태 변경 검사 |
| [질문 화면](web/questions.js), [안내](../../../docs/document-qa/question-screen.md) | Q08·현재 질문 정책·접수/명시 실행/동일 요청 복구·과거 답변·실제 출처의 원문/위치 표시 |
| [질문 HTTP 검사](tests/test_screen_questions.py), [질문 화면 대역](tests/screen_questions_ui_test.cjs) | 새 HTTP10개·DOM/fetch36개. 전체 관련42개·QA550개·세 화면 대역85개는 개발 검사이며 실제 브라우저/운영 질문은 후속 |
| [설정 예시](settings.example.json) | API origin/인증 참조 |

서버는 Python 3.10 이상/표준 라이브러리다. Q07b와 결과 문서9개 적용까지 완료한 호스트에서 이번 화면 관련42개 검사와 읽기 전용 status만 실행한다. 화면은 번들/Node 서버·추가 DB 초기화가 필요 없다. JS 대역 검사는 개발 환경 Node 20 이상에서 수행하며 서버 적용의 필수 명령이 아니다.

```bash
python3 -B -m unittest discover -s src/apps/document_qa/tests -p 'test_screen*.py' -q
python3 -B -m src.apps.document_qa.api status
```

Q06b-3 init는 기존 DB에 별도 qa_head_* 저장만 추가하며 Q06b-1 head/revision·기존 전역 user_version=1·library.question_revision=0·작업·receipt·Flow setup·키를 유지한다. 기존 초기화/migration을 반복하지 않는다. CLI init/status는 운영 API·모델/GPU·HTTP 서버를 호출하거나 시작하지 않는다. 구버전 코드 복귀 시 추가 DB/질문 이력은 삭제하지 않는다.

기본 serve는 이미 초기화한 Q06b-3 저장을 읽어 질문 접수/조회·정책 경로를 연결한다. 새 저장을 자동 초기화하지 않으며 미초기화 상태는 비활성으로 둔다. head 전환은 --allow-question-head-activation, 질문 AgentRun 전송은 --allow-question-inference가 각각 필요하고 기본값은 모두 금지다. 색인 추론 옵션과도 별개다. Q06b-3 저장 init/status는 이미 완료했다. Q07a 적용/관련 검사/status는 API/서비스를 시작하지 않는다. `/console` 확인 때 수동 QA API만 명시 재실행해야 하며 추론 옵션은 그대로 금지한다. 운영 질문 인수는 후속이다. 질문의 답변/인용은 필요한 bounded 결과로 저장하며, 전체 원본/canonical/청크/벡터를 별도 복제하지 않는다.

Q05 실행 adapter 함수는 Q05b 연결을 위한 구현이다. Knowledge index 생성/활성화는 명시 쓰기 opt-in, build는 별도의 추론 opt-in도 필요하다. Q05a CLI에는 원격 configure/dispatch/activate 명령이 없다. Q05b의 명시 Flow prepare/dispatch는 별도이며 서버 추론 opt-in과 live profile gate를 통과해야 Knowledge 쓰기를 수행한다. 본문·청크·벡터를 복제하지 않고 원래 플랫폼의 식별자·검증 요약만 저장한다. SQLite/file lock은 단일 호스트 adapter이며 여러 replica의 실행권 인수를 뜻하지 않는다.

절차와 한계는 [Q05b workflow](../../../docs/document-qa/indexing-workflow.md), [Q05a 색인](../../../docs/document-qa/indexing.md), [Q04b CPU Flow](../../../docs/document-qa/refinement-workflow.md), [작업계획서](../../../docs/plans/document-qa-work-plan-20261002.md), [현재 상태](../../../docs/status.md)에 모은다.
