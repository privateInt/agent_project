# Metadata probe v2 제거 delta 독립 검토

판정: **REQUEST_CHANGES** — items/list 제거 delta 자체는 적절하다. v1의 F1 및 남은 F2가 미해결이므로 현재 v2 명령도 실행하지 않는다.

- v2 전체 SHA256: `f0242b8964451e15fd1772330956854cdaa5c4d5cb07090dc727586fa0bdf9eb`
- 승인 창: **2026-10-17 03:16:41Z→03:31:41Z**, 재시도0·기한 연장/재설정0. reviewer 실행/RPC0.
- 정확한 v2 예정 명령(미실행, 현 상태 실행 불가; v1 실행 금지):

```sh
/usr/bin/timeout --signal=TERM --kill-after=1s 49s python3 -B /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/transition-evidence/native-contract-20261017/observe_metadata_once_v2.py
```

**제거 delta 확인:** 실제 v1→v2 diff는 thread/items/list validator와 조건부 요청·응답 투영 분기의 전체 제거, record.max_rpc 8→7 및 call 상한 8→7이다. 선언 파일의 전체 SHA와 실제 v2 bytes가 일치한다. 남은 items 빈 배열은 외부 호출이나 원문 보존 경로가 아니다. 따라서 items/list의 임의 field_names 보존 경로도 함께 제거됐다. 고정 transport/핀·절대기한·exclusive receipt·기존 timeout 등 변경 없는 부분은 v1 검토를 재사용하며 PASS 검토를 반복하지 않았다.

**F1 유지 — queue/list는 아직 실행 금지(33–34, 87–95행):** 제거 delta에는 queue/list 두 호출과 허용 분기가 그대로 남아 있다. 앞선 검토에서 확인한 generated request/response schema는 페이지 형식의 근거이며 서버의 load/subscribe/resume/queue 변경 부재를 확정하지 못한다. 새로운 조회 전용 handler/공식 계약 근거도 이 delta에는 없다. 따라서 현재 근거로 두 항목은 미실행해야 한다. 단순히 RPC 상한을7로 줄이는 것으로 해결되지 않으며, 현재 코드는 무조건 호출하므로 coordinator가 두 callsite와 validator 분기를 제거한 새 pin을 제시해야 한다. 지원 여부를 확인하기 위한 시험 호출·실패 후 fallback도 하지 않는다.

**F2 일부 유지 — raw nested projection(82–84, 99–101행):** thread.status 및 turn.status/itemsView 등을 type/enum/길이 검증 없이 저장하는 기존 경로는 변경되지 않았다. items/list 삭제로 임의 item field_names 문제는 해소됐지만 남은 필드의 예상 밖 dict/message 저장 가능성은 그대로다. 알려진 enum 및 필요한 bounded builtin scalar/hash만 투영하고 예상 밖 값은 고정 코드로 중단 또는 불명 처리해야 한다. 이는 정적 코드 경로 finding이며 실제 서버 유출을 관찰한 것이 아니다.

**더 좁은 실행 후보:** 위 F2 보정과 함께 queue/list 요청·allowlist를 제거하면 initialize1 + thread/read3(includeTurns=false) + thread/turns/list1(itemsView=notLoaded, cursor=null, limit=1)로 **최대5 RPC**다. items/list는 되살리지 않고 남는 예산을 채우지 않는다. thread/read 및 turns/list의 resume 없는 조회 근거는 [앞선 공식 문서 확인](https://learn.chatgpt.com/docs/app-server#read-a-stored-thread-without-resuming)을 재사용한다. 이를 queue/list의 부작용 보증이나 서버 내부 기록이 전혀 없다는 주장으로 확대하지 않는다. 수정본은 정확한 새 SHA/명령의 delta 확인 후에만 별도 실행 판단이 가능하다.

이번에는 기존 보고서·선언·v2 bytes와 제거 diff만 읽고 이 보고서만 작성했다. source 수정/import/검사/실행/socket/RPC/다른 agent 조회는 하지 않았다. scheduler PAUSE·운영/auth 보호는 유지한다. 전체 실행은 03:31:41Z 전에 끝나야 하며, 이 검토는 권한 범위 확대·D18 창 연장·native 인수 승인이 아니다.
