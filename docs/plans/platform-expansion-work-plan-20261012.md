# 플랫폼 확장과 Codex 작업계획

갱신: 2026-10-12. 사용자가 제안한 10개 확장을 아래 범위와 순서로 채택한다. 이 문서는 설계·개발 계획이며 설치·이전·운영 검증 완료를 뜻하지 않는다. 현재 상태는 [상태표](../status.md), 첫 작업과 실제 환경은 [인수인계](../HANDOFF_NEXT_SESSION.md), 매 작업의 규칙은 [AGENTS.md](../../AGENTS.md)를 따른다. 기존 [자동화·적응성·플랫폼 독립성](../design-principles.md)을 유지한다.

## 사용자 제안과 채택 범위

사용자 번호는 요구사항 식별자다. 구현 우선순위는 뒤의 실행 순서로 관리한다.

| 번호 | 제안 | 결정과 책임 | 첫 완료 기준 |
|---|---|---|---|
| 1 | 학습데이터 관리 Model → Data | 채택. Data가 원본·가공·검증·Dataset/Version·split·계보를 관리하고 Model은 학습 실행·checkpoint·평가 실행·모델 결과를 유지한다. 기존 데이터셋 계약은 점진 이전 | Data 불변 버전으로 새 학습 입력을 고정하고 이전 학습·요청 키·파일 hash·조회가 유지됨 |
| 2 | Data에 Airflow scheduling | 채택. Data가 일정·대상·기간·중복/재처리 정책을 소유하고 infra가 Airflow를 운영한다. DAG는 공식 API를 호출 | 하나의 정기 수집/정제 DAG, 시간대·일시중지·누락 구간·동일 실행 재처리 검증 |
| 3 | n8n·LightRAG·Neo4j·LiteLLM·Langfuse 확장 | 기존 n8n/Langfuse 재사용. LiteLLM은 외부 모델 경로와 함께 단계 도입. LightRAG/Neo4j는 Knowledge의 평가 실험으로 분리 | 기존 인프라 inventory/관측 유지. 각 신규 구성은 자기 목적·버전·자원·복구 기준을 검증 |
| 4 | 날짜 확인 등 기본 MCP Tool | 채택. Agent가 버전·스키마·실행·권한을 소유. 첫 Tool은 서버 기준 현재 날짜/시각/시간대 조회 | 유효/잘못된 IANA 시간대, 날짜 경계, 구조화 결과·MCP 조회/호출 검증 |
| 5 | RAFT·instruction tuning 등 학습 형식 선택 | 채택. Data의 불변 변환/생성 Job과 Model의 학습 입력 adapter를 분리. 의미가 다른 레시피는 단순 필드 변경으로 처리하지 않음 | instruction/messages 결정적 export부터 통과. RAFT는 질문·정답·근거·distractor와 생성/검증 근거를 가진 별도 버전 |
| 6 | HWP parser | 채택. Data parser 확장. HWPX와 HWP 5.x를 별도 지원 범위로 구현 | 본문·제목·표·읽기 순서와 위치를 canonical에 보존. 암호화/배포용/손상/미지원 버전 결과 명시 |
| 7 | Model에서 상용 API 사용 | 채택. Model이 provider·모델 신원·credential 참조·capability·호출 정책/사용량을 소유 | 한 제공자 chat을 기존 Agent API 경계로 연결. 인증·timeout·rate limit·usage·모델 신원 검증 |
| 8 | 대화 세션·기억·ChatGPT 참고 UI | 채택. Agent가 세션/turn/기억·실행 연결을 소유. 대화 이력과 장기 기억은 별도 기능 | 세션 목록/제목/재접속·중복 turn 방지부터, 이후 사용자가 확인·수정·삭제 가능한 명시 기억 |
| 9 | 문서 생성·수정·서식 채우기 Tool | 채택. Agent가 Tool/실행, Data가 문서·서식·결과 artifact/버전·계보를 소유 | DOCX 서식 한 종류의 필드 확인→채우기→렌더/검증→다운로드, 원본과 이전 결과 보존 |
| 10 | CI/CD | 채택. CPU/계약/화면 검증 CI를 먼저 구축하고 운영 CD는 단계별 인수 후 연결 | 실제 사용 가능한 runner에서 필수 검사를 실행. CD는 고정 image/artifact·staging·readback·복귀 검증 |

