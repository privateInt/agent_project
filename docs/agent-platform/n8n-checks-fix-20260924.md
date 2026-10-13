# n8n 서버 복구 확인과 격리 검사 실행 수정

2026-09-24. Agent a07.1 / schema 5 유지. 운영 n8n은 infra, 연동 검사는 Agent 소유다. 이번 변경은 검사 이미지·실행 안내와 검증 기록에 한정한다.

**최신 서버 결과 — 통과:** 09:24 후속 로그에서 권한 수정 패치 10개 파일 적용, 검사 이미지 a07.3 빌드·비루트 접근 검사와 실제 n8n CLI 검사 7개가 모두 통과했다. 기동·검사 이미지 오류 대응은 완료했다. 아래 수정 적용 명령은 아직 미적용한 환경용 이력이며, 이번 통과 서버에서 재실행할 필요는 없다.

## 실제 서버에서 확인한 결과

사용자가 붙여넣은 로그의 패치 백업 시각은 `20260924T052059`다.

- DB 복구 패치 19개 파일 적용·백업 완료.
- 기존 `agent-flows_flows-db`, `agent-flows_flows-data` 볼륨을 재사용.
- `GRANT` 성공, `flows_service`로 `CREATE SCHEMA IF NOT EXISTS "n8n"`와 테이블/INSERT/ROLLBACK 통과.
- `infrastructure-flows-postgres-1`, `infrastructure-n8n-1` 모두 Healthy.
- `PASS: infra n8n ready; original volumes/keys preserved` 확인.
- 뒤이어 **별도 검사 컨테이너**가 `Cannot find module '/checks/tests/run.cjs'`로 종료했다. 검사가 시작되지 않았으며 검사 PASS로 기록하지 않는다.

운영 n8n의 기동 복구는 완료됐다. 사용자 워크플로 실행·질문 품질·편집기 화면·사용자 AgentRun 재시작 보존 검증을 완료했다는 뜻은 아니다.

## 09:24 서버 검사 통과 근거

- 패치 백업: `n8n-permissions-20260924T092438-aeza5294`, 10개 파일 적용.
- 검사 이미지: `agent-platform-n8n-checks:a07.3`.
- 빌드 이미지 ID: `sha256:bd4da8a142fdf10a842a87c7e008b9934d9c88a1012dc5d8372dbc9856e6ed27`.
- USER node 단계에서 비루트 여부·디렉터리 탐색·파일 읽기·Node 구문·Code node 계약 검사 명령이 성공했다.
- 최종 출력: `result: passed`, `stage: A07`, `scope: real n8n 2.40.5 CLI / isolated SQLite / Agent HTTP fixture`.
- POST 응답 유실 후 같은 요청 재시도, 완료까지 조회/답변·출처, 새 실행의 원래 caller_context 보존, 같은 키/다른 본문 409, 빈 근거 정상 종료, Agent 실패·중단 전달의 7개 검사가 통과했다.
- 대역 집계: `effects=4`, `posts=9`, `reads=6`. 여러 검사 시나리오의 합계이며 실제 사용자 요청의 실행 횟수가 아니다.

운영 DB의 Healthy/키·볼륨 보존은 앞선 기동 로그 근거다. 이번 CLI 검사는 운영 PostgreSQL이 아닌 격리 SQLite·Agent HTTP 대역을 사용한다. 따라서 실제 서버 Agent API 연결, GPU/Knowledge 응답 품질, 사용자 Flow 편집·저장·재시작을 추가로 검증했다고 표시하지 않는다.

근거 요약: `agent_platform/tests/results/a07-n8n-server-verified-20260924.json`. 이번 문서 반영은 재빌드·재시작·검사 재실행이 필요 없다.

## 검사 수정

직전 전달 소스와 압축 파일에는 `run.cjs`가 존재하고, 로컬 Compose 2.39.4도 기존 bind 경로를 정상 해석했다. 서버의 실제 mount/파일/권한 정보는 없으므로 구체적인 원인을 상대경로 오류나 파일 삭제로 단정하지 않는다.

