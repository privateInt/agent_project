# Ops 측정 기반 수집과 명시 복구

2026-10-02 최신 사용자 제공 서버 관측 기준이다. **자동갱신 worker의 명시 복구와 새 정상 평가 1회를 확인했다. 이번 운영 범위는 복구 확인과 문서 정리로 마무리하며 백업 실행/복원과 추가 자동화는 보류한다.** 운영 plan과 격리 파일 복원 근거는 유지하지만 실제 크기 조사·최초 운영 세대·DB/image/application 복원은 미완료다. `backup_ready=false`다.

## 현재 결정과 복구 결과

진단 보완 7파일 적용 뒤 2026-10-02T02:24:24Z 새 `plan`은 PASS였다. 소유 서비스 9개·보호 running 55개·안정된 current inventory·backup helper 부재를 확인했다. 해당 review ID로 별도 명시 `apply`를 실행한 결과도 PASS였다.

| 확인 항목 | 마지막 관찰 결과 |
|---|---|
| 관찰 시점 | 2026-10-02 11:37:18 KST(02:37:18Z) |
| worker | 새 systemd invocation, `active/running`, `desired_state=running`, `recovery=null` |
| 평가 | `healthy`, 새 정상 평가 1회, 과거 관찰 재사용 없음 |
| 현재 설정 | 인증 readback 시도/검증 완료, 설정 변경 없음 |
| 실행 범위 | owned worker reset-failed/start 2회, 업무 서비스 제어·image build/pull·추론·백업·복원 없음 |
| 검증 한계 | 관찰 3.038초. 실제 재부팅·장기 안정성·최초 실패 원인은 미확인 |

[최신 복구 결과와 범위 결정](../testing/ops-worker-recovery-closure-observed-20261002.json)에 공개 JSON을 보존한다. 원래 실패와 앞선 제출 전 차단은 삭제하지 않는다. 이 문서 반영을 이유로 recovery apply·prepare·start/resume·완료한 격리 인수를 반복하지 않는다.

public `measure`는 helper/backend/새 lock/기록 생성 전에 `BACKUP_MEASURE_MAINTENANCE_REQUIRED`로 차단한다. worker가 paused인 것만으로 허용하지 않는다. 검토된 유지보수 구간의 크기 조사/worker 복귀 연결은 구현되지 않았으며 이번 범위에서 보류한다. 성공 출력의 `next_step=reviewed_maintenance_size_survey_required`는 기존 도구의 후속 항목이다. 현재 실행 지시는 [로드맵](../roadmap.md)의 사용자 결정을 따른다.

## 보존한 실패와 진단

이전 `auto_refresh.py status`는 Docker/Ops healthy·installation/configuration/provenance 정상·intent 부재, worker `OTHER_CONTAINER_CHANGED` failed/paused와 1,040회 완료 뒤 child 정리를 확인했다. 기존 boot 전환과 같은 boot였다. [이전 worker 상태](../testing/ops-changed-worker-observed-20261002.json)를 보존한다. 외부 Agent credential reference의 mode 경고는 blocker와 별개이며 변경하지 않았다.

전용 복구 9파일 적용 후 02:11:25Z plan이 PASS였지만 02:12:50Z 첫 apply는 `MAINTENANCE_RUNNING_SET_CHANGED`로 중단됐다. worker 제어 0회·readback 시도 0회이며, 코드상 새 recovery journal/start 제출 전 inventory 조회에서 차단된 것으로 판단한다. 당시 실패 phase/행/컨테이너는 미확정이다. [첫 검토/제출 전 차단](../testing/ops-changed-worker-inventory-observed-20261002.json)을 보존한다.

running 목록과 후속 inspect의 행 수·ID·Running·StartedAt 검사, 현재 inventory 차이 확인을 완화하지 않았다. 이번 복구 인스턴스에 고정 `phase`/`reason`과 0..128 또는 null 개수의 `running_inventory_diagnostic`을 추가했다. 이는 새 검토의 current discovery 차이이며 과거 worker 실패 baseline을 복원하지 않는다. ID·이름·경로·StartedAt·raw stdout/stderr·credential을 반환하지 않고 자동 재조회나 helper 예외를 추가하지 않는다. [진단 보완 검사](../testing/ops-changed-worker-inventory-offline-20261002.json)를 참고한다.

