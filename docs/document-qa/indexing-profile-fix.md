# Q05c 설정 표시 필드 호환 수정 — 2026-10-12

사용자 서버에서 QA용 embedding과 답변 모델 준비·Q02 기본값 저장을 확인했다. 기존 TXT의 색인 Flow generation=2/execution=9는 접수됐지만 첫 단계에서 `INDEXING_PROFILE_INVALID`로 중단됐다. 저장된 설정에 포함된 표시용 `name`, `version`, `label`을 Q05a의 필드 집합 검사에서 거부한 원인이다. 모델 기동 실패가 아니며 색인 생성 전 검증에서 중단됐다. [개발 검사와 서버 관찰](../testing/document-qa-indexing-profile-fix-20261012.json).

## 수정 범위

[defaults_checked](../../src/apps/document_qa/indexing_results.py)는 필수 6개 식별/한도 필드에 표시용 문자열 3개만 선택적으로 허용한다. 알려지지 않은 필드, 누락된 식별자, 잘못된 profile/space/토큰/byte 한도는 계속 거부한다. 반환하는 색인 설정은 필수 필드만 포함하므로 [실제 Model GET gate](../../src/apps/document_qa/indexing_runtime.py)의 정확한 비교와 불변 binding/body/hash에도 표시용 문자열이 들어가지 않는다. Q02 설정 파일은 그대로 보존한다.

[새 회귀 검사](../../src/apps/document_qa/tests/test_indexing_defaults.py)는 실제 readiness configure의 임시 파일 저장/재로딩을 통해 gate를 확인한다. 별도 대역 검사는 기존 paused 세대의 receipt/키·CPU 완료를 유지하며 새 세대에서 index 생성/build/ready 조회/CAS 활성화까지 진행한다. 이런 검사는 실제 운영 색인/검색 인수를 대신하지 않는다.

## 현재 서버 관찰

| 대상 | 확인한 결과 |
|---|---|
| LAN 화면 | `192.168.0.119:17110/console` 렌더·인증 연결·기존 문서 1개 조회. 새 업로드/질문 POST는 미실행 |
| Embedding | `d4d3eda4-a857-4083-8828-e36e7fa0f534`, version `65c3f2ff-b1fd-4a39-8dd7-aaf34c34579e`, Snowflake profile 검증 |
| 답변 모델 | `14527d72-751a-459f-8615-7186e5faa0f3`, version `a6debd09-1bc7-4978-9208-8f6eb7935163`, 표시 이름 gemma3-v1/base-v1, 준비 후보 확인 |
| Q02 저장 | defaults_ready=true, blockers=[], configuration_written=true, GET 14회, 추론 요청 없음 |
| GPU | RTX 4090, 사용 14482/24564 MiB, utilization=0% — 사용자 조회 시점의 값 |
| 색인 Flow | generation=2, handoff=d06cbe53-8832-5015-b44c-b20e57a05f8e, execution=9, server_policy, attention_required/INDEXING_PROFILE_INVALID, steps=1 |
| 로컬 색인 | binding 없음, job not_requested, generation=0, active_document_count=0 |
| 질문 | head revision=0, 대상 없음, 전환/질문 0, question_ready=false |

관찰 시각은 사용자 메시지 접수 시각이며 실제 실행 시각을 재구성하지 않는다. 과거 GPU 보류/실패/오프라인 기록을 성공으로 덮어쓰지 않는다. 현재 코드의 정적 next_step/production_verified 값은 현재 진행 판단의 단독 근거가 아니다.

## 이 서버에서 적용 후 실행

원래 shshin 계정에서 패키지 checksum·적용·관련 검사를 완료한다. init/migrate/configure/Flow prepare나 기존 업로드·정제를 반복할 필요가 없다. 패치 자체는 서비스를 제어하거나 GPU/운영 API를 호출하지 않는다. 파일만 원복할 때는 apply_patch.py의 --rollback을 사용한다.

```bash
python3 -B agent_project_q05c_profile_fix_20261012/apply_patch.py \
  --project-root /home/shshin/workspace/agent_project --apply
cd /home/shshin/workspace/agent_project
python3 -B -m unittest discover \
  -s src/apps/document_qa/tests -p 'test_indexing*.py' -q
```

QA foreground 터미널에서 Ctrl+C로 종료한 뒤, 같은 shshin 계정으로 아래 명령을 실행하고 터미널을 유지한다. `indexing_inference_enabled=true`를 확인한다. 저장된 n8n callback 주소와 LAN 주소를 함께 유지한다.

```bash
cd /home/shshin/workspace/agent_project
/home/shshin/miniforge3/envs/sh_test/bin/python3 -B \
  -m src.apps.document_qa.api serve \
  --bind 172.25.0.1 --additional-bind 192.168.0.119 --port 17110 \
  --allow-indexing-inference
```

별도 터미널에서 기존 attention_required 세대를 한 번 재개한다. 이 관찰 직후에는 generation=3이 기대된다. 기존 세대 기록을 유지하며 새 실행권만 발급하고 기존 domain intent/receipt를 재사용한다. 실행 중·완료 상태에서는 resume을 반복하지 않는다.

```bash
cd /home/shshin/workspace/agent_project
/home/shshin/miniforge3/envs/sh_test/bin/python3 -B \
  -m src.apps.document_qa.indexing_flow resume \
  --job-id 430b0e7b-7b4c-4ae7-9c57-6c979ca94343
```

약 30초 뒤 로컬 status를 조회한다. 아직 running이면 이후에도 status로 관찰한다. 이 조회 자체는 운영 HTTP/추론 요청을 하지 않지만 실행 중인 n8n callback은 별도로 진행할 수 있다.

```bash
/home/shshin/miniforge3/envs/sh_test/bin/python3 -B \
  -m src.apps.document_qa.api status
```

Flow의 indexing_activated와 문서 indexing_active=true, 정확한 canonical/version 및 활성 readback을 확인한 뒤 검색을 검증한다. Tool/Agent 시험·발행과 질문 head CAS·질문 추론은 후속 단계이므로 이 단계의 question_ready=false는 정상이다. 추가 문서/답변·출처/의미 품질 인수는 별도로 진행한다. Ops 백업·재부팅·Kubernetes 보류는 유지한다.
