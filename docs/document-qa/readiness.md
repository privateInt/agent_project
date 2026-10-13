# Q02 — 연결과 문서 QA 기본값

2026-10-07 갱신: Q02 사용자 서버 applied/written=17·29개 검사 OK와 Data/Knowledge/Agent/Model API reachable을 확인했다. 당시 embedding/chat 후보가 없어 defaults_ready=false, 설정 저장은 미수행이었다. 네 배포는 모두 stopped로 조회됐다. [서버 근거](../testing/document-qa-q02-server-observed-20261002.json)를 보존한다. GPU가 사용 중이라는 사용자 결정에 따라 모델 준비/실제 GPU 연동은 보류한다. 과거 조회를 현재 running 확인으로 사용하지 않는다.

일반 사용자는 문서와 질문만 입력한다. Q02는 운영 기본값 준비 단계다. 이후 [Q03a](uploads.md)에서 업로드 HTTP API·자기 자료함/job 저장을 구현하고 `/api/readiness`로 inspect를 노출했다. n8n 진입 전달은 [Q03b](workflows.md)로 구현했고 기존 TXT의 진입/CPU callback은 실제 인수했다. 사용자 화면과 실제 색인/질문은 후속이다. GPU 없는 업로드 접수는 defaults_ready=false여도 가능하다.

## 실행과 저장

프로젝트 루트에서 실행한다. Python 3.10 이상/표준 라이브러리만 사용한다.

```bash
python3 -B -m apps.document_qa.readiness inspect
python3 -B -m apps.document_qa.readiness configure
```

| 명령 | 조회 | 쓰기 |
|---|---|---|
| inspect | 기존 Gateway의 GET만 호출, 준비 상태/후보/제외 이유 출력 | 없음 |
| configure | 같은 조회와 기본값 확인 | defaults_ready=true인 경우 문서 QA 전용 settings.json만 저장 |

기본 설정 경로는 `$HOME/.local/state/agent-project-document-qa/settings.json`이다.
없으면 기존 `$HOME/.local/state/agent-project-ops/server/host-config.json`의 platforms에서
API 주소·enabled·token_file 참조만 가져온다. Ops 설정을 import/수정하거나 worker·Docker·
systemd를 호출하지 않는다. Ops의 access-key, Langfuse 비밀, DB 자격은 읽지 않는다.

기존 참조가 없거나 다른 배포에서는 `--settings`와 `--connections-from`으로 명시 파일을
지정한다. [설정 예시](../../apps/document_qa/settings.example.json)의 주소는 프로젝트 기본
host 주소이며 현재 접속 성공을 보장하지 않는다. token_file은 해당 설정 파일의 위치를
기준으로 해석한다. 키 값을 JSON에 넣지 않는다. 실제 저장 대상 파일명은 settings.json이며
부모 디렉터리는 0700, 저장 파일은 0600을 사용한다. 기존 외부 키 파일은 읽고 재사용하며
권한을 변경하지 않는다. 기존 Agent의 0444 파일도 소유자/쓰기 권한 검사를 통과하면 재사용한다.

이미 저장한 설정을 다시 실행하면 그 설정을 사용한다. 기존 플랫폼 접속 설정의 변경을
자동 따라가거나 모델을 자동 교체하지 않는다. configure는 읽은 설정 hash를 확인하고
lock/원자적 교체로 동시 수정 충돌을 거부한다. 중단 후 settings.lock이 남았다면 저장을
재시도하기 전에 실행 중인 configure와 현재 settings.json을 확인해야 한다.

## 기본값 선택

| 대상 | 확인 기준 | 선택 |
|---|---|---|
| Data | health와 원본 목록 API 접속 | 설정된 주소 재사용 |
| Knowledge | health와 현재 청커 catalog 조회 | structure_v1, max_characters=1000/overlap_characters=100 초기 프리셋 |
| Agent | readyz와 인증된 Connection 목록 조회 | 기존 이름/버전 참조를 표시; 내부 대상 연결 검증은 Q06 |
| embedding | ready/running 배포와 GetProfile, Knowledge의 snowflake-ko-v1 조건 및 동일 model/version/runtime | 전체 목록 확인 뒤 호환 후보 하나 자동 선택 |
| chat | ready/running vLLM, 빈 serving profile, LLM 등록, 모델/버전/서빙 identity | 전체 목록 확인 뒤 호환 후보 하나 자동 선택 |

모델 목록은 모든 status를 조회해 중지 모델도 구분한다. embedding/reranker repository가
과거에 llm으로 등록됐어도 답변 모델로 선택하지 않는다. profile 조회는 Model 내부에서 소유
runtime/설정/토크나이저 metadata를 확인할 수 있으나 embedding/chat 추론은 실행하지 않는다.
profile의 runtime ID는 현재 조회의 identity 검사에만 사용하며 QA 업무 계약/저장 기본값에는
Docker ID를 넣지 않는다. 설정에는 deployment/model version/embedding space와 입력 한도를 고정한다.

