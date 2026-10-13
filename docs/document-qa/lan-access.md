# 문서 QA 화면의 LAN 연결

2026-10-12. 사용자 서버의 `q08.1` QA API가 `172.25.0.1:17110`에서 listening인 것을 확인했다. Windows 브라우저 캡처는 같은 주소의 `ERR_CONNECTION_TIMED_OUT`이다. 아직 화면 렌더나 자료함 연결을 인수한 결과는 아니다. 서버 주소 조회에서 `172.25.0.1`은 Docker bridge, `192.168.0.119`는 LAN 인터페이스였다. 모델/API 준비 조회는 네 플랫폼 reachable, embedding/chat 후보 없음·defaults_ready=false였고 GPU는 RTX 4090·사용량279MiB·사용률0%로 관찰됐다. 당시 관찰을 이후 모델 running이나 추론 검증으로 대체하지 않는다.

기존 API는 private IPv4 한 주소에만 수신하고 그 주소의 Host/Origin을 검증한다. 이번 변경은 `--additional-bind`로 같은 프로세스에 private IPv4 수신 주소를 추가한다. 같은 handler·키·자료함·callback·처리 정책을 사용하고 전체 연결 한도도 공유한다. 기존 n8n의 `172.25.0.1:17110` callback을 바꾸거나 Flow를 재등록하지 않는다. 화면 HTML/JS/CSS·버전q08.1·인증·DB/schema·추론 기본값은 유지한다.

## 적용과 실행

Q08 코드와 결과 문서9개 적용까지 완료한 프로젝트에 패치를 적용하고 새 수신 검사만 수행한다.

```bash
cd /home/shshin/workspace/agent_project
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_screen_hosts.py' -q
```

현재 실행 중인 QA API의 터미널에서 Ctrl+C로 정상 종료한다. 다른 플랫폼/서비스/모델을 재시작하지 않는다. `shshin` 계정 터미널에서 아래처럼 명시 실행하고 유지한다. root의 기본 상태 경로로 실행하거나 새 init/migration을 수행하지 않는다.

```bash
cd /home/shshin/workspace/agent_project
/home/shshin/miniforge3/envs/sh_test/bin/python3 -B \
  -m apps.document_qa.api serve \
  --bind 172.25.0.1 --additional-bind 192.168.0.119 --port 17110
```

listening 출력의 `bind=172.25.0.1`, `additional_binds=["192.168.0.119"]`, `screen_version=q08.1`을 확인한다. 추가 주소 바인딩 실패/포트 충돌에서는 부분적인 추가 서버를 정리하고 기동을 중단한다. 정상 종료 때 모든 수신 루프도 종료한다. wildcard `0.0.0.0`·공인 주소·hostname은 수신 주소로 받지 않는다. 추가 주소는 최대4개이고 기본/추가 주소의 중복은 거부한다. 추가 옵션을 생략하면 이전 한 주소 동작을 유지한다.

서버의 별도 터미널에서 두 주소의 정적 화면 GET을 확인할 수 있다. 이 명령은 인증 키·자료 본문을 출력하거나 새 작업을 생성하지 않는다.

```bash
curl --noproxy '*' --fail --max-time 5 -o /dev/null -w 'HTTP %{http_code}\n' \
  http://172.25.0.1:17110/console
curl --noproxy '*' --fail --max-time 5 -o /dev/null -w 'HTTP %{http_code}\n' \
  http://192.168.0.119:17110/console
```

같은 LAN의 PC 브라우저는 [문서 QA](http://192.168.0.119:17110/console)에 접속한다. 기존 `~/.local/state/agent-project-document-qa/uploads/api-token`의 QA 접속 키로 연결하며 키를 채팅/근거/스크린샷에 포함하지 않는다. 정상 연결에서 기존 문서1개·CPU 정제 완료·GPU 대기·질문 준비 전을 확인한다. HTML200과 실제 화면 렌더/인증 자료 조회는 별도 판정한다.

서버에서 두 GET은200인데 PC에서 시간 초과되면 서버 코드 바인딩과 LAN 경로/방화벽을 구분해 확인한다. 이 패치는 방화벽·라우터·포트포워딩·프록시를 수정하지 않는다. 현재 서버에서 관찰한 LAN 주소가 다른 호스트나 이후 IP 변경에도 유지된다고 보장하지 않는다.

## 검증과 다음 단계

[개발 근거](../testing/document-qa-lan-offline-20261012.json)에 실제 loopback 두 인터페이스/하나의 포트, 동일 키/handler/저장, 정확한 Host/Origin·잘못된 포트 거부, 인증401, 기존 HTTP/1.0 Host 없는 호출 호환, 전체 연결 한도 공유, 종료/포트 충돌/부분 바인딩 정리, 실제 CLI/SIGINT와 저장 바이트 보존을 기록한다. 실제 사용자 LAN 렌더·n8n 실행·운영 질문·GPU 색인 성공은 이 검사로 판정하지 않는다.

화면 연결 후 모델 배포 준비·기본값 저장→별도 색인 opt-in과 기존 보류 Flow의 새 실행 세대→Q05c 색인/검색을 인수한다. 기존 paused_check receipt/grant는 수정하지 않는다. 실제 Tool/Agent 시험/발행/readback·CAS·질문 추론 허용·답변/출처 인수는 뒤이어 진행한다. GPU 사용 가능이라는 사용자 갱신은 확인했지만 이 LAN 패치나 실행 명령이 추론 옵션을 켜지는 않는다.

rollback은 이 코드/검사/문서만 원복한다. 원래 코드로 복귀한 뒤 QA API를 기존 한 주소 명령으로 명시 실행한다. DB·키·job·질문/head·Flow/intent/receipt 이력은 삭제하거나 초기화하지 않는다. [화면 안내](screen.md), [질문 화면](question-screen.md), [색인 Flow](indexing-workflow.md)를 따른다.