검사 파일을 호스트 디렉터리에서 bind mount하는 의존성을 제거했다. `tests/Dockerfile`은 운영과 같은 고정 n8n 2.40.5 이미지에 `run.cjs`, `contract.cjs`, 예제 workflow만 복사한다. 파일을 명시해 누락 시 빌드가 실패하며, 빌드 중 구문·Code node 계약 검사도 실행한다. 후속 서버 빌드에서 기존 `COPY --chmod=0444`의 결함을 확인했다. 이 옵션은 새 디렉터리에도 영향을 줄 수 있어 탐색(x) 권한을 없앤다. 로컬에서 파일이 존재하는 디렉터리를 0444로 설정하면 동일 MODULE_NOT_FOUND가 발생했고, 0555로 복구하면 통과했다.

현재 Dockerfile은 COPY의 --chmod를 제거했다. 빌드 중 root 단계에서 디렉터리는 0555, 세 입력 파일은 0444로 **따로** 지정한다. 이어 USER node로 전환한 뒤 비루트 여부·디렉터리 탐색·파일 읽기·Node 구문·Code node 계약을 검사한다. 검사 컨테이너의 최종 실행 계정은 node다. 운영 계정이나 호스트 권한은 변경하지 않는다.

BuildKit의 부모 디렉터리 권한 적용 근거: [moby/buildkit #6801](https://github.com/moby/buildkit/issues/6801). 이 원인 확인은 이번 COPY 기반 이미지 빌드 실패에 대한 것이다. 앞선 호스트 bind mount 실패의 정확한 파일/권한 상태까지 확인한 것은 아니다.

`tests/Dockerfile.dockerignore`는 이 파일들만 빌드 컨텍스트에 포함한다. 기존 A07 설정·키가 `agent_platform/n8n`에 남아 있어도 빌드에 전달하지 않는다. 컨테이너는 `network_mode: none`, 임시 SQLite/임시 자격·로컬 HTTP 대역만 사용한다. 운영 DB·키·볼륨·실제 Agent/Model/Knowledge 호출과 의존성이 없다.

이번 후속 패치는 Dockerfile·검사 이미지 태그와 문서/검증 기록을 갱신한다. 기존 검사 코드·워크플로 입력은 그대로다. 사용자가 수정한 대상 파일은 전체 쓰기 전에 충돌로 중단한다.

## 적용 및 다음 검사

직전 `agent_platform_n8n_checksfix_20260924` 적용본이 기준이다. DB 복구와 이전 검사 패치를 다시 적용하지 않는다.

```bash
(
set -e
cd ~/workspace
tar -xzf agent_platform_n8n_permissions_20260924.tar.gz
python3 agent_platform_n8n_permissions_20260924/apply_patch.py \
  --project-root ~/workspace/agent_project --apply

cd ~/workspace/agent_project/agent_platform
docker compose -f n8n/tests/docker-compose.yml run --build --rm n8n-checks
)
```

빌드 중 `PASS: non-root check inputs readable`와 Code node 계약 검사 6개, 실행 마지막에 `result: passed`, `stage: A07`, `scope: real n8n 2.40.5 CLI / isolated SQLite / Agent HTTP fixture`를 확인한다. `up.py`·운영 n8n 재시작·Agent 재빌드·권한 복구 SQL 재실행은 필요 없다.

## 검증 범위와 보류 항목

로컬에서 원래 디렉터리 0444 권한으로 동일 MODULE_NOT_FOUND를 재현했다. 실제 Dockerfile의 chmod 명령을 복사한 검사 폴더에 실행해 디렉터리 0555·파일 0444를 확인하고 Node 구문·Code node 계약 6개·실제 n8n CLI 검사 7개가 통과했다. 이 실행기는 uid 0이지만 DAC 권한 우회 capability가 없어 탐색 권한 실패를 재현할 수 있다. 실제 컨테이너의 비루트 검사를 실행했다는 의미는 아니며, Dockerfile에서 USER node 이후 해당 검사를 필수화했다.

로컬에는 Docker daemon이 없었지만 최종 이미지 빌드·컨테이너 실행은 이번 사용자 서버 로그로 통과를 확인했다. 로컬 Node 24.19.0과 서버 이미지 Node 26.7.0의 차이도 유지한다. 이전 검증은 `agent_platform/tests/results/a07-n8n-checksfix-20260924.json`, 이번 권한 재현·수정 검증은 `agent_platform/tests/results/a07-n8n-permissions-20260924.json`에 기록한다.

사용자가 외부 작업 중이므로 페이지 확인은 계속 보류한다. 실제 LLM·Knowledge 답변 품질, 사용자 Flow 편집·실행, 사용자 AgentRun 재시작 조회는 별도 인수 항목으로 남긴다.