활성 worker의 `set_public_url.protect`는 샘플링한 타 컨테이너가 사라지거나 StartedAt이 달라지면 차단한다. worker가 짧은 측정 helper를 baseline에 포함했다가 제거 후 실패하는 경로를 대역 metadata로 재현했다. **실제 서버에서 처음 바뀐 컨테이너와 원인은 여전히 미확정**이다. 이번 복구 성공은 그 원인을 특정하거나 무해하다고 증명한 결과가 아니다.

## 복구 도구의 제어 계약

[recover_changed_worker.py](../../src/ops/tools/recover_changed_worker.py)는 기존 [recover_boot_worker.py](../../src/ops/tools/recover_boot_worker.py)의 one-shot receipt·journal·lease·기동 관찰을 재사용하되 `OTHER_CONTAINER_CHANGED`만 지원한다. 기존 boot 복구·maintenance·활성 worker 61파일과 보호 검사는 수정하지 않았다. 아래는 도구의 계약이며 이미 완료한 복구를 다시 수행할 지시가 아니다.

| 단계 | 동작과 조건 |
|---|---|
| `plan` | 같은 boot의 failed/exit-code 2/MainPID 0/빈 cgroup, stop token·실패 status·receipt, intent 부재, 설치 소유권/enable/unit/source 확인. 현재 소유 서비스·다른 running ID/StartedAt·설정 metadata·daemon/Docker binary를 두 번 샘플링해 review에 고정 |
| helper 검사 | reader label의 running/exited helper와 미복귀 유지보수 journal이 있으면 거부. helper를 채택·중지·삭제하지 않음 |
| 별도 `apply` | lease 아래 재검토하고 원래 실패를 먼저 보존. 현재 설정의 인증 readback 후 동일 owned worker만 reset-failed/start 한 번. 업무 서비스·설정·키 변경 없음 |
| 기동 인수 | 새 invocation·같은 boot·새 receipt/live process·정상 평가 확인. 현재 inventory/source/stop token 변화는 차단. 실제 재부팅 인수가 아님 |
| `check` | 이미 제출한 해당 recovery 결과 관찰. 기록/control 변경·start 재제출 없음. 다른 reason/불완전 journal/미확인 시작을 채택·재생하지 않음 |

Docker metadata 비교는 시점별 관찰이며 연속 또는 atomic snapshot을 뜻하지 않는다. 미래에 같은 오류가 발생하더라도 과거 검토 ID나 복구 제출을 재사용하지 않는다.

## 보류한 실행기의 계약

아래 측정·수집·명시 복귀 설명은 재개 시 검토할 기존 구현의 계약이다. 현재 운영 측정 파일·최초 세대·복원 인수는 없으며 지금 실행할 순서가 아니다. 새 실행기 연결 인수와 유지보수 조사 연결부터 범위를 정해야 한다.

## 앞선 측정 중단 이력

17파일 적용 후 최초 측정은 `BACKUP_MEASURE_PROTECTED_CHANGED`, 비교 보완 8파일 적용 후에는 `COLD_DOCKER_METADATA_FAILED`로 중단됐다. 각각 원본 출력에 비교 단계/실패 query가 없어 서버 원인은 미확정이다. [첫 실패](../testing/ops-backup-measure-protected-observed-20261002.json), [비교 보완 검사](../testing/ops-backup-measure-inventory-fix-offline-20261002.json), [Docker 실패](../testing/ops-backup-measure-docker-observed-20261002.json)를 보존한다.

helper recipe의 reviewed/validated 계약 해시 불일치와 optional map 필드 투영 누락은 별도 재현·수정했다. Docker 28.5.1 CLI와 loopback 대역 Engine에서 Health/Volumes 부재 오류를 재현했으며 volume 선언 거부와 helper image/label/mount/user/command/security 조건은 유지한다. [실제 CLI/오프라인 보완 검사](../testing/ops-backup-measure-docker-fix-offline-20261002.json)는 이번 서버 Docker 29.0.4의 실제 측정 인수와 구분한다.

