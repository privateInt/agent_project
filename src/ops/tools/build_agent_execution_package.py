"""Prepare, never install, a complete externally pinned fixture-controller bundle."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import shlex

APPROVED_WORKSPACE = Path('/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project')
MODULES = ('github_check_audit', 'github_fixture_collector', 'github_fixture_ledger',
           'github_fixture_controller', 'github_fixture_config', 'codex_readiness',
           'codex_daemon_websocket', 'codex_unattended_admission',
           'codex_unattended_intent_journal', 'codex_unattended_queue_observer',
           'codex_unattended_quota_reader', 'codex_unattended_submission_binding',
           'codex_unattended_queue_receipts', 'codex_unattended_owner_ack_adapter',
           'agent_execution_package', 'codex_owner_cli_controller', 'codex_owner_turns',
           'codex_owner_stage_results')
DBS = [('fixture-ledger.sqlite', 'github_fixture_ledger'),
       ('fixture-controller.sqlite', 'github_fixture_controller'),
       ('intents.sqlite', 'codex_unattended_intent_journal'),
       ('bindings.sqlite', 'codex_unattended_submission_binding'),
       ('receipts.sqlite', 'codex_unattended_queue_receipts')]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def git_bytes(repository, commit, path):
    return subprocess.check_output(['git', 'show', commit + ':' + path], cwd=repository)


def build(repository, commit, output, target, workspace, uid, *, development_launcher=None, temporary_fixture=False):
    if re.fullmatch('[0-9a-f]{40}', commit or '') is None or uid != os.geteuid():
        raise ValueError('EXPLICIT_COMMIT_UID_REQUIRED')
    output, target, workspace = Path(output).absolute(), Path(target).absolute(), Path(workspace).absolute()
    if workspace != APPROVED_WORKSPACE or (not target.is_relative_to(workspace) and not (temporary_fixture and target.is_relative_to(Path('/tmp')))):
        raise ValueError('PROJECT_TARGET_INVALID')
    if output.exists() or output.is_symlink() or target.is_relative_to(output) or output.is_relative_to(target):
        raise ValueError('OUTPUT_MUST_BE_NEW_PREPARATION_PATH')
    # Source bytes are read from exactly this Git commit, never the worktree.
    source_files = ['src/__init__.py', 'src/ops/__init__.py'] + ['src/ops/tools/' + n + '.py' for n in MODULES]
    sources = {p: git_bytes(repository, commit, p) for p in source_files}
    launcher = (Path(development_launcher).read_bytes() if development_launcher else
                git_bytes(repository, commit, 'src/ops/tools/agent_execution_package.py'))
    ast.parse(launcher, feature_version=(3, 10))
    policy_source = 'src/ops/tools/tests/fixtures/codex_unattended_admission/flex_gate.py.txt'
    policy = git_bytes(repository, commit, policy_source)
    schemas = {}
    for filename, module in DBS:
        constants = {}
        for node in ast.parse(sources['src/ops/tools/' + module + '.py']).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ('_DDL', '_APPLICATION_ID'):
                constants[node.targets[0].id] = ast.literal_eval(node.value)
        schemas[filename] = {'module': module, 'application_id': constants['_APPLICATION_ID'], 'user_version': 1, 'ddl': constants['_DDL']}
    state = target / 'state'
    policy_config = {'version': 2, 'default': {'enabled': True, 'start': '00:00' if temporary_fixture else '23:00',
                     'end': '23:59' if temporary_fixture else '07:00', 'max_starts': 3}, 'dates': {}}
    fixture_jobs = []
    for index, profile in enumerate(('ok', 'nonzero')):
        assignment = {'schema': 1, 'mode': 'fixture', 'job_id': 'package-fixture:' + str(index + 1),
                      'target': {'repository': 'privateInt/agent_project', 'pr_number': 23,
                                 'base_sha': commit, 'head_sha': commit,
                                 'release_sha256': sha(git_bytes(repository, commit, 'docs/codex/common/release.json')),
                                 'plan_sha256': sha(b'EXPLICIT_FIXED_FIXTURE_PACKAGE_PLAN_NOT_OWNER_APPROVAL')},
                      'fixture_profile': profile, 'timeout_ms': 1000, 'max_stream_bytes': 65536}
        fixture_jobs.append({'assignment': assignment, 'sha256': sha(canonical(assignment))})
    pauses = ([str(target.parent / 'root-PAUSE'), str(target.parent / 'user-PAUSE'), str(state / 'PAUSE')] if temporary_fixture else
              ['/etc/codex-agent/PAUSE', '/home/codex_shshin/automation_mvp/PAUSE', str(state / 'PAUSE')])
    config = {'schema': 1, 'project_id': 'agent_project', 'repository': 'privateInt/agent_project', 'workspace': str(workspace),
              'target_base': str(target), 'state_dir': str(state), 'installation_root': str(target / 'installation'),
              'pause_paths': pauses, 'schedule': policy_config, 'fixture_jobs': fixture_jobs,
              'role_threads': {'test_agent': '01a13fb0-1721-7af3-82e6-747eac4de411', 'reviewer': '01a13fb0-1abe-7623-a15e-cf07071ce54a'},
              'native_dispatch': False, 'temporary_fixture': temporary_fixture}
    fixture_config = {'schema': 1, 'installation_root': config['installation_root'], 'state_dir': config['state_dir'],
                      'ledger_db': str(state / 'fixture-ledger.sqlite'), 'control_db': str(state / 'fixture-controller.sqlite')}
    contents = {('installation/' + p): (raw, 0o644, commit + ':' + p) for p, raw in sources.items()}
    contents.update({'installation/launch.py': (launcher, 0o644, ('DEVELOPMENT_WORKING_BYTES' if development_launcher else commit + ':src/ops/tools/agent_execution_package.py')),
                     'installation/schedule_policy.py': (policy, 0o644, commit + ':' + policy_source),
                     'installation/fixture-controller-installation.json': (canonical({'schema': 1, 'code_version': commit}), 0o600, 'generated'),
                     'runtime.json': (canonical(config), 0o600, 'generated-explicit-fixture-settings'),
                     'fixture-controller.json': (canonical(fixture_config), 0o600, 'generated'),
                     'schema-contracts.json': (canonical(schemas), 0o600, 'AST-from-' + commit),
                     'usage.md': (b'Fixture-only package. Native dispatch and owner approval NOT VERIFIED. External launcher/manifest hashes must be checked before import. Five DBs are initialized only by init-new. Runbook lives outside this bundle to avoid manifest hash cycles.\n', 0o600, 'generated')})
    output.mkdir(mode=0o700, parents=False)
    bundle = output / 'bundle'
    bundle.mkdir(mode=0o700)
    entries = []
    for name, (raw, mode, origin) in sorted(contents.items()):
        path = bundle / name
        missing = []
        parent = path.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
        with path.open('xb') as stream:
            stream.write(raw)
        path.chmod(mode)
        entries.append({'path': name, 'sha256': sha(raw), 'size': len(raw), 'mode': mode, 'uid': uid, 'origin': origin})
    manifest = canonical({'schema': 1, 'code_version': commit, 'expected_uid': uid, 'files': entries,
                          'development_launcher': development_launcher is not None})
    (bundle / 'manifest.json').write_bytes(manifest)
    (bundle / 'manifest.json').chmod(0o600)
    manifest_pin, launcher_pin = sha(manifest), sha(launcher)
    expected = f'--expected-manifest-sha256 {manifest_pin} --expected-code-version {commit} --expected-uid {uid}'
    bootstrap = '/usr/bin/python3 -I -S -B ' + shlex.quote(str(bundle / 'installation/launch.py'))
    installed = '/usr/bin/python3 -I -S -B ' + shlex.quote(str(target / 'installation/launch.py'))
    commands = f'''# 실행 패키지 명령서 (실제 설치/운영 변경 미실행)

소스 commit: {commit}. Launcher hash: {launcher_pin}. Manifest hash: {manifest_pin}. UID: {uid}.
모든 설치 파일과 DB 계약은 bundle/manifest.json, schema-contracts.json에 있다.
새 코드/설정의 기대값은 이 검토 산출물에서 외부로 고정한 것이며 소유자 승인값이 아니다.
기존 var 후보의0775 상위경로를 보존하고 target은 {target}로 제안한다.
bootstrap/source 부모는0700이어야 한다. 기존 /opt·automation_mvp·service/timer/PAUSE는 수정하지 않는다.

실행 계정은 codex_shshin이다. 이미 해당 계정으로 로그인했으면 그대로 진행한다.
다른 관리 계정에서는 `sudo -iu codex_shshin`으로 전환한다. HOME을 수동 변경하거나 root로 실행하지 않는다.
`id -un`이 codex_shshin, `id -u`가 {uid}인지 확인하고 `cd {workspace}`로 이동한다.
아래 명령은 승인 후에만 실행한다. 이번 작업은 temp fixture 대상에서만 같은 명령을 검사한다.

```sh
set -eu
cd {shlex.quote(str(workspace))}
printf '%s  %s\\n' '{launcher_pin}' '{bundle}/installation/launch.py' | sha256sum -c -
{bootstrap} verify-bundle {expected}
{bootstrap} stage {expected}
printf '%s  %s\\n' '{launcher_pin}' '{target}/installation/launch.py' | sha256sum -c -
{installed} init-new {expected}
{installed} verify {expected}
{installed} status {expected}
```

현재 PAUSE를 유지한 run 확인은 별도 동작이다. 설치 후 현재 PAUSE가 유지되면 REJECTED/PAUSED exit2가 기대값이다. 임시 fixture 인수의 exit1과 구분한다.

```sh
set -eu
printf '%s  %s\\n' '{launcher_pin}' '{target}/installation/launch.py' | sha256sum -c -
{installed} run {expected}
```

복귀는 아래 별도 명령으로 수행한다. 현재 candidate를 보존하여 퇴역시키며 기존 서비스는 전환하지 않는다.

```sh
set -eu
printf '%s  %s\\n' '{launcher_pin}' '{target}/installation/launch.py' | sha256sum -c -
{installed} rollback {expected} --rollback-to {shlex.quote(str(target) + '.retired')}
```

init-new는 없는state에만, 이미있으면거부하며 자동migration0이다. run은 기존root/user/package PAUSE와23–07KST/최대3 시작을 유지한다.
고정 fixture 정상과exit7의 실제 artifact를 저장하므로 두번째 결과는 FAILED이며 run exit1이 정상 인수 기대값이다. UNKNOWN은재실행하지않고exit3/원래claim을보존한다.
rollback은 exclusive lock 뒤 이번 candidate 전체를 같은parent 새이름으로rename하며 DB/근거를삭제하지않는다. 충돌은덮지않는다. 서비스가활성화된환경의전환명령은포함하지않는다.

## 지원되는 native 연결 최소 대안 (미실행)

queue/list handler의 부작용 계약을확인하지못해가상adapter/직접RPC를추가하지않았다.
공식 CLI reference의codex queue를 기존소유client 경로로사용할수있다. 아래는승인된task1건의명령형태이며자동재전송이나완료확인계약은별도필수다.

```sh
/home/codex_shshin/.local/bin/codex queue --approve-for-me -C {workspace} --thread 01a13fb0-1721-7af3-82e6-747eac4de411 --message '<승인된 단일 작업>'
```

이명령은이번작업에서실행하지않았다. CLI 문서 https://learn.chatgpt.com/docs/cli/reference#codex-queue .
전체controller native dispatch·자동작업두건·설치·서비스전환·PAUSE해제는NOT_RUN이다.
'''
    (output / 'COMMANDS.md').write_text(commands)
    result = {'bundle': str(bundle), 'target_base': str(target), 'code_version': commit, 'expected_uid': uid,
              'manifest_sha256': manifest_pin, 'launcher_sha256': launcher_pin, 'files': len(entries),
              'development_launcher': development_launcher is not None, 'temporary_fixture': temporary_fixture,
              'state_created': False, 'native_dispatch': False, 'owner_approval_verified': False}
    (output / 'DELIVERY.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-repo', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--target-base', required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--expected-uid', type=int, required=True)
    parser.add_argument('--development-launcher')
    parser.add_argument('--temporary-fixture', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build(args.source_repo, args.commit, args.output, args.target_base, args.workspace, args.expected_uid,
                           development_launcher=args.development_launcher, temporary_fixture=args.temporary_fixture), sort_keys=True))


if __name__ == '__main__':
    main()
