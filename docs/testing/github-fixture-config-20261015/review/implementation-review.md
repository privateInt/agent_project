# L01 구현 중간 독립 검토

상태: **PENDING_EXECUTION_EVIDENCE — 현재 정적 검토의 미해결 finding 없음**.
최종 PASS를 내리지 않는다. 새 검사·D38·실행 및 보존 근거의 최종 검토는 별도 단계다.

| 대상 | 고정 값 |
|---|---|
| 현재 loader SHA-256 | `487dc146b24b302794dc650128f6aca1349e887e28946976148389dfb603e122` |
| 최초 검토 loader SHA-256 | `06c1ad502c1bfeac63bd7a3cc600277115923cccbb47ff7d020f0770a465910d` |
| 부모 commit | `c91e9e628402fcc89c2daa4301d54c3c321221ed` |
| 승인 계획 | L01-D29-v1, SHA `b350192d28825de5761c448ddff6f73f00dcf778fceec164c2f2134eda353d50` |
| 사전 검토 SHA | `1deb9bb9673684848bf3843d79628e02cf9d42c994445c7c78829ba89ac856e4` |
| 최초 구현 검토 사본 | `review/implementation-review-first.md`, SHA `1e2b4dc80c8cf02bb6ab60c72bdbcf4a480fabb457aa14c3130c20f143e375e9` |

## 최초 P1의 현재 상태: 정적 수정 확인, 실행 검증 대기

최초 pin의 `_connected_inode`는 database_list 한 행만 허용해, 기존 API가 조회하는
정상적인 빈 temp namespace가 나타난 연결을 거부하는 문제가 있었다. 최초 발견과
이유는 보존 사본에 남겼다. 이는 이 reviewer의 제품 실행으로 관찰한 실패가 아니다.

현재 326~337행은 main 행을 따로 찾아 정확히 하나임을 확인하고 그 main inode만 pinned
FD에 결합한다. main/temp 외의 DB는 계속 거부한다. 빈 temp namespace를 허용하되 실제
temp 객체와 다른 attached DB는 기존 `controller._preflight`와 양쪽 schema 검증이 계속
거부한다. 기존 audit/collector/ledger/controller 네 source SHA는 기존 pin과 일치한다.

텍스트만 읽어 위 guard 변경을 원래 표현으로 되돌린 메모리 사본의 SHA가 최초 source
`06c1ad50…`와 일치함을 확인했다. 현재 `487dc146…`와의 차이는 이 guard와 설명뿐이다.
파일을 되돌리거나 제품을 import/실행한 것이 아니다. 계획의 보안 조건이나 기존
schema 검증을 완화하지 않고 최초 문제를 해소하는 수정으로 판단한다. 실행상 해소는
test_agent의 승인된 L11·L13 등 새 실제 연결 근거에서 확인해야 한다.

`evidence/implementation-ready-1.json`은 최초 pin, 현재 `implementation-ready.json`은
새 pin을 가리킨다. 현재 marker는 2026-10-15T13:33:09.701292+00:00와 최초 실행 전
코드 조사 수정 사유를 기록한다. 두 marker는 보존돼 있다. 이 기록을 실제 검사 PASS로
세지 않으며, 실행이 어느 pin에서 시작됐는지는 실제 runner 결과와 로그로 별도 대조한다.

## 정적 대조 범위와 남은 검토

전체 최초 loader·계약과 수정된 guard를 승인 계획·기존 API에 대조했다. 입력·설치 pin,
동일 runtime 설치와 namespace, nofollow 경로 walk, 두 DB의 접속 전 sidecar/header 검사,
원래 FD와 inode 재검증, initialize=False schema 검증, 자원 소유·정리, controller로의
직접 전달을 읽었다. 현재 확정해 보고할 다른 finding은 없다. 실제 실행 통과나 남은 결함
부재를 뜻하지 않는다. 신뢰한 process/stdlib/메모리 및 같은 UID의 악성 TOCTOU 제한을
유지하며 native·owner 승인·운영 설치·issuer/서명/required-check는 범위 밖이다.

검사·변이·메타데이터 도구 실패가 생기면 각 최초 원문과 실행 pin을 별도로 보존한다.
정상 참조를 얻기 위해 기존13+3·R03/R04/R15·전체 suite를 반복할 필요는 없다.
최종 검토는 새 검사와 정확히 두 변이의 실제 소스/검사 pin, 최초 실패 및 한정된 재검증,
bytes/hash/binding 재조회와 보존 근거를 받은 뒤에 수행한다.

reviewer는 본 중간 검토와 최초 검토 보존 사본만 작성했다. 제품/검사/계약을 수정하지
않았고 제품 import·제품/검사/변이 실행, credential/network 접근, 운영·권한 변경,
추가 위임·MCP 통지를 하지 않았다. 증거 디렉터리 조회 시에는 구현 시작·준비 marker만
보였으며, 타 담당의 미게시 실행 여부까지 추정하지 않는다.
