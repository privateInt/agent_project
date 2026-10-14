# Experiment Advisor 조사와 최소 설계

2026-10-14 사용자 요청에 따른 후속 계획 후보다. 이번 단계는 저장소 소스·계약과 공식 모델 문서의 읽기 조사, 설계 제안뿐이다. Advisor 구현·실험·모델 다운로드·서비스 재시작·운영 변경을 승인하거나 수행한 기록이 아니다. 첫 기능 작업 F0a의 우선순위는 유지하며, F11 구현은 이 검토 결과를 사용자가 확인하고 별도로 승인한 뒤 시작한다.

조사 입력은 Agent 준비 PR의 `5962f897d70d0d754cbcd43627548a84750ba0b6`와 내용이 같은 격리된 저장소다. 실제 simsys4090 API·배포·자원 상태를 이번에 조회하지 않았다. 아래의 구현 확인과 과거 서버 근거를 현재 운영 인수로 해석하지 않는다.

## 판단과 책임

추가하는 것이 적절하다. 다만 새 기술이 더 좋다고 단정하는 추천기보다, 확인한 근거·미확인 조건·공정한 비교 방법을 설명하는 실험 제안기로 시작한다. 사용자 기준인 중복 제거·명확한 구분·설명 가능성을 유지한다.

Agent Platform이 제안과 사용자 결정·후속 작업 연결을 소유한다. 별도 Utils/Benchmark 플랫폼이나 모델 등록부를 새로 만들지 않는다. 구현 시 `src/platform/agent/`의 기존 Gateway/Service·저장 구조에 작은 도메인 모듈로 넣고, 기존 `src/apps` 화면에서 필요할 때 연결한다. 최초 인터페이스는 제안 미리보기 한 경로만 정하며, CLI·Tool·화면에 제안 로직을 각각 복제하지 않는다. 이는 후보 배치이며 아직 파일·API·DB schema를 만든 상태가 아니다.

| 소유자 | 맡을 책임 | Advisor가 보관할 것 |
|---|---|---|
| Agent | 실험 목적·후보·근거·제안 버전·사용자 결정·조정 | 자기 제안 메타데이터와 타 플랫폼 자산/결과 참조 |
| Model | 모델/버전·capability·배포·추론·학습 및 해당 평가 | Model ID/version/profile과 조회 시각·결과 참조 |
| Data | 원본·canonical·문서 처리/OCR 작업·품질 결과, F1 이후 Dataset/Version | 원본/정답/처리 결과의 불변 참조와 hash |
| Knowledge | 색인·검색·rerank 연동·검색 평가 | index/profile·평가 데이터/프로토콜·결과 참조 |
| infra/Ops | 기존 공용 서비스와 관측·자원 조회 | 관측 구간·방법·환경 참조 |

타 플랫폼 DB·파일·객체를 직접 변경하지 않고 공식 계약을 사용한다. Model/Version/Deployment, Data 문서·Dataset, Knowledge 색인·전체 평가 결과를 Agent Registry에 이중 원본으로 저장하지 않는다. 아직 API가 없는 평가 CLI 산출물은 필요한 읽기 참조/수집 계약을 먼저 정하며, 현재 REST 평가 서비스가 존재한다고 가정하지 않는다. 현재 Dataset/Version은 legacy Model 소유이고 Data 이전은 [F1 계획](../plans/platform-expansion-work-plan-20261012.md)이다.

## 현재 기능과 재사용 범위

전용 Experiment Advisor·제안/승인 Registry·범용 자동 Benchmark는 조사한 소스에 없다. 아래 기능은 각각의 목적에 맞게 재사용한다.

| 확인한 구현 | 재사용할 부분 | 그대로 제공하지 못하는 것 |
|---|---|---|
| [Model 학습 평가](../reference/training-evaluation.md), [Gateway](../../src/platform/model/gateway/app/training_evaluation.go), [평가 결과 검증](../../src/platform/model/internal/trainingevaluation/result.go) | 학습 후 `final_assistant_nll_v1`, 고정 DatasetVersion/hash·환경 신원, 결과 GET·2~5개 TrainingRun 비교·기존 MLflow 연결 | OCR/검색/생성 답변 정답률 평가. 서로 다른 base/tokenizer의 NLL을 공통 품질 순위로 비교할 수 없음 |
| [Knowledge K08](../knowledge-platform/evaluation.md), [runner](../../src/platform/knowledge/evaluation/runner.py), [metrics](../../src/platform/knowledge/evaluation/metrics.py) | 검토된 고정 질문·원문 span/quote·dataset/hash, Hit/Recall/MRR/nDCG, 실패 포함 분모·후보 신원·시간 기록 | 답 없음/최종 답변 의미 품질, 범용 부하/throughput/VRAM benchmark. 현재 runner는 같은 query profile/vector를 공유하므로 다른 embedding 공간 비교에는 별도 프로토콜이 필요 |
| [Data OCR 품질 계산](../../src/platform/data/tests/ocr_quality.py), [live wrapper](../../src/platform/data/tests/live_ocr_quality.py), [검사](../../src/platform/data/tests/test_ocr_quality_tools.py) | NFC·공백 제거 CER, 고유 marker 읽기 순서, 좌표 anchor, 표 cell 값/row-column span, 한국어 본문·표·2단 fixtures | WER/TEDS·범용 다중 모델 실행. 기존 wrapper/provenance는 Surya 전용이며 공통 canonical 변환/모델별 adapter가 필요 |
| [문서 품질 감사](../../src/platform/data/tests/audit_document_quality.py), [실제 OCR 한계](../data-platform/testing-ocr-quality.md) | 고정 정답·원본 보존·품질 경고·문서별 표/도식 검사와 실패 기록 | 특정 문서·합성 fixtures 결과를 일반 한국어 품질 우위로 확대할 수 없음 |
| [기존 GPU 관측](../../src/infra/observability/ops/gpu.py), [관측 구성](../../src/infra/observability/README.md) | Prometheus/DCGM의 GPU 사용률·메모리와 기존 관측 기반 | 전체 GPU 수치는 실험별 peak VRAM이나 요청 지연/처리량을 자동 분리하지 못함. 격리·동시 부하·sampling 조건을 정해야 함 |

