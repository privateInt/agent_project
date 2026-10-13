# 질문·답변·출처 화면 — Q08

2026-10-08에 GPU 없이 기존 `/console` 화면에 질문을 연결했다. 코드 버전은 q08.1이다. [화면 안내](screen.md), [질문 adapter](qa-agent.md), [head/정책](question-head.md), [개발 근거](../testing/document-qa-q08-offline-20261008.json)를 따른다. 당시 서버 applied/written=20·관련42개(5.166초) OK·status passed는 [서버 근거](../testing/document-qa-q08-server-observed-20261008.json)에 보존했다.

2026-10-12에는 기존 TXT의 실제 GPU 색인·검색 Tool·QA Agent 답변/출처 시험과 발행, 첫 head CAS(head_revision=1)·인증된 question-policy의 question_ready=true까지 확인했다. 이후 PC 화면은 서버의 UUID v5 target을 거부해 확인 필요로 표시했다. [질문 UUID 호환 수정](question-uuid-fix.md)이 현재 복구 절차다. 수정/서버 적용 후 PC에서 질문 가능·코드 질문 접수를 확인했고 해당 답변은 MISSING_REFERENCE로 실패했다. prompt-only 초안도 동일 실패다. 코드/부서 답변·원문 인수는 남았으며 [최신 관찰](../testing/document-qa-live-observed-20261012.json)과 [인수인계](../HANDOFF_NEXT_SESSION.md)를 따른다. 과거의 빈 head/미확인 상태 기록은 당시 근거이며 현재 head 상태를 대신하지 않는다.

## 사용자 흐름

1. 자료함에 연결하면 기존 문서와 질문 기록을 조회한다. 질문 가능 여부는 현재 `/api/question-policy`로 확인한다. 준비 전이거나 조회가 실패하면 새 질문은 비활성으로 표시한다. 화면에 모델·기술 ID·추론 옵션 설정을 요구하지 않는다.
2. 질문하기는 UTF-8 8KiB까지의 질문을 현재 정책으로 재검증한 뒤 로컬 접수한다. 정상 접수된 새 질문만 첫 실행을 한 번 요청한다. 접수 성공과 답변 완료를 구분한다.
3. 답변 준비 중에는 답변 진행 확인을 선택하고 확인 화면에서 실행한다. 기존 AgentRun/ToolCall만 조회하며 새 추론은 생성하지 않는다. 자동 5초 갱신은 로컬 저장 GET뿐이며 observe/dispatch/retry POST를 실행하지 않는다.
4. 실행 전 질문은 답변 생성하기, 접수 불확실 질문은 같은 질문 실행 접수 확인으로 원래 질문/intent를 유지한다. 선택·취소는 GET뿐이며 실행 직전에 현재 정책/질문 상태를 다시 조회한다. 완료·실패·재전송 한도에 도달한 질문은 새 실행 동작을 제공하지 않는다.
5. 답변 완료·답변 실패·근거를 찾지 못함을 별도로 표시한다. 검증된 complete 결과만 답변으로 표시하고 빈 응답이나 인용 오류를 성공으로 바꾸지 않는다. 답변 잘림·일부 자료 제외·자료 품질 경고를 함께 표시한다.
6. `[S1] 원문 보기` 같은 링크는 같은 화면의 해당 출처 발췌로 이동한다. 출처의 실제 원본 ID가 현재 자료함의 문서 하나와 일치하면 문서 이름과 준비 상태 보기 버튼을 연결한다. 없거나 중복이면 질문 당시 자료로 표시하고 기술 참조는 접힌 확인 정보에 둔다.

과거 질문은 당시 target/revision·Agent 버전·검증된 출처를 유지한다. 현재 head나 재정제된 문서의 본문으로 바꾸지 않는다. 표시하는 원문은 당시 검증된 검색 조각의 발췌이며 전체 원본/PDF 다운로드 경로는 추가하지 않는다. 페이지는 `source_spans[].metadata.location.page`의 실제 양수 값만 표시한다. 없는 경우 블록 ID·저장된 문자 구간(0부터, 끝 제외)만 표시하며 페이지 번호나 외부 URL을 추정하지 않는다.

## 응답 유실과 개인정보

