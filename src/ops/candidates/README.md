# Ops 후보

보류 중인 후보와 당시 manifest·source payload를 보관한다. manifest와 source payload 안의 `ops_platform`, `ops_tools`, `infra` 경로는 과거 구조다. 현재 경로를 이유로 과거 hash를 다시 쓰지 않는다.

공통 Ops 화면·API는 `../platform`, 호스트 실행 도구는 `../tools`에 있다. 이전 후보 검사에 남아 있는 scope/hash 불일치는 활성화 전에 별도 검토해야 한다. 후보 활성화·운영 백업/복원·재부팅은 보류 상태다. [구조와 검증 범위](../../../docs/architecture/source-layout.md)를 따른다.
