판정: **REQUEST_CHANGES**

`source-ready.json`의 SHA와 두 소스가 일치한다. 아래는 실행 없이 확인한 수정 필요 사항이다.

1. **P1 — 외부 launcher 해시 검증이 실행을 차단하지 않는다.**
   `build_agent_execution_package.py:125–129`는 `sha256sum -c`와 Python 실행을 독립 명령으로 생성한다. 명령서를 연속 실행하면 해시 불일치 후에도 변조된 launcher가 실행된다. 검증 성공을 조건으로 실행하도록 연결하고, stage 실패 후 설치본 명령으로 진행하는 것도 차단해야 한다. 예상된 fixture 종료 코드 1은 별도로 처리해야 한다.

2. **P1 — 승인 workspace를 다른 경로로 교체할 수 있다.**
   builder의 39–41행과 launcher의 194–200행은 workspace의 이름이 `agent_project`인지와 target의 상대적 포함 관계만 검사한다. 따라서 소유권·권한을 만족하는 `/tmp/other/agent_project`도 `temporary_fixture=False`인 생산 설정으로 허용된다. 독립적으로 고정한 승인 workspace와 정확히 대조해야 한다. 또한 launcher 89–90행은 `/tmp` 이외의 root 소유 sticky 디렉터리까지 허용하므로, 승인된 예외 범위로 제한해야 한다.

3. **P2 — controller 설정의 경로 결합을 import 전에 검증하지 않는다.**
   launcher 321–333행은 `fixture-controller.json`의 의미를 검사하기 전에 `modules()`를 호출한다. 이 파일의 installation/state/DB 경로가 `runtime.json`과 다른 경우도 manifest 해시만 맞으면 앞선 검증을 통과한다. 두 설정의 경로를 결합해 검증한 뒤 제품 모듈을 import해야 한다.

4. **P2 — staging이 원본 bundle 내부에 생성될 수 있다.**
   builder 42행은 `output == target`만 거부한다. 임시 모드에서 `target=<output>/bundle/candidate`를 지정하면 launcher 355–365행이 bundle 내부에 staging을 만들고, `os.walk(root)`가 그 staging까지 재귀 복사한다. 경로 길이 오류나 저장공간 소모로 이어진다. 목적지와 staging 부모가 원본 bundle 내부인 경우 생성 전에 거부해야 한다.

검토 소스와 첫 12행은 다음과 같다.

[agent_execution_package.py](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/execution-package-development/src/ops/tools/agent_execution_package.py)
SHA-256: `694cc360846b20190978fc8325f0c0c5a984d2b4265701a24736cc3e991f86ea`

```python
"""Linux/Python3.10 fixture-controller package entry; never a native dispatcher.

Bootstrap is externally hash-pinned before execution. No RPC, CLI queue, automatic
DB initialization/migration, service control, credential access or PAUSE removal.
"""
import argparse
import contextlib
import ctypes
import datetime as dt
import fcntl
import hashlib
import importlib
```

[build_agent_execution_package.py](/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/execution-package-development/src/ops/tools/build_agent_execution_package.py)
SHA-256: `07a234dbacc1d0fbeed9fee51ed3943175d435d6ca8beb87c0939fe0229d092d`

```python
"""Prepare, never install, a complete externally pinned fixture-controller bundle."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

MODULES = ('github_check_audit', 'github_fixture_collector', 'github_fixture_ledger',
           'github_fixture_controller', 'github_fixture_config', 'codex_readiness',
```

제품·테스트 import/실행, socket/RPC/네트워크, 설치 및 파일 변경은 수행하지 않았다. 기존 코드·검사는 반복하지 않았으며, 기존 지적과 D29 v2 계획 PASS는 보존한다. 이번 판정은 위 SHA의 신규 소스에 한정한다.
