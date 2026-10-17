# 최종 독립 검토 추가 대조 — native RPC 카운트 정정

판정: **PASS** — 최종 budget의 예약값 잔존을 실제7/7 응답에 맞춘 근거 정정이다. 기존 최종 구현/새4/변이2 PASS 및 보고서 bytes는 유지한다.

- 정정 budget-final.json SHA256: `f44d887e46753569d1388a8cc4f300c6b948108d53a35e67b16f5d8583cfbcda`
- 보존 이전 final SHA256: `48eb71e22e6bfa67750259fac03f52bf1f87ccdbbf25e2567299449f4a4123e5`
- 최초 budget.json SHA256: `719178f8baba365ca210314d0fa15bc686291562b2b4cf8d1b4d89524ba10324`
- 실제 native-observation.json SHA256: `1f99dbdbf0de5923cee9679e52e8742a7807910a6ce2e48772ebcfd14c419acd`
- 기존 최종 보고서 SHA256: `850397b3226dde8683cdc2617127d0cdab8fc1cfca6a83475d37671511ca788a`

정정 전후 실제 diff는 rpc_sent 0→7과 count_reconciliation 이유·시각·이전 final 경로·new_rpc=0 기록 추가뿐이다. 이전 final에도 confirmed_by_matching_response=7과 entered=7이 있었고, 최초 예약 budget의 rpc_sent=0만 잘못 복사돼 있었다. 최초 예약0은 과거 준비 상태로 보존하며 최종 실행 카운트로 사용하지 않는다.

저장된 실제 관측은 initialize1/thread-read3/turns-list3, request7/entered7/response_observed7, retries0, 성공 상태 및03:25:25.546989Z 종료를 기록한다. response_observed는 검토한 고정 transport가 matching JSONRPC ID/result를 반환한 뒤 기록되는 값이다. 따라서 이 D18 창의 최종 카운트는 **실제 RPC7 / matching responses7 / retry0** 하나로 일치한다. initialized notification1은 RPC와 별도다. rootstate.d18_renewal_20261017도 같은 final7과 사유를 반영한다.

승인 창03:16:41–03:31:41Z, rpc_limit8, CLOSED_SUCCESS_NO_FURTHER_PROBES, 남은1 이월 불가, resume/old6 금지와 scheduler PAUSE는 변경되지 않았다. 정정 시각은 근거 문서 정리 시각이며 native 조사 재개나 실행 시각으로 해석하지 않는다. D06상 기대 결과·필수 검사·권한·기한을 완화하지 않는 기록 정정이며 제품/검사 재실행은 불필요하다.

기존 최종 보고서도 이미 실제7/7을 명시하므로 본문과 full SHA는 변경할 필요가 없다. 이번 정정 final·보존 이전 final·이 추가 검토를 최종 evidence 묶음과 manifest/hash/readback에 함께 반영해야 하며, 이 판정만으로 원격 발행본 갱신 완료를 주장하지 않는다. 수정본 native acceptance·전체 controller·두 native 작업은 계속 NOT_RUN이다.

이번에는 저장된 JSON 전후 diff/카운트/hash와 로컬 rootstate 항목만 읽고 이 보고서만 작성했다. 추가 RPC/native/DB/socket/다른 agent 조회·제품/검사 import/실행·운영 변경은0이다. 초기 budget·실제 관측·기존 review/실행 결과를 수정하지 않았다. 기존 단위04:00Z 마감과 누적 제한은 그대로다.