Model 학습 평가의 과거 GPU 인수는 [검사 문서](../testing/training-evaluation.md)에 보존된 범위다. Knowledge의 [12문항 과거 결과](../../src/platform/knowledge/tests/results/evaluation-candidate-diagnostics-20260921.json)는 한정 자료 평가이며 recursive ON의 원래 실패와 후속 조회를 구분한다. Data의 특정 raster 9건 품질 0/9·timeout 2건 기록은 위 CER/geometry 합성 suite와 다른 평가다. 이번 조사에서는 기존 검사·Benchmark를 재실행하지 않았다.

## 첫 제안 후보와 호환성

후보의 공식 문서는 확인했지만 우리 한국어 자료에서의 개선 여부·실제 비용/속도는 미측정이다. 현재 기준 모델·version·profile·배포 상태도 향후 공식 API 조회로 고정해야 한다. 과거 모델 이름만으로 현재 baseline을 확정하지 않는다.

| 영역 | 사용자 기준 → 후보 | 비교할 내용 | 실행 전 필요한 일 |
|---|---|---|---|
| OCR | Surya → [PaddleOCR-VL 1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6) | 동일 한국어 본문·표·2단/스캔 문서의 CER·표/읽기 순서·페이지 시간·실패율·자원 | Model OCR profile/adapter와 Data canonical 변환 검증, 동일 page/render/preprocess 및 정답 기준 고정 |
| Embedding | Snowflake Arctic Embed KO → [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | 같은 canonical/청크·질문·정답에서 검색 지표·embedding/query 시간·처리량·자원 | 별도 embedding profile/공간과 격리 index version. 모델별 instruction/pooling/정규화 고정, cross-space 비교 프로토콜 |
| Reranker | Dongjin-kr/ko-reranker → [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) / [4B](https://huggingface.co/Qwen/Qwen3-Reranker-4B) | 동일 질문·고정 후보 집합에서 MRR/nDCG·rerank 시간·자원 | Model rerank adapter/profile·입력/점수 의미 검증. 다른 모델의 raw score 수치를 같은 척도로 비교하지 않음 |

[현재 serving profile 검증](../../src/platform/model/deployment/runtime/ocr.go), [embedding](../../src/platform/model/deployment/runtime/embedding.go), [reranking](../../src/platform/model/deployment/runtime/reranking.go)은 Surya/snowflake-ko/ko-reranker의 지원 snapshot·구조를 확인한다. 후보를 모델명만 바꿔 바로 배포할 수 없다. Qwen embedding의 1024차원이 현재 baseline과 같아도 같은 embedding 공간이 아니다. 공식 Qwen 지침의 query instruction·last-token pooling과 현재 Snowflake의 CLS 규칙, Qwen causal-LM 기반 rerank와 현재 classification/logit 규칙을 각각 구분한다.

같은 Dataset을 쓴다는 조건만으로 공정한 비교가 완성되지 않는다. 목적·정답·문서 hash/split·전처리·chunking·candidate_k/top_k·모델 revision/런타임/정밀도·측정 구간·warmup/반복 횟수를 고정하고, 의도적으로 바꾸는 조건과 필수 모델별 입력 규칙을 기록한다. OCR→embedding→rerank를 한 번에 모두 교체하지 않는다. 품질/허용 손실·지연·비용 목표는 실행 전에 사용자가 검토하며 결과를 본 뒤 통과 기준을 낮추지 않는다.

단일 GPU와 다중 노드 분산 추론은 장기 예시로만 남긴다. 현재 분산 학습 기능을 분산 추론 지원으로 간주하지 않으며 첫 세 모델 비교의 구현·실행 범위에 포함하지 않는다.

## 최소 MVP와 제안서 한 가지 형식

F11a는 현재 모델/버전/profile을 공식 GET 또는 출처가 표시된 사용자 제공 자료로 읽고, 사용자가 지정한 후보 하나의 제안서를 미리보기/내보내는 범위다. 자동 인터넷 탐색·LLM 제안 생성·모델 다운로드·배포·추론·Benchmark 호출은 포함하지 않는다. 처음에는 확인한 메타데이터와 한 가지 템플릿으로 생성하고, 미확인 항목을 숨기지 않는다. 처음부터 새 DB·상시 worker·scheduler·알림 서비스를 만들지 않는다.

| 제안 필드 | 기록할 내용 |
|---|---|
| 목적과 판단 근거 | 해결하려는 문제, 공식 출처/조회일, 사실·개선 가설·미확인 조건을 구분 |
| 기준/후보 | 소유 플랫폼·model/version/profile/revision 참조. 현재 배포 관찰과 과거 근거 구분 |
| 평가 입력/프로토콜 | 문서/정답/split·hash 참조, 변경 변수·고정 조건, 평가 코드/rubric 버전과 비교 가능성 |
| 지표와 완료선 | OCR/검색/시간/처리량/실패율/자원 중 적용 항목, 방향·통과 기준. 미측정은 미측정으로 기록 |
| 비용과 자원 | 데이터/실행 수·시간·GPU 상한·다운로드/저장 공간·외부 API 비용/전송 여부. 추정과 실측 구분, 근거 없는 숫자 생성 금지 |
| 실행 범위 | 담당 플랫폼·격리 환경·중단 조건·기존 운영 자산 보존·필요한 adapter/프로토콜 |
| 이력과 승인 | 제안 ID/version/hash·유사 제안 참조, 사용자 검토 상태. 실험 실행과 운영 적용의 별도 승인 필요 |

예시 문구는 “PaddleOCR-VL 1.6을 문서·표 인식의 비교 후보로 제안합니다. 같은 한국어 문서로 Surya와 CER·표 구조·시간을 비교할까요? 우리 자료에서 개선 여부는 아직 확인되지 않았고 새 adapter가 필요합니다.”처럼 쓴다. 공개 벤치마크나 매개변수 크기를 우리 자료의 성능 보증으로 표현하지 않는다.

## 단계와 승인 경계

| 단계 | 범위 | 완료선 |
|---|---|---|
| F11a | 읽기 전용 Advisor·제안 미리보기 MVP | 한 후보·한 입력 그룹의 제안 생성, 공식 조회/자료 출처·미확인/지원 불가·승인 대기 표현, 다른 플랫폼 변경/실험 실행 0. 구현은 사용자 별도 승인 후 |
| F11b | Agent 제안·결정 이력 | proposed/approved/deferred/rejected의 의미와 제안 version/hash를 고정. approved는 해당 제안 검토 결정이며 실행 권한이 아님. 기존 Agent 저장 기반에 최소 자기 메타데이터만 추가 |
| F11c | 승인한 수동 격리 비교와 결과 연결 | 필요한 Model profile·Data 입력/정답·Knowledge 비교 프로토콜을 준비한 뒤 승인된 실험 범위만 수행. 기존 결과 참조·실패/미실행·환경·측정 한계와 개선 여부 보고 |
| F11d | 자동 탐색·실험 조정·운영 연결 검토 | 앞 단계의 실사용 가치 확인 후 별도 설계/승인. 기존 Tool·Flow·scheduler를 검토해 중복 worker를 피하고, 운영 승격은 실험 승인과 별도 결정 |

중복 탐지는 baseline/candidate revision·입력/정답 hash·프로토콜·환경 조건으로 실험 지문을 만들고 이전 제안/결과를 보여주는 방향으로 설계한다. 유사하다는 이유만으로 필요한 재평가를 금지하거나 이전 실패를 성공으로 덮어쓰지 않는다. 기준·데이터·환경이 바뀌면 이유를 기록하고 새 제안 버전으로 다룬다. Registry에는 결과 원본을 복제하지 않고 소유자의 artifact/Run 참조와 무결성·판정 근거만 연결한다.

비교 실행 승인은 제안의 고정 버전·입력·환경·자원/시간/비용 상한·허용 작업에 연결한다. 다운로드·후보 기동·Data 정제·색인·추론은 제안 미리보기나 계획 문서 추가만으로 허용되지 않는다. 변경되거나 만료된 조건을 예전 승인으로 수행하지 않는다. 검증된 결과의 채택·운영 배포·head 전환은 다시 사용자 승인 대상이다.

우선순위는 [기존 계획](../plans/platform-expansion-work-plan-20261012.md)과 [로드맵](../roadmap.md)의 비긴급 F11 후보로 관리한다. 현재 F0a→CI 기준선→문서 QA 제품 인수의 우선순위는 유지한다. CD 전체 완료가 Advisor 미리보기의 기술적 선행조건인 것은 아니며, 실제 착수 시 요구 API·입력 참조·검사 준비와 우선순위를 사용자와 확인한다. 현재는 조사/계획 완료이며 Advisor 구현이나 운영 사용 가능 판정은 아니다.