낮은 수준의 크기 조사 adapter는 읽기 전용 UID helper로 stat/descriptor/xattr만 확인하며 file body를 읽지 않는다. 발견·등록·검증한 현재 helper만 정리하고 저장된 ID는 채택하지 않는다. 고정 phase/reason/제한된 개수의 `measure_diagnostic`, `docker_query_diagnostic`, `measure_cleanup_diagnostic`은 유지한다. raw stderr/경로/ID/credential을 반환하거나 오류를 무시·자동 재조회하지 않는다. 이 adapter를 운영 public 경로에서 다시 호출하려면 위 유지보수 연결을 먼저 구현해야 한다.

## 검토와 한도

측정 파일은 15분 동안 유효하다. 대상 container/image/owner/mount, 보호 대상 StartedAt, source metadata, 준비 정보, Docker binary와 adapter pins가 바뀌면 거부한다. 계획은 현재 worker·이력·제어 상태도 다시 확인한다.

조사 결과 확인 후 `plan --measurement-file`에 출력된 절대 경로를 지정한다. `plan`은 metadata 조회만 하고 `review_authorizes_execution=false`를 반환한다. 공간이 부족하면 `result=blocked`, `capture_available=false`다. 예약 포함 필요 공간과 가용 공간은 검토 ID에 고정된 원본 크기로 오인하지 않는다. 실행 직전에 현재 공간을 다시 확인한다.

| 제한 | 실제 적용 범위 |
|---|---|
| `per_storage_bytes` | UID 저장소 하나마다. 측정한 가장 큰 저장소에 기본 30% 여유를 더한 공통 상한 |
| `aggregate_file_bytes` | actor 파일 artifact 전체 합계. 폴더 개수만큼 곱하지 않음 |
| `aggregate_image_export_bytes` | 고유 image save stream 전체 합계. metadata 합계의 여유와 이미지별 16MiB 추가 |
| `max_entries` | 파일 artifact 전체 또는 저장소 하나의 개수. 여유+32, 최대 65,536 |
| 시간 | 저장소별 조사 최대 120초, 기존 capture 최대 3,600초, 서비스 stop 최대 60초 |
| 예약 | 암호문 예상 상한의 4배: 세대 2개·staging·복원 공간, audit 8MiB, 파일시스템 총량 10% 추가 보존 |

archive framing/manifest를 포함하고 압축 절약은 가정하지 않는다. 암호문 allowance는 계산 상한이며 실제 archive 검증은 아니다. 수집 중에도 공간을 관찰하고 실제 stream 한도를 강제한다. 공간 부족을 해결하려고 과거 세대나 데이터를 삭제하지 않는다.

종전 `backup_plan.py`는 파일/이미지 상한을 role별로 보수적으로 곱해 약 1.34TB를 출력했다. 검토된 generation 코드의 실제 제한은 파일·이미지 전체 합계다. 기존 plan/candidate/worker pins를 변경하지 않고 새 실행기의 계산에서 이를 바로 연결했다.

## 명령별 제어 범위

| 명령 | 동작과 조건 |
|---|---|
| `measure` | 유지보수 조사 연결 전까지 차단. helper/service/기록 변경 없음 |
| `plan` | 유효 측정 파일로 새 검토 ID/예약 공간 산출. 실행 승인 아님 |
| `capture` | 현재 측정 파일·검토 ID와 키 별도 보관/application drain에 대한 두 명시 확인 flag 필요. 먼저 worker를 정지/disable하고 live lease 아래 수집 |
| `status` | 현재 worker와 실행 기록 조회. 저장된 상태는 재실행 권한 아님 |
| `return` | 이번 job ID로 별도 worker 복귀. 운영 대상 healthy/identity와 ciphertext integrity 확인, 현재 설정 GET/auth readback, 새 receipt/invocation/evaluation 확인 |
| `check` | 이미 제출한 worker 복귀의 결과 관찰. start 재제출 없음 |
| `recovery-plan` | 현재 실패 job/worker token·이력·lease·container 상태를 읽기 전용 비교하고 별도 recovery 검토 ID 생성 |
| `recover` | 이번 recovery 검토 ID로 정지 근거가 있는 동일 container만 dependency 역순으로 한 번 시작. 실패 수집은 재실행하지 않고 worker는 paused 유지 |

