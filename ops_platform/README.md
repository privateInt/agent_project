# Ops Platform

공식 플랫폼/관측 API를 읽어 통합 상태·자원·추적·경보를 제공한다. 최신 완료 범위는 [Ops 문서](../docs/ops-platform/README.md), 현재 백업 단계는 [백업 안내](../docs/operations/backup-restore.md)를 따른다.

## O03 기동과 접속

아래는 최초 설치용 운영 참조다. 현재 서버의 LAN 접속과 핵심 화면 확인은 완료했으므로 이번 문서 적용 때문에 반복하지 않는다. 최초 설치 때 프로젝트 루트에서 실행하고 prepare의 주소 질문에는 실제 브라우저 접속 주소인 `http://서버IP:17100`을 입력한다. 다른 플랫폼/관측 서비스 7개가 이미 기동된 환경에서 Ops만 최초 준비한다. prepare가 실제 upstream 주소·지표 역할/job·마운트를 발견하고 출처를 기록하며 up은 Ops 기동 후 자원 7개 그룹·경보 소스 2개를 조회한다. 기본 upstream 포트로 대체하지 않으며 실패 시 역할별 원인을 출력한다. 기존 준비 상태에서 prepare는 발견/설정 재생성 없이 재사용한다. 기존 서비스는 기동 대상이 아니다.

```bash
python3 -B ops_platform/ops/manage.py prepare
python3 -B ops_platform/ops/manage.py up
python3 -B ops_platform/ops/manage.py credentials
```

`up`은 Ops 이미지 하나만 빌드·기동하고 health를 기다린다. `credentials`의 Ops 접속 코드를 브라우저에 입력한다. 현재 신뢰 LAN HTTP·Linux amd64/Python 3.12 기준이다. 기존 Agent·Langfuse 키는 서버에서 GET 조회용으로 참조하며 브라우저에 전달하지 않는다. 최초 접속 코드/설정은 전용 비공개 폴더에 저장하고 반복 prepare에서 보존한다.

```bash
# 새 Ops API의 읽기 검사. 원래 Agent 실패는 그대로 유지한다.
python3 -B ops_platform/ops/check_console.py \
  --agent-run-id 1a7d571b-e7e5-4b07-8f41-ad3392025b95

# Ops만 상태 확인 / 중지
python3 -B ops_platform/ops/manage.py status
python3 -B ops_platform/ops/manage.py stop
```

화면에서는 Agent 실행 ID 상세 → 5단계·오류·ToolCall → 추적 조회를 확인한다. 수집기/원본 API의 장애·키 누락·미지원·빈 결과는 별도로 표시한다. 값이 없는 자원을 0으로 만들거나 추적을 얻으려고 업무를 재실행하지 않는다. 완료한 O01/O02 인수·기존 서비스 재시작·새 추론은 반복하지 않는다.

