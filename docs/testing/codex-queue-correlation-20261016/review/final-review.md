# U01b2 최종 독립 검토

판정: **PASS — 고정 PR18 위의 오프라인 queue/list 관찰기라는 한정된 로컬 단위가 완료 기준을 충족한다.** 수정이 필요한 U01b2 제품 결함이나 실행 근거의 차단 사항은 발견하지 않았다. owner/provenance·native 접수·OS enforcement·controller 연결·두 실제 작업의 인수 승인이 아니다.

| 확인 대상 | 정확한 SHA-256 |
|---|---|
| 제품 `correlation-development/src/ops/tools/codex_unattended_queue_observer.py` | `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a` |
| 검사 `correlation-development/src/ops/tools/tests/test_codex_unattended_queue_observer.py` | `5555695acfd663b1d23092490db93d6a1e1e27d6e595f0c7c0e46f4ef11cb371` |
| 실제 runner `transition-evidence/correlation-run-1/run-once.py` | `704ce24042dc81db2978bc9b955656249c622d1a69c87cb86e202e1165defa37` |
| 승인 v2 계획 | `25c798b43efd07b01c8c9030c0de7ff41a04b94cab8dd62dc4462ed13025403f` |
| 실제 `correlation-run-1/result.json` | `c8a77dfebf70b4098d6e0c98d1d44aa7a116af6e77c288f93a074ecf0047d39d` |
| 정상 111개 `observations.json` | `1fe19f8bf235ad17e869eefc3a11c6d6453573116345ab23f4a46c37e950a2f0` |
| 실행 근거 `evidence-sha256.json` | `3b64a8994aaa2b5e8c27e1d658a8268466a5cb975da4ce41635d7181c7a24f67` |
| 검토 당시 observed JSON | `fa670a1fd1b7c46ec3972ed47b99b94567b9643c64d01488d842fd0dcb98ec92` |

기준 commit은 `780d0755378b9f95f0282ec766ca99300b2b0b53`이다. 전체 소스·검사와 수정 diff를 읽은 기존 정적/D06/최초 실행 전 결론을 변경 없는 정확한 pin으로 재사용하고, 이번에는 실제 결과·raw log·관찰·변이 원문과 diff·실행 입력·문서를 대조했다. 재사용 검토 pin은 사전 검토 `29709c6c34feaf5fa673f325e43855b9dc44ab761beb1d815e9ef01997fdb5ac`, 정적 검토 `3ca50732bb902629f06d58273f2283933c5a9dee46cbc8dbdaaa8d9fccc7e723`, 검사 D06 `f75cb20fd91d507f2eb74a73719f69cdd3515d72c63fe29c6c4b14582e28c665`, runner D06 `39b1eaae19d9428822e996db7886c1be52b41aa33443b04e405851b5e15c03f5`, 실제 bytes 확인 `16c770f75a9a16fbd20dc4945f8eae1846aec56215ffaa77d847b9fc65949f20`이다.

실제 검증 결과:

- 최초 네 method는 각 한 번 **4 PASS / FAIL0 / ERROR0 / SKIP0**다. `first-four.log`와 normal-result/result JSON이 일치한다. 정상·거부 관찰은 111개이며 public read0이 37개, read1이 74개다. 제출 JSON 오류 read0/INPUT_INVALID와 요청·응답 JSON 오류 read1/OBSERVATION_INVALID가 실제 기록에 구분돼 있다. 기존 PASS와 전체 suite 재실행은 없다.
- **M1:** source `5120741bc692434b19acb40ba8ac4cef6fad8898258a29075d72cea7e7f45070`에서 item text 비교만 False로 바꿨다. 지정 target 한 번이 semantic **FAIL1/ERROR0/SKIP0**이며, CONTENT_MISMATCH 대신 실제 CANDIDATE_OBSERVED와 원래 synthetic queue ID가 반환됐다. result pin `6e219ac0c7e52d68e407d2ac5fd2eec091bb922c688fcbd615aa1fbde03437ea`.
- **M2:** source `771a561f8bd3512d6d0d3aadfd892434e561508fc5ff2054581703dfb8a22717`에서 유일성 비교만 `>= 1`로 바꿨다. 정상 두 page의 서로 다른 두 queue ID가 있는 지정 target 한 번이 semantic **FAIL1/ERROR0/SKIP0**이며 실제 첫 후보를 잘못 반환했다. result pin `e3eeb40fc5b79ba785e2e94e32a4bccb15201c168d80b0e1c95ad0e1f5a9b21e`.
- 두 변이는 모두 공통 helper 243행의 reason assertion에서 실패했으며 import/SQL/harness/flag 오류가 아니다. raw log와 실제 반환 candidate를 확인했다. 정상 target PASS는 최초 네 개의 결과를 재사용했다. 변이 diff는 각각 지정 비교 한 곳뿐이다.
- 111개 정상 관찰 모두 DB 보존=true, 외부 IO 시도=0, 직접 SQL=0이고 모든 origin/acceptance/remote/replay/native-ready flag=false다. 통과한 helper는 SQL trace 허용문·설정 불변·file bytes/row/raw/hash/revision/profile 및 정상 scope 종료 후 close/reopen을 검사한다. PREPARED0 사례 하나는 계획대로 INTENT_NOT_UNKNOWN으로 차단된다. UNKNOWN1 상태는 어느 관측 결과에서도 해제되거나 재claim·replay되지 않는다.

