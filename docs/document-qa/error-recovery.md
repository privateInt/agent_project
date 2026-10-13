# 문서 오류·CPU 재개 화면 — Q07b

2026-10-08. [업로드 화면](screen.md)에 단계별 명시 동작을 연결했다. 서버의 최신 문서 상태와 준비된 연결에 따라 필요한 동작만 표시한다. 화면/HTTP 대역 검사와 서버 applied/written=21·관련32개(1.980초) OK·status passed를 확인했다. [서버 근거](../testing/document-qa-q07b-server-observed-20261008.json)에 전체 로컬 status를 기록했다. 실제 QA API 재실행·브라우저/운영 재개 인수는 남았다. [개발 근거](../testing/document-qa-q07b-offline-20261008.json), [상태표](../status.md), [계획서](../plans/document-qa-work-plan-20261002.md)를 따른다.

## 사용자 흐름

1. 문서를 선택해 원본 업로드·내용 정리·검색 준비를 확인한다. 상태 다시 확인과 5초 갱신은 GET뿐이다.
2. 실행할 수 있는 동작을 선택한다. 서버의 문서 상태를 다시 조회하고 실행 내용을 보여준다. 선택만으로 처리 요청을 보내지 않는다.
3. 실행 버튼을 누르면 최신 상태를 한 번 더 조회하고 선택한 동작을 한 번 요청한다. 그동안 다른 실행 버튼과 파일 접수를 잠근다.
4. 응답 확인과 단계 완료를 구분한다. 원본 전달 완료, CPU 정제 완료, 검색 준비, 질문 가능 상태는 각각 확인한다.
5. 응답이 없으면 이전 동작 상태 확인을 사용한다. 조회만으로 POST를 반복하지 않는다. 상태가 그대로인 경우에만 같은 동작을 다시 선택·확인할 수 있다.

GPU 대기 문서에는 색인 재개 버튼을 제공하지 않는다. 현재 성공한 TXT 문서도 원본/정제 재실행 버튼이 없다. 정제 실패의 terminal 결과·내용/출처 불일치·한도 초과는 관리자 확인이 필요하다. 모델·ID·n8n 설정을 일반 사용자에게 요구하지 않는다.

## 기존 요청을 유지하는 동작

| 현재 미완료 단계 | 화면 동작 | 기존 계약 |
|---|---|---|
| 원본 전송 전/결과 불확실 | 원본 전달 이어가기 | 서버의 원래 파일 bytes·metadata·업로드 키 재사용. 새 원본/job 생성 없음 |
| 진입 흐름 미접수 | 문서 처리 연결하기 | 이미 접수된 원본으로 기존 진입 handoff 접수 |
| 진입 요청 결과 불확실 | 처리 연결 다시 확인하기 | 같은 handoff/body 재전달, accepted receipt 재사용 |
| CPU 흐름 미접수 | 내용 정리 시작하기 | 현재 원본과 확인된 진입 receipt로 CPU 흐름 접수 |
| CPU 전달 결과 불확실 | 내용 정리 연결 다시 확인하기 | 같은 실행 세대·handoff/body 재전달 |
| Data 정제 접수 결과 불확실 | 내용 정리 접수 다시 확인하기 | 같은 정제 intent/key/body 재전달. 새 CPU 세대로 불확실 Data POST를 대체하지 않음 |
| 정제 receipt 있음·아직 미완료 | 정제 결과 확인하기 | Data GET만 수행하고 자기 정제 상태 갱신. 정제 POST 없음 |
| CPU 흐름 attention/시간 만료 | 내용 정리 이어가기 | 기존 정제 intent/receipt 유지, 새 CPU 세대 생성. 기존 세대 실행권은 superseded |

새 라우트는 선택한 단계 하나만 호출하며 다음 단계나 색인으로 직접 이어 보내지 않는다. CPU 흐름의 기존 n8n callback은 정제 완료 후 기존 정책에 따라 Q05b 색인 흐름을 전달할 수 있다. 저장된 paused_check 실행권과 기본 추론 금지 정책은 그대로이며, 이 화면은 이를 GPU 실행권으로 바꾸지 않는다. 실제 GPU 색인 재개는 Q05c의 별도 인수 대상이다.

## HTTP와 동시 실행

인증된 `/api/jobs`·`/api/jobs/{job_id}`는 `recovery`에 schema=1·문서 ID·불투명 revision·가능한 actions·이유를 추가한다. 조회는 로컬 상태/설정만 읽으며 원격 연결 확인·새 intent·DB 초기화를 하지 않는다. 기존 다른 클라이언트용 operator 경로는 유지한다.

