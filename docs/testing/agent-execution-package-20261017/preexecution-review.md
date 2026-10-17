**PASS — 지정 delta의 실행 전 정적 검토를 통과했습니다.** 기존 PASS 범위는 재사용했습니다.

- `located=False`에서도 runtime 경로를 `target/installation`에 결합하며 controller exact binding을 유지합니다. P04의 stage 거부·target 부재 검사도 확인했습니다.
- 모든 `mkdtemp`는 `/tmp`로 고정됐습니다. 결과 환경변수 경로를 제거하고 새 0700 전용 root 내부에 `x` 모드로 저장합니다.
- mutant의 종료값 0·`BUNDLE_VERIFIED`·동일 크기 선행조건 미충족은 `RuntimeError(ERROR)`로 처리하며 지정 assertion 실패만 detected로 인정합니다.
- 외국 UID `fstat` mock과 두 번째 파일 복사 실패 주입·partial staging 잔존 검사를 확인했습니다. 실제 `chown` 호출은 없습니다.

실제 SHA-256은 v2 pin 4개와 모두 일치합니다.

```text
agent_execution_package.py
73345b30c51f09e6ed7a9f697f561b9a4f3e2f95130fdfa8c22ef3667c3ebaf8
build_agent_execution_package.py
992fcbfa94449e983ce21dd47acc3eaa821ad6100cf90b00043941283e81e585
test_agent_execution_package.py
6fc86e199884568678fea45dc4ee5dca8e5ba09477de223482eb12e8eee31b47
test_agent_execution_package_mutant.py
2f000c085db4f48f4fa9671990f21299f70874779dfcbe7625e87f3584aa5f5a
```

제품·검사 실행, import, 쓰기, RPC/socket/network 및 인증정보 접근은 수행하지 않았습니다.