capture의 확인 flag는 사용자의 별도 보관/쓰기 중단 확인을 **acknowledged**로 기록한다. 도구가 보관·drain을 자동 검증했다고 표시하지 않는다. 서비스의 정상 exit code 0/PID 0/no OOM과 새 FinishedAt은 확인하지만 애플리케이션 데이터 일관성 인수까지 대신하지 않는다.

성공 수집은 모든 component와 암호화 catalog, 같은 ID 서비스의 복귀, 현재 helper/child 종료가 확인된 뒤 receipt를 저장한다. plaintext archive를 디스크에 저장하지 않고 age로 stream한다. worker는 여전히 paused이며 별도 `return`이 필요하다.

실패·부분 세대·기존 오류·복구 전후 journal을 보존한다. 새 stop token/수정된 설정·소스·owner·image·mount·보호 대상, 열린 refresh intent, active worker/cgroup, 남은 helper 또는 알려지지 않은 시작 결과가 있으면 복구를 거부한다. 시작 요청을 journal에 먼저 적고, 응답이 불확실하면 다음 조회에서 새 healthy StartedAt을 증명할 수 있을 때만 관찰로 인정한다. 계속 정지 상태인 불확실한 요청은 반복하지 않는다. worker의 이미 armed/submitted start도 재생하지 않는다.

## 보존과 검증

- 기존 활성 worker 61파일 해시, 검토된 candidate pins, `backup_plan.py`·`backup_setup.py`·`maintenance_execute.py`를 변경하지 않는다.
- 이번 마무리 패키지는 현재 문서와 공개 서버 결과만 포함한다. host 도구·활성 worker·키·설정·DB·서비스를 변경하지 않는다. 작은 문서 원본은 패키지의 `before`에 포함해 rollback이 가능하며 프로젝트에 `.patch-backups`를 다시 만들지 않는다.
- [복구/측정 차단 오프라인 검사](../testing/ops-changed-worker-recovery-offline-20261002.json): 새 reason 복구와 기존 one-shot 경로, 현재 metadata inventory·helper 차단, 측정 사전 차단을 검증. 보고서 당시 실제 서버 인수는 미검증이었다. 이후 명시 worker 복구의 실제 서버 결과는 위 최신 근거로 구분한다. 새 백업 실행기 연결 인수는 여전히 미완료다.
- [오프라인 검사](../testing/ops-backup-execution-offline-20261002.json): 신규 30개, 전체 선택 검사 226개 중 179 통과·47 skip. skip은 native 도구/환경 미제공 범위다. native age generation 35개는 별도 실행 시 도구 부재로 수행 불가였고 최종 offline suite에서 제외했다.
- 기존 [격리 native 생성/파일 복원 16/16](../testing/ops-backup-generation-native-server-20261001.json)은 코어 인수 근거로 유지한다. 새 운영 어댑터의 native 연결 인수로 표시하지 않는다.
- 실제 age 인증 복호 EOF, 실제 DB 복원·image import·서비스 기동·지표/trace/설정 비교는 최초 운영 세대 이후 별도 작업이다. 전 플랫폼 복원 완료도 아니다.

코드: [실행기](../../src/ops/tools/backup_execute.py), [크기 조사](../../src/ops/tools/backup_measure.py), [검사](../../src/ops/tools/tests/test_backup_execute.py). 최신 worker 관측: [명시 복구/새 평가 확인](../testing/ops-worker-recovery-closure-observed-20261002.json). 이전 상태: [실패/paused와 Docker 정상](../testing/ops-changed-worker-observed-20261002.json). 과거 관측: [서버 plan PASS](../testing/ops-backup-plan-passed-server-20261002.json). 전체 진행은 [상태](../status.md)와 [로드맵](../roadmap.md)을 따른다.
