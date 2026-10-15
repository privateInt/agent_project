# 테스트·리뷰 근거의 생산과 서명 계약

이 문서는 [필수 검사 신뢰 경계](common/GITHUB_GATES.md)의 프로젝트 구현 모델을 고정한다. 현재 구현 범위는 **외부에서 제공한 고정 공개키·대상과 실제 artifact bytes에 대한 Ed25519 서명 검증**이다. 실제 collector 설치·서명키 발급·역할 실행·필수 검사 발행·보호 설정은 아직 연결하지 않는다. [사전 검사 계획](github-signed-receipts-test-plan.md)과 [개발 근거](../testing/github-signed-receipts-offline-20261015.json)를 함께 확인한다.

## 실행 모델과 신뢰 시작점

owner가 PR 작업 디렉터리 밖의 전용 상태 디렉터리(0700)에 reviewed collector/evaluator release, 공개키 roster(0600), 검사 계획과 assignment ledger를 고정한다. 디렉터리와 상위 경로는 head worker가 교체·수정할 수 없어야 한다. GitHub main이 현재 보호되지 않았으므로 main의 helper를 다운로드했다는 사실만으로 신뢰 시작점을 만들지 않는다. owner가 별도로 검토한 release digest와 설치 파일을 대조한 뒤에만 실행하는 모델이다. 이 설치·소유권·상위 경로·실행 파일의 인수는 후속 작업이다.

| 구성 요소 | 책임과 경계 |
|---|---|
| owner/controller | 고정 evaluator release·계획·현재 repo/PR/base/head·actor/key roster를 PR 밖에서 로드하고 job별 기대 binding과 역할을 ledger에 배정한다. PR head가 보낸 기대 공개키·PASS·작업 완료 주장을 신뢰 입력으로 쓰지 않는다. |
| tests supervisor | controller가 승인한 계획의 각 필수 명령을 비신뢰 head worker에서 실행하고 실제 child 종료·실행 대상 source/build·결과·artifact를 직접 수집한다. worker에는 tests/reviewer 서명키·App credential을 주지 않는다. 임의 bytes에 대한 signing 요청을 받지 않는다. |
| reviewer supervisor | 구현에 참여하지 않은 별도 reviewer job을 controller가 배정하고, 고정 규칙·계획·대상 source/build·tests artifact를 전달한다. 실제 invocation ID·역할 배정·결과를 직접 수집한다. author가 제출한 리뷰 문장을 독립 실행으로 세지 않는다. |
| receipt signer | 각 supervisor의 수집 결과와 ledger의 동일 job을 확인한 뒤 해당 역할의 별도 키로 서명한다. author/test/reviewer actor와 key fingerprint가 달라야 한다. 키 분리만으로 실제 사람·프로세스 독립성을 입증한 것으로 보지 않는다. |
| evaluator/emitter | 보호된 roster와 최신 기대 binding으로 서명을 검증한 뒤, artifact 의미·계획 충족·실제 reviewer 배정·policy를 별도로 평가한다. 이후 최신 PR pins를 재조회하고 발행한다. 서명 검증 결과만으로 success를 발행하지 않는다. |

ledger는 controller가 만든 job ID, 역할, actor/key fingerprint, 고정 evaluator·계획·source/build, 실행 시작/종료 결과와 artifact digest를 연결한다. 동일 assignment의 재수집·실패·응답 유실은 기존 job과 근거를 조회해 처리한다. worker 제공 job ID로 역할을 바꾸거나 실패한 job을 새 성공으로 대체하지 않는다. 실제 실행 adapter·ledger 저장/동시성·실패 복구·issuer 권한 검사는 후속 구현의 사전 계획에서 고정한다.

## 서명이 덮는 bytes

공통 binding은 기존 [7필드 계약](github-evidence-binding-test-plan.md)을 그대로 쓴다: `repository`, `pr_number`, `base_sha`, `head_sha`, `release_sha256`, `plan_sha256`, `evidence_sha256`.

`evidence_sha256`는 tests supervisor가 수집한 **별도 필수 실행 근거 artifact의 원래 bytes**의 SHA-256이다. 향후 producer는 source/build·승인 계획·각 필수 실행의 job/exit/result·artifact manifest를 포함한 canonical UTF-8 JSON을 한 번 직렬화해 보관한다. FAIL·SKIP·미실행·환경 불명은 성공 근거가 아니다. reviewer artifact는 reviewer assignment·실제 invocation·검토 대상/규칙/계획·그 tests artifact digest·판정과 한계를 담은 별도 canonical UTF-8 JSON이다. reviewer receipt는 이 artifact의 hash와 tests hash를 포함한 공통 binding을 모두 덮는다. 어느 artifact도 자기 receipt·signature의 hash를 포함하지 않아 순환하지 않는다.

