# Metadata probe v3 독립 delta 검토

판정: **PASS** — 고정 v3의 정적 delta 범위에서 F1/F2 해결을 확인했다. 실제 실행 또는 native 인수 PASS가 아니다.

- v3 전체 SHA256: `39e56cc65ba36ee4314384a26502e734720e9a232bc063407cee55ec3c19b790`
- 범위: initialize1 + 고정 세 thread/read3 + 같은 세 thread/turns/list3 = 최대7 RPC, initialized notification1 별도, 재시도0.
- 승인 창: **2026-10-17 03:16:41Z→03:31:41Z**. 전체 실행 종료 마감은 그대로이며 연장/재설정하지 않는다.
- 정확한 검토 대상 명령(리뷰어 미실행):

```sh
/usr/bin/timeout --signal=TERM --kill-after=1s 49s python3 -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/transition-evidence/native-contract-20261017/observe_metadata_once_v3.py
```

**F1 해결(24–39, 119–129행):** 실제 v2→v3 diff에서 queue/list validator 및 두 요청·응답 보존 분기가 모두 제거됐다. items/list도 요청과 validator에 없다. turns/list 허용은 세 고정 thread, cursor=null, builtin int limit=1, itemsView=notLoaded, sortDirection=desc로 제한된다. 따라서 queue/list 부작용을 확인하기 위한 호출은 전혀 필요하지 않으며 허용하지 않는다. 빈 queues/items 배열은 조회 실행 근거가 아니다.

**F2 해결(46–83행):** thread.status는 알려진 type enum과 최대2개 승인 activeFlags만 새 dict로 투영한다. 기타 원문 status 필드는 복사하지 않는다. thread 식별자는 요청한 고정 값이고 parent는 null 또는 같은 고정 집합만 허용한다. cwd/role/version/model 등의 freeform은 null 또는 builtin str/bool/int만 받아 hash하며 문자열은4096자로 제한한다. role 평문은 고정 alias만 허용한다. turn은 제한된 문자·길이의 id, 고정 status enum, notLoaded만 평문 보존하고 root/time scalar는 hash한다. 반환 items가 비어 있음을 확인하며 message/error/name/account 및 임의 필드명 복사 경로는 없다. 예상 밖 nested/type/enum은 assert로 중단되어 기존 고정 오류 분류·STOPPED_NO_RETRY 경로로 간다. 정적 코드 대조이며 실제 응답의 형태를 입증한 것은 아니다.

변경 없는 PR16 transport/readiness pin, exclusive receipt·call entered/response_observed 의미, total45s/request5s·외부 TERM49/KILL+1·연결 전 남은55초 guard 및 첫 실패 중단은 v1 검토를 재사용했다. 기존 PASS/실행/검사를 반복하지 않았다. [이전에 확인한 공식 조회 계약](https://learn.chatgpt.com/docs/app-server#read-a-stored-thread-without-resuming)에 따라 thread/read(includeTurns=false) 및 turns/list(notLoaded)는 resume 없는 조회로 한정된다. 설치 버전이 이를 지원하지 않거나 응답 검증에 실패하면 중단하며 resume·fallback·추가 page·재시도로 보완하지 않는다.

**한계 및 실행 조건:** 최신 turn 최대1건씩의 제한된 metadata 관측이며 전체 history/queue/내용·원격 완료를 검증하지 않는다. v3 파일과 위 명령에만 판정이 적용된다. v1/v2는 실행하지 않고 기존 기록을 보존한다. coordinator 실행 시에도 동일 exclusive marker가 이미 있으면 중단하며 창 안에 전체 wrapper가 끝날 여유가 없으면 실행하지 않는다. 결과에 entered만 있으면 송신/원격 적용을 단정하지 않는다. 실행 실패·부분 receipt도 보존하고 재시도하지 않는다.

리뷰어는 v3 bytes/hash와 실제 diff만 읽고 이 보고서를 작성했다. source 수정/import/검사/진단 실행/socket/RPC/다른 agent 조회는 없다. scheduler PAUSE·운영/auth 보호를 유지한다. 이 PASS는 승인된 metadata 창의 정적 적합성만 의미하며 controller/owner/두 native 작업 인수 또는 창 밖 D18 보류 해제가 아니다.
