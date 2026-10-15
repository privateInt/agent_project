# L01 구현 중간 독립 검토

판정: **REQUEST_CHANGES — 정적 검토에서 P1 한 건**.
검사·변이·실행 근거가 대기 중이므로 최종 승인 판정은 하지 않는다.

| 검토 대상 | 고정 값 |
|---|---|
| loader | `workspace/src/ops/tools/github_fixture_config.py` |
| loader SHA-256 | `06c1ad502c1bfeac63bd7a3cc600277115923cccbb47ff7d020f0770a465910d` |
| 부모 commit | `c91e9e628402fcc89c2daa4301d54c3c321221ed` |
| 승인 계획 | L01-D29-v1, SHA `b350192d28825de5761c448ddff6f73f00dcf778fceec164c2f2134eda353d50` |
| 사전 검토 | L01-D29-review-v2, SHA `1deb9bb9673684848bf3843d79628e02cf9d42c994445c7c78829ba89ac856e4` |

## P1: 기존 검증이 여는 빈 temp schema를 DB 경로 오류로 거부

문제 위치: `github_fixture_config.py:326`의 `_connected_inode`, 특히 329~330행.
기존 API와 연결되는 위치는 `_schemas`의 340~344행, `_check`의 388~391행,
load의 마지막 `handle._check()` 및 run/read 진입의 `self._check()`다.

`_connected_inode`는 `PRAGMA database_list`의 행이 정확히 하나이고 그 행이 main이어야
한다고 요구한다. 그러나 기존 `controller._connection`은 116~120행에서 main과 temp를
허용하고 `SELECT 1 FROM temp.sqlite_master LIMIT 1`을 실행한다. 기존 ledger의 `_schema`
역시 158~162행에서 temp를 조회하며 빈 temp namespace를 정상으로 허용한다.

SQLite에서 temp schema가 열려 `database_list`에 `(1, 'temp', '')`가 나타나는 정상 연결은
경로 변경 없이도 로더의 `len(databases) != 1` 조건에 걸린다. load의 마지막 `_schemas`가
이 temp 조회를 수행한 다음, 반환 handle의 첫 run/read는 `_connected_inode`부터 다시
검사하므로 controller에 도달하기 전에 DATABASE_PATH_INVALID로 거부될 수 있다.
즉 허용된 기존 API 자체가 만든 정상 연결 상태를 로더가 거부한다. 기존 controller도
바꾸지 않고 실제 연결을 완료해야 하는 L11·L13~L16 요구와 충돌한다.

이 발견은 source/API 경로의 정적 대조다. 이 reviewer가 SQLite query나 제품·검사를
실행해 얻은 재현 결과는 아니다. 실제 SQLite 버전에서의 행과 새 검사 결과는 test_agent의
기존 승인 검사 실행 근거에 귀속해야 한다.

필요한 수정은 로더의 연결 inode 검증으로 한정한다. main 행이 정확히 하나인지 찾고
그 main 파일만 pinned FD와 대조하며, 정상적인 빈 temp namespace를 기존 API와 같은
범위에서 허용해야 한다. 추가 attached DB나 temp 객체를 허용하도록 기존 schema·connection
검증을 약화하면 안 된다. 기존 audit/collector/ledger/controller 소스와 기존 검사는
변경하지 않는다. 새 항목을 늘리기보다 승인된 L11의 실제 연결과 L13의 load→run/read
경로가 확인할 문제다. 실패가 이미 실행됐다면 최초 로그를 보존하고 관련 실패 target만
재검증하며, 아직 실행 전이라면 그 사실과 수정 전후 source pin을 기록한다.

## 나머지 검토 범위와 상태

전체 loader와 계약 문서를 읽고, 입력/설치 pin/namespace 경로 대조, nofollow 경로 walk,
두 DB의 접속 전 header·sidecar 검사, 원래 FD와 inode 재검증, initialize=False schema
검증, 자원 소유·정리, 기존 controller로의 직접 전달을 승인 계획과 대조했다. 이 단계에서
확정해 보고할 다른 finding은 없다. 이를 해당 동작의 실제 통과나 누락 부재의 증명으로
확대하지 않는다. 신뢰된 process·stdlib·메모리와 같은 UID의 악성 TOCTOU가 범위 밖이라는
제한도 유지한다.

검토자가 작성한 파일은 본 문서뿐이다. 구현·검사·계약 파일을 고치지 않았고 제품 import,
테스트·변이 실행, credential/network 접근, 운영·권한 변경, 추가 위임·MCP 통지는 하지 않았다.
최종 PASS는 수정된 실제 diff와 test_agent의 새 검사·정확히 두 변이·보존/최초 실패 근거를
별도 지시 후 대조하는 단계까지 유보한다.