`POST /api/jobs/{job_id}/recover`는 JSON `{ "action": "…", "revision": "…" }`만 받는다. 기존 bearer·Host/Origin 검사를 유지한다. 상태/로컬 설정 revision이 달라지면 409 RECOVERY_STATE_CHANGED, 가능한 동작이 아니면 409 RECOVERY_ACTION_NOT_ALLOWED로 실행 전에 거절한다. 색인·head 전환·질문 동작은 허용 목록에 없다. 서비스나 모델을 기동하는 경로도 없다.

한 Linux 호스트의 문서별 recovery lock은 Q07b 요청을 직렬화한다. 같은 상태를 보고 동시에 요청하면 첫 요청의 attempts/receipt/세대 변경으로 다음 요청은 거절된다. 실제 전송은 기존 adapter의 원본/정제 lane·handoff lock·게시 정의/대상/요청 검증을 사용한다. 외부 callback과 모든 기존 operator API를 아우르는 분산 트랜잭션이나 새로운 exactly-once 실행 ledger를 추가한 것은 아니다. 완료된 도메인 receipt와 실행 중인 세대의 보호는 기존 adapter가 유지한다.

## 응답 유실·새로고침

전송 전에 action·job ID·자료함 ID·revision만 탭 sessionStorage에 저장한다. 파일 bytes·접속 키·플랫폼 credential은 저장하지 않는다. 새로고침/재연결 후 GET으로 기존 문서부터 조회하며 자동 POST를 하지 않는다. 저장이 안 되거나 기록이 손상됐거나 다른 자료함이면 재개 POST를 막는다.

revision 변경은 현재 상태가 달라졌다는 뜻이다. 이전 요청 자체가 성공/완료했다는 증거로 표시하지 않는다. 상태가 그대로이면 같은 action/revision으로 명시 요청할 수 있으며, native adapter가 원래 key/body를 재사용한다. 탭을 닫으면 확인 기록은 사라진다. 다중 탭 기록은 공유하지 않지만 서버의 상태 재검증·직렬화는 모든 Q07b 요청에 적용된다. 오래된 브라우저에 기록만 있고 서버 코드를 원복했다면 복구 UI를 재적용해 확인하거나 관리자가 기존 job/receipt를 확인해야 한다.

## 완료한 서버 로컬 확인과 남은 인수

Q07a 서버 결과 문서8개까지 적용한 기존 프로젝트에서 Q07b 패치 written=21과 아래 검사/status 통과를 확인했다. 관련32개(1.980초) OK다. 결과 문서 적용 때문에 아래 검사/status·이전 init/migration·성공 업로드·Flow callback을 반복하지 않는다.

```bash
python3 -B -m unittest discover -s src/apps/document_qa/tests -p 'test_screen*.py' -q
python3 -B -m src.apps.document_qa.api status
```

서버에서 통과한 관련 검사는 기존 화면12개와 새 재개20개, 총32개(1.980초)다. 전체 QA540개와 JS 화면 대역25+24개는 개발 환경의 임시 SQLite·독립 플랫폼 대역·loopback HTTP·DOM/fetch 모델 검사다. 실제 서버/브라우저에서 재개 동작을 실행한 근거는 아니다. CLI status는 q07b.1·screen_recovery_implemented=true·upload_and_CPU_only를 보고하며 CLI job 조회에는 recovery 동작 목록이 없다. 실제 UI의 동작 목록은 설정을 읽은 실행 중인 API에서 제공한다. 기존 문서1개는 canonical_verified·색인 not_requested·색인 Flow blocked/GPU_VERIFICATION_DEFERRED이며 head revision=0·대상 없음·전환0·question_ready=false를 유지했다. 운영 API 요청0·추론 요청false인 로컬 조회이며 실제 재개 POST를 실행한 근거가 아니다.

사용자가 다음 주(10월 12일 주간)에 가능할 때 기존 수동 QA API를 [화면 안내](screen.md)의 같은 주소/사용자/설정으로 정상 종료 후 명시 실행한다. 첫 확인은 기존 GPU 대기 문서가 조회되고 재실행 버튼이 없는지다. 임시 대표 문서로 확인 화면·취소·중복 클릭·응답 유실 후 조회·새로고침·긴 한국어 이름·좁은 화면을 인수한다. 실제 브라우저 렌더·모바일·multipart/재개 전송은 이번 개발 환경에서 검증하지 않았다. GPU 준비와 Q05c, Q08 질문/답변/출처는 별도 단계다.

DB/schema·기존 키·Flow 정의·실행권·추론 기본값은 변경하지 않는다. 코드 원복은 새 화면/API/안내만 되돌리고 이미 생성한 문서·intent/receipt·CPU 세대·head/질문 이력을 삭제하지 않는다.
