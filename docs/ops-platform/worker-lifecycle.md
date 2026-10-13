# Ops worker 수명주기와 복구 계약

2026-10-02 마지막 사용자 제공 복구 결과에서 active/running·desired=running·recovery=null, 새 invocation 정상 평가 1회와 현재 설정 readback을 확인했다. 관찰은 3.038초이며 현재 서버 재조회·실제 재부팅·장기 안정성의 근거가 아니다. [서버 원문](../testing/ops-worker-recovery-closure-observed-20261002.json), [상태표](../status.md)를 따른다. 이번 운영 범위는 마무리됐으며 다음 업무는 문서 QA다.

## 명령과 효과

모든 명령은 프로젝트 루트의 별도 Python 프로세스로 수행한다. 아래는 코드 인터페이스이며 이번 문서 패치에서 실행할 명령 목록이 아니다. 기존 설정/키·source binding·정책·사용자 manager를 재사용한다.

| 코드/명령 | 현재 효과 |
|---|---|
| [auto_refresh.py](../../ops_platform/ops/auto_refresh.py) `plan/status` | 설치·manager·Docker·provenance·worker 상태 metadata 조회. 환경 준비와 실제 정상 평가 구분 |
| 같은 CLI `install/start/stop/resume/remove` | 소유한 user unit·영속 desired/recovery·lease·현재 invocation을 확인한 서비스 제어. 실패/불확실 상태를 무시한 반복 시작 불가 |
| [boot_control.py](../../ops_tools/boot_control.py) `plan/status` | 검토된 source와 현재 전환/기동·정책·소유권 상태 확인 |
| 같은 CLI `apply/finish/rollback` | 검토 ID·journal·현재 bytes/binding 검증. 종료/lease/cgroup 확인 뒤 전환/복원, paused/disabled 유지 |
| 같은 CLI `start/check/cancel/stop/resume-session` | 각 현재 phase의 기동·관찰·취소·영속 정지·원본 session 복귀. check는 순수 GET이 아니며 이미 제출한 start 재제출 없음 |
| [recover_changed_worker.py](../../ops_tools/recover_changed_worker.py) `plan/apply/check` | OTHER_CONTAINER_CHANGED의 소유 failed boot worker·같은 boot·현재 inventory/보호 running/helper 상태를 검토한 별도 one-shot 복구 |

worker의 단일 실행 lease·현재 PID/start ticks/boot ID·systemd InvocationID를 함께 확인한다. 과거 accepted 관찰을 현재 healthy로 재사용하지 않는다. stop은 durable pause를 먼저 기록하고 자기 unit/cgroup만 대상으로 한다. recoveryRequired/unknown outcome/새 stop token/변경 설정·image/mount/source/보호 대상이면 기존 실행권을 그대로 재사용하지 않는다.

`persistence=boot`와 enable/linger는 설정된 지속 정책이다. 실제 reboot acceptance와 다르다. 설치에 따른 owner/path·provenance·binding 검증과 logind runtime 선택을 유지한다. 자격증명은 기존 참조로 조회에 사용하며 mode 경고만으로 키 내용 복사·chmod·재생성을 하지 않는다.

## 출처와 오류

활성 worker Python 61파일 기준 SHA-256은 `2bf14042a6aa15e4e12b46aa7be494eef2b8834c9b44c4a5587610d5d3a3bb1f`다. [production manifest](../../ops_tools/backup-production-plan-20261001.json), boot 후보/전환/기동 manifest와 source pin은 보존한다. candidate는 문서처럼 보이더라도 실제 import/무결성 비교 대상이므로 중복 파일 정리 대상이 아니다.

원래 OTHER_CONTAINER_CHANGED와 첫 apply 제출 전 MAINTENANCE_RUNNING_SET_CHANGED를 [실패](../testing/ops-changed-worker-observed-20261002.json), [비교 진단](../testing/ops-changed-worker-inventory-observed-20261002.json)에 남긴다. 이후 안정된 inventory 검토의 apply는 통과했지만 최초 변경 컨테이너·원인은 미확정이다. helper 충돌의 로컬 재현을 실제 서버 원인으로 단정하지 않는다.

정상 worker의 현재 업무 제어와 격리 검사/과거 전환 명령을 혼동하지 않는다. 필요할 때 현재 상태와 phase를 먼저 조회하며 이번 정리 때문에 새 plan/apply/start/resume/native 인수나 호스트 재부팅을 실행하지 않는다. 백업 public measure는 별도 차단/보류 상태다. 상세 옛 전환 계약·검사·복구 기록은 [압축 이력 목차](../history/ops-evidence-catalog.md), 운영 도구 역할은 [ops_tools](../../ops_tools/README.md)에 있다.
