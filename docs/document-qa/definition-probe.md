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

검사는 [test_definition_probe.py](../../src/apps/document_qa/tests/test_definition_probe.py)에서 fixture와 실제 loopback HTTP/CLI를 구분한다. fixture·loopback 검사와 실제 서버 조회를 구분하며, native 조회 성공을 전체 Python 3.10 검사나 F0a 품질 통과로 표시하지 않는다.

격리 검사·오류 주입·호환성의 실행 근거는 [오프라인 결과](../testing/document-qa-definition-probe-offline-20261014.json)에 둔다.

첫 사용자 실행의 import 실패, 전달 경로의 격리 확인과 두 번째 native 실행은 [native 실행 기록](../testing/document-qa-definition-probe-native-20261014.json)을 따른다. 두 번째 실행은 Python 3.10.19에서 checksum 확인 후 네 GET으로 `OBSERVED`를 반환했다. 두 정의는 prompt만 다르고 같은 Agent·입력·모델 pin과 generation/budget을 유지했으며, 기존 두 Run은 계속 `ANSWER_REFERENCE_INVALID`/finalize 실패였다. 쓰기·추론은 각각 0건이다. 이는 이 native 조회 경로의 관찰 성공이며 전체 3.10 검사 실행은 아니다. 활성 runtime/template·실제 payload·head·답변 품질 인수는 계속 미완료다.

후속 제한 Docker metadata 조회는 같은 native 기록의 `serving_container_metadata`에 분리했다. 고정 배포 이름과 owner/deployment label이 일치하고 컨테이너는 running·비paused·비restarting이었다. 이미지 ID를 관찰했으나 현재 Model API의 routing·실행 옵션·실효 template은 아직 확인하지 않았다. 이 조회는 전체 inspect/env/argv/log 출력과 컨테이너 내부 실행을 포함하지 않는다.

후속 argv 관찰 명령은 사용자 출력에서 `SERVING_ARGV_UNCONFIRMED`로 중단됐다. 전달된 명령의 동일성과 내부 실패 단계는 확인되지 않았으며, entrypoint·옵션·모델/template 오류 중 어느 하나로 원인을 확정하지 않는다. 기존 컨테이너 metadata 성공과 argv 미확인을 분리하고 허용된 분류값만 읽는 짧은 후속 조회로 실패 범위를 좁힌다.

후속 형태 조회에서 이름·label·이미지와 상태가 같은 컨테이너의 실행 프로그램은 정확히 `vllm`, 인자는 18개로 관찰됐다. 앞선 의도된 argv 도구는 Python module 형식만 허용하므로 이 현재 형식을 거부한다. 이 대조가 앞선 명령 전달의 무결성이나 다른 argv 조건까지 검증하는 것은 아니며, Model/template 실패 원인을 확정하지 않는다.

[serving_metadata_probe.py](../../src/apps/document_qa/serving_metadata_probe.py)는 별도의 단독 실행 도구다. 필수 `--deployment`/`--image` 값을 내부 pin으로 받아 같은 inspect 한 번의 신원·상태를 대조하고, Python module 또는 `vllm serve`의 선택된 argv 필드를 확인한다. 8초·64KiB 캡처 한도와 stage별 고정 오류를 사용하며 이미지·실제 식별자·경로·argv·오류 원문은 출력하지 않는다. 이 파일은 stdlib만 사용하므로 저장소 clone/PYTHONPATH 없이 고정 commit과 파일 hash를 검증한 임시 파일에서 실행할 수 있다. 기존 정의조회 도구와 달리 Agent GET/settings를 사용하지 않으며 Docker exec·모델/template 파일 읽기·추론은 없다.

성공 결과의 template/tokenizer option 여부는 CLI에 명시됐는지만 나타낸다. 해당 옵션이 없을 때의 기본 파일 선택과 실효 template, Model API routing·payload·head·답변 품질은 계속 미검증이다. 새 도구의 사전 기준은 검사 계획의 Serving 메타데이터 조회 도구 v1이며, 격리 결과는 [별도 근거](../testing/document-qa-serving-metadata-probe-offline-20261014.json)에 둔다.

새 standalone 도구의 사용자 실행은 checksum PASS 뒤 `OBSERVED`를 반환했다. 같은 inspect 한 번에서 `vllm_cli`·image/model 경로/alias pin 일치·max_model_len=4096을 확인했고, 명시 chat-template/tokenizer 옵션은 없었다. 기본 template/tokenizer 선택·실효 동작과 전체 Python 3.10 검사·품질 인수는 계속 미완료다. 이 serving 조회는 서버 Codex worker·샌드박스·무인개발 준비 인수를 대신하지 않는다.
