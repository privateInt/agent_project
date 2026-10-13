# A04 OpenAPI·HTTP·화면 검증 — 2026-09-23

최신 후속: [직접 API 등록 / OpenAPI 가져오기](tool-registration-20260923.md) 패치 22개 파일 적용, Gateway 이미지 빌드·Healthy와 연결 전 화면 표시를 사용자 로그/화면으로 확인했다. test1 질문 임베딩 1회 및 검색 API 직접 호출에서 결과 5개를 확인했다. 이후 사용자 화면에서 Agent를 통한 Knowledge 검색 Tool 시험 성공(mode=test, state=succeeded)을 확인했다. 게시·발행 후 실행과 남은 A04 인수는 미확인이다. 내부 API schema 자동완성과 실행값 자동 연결은 후속 개선 요구로 추가했다. [자연어 검색 조합·서비스별 API 관리 방향](tool-composition-and-service-catalog-20260923.md)을 기록했으며 A05는 미구현이다.


최신 화면: 최초 A04 화면 접속은 사용자 확인을 받았다. 이후 [화면 간소화](console-ux-20260923.md)와 새 로컬 DOM/API 대역 검사를 수행했다. 아래의 기존 브라우저 결과는 최초 화면 기록이다. 개선 화면 표시·후속 패치 Gateway 빌드/Healthy는 사용자 근거로 확인했으며, 검색 Tool 시험 성공 화면을 받았으며 게시·발행 후 실행은 아직 미확인이다.

판정: **구현·로컬 검증 완료, 실제 사용자 서버 인수 대기**. A03 서버 인수 완료를 A04 인수로 확대하지 않는다. [현재 적용 절차](openapi-foundation.md), [작업 순서](work-plan.md), [작은 결과 요약](../../src/platform/agent/tests/results/openapi-local-20260923.json)을 따른다.

## 실행한 검사

### 사용자 서버 후속 확인

`붙여넣은 텍스트 (1)(20260923-022956).txt`의 로그로 직접/OpenAPI 패치 22개 파일 적용, Gateway 이미지 빌드 완료와 `agent-platform-agent-gateway-1 Healthy`를 확인했다. `Cap 2026-09-23 11-28-10-306.png`는 접속 토큰이 비어 있는 연결 전 화면이다. 필수 입력 안내·비활성화된 도구 추가는 인증 전 상태이며 실제 등록·실행 성공 근거가 아니다.

같은 로그에서 `prepare_knowledge_tool_test.py --index-name test1 --check-search`가 prepared로 끝났다. Model 질문 임베딩 1회, lifecycle_writes 0, rerank false, 검색 API 직접 호출 passed/5개 결과다. `agent_tool_execution: not_run`을 그대로 유지한다. 준비 폴더는 `/home/shshin/workspace/knowledge_tool_test_20260923`이며 [화면 시험](tool-registration-20260923.md)에서 기존 파일을 사용한다. 질문·벡터·원문 검색 결과를 추가 검증 기록에 보관하지 않는다.

작은 기록은 [서버 확인 요약](../../src/platform/agent/tests/results/tool-registration-server-20260923.json)에 있다. 이후 `Cap 2026-09-23 11-38-57-376.png`와 사용자의 성공 확인에서 `mode: test`, `state: succeeded`를 확인했다. 이는 Agent를 통한 실제 검색 시험의 성공 근거다. 게시·발행 후 실행은 아직 미확인이다. 이 근거를 A04 전체 기능/DB·REST/MCP·재기동 인수나 A05 완료로 확대하지 않는다. 이번 문서 갱신으로 기존 Go/Python/GPU 검사를 재실행하지 않았다.

### 최초 A04 로컬 검사

| 검사 | 결과·확인 범위 |
|---|---|
| Go 전체 | 최상위 23개 PASS, native PostgreSQL 전용 TestDatabaseFoundation 1개 SKIP. 하위 사례 별도 포함 |
| Go build/vet | service/gateway/mcp/healthcheck 빌드 및 전체 vet PASS |
| 명세 분석 | JSON/YAML, 3.0/3.1 nullable, local ref, OR/무인증 override, 정밀한 수치 경계, Unicode, 미지원 키워드/인증/직렬화/참조/202 표시 |
| SQL·버전 | 원문 보존·중복 import·버전/연결 snapshot 고정·지원 불가 초안 거절·lease 유실 write=unknown·지연 결과 fencing·같은 키 재조회 |
| 실제 HTTP wire | Go service→별도 Python gRPC runtime→임시 HTTP 서버. path/query/body 동명 입력·null·헤더·호출 키 전달·JSON 결과 검증 |
| REST/MCP | 공식 Go MCP SDK 클라이언트, HTTP Tool 발행 전 시험 강제, 같은 키로 REST/MCP 호출 시 HTTP 효과 한 번, 같은 ToolCall/버전 유지 |
| 응답·실패 | 외부 202=external_pending, 쓰기 timeout/응답 schema 오류=unknown, 읽기 schema 오류=failed, redirect 미추종, 204, 300 KB 정상 응답 |
| Python | 13개 PASS. 진단/인증 회귀, API Key header/query, DNS 혼합/metadata 차단, path/query 인코딩, credential 반사 마스킹, 크기 제한·잘린 응답·연결 종료·쓰기 불명확성 |
| 운영 스크립트 | 설정/사전 검사/추가 credential 파일 5개 PASS. 전달용 A03 REST/MCP smoke·조회와 새 smoke_openapi.py도 로컬 실제 Gateway에서 PASS |
| 실제 브라우저 | Chromium/Playwright에서 업로드·미지원 표시·연결·초안·발행 전 성공 시험 요구·null 시험·발행·실행·비활성화·재로그인 후 조회 PASS. 접수 후 polling 연결 실패 시 재호출 대신 저장한 ID 조회 안내·복구 PASS. script error 없음, 토큰 local/session storage 없음. 1440px/390px에서 화면 확인·가로 넘침 없음 |
| Compose | Compose 2.39.4 정규화와 경계 검사 PASS. 상시 5개/선택 검사 1개, Gateway 포트만 공개, Agent DB/비밀/네트워크 독립 |
| 전달 패키지 | 적용 전 해시/경로 검증, A03→A04 적용·동일 패치 재적용, 충돌 시 사전 중단, 전체 소스/패치 결과 일치 검사 |

