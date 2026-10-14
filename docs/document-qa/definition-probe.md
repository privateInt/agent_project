# 기존 QA 정의의 읽기 전용 대조

F0a의 원래 실패와 prompt-only 후보가 현재 어떤 Agent 정의를 참조하는지 확인하는 도구다. [사전 계획](../plans/document-qa-f0a-test-plan-20261014.md#정의조회-도구-사전계획-v1)의 DP01~DP12를 따른다.

## 읽는 대상

[definition_probe.py](../../src/apps/document_qa/definition_probe.py)는 기존 관찰 JSON에서 두 Run/Version 참조를 읽는다. 고정 순서로 original Run→original Version→candidate Run→candidate Version의 GET 네 개만 허용한다. 기존 QA settings와 인증 파일을 재사용하며 설정 부재를 새 초기화나 Ops 설정 fallback으로 처리하지 않는다.

Run의 실패 code/stage, 대응 Version hash, 정의의 schema·한도, 같은 Agent·입력·모델·Tool pin, prompt-only 차이를 확인한다. 서비스가 보고한 definition hash와 Run 값의 일치를 확인하며 서비스 hash를 재계산하지 않는다. prompt 원문 대신 UTF-8 byte 수와 도구가 계산한 SHA-256을 보고한다.

기존 실패 Run의 steps를 다시 읽지 않는다. 따라서 `MISSING_REFERENCE` 세부 진단을 현재 서버에서 다시 검증한 결과가 아니며, 현재 Run의 `ANSWER_REFERENCE_INVALID`/finalize와 과거 진단을 구분한다. 모델·Tool 실행, 새 요청 키, 발행/head 전환, Docker 접근, DB/상태 파일 쓰기는 없다.

## 실행과 결과

도구가 있는 개발 사본에서 프로젝트 루트를 작업 디렉터리로 사용한다.

```bash
python3 -B -m src.apps.document_qa.definition_probe
```

`--project-root`, `--settings`, `--baseline`으로 기존 경로를 명시할 수 있다. 상대 baseline은 project root를 기준으로 읽는다. settings의 상대 token_file은 settings 파일의 디렉터리를 기준으로 처리한다. 서버 원본을 수정하지 않는 실행의 고정 commit과 명령은 작업 PR/사용자 전달 단계에서 정한다.

2026-10-14 첫 사용자 실행은 `src` import 단계에서 멈췄다. 단일 파일 실행도 의존 모듈을 포함하는 `src` 구조의 개발 사본이 필요하다. 운영 원본과 개발 사본의 구조가 같다고 가정하지 않는다. 수정된 전달 방식은 고정 commit 전체를 임시 clone으로 받아 detached HEAD와 helper hash를 검증하고, 그 임시 사본을 PYTHONPATH와 project-root로 사용한다. 기존 settings 경로는 별도로 명시해 인증 참조 해석을 보존하며 운영 원본의 Git이나 폴더 구조를 바꾸지 않는다. 이때 source hash는 임시 개발 snapshot의 값이다.

| 종료/출력 | 의미 |
|---|---|
| 0, stdout JSON `OBSERVED` | 네 개 조회의 정의·실패 pin이 기대 비교 조건과 일치 |
| 2, stderr JSON `BLOCKED` | 설정/참조/응답/한도/비교 조건이 확인되지 않음. 고정 reason과 stage로 다음 조사를 정함 |

60초의 공유 네트워크 budget, 요청별 최대 8초, HTTP 시도 최대 네 번과 기존 2 MiB 응답 한도를 사용한다. 실패 시 재시도하지 않으며 redirect와 환경 proxy를 사용하지 않는다. 결과에 UUID·주소·credential·질문/답변 원문·서비스 definition/input hash를 출력하지 않는다.

`repository_source_sha256`은 프로젝트 사본의 Agent runtime, Model chat gateway, QA transport 소스 세 파일이다. 활성 컨테이너가 그 파일을 실행한다는 증거는 아니다. 실제 payload·tokenizer/chat template·head·답변 품질은 계속 미검증이며 `f0a_accepted=false`를 유지한다.

검사는 [test_definition_probe.py](../../src/apps/document_qa/tests/test_definition_probe.py)에서 fixture와 실제 loopback HTTP/CLI를 구분한다. 실제 서버 실행 전에는 native 조회 인수나 Python 3.10 실제 실행을 통과로 표시하지 않는다.

격리 검사·오류 주입·호환성의 실행 근거는 [오프라인 결과](../testing/document-qa-definition-probe-offline-20261014.json)에 둔다.

첫 사용자 실행의 import 실패와 수정한 전달 경로의 격리 확인은 [native 실행 기록](../testing/document-qa-definition-probe-native-20261014.json)을 따른다. 실제 정의 조회는 아직 미완료다.
