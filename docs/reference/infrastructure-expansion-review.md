# 인프라 확장 검토와 도입 조건

최초 기록: 2026-09-22. 갱신: 2026-10-12. 현재 구현/과거 서버 인수와 향후 도입을 구분한다. 새 10개 확장의 책임·진행 순서·완료선은 [확장 계획](../plans/platform-expansion-work-plan-20261012.md), 실제 상태는 [상태표](../status.md)에만 둔다. 이 문서는 제품의 역할·중복·선택 조건을 관리한다.

## 현재 구성과 채택 범위

[infra Compose](../../src/infra/docker-compose.yml)에는 PostgreSQL·MinIO·Redis·Kafka·OpenSearch·MLflow·n8n 구성이 있다. 별도 [Langfuse 구성](../../src/infra/langfuse/docker-compose.yml)과 [관측 구성](../../src/infra/observability/docker-compose.yml)도 소스에 있고 O01~O04/Agent trace·Ops 연결의 과거 서버 인수 근거가 있다. 따라서 n8n/Langfuse를 미설치로 안내하거나 같은 환경에 새 DB/볼륨을 만들어 다시 설치하지 않는다. 현재 실행 상태/실제 버전은 도입 작업 전에 기존 inventory/공식 API로 확인한다.

| 제품 | 역할과 판단 | 도입/확장 조건 |
|---|---|---|
| n8n | 기존 상위 업무 Flow. Agent 연동/문서 QA 준비에 사용 | 기존 workflow ID·게시 버전·receipt·볼륨 유지. 같은 일정의 Airflow/n8n 이중 실행 방지 |
| LangGraph | 기존 Agent 내부 고정 knowledge_qa_v1 실행기 | 범용 자율 그래프는 별도 대표 업무·검증이 필요 |
| Langfuse | 기존 Agent/LLM 추적 backend·Ops 조회 연결 | trace/usage·샘플링/보관·민감값/원문 정책을 단계별 확장. 학습 MLflow와 자산 소유권 분리 |
| Airflow | Data 일정/구간 재처리 기능용으로 계획 채택 | F2a 일정/동시성/기간 key 계약 후 F2b 한 DAG. infra 기동과 Data 업무 상태 분리 |
| LiteLLM | Model 외부 provider·공통 추론/routing 후보 | F7과 함께 protocol/키·한도·retry 소유자·배포 동기화 계약. 구체 SDK/Proxy 범위 결정 |
| LightRAG | Knowledge의 그래프 검색 비교 실험 | 기존 벡터 검색 실패 중 관계 질문, 동일 평가 자료·출처·갱신/삭제·추출 비용 확인 |
| Neo4j | 선택 그래프 저장소. LightRAG의 필수 조건 아님 | 실제 저장/질의 요구와 대안 비교 후 선택. 별도 schema·근거·복구·동기화 인수 |
| MLflow | 기존 학습 실험/결과 연결 | Langfuse 도입을 이유로 학습 결과/ModelVersion 기준을 변경하지 않음 |
| ZenML | 반복 ML 파이프라인/캐시 후보, 별도 보류 | 현재 Data/Airflow/Operation 체계로 해결되지 않는 재사용 요구가 있을 때 검토 |

## 중복을 줄이는 경계

- Airflow는 Data 배치 일정/누락 구간, n8n은 상위 사용자 업무 흐름이다. Job 접수/실행권/완료는 각 소유 플랫폼 API·Operation/Dispatcher가 유지한다. 같은 파이프라인의 일정/재시도 소유자는 하나다.
- Model은 local/external backend 신원·capability·credential 참조·추론 정책, LiteLLM은 선택 protocol/routing 구현이다. [등록 동기화·embedding 공간 계약](litellm-integration-review.md)을 유지한다.
- Knowledge는 LightRAG 입력/검색/그래프·버전/평가, Data는 canonical, Model은 실제 추론, Agent는 최종 답변/Tool/실행을 소유한다. Neo4j 도입으로 PostgreSQL/OpenSearch와 원본을 무조건 이중 관리하지 않는다.
- Langfuse trace와 MLflow 학습 기록은 실행 ID로 연결하며 Model/Agent 버전의 기준 원본을 대체하지 않는다. Ops는 공식 조회 API/관측 결과를 사용한다.
- 신규 영구 서비스는 infra에서 버전·자원·포트·인증 참조·보관/복귀를 관리한다. 기존 domain/관측 DB·볼륨을 임의 공유하거나 재생성하지 않는다.

