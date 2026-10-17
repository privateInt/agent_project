# Native metadata packaging 설정 delta 독립 정적 검토

판정: **REQUEST_CHANGES** — 코드 delta에는 미해결 동작 finding이 없으나 아래 F1의 변경 근거 pin 불일치를 수정해야 최종 검토에서 일치하는 근거로 재사용할 수 있다. 발행·제품·검사를 실행한 판정이 아니다.

- package_unit.py SHA256: `f4c363ad801ea378bf73931a83409a2c25948b8a462321dc37f571d8ac45a532`
- 보존 package 원본 SHA256: `a7d2387979ee762c670a4795a10bdad5283b9896f452f5b66a001e34fdb45747`
- persist_unit_publication.py SHA256: `564ab0fe851bdca827b247a67d8c0ed375c83fc0a2d1f2833eae21faf8022615`
- 보존 persist 원본 SHA256: `f775b637e2af4cc56475a8fc4f190d6c739aef2b2e53aa2df5b2152bae0ad4b7`
- 기준 parent: PR22 `d59de21d4b43a71f3e518592c589b0761e668de5`; Draft base: `codex/agent-project-owner-ack-adapter-20261017`.

**F1 — 변경 기록 after pin이 실제 bytes와 다름:** transition-evidence/native-metadata-helper-change.json 3행은 after_sha256을 `797d6d97941c0a25df37314c9d37b07b23df04adab2878ea564381212b0ebe30`으로 기록하지만, 현재 package_unit.py의 실제 SHA256은 위 `f4c363ad…`이다. before pin은 보존 원본과 일치한다. 이 JSON도 metadata records에 포함되어 게시될 예정이므로 현재 상태로는 고정된 변경 근거와 검토본이 일치하지 않는다. 기존 JSON bytes를 별도로 보존한 뒤 현재 helper의 full after pin을 명시한 새 정정 기록을 남기거나 원본 보존을 동반해 해당 기록을 갱신해야 한다. 제품 수정·검사 재실행은 필요하지 않으며 이 근거 delta만 확인하면 된다. 현 두 helper bytes가 유지되면 아래 코드 검토를 반복할 필요는 없다.

**실제 delta/기본값:** 보존 원본 두 파일과 현재 파일의 실제 diff를 읽었다. package 변경은 metadata SETTINGS 항목, optional test 경로 선택, fixture_only의 cfg override뿐이다. persist 변경은 metadata choice/prefix 및 parent/base 선택뿐이다. 저장된 두 .diff도 이 변경 내용과 맞는다. 기존 unit의 SETTINGS, PARENT fallback, test 경로 기본식, fixture_only=True 기본값과 기존 parent/base 선택은 그대로다. argparse에 새 선택지가 추가된 것 외에 기존 unit의 행동을 바꾸는 경로는 없다.

**metadata 설정:** test override는 새 read_only 검사 파일을 가리키고, final review 경로·관측 JSON·별도 workspace·새 fixture family가 metadata 전용이다. parent는 package의 cfg 선택→HEAD 검증과 persist의 기대 parent/assertion 양쪽에 고정된다. persist가 Draft 여부와 정확한 base도 확인한다. fixture_only=False는 caller가 사용할 transport 제품이라는 범위 표기이며 native 성공을 뜻하지 않는다. manifest의 actual_runtime_transition=NOT_COMPLETE/main_merged=False는 유지된다. 최종 관측 JSON도 fake 검사와 수정본 native NOT_RUN을 계속 구분해야 한다.

**최종 review 및 metadata-only 갱신 보호:** package 129–142행은 최종 독립 보고서 첫12줄의 PASS, 현재 source/test 전체 SHA의 보고서 포함, 관측 JSON의 final_independent_review=PENDING을 요구한다. final status/file/review hash만 PASS로 바꾸며 결과 수치나 native 상태를 생성하지 않는다(190–200행). 변경 전 summary bytes를 exclusive 방식으로 별도 보존하고 최종 보고서도 보존한다. 이 보호는 static source PASS만으로 제품 최종 PASS를 대신하지 못하게 유지한다. 실제 최종 source/test/report/summary 대조는 후속 최종 독립 검토 대상이다.

**원본11·remote exact readback 보호:** persist 52–78행의 parent/Draft base/파일 집합/bytes_match/tree 검증, remote commit 원문 재구성의 exact SHA, branch/head 일치, CAS update-ref, clean tree/parent 및 각 commit blob의 로컬 bytes+SHA 대조는 변경되지 않았다. 원격 관측은 coordinator가 만든 remote.json을 입력으로 하는 기존 신뢰 경계이고 helper 자체가 새 원격 조회를 수행하지 않는다. 80–86행의 checkpoint 원본 파일 hash, status.z bytes, 원본 HEAD 대조도 그대로다. package의 최종 staged 파일 집합·index bytes·부모 HEAD·민감 패턴 검사와 역사적 코드/log .txt 보존도 유지된다. 이는 보호 코드의 정적 보존 확인이지 이번 turn에서 원본 상태나 remote readback을 실행 확인했다는 뜻이 아니다.

제품 import/검사·helper 실행·git mutation·발행/network/RPC/socket·추가 환경/다른 agent 조회는0이다. 이 보고서만 작성했으며 기존 source PASS를 반복하지 않았다. CLOSED native 조사·scheduler PAUSE·운영/auth 보호를 유지한다. 로컬 단위 마감04:00Z와 누적 제한을 연장/재설정하지 않는다.
