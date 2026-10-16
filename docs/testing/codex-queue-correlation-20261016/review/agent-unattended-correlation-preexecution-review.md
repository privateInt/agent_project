# U01b2 최초 실행 전 정확한 수정 bytes 독립 확인

판정: **PASS — 실제 test/runner 수정은 두 D06 승인 범위 안에 있다.** 아래 pin의 최초 실행 전 확인이며, 검사·변이 실행 PASS나 최종 독립 검토를 대신하지 않는다.

| 대상 | 확인한 SHA-256 |
|---|---|
| 제품 `correlation-development/src/ops/tools/codex_unattended_queue_observer.py` | `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a` |
| 검사 `correlation-development/src/ops/tools/tests/test_codex_unattended_queue_observer.py` | `5555695acfd663b1d23092490db93d6a1e1e27d6e595f0c7c0e46f4ef11cb371` |
| runner `transition-evidence/correlation-run-1/run-once.py` | `704ce24042dc81db2978bc9b955656249c622d1a69c87cb86e202e1165defa37` |
| frozen v2 계획 | `25c798b43efd07b01c8c9030c0de7ff41a04b94cab8dd62dc4462ed13025403f` |
| 검사 구성 D06 범위 검토 | `f75cb20fd91d507f2eb74a73719f69cdd3515d72c63fe29c6c4b14582e28c665` |
| runner D06 범위 검토 | `39b1eaae19d9428822e996db7886c1be52b41aa33443b04e405851b5e15c03f5` |
| 최종 `tests-ready.json` | `6b073bb217cbd8af6902f8bce7e7b734d199abc1260cc6b290ff766a5b6df96e` |

실제 파일을 읽고 보존 before와 직접 diff했으며, 두 correction 디렉터리의 before/after pin·diff·audit와 일치함을 확인했다.

1. **검사:** before `5eab928148a081e1f5ef5c27b0d8cc25b542988074c05c7b35e0b006a9caa048` 대비 유일한 diff는 기존 bad_json loop의 kwargs와 설명 주석이다. submission은 INPUT_INVALID/read0/confirmed=False, request·response는 OBSERVATION_INVALID/read1/confirmed=True다. binding=True, candidate=None, 권한 flag=false, 읽기 횟수·DB snapshot/profile·원래 state/revision·원문/hash·close/reopen·sanitization assertion은 유지됐다. standalone expanded_page는 기존부터 null 첫 cursor이며 별도 변경이 없다. first-not-null 음성 case도 남아 있다. 보존 diff pin: `472e6e29072a07e2356db8ceb6d5f21d5de80bb4ace12903038ef61633f10778`.
2. **runner:** before `274067174deb402afe9f193e65a0748681fb620f34d97b217e78cb79cd493823` 대비 유일한 diff는 M1 selector guard다. 일반 literal/len 판별 대신 정확한 `matches[0][1] != text` AST 동일성을 요구한다. 이 방식은 승인 범위보다 넓게 정수 literal을 허용하지 않으며, 인덱스 0/1만 포함하고 모든 Call·직접 literal 피연산자·다른 비교식을 배제한다. 유일 Compare·단일 operator·소스 pin·한 구간 치환·의도 AST와 결과 AST 동등성 검사 및 M2 로직은 그대로다. 보존 diff pin: `d73492fc03a58b15d02aace702f9d2c7dd575a89942e1655c39d34d6e40480e3`.
3. **정확히 네 method와 두 변이:** test 파일과 runner의 METHODS가 다음 네 개로 일치하며 기존 TestCase를 상속하거나 과거 suite를 실행하지 않는다.

   - `test_valid_observation_and_read_only_boundary`
   - `test_unknown_and_bounded_evidence`
   - `test_matching_client_wrong_prompt_remains_unknown` — M1 단일 target
   - `test_two_matching_queue_ids_remain_ambiguous` — M2 단일 target

   최초 네 개 PASS/FAIL0/ERROR0/SKIP0 뒤에만 지정 target을 각 한 번 실행한다. M1은 bytes 비교만 우회하고 M2는 `len(matches) == 1`을 `>= 1`로 바꾼다. semantic reason assertion의 FAIL1/ERROR0/SKIP0, 실제 잘못된 OBSERVED/CANDIDATE_OBSERVED와 원래 queue ID 반환이 모두 필요하다. 정상 target PASS 재사용·오류 시 중단·기존 PASS 재실행 금지·원본 소스 비변경 조건은 유지돼 있다. 의미 assertion은 현재 243행이다.
4. **근거 보존:** before/after test·runner 사본 해시가 실제 파일과 대응한다. 기존 helper test의 pin `a7ed452257d098cd0003d25675722d83a73fb70affc999e0ea269efc14c49efe`도 그대로다. `runner-index-correction/static-audit-first-error.txt`의 AttributeError는 보존된 정적 metadata traversal 오류이며 검사·제품 실행 실패로 집계하지 않는다. ready/audit는 실행0으로 기록하고 있다. 이 검토 자체도 실행을 수행하지 않았다.

최초 실행 시 고정 입력은 위 source pin, M1 `matches[0][1] != text`, M2 `len(matches) == 1`이다. 제품 pin이 기존 정적 PASS와 같으므로 해당 소스 검토를 재사용한다. 아직 실제 네 검사·두 변이 결과는 없으며, 실행 후 원문 근거와 정확한 실행 pin을 대상으로 최종 독립 검토가 필요하다.

이 보고서만 작성했다. 검사/import/AST 실행/DB/native/runtime·권한 probe·다른 agent 접근·위임·통지는 하지 않았다. 기존 오류·원본11·과거 결과는 보존했다. runtime D18 HELD와 scheduler/root/user PAUSE, 기존 조사 상한은 그대로이며 이 판정은 live 실행 권한을 부여하지 않는다. 공유 시작 14:45:08Z, 보수적 마감 2026-10-16 15:45:08Z를 유지하고 시계를 재설정하지 않는다.