검증기는 전달받은 bytes를 재직렬화하거나 내용을 해석하지 않는다. tests/reviewer 각각 1..1,048,576 bytes이며 JSON 의미·필수 검사 전부 실행·review PASS·실제 invocation의 출처는 이 모듈의 증명이 아니다.

공개 API는 `verify_receipt_pair(expected_binding, expected_signers, receipts, artifacts)`다. CLI·파일 경로·URL·서명 API는 없다. 호출자가 이미 파싱한 builtin dict/str/int/bytes만 받는다. 외부 JSON decoder와 보호된 config loader는 이 모듈 밖이며 다음 연결에서 엄격히 구현해야 한다.

| 인수 | 정확한 구조 |
|---|---|
| `expected_binding` | 기존 엄격한 7필드. 기존 `canonical_binding_digest`로 유효성을 확인한다. |
| `expected_signers` | 정확히 `author`, `tests`, `reviewer`. author는 `{actor, key_sha256}`, 나머지는 `{actor, public_key_hex}`. raw Ed25519 공개키 32 bytes의 소문자 hex와 그 raw bytes의 SHA-256 fingerprint를 쓴다. 세 actor와 세 fingerprint 모두 달라야 한다. |
| `receipts` | 정확히 `tests`, `reviewer`. 각 값은 정확히 `{payload, signature_hex}`. signature는 64 bytes 소문자 hex다. 알고리즘 선택·내장 공개키·추가 필드를 허용하지 않는다. |
| `payload` | 정확히 `{schema, role, actor, key_sha256, binding, artifact_sha256}`. schema는 bool이 아닌 정수 1, role은 해당 역할, actor/key는 외부 roster와 일치, binding은 외부 기대 7필드 전부와 일치한다. |
| `artifacts` | 정확히 `tests`, `reviewer`의 실제 bytes. 각 payload의 hash와 대조하고 tests bytes의 hash는 binding의 `evidence_sha256`와도 대조한다. |

actor는 `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`다. 서명 메시지는 `b"agent-project:signed-receipt:v1\n"` 뒤에 payload 전체의 `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")`를 붙인 bytes다. JSON 순서·공백은 서명 의미가 아니며 모든 서명 필드는 canonical 메시지에 포함된다. 다른 domain이나 부분 payload에 대한 서명은 거부한다.

Ed25519는 [RFC 8032](https://www.rfc-editor.org/rfc/rfc8032.html), 공개키 SPKI는 [RFC 8410](https://www.rfc-editor.org/rfc/rfc8410.html)을 따른다. raw32 공개키 앞에 DER prefix `302a300506032b6570032100`을 붙인다. [OpenSSL 3 pkeyutl](https://docs.openssl.org/3.0/man1/openssl-pkeyutl/)의 `-verify -pubin -keyform DER -rawin`을 쓴다. 이 domain은 메시지 prefix이며 Ed25519ctx 또는 사전 hash 서명이 아니다.

## 검증기의 실행 경계와 판정

Linux의 public-only memfd 3개(공개키 DER·canonical 메시지·signature)를 `/usr/bin/openssl`에 전달한다. FD는 3 이상으로 고정하고 모든 성공·실패 경로에서 닫는다. shell 없이 고정 argv, `LC_ALL=C`/고정 PATH/`OPENSSL_CONF=/dev/null`만 있는 환경, stdin/stdout/stderr는 DEVNULL, 호출당 timeout 5초를 쓴다. 외부 openssl config/provider 설정을 환경에서 받지 않는다. 누락·timeout·비정상 종료·FD 실패는 닫힌 판정이며 원문 오류·입력·key·signature를 결과에 노출하지 않는다. artifact 자체는 subprocess에 넘기지 않는다.

두 서명·두 artifact hash·제공된 binding이 모두 일치할 때만 `CRYPTOGRAPHIC_MATCH`와 좁은 세 flag를 true로 보고한다. 하나라도 실패하면 전부 false다. `input_origin_verified`, `signer_roster_origin_verified`, `issuer_environment_verified`, `runtime_isolation_verified`, `independent_review_verified`, `trusted_gate_verified`, `binding_verified`, `native_ready`, `merge_authorized`는 항상 false다. caller가 roster와 서명을 함께 교체하면 새 쌍도 수학적으로 일치할 수 있다. 그래서 이 함수가 실제 roster의 출처·최신 GitHub 대상·실제 독립 검토·정책/병합을 승인하지 않는다.

이번 개발 검사에서 만드는 키는 격리된 임시 fixture뿐이다. 실제 GitHub App RSA 키는 읽거나 재사용하지 않는다. 다음 구현은 보호된 config loader·controller/issuer의 실제 실행과 수집 연결이다. 이 연결의 실제 거부 인수, 보류된 native sandbox 인수, 필수 검사/보호 적용을 마치기 전에는 무인 개발·자동 병합을 시작하지 않는다. 기존 sandbox의 세 실패와 조사 시간 상한은 이 계약으로 재개하지 않는다.
