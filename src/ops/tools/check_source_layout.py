"""Check import/root/build connections without reading runtime state or starting services."""
import ast
import importlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def check(root=ROOT):
    failures=[];count=0
    legacy={'agent_platform','data_platform','knowledge_platform','model_platform',
            'ops_platform','ops_tools','ops_candidates','apps','infra'}
    for name in legacy:
        if (root/name).exists() or (root/name).is_symlink():failures.append('LEGACY_ROOT:'+name)
    for path in sorted((root/'src').rglob('*.py')):
        rel=path.relative_to(root)
        if 'candidates' in rel.parts and 'source' in rel.parts:continue
        count+=1
        try:tree=ast.parse(path.read_text(),feature_version=(3,10))
        except SyntaxError:
            failures.append('PYTHON310_SYNTAX:'+str(rel));continue
        for node in ast.walk(tree):
            names=([node.module] if isinstance(node,ast.ImportFrom) and node.module else
                   [n.name for n in node.names] if isinstance(node,ast.Import) else [])
            for name in names:
                if name.split('.')[0] in legacy:failures.append('LEGACY_IMPORT:'+str(rel))
    # Load the actual entrypoint modules; __main__ routines are not invoked.
    for name in ['src.apps.document_qa.api','src.ops.platform.ops.manage',
                 'src.ops.platform.ops.refresh_worker','src.ops.tools.check_boot_candidate']:
        try:importlib.import_module(name)
        except Exception:failures.append('IMPORT_FAILED:'+name)
    import platform
    if Path(platform.__file__).resolve().is_relative_to(root/'src'):
        failures.append('STDLIB_PLATFORM_SHADOWED')
    from src.ops.platform.ops import manage,refresh_worker,docker_acceptance
    if manage.ROOT.parents[2]!=root or refresh_worker.ROOT!=root or docker_acceptance.ROOT!=root:
        failures.append('OPS_REPOSITORY_ROOT_INVALID')
    if not docker_acceptance.source_files(root):failures.append('EMPTY_OPS_SOURCE_CLOSURE')
    includes=0
    for path in sorted((root/'src').rglob('docker-compose.yml')):
        if 'candidates' in path.parts:continue
        text=path.read_text()
        # Included Compose files use ./ sibling paths. No interpolation or secret evaluation.
        if '\ninclude:' in text:
            body=text.split('\ninclude:',1)[1].split('\n\n',1)[0]
            for rel in re.findall(r'^\s+- (?:path: )?(\./[^\s]+\.yml)\s*$',body,re.M):
                includes+=1
                if not (path.parent/rel).is_file():failures.append('COMPOSE_INCLUDE:'+str(path.relative_to(root)))
    operational=root/'src/platform/agent/n8n/tests/operational'
    text=(operational/'docker-compose.yml').read_text()
    context=re.search(r'^\s+context: (\.\.[^\s]*)$',text,re.M)
    if not context or (operational/context.group(1)).resolve()!=root:
        failures.append('OPERATIONAL_BUILD_CONTEXT_INVALID')
    if 'dockerfile: src/platform/agent/n8n/tests/operational/Dockerfile' not in text:
        failures.append('OPERATIONAL_DOCKERFILE_INVALID')
    docker=(root/'src/ops/platform/Dockerfile').read_text()
    compose=(root/'src/ops/platform/docker-compose.yml').read_text()
    if '/app/src/ops/platform/' not in docker or 'src.ops.platform.server' not in docker:
        failures.append('OPS_IMAGE_NAMESPACE_INVALID')
    if '/app/src/ops/platform/healthcheck.py' not in compose:
        failures.append('OPS_IMAGE_HEALTHCHECK_INVALID')
    commands=[['-m','src.apps.document_qa.api','--help'],
              [str(root/'src/ops/platform/ops/manage.py'),'--help'],
              [str(root/'src/ops/platform/ops/platform_status.py'),'--help']]
    for command in commands:
        proc=subprocess.run([sys.executable,'-B',*command],cwd=root,
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
        if proc.returncode:failures.append('CLI_HELP_FAILED:'+command[-2])
    return {'source_layout_check_passed':not failures,'python310_source_files_checked':count,
            'compose_include_paths_checked':includes,'failure_codes':sorted(set(failures)),
            'runtime_state_read':False,'services_started':False,'live_acceptance_completed':False}


if __name__=='__main__':
    result=check();print(json.dumps(result));sys.exit(0 if result['source_layout_check_passed'] else 1)
