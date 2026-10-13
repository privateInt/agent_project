# 모델 플랫폼 변경 이력 — 2026년 9월

과거 변경의 이유·검증 범위만 요약했다. 현재 구현·대기는 [프로젝트 상태](../status.md)와 [Data 상태](../data-platform/status.md)를 따른다. 결과 원본은 요약 후 삭제했으며, 과거 실행 식별자는 현재 자원 존재를 보장하지 않는다.

## 2026-09-08-01-platform

**2026-09-08 — 모델 학습과 제어 경로 안정화**

Gemma 3 1B 단일 GPU QLoRA 1 step → MLflow → MinIO 검증 → ready adapter 등록을 확인했다. Operation SQL 타입 오류를 수정하고 Outbox·Dispatcher·Reconciler와 실제 CPU 제어 경로를 검증했다. 실제 Dispatcher SIGKILL 후 자연 lease 만료·재발행·generation 2 실행·이전 token 거절까지 통과했다. 기능 smoke이며 학습 품질 검증은 아니다.

## 2026-09-08-02-refactor

**2026-09-08 — 문서 통합과 Operation 리팩터링**

흩어진 문서를 상태·계약·검증·운영으로 통합하고 Operation schema·lifecycle·repository 책임을 분리했다. 공통 SELECT를 통합하고 실행 전 lease 복구 설명을 맞췄다. 서버 교체·회귀 검증을 완료했다. 당시 일회성 삭제 목록은 현재 문서 정리에서 제거했다.

## 2026-09-08-03-build-layout

**2026-09-08 — Compose·빌드·런타임 분리**

서비스별 Dockerfile/Compose와 상위 include·Bake 구조로 전환했다. 공통 Go 빌드는 재사용하되 이미지·컴파일 범위를 분리했다. 서버 Compose/Bake, Python 10개, Go/DB 45개·하위 5개, 상시 7개/Job 3개 빌드와 CPU 제어 smoke를 확인했다.

## 2026-09-08-04-job-gc

**2026-09-08 / 04 — Job 로그 보존과 컨테이너 GC**

종료한 플랫폼 소유 Job의 로그를 MinIO에 검증 보관하고 보존 기간 뒤 컨테이너를 정리하는 GC를 추가했다. Docker/MinIO 격리 검사와 실제 Job 2개의 archived_retained 결과를 확인했다. 상시 모드는 dry-run을 유지했으며 archive 검증이 자동 삭제 활성화를 뜻하지 않는다.

## 2026-09-09-01-request-creation

**2026-09-09 — 생성 요청 멱등성과 전달 복구**

응답 유실과 도메인 저장/Operation 전달 실패를 분리하기 위해 멱등 키·도메인 전달 대기·재전송을 구현했다. 서버 REST/gRPC/DB·이미지 전환 후 delivered → 시작 승인 → succeeded, Outbox 발행과 Reconciler 보정을 확인했다. 기존 고아 리소스 backfill, 키 정리, tenant 범위는 별도다.

## 2026-09-09-02-vllm-serving

**2026-09-09 / 02 — 단일 GPU snapshot 서빙**

CPU demo와 구분한 실제 vLLM 배포·지속 런타임·ready·채팅·중지 경로를 추가했다. 서버 Go/DB 77개·하위 62개와 Gemma snapshot 단일 GPU 추론을 검증했다. 배포 준비 Operation의 성공은 런타임 중지 후에도 유지하며 과거 endpoint는 현재 실행 주소가 아니다.

## 2026-09-09-03-adapter-serving

**2026-09-09 / 03 — 학습 adapter 서빙 구현**

학습 결과를 base snapshot과 연결해 vLLM adapter로 제공했다. tokenizer·embedding·manifest를 검증하고 원본을 유지한 서빙용 사본을 사용한다. 초기 호환·응답 문제 수정 후 adapter alias로 추론·멱등 재전송·중지까지 통과했다. 1 step 결과의 기능 확인이며 품질 향상·일반 모델 호환성은 별도다.

## 2026-09-09-04-dataset-versioning

**2026-09-09 / 04 — 데이터셋 버전 등록과 학습 입력 고정**