## 책임 이동과 유지할 경계

| 영역 | 목표 소유자 | 유지할 책임/호환성 |
|---|---|---|
| 학습 원본·정제·라벨·중복 제거·Dataset/Version·train/eval split·export | Data | 원본 보존, 내용 hash·크기·레코드 수·schema/recipe 버전·부모 계보·권한/보관 정책 |
| 학습 요청·하이퍼파라미터·tokenizer/chat template·GPU·checkpoint·평가 실행·모델/adapter | Model | Data 입력 pin을 저장하고 실행 전 무결성 확인. 기존 TrainingRun의 입력/결과는 변경하지 않음 |
| 청크·검색 색인·벡터/그래프·검색 평가 | Knowledge | Data canonical 사용. Model API로 embedding/관계 추출. 기존 embedding_space_id·버전·출처 계약 유지 |
| Tool·Agent·세션/turn·기억·문서 작업 실행 | Agent | 다른 플랫폼 공식 API 사용. 도구 정의와 데이터/문서 원본을 이중 원본으로 관리하지 않음 |
| Airflow·n8n·LiteLLM·Langfuse·선택한 DB 기동 | infra | 도메인 상태/자산의 소유권은 각 플랫폼에 남음. Ops는 공식 API·관측으로 조회 |

### 1. 학습데이터 이전

현재 Dataset/Version은 [Model Training DB와 model-datasets 버킷](../reference/datasets.md)에 있다. 문서 변경만으로 소유권이 이동하지 않는다. F1은 계약→Data 구현/가져오기→Model 읽기 연동→신규 생성 경로 전환 순서다. Data가 Model DB를 직접 읽거나 쓰지 않고 기존 소유자의 API/검토된 export를 통해 이전한다.

Model에는 `provider=data` 성격의 불변 입력 참조와 resolve adapter를 추가하는 방향으로 설계한다. 필드/API 이름은 F1a에서 기존 REST/gRPC·멱등 fingerprint와 대조해 확정한다. 참조에는 원본 플랫폼 신원·dataset/version ID·schema/format·URI 또는 다운로드 capability·hash/크기/레코드 수를 고정한다. 만료되는 다운로드 URL과 장기 논리적 신원은 분리하고 credential은 참조로 공급한다.

기존 source_type=version 요청과 이전 Model Dataset ID는 legacy namespace로 보존한다. 이미 수락한 학습/재개/평가와 요청 키 재현은 당시 pin을 사용한다. 새 Data ID와 이름이 같다는 이유로 이전 참조를 바꾸지 않는다. import mapping·수량·hash/내용 readback·부분 실패 재개·중복 import·혼재 기간의 생성 주체를 검증한 뒤 신규 경로를 전환한다. 이전 Model 메타데이터/객체의 삭제는 이 이전 단계의 기본 동작에 포함하지 않는다.

### 2. Airflow와 n8n

Airflow는 정기 수집·정제·학습데이터 준비와 누락 기간 재처리를 맡는다. n8n은 사용자 상위 업무 Flow와 이벤트 연결을 맡으며, 현재 문서 QA의 준비 Flow를 그대로 유지한다. 같은 파이프라인의 일정·재시도 소유자는 하나로 정한다. Airflow 재시도는 원래 Data 요청/기간 key를 조회·재사용하며 외부 작업을 매번 새로 만들지 않는다. 플랫폼 Operation/Dispatcher가 실제 Job 실행권과 완료 판정을 유지한다.

F2a에서 schedule_id·대상/설정 버전·IANA 시간대·data interval·동시 실행 한도·catchup/backfill·명시 중지·수동 실행 계약을 정한다. F2b에서 한 DAG만 연결한다. 기본 catchup은 꺼두고 과거 구간은 명시된 범위로 제한한다. DAG parsing 시 업무 API/추론을 호출하지 않는다. Data 화면에는 일정/최근 결과를 제공하고, Airflow 관리 UI는 고급 운영 경로로 둔다. Airflow metadata DB와 domain DB를 혼용하지 않는다.

### 3. 인프라와 그래프 검색

