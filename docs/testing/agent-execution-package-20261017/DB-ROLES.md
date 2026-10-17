# 전용 상태와 DB 계약

후보는 승인 workspace 바로 아래 `runtime-candidate/pr23-314622f0ef1a`이며 최종 코드 버전은 DELIVERY.json에 별도로 고정한다. 기존 var 아래 후보는 상위경로0775로 거부되므로 변경하지 않는다. candidate/state는 UID1011,0700이고 파일은0600, 별도 inode다. 어떤 기존 DB도 초기화하거나 migration하지 않는다.

|파일|기존 API의 역할|application_id|user_version|
|---|---|---:|---:|
|fixture-ledger.sqlite|작업 배정 원문·hash·revision과 artifact 원문·hash 보존|1095779916|1|
|fixture-controller.sqlite|CLAIMED 및 PREPARATION_FAILED 기록. 응답 유실은 OUTCOME_UNKNOWN으로 보존|1095779907|1|
|intents.sqlite|기존 native 제출 intent 상태 보존용. 이 fixture launcher에서는 제출하지 않음|1095322161|1|
|bindings.sqlite|기존 intent와 소유 thread/turn의 binding 보존용. 현재 제출 없음|1095320113|1|
|receipts.sqlite|기존 queue 접수·관측 receipt 보존용. 현재 조회/제출 없음|1095848497|1|

정확한 CREATE TABLE은 bundle/schema-contracts.json의 ddl에 있다. 해당 DDL과 application_id는 고정 Git commit의 기존 다섯 원장 소스에서 AST로 추출한다. 이 표의 ID는 생성 시 schema-contracts와 다시 대조한다.

새 DB 초기화 스크립트는 설치본 installation/launch.py의 명시적 init-new 동작이다. COMMANDS.md의 외부 launcher hash 검증 후 실행한다. state가 조금이라도 존재하면 거부한다. 중간 오류는 init-failed.json과 부분 DB를 남기며 재실행으로 보정하지 않는다. verify/run/status는 기존 5DB 모두를 읽기 전용으로 스키마·버전 검사하고 누락/빈 파일/불일치를 거부한다.

assignment.json은 controller에 전달한 canonical bytes 그대로0600으로 저장한다. admissions.json은 시간대별 시작 수와 clock floor, checkpoint.json은 결과와 다음 index를 보존한다. checkpoint의 native_ready는 항상 false이다. root/user/package PAUSE 중 하나라도 있으면 새 배정을 시작하지 않는다. init-new 자체는 서비스 시작이나 작업 실행이 아니다.
