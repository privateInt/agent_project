# D06 authorizer fixture 보정 독립 검토

판정: **PASS — D06 경미한 검사 fixture/실행기 보정**. 아래 고정 변경과 제한된 후속 실행 범위에 대한 정적 판정이며, B3·변이 실행 성공이나 최종 제품 승인을 뜻하지 않는다.

- B test 이전 SHA256: `4190ad0152637cb06d5276bd94682a767def9ee8da9f7dbc86ee31930569a117`
- B test 수정 SHA256: `63e1b380519df8f78d6e143c8824150049b73c4cf024a437442501ba9fd281d6`
- run-3 runner SHA256: `025372067ccd97fdd8f1a5f7070fe2ea5161d6e99903403abad624ffb4670238`
- 불변 quota 제품 SHA256: `699f3a6e8993f28d88d7413ac51197a144b6d8b69a01f321da11a17398a4a23c`
- 불변 binding 제품 SHA256: `1663ab454e0f66a5e7f23382b9cf4c047068a0c86f10d895c4ad19a4d57f2a3c`
- 재사용 run-2 normal/result.json SHA256: `3556762620e0634a645969a0396f972ae5607c30171453b926d5f21bdafaf43d`

`test-authorizer-cleanup.diff`와 실제 전후 파일의 변경은 B3의 `after_call` 한 줄뿐이다. 제품 호출이 끝난 뒤 `set_authorizer(None)` 대신 `set_authorizer(lambda *args: sqlite3.SQLITE_OK)`로 fixture 조회 권한을 복구한다. 제품 실행 중 INSERT 후 SELECT를 거부하는 authorizer, `CHECKPOINT_UNCONFIRMED`/`BINDING_READBACK` 기대값, INSERT·거부 횟수 및 peer의 빈 원장 확인은 그대로다. 제품 실행 후 profile PRAGMA를 읽는 검사 자체의 호환성 오류를 고치며 오류 주입이나 판정 기준을 약화하지 않는다.

`authorizer-cleanup-diagnosis.json`의 Python 3.10.12/SQLite 3.37.2 전용 임시 DB 관찰은 None 정리 후 조회 실패, allow-all callback 후 조회 성공을 구분한다. 해당 환경의 기록으로만 해석했으며 재실행하지 않았다. B1/B2와 모든 helper는 불변이고 AST 비교 gate도 B3만 변경됐음을 요구한다.

`runner-authorizer-resume.diff`와 실제 실행기 전후 차이는 실패 B3 메서드 전체를 한 번만 재검증하도록 좁힌다. 초기 Q1–Q3 PASS와 run-2 B1/B2 PASS·UNKNOWN guard helper PASS를 결과 및 제품/검사 pin·보존 snapshot·AST gate로 재사용한다. run-2의 3실행/0실패/1오류/0스킵과 PASS/PASS/ERROR 순서, 경계 위반 없음도 요구한다. B3가 PASS해야 아직 미실행인 M_Q/Q3 및 M_B/기존 UNKNOWN guard helper를 각각 한 번 실행하며, 기존 정확한 AST 변경·실제 잘못된 후보·semantic FAIL1/ERROR0 기준은 유지된다.

허용된 다음 실행 범위는 **B3 1회 → M_Q 1회 → M_B 1회**이며 정상 범위는 기존 3+2 PASS와 새 B3의 합계 6개다. 통과한 다른 메서드나 기존 suite 재실행은 없다. 최초 run-1/run-2 오류·로그·snapshot은 보존한다. 단위 보정 누적 2회(상한 3회), 원래 IO 경계와 마감 `2026-10-17T01:57:31Z`를 유지한다.

이번 검토는 파일 정적 대조만 수행했다. 테스트·import·DB·RPC·socket·queue·서비스 실행은 하지 않았다. PAUSE와 native D18 HELD는 유지되며 이 판정은 실행 권한이나 native 인수를 부여하지 않는다. 실제 B3·두 변이 결과의 최종 독립 검토는 별도다.