단일 chat_messages JSONL을 불변 버전으로 등록하고 학습 입력을 ID·URI·SHA-256·크기로 고정했다. 등록·다운로드·재전송·충돌 검사, 실제 단일 GPU 학습과 새 adapter 추론을 확인했다. 최초 구현의 최대 입력은 64 MiB이며 API 불변성은 Object Lock과 다르다. train/eval 분리는 이후 평가 항목에서 추가됐다.

## 2026-09-09-05-local-ddp

**2026-09-09 — 로컬 DDP 학습과 GPU 점유 관리**

동일 호스트의 torchrun DDP와 학습/서빙 간 GPU 예약을 구현했다. 서버 Go/DB 89개·하위 70개 통과 후 단일 GPU 회귀는 GPU 대기 수정 항목에서 완료했다. 실제 GPU 두 개의 NCCL/DDP 학습은 미검증이며 FSDP·DeepSpeed·다중 호스트는 범위 밖이다.

## 2026-09-09-06-gpu-wait

**2026-09-09 — GPU 대기 처리와 미시작 요청 복구**

GPU_BUSY 때 미시작 작업이 dispatching에 남던 경로를 waiting_resources와 제한된 재전달로 보완했다. 외부 ContextRAG 점유를 별도로 정리한 뒤 같은 요청의 단일 GPU 학습·adapter 등록·vLLM 추론까지 통과했다. 중간 RUNTIME_CREATE_UNCONFIRMED는 후속 ready로 해소됐지만 상세 원인은 로그만으로 특정하지 않았다.

## 2026-09-09-07-remote-nodes

**2026-09-09 / 07 — 명시적 원격 노드 학습 기반**

정적 노드·SSH worker·Docker daemon 식별자를 사용하는 명시적 원격 학습 경로를 구현했다. 중앙 서비스 Go/DB 100개·하위 41개를 통과했으며 SSH 단위 검사는 대역이다. simsys4070의 실제 원격 GPU 학습·원격 서빙/GC·arm64 실행은 확인하지 않았다.

## 2026-09-09-08-single-node-finish

**단일 서버 완성 우선과 확장 보류**

사용자 결정으로 Docker 단일 서버 완성을 우선하고 Kubernetes와 실제 원격 환경 구성·GPU 검증을 보류했다. 중앙의 원격 실행 코드는 유지하며 자동 스케줄링이나 Kubernetes 적용을 추가하지 않았다. 이 보류는 기능 성공 판정이 아니다.

## 2026-09-09-09-catalog-api

**목록·검색 API와 Streamlit 목표 반영**

모델·버전·학습·배포·Operation 목록, 검색·필터·커서 페이지네이션을 추가했다. Python 시각 파싱 오류를 보완한 후 서버 Go/DB와 실제 읽기 전용 REST smoke가 통과했다. 이 검사는 새 Job이나 GPU 실행을 만들지 않았다.

## 2026-09-10-01-training-control

**학습 취소와 로그 조회**

대기·실행 중 학습 취소와 현재/보관 로그 조회를 구현했다. 과거 Job의 로그 503 호환 문제를 수정하고 실제 대기 취소·GPU Job 취소·로그 조회를 확인했다. 별도 격리 CPU/MinIO의 TestLiveTrainingLogArchiveFallback도 통과했다. 취소 성공을 학습 step/품질 완료로 해석하지 않는다.

## 2026-09-10-02-training-output

**완료 출력 보존·업로드 전용 복구**

학습은 끝났지만 업로드가 실패한 경우 로컬 완료 출력을 보존하고 학습 없이 업로드만 재시도하도록 했다. 실패 주입 → saved 출력 확인 → 업로드 복구 → ready adapter → 실제 서빙을 통과했다. 실제 디스크 부족 재현과 선택적 output/release의 실환경 검증은 별도다.

## 2026-09-10-03-training-checkpoints

**2026-09-10 / 03 — Checkpoint 보존·새 학습으로 재개**

완료 checkpoint의 설정·입력·파일을 검증하고 실패/취소한 작업으로부터 새 TrainingRun을 생성하도록 했다. synthetic 4 step 중 2 step 이후 실패/취소 → 남은 step 학습 → 새 adapter 등록, 취소 재개 adapter 추론을 확인했다. 장시간 품질, 등록 데이터셋 재개의 실제 GPU 검사, 원격 복제·이동·TTL은 별도다.

