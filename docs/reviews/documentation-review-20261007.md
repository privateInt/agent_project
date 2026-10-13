# 최신 프로젝트 문서 검수와 통합

기준: 2026-10-07 사용자 첨부 `agent_project(5).tar`, Q05b 완료 기록까지 적용된 소스. 코드·API/저장/Flow 계약·보존된 결과와 문서를 대조했다. credential 28파일의 내용은 열지 않았고 패치에 넣지 않는다. 직접 서버 접속·GPU/모델/백업/복원/서비스 제어는 수행하지 않는다.

## 확인과 수정

| 발견한 항목 | 수정 |
|---|---|
| Q05b 완료 기록은 있지만 별도 다음 세션 계획이 저장소에 빠짐 | Q06a→Q06b 저장/Agent/CAS→Q05c 실제 색인→Q06 실제 연결→화면/변경/품질 순서와 완료선 통합 |
| 계획에 Q06 질문 대상의 실제 저장 전환·질문 API가 구체화되지 않음 | uploads schema의 `CHECK(question_revision=0)`/user_version=1, 순수 switch_head와 구분해 호환 migration을 Q06b-1, 질문 접수/AgentRun 조회를 Q06b-2로 명시 |
| Q01/Q03a/Q02 안내에 “HTTP/DB 없음·실제 Data 업로드/엔진 처리 미확인”이 최신 상태처럼 남음 | 각 단계 최초 결과와 후속 TXT 실제 서버 인수를 구분하고 상태표로 연결 |
| 날짜별 Ops 문서의 pending/다음 단계가 현재 안내와 섞임 | 현재 관측/발견·worker 계약 문서와 읽기 전용 이력 목차로 분리. old 명령은 압축 원문에서만 조회 |
| storage/source preservation/cleanup의 기준일·미선택/미확인 상태가 과거에 머묾 | 최신 업로드와 실제 기록에 맞춤. 운영 백업 보류·7일 이미지 보존 선택·서버 공간/503→후속 성공을 범위와 함께 반영 |
| 긴 완료 문단을 README/계획/HANDOFF/기능 문서에 반복 | 완료 근거는 status, 계획은 master plan, 환경/ID는 HANDOFF로 소유 위치를 정리 |
| 72개 Ops 단계 Markdown과 다수의 오래된 JSON이 분산 | 원문·SHA·byte·mode를 한 gzip JSON으로 무손실 통합. 옛 링크는 원문 항목 목차로 갱신 |
| readiness 코드가 capacity JSON을 직접 읽음 | 원래 경로/바이트 보존. 최신 QA/복구 결과와 대표 운영 근거도 분리 유지 |
| static configuration flag와 live receipt가 혼동될 수 있음 | 코드 flag를 변경하지 않고 실제 서버 인수/대역 검사/현재 실행 상태를 구분 |

원래 Markdown의 파일 경로 링크는 깨진 항목이 없었다. 이번 파일 이동 이후 전체 Markdown 파일·anchor와 source consumer 참조를 다시 검사한다. 구체적 파일 변경·삭제/통합 경로·용량·검증 판정은 패키지 `manifest.json`/`review.json`에 기록한다.

## 보존한 한계

문서 QA question_ready=false·실제 색인/검색·Agent/화면/품질 미인수, Q02 모델 기본값 미준비, GPU 실행 보류를 유지한다. 과거 Agent의 질문 반복/인용 실패·Knowledge 최초 후보 불일치·Data 어려운 OCR 실패와 Ops 최초 컨테이너 변화 원인 미확정도 유지한다. 업로드 소스와 현재 서버 이미지/상태가 같다는 검증은 하지 않았다.

Ops worker의 짧은 정상 평가와 격리 암호화 파일 복원은 운영 백업/전체 재해복구·실제 reboot/Kubernetes 성공이 아니다. `backup_ready=false`, public measure 차단과 사용자 보류를 유지한다. runtime/설정/테스트/fixtures/source pins를 건드려 문서 상태에 맞추지 않는다.

## 원복과 이후 관리

적용 전에 검토한 모든 변경 파일과 관련 코드 해시를 확인하며 변경된 사용자 파일은 덮어쓰지 않는다. 원문은 패키지 before와 [통합 압축 이력](../history/ops-evidence-catalog.md)에 있다. 적용 중 오류는 그 호출에서 변경한 문서를 복원하며, 명시 `--rollback`은 문서만 되돌린다. 프로젝트에 패치 백업 사본을 만들지 않는다.

이 보고서의 적용 완료는 사용자가 서버에서 적용한 뒤에만 기록한다. 다음 개발은 [Q06a](../plans/document-qa-work-plan-20261002.md)이며 이번 검수에 기능 구현·GPU 인수·운영 장애 조사를 추가하지 않는다.

## 용량과 검증 결과

아래는 파일 내용 바이트의 합계이며 디스크 할당량/파일시스템 예약 공간과 다르다. 업로드에 포함된 비공개 28파일(7,914 bytes)은 내용 열람 없이 원래 크기를 합계에 유지했다.

| 범위 | 정리 전 | 정리 후 |
|---|---:|---:|
| 프로젝트 파일 내용 | 13,911,512 bytes | 12,787,512 bytes |
| 프로젝트 파일 수 | 1777 | 1584 |
| docs 파일 내용 | 3,183,707 bytes | 2,061,150 bytes |
| docs 파일 수 | 371 | 178 |

프로젝트 파일 내용 감소는 **1,124,000 bytes**다. Ops 원문 72 Markdown+126 JSON=198개를 377,408 bytes의 gzip JSON으로 무손실 통합했다. 현재 API/개발/검사 입력을 지워 용량을 줄이지 않는다. Model·Data·Knowledge tests/results 합계는 171,431 bytes로 기존 1 MB 예산 안에 있으며 이 수치는 docs/testing/fixtures와 별도다.

전체 내부 Markdown 경로/anchor와 코드의 문서 consumer를 검사했고 오류는 없다. 원문 198개의 byte·SHA·mode 일치, 실제 적용→재적용 0건→문서 원복의 byte/mode 일치, 사용자 수정 충돌 시 전체 쓰기 전 거부, symlink/hardlink/상위 경로 거부, 적용 중 쓰기/삭제 오류 주입 후 원복을 확인했다. Python 3.10 문법도 통과했다. 실행 코드/테스트/fixtures/설정/pins는 모두 동일 바이트·모드다. 활성 worker 61파일 해시도 그대로다.

이 검수는 문서·패키지 검사이며 플랫폼 전체 Go/DB/GPU/브라우저 테스트를 다시 수행한 결과가 아니다. 정확한 검사 항목과 판정은 패키지 validation.json에 보존한다.