후보가 0개면 NO_COMPATIBLE_MODEL, 여러 개면 SELECTION_REQUIRED다. 출력 candidates의 name
또는 label로 한 번 선택한다. 이름이 같으면 전체 label을 사용한다.

```bash
python3 -B -m apps.document_qa.readiness configure \
  --chat '출력에 나온 답변 모델 이름 또는 label'
```

embedding도 `--embedding`으로 선택한다. 명시 선택은 기존 pin을 바꾸는 운영 요청이다.
선택하지 않았을 때 저장된 모델이 중지/삭제됐거나 model version/embedding space가 바뀌면
SELECTED_MODEL_UNAVAILABLE/SELECTED_MODEL_CHANGED로 차단한다. 자동 대체하지 않는다.
불완전한 페이지·중복 ID·반복 cursor·조회 중 identity 변경은 일부 후보가 보여도 자동 선택을
허용하지 않는다. 매 조회 결과는 그 시점의 관찰이며 실제 처리 단계에서도 준비 상태를 다시 확인한다.

청커는 현재 catalog에서 지원 여부와 구현 version을 확인한다. 이름만 같고 version이 바뀌면
준비 미완료로 표시한다. 1000/100은 첫 연결에 사용할 명시 프리셋이며 다른 청커보다 품질이
좋다는 결과가 아니다. 문자의 최대 UTF-8 byte 크기와 Model byte 한도를 비교한다. 문자 수를
token 수로 환산해 통과시키지 않는다. 실제 청크/token 한도 검사는 Q05에서 수행하고 넘는
본문을 조용히 잘라내지 않는다.

OCR는 Q04, 선택적 reranker와 Agent runtime 내부 Connection 검증은 Q06, 새 n8n trigger는
[Q03b](workflows.md)에서 진입 adapter를 구현했다. 이 단계의 passed는 준비 정보 조회의 판정이다. 후속 TXT 진입/CPU callback·Data 정제 인수는 [상태표](../status.md)의 별도 근거이며 모델 기본값/실제 색인 준비를 대신하지 않는다.

## 결과와 완료의 의미

| 필드 | 의미 |
|---|---|
| defaults_ready | Q02의 API 조회와 필수 embedding/chat/청커 기본값 확인을 통과 |
| question_ready | 현재 false. TXT canonical은 확인했으나 실제 색인/Tool/Agent 질문 연결은 미완료 |
| blockers | 접속/권한/후보 선택/profile/identity/목록/한도 문제와 범위 |
| effects.configuration_written | 이번 실행이 자기 설정을 실제 새로 저장했는지 여부 |
| effects.credential_contents_read | API 인증을 위해 기존 bearer 파일을 읽었는지. 값은 출력하지 않음 |
| deferred | 후속 단계에서 확인할 항목 |

passed의 종료코드는 0, attention_required는 2다. 여러 모델 선택 필요나 모델 중지는
프로그램 crash와 구분한다. 원격 오류 본문·endpoint·비밀값·원본 문서 본문을 출력하지 않는다.
LOCAL_SETTINGS_WRITE_UNCERTAIN이면 현재 settings.json을 inspect로 확인한다. 쓰기 중 문제가
발생한 후 설정이 전혀 바뀌지 않았다고 단정하지 않는다.

HTTP에는 GET 경로 allowlist, redirect 거부, 환경 proxy 미사용, 응답 2MiB 제한, 목록당
10페이지/총 100요청, 공유 50초 budget·요청 socket timeout 8초를 적용한다. 재시도·모델 기동은
없다. 이 제한은 애플리케이션 조회 budget이며 OS DNS 처리까지 강제 중단하는 실행기가 아니다.

준비 상태의 재사용 함수는 [readiness.py](../../apps/document_qa/readiness.py)의 inspect(settings, reader, choices)다. [Q03a API](uploads.md)의 `/api/readiness`에서 재사용한다. GET 경로를 실제 호출할 때만 조회하며 패치 설치/init/status는 운영 API를 호출하지 않는다. configure 재실행과 모델 기동은 현재 GPU 보류 기간의 다음 명령이 아니다.

## 관련 검증

```bash
python3 -B -m unittest discover \
  -s apps/document_qa/tests -p 'test_readiness.py' -q
```

29개 검사는 fake API와 임시 설정, loopback HTTP server 하나에서 실행한다. 정상 후보,
0/여러 후보, 중지·기존 pin 변경, legacy embedding/chat 구분, identity 변경, 불완전 목록,
chunker version/byte 한도, 설정 재사용/동시 수정, 비밀 비노출, redirect/GET 한도, 잘못된
응답을 확인한다. 운영 Gateway·DB·모델을 호출하지 않는다. Q01/전체 플랫폼 검사를 반복할
필요는 없다. [검증 결과](../testing/document-qa-q02-offline-20261002.json)와
[조회 계약/코드 근거](../../apps/document_qa/readiness-contract.json)를 참고한다.