## 2026-09-10-04-training-evaluation

**2026-09-10 — 학습·평가 데이터 분리와 결과 비교**

train/eval 버전을 분리하고 loss/perplexity·MLflow·실행 비교를 연결했다. 초기 MLflow 실행 ID 문제 수정 후 두 실제 GPU 실행과 comparable 비교, 평가된 adapter의 vLLM/Gateway 추론을 확인했다. 자동 분할·base 사전 평가·best checkpoint·생성 답변 채점은 구현 완료 범위가 아니다.

## 2026-09-10-05-streamlit-playground

**2026-09-10 — Streamlit 모델 체험 페이지**

모델 검색·선택·대화와 상태/오류 표시를 갖춘 Streamlit을 추가했다. 이미지 내부 소스 권한과 AppTest 실행 명령을 보완한 뒤 서버 AppTest·기동, Gateway 경유 실제 GPU 추론·중지를 확인했다. 이후 화면은 관리/채팅 구조로 발전했다.

## 2026-09-10-06-model-console

**2026-09-10 — 버튼으로 사용하는 모델 관리·학습·배포 콘솔**

모델·데이터셋 추가, 학습·배포·취소·복구를 브라우저의 명시적 동작으로 연결했다. 폼 값과 멱등 키로 응답 유실을 복구하며 조회·페이지 이동으로 새 작업을 만들지 않는다. 사용자 서버 검사·기동을 확인했고 UI 구조 변경은 후속 항목에 기록했다.

## 2026-09-10-07-model-origin-ux

**2026-09-10 — 모델 원본 표시와 쉬운 기본 설정**

관리 이름과 HF 저장소/revision 원본을 구분하고 상세 정보를 정리했다. UI의 예제 값을 실제 등록 모델처럼 표시하지 않으며 선택한 버전과 실행 대상 고정을 유지했다. 로컬 UI 검사 후 최종 서버 35개 AppTest에 통합됐다.

## 2026-09-10-08-unified-add

**2026-09-10 — 모델·데이터셋 추가 폼 통합**

모델·데이터셋 추가 폼을 통합하고 실제 입력에 따라 요청을 생성했다. 폼 전환·취소 시 불필요한 POST를 막고 멱등 요청·파일 보존 규칙을 유지했다. 최종 관리/채팅 UI 검사 범위에 포함됐다.

## 2026-09-10-09-model-row-actions

**2026-09-10 — 모델·배포 목록에서 바로 실행**

모델/배포 행에서 조회·학습·배포·종료를 시작하도록 했다. 모델 목록 삭제는 soft delete이며 파일·기존 작업 이력을 물리 삭제하지 않는다. 관련 서버 Go/DB는 운영 마무리의 9개 검사로, UI는 최종 35개 AppTest로 확인했다.

## 2026-09-10-10-table-ui

**2026-09-10 — 이미지 시안 기준 표 UI 재구성**

목록을 표 중심으로 정리하고 원본·revision·상세를 분리했다. 검색 조건과 페이지 이동, 버전 선택·실행 대상 고정을 유지하며 고정 CSS와 Streamlit 위젯을 사용했다. 로컬 AppTest 29개 후 최종 서버 35개 검사에 포함됐고 실제 브라우저 시각 확인과는 구분한다.

## 2026-09-10-11-two-page-progress

**2026-09-10 — 관리·채팅 통합, 추가 팝업과 실제 전송량**

관리를 모델·배포·데이터셋·학습 목록으로 모으고 채팅을 별도 페이지로 구성했다. import의 실제 파일 바이트·저장 완료 수와 상태 자동 조회를 추가했다. 크기 미상은 퍼센트를 추정하지 않으며 이전 Job에 수치를 소급하지 않는다. 로컬 UI 32개 및 후속 서버 Go/DB 검사를 통과했으나 실측 다운로드의 브라우저 관찰은 다음 사용 시 확인한다.

## 2026-09-10-12-detail-dialogs

**관리 화면 하단의 선택 상세를 팝업으로 이동**