n8n과 Langfuse는 소스 구성과 과거 서버 인수가 있으므로 신규 설치 목록에 넣지 않는다. 현재 inventory를 조회하고 기존 ID·볼륨·인증 참조·trace 연결을 재사용한다. 새 LiteLLM/LightRAG/Neo4j는 도입 당시 공식 버전·자원·포트·credential·보관/복구·실제 API 호환성을 확인한다. 영구 공용 서비스 의존성은 `src/infra/`에서 관리하고 도메인 코드는 adapter를 둔다.

LightRAG는 Knowledge 안의 격리된 검색 adapter 실험으로 둔다. Data canonical을 입력으로 사용하고 Model 공식 추론 경로를 호출한다. Agent가 최종 답변을 생성하는 기존 경계와 출처 pin을 유지하며 LightRAG의 독립 업로드/생성 기능을 플랫폼 경계 밖의 새 기준으로 삼지 않는다. 동일 고정 질문셋에서 벡터 기준선과 관계 검색의 정확도·출처·지연·추출 비용·삭제/갱신을 비교한다. Neo4j는 선택 가능한 그래프 저장소다. LightRAG 도입만으로 Neo4j가 필수라는 결정을 하지 않는다. 효과가 없는 실험은 종료 근거를 남기고 기존 검색을 유지한다.

### 4. MCP 기본 도구

첫 `current_datetime` 성격의 도구는 입력 시간대와 서버 시각으로 ISO 8601 시각·UTC·날짜·요일·IANA 시간대·offset·조회 시각을 구조화해 반환하는 범위로 설계한다. 이름과 스키마는 기존 ToolVersion 계약에 맞춰 확정한다. 환경 기본 시간대는 설정으로 읽으며 사용자/tenant 재정의와 잘못된 시간대를 처리한다. LLM이 날짜를 추측하지 않게 한다.

Agent의 기존 MCP 목록/호출·Tool 시험/발행·timeout·실행 기록을 사용한다. 별도 임의 shell/파일 접근 Tool을 기본 목록으로 넣지 않는다. 결정적 계산·단위 변환 등 다음 도구는 대표 업무 요구가 생길 때 작은 단계로 추가한다. MCP 입력/구조화 출력 schema와 client/runtime 버전 호환을 실제 지원 버전으로 검증한다.

### 5. 학습 형식과 RAFT

파일 형식(JSONL 등), 레코드 schema(messages/instruction 등), 학습 레시피(SFT/RAFT 등), 모델별 tokenizer/chat template를 별도 필드/단계로 관리한다. 단순 변환은 원본에서 새 불변 artifact로 만들고 원본을 덮어쓰지 않는다. instruction/input/output → messages의 유실 없는 변환을 먼저 구현하며, 필수 정보가 부족하면 필요한 필드를 표시한다.

여기서 RAFT는 Retrieval-Augmented Fine-Tuning이다. 질문·정답만을 RAFT로 이름 변경하지 않는다. 근거 문서와 distractor 선택, 답변/인용 검증, 생성 모델/prompt·recipe 버전·seed·사용량·원본 계보가 필요하다. 합성 생성은 Model API를 사용하고 구조/근거 검증과 표본 검토를 거친 버전만 ready로 등록한다. 문서 단위 분리와 train/eval 중복 검사로 평가 누출을 막는다. RAFT의 근거 기반 답변과 SFT용 순수 답변을 별도 export로 관리한다. 사용자 화면에 내부 추론문 생성을 필수 필드로 요구하지 않는다.

현재 Model Job은 제한된 chat_messages JSONL을 처리한다. 새 레시피를 선택했다고 미지원 Training adapter를 조용히 사용하거나 학습 성공으로 표시하지 않는다. Data schema 검증과 Model tokenizer/학습 호환 검증·품질 평가를 별도로 인수한다.

### 6. HWP/HWPX

`.hwpx`는 ZIP/XML, `.hwp`는 바이너리 계열이므로 각각 검증한다. F6a는 HWPX의 실제 content spine 읽기 순서·제목/문단·표/셀·이미지 참조를 canonical에 연결한다. F6b는 HWP 5.x parser adapter와 대표 문서로 확장한다. HWP 97 등 다른 버전은 지원 범위를 별도로 표시한다. 공개 사양과 선택한 parser의 라이선스/필수 고지를 구현 시 확인한다.