실제 실행 범위는 Python **3.10.12**, stdlib SQLite **3.37.2**의 전용 임시 파일 DB와 borrowed 연결의 기존 공개 `read_intent`다. 실제 RPC·native queue를 실행하지 않았다. generated schema 세 개와 모든 queue/list 데이터는 상대 경로의 hash 고정 합성 fixture이며, 입력 출처·원격 접수·현재 유일성·신선도·원자적 pagination을 입증하지 않는다.

포터빌리티·보존 확인: `execution-pins.json`의 모든 입력을 현재 correlation-development와 실행한 새 경로 `/tmp/u01b2-portable-source-a0u5n23r`에서 hash 대조해 일치를 확인했다. 보존 execution-inputs와 raw log·변이·수정 전 사본 등 evidence manifest의 전 항목도 hash가 일치한다. 최초 네 검사가 처음부터 이 독립 소스 사본과 상대 fixture에서 수행됐고 기존 private fixture나 `/opt` 의존으로 통과했다는 근거는 없다. 이 판단은 테스트의 IO 차단과 runner portability guard 범위이며 OS 격리 인수가 아니다.

원본 checkout의 11개 파일을 test-writing baseline과 실제 hash 대조해 모두 일치함을 확인했다. Git 상태도 같은 11개이며 HEAD `b1fda4916041fde8438251b04db62e6ff702c24f`, branch `codex/resume-preparation-20261015`가 보존돼 있다. 이전 intent-run-1 보존 manifest 전 항목과 최초 실패 log/관찰은 그대로이며, intent-run-2 결과 pin `4d54222162084ab4de0504b022c84ce0c3739741c0c4b6d49261b38dee544d3b`도 일치한다. 이번 U01b2에는 제품/검사 실행 실패가 없었다. 별도 보존된 정적 metadata traversal AttributeError는 실행 전 audit 오류로 구분하며 이를 검사 성공이나 제품 실행 실패로 바꾸지 않는다.

현재 추가된 HANDOFF/status/roadmap/development 네 문단과 observed JSON은 오프라인 범위·실제 네 PASS·두 의미 변이·남은 live 연결을 구분한다. observed JSON의 final_independent_review=PENDING은 이 판정 이후 보고서 pin을 연결하는 metadata 갱신 대상이다. 제품·검사·실행 근거를 다시 바꾸거나 재실행할 사유는 아니다.

**별도 owner 유틸리티 경계:** `owner-metadata-prepared.json`은 NOT_RUN_D18_HELD 준비 기록이다. source `10f8d9c83162dcdfbca15490fbdead0f93aa5221168a49b6b815321d74d01c64`의 독립 정적 보고서 `317f90ec49017cd8db0a3907ffa16ddcd7dcb2727eb1aa9d54022c72ea4dad75`에는 원문 thread ID 저장·예외 출력 경로·전체 시간 제한의 수정 필요 세 건이 있다. 이 별도 도구는 본 PASS에 포함되지 않는다. observed JSON의 owner 항목에 남은 “static review pending”은 해당 실제 판정으로 metadata를 갱신해야 한다. 준비 기록이나 초기/부분 receipt는 완료된 owner 인수·native 접수·실행 권한이 아니다. 이 검토에서 owner 명령을 실행하지 않았다.

**미실행/후속 경계:** 실제 ACK 저장·turn/result/controller 연결, 두 full native jobs, owner/provenance 승인, OS enforcement와 native acceptance는 **NOT_RUN/미인수**다. runtime D18 HELD와 scheduler/root/user PAUSE, vms 제외·운영/인증정보/main 보호·기존 조사 상한은 유지된다. 다음 연결에는 승인된 caller의 thread/params/RPC ID 출처 결합과 별도 최소 계획·실행 승인 및 실제 인수가 필요하며 본 로컬 PASS가 그 권한을 부여하지 않는다.

reviewer는 검사/import/DB/native/runtime/probe·다른 agent 조회·위임·통지를 하지 않았다. 기존 결과를 읽고 hash만 확인했으며 이 최종 보고서 외에는 수정하지 않았다. 공유 시작 2026-10-16 14:45:08Z, 보수적 마감 15:45:08Z를 유지하고 시계를 재설정하지 않는다.