하단에 남던 선택 상세를 필요할 때 여는 팝업으로 이동했다. 닫힌 상세의 주기 조회를 멈추되 선택·필터·응답 유실 기록은 보존했다. 2026-09-10 23:26 UTC 실제 실행 이미지에서 AppTest 35개가 4.890초에 통과했다. API/파일 위젯은 대역이며 GPU 작업·시각 확인을 추가한 검사는 아니다.

## 2026-09-10-13-operations-closeout

**단일 서버 운영 마무리와 후속 개발 문서**

읽기 전용 audit, 일관 백업·SHA 검증·격리 복원, 서비스 재기동 비교, 개별 캐시 정리 도구를 추가했다. 운영 18개·재기동 비교 4개 단위 검사를 통과했다. 실제 데이터 백업·복원은 다음 사용자 결정으로 이번 완료 기준에서 제외했고 검증 완료로 표시하지 않는다.

## 2026-09-10-14-source-preservation

**테스트 데이터 백업 제외와 소스 보존**

사용자는 당시 데이터가 테스트용이므로 백업을 생략하고 소스만 보존한 뒤 초기화하기로 했다. 최초 audit은 여유 공간 기준에서 실패했고 이후 초기화 항목에서 해소됐다. 당시 소스 전용 보관본은 실제 환경/키를 제외했으나, 2026-09-16 사용자가 업로드한 전체 프로젝트는 그 범위와 다르다.

## 2026-09-10-15-test-data-reset

**테스트 데이터 초기화와 마무리 검증 도구**

소유권을 확인한 prepare → apply → verify_reset으로 테스트 데이터만 초기화했다. Management의 잔여 operations 테이블을 선택적으로 허용하고 계획 이후 변경을 거절하도록 보완했다. 서버 단위 31개·Go/DB 9개, 실제 삭제·9개 서비스 기동/재기동·공간 검사와 UI 35개를 확인해 당시 모델 v1 검증을 마쳤다. 당시 MLflow 16개 실행은 soft delete, 공유 인프라·소스·설정은 유지했다. 이 기록은 현재 데이터 삭제 지시나 빈 상태 보장이 아니다.

## 2026-09-16-01-chat-model-filter

OCR도 vLLM 엔진을 사용해 채팅 후보에 섞이던 조건을 수정했다. UI의 ready/running 검사에 일반 채팅 프로필과 model_type=llm 확인을 추가하고 Surya/알 수 없는 용도는 제외한다. 관리 목록에서는 OCR을 유지하며 OCR 채팅 직접 링크도 입력/예제/재전송을 비활성화한다. 모델 기본/adapter LLM 선택과 대화 분리 동작은 유지한다.

상태/API 검사 24개·Streamlit AppTest 38개, 62개 로컬 통과. 목록 필터·관리 유지·직접 링크·모델 종류 검사를 추가했다. 서버/GPU 추론 검증으로 확대 해석하지 않는다. 데이터 화면의 OCR 조회/선택은 별도 Data 이력을 따른다.

## 2026-09-18-document-consistency

문서의 GPU 단독 사용 설명을 현재 코드와 맞췄다. 학습 Job은 점유 충돌 시 대기하지만 서빙 create/start는 Shared=true로 로딩을 시도한다. VRAM 보장·선점 기능은 아니며 GPU_OUT_OF_MEMORY 등 실패 분류와 자동 재시작 금지를 유지한다. 실제 동시 GPU 로딩/OOM의 추가 서버 검증을 했다는 뜻은 아니다.

저장 정책에 목록 숨김(DELETE)과 모델 파일 영구 삭제(purge)를 구분했고, 모델/데이터셋·학습 작업 파일의 삭제 범위를 분리했다. Compose 기본 GC apply·성공 1시간/실패·취소 7일을 반영해 과거 dry-run·24시간 문구를 바로잡았다. 신규 설치용 dry-run 검증 절차는 유지한다. [GPU 계약](../reference/multi-gpu.md), [서빙](../reference/serving.md), [저장 정책](../operations/storage.md)이 현재 기준이다. 이번 변경은 문서만 대상이며 서비스·GC 설정이나 서버 데이터는 수정하지 않았다.

