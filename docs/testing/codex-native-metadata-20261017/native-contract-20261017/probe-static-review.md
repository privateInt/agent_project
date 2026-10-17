# Native metadata probe 독립 정적 검토

판정: **REQUEST_CHANGES** — 현재 고정 명령은 아래 F1/F2 보정 전 실행하지 않는다. reviewer는 실행/RPC를 수행하지 않았다.

- observe_once.py 전체 SHA256: `5d609b664e56e16d6c582c609e5188e270540fd44855d55e54fc94a2f0107d7e`
- 승인 창: **2026-10-17 03:16:41Z→03:31:41Z**, metadata RPC 최대8·재시도0. 이번 검토 시간도 포함하며 연장/재설정하지 않는다.
- 정확한 현재 예정 명령(미실행·현재 판정으로 실행 불가):

```sh
/usr/bin/timeout --signal=TERM --kill-after=1s 49s python3 -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/transition-evidence/native-contract-20261017/observe_once.py
```

**F1 — queue/list 두 호출의 부작용 부재 근거 부족(33–34, 93–101행).** generated-contract-projection의 ThreadQueueListParams/Response는 thread/cursor/limit·data/nextCursor의 페이지 형식을 설명한다. 이는 서버 handler가 thread를 load/subscribe/resume하거나 queue 상태를 바꾸지 않는다는 증명은 아니다. 확인한 공식 문서에는 thread/queue/list 계약이 없다. 해당 설치 버전 handler의 조회 전용 구현이나 이를 명시한 공식 근거가 없는 현재 상태에서는 두 queue/list를 보내면 안 된다. 현재 코드에는 이 조건을 확인하거나 해당 호출만 보류하는 gate가 없으므로 호출과 allowlist 양쪽에서 제거해야 한다. 지원 여부 확인을 위해 먼저 호출해 보는 방식이나 실패 뒤 대체 호출도 허용하지 않는다.

**F2 — raw nested 응답을 그대로 보존하는 경로(88–91, 106, 118행).** thread.status/itemsView 등을 type/허용 값/길이 확인 없이 record에 복사한다. 예를 들어 thread.status가 error/message를 포함한 dict이면 현재 projection/save는 그 원문을 그대로 저장한다. 고정 transport는 result가 dict임을 검사할 뿐 이 nested schema를 검증하지 않는다. 이는 실제 응답에서 관찰한 오류가 아니라 현재 코드의 구체적인 비정상/확장 응답 저장 경로다. thread status는 알려진 type enum과 승인된 activeFlags만, turn status/itemsView는 고정 enum만 투영하고 나머지는 중단 또는 불명으로 처리해야 한다. freeform 메타데이터는 필요한 bounded builtin scalar/hash만 보존한다. sorted(item)의 임의 field_names도 알려진 schema key만 남기거나 미지 key의 개수/hash로 바꾼다. name/account/prompt/error body를 예상 밖 nested 객체와 함께 저장하지 않도록 한다.

**더 안전한 최대8 RPC 대체안:** initialize1 → 고정 세 thread의 thread/read 각1(includeTurns=false) → 같은 세 thread의 thread/turns/list 각1(cursor=null, limit=1, itemsView=notLoaded, sortDirection=desc) → 세 번째 thread에서 반환된 최신 turn이 있을 때만 thread/items/list1(cursor=null, limit=2, sortDirection=asc). 총 최대8, 최신 turn이 없으면7이며 남는 호출을 채우지 않는다. turns allowlist는 이 세 고정 thread/정확한 params·builtin int limit으로 제한하고 items는 세 번째 thread 및 거기서 검증한 turnId 한 개에만 제한한다. 첫 실패/unsupported/불완전 응답이면 즉시 전체 중단·0retry이며 다른 method나 resume으로 전환하지 않는다. 현재 source를 이 안으로 수정한 뒤 새 전체 SHA와 정확한 명령을 별도 delta 정적 검토해야 한다.

**부작용 근거:** [공식 OpenAI App Server 문서](https://learn.chatgpt.com/docs/app-server#read-a-stored-thread-without-resuming)는 thread/read가 subscribe/resume/load 및 thread/started 없이 저장 metadata를 읽고, turns/list가 resume 없이 history를 읽으며 notLoaded가 items를 생략하고, items/list도 resume 없이 persisted items를 읽는다고 설명한다. items pagination 미지원은 error이며 별도 load/resume으로 보완할 근거가 아니다. 조회가 서버 내부 로그 등 어떤 부수 기록도 전혀 남기지 않는다는 뜻이나 설치 버전의 완전한 일치 증명으로 확대하지 않는다.

**유지 가능한 제한:** PR16 transport `7f70d9eb3402fddd97f2a8c0f448b2b3336db28512d0c8e004adc7dc76e4b415` 및 readiness `5b84534a6e0b3bb19fdd6a2bd6e3c38f23ea89edc205b461751b5c0e857ee399` pin은 실제 파일과 일치한다. 수정 없이 subclass validator만 사용하는 구조다. 요청 ID 검증·remote error body 폐기는 고정 transport에서 수행한다. 한 연결 total45s/request5s, 외부 TERM49/KILL+1, 연결 전 남은55초 guard, 최초 exclusive marker 및 호출 전 entered 기록·첫 실패 중단·요청 상한8은 적절하다. initialized notification1은 RPC8과 별도다. entered/response_observed는 실제 전송 횟수나 원격 적용 확정과 구분한다. 수정본도 시작 전 전체 wrapper가 절대 마감 안에 끝날 여유를 확인하고 창 종료 시 중단해야 한다.

이번에는 OpenAI Docs skill로 공식 문서 및 로컬 고정 script/입력/저장된 generated projection을 정적으로 읽고 이 보고서만 작성했다. 제품/진단 코드 수정·import/검사·실행·socket/RPC·다른 agent 조회·credential/service 호출은 없다. 이전 PASS 검토는 반복하지 않았다. metadata 한정 재개는 resume/old6script/queue add/start/settings/worker 권한을 포함하지 않는다. scheduler/root/user PAUSE·운영/auth 보호는 유지하며, 이 창 밖의 기존 D18 보류를 해제하거나 native owner/controller/두 작업 인수로 표시하지 않는다.
