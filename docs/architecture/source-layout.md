# 소스 디렉터리 구조

2026-10-13 사용자 요청에 따라 Agent 소스를 `src` 아래에 모으고 Ops의 세 역할을 한 경로에 모았다. 문서는 저장소 루트의 `docs`에 둔다.

| 이전 경로 | 현재 경로 | 책임 |
|---|---|---|
| agent_platform | src/platform/agent | Agent 도메인과 runtime |
| data_platform | src/platform/data | 수집·정제·canonical |
| knowledge_platform | src/platform/knowledge | 청킹·색인·검색 |
| model_platform | src/platform/model | 모델·학습·배포·추론 |
| ops_platform | src/ops/platform | Ops API·화면·관측 |
| ops_tools | src/ops/tools | 호스트 검사·운영 도구 |
| ops_candidates | src/ops/candidates | 보류 중인 후보와 당시 manifest |
| apps | src/apps | 플랫폼을 연결하는 제품 앱 |
| infra | src/infra | 공용 서비스 구성 |

저장소 루트에는 README, AGENTS, docs와 src가 남는다. 각 플랫폼의 Dockerfile·Compose·tests·contracts는 담당 소스와 함께 이동한다. Go module 경로(`agent-platform` 등), DB 이름, 서비스/이미지 이름, 볼륨과 MLflow tag 계약은 유지한다.

## 실행 경로

Python의 저장소 모듈은 `src` 이름공간을 사용한다. `platform`은 Python 표준 라이브러리에도 있으므로 `platform.agent`로 직접 import하지 않는다. 이전 최상위 패키지 alias나 symlink는 만들지 않는다.

```bash
# 저장소 루트에서 실행
python3 -B -m src.apps.document_qa.api --help
python3 -B src/ops/platform/ops/manage.py --help
python3 -B -m src.ops.tools.check_source_layout

# 플랫폼별 Docker/Compose 빌드 문맥은 각 플랫폼 디렉터리
cd src/platform/model
python3 -B tests/check_build_layout.py
```

기존 운영 state·credential·receipt·DB·컨테이너·worker는 이번 소스 이동으로 바꾸지 않는다. 운영 owner의 project_root와 code hash 검증을 완화하거나 자동으로 재기록하지 않는다. 개발 worktree에서 운영 state를 가져오거나 prepare/up/install/start를 수행하지 않는다.

## 과거 근거와 후보

`docs/history`, `docs/reviews`, 날짜별 검사 JSON, tests/results 및 후보 manifest와 `candidates/**/source` payload는 당시 바이트를 보존한다. 그 안의 이전 경로는 당시 구조를 뜻한다. 현재 명령과 문서 링크는 새 경로를 사용한다. 후보의 재사용 Python 도구와 테스트에는 새 import 경로를 적용하지만, 당시 manifest의 hash를 새 코드에 맞춰 다시 쓰지 않는다.

공개 원본에서도 과거 부팅 후보의 prepare 검사가 `CANDIDATE_SOURCE_SET_CHANGED`였다. 또한 ServiceTests의 `test_boot_links_owned_only_and_stop_disables`는 `BOOT_EXPLICIT_START_ADAPTER_REQUIRED`로 실패했다. 이 둘을 소스 이동의 성공 근거나 운영 활성화 승인으로 해석하지 않는다. 과거 후보·운영 hash pin은 별도 검토와 명시적 재결합 전까지 거부할 수 있다. 후보 활성화와 운영 백업/복원·재부팅 보류를 유지한다.

이번 단위는 경로와 import·빌드 연결 정리다. F0a의 MISSING_REFERENCE, 기존 head revision1, 질문/Run 실패 및 제품 인수 상태를 바꾸지 않는다. 서버 적용 근거는 별도의 `codex_agent_source_layout_result_20261013.json` 보고서로 기록한다. GitHub push와 scheduler 활성화는 이 단위에 포함하지 않는다.
