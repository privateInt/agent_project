판정: **PASS**
v2 SHA-256: `4af3d60f188559f59e93bac3da432f1b32a96bf2ea772088e29c35a78bc5052f`

보존된 `test-plan.md`, `plan-addendum.md`, `plan-review.md`와 v2를 결합하면 기존 지적 6건과 semantic mutant 요구가 계획 수준에서 구체적으로 해소됐다. 원 계획·부록의 SHA는 기존 검토 기록과 일치한다. 제품 구현·인수 통과 판정은 아니다.

1. **신뢰 시작점:** 외부 launcher SHA 선검증, `Python -I -S -B`, 제품 import 전 전체 manifest 검증, 외부 commit·UID 기대값, detached manifest SHA 및 경로·중복·링크 거부가 명시됐다.
2. **경로·소유권:** 기존 group-write 상위 경로 후보를 직접 workspace 후보로 대체한다. 전체 ancestor의 UID·mode·no-follow, 기존 root-owned sticky `/tmp` 예외, 임시 root 0700을 구분하며 실제 후보 생성·원본 chmod는 하지 않는다.
3. **DB 5개:** ledger/controller/intents/bindings/receipts 모두 파일 속성·독립 inode·정확한 SQL DDL·`application_id`·`user_version` 검증 대상이다. 계약은 기준 모듈 AST에서 추출해 manifest에 포함한다. 기존 공개 초기화 API는 `init-new`에서만 각각 1회 사용하며, 부분 실패·불완전 상태는 보존하고 자동 생성·migration 없이 거부한다.
4. **실제 fixture 흐름:** 기존 `load_fixture_controller` 및 `run/read`, 실제 collector를 사용한다. 두 assignment의 원자 교체·0600·bytes/hash, 각 job의 pin·claim/revision·artifact 및 실행 횟수를 관찰한다. UNKNOWN 재조회는 collector 실행과 DB 변경이 없어야 한다.
5. **실행 제한:** 세 PAUSE와 schedule을 assignment 기록·admission·claim·child보다 먼저 검사한다. 고정 정책·시간대·시각 주입, invalid/closed schedule, 최대 3회 제한, admissions checkpoint·clock floor를 구체화했다. 실제 전역 PAUSE는 변경하지 않는다.
6. **stage·rollback:** 같은 private parent에서 검증 후 `renameat2(RENAME_NOREPLACE)`로 공개한다. 설치된 `/usr/include/stdio.h`의 함수 선언과 `/usr/include/linux/fs.h`의 `RENAME_NOREPLACE=(1<<0)` 정의를 읽어 확인했다. fallback은 없으며 unsupported·충돌·복사 실패를 고정 실패로 처리한다. 최종 config 경로 확인과 exclusive/nonblocking lock 아래 rollback·DB/파일 보존도 명시됐다.

semantic mutant는 기존 loader의 7개 소스 pin 밖 불변 파일을 동일 크기로 변경하고 새 manifest 검증만 우회한다. 정상 거부, mutant의 잘못된 `VERIFIED` 수락 witness, 지정 assertion **FAIL 1 / ERROR 0**을 요구하므로 의미상 검출 조건이 명확하다.

시험에서 사용하는 **개발 launcher 바이트·별도 SHA**는 향후 새 검토 커밋이 제공할 **최종 Git 고정 launcher**와 구분해야 한다. 최종 manifest와 code-version은 최종 검토 소스 커밋에서 재생성하고 commit/version/manifest 연결을 별도로 검증한다. 기존 동작 검사는 재실행하지 않는다.

native adapter는 없으며 공식 CLI queue 대안은 명령 준비만 한다. native dispatch와 실제 native 작업 2개는 **NOT_RUN**이다. 고정 마감 **05:00Z**와 미완료 항목 표기를 유지한다.

기존 승인 코드 재검토, import·제품 실행·테스트·socket/RPC·네트워크·다른 에이전트 호출·파일 편집·운영 설치·PAUSE 변경은 수행하지 않았다.