접수 POST 전에 schema/kind/library/client key/질문 내용, 실행 POST 전에 schema/kind/library/question ID/operation을 탭 sessionStorage에 기록한다. 접속 키·플랫폼 credential·답변/파일 bytes는 저장하지 않는다. 같은 질문 내용/키 복구를 위해 접수 확인 기록에는 질문 내용이 임시 저장된다는 안내를 화면에 둔다. 접속 키는 기존 화면 메모리만 사용한다.

응답 유실·새로고침·재연결에서는 원래 key 또는 question ID로 GET부터 확인한다. 기존 접수가 발견되면 자동 실행하지 않고 필요한 동작을 다시 선택한다. 접수가 없으면 명시 같은 질문 접수 다시 확인만 원래 키/본문으로 재전달하며 그 응답 뒤에도 자동 첫 실행을 하지 않는다. 실행 불확실 상태는 같은 질문과 adapter의 원래 key/body로 명시 재확인한다. 다른 자료함/질문, 손상된 기록, 기록 저장 실패는 쓰기를 차단한다.

401/연결 해제/pagehide는 질문·답변과 인증 메모리를 지운다. 불확실 요청의 탭 기록은 이후 GET 확인을 위해 보존하며 확인되면 제거한다. 영구 탭 종료 시 기록은 사라지고 다른 탭과 공유하지 않는다. 서버의 기존 lane·intent·receipt 보호는 유지한다. UI 재검증은 기존 backend 검증을 대체하는 전역 CAS/분산 트랜잭션이 아니다.

## 완료한 서버 로컬 확인

Q07b 구현과 결과 문서9개까지 적용한 프로젝트에서 Q08 checksum OK·applied/written=20을 확인했다. 아래 검사는 이미 서버에서 42개(5.166초) OK·status passed로 완료한 기록이다. 이번 결과 문서 패치 적용 때문에 검사/init/status를 반복하지 않는다.

```bash
python3 -B -m unittest discover -s src/apps/document_qa/tests -p 'test_screen*.py' -q
python3 -B -m src.apps.document_qa.api status
```

개발 검사는 관련 화면42개(기존32+새10), 전체QA550개, JS DOM/fetch 대역25+24+36=85개다. 임시 SQLite·독립 Agent 대역·loopback HTTP/DOM 모델이며 실제 서버/브라우저 질문을 실행한 근거가 아니다. 서버 CLI status는 screen_questions_implemented=true·q08.1·/console·screen_question_policy_source=/api/question-policy를 보고했다. 이 경로 metadata는 실행 중 API의 실제 GET 근거가 아니다. 기존 문서1개·CPU canonical_verified·색인 not_requested/Flow GPU_VERIFICATION_DEFERRED, head revision0·대상 없음·전환0·Agent plan0·질문0·question_ready=false를 조회했다. 운영 API 요청0·추론 요청false다. 새 init/migration·fixture SQL·추론/전환 옵션 변경을 하지 않는다.

다음 주 화면 확인 때 기존 수동 QA API만 [화면 안내](screen.md)의 같은 사용자/주소/상태로 정상 종료 후 명시 실행한다. 패키지 자체는 어떤 서버나 모델도 기동하지 않는다. 빈 head에서는 질문 준비 전과 기존 GPU 대기 문서 조회를 확인한다. 실제 질문/출처·답변 품질 인수는 GPU 사용 가능→Q05c 첫/두 번째 문서 색인/검색→Tool/Agent 시험/발행/readback→CAS와 별도 질문 추론 허용이 선행한다.

실제 브라우저에서는 한국어 긴 답변/출처·좁은 화면·키 해제·동시 클릭·접수/실행 응답 유실·새로고침·실행 대기/실패/근거 부족·과거 질문을 확인한다. 답변 구조/출처 검증과 의미 품질을 구분한다. Q07a/Q07b의 실제 업로드/CPU 재개 인수도 남아 있다.

rollback은 화면/코드/문서만 원복하고 DB·질문·Run/intent/receipt·head·키를 삭제하지 않는다. 구 화면은 Q08 탭 기록을 해석하지 않으므로 미확인 요청은 재적용하거나 기존 질문 API/관리자가 원래 참조로 확인한다. 실행 중인 QA API의 코드 복귀도 명시 재실행이 필요하다.