## 관측과 운영 후보

Prometheus·Node Exporter·cAdvisor·GPU 수집·Langfuse·Alertmanager/Ops 조회와 경보의 과거 인수는 [Ops 근거 목차](../history/ops-evidence-catalog.md)를 따른다. Grafana 추가/외부 알림 발송·장기 보관/부하는 별도 범위다. 수집 성공·서비스 건강·업무 완료·내용 품질은 서로 다른 상태로 표시한다. 운영 백업/복원·추가 운영 자동화·실제 재부팅·Kubernetes 보류는 유지한다.

## 5. GPU와 서비스 관측 범위

- 장치: GPU 사용률, VRAM 사용/총량, 온도, 전력부터 확인한다. 클럭·스로틀링·Xid 등 추가 지표는 장치/드라이버 지원을 확인한다. GeForce 계열은 DCGM 일부 기능이 제한되므로 프로파일링·ECC·NVLink 지표가 모두 제공된다고 가정하지 않는다.
- 연결: GPU UUID와 호스트를 기준으로 관측하고 Model의 배포·runtime 컨테이너·할당 GPU 정보를 연결한다. 다중 컨테이너가 공유하는 GPU의 사용률/전력을 배포별 사용량으로 단순 나누지 않는다.
- 모델 서비스: vLLM의 `/metrics`에서 제공하는 대기/실행 요청 수, KV 캐시, 지연·처리량 등을 실제 배포 버전에 맞춰 확인한다. 첫 토큰 지연은 해당 생성 모델에서 수집한다. GPU 사용률 하나로 지연 원인을 판단하거나 높은 VRAM 예약량을 곧바로 OOM으로 판정하지 않는다.
- 플랫폼: 초기에는 exporter/서비스 수집 성공과 호스트 자원부터 확인한다. 이후 필요한 API 오류·지연, 작업 대기/성공/실패·재시도 지표를 추가한다. 짧은 Job의 성공/실패는 cAdvisor 관측만으로 판정하지 않는다.
- 저장: 지표 보관 기간과 최대 저장 용량은 서버 여유 공간을 보고 별도로 정한다. 기존 [테스트 결과 1 MB 정책](../testing/artifact-retention.md)은 프로젝트에 보관하는 검증 산출물 정책이며 운영 시계열 DB의 용량 제한과 다르다. 질문 원문·토큰·비밀 값을 지표 라벨에 넣지 않고 무제한 요청 ID 라벨을 피한다.

## 공식 자료와 실제 검증

2026-10-12 확인: [Airflow](https://airflow.apache.org/docs/apache-airflow/stable/index.html), [LightRAG](https://github.com/HKUDS/LightRAG/blob/main/README.md), [LiteLLM routing](https://docs.litellm.ai/docs/routing), [Langfuse](https://langfuse.com/docs/observability/overview). 기존 n8n/LangGraph/MLflow·운영/추적의 고정 버전과 인수 근거는 각 [플랫폼/운영 문서](../README.md)에서 확인한다.

여기서 채택/조건부 실험은 프로젝트 설계 판단이며 성능·비용 실측이나 설치 완료가 아니다. 버전/호환성·상용 제공 기능은 실제 도입 때 다시 확인한다. 현재 재개 위치는 [인수인계](../HANDOFF_NEXT_SESSION.md), 실행 순서는 [로드맵](../roadmap.md)을 따른다.
