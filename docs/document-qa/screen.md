# 문서 업로드·자료 상태 화면 — Q07a/Q07b/Q08

2026-10-08. GPU 보류 중 전용 화면을 먼저 구현했다. 기존 QA 서버가 `/`·`/console`·`/console/`에서 같은 화면을 제공한다. 서버 코드 적용·관련12개 검사(0.583초)·status passed까지 확인했다. [서버 근거](../testing/document-qa-q07a-server-observed-20261008.json)에 전체 status 출력을 기록했다. Q07a 결과 문서8개 적용도 확인했다. Q07b [오류·CPU 재개 화면](error-recovery.md)을 구현·대역 검사했다. Q07b도 서버 applied/written=21·관련32개(1.980초) OK·status passed, q07b.1·CPU 재개 기능 표시까지 확인했다. [Q07b 서버 근거](../testing/document-qa-q07b-server-observed-20261008.json)를 따른다. Q07b 결과 문서9개 적용도 확인했다. Q08 [질문·답변·출처 화면](question-screen.md)은 구현·대역 검사했고 현재 코드 화면은 q08.1이다. Q08도 서버 applied/written=20·관련42개(5.166초) OK·status passed까지 확인했다. [Q08 서버 근거](../testing/document-qa-q08-server-observed-20261008.json)의 질문 기능/정책 경로 표시는 실제 정책 HTTP 조회나 화면 렌더 근거가 아니다. 실제 QA API 재실행·브라우저/운영 질문 인수는 남았다. 개발 HTTP/화면 대역 근거는 [검사 결과](../testing/document-qa-q07a-offline-20261008.json), 진행 범위는 [상태표](../status.md), 순서는 [계획서](../plans/document-qa-work-plan-20261002.md)에 둔다.

## 사용하는 흐름

1. 기존 자료함의 접속 키로 연결한다. 서버 주소·자료함·문서/job·플랫폼 ID를 화면에서 입력하지 않는다. 접속 키는 화면 메모리에서만 사용하고 새로고침/연결 해제 시 지운다. 쿠키·URL·탭 저장에 키를 넣지 않는다.
2. 파일 하나를 선택한다. 문서 이름은 파일명에서 자동으로 채우며 바꿀 수 있다. TXT·Markdown·텍스트 PDF, 파일당 32MiB, 자료함 최대 20개를 유지한다. 스캔 PDF의 OCR 준비를 파일 접수 성공으로 대체하지 않는다.
3. 문서 올리기는 기존 `/api/uploads` 한 경로를 호출한다. 기존 Data/CPU/Flow 처리 계약을 사용하며 서버에 허용되지 않은 추론·색인·모델 기동을 화면에서 켜지 않는다. 접수 완료가 정제/색인/질문 완료를 뜻하지 않는다.
4. 등록한 문서와 원본 업로드→내용 정리→검색 준비를 확인한다. 5초 조회·상태 다시 확인·이 문서 다시 확인은 기존 자기 API의 GET뿐이다. 이 조회 버튼/자동 갱신은 새 작업/observe/재시도 POST를 만들지 않는다. Q07b의 별도 명시 동작은 [재개 안내](error-recovery.md)에 따른다. GPU_VERIFICATION_DEFERRED는 GPU 사용 대기로 표시한다. 다른 blocked 오류를 GPU 대기로 숨기지 않는다.
5. 문서 상세의 실행 가능한 동작을 선택하면 최신 상태를 조회하고 실행 내용을 보여준다. 실행 버튼을 눌러야 업로드·CPU 단계 하나만 요청한다. 응답이 유실되면 이전 동작 상태 확인부터 수행한다. 완료 문서·GPU 대기·terminal 정제 실패에는 재실행 버튼이 없다.
6. 질문 가능 상태는 Q08의 `/api/question-policy`가 보고한 현재 실행 정책을 재확인해 표시한다. 한 문서의 색인 완료나 과거 질문 응답의 question_ready=false로 현재 실행 가능 여부를 추정하지 않는다. [질문·답변·출처 화면](question-screen.md)에 접수/첫 실행·명시 결과 조회·같은 요청 복구·과거 답변/실제 출처 표시를 연결했다. 자동 갱신은 로컬 GET만 수행한다.

기술 ID·오류 코드는 문서 상세의 접힌 확인 정보에 둔다. 문서명·파일명·서버 문자열은 textContent로 표시하고 HTML로 실행하지 않는다. 외부 CDN·폰트·아이콘·브라우저 외부 연결은 필요 없다. 화면은 HTML/CSS/JS이며 별도 프론트 빌드나 Node 서버가 없다.

## 접수 응답 유실과 새로고침

POST 전에 원래 client key·자료함 ID·문서 이름·파일명·크기·SHA-256만 탭 sessionStorage에 저장한다. 파일 bytes·접속 키·다른 플랫폼 credential은 저장하지 않는다. LAN HTTP에서도 로컬 해시 계산과 UUID 생성이 동작하도록 SubtleCrypto/crypto.randomUUID에 의존하지 않는다. SHA 검사는 UTF-8·padding 경계·1MiB·32MiB로 확인했다.

응답이 유실되면 원래 `/api/upload-requests/{key}` GET부터 확인한다. 접수가 발견되면 기존 job을 복구하고 다시 파일을 전송하지 않는다. 아직 조회되지 않으면 명시적으로 같은 파일/이름을 선택했을 때만 byte hash를 대조한 뒤 원래 키로 재접수한다. 다른 파일로 새 요청을 자동 생성하지 않는다. 서버의 원래 metadata/hash 검증과 동일 파일 중복 제거를 그대로 유지한다. 입력 검증으로 명확히 거절한 일부 400/413은 접수 기록을 해제하지만, 불확실한 네트워크/503/401이나 충돌에서는 확인 기록을 유지한다.

