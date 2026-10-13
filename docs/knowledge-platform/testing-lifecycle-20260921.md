# Knowledge 관리 화면과 인덱스 삭제 검증 — 2026-09-21

현재 상태는 **2026-09-22 사용자 서버 빌드·두 버전 삭제·참조 해제·재정렬 ON 화면 확인 완료, 검색 안내·ON/OFF·선택 전환 사용자 확인 완료**다. `knowledge_scope_docs_20260921`의 15개 파일 적용은 사용자 로그로 확인했다. 이 문서는 당시 관리·삭제 확인을 보존하며 PDF 파싱·OCR·제목 추정은 포함하지 않는다.

## 변경과 책임

관리 화면에서 인덱스 선택을 바꾸면 기존 상세·버전·작업 대상을 해제한다. 늦은 인덱스/버전/목록 응답이 새 선택을 덮지 않으며, 이전 카드의 버튼도 다른 선택에 삭제 요청을 보내지 않는다. 요청 처리 중 다른 인덱스를 열었다면 처리 응답이 이전 인덱스로 돌아가게 하지 않는다. 삭제 중인 인덱스의 버전 삭제와 실행 ID가 없는 실행 상태 조회도 비활성화한다.

검색 시 이전 결과를 먼저 비운다. 새 요청의 입력 검증부터 실패하더라도 이전 성공 결과가 남지 않는다. 버전을 바꾸면 이전 자료 필터도 해제한다. 검색 API·점수·순위·rerank 정책은 변경하지 않는다.

삭제 서비스 자체는 기존 `OpenSearch 삭제 확인 → Data 참조 해제 확인 → deleted` 순서를 유지한다. 신규 `tests/live_index_lifecycle.py`는 사용자 서버에서 별도 테스트 인덱스 하나와 버전 두 개를 만들어 이 흐름을 검증하는 명시적 실행 도구다. 기존 기준 인덱스에는 조회만 한다.

`ops/verify_cleanup`은 조회 전용 Go 명령이다. OpenSearch GET으로 버전별 물리 인덱스의 소유 정보·청크 수/부재를 확인하고, Data의 GetKnowledgeReferences로 소유자별 참조 상태·문서 해시를 확인한다. Data의 공개 조회/읽기 RPC로 기존 원본 메타데이터와 canonical 바이트 해시도 확인한다. 다른 플랫폼의 DB나 버킷에 직접 접근하지 않으며 Data 원본의 영구 삭제를 호출하지 않는다. 원본 PDF 바이트를 다시 다운로드하는 검사는 포함하지 않는다.

## 로컬 검사와 한계

- Python 12개: 전체 수명주기, 생성·두 빌드·활성화·두 종류 삭제의 응답 유실 후 재실행, 기존 자료 보존, 입력/소유자 변경 거절, 추가 버전의 삭제 차단, 관측 실패·참조 미해제·청크 변동 거절, HTTP 오류/리다이렉트, Docker 전달 환경 제한.
- Node DOM/네트워크 대역 3개 스크립트: 기존 관리 요청 재전송, 선택/응답 경쟁, 검색·rerank·이전 결과 제거.
- Go 관측기 소스의 구문/서식 확인. 실제 Go 컴파일과 7개 HTTP 관측 하위 검사는 서버의 `cleanup-verifier` 이미지 빌드에서 실행한다.
- 로컬 Docker·실제 OpenSearch/Data·GPU·실제 브라우저 렌더링 검증은 수행하지 않았다. 합성 성공을 서버 삭제 완료로 표시하지 않는다.

## 관련 코드 변경 후 검사·Gateway 반영

최신 통합 소스에 관리 화면 보완과 삭제 검증 도구가 포함돼 있다. 아래는 관련 코드 변경 후의 절차다. 문서 통합만으로 실행하지 않는다.

```bash
cd ~/workspace/agent_project
(
  set -euo pipefail

  cd "$HOME/workspace/agent_project"
  python3 -m unittest discover -s knowledge_platform/tests -p 'test_index_lifecycle.py' -v
  docker build -f knowledge_platform/Dockerfile --target cleanup-verifier \
    -t knowledge-platform-cleanup-verifier:20260921 knowledge_platform

  cd knowledge_platform
  docker compose --env-file .env --env-file .env.indexing \
    -f docker-compose.yml -f indexing.compose.yml build knowledge-gateway
  docker compose --env-file .env --env-file .env.indexing \
    -f docker-compose.yml -f indexing.compose.yml up -d --no-deps knowledge-gateway
)
```

교체 대상은 화면을 제공하는 Gateway 하나다. 관측기는 별도 이미지로 준비하고 명시적 검증 시에만 `--rm` 컨테이너로 실행한다. 신규 상시 서비스나 모델 배포는 없다. 현재 `knowledge-service` 컨테이너의 네트워크와 필요한 연결 환경만 사용한다. PostgreSQL/MinIO 자격증명은 관측기에 전달하지 않는다. Docker 명령 인자/로그/결과 파일에 OpenSearch 비밀번호를 쓰지 않는다.