CPU parser의 byte/압축 해제/블록·시간 한도, 매크로/스크립트·외부 링크 비실행, 손상/암호화/배포용 문서 판정, 텍스트와 표 읽기 순서·원본 계보를 유지한다. 추출 못한 그림/텍스트는 OCR 필요 상태로 남기고 허위 페이지 번호를 만들지 않는다. HWP 읽기 지원과 HWP 문서 생성/편집 지원은 서로 다른 인수 범위다.

### 7. 상용 API와 LiteLLM

Model에 local runtime과 external provider를 구분하는 추론 backend 계약을 추가한다. provider/model/deployment 신원·지원 capability·context/output limit·credential 참조·명시 routing 정책·사용량/비용 근거를 소유한다. 외부 API 연결을 GPU Deployment ready나 로컬 모델 artifact로 가장하지 않는다. Agent/Knowledge/Data는 계속 Model 공식 API를 사용한다.

한 provider의 chat부터 adapter와 fake-provider 계약 검사를 통과시킨다. 실제 제공자·계정·credential·호출 한도가 정해진 뒤 작은 live 호출을 인수한다. 문서 원문/질문의 외부 전송 범위와 제공자의 저장 정책을 설정에 명시한다. credential 값을 UI/log/git/문서에 복사하지 않는다. 기존에 허용된 credential 참조는 재사용한다.

LiteLLM은 provider protocol·선택한 routing/usage 정책의 구현 후보다. SDK adapter와 Proxy를 중복 호출 경로로 만들지 않고 F7a에서 선택한다. timeout/retry/fallback 담당 계층을 하나로 정하고 재호출이 추가 비용을 낼 수 있음을 사용량에 반영한다. 다른 chat 모델로의 fallback은 설정·실행 기록에 표시한다. 다른 embedding 공간으로의 자동 fallback은 허용하지 않는다. 외부 streaming·tool calling·embedding/rerank는 chat 완료와 별도 capability 인수다.

### 8. 세션과 기억

첫 범위는 Agent의 세션 목록/제목·turn 저장·질문/AgentRun 연결·새 대화·재접속과 단일 대화 UI다. turn별 요청 키와 target/index/Tool/Agent/model pin을 보존한다. 새로운 turn의 자료 선택 정책은 명시하고, 과거 답변의 근거를 현재 자료로 교체하지 않는다. history를 모델 입력으로 구성할 때 token budget과 포함/제외·요약 버전을 기록한다. 사용자·세션 권한을 데이터 모델부터 분리한다.

장기 기억은 대화 로그와 별도 자산으로 둔다. 초기에는 사용자가 명시 저장한 기억만 사용하고 목록·수정·삭제·적용 범위·동의/보관·출처를 제공한다. 자동 추론 기억은 평가 후 별도 기능이다. 세션 내용이 자동 학습데이터가 되거나 다른 사용자 기억으로 넘어가지 않는다. UI는 ChatGPT의 대화 목록·제목·새 대화·입력/응답 흐름을 참고하되 자체 지원 기능과 실제 상태만 표시한다.

### 9. 문서 Tool

Agent는 문서 Tool 정의/버전·요청 intent와 실행 결과를 관리한다. Data는 업로드 원본·template/version·필드 schema·생성 artifact/version·계보/다운로드를 관리한다. 임의 host 파일 경로 대신 접근 권한이 있는 document/template ID를 사용한다.

F9a는 DOCX template의 명시 placeholder/필드 schema를 읽고 값 검증·미입력 안내→내용 채우기→새 버전 저장→렌더/레이아웃·내용/필드 검사→다운로드까지 구현한다. 표·스타일·한글·다중 run placeholder를 대표 사례로 확인한다. F9b는 expected document revision으로 수정해 원본/이전 결과를 보존하고 변경 내역을 표시한다. PDF 양식 채우기와 일반 PDF 재편집·HWP 서식은 다음 형식별 adapter로 분리한다. 임의 파일도 완벽한 서식 보존을 약속하지 않는다.

### 10. CI/CD

