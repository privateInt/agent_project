# U01a 순수 admission 계획 독립 사전 검토

판정: **PASS — 아래 exact pin의 U01a-D29-v1 계획에 한정**.

| 검토 대상 | SHA-256 |
|---|---|
| `/tmp/agent-unattended-admission-plan.md` | `321c5c9f8fa01246aeb627677505bc5581cc390174b47279b10f4760d489f410` |
| 기준 `/tmp/agent-unattended-plan-review.md` | `498f7eda206c8b6f2c04b4d6626975f8383dfc441c1616a7ebc00d6d46aeca29` |

기준 검토에서 요구한 가장 작은 U01a로 범위를 줄였으며 수정 요구 사항은 없다.
공개 함수는 외부 supplied config/snapshot/state와 고정 순수 schedule dependency만 받아
후보·고정 reason·미적용 설정 제안을 반환한다. 파일·clock·socket·subprocess·queue·turn·
settings IO와 durable journal/CAS/fsync·운영 entrypoint는 제외된다. setup의 고정 함수 로딩은
선택 함수 내부 IO와 구분한다. 출력은 실행 또는 mutation 권한을 부여하지 않는다.

정확한 project/root/common-dir/base 결합, root→user→state PAUSE 우선순위,
필수 freshness 정책과 미래·rollback·경계값, 명시 quota/control/settings 조건,
occupied slot 최대1·실패/불명도 포함한 lifetime 최대2가 고정됐다.
T2는 T1 terminal뿐 아니라 같은 binding의 필수 검사·독립 review·Draft PR·게시 head/readback을
모두 요구한다. UNKNOWN은 후속 후보를 차단한다. all-false granular와 auto_review는 제안이며
지원 불명은 차단한다. 문자열 pin·bundle 결합은 realpath/서명/OS 격리/출처 인증이 아니다.

검사 범위는 계획의 **새 6 methods와 정확히 2 mutants**로 승인한다.
M1은 다른 조건을 모두 충족한 세 번째 후보의 lifetime cap 생략,
M2는 다른 조건을 모두 충족하고 review만 PENDING인 T2의 완료 guard 생략이다.
각 단일 target의 정상 최초 PASS 재사용 및 변이 semantic FAIL1/ERROR0을 요구하므로
다른 guard·fixture ERROR를 변이 검출로 오인하지 않는다. 실제 dispatch는 없다.
기존 통과 검사 반복이나 새 검사 항목을 추가하지 않는다.

이는 계획 사전 검토 PASS다. 구현·6 methods·2 mutants·최종 독립 검토가 수행됐다는
실행 PASS가 아니다. 실제 daemon/두 작업·권한 강제·출처 증명은 NOT_RUN이다.
root/user PAUSE, vms 제외, 기존 로컬 변경·최초 오류·이전 결과와 sandbox 조사 상한 및
기존 native 조사 보류를 유지한다. 사용자 재승인이나 운영 활성화를 요구·허가하지 않는다.

리뷰어는 두 계획 문서를 읽고 해시를 확인한 뒤 이 검토 파일만 작성했다.
소스·검사·계획 변경, 검사 실행, runtime/credential/운영 접근, 추가 위임·통지는 수행하지 않았다.
