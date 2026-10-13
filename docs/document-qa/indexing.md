# Q05a 문서 QA 색인 adapter

2026-10-12 갱신: 실제 QA용 embedding·답변 모델 준비와 Q02 설정 저장은 사용자 출력으로 확인했다. 기존 TXT의 색인 Flow generation=2는 첫 단계에서 `INDEXING_PROFILE_INVALID`로 중단됐으며 index/build/활성화는 생성되지 않았다. Q02가 저장한 표시 필드와 Q05a 검증의 호환을 수정했다. 패치의 서버 적용·실제 색인/검색 성공은 아직 확인 전이다. [원인·수정·명시 재개 절차](indexing-profile-fix.md), [근거](../testing/document-qa-indexing-profile-fix-20261012.json).

2026-10-07: 구현과 GPU 없는 새 35개·문서 QA 전체 276개 검사가 통과했다. **Q05a 서버 적용·35개 검사(1.564초)·init/status·TXT canonical 후보1개 plan은 확인했다.** [서버 근거](../testing/document-qa-q05a-server-observed-20261007.json). 운영 색인/검색은 미실행이다. [오프라인 근거](../testing/document-qa-q05a-offline-20261007.json)를 실제 GPU 인수와 구분한다. 이미 인수한 TXT 업로드·n8n/Data CPU 정제는 유지한다.

## 구현 경계

[색인 저장/adapter](../../apps/document_qa/indexing.py), [결과 검증](../../apps/document_qa/indexing_results.py), [Knowledge 전용 HTTP](../../apps/document_qa/indexing_transport.py)를 추가했다. Q05a 단독 패치는 API와 Flow 자동 연결이 없다. 후속 [Q05b 색인 workflow](indexing-workflow.md)가 정제 완료 뒤 별도 전달/실행권을 연결하며 실제 embedding/build/활성화·두 문서 검색은 Q05c다. Q06의 Tool/Agent 연결 전까지 question_ready=false다.

Data는 원본/canonical, Knowledge는 청킹·embedding·index/version, Model은 모델 실행을 소유한다. 연결 계층에는 문서/모델/색인 식별자·hash·intent·receipt와 판정 요약을 저장한다. 청크/벡터·본문은 저장하지 않는다. 플랫폼 DB·MinIO·OpenSearch 직접 접근은 없다. 이번 모듈에는 systemd/Docker ID/컨테이너 경로가 업무 계약으로 들어가지 않는다.

## 저장과 처리

같은 자기 uploads.sqlite3에 indexing_metadata/binding/head/tasks를 추가한다. 기존 PRAGMA user_version=1, upload/workflow/refinement 테이블·job·token·receipt와 library.question_revision=0은 유지한다. indexing_generation은 로컬 활성 색인 세대이며 질문 revision이 아니다. Q06에서 질문 대상과 원래 자료함 schema의 전환을 별도 처리해야 한다.

1. 로컬 Q04 canonical_verified·terminal/ready·issue_count=0·OCR 불필요 및 Data upload receipt를 확인한다. 원본/정제 hash·run·processor image·schema/rules와 형식을 고정한다.
2. 새 문서와 **이전 활성 문서 전체**를 immutable snapshot으로 묶는다. 최대 20문서·정제 본문 합계 32MiB, 중복 ID 금지·정렬 규칙을 적용한다. 이전 자료를 새 문서 하나로 덮어쓰지 않는다. 제거/교체 workflow는 Q09에 따른다.
3. Q02에서 검증해 저장한 embedding deployment/model version/space/profile/한도와 structure_v1 청커 버전/options를 고정한다. 자기 자료함용 논리 index 생성 intent와 build intent의 body/hash/key를 영속화한다.
4. 논리 index의 현재 활성 버전이 로컬 이전 버전과 일치해야 build를 접수한다. 접수202만으로 준비 완료가 되지 않는다. UUID/operation/run·snapshot 전체 입력·profile·청커·chunk 수/held 참조·chunk set hash를 조회로 검증한다.
5. ready 조회 뒤 활성화 직전에 index를 다시 읽는다. **build가 revision을 증가시키므로 생성 당시 revision은 재사용하지 않는다.** 이전 활성 버전+최신 revision CAS로 activate한다. 새 활성 버전과 revision, ready 상태를 GET으로 다시 확인한 뒤에만 로컬 head를 원자 전환한다. 질문 대상은 전환하지 않는다.

한 자료함의 비종료 작업은 file lock과 저장된 lane으로 직렬화한다. HTTP 중에 SQLite 트랜잭션을 붙잡지 않으며, 다른 업로드/정제 저장을 막는 전역 DB lock을 오래 유지하지 않는다. 두 번째 문서의 build 실패는 기존 활성 버전/문서를 유지하고 lane을 반납한다. 입력/설정 불일치나 응답 유실은 기록과 lane을 보존한 확인 필요 상태다.

| 상태 | 의미와 다음 조건 |
|---|---|
| pending / confirmation_required | 영속 intent, build 접수 결과 미확정. 새 key/job 생성으로 우회하지 않음 |
| waiting | 유효한202 receipt 확인, 준비 완료 조회 전 |
| index_ready | 정확한 입력/profile/청커·held/개수 확인. 아직 활성화/질문 준비 아님 |
| activation_confirmation_required | CAS 시도 기록 유지, 먼저 GET으로 결과 확인 |
| activated | 당시 새 색인 readback과 로컬 head 전환 확인. 현재 서버 건강 보장이나 질문 연결 완료가 아님 |
| attention_required / failed | 불일치/미확정 또는 확정 build 실패. 기존 정상 head 유지 |