[O03 적용·배치·API·검증 범위](../docs/history/ops-evidence-catalog.md#console-o03-20260927)를 따른다. 상태는 `~/.local/state/agent-project-ops/server`, 기본 17100 포트, 서비스는 `ops-web` 하나다. 새 Ops 서버 기동/조회 인수는 PASS다. 경보 화면까지 사용자 확인을 마쳤으며 O04의 남은 범위와 관측 데이터 백업/복원 계획을 정리했다. 현재 서버 확인은 상단 기록의 범위에서 완료했고 후속 자동 발견 범위는 공통 설계 원칙을 따른다. 다중 사용자 권한·조회 키 역할 분리·장기 지표 추이는 후속이다.

## O02 조회 모듈과 CLI (기존 운영 참조)

Model·Data·Knowledge·Agent의 기존 REST API를 조회하는 Python 표준 라이브러리 모듈이다. O03 운영 서버/화면이 재사용하는 조회 계층과 독립 CLI를 제공한다. 아래 O02 CLI 자체는 상시 서버를 기동하지 않는다. O03 서버는 위 절차로 별도 기동하며 경보는 O04에서 구현·서버/사용자 화면 확인을 마쳤다.

**서버 인수 PASS — 2026-09-27:** 네 플랫폼 표본 조회·연관 Operation·Agent 준비 상태·기존 실패 실행/단계/ToolCall을 확인했다. 학습 목록은 비어 있어 상세는 미검증이다. [서버 결과](../docs/history/ops-platform-202609-20261001.md#platform-api-verified-20260927)를 참고한다. 아래 명령은 운영 참조이며 문서 패치 적용을 이유로 다시 실행하지 않는다.

## 서버 확인

조회가 별도로 필요할 때 프로젝트 루트에서 아래 명령을 실행한다. 이번 서버 표본 인수는 완료됐다. 모든 원격 요청은 GET이며 현재 저장된 목록·상세·연관 Operation·Agent 5단계·ToolCall을 조회한다.

```bash
python3 -B ops_platform/ops/platform_status.py check \
  --agent-run-id 1a7d571b-e7e5-4b07-8f41-ad3392025b95
```

기본 주소는 `config.example.json`에 있다. Model 13000, Data 14000, Knowledge 15100, Agent 16100의 호스트 loopback 포트를 사용한다. 실제 설정이 다르면 별도 config를 만들어 `--config /절대/경로/config.json`으로 지정한다. 상대 `token_file`은 그 config가 있는 폴더 기준이다. 복사한 config는 Agent token_file을 현재 프로젝트의 절대 경로로 바꾸는 것이 명확하다. 실제 키는 JSON에 넣지 않는다.

Agent는 기존 `agent_platform/.secrets/api-token`을 읽는다. 네이티브 Model·Data·Knowledge Gateway에는 현재 Bearer 인증이 없으며 이 모듈이 서버 권한을 새로 만들지는 않는다. 외부 보호 프록시의 API Bearer 인증이 필요한 경우 해당 플랫폼에 `token_file` 경로를 명시할 수 있다. 자격증명이 있는 원격 주소는 HTTPS를 요구한다. 기본 호스트 loopback HTTP는 허용한다. API 토큰 자체가 조회 전용 권한인 것은 아니며 클라이언트가 GET만 구현한다. 키를 새로 만들거나 원래 파일을 변경하지 않는다.

`check`는 자원마다 최대 3개 목록과 첫 항목의 상세를 확인한다. Knowledge 버전은 샘플 인덱스 한 개의 목록만 조회한다. 고정 AgentRun은 목록 위치와 관계없이 별도로 직접 조회한다. 따라서 `passed`는 이 한정된 조회 계약의 통과다. Gateway가 응답하거나 조회에 성공해도 업무 성공·전체 의존 서비스 정상·전체 운영 인수를 뜻하지 않는다. Agent 원래 실패 결과는 `failed/ANSWER_REFERENCE_INVALID`로 그대로 표시된다.

| 출력 | 의미 |
|---|---|
| result=passed, 종료 0 | 선택한 플랫폼의 실제로 수행한 조회·연결 검사가 통과 |
| attention_required, 종료 2 | 연결/권한/원본 준비 상태/응답 계약 또는 관련 조회에 문제 있음. 다른 플랫폼 결과도 함께 반환 |
| error, 종료 1 | 설정·인자·로컬 상태 파일 문제. 상태 파일 오류는 삭제하지 말고 권한/경로를 확인 |
| sampled_count | 이번 페이지에서 읽은 개수. 전체/실행 중/최근 실패 개수가 아님 |
| has_more=true | 뒤에 페이지가 있음. 해당 소스의 순서이며 최신순으로 재해석하지 않음 |
| unsupported | 해당 원본에 목록 또는 의존성 readiness 계약이 없음. 네트워크 호출하지 않음 |
| not_checked_empty_page / NO_SAMPLED_INDEX_PARENT | 표본이 없어 그 상세/버전 검사는 실행하지 못함 |
| last_success_at | 같은 주소·같은 조회 경로가 계약 검사를 통과한 마지막 시각. 업무 갱신 시각이 아님 |

조회 실패는 `data=null`이며 이전 응답을 현재 값처럼 보여주지 않는다. 마지막 성공 시각만 `~/.local/state/agent-project-ops/api-last-success.json`에 저장한다. 부모 폴더 0700·파일 0600이며 URL/키/질문/응답/원래 실행은 저장하지 않는다. `--state-file`로 전용 비공개 폴더 안의 다른 경로를 지정할 수 있다. 동시에 같은 파일을 사용하는 두 명령은 뒤 요청을 STATE_BUSY로 중단한다.

## 개별 조회

```bash
# 모델 가져오기·학습·배포 Operation 목록 한 페이지
python3 -B ops_platform/ops/platform_status.py list \
  --platform model --resource operations --limit 20

# Data Operation 목록: 최대 5페이지; 읽기 범위는 결과에 표시
python3 -B ops_platform/ops/platform_status.py list \
  --platform data --resource operations --limit 20 --max-pages 5

# 고정 Agent 상세·5단계·검색 ToolCall
python3 -B ops_platform/ops/platform_status.py get \
  --platform agent --resource agent-runs \
  --id 1a7d571b-e7e5-4b07-8f41-ad3392025b95
```

한 페이지의 `data.next_cursor`를 `--cursor`에 그대로 지정할 수 있다. Knowledge index-versions 목록은 `--parent-id <index_id>`가 필요하다. `get --resource index-versions --id <index_version_id>`는 부모 없이 원래 상세를 읽는다. source_order는 API가 반환한 순서이며 Agent는 UUID 순서다. 페이지 예산 소진·중간 오류·커서 반복·중복 행은 `partial`로 표시하고 확보한 행을 보존한다. 페이지 간 스냅샷 일관성이나 전역 총건수는 보장하지 않는다.

## 코드와 검증

- `contracts.py`: 원본 상태/ID/시간/오류 메타데이터 투영. 사용자 입력·본문·프롬프트·답변·설정·오류 메시지 전문 제외.
- `transport.py`: GET-only·redirect/proxy/retry 없음, 기본 응답 한도 4 MiB, 요청별 5초 네트워크/읽기 예산. DNS 조회는 운영체제 resolver에 의존하므로 엄격한 전체 실행 시간 상한으로 보장하지 않는다.
- `client.py`: 공식 API 목록/상세·연관 Operation/ToolCall·단계 연결, 페이지 범위와 실패 격리.
- `state.py`: 최근 성공 조회 시각만 원자적으로 저장.
- `ops/platform_status.py`: 최대 4개 플랫폼을 동시에 조회하는 CLI. `check --full`은 정규화된 상세 envelope도 표시.

```bash
# 기존 O02 stdlib 조회 검사만
python3 -B -m unittest discover -s ops_platform/tests -p test_status.py -v

# O03 개발 검사: 별도 Python 3.12 가상환경에서 테스트 의존성 설치 후 실행
python3 -m pip install -r ops_platform/tests/requirements.txt
python3 -B -m unittest discover -s ops_platform/tests -v
```

로컬 28개 HTTP 대역/CLI 검사를 통과했다. 실제 서버의 네 플랫폼 GET-only 표본 조회도 통과했으며 위 명령의 재실행은 필요 없다. 실행 서비스 코드를 변경하지 않았으므로 Go/Docker/DB/GPU 검사를 다시 수행하지 않는다. [계약·제약과 서버 인수 범위](../docs/history/ops-evidence-catalog.md#platform-api-o02-20260927)를 참고한다.