A04 대표 로컬 REST/MCP 호출은 `eda035ae-62b5-4221-a847-d37cd4baf88a`, 버전 `b26852fb-76b1-4849-83d8-57b8ca5aeedd`이다. 로컬 임시 DB의 값이며 사용자 서버 조회 명령에 사용하지 않는다. 브라우저 검사는 별도의 Tool/호출을 생성했다.

## 환경과 한계

로컬은 Go 1.26.8·실제 Python gRPC runtime·공식 MCP SDK 1.8.0과 보조 PGlite 0.5.8(PostgreSQL 18.3 WASM)을 사용했다. PGlite는 단일 backend이므로 검사 pool=1/DescribeExec/패키지 순차 실행을 사용했다. Docker daemon 및 native PostgreSQL 16/실제 Compose 재기동 검사는 여기서 실행하지 않았다. 서버의 checks profile이 native DB 권한/migration/SQL/실제 HTTP 경로를 검증한다.

이 환경은 loopback 인터페이스만 제공하여 로컬 fixture의 `fixture.agent.test` DNS만 별도 검사 bootstrap에서 127.0.0.1로 연결했다. 실제 HTTP 요청/응답과 gRPC/SQL은 실행했고, production 코드의 IP 검사는 변경하지 않았다. DNS/IP 차단은 독립 Python 검사로 확인했다. 서버 checks는 검사 컨테이너의 실제 사설 IP를 사용하므로 전체 production DNS/IP 경로 인수가 남아 있다. 네트워크·TLS·인증이 서로 다른 사용자 API 전체를 시험한 것은 아니다.

브라우저 엔진 기본 설치가 이 환경에서 실패해 별도 임시 Chromium 바이너리로 검사했다. 브라우저·PGlite·임시 DNS bootstrap·빌드 산출물은 제품/패치 의존성에 넣지 않는다. 서버에는 별도 프런트엔드 빌드나 브라우저 패키지 설치가 필요 없다.

## 수정 중 발견한 문제와 처리

HTTP/1.0 또는 Connection:close 응답이 완전히 읽힌 뒤 이미 닫힌 소켓에 timeout을 설정하던 오류를 재현하고, 응답 종료 상태를 확인하도록 수정했다. Content-Length보다 짧은 응답도 성공으로 처리하지 않도록 검사한다. 함께 발견한 화면 예제 JSON의 닫는 괄호 오류를 고쳤다. 수정 뒤 Python HTTP 검사·전체 Go 통합 검사·전달 smoke·브라우저 흐름이 통과했다. 중간 실패를 사용자 서버 실패로 기록하지 않는다.

## 서버에서 남은 인수

1. 패치 적용, Agent Go/Python 검사 이미지 및 a04.1 이미지 빌드, migration 003/5개 Healthy.
2. 기존 A03 ToolCall `8e3b3160-8e29-49d8-96d2-e0540e67682e` 읽기 조회 유지.
3. 실제 Gateway의 `ops/smoke_openapi.py`와 checks profile의 native PostgreSQL/HTTP 검사.
4. checks가 출력한 A04 HTTP call ID를 저장하고 5개 서비스 재기동 후 같은 기록 조회.
5. 사용자 브라우저 `/console`에서 본인 명세로 등록·시험·발행 확인.

완료된 GPU/OCR/전체 검색 평가는 반복하지 않았고 다른 플랫폼 실행 코드·DB·원본·인덱스·실패 자료를 변경하지 않았다. 기존 결과 요약과 합쳐 1 MiB 미만을 유지하며 원시 로그/스크린샷/생성 코드/비밀을 배포 소스에 넣지 않는다. A04 인수가 확인되면 상태 문서만 갱신한 뒤 A05 Knowledge 검색 Tool로 진행한다.

근거 규격: [OpenAPI 3.0.3](https://spec.openapis.org/oas/v3.0.3), [OpenAPI 3.1.1](https://spec.openapis.org/oas/v3.1.1.html), [공식 Go YAML 패키지](https://pkg.go.dev/go.yaml.in/yaml/v3). YAML 의존성은 `go.yaml.in/yaml/v3 v3.0.4`로 고정했다. 지원 부분은 구현 안내를 우선하며 전체 규격 지원으로 해석하지 않는다.