완료/확정 실패의 로컬 재조회는 HTTP를 반복하지 않는다. 원격 현재 상태 인수는 별도다. index.create/build 접수 응답 유실은 명시 retry에서 **동일 body/key**를 사용한다. 자동 POST 재시도는 없다. activate에는 Idempotency-Key를 보내지 않고 시도별 CAS body/history를 보관한다. 응답 유실·프로세스 중단 뒤 GET으로 이미 활성화된 결과를 확인하면 POST 없이 반영한다. 결과 미확정 시 명시 retry도 이전 활성 버전이 그대로일 때만 가능하며 타 작업의 활성 버전을 강제로 덮어쓰지 않는다. 새 doc·새 index/version을 만들어 실패를 숨기거나 예전 색인을 자동 삭제하지 않는다.

## 로컬 검사 참조 — 서버 실행 완료

프로젝트 루트와 원래 shshin 사용자로 실행한다. root의 HOME에 별도 상태를 초기화하지 않는다.

```bash
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_indexing.py' -q
python3 -B -m apps.document_qa.indexing init
python3 -B -m apps.document_qa.indexing status
python3 -B -m apps.document_qa.indexing plan   --job-id 430b0e7b-7b4c-4ae7-9c57-6c979ca94343
```

plan은 인수한 기존 TXT job의 저장된 canonical 참조로 후보 snapshot을 계산한다. 논리 index/build intent를 만들거나 API/credential 내용을 읽지 않는다. plan/state 조회용 lock 외에는 저장 내용을 변경하지 않는다. 처음에는 index_configured=false, indexing_counts={}, indexing_generation=0, question_ready=false가 정상이다. 기존 TXT 후보는 document_count=1이며 base_generation=0/base_version_id=""다. 기존 upload_state=preparation_pending도 Q06 전까지 실패를 뜻하지 않는다.

패치 적용과 위 검사에는 GPU·실제 Model/Knowledge API·Flow 등록·서비스 제어가 없다. 수동 QA API 재실행도 필요 없다. 되돌리기는 패키지의 apply_patch.py --rollback으로 파일만 원복한다. 이미 초기화한 indexing 테이블·job/receipt·원격 자산을 삭제하지 않는다.

## 후속 연결 시 실행 조건

KnowledgeIndexer는 현재 연결 origin/token_file을 받으며 execute=True가 없으면 POST를 거부한다. build에는 allow_inference=True도 필요하다. 이번 CLI는 init/status/plan만 제공하므로 배포한 것만으로 실 임베딩이 시작되지 않는다. 허용 API는 index 생성, 특정 index/version GET, 해당 index build/activate뿐이다. 최대8요청·125초 budget·각 요청100초 timeout·64KiB body/response 한도로 제한한다. redirect/proxy/원문 에러 메시지는 전파하지 않는다.

[Q05b](indexing-workflow.md)는 모델 준비 GET gate·별도 실행권·작업 budget을 구현했다. 기본 서버 정책과 paused_check는 추론을 금지한다. GPU 보류 조건과 n8n 실행권/callback은 대역 검사 후 실제 서버에서도 확인했다. [Q05b 서버 근거](../testing/document-qa-q05b-server-observed-20261007.json). 실제 모델 준비·색인 gate 인수는 별도다. 현재 embedding/chat 중지 상태를 자동 기동하거나 Q02 기본값을 임의 합성해 운영 실행하지 않는다. byte 상한과 profile 고정은 구현됐으나 실제 chunk token 수·의미 검색 품질은 Knowledge/Model의 운영 경로와 Q05c에서 검증한다. 오프라인 가짜 profile은 테스트 fixture이며 운영 기본값으로 복사하지 않는다.

SQLite/file lock은 단일 호스트/지속 볼륨용이다. Kubernetes 단일 replica/볼륨 연결, 여러 replica의 저장/실행권 adapter 교체와 실제 이식은 별도 계획/인수다. 이 구현 검사를 Kubernetes 배포 검증으로 표시하지 않는다.

## 검증 근거와 한계

35개 검사는 임시 자기 DB와 독립 Knowledge 대역으로 두 문서 보존·32MiB/20문서·같은 body/key·응답 유실·프로세스 중단·동시 lane·실패·오염된 readback·fresh revision/CAS 충돌·GET 결과 복구를 확인한다. loopback HTTP로 헤더·redirect/proxy 거부·JSON/크기/budget를 검사한다. 전체 QA 276개 검사는 기존 업로드/정제/Flow 코드 호환성을 확인했다. 운영 API·서비스 제어·Model/GPU 호출은 0이다.

Q05b의 실제 n8n 등록/게시 정의·보류 callback은 확인했다. 2026-10-12 Model 준비·Q02 기본값 저장과 LAN 인증/기존 문서 조회까지 후속 확인했다. embedding/build·검색·신규 업로드 전체 흐름·운영 Tool/Agent·질문/답변 품질·장기 재시작/Kubernetes는 미검증이다. 이미 확인한 [TXT CPU 서버 근거](../testing/document-qa-cpu-live-observed-20261007.json)는 그대로 보존한다. 최신 진행은 [상태](../status.md), 다음 단계는 [작업계획서](../plans/document-qa-work-plan-20261002.md)를 따른다.