## 2026-09-18 — 관리 이름·목록 진행률·배포 삭제

사용자 요청으로 Knowledge 다음 개발 전에 Model 관리를 보완했다. ID·파일을 유지하는 CAS 라벨 변경, 기존 Operation 기반 행 진행률, 중지된 배포의 컨테이너/전용 cache 삭제와 tombstone을 추가했다. Python 30개 통과, UI 42개는 환경 부재 SKIP, Go/DB·Docker 실행은 서버 대기다. [동작 계약](../reference/model-management-update.md), [검증 안내](../testing/model-management-update.md)를 따른다.

## 2026-09-18 — 배포 삭제 실제 기동 구성 보완

최초 관리 수정은 사용자 서버에서 Go/DB 5개 package, Python 25+5+42개와 서비스 교체를 통과했다. 중지된 배포 삭제의 Unavailable 보고를 추적해 main.go의 Prepare-only FetchFunc가 Purge를 노출하지 않는 문제를 확인했다. production fetcher에 두 기능을 유지하고 MinIO 없이 cache만 지우도록 수정했다. 실제 기동 factory 회귀 검사 2개와 deployment build test gate를 추가했다. 후속 Go/빌드·실제 삭제 성공은 아직 확인하지 않았다. [최소 적용 절차](../testing/model-management-update.md#배포-삭제-unavailable-후속-수정)를 따른다.

## 2026-09-18 — 삭제 성공·GC 확인과 LiteLLM 검토 기록

사용자가 fetcher 후속 수정 후 “이제 삭제 된다”고 확인했다. 새로운 개별 Go/빌드 로그나 cache 파일시스템 검증 결과를 받은 것은 아니므로 기존 검사 로그와 사용자 기능 확인을 구분한다. GC 로그는 apply·성공 1시간/실패 168시간, 32회 cycle errors=0, 성공 Job 두 개 removed를 확인했다. 남은 두 deployment Job은 failed/archived_retained로 7일 보존 중이며 정책 변경은 필요하지 않았다.

기존 snowflake 배포의 `/v1/embeddings` 직접 요청에서 입력 2개 각각 1,024차원·유한값·L2 norm 1.0 출력이 확인됐다. 공식 Model 임베딩 API·profile/공간 identity·용도 표시는 아직 미구현이다. [검증 근거](../testing/embedding.md#runtime-check).

[LiteLLM 도입 검토](../reference/litellm-integration-review.md)에 이득·운영 부담·현재 호출 경로·등록 동기화 소유권·임베딩 호환성·도입 조건을 기록했다. 이번 작업은 문서와 검증 요약만 변경했다. LiteLLM은 미통합 상태를 유지하고 다음 대화에서 Knowledge를 재개한다.

## 2026-09-18 — K03 기존 임베딩 배포 재사용

별도 GPU runtime 생성 없이 배포 ID 기반 Model EmbeddingService gRPC와 REST profile/embeddings를 추가했다. 고정 artifact·tokenizer·전처리·pooling·정규화·차원·runtime image 기반 공간 ID, query/document 역할, 실제 토큰 한도·부분 응답 검증을 구현했다. 지원하는 신규 profile 배포, legacy 임베딩의 채팅 거절, 화면 용도·명시적 임베딩 확인을 연결했다.

Python 34개와 문법 검사 통과. Go·Docker·Streamlit 부재로 빌드/Go/AppTest 및 서버 공식 API 확인은 대기다. 과거 직접 호출 성공과 새 코드 통과를 혼동하지 않는다. [현재 계약](../reference/embedding.md), [적용·검사](../testing/embedding.md).

## 2026-09-18 — K03 사용자 서버 확인

패치 53개, Deployment/Gateway Go 검사·3개 이미지 빌드, Python/UI 78개, 기존 runtime에서 query 1·document 2개 각 1,024차원·유한값·L2 norm 1.0, 공간 불일치·길이 초과 거절을 사용자 로그로 확인했다. runtime 재배포는 없었다. 등록 model_type=llm과 capability=embedding을 구분한다. DB 통합·신규 profile GPU 배포는 이 로그의 확인 범위가 아니다.
