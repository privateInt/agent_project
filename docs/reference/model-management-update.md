# 모델 이름·가져오기 진행률·배포 삭제

기준일: 2026-09-18. 최초 소스의 서버 Go/DB 검사·Python 25+5+42개와 서비스 교체를 확인했다. 실삭제에서 Unavailable이 발생해 production fetcher의 캐시 삭제 연결을 보완했다. 후속 수정 뒤 사용자가 “이제 삭제 된다”고 확인했다. 개별 새 Go/빌드 로그와 cache 파일시스템 관찰은 미수령이며 사용자 기능 확인과 구분한다. 적용·검증은 [실행 안내](../testing/model-management-update.md)를 따른다.

## 이름 수정

모델 목록의 **이름 수정**에서 등록 이름과 선택한 버전 이름을 각각 저장한다. 편집을 열 때 선택한 버전 UUID를 고정한다. 행의 버전 선택을 바꿔도 열린 편집 대상은 바뀌지 않으며, 패널에 대상 버전 이름과 ID 일부를 표시한다. 다른 버전을 수정하려면 해당 버전을 선택하고 이름 수정을 다시 연다.

| REST | 요청 | 응답 |
|---|---|---|
| `PATCH /api/models/:id` | `name`, `expected_name` | 모델 요약, 200 |
| `PATCH /api/model-versions/:id` | `version`, `expected_version` | 버전 요약, 200 |

gRPC는 Management의 `RenameModel`, `RenameModelVersion`이다. 등록 이름은 공백 제거 후 1~200자, 버전은 1~128자이며 제어문자는 거절한다. 기존 모델 이름은 전체 카탈로그에서, 버전 이름은 같은 모델 안에서 유일해야 한다. 삭제된 모델·purge 진행 모델·삭제된 버전은 수정할 수 없다. 두 라벨의 저장은 독립적인 요청이다.

`expected_*`에는 편집 시작 때 읽은 라벨을 보낸다. 현재 라벨이 기대값과 다르면 `409/Aborted`이며 최신 값을 다시 읽는다. 이미 원하는 라벨인 경우 같은 요청을 다시 보내도 성공하며 수정 시각을 다시 바꾸지 않는다. 중복 이름은 `409/AlreadyExists`, 입력 오류는 400, 없는 UUID는 404다. UI는 응답이 유실돼도 자동 재전송하지 않고 저장 버튼으로 같은 대상·기대값을 재확인한다.

UUID, source revision, `source_ref`, artifact URI/해시, 학습·배포 참조는 바꾸지 않는다. 실행 중이거나 가져오기 중인 버전도 표시 이름을 바꿀 수 있다. HF repo ID나 revision 변경·재다운로드 기능은 아니다.

## 목록의 가져오기 진행률

선택한 버전이 `importing`이면 3초마다 버전과 연결된 Operation을 조회한다. 상태 아래 작은 글씨로 `현재 파일 42% · 3/8개 저장`을 표시한다. 퍼센트는 현재 파일의 다운로드 바이트 비율이다. 총 가져오기 완료율·남은 시간으로 표시하지 않는다. 전체 크기를 모르면 바이트 수만, MinIO 저장 단계에서는 저장 중 문구와 저장한 파일 수만 표시한다. Operation 보고가 지연돼도 추정값을 만들지 않으며 상세 화면의 마지막 보고 시각을 확인할 수 있다. 조회 실패는 `진행률 조회 지연`으로 표시한다.

새 importer·Operation 배포는 필요 없다. 기존 `TransferProgress`를 사용한다. 최종 `ready/failed`를 확인하면 목록 캐시를 무효화한다. 열려 있는 이름 수정·추가·상세/작업 폼은 전체 화면 자동 갱신으로 닫지 않는다.

## 배포 삭제

배포 목록에서 **종료 → 중지됨 확인 → 삭제 → 배포 삭제** 순서로 사용한다. 이미 중지된 배포는 바로 삭제를 선택한다. 삭제 확인에는 컨테이너 로그도 지워진다는 사실을 표시한다.

`DELETE /api/deployments/:id` → Deployment gRPC `DeleteDeployment` → 200 배포 상세(`deleted_at` 포함). `status`, `desired_state`, `runtime_state`가 모두 `stopped`인 실제 vLLM 계열 배포만 허용한다. 실행·시작·종료 중이거나 상태를 확인할 수 없으면 409다. 이전 CPU demo에는 이 런타임 삭제 기능을 적용하지 않는다.

1. 컨트롤러와 같은 배포별 PostgreSQL advisory lock을 얻는다. 관찰/준비 중이면 재시도를 요청한다.
2. Docker에서 이름·owner/deployment/operation/plan 라벨·이미지·network·mount·기록된 전체 컨테이너 ID와 정지 상태를 확인한다. 모호한 컨테이너를 채택하지 않는다.
3. 남은 GPU reservation을 정리하고 해당 전체 ID로 `docker rm`을 실행한다. force·image·volume 삭제는 하지 않는다. 외부에서 다시 실행되면 Docker가 삭제를 거절한다.
4. 컨테이너 부재를 재확인하고 검증된 배포 UUID의 전용 cache 디렉터리만 지운다. 다른 배포 캐시는 보존한다.
5. 마지막에 `deleted_at`을 기록하고 기본 목록에서 제외한다. DB 행·Operation·과거 참조는 남긴다. 단건 GET으로 삭제 이력을 조회할 수 있다.

중간 실패는 삭제 완료로 표시하지 않는다. 컨테이너만 제거된 상태도 같은 DELETE로 cache 정리부터 이어갈 수 있다. 성공 후 다시 DELETE하면 같은 삭제 기록을 반환한다. 삭제된 배포는 컨트롤러 후보에서 제외하며, 새 StartServing은 거절한다. 등록 모델과 MinIO 파일은 그대로여서 별도 새 배포를 만들 수 있다. 모델 자체 삭제는 기존 카탈로그/파일 purge 기능을 사용한다.

## `docker ps -a`의 Exited 해석

| 이름/이미지 | 의미 |
|---|---|
| `model-serving-*` | 실제 모델 서빙 런타임. Exited는 프로세스 종료이며 플랫폼의 의도적 중지인지는 API 상태·종료 코드·로그로 구분 |
| `model-op-*` + `management-import-job` | 가져오기 일회성 작업. Exited(0)은 정상 종료 프로세스일 수 있으며 최종 도메인 결과는 Operation/버전으로 확인 |
| `model-op-*` + `deployment-job` | 서빙 시작을 조정하는 일회성 작업. 이 작업 종료와 모델 런타임 종료는 별개 |

첨부 화면의 `model-serving-b35f856b...`는 Exited(1)이므로 출력만으로 정상 중지라고 판단하지 않는다. 실제 배포와 무관한 종료 컨테이너도 있으므로 일괄 `docker container prune` 또는 전체 Exited 삭제를 적용하지 않는다. 작업 컨테이너는 기존 GC 보존 정책이 관리한다.