## 실제 삭제 확인

기준은 개선 structure의 기존 ready 버전이며 작은 표 발췌 canonical 한 건을 사용한다. 기본 상한은 버전당 8개 청크다. 이를 넘으면 새 인덱스를 생성하기 전에 중단한다. 새 버전 두 개의 문서 임베딩만 수행하므로 기본 최대 16개 청크를 처리한다. 실제 모델 요청 수는 배치/입력 한도에 따라 달라진다. 질문 임베딩·rerank·전체 품질 평가는 실행하지 않는다.

```bash
cd ~/workspace/agent_project
python3 knowledge_platform/tests/live_index_lifecycle.py \
  --baseline-index-version-id a1334595-583e-490c-8ad9-7ec279f188b3 \
  --canonical-id acd8d804-ff15-484e-8cfb-0dd13d339eb2 \
  --state-file "$HOME/workspace/knowledge_eval_20260921/records/index_lifecycle_20260921.json"
```

검사 순서:

1. 기준 인덱스/버전 ready·held, 입력 ChunkSet, 자료의 크기 상한과 저장소/Data 조회 가능 상태 확인.
2. UUID를 포함한 전용 이름과 저장된 요청 키로 새 인덱스와 A 버전 생성. 기존 Model 배포·ChunkSet을 사용한다.
3. A 활성화 후 A 단독 삭제가 `409 ACTIVE_VERSION`으로 거절되는지 확인.
4. 같은 자료로 비활성 B 생성. 기준/A/B의 물리 인덱스와 held 참조를 직접 조회한다.
5. B 삭제 후 물리 B 부재·B released·A와 기준의 held/청크 수 유지 확인.
6. 전용 인덱스 전체 삭제 후 A/B 물리 부재·released와 부모 deleted 확인.
7. 기존 기준의 활성 버전/revision/입력/profile·canonical/청크 본문/출처가 보존되는지 확인.

`status: passed`와 `physical_indexes_absent: true`, `data_references: released`, `baseline_unchanged: true`가 실제 완료 기준이다. 인덱스·버전 이력 행과 공유 ChunkSet/canonical은 남는다. 이 검사만으로 서비스 장애·물리 삭제/참조 해제의 모든 경쟁 상황이나 검색 품질을 보장하지 않는다. 삭제 순서·실패 보존은 기존 통합 검사와 함께 판단한다.

진행 파일에는 생성 전 요청 키와 고정 입력, 전용 리소스 ID, 단계만 저장하며 벡터나 원문 청크를 저장하지 않는다. 같은 파일로 재실행하면 응답 유실을 같은 키로 확인하고 완료한 빌드를 새로 만들지 않는다. 성공 후 재실행은 읽기 전용 확인이다. 동시 실행은 파일 잠금으로 차단한다. **도중 실패 시 진행 파일을 지우거나 새 경로로 재시도하지 않는다.** 상태와 오류를 먼저 확인하며 실패한 빌드를 자동 재생성하지 않는다. 예상과 다른 인덱스 이름·입력·추가 버전·기준 변경을 발견하면 삭제를 중단한다. 관측기 오류도 삭제 성공으로 처리하지 않는다.

완료 후에도 `index_lifecycle_20260921.json`은 `records/`에 보관한다. 패치 압축파일과 해제 폴더는 적용 후 삭제 가능하다. 이전 `indexing_*.json`을 records로 이동했다면 예전 live_indexing 명령의 `--state-file`도 새 경로를 사용해야 한다.

## 실제 화면 확인

Gateway 교체 후 브라우저를 새로고침한다. 기존 개선 structure 인덱스 `dab75094-38a6-47ec-9dee-d016397a0bce`와 버전 `a1334595-583e-490c-8ad9-7ec279f188b3`를 조회한다.

- 관리: 상세를 열고 다른 인덱스를 선택하면 이전 상세/버전이 지워지는지 확인한다. 선택 후 상세를 열기 전에는 빌드·삭제 요청이 접수되지 않아야 한다.
- 검색: 해당 버전을 준비하고 짧은 질문으로 OFF 검색한다. 출처/품질 경고를 확인한다.
- ON: 재정렬 배포 `cc64a683-a69f-48a5-836e-9f52f0255cc6` 조회 후 후보 20·결과 5로 검색한다. 재정렬 적용 여부, 벡터/최종 순위, 점수를 확인한다.
- 오류: ON 상태에서 후보 수를 결과 수보다 작게 설정하고 검색하면 오류를 표시하며 이전 결과가 남지 않아야 한다. 이 입력 오류 확인에는 추론이 필요 없다.

브라우저 확인 결과는 자동 삭제 도구의 passed와 별도로 기록한다. 운영 모델을 중지하거나 기준 인덱스를 삭제해 오류를 만들 필요는 없다.

## 2026-09-22 사용자 서버와 화면 확인