F10a는 현재 저장소/remote/runner부터 확인하고 공통 검사 entry point를 만든다. 실제 remote가 GitHub이면 Actions, 다른 제공자이면 해당 runner로 연결하며 CI 제공자를 문서만으로 단정하지 않는다. secret/state/원본 대용량 자료는 소스 업로드 대상에서 제외하고 credential은 기존 환경의 참조로 공급한다. CPU 계약·Python·Go·지원 Node의 실제 JS 검사를 필수 lane으로 두고 DB 통합·GPU·live-provider는 격리된 별도 lane으로 둔다.

개발 서버의 Node 12 skip을 필수 화면 검증 통과로 인정하지 않는다. CI 화면 lane은 지원 Node/기능을 준비하고 실제 검사를 실행한다. 실행하지 않은 lane은 skipped/not_run으로 표시하며 일반 PR CI가 운영 GPU inference·모델 lifecycle·서비스 재시작을 수행하지 않는다. 이미 실패한 GPU/공급자 실험의 반복 호출도 자동 CI에 넣지 않는다.

F10b는 immutable source/image digest·migration 호환성·staging 인수·health/readback·배포/복귀 절차를 갖춘 뒤 운영 CD를 연결한다. 자동 실행 범위는 기록된 배포 환경·서비스·credential·검증 기준으로 제한한다. UI health만으로 데이터/업무 호환을 통과 처리하지 않는다. 데이터/credential 삭제와 새 유료 계정/예산·배포 목적지 결정은 기본 CD 작업이 아니다.

## 실행 순서와 단계별 완료선

한 행 전체를 한 번에 크게 구현하지 않고 표시된 첫 작은 단위부터 시작한다. 각 단위의 코드·관련 검사·필요한 실제 인수·문서 기록을 마치면 다음 단위로 이동한다. 이번 사용자의 문서/계획 채택은 현재 서버에서 모든 후보를 즉시 기동하는 명령이 아니다.

| 순서/ID | 작은 첫 작업 | 선행 조건과 단계 완료선 |
|---|---|---|
| 0 / F0a | 현재 첫 문서 인용 누락 해결 | 실패 두 Run과 head1 보존. 코드/담당 부서 질문·정확한 근거·인용, 근거 없는 질문 확인. 모델 교체는 비교 근거로 판단 |
| 1 / F10a | CPU/화면 CI 기준선 | actual repo/runner 확인, 기존 테스트 entry point, 지원 Node에서 화면 검사 실행. Git 연결이 없으면 portable runner부터 준비 |
| 2 / F0b | 문서 QA 첫 제품 인수 | Q05c-2 두 문서 누적·Q09 변경/복구·Q10 품질·Q11 한 화면 사용. 새 자료 준비→색인→Tool/Agent→head 연결의 자동화 공백도 해결 |
| 3 / F1a→F1b→F1c | Dataset 소유권 이전 | 호환 계약/fixture→Data catalog/import/readback→Model Data resolve. 기존 Run/key/legacy version 보존과 신규 경로 인수 |
| 4 / F5a→F5b | instruction export, 이후 RAFT | Data 불변 버전 선행. 단순 변환/불충분 입력→생성/근거 검증/누출 없는 평가·Training adapter |
| 5 / F2a→F2b | 일정 계약, Airflow 한 DAG | 안정된 Data Job API 선행. 일정·기간 key→조회/중복/중지/backfill→선택 infra 구성/live DAG |
| 6 / F6a→F6b | HWPX, 이후 HWP 5.x | Data canonical·fixture 기반. 기존 TXT/Markdown/PDF 회귀와 각 지원 형식 readback |
| 7 / F7a→F7b·F3a | provider 계약과 첫 API, LiteLLM | 모델 선택/credential/한도 확정. fake provider→첫 live chat→필요한 Proxy/routing, 각 호출 경계 검증 |
| 8 / F4a | 현재 날짜/시간 MCP Tool | Agent 기존 Tool/MCP 버전 경로 활용. 날짜 경계·시간대·구조화 결과·시험/발행 |
| 9 / F8a→F8b | 대화 세션, 이후 명시 기억 | turn 멱등·권한·자료 pin·token budget 먼저, 이후 기억 관리/삭제/격리 |
| 10 / F9a→F9b | DOCX 서식 채우기, 이후 수정 | Data artifact/version·Agent Tool 계약, 렌더 인수·미입력·충돌·원본 보존 |
| 11 / F3b→F3c | LightRAG 비교, 필요 시 Neo4j | 관계 검색용 고정 평가 자료·Model 호출 준비. 이득·출처·비용·삭제 정책 검증 후 채택/종료 |
| 12 / F10b | staging에서 검증한 CD | 위 단계마다 CI를 확대. 환경/서비스·image pin·migration/readback·복귀를 검증하고 배포 automation 범위 기록 |