새로고침 후 접속 키를 다시 입력하면 기존 작업 목록과 남아 있는 같은 탭의 접수 확인을 복구한다. 다른 자료함·손상된 탭 기록·저장 불가 시 파일 POST를 차단한다. 탭을 영구 닫으면 탭 기록은 사라지므로 기존 문서 목록을 먼저 확인한다. 서버는 동일 metadata/hash의 재접수를 기존 문서에 연결하지만, 이름/파일명까지 바꾼 새 접수는 별도 문서가 될 수 있다. 다중 탭의 공유 접수 기록/다중 사용자는 이번 인수 범위가 아니다.

## 완료한 Q07a 서버 로컬 확인

기존 Q06b-3 초기화와 결과 문서 적용을 재사용하고 아래 명령의 통과를 확인했다. 서버 적용은 written=17, 관련12개 검사(0.583초) OK다. 이번 결과 문서 적용 때문에 검사/status·init/migration·성공한 업로드/Flow를 반복하지 않는다.

```bash
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_screen.py' -q
python3 -B -m apps.document_qa.api status
```

status는 screen_implemented=true·screen_version=q07a.1·screen_path=/console을 보고했다. 기존 문서1개는 canonical_verified, 색인은 not_requested이고 색인 Flow는 GPU_VERIFICATION_DEFERRED로 blocked다. 질문 head revision=0·대상 없음·전환0·question_ready=false이며 운영 API 요청0·추론 요청false다. 이 화면 값은 코드 준비 여부다. 실행 중인 API의 새 화면 노출이나 실제 브라우저 인수를 뜻하지 않는다. 기존 DB/head·job/키/receipt/Flow를 유지하고 추론 요청 없이 조회한다. 패치 자체는 어떠한 서버도 재시작하지 않는다.

## 실제 화면 확인과 LAN 연결

2026-10-12 사용자는 GPU와 화면 확인이 가능하다고 갱신했다. q08.1 listening은 확인했으며 Windows 브라우저의 Docker 주소 접속은 시간 초과였다. 다른 PC에서는 [LAN 수신 주소 추가](lan-access.md)를 적용하고 `192.168.0.119:17110/console`로 확인한다. 기존 callback 주소를 유지하는 같은 프로세스의 추가 수신이며 실제 렌더/자료 조회 인수는 아직 남았다. 화면을 확인할 때 기존 수동 QA API를 해당 터미널의 Ctrl+C로 정상 종료한 뒤 기존 사용자/상태/주소로 명시 실행한다. 다른 플랫폼 서비스를 재시작하거나 키를 새로 만들지 않는다. 현재 알려진 QA bind/port의 예시는 다음과 같다.

```bash
cd /home/shshin/workspace/agent_project
python3 -B -m apps.document_qa.api serve --bind 172.25.0.1 --port 17110
```

이 명령은 색인 추론·질문 head 전환·질문 추론 허용 옵션을 추가하지 않는다. 직접 접속 가능한 bind 주소와 포트만 Host로 허용하고 loopback bind에는 localhost도 허용한다. 브라우저 Origin이 있으면 같은 허용 주소만 받는다. 기존 비브라우저 HTTP/1.0 callback의 Host 생략은 유지한다. 다른 hostname/프록시/tunnel 경로의 Host 계약은 이번 단계에서 추가하지 않는다. 알려진 주소가 다른 환경에서 곧바로 접속 가능함을 보장하지 않는다.

`http://172.25.0.1:17110/console`은 당시 환경의 접속 후보이며 이번 개발 환경에서 운영 주소를 직접 확인한 결과는 아니다. 기존 `~/.local/state/agent-project-document-qa/uploads/api-token`의 값을 사용자가 직접 접속 키에 입력한다. 실제 키를 채팅·검사 근거·스크린샷에 올리지 않는다.

첫 확인은 기존 문서 목록·상태만 조회한다. 이어 대표 새 파일로 이름 자동 채움·32MiB/20개 한도·정제/색인 단계·GPU 대기/오류·새로고침·접수 확인을 검사한다. 넓은 화면/좁은 화면에서 긴 한국어 이름·파일명·키 입력이 잘리는지, 키가 화면/저장에 남지 않는지 확인한다. 개발 브라우저 실행은 로컬 브라우저 다운로드 실패와 클라우드 브라우저의 임시 loopback 서버 접근 불가로 완료하지 못했다. 실제 렌더·모바일 배치·브라우저 multipart 전송/접수 유실 인수는 남았다.

GPU 사용 가능을 실제 확인한 후 Q05c 첫/두 번째 문서 색인·검색→실제 Tool/Agent 시험·발행/readback·CAS→Q08 질문/답변·출처 연결을 별도 진행한다. Q07b의 단계별 오류·CPU 재개는 서버 코드 적용·관련32개(1.980초) 검사/status까지 확인했다. CLI 조회는 동작 목록이나 실제 재개 실행의 근거가 아니다. 실제 브라우저 확인 때 Q07b 동작의 확인/취소·중복 클릭·응답 유실 후 GET 확인도 인수한다. 코드 rollback은 화면/코드/문서만 되돌리며 이미 접수한 문서·DB/head/질문 이력·키를 삭제하지 않는다.