사용자 첨부 로그에서 기존 패치 19개 파일 적용, Python 12개(0.633초), cleanup-verifier의 Go 검사·빌드와 Gateway 이미지 빌드·기동을 확인했다. 실제 전용 인덱스 `b2c0f0df-bb40-4d9b-a8bc-7ccd37235256`의 A `ed640dc6-6443-4429-8057-341af2cf433a`와 B `3635673b-5838-4ddf-adce-4bac1fad0b9c`를 삭제했고 독립 조회에서 두 물리 인덱스 부재·Data 참조 released를 확인했다. 버전당 2개 청크를 사용했으며 기준 `dab75094`/`a1334595`·canonical·공유 청크는 보존됐다. 최종 status는 passed다. 원본 PDF 바이트 재검사·원본 purge 호출·모든 장애/동시성 경로를 완료한 뜻은 아니다.

사용자 화면에는 두 버전의 deleted/released 이력이 표시됐다. 이후 검색 결과를 붙여넣고 성공을 확인했다. 선택한 개선 structure 버전·snowflake 1024차원·ko-reranker 프로필과 함께 후보 20개→결과 5개, 재정렬 적용을 확인했다. 표시된 300ms는 Knowledge 응답의 검색 처리 시간이며 앞선 질문 임베딩·브라우저/네트워크를 포함한 전체 대기 시간이 아니다.

범위를 직접 설명하는 2.2.1 문단은 벡터 1위→최종 2위(0.7182 / -4.6126), 2.2.3 영향평가 설명은 5위→1위(0.6721 / -4.5361)였다. 음수 logit은 오류나 확률이 아니다. 이 사례를 재정렬의 일관된 품질 개선으로 해석하지 않으며 기존 12문항 평가 기록을 바꾸지 않는다. needs_ocr 경고도 유지했다. 결과 5개는 화면의 반환 개수에 근거하고 사용자 붙여넣기에는 앞부분만 있으므로 전체 5개 내용·펼친 출처까지 검수했다고 표시하지 않는다.

### 검색 안내 수정

최초 화면에서는 배포 ID를 입력했지만 프로필 조회 완료 표시가 없었다. 코드를 확인하면 프로필 미조회·빈 질문·잘못된 후보 수 등은 추론 전 검증에서 거절되고 공통 action이 페이지 맨 위에만 오류를 표시했다. 검색 섹션에서는 이전 안내가 지워져 무반응처럼 보일 수 있었다. 사용자가 이후 프로필 표시와 ON 결과를 확인했으며 서버 검색 장애로 확정하지 않는다.

검색 관련 버튼은 오류·진행 안내를 검색 버튼 아래 searchStatus에 표시한다. 오류는 색상과 배경으로 구분하고 기존 aria-live를 사용한다. 검색/조회/준비 중 버튼 문구를 바꾸고 중복 클릭을 막으며 완료·실패 후 복구한다. 모델 프로필 조회의 진행·성공·실패도 같은 영역에 표시한다. 입력 변경 뒤 늦게 도착한 안내는 새 선택을 덮지 않는다. 검색 API·모델 정책·점수·PDF 처리는 변경하지 않는다.

로컬 Node의 search_ui_test.cjs에서 실제 등록된 클릭 핸들러로 미조회 profile, 후보 수 2<5, 빈 질문, 잘못된 결과 수, 준비 오류와 profile 조회 실패를 검사했다. 잘못된 입력의 추론 요청 0회·이전 결과 제거, 실행 중 버튼 복구·중복 클릭 차단·늦은 응답 안내 억제를 확인했다. 기존 관리 관련 두 스크립트도 통과했다. DOM/fetch 대역이며 실제 렌더링 검사는 아니다. [작은 검증 요약](../../knowledge_platform/tests/results/search-feedback-acceptance-20260922.json)에 근거를 보존한다.

이번 knowledge_search_feedback_fix_20260922 패치는 직전 수명주기 적용본이 기준이며 Gateway만 빌드·교체한다. 서버 적용과 수정 후 실제 오류 표시는 아직 대기다. 다음 실제 화면 확인은 후보 2·결과 5의 입력 오류와 이전 결과 제거(추론 없음), OFF 검색, 관리 인덱스 선택 전환이다. 완료한 삭제 검증·전체 평가를 반복할 필요는 없다. 기존 lifecycle 진행 파일은 records/에 보관한다.

### 후속 사용자 확인

같은 대화에서 후보 수가 결과 수보다 작을 때 검색 영역에 오류가 보이는 것을 확인했다. 재정렬 OFF는 후보 5개→결과 5개·재정렬 미적용·표시된 검색 처리 시간 5ms다. 관리에서 다른 인덱스를 선택하면 이전 상세·버전 목록이 사라진다고 사용자가 확인했다. 이 세 화면 경로를 확인 완료로 기록하며 앞 절의 대기 상태를 갱신한다. 전체 5개 결과/펼친 출처 검수, 모든 실제 네트워크 경쟁 검증을 의미하지 않는다. 다음 변경은 [HNSW 고급 색인 옵션](api-indexing.md#hnsw)이다.