## Codex가 이어서 수행할 방법

시작 시 실제 repository와 runtime을 읽기 전용으로 대조한다. [AGENTS.md](../../AGENTS.md)→[인수인계](../HANDOFF_NEXT_SESSION.md)→[상태표](../status.md)→이 실행 순서에서 첫 미완료 단위를 선택한다. F0a가 현재 첫 작업이다. 완료/미완료/실제 서버 확인/대역 검사를 분리하고, 한 단계의 성공으로 전체 확장 완료를 선언하지 않는다.

계획 채택 후 사용자가 실행 중인 Codex에 연속 작업을 요청하면, 명시된 범위에서 코드·문서·테스트·격리 검증을 끝내고 다음 의존성 만족 단위로 진행한다. 매번 계속 여부를 묻지 않는다. 실제 서버 인수가 필요한데 해당 서버/API에 접근할 수 없으면 실패 원인·검증 가능한 코드/테스트·정확한 명령을 남기고 독립적인 단위를 진행한다. 인수가 없으면 해당 의존 단계 완료를 건너뛰지 않는다. 미정 provider/예산/템플릿/대표 HWP 자료 등은 기존 설정에서 먼저 확인하고 꼭 필요한 값만 요청한다.

`AGENTS.md`와 계획 파일은 실행 지침이다. 파일 적용만으로 상시 Codex 실행이나 24시간 worker가 만들어지지는 않는다. 기존 Ops 보류인 운영 백업/복원·재부팅·Kubernetes는 이 확장 채택으로 해제되지 않는다. 이 계획은 ZenML/DDP·외부 알림·범용 자율 그래프를 추가로 승인하는 문서도 아니다.

## 공식 근거와 구현 시 확인할 내용

조회일: 2026-10-12. 제품의 기능 설명과 위 프로젝트의 소유권/도입 순서는 구분한다. 버전은 도입 시 고정하며 최신 문서 URL은 현재 설치 버전의 증거가 아니다.

- [Airflow 개요](https://airflow.apache.org/docs/apache-airflow/stable/index.html): 일정·의존성·구간 재처리 기반 배치. Data/n8n 역할 분리는 프로젝트 설계 결정.
- [RAFT 원 연구](https://sky.cs.berkeley.edu/project/raft/): 도메인 문서와 distractor를 사용하는 학습 레시피. 구현은 [원 논문](https://arxiv.org/abs/2403.10131)과 선택한 adapter를 함께 확인.
- [LightRAG 공식 README](https://github.com/HKUDS/LightRAG/blob/main/README.md): 그래프/벡터와 여러 storage backend. Neo4j는 선택이며 실제 이득은 자체 평가로 판단.
- [MCP Tool 사양](https://modelcontextprotocol.io/specification/2025-06-18/server/tools): inputSchema·구조화 결과·선택 outputSchema. 실제 제공하는 MCP 버전에 맞춰 호환 검증.
- [한컴 HWP/OWPML 공개 사양](https://www.store.hancom.com/support/downloadCenter/hwpOwpml), [HWPX 구조](https://tech.hancom.com/hwpxformat/): 바이너리 HWP와 ZIP/XML HWPX의 구분·읽기 순서·공개 사양 고지 확인.
- [LiteLLM routing](https://docs.litellm.ai/docs/routing), [Langfuse 관측](https://langfuse.com/docs/observability/overview): 추론 경로와 trace 기능. 기존 platform identity/출처/호환성은 자체 계약을 유지.
- [CI/CD workflow 예시](https://docs.github.com/en/actions/get-started/understand-github-actions): 실제 repository 제공자에 맞춰 선택.
- [Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md): 프로젝트 지침 자동 발견. 별도 실행 세션/환경은 실제 설정 필요.
