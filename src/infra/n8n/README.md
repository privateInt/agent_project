# infra n8n

**2026-09-24 최신 서버 확인:** accessfix 적용 후 n8n 최초 계정 등록·API 키 연결과 운영 Flow 등록/게시가 성공했다. 첫 운영 질문은 validate/search/context/generate 성공 후 finalize에서 `ANSWER_REFERENCE_INVALID`로 실패했다. 인용 누락·잘못된 형식·제공 범위 밖 ID 중 정확한 원인은 현재 기록만으로 확정할 수 없다. 출처 검증을 유지하며 인용 형식 준수를 품질 후속에 기록한다. 운영 질문의 답변 전달 성공·재실행 인수는 미완료다. `set -e`로 두 번째 run과 별도 lookup은 실행되지 않았으며 첫 run 내부 조회가 위 실패를 확인했다.

**최신 서버 확인:** DB 권한 복구 후 PostgreSQL/n8n Healthy와 원래 키/볼륨 보존을 확인했다. 09:24 후속 로그에서 검사 이미지 a07.3 빌드와 격리 n8n CLI 검사 7개도 통과했다. [검증 근거·오류 이력](../../../docs/agent-platform/n8n-checks-fix-20260924.md)을 따른다. 아래 unhealthy·파일 접근 오류·미실행 서술은 이전 로그의 이력이다. 운영 기동과 통과한 검사를 반복하지 않는다.

n8n과 전용 PostgreSQL은 공유 infra가 관리합니다. Agent 연동 코드·워크플로 템플릿은 `src/platform/agent/n8n`에 있습니다.

프로젝트 루트에서 `python3 src/infra/ops/up.py`로 infra 전체를 초기 기동합니다. 기존 A07 환경의 n8n만 이전/실행할 때는 `python3 src/infra/ops/up.py --n8n-only`를 사용합니다. 기존 설정·키·볼륨을 가져오고 이전 컨테이너를 중지한 뒤 새 프로젝트에서 재사용합니다. 원본 컨테이너와 볼륨을 삭제하지 않습니다.

상세 사용법·보존/복구·서버 unhealthy 진단·확인 보류 항목은 [이전 안내](../../../docs/agent-platform/n8n-infra-20260924.md)를 따르세요. Agent 자체 재빌드는 필요 없습니다.

## 화면 없는 Agent 연동 계정 연결

프로젝트 루트에서 `python3 src/infra/n8n/ops/api_access.py`를 실행하면 기존 소유자/관리자로 로그인해 연동 API 키를 준비한다. 최초 n8n은 사용자가 입력한 계정으로 초기 등록한다. 비밀번호는 저장하지 않으며 기존 계정 초기화·DB 직접 변경은 하지 않는다. API 키는 infra 비밀 파일에 보관한다. [운영 워크플로 등록·실행](../../../docs/agent-platform/n8n-operational-20260924.md)을 따른다. 실행 구성·볼륨·암호화 키를 바꾸거나 n8n을 재시작할 필요는 없다.

계정 연결 HTTP 400 후속: `python3 src/infra/n8n/ops/api_access.py --diagnose`로 계정 등록 필요 여부와 로컬 키/미확정 발급 기록의 존재를 읽기 전용으로 확인할 수 있다. 기본 등록 비밀번호는 8~64자·영문 대문자·숫자를 각각 포함해야 하며 서버의 최소 길이 정책을 따른다. 오류는 등록/로그인/키 발급 단계를 구분하고 민감한 응답 원문은 출력하지 않는다. 기존 계정이 등록됐다면 같은 계정으로 로그인한다. 상세 적용법과 검증 범위는 위 운영 안내의 HTTP 400 후속 절을 따른다.
