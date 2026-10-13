"""Atomic, immutable snapshots of fully saved single-GPU Trainer checkpoints.

Only the local trainer writes these files. No uploaded pickle/checkpoint input
is accepted. Incomplete temporary snapshots are never selectable for resume.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

REQUIRED = {'adapter_config.json', 'adapter_model.safetensors', 'optimizer.pt',
            'scheduler.pt', 'rng_state.pth', 'trainer_state.json', 'training_args.bin'}
MAX_RECEIPT = 4 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def real_directory(path):
    # Validate all components, not just the leaf; mounted data is local/trusted.
    for p in [path, *path.parents]:
        if not stat.S_ISDIR(p.lstat().st_mode):
            raise RuntimeError('checkpoint path must contain real directories')


def manifest(path):
    real_directory(path)
    entries = []
    for p in sorted(path.rglob('*')):
        mode = p.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode) or p.stat().st_size <= 0:
            raise RuntimeError('checkpoint contains a link, special or empty file')
        entries.append({'path': p.relative_to(path).as_posix(), 'size_bytes': p.stat().st_size,
                        'sha256': file_hash(p)})
    if not entries or len(entries) > 4096:
        raise RuntimeError('invalid checkpoint file count')
    return entries


def validate_state(data, step, max_steps):
    if not REQUIRED.issubset({p.name for p in data.iterdir()}):
        raise RuntimeError('checkpoint is missing model/optimizer/scheduler/RNG/trainer state')
    state = json.loads((data / 'trainer_state.json').read_text())
    if type(state.get('global_step')) is not int or state['global_step'] != step:
        raise RuntimeError('checkpoint trainer step mismatch')
    if state.get('max_steps') != max_steps or not 0 < step <= max_steps:
        raise RuntimeError('checkpoint total training steps changed')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def seal(saved, source, metadata, step, signature):
    root = saved / 'checkpoints'
    root.mkdir(parents=True, exist_ok=True)
    real_directory(root)
    destination = root / f'checkpoint-{step}'
    if destination.exists():
        raise RuntimeError('refusing to overwrite a sealed checkpoint')
    validate_state(source, step, metadata['training']['max_steps'])
    original = manifest(source)
    temporary = Path(tempfile.mkdtemp(prefix='.saving-', dir=root))
    try:
        shutil.copytree(source, temporary / 'data', symlinks=True)
        copied = manifest(temporary / 'data')
        if copied != original:
            raise RuntimeError('checkpoint changed during preservation')
        receipt = dict(metadata, version=1, step=step, max_steps=metadata['training']['max_steps'],
                       signature=signature, files=copied, size_bytes=sum(x['size_bytes'] for x in copied))
        raw = canonical(receipt)
        if len(raw) > MAX_RECEIPT:
            raise RuntimeError('checkpoint receipt too large')
        (temporary / 'receipt.json').write_bytes(raw)
        for p in temporary.rglob('*'):
            if p.is_file():
                with p.open('rb') as handle:
                    os.fsync(handle.fileno())
        for p in sorted([temporary, *[p for p in temporary.rglob('*') if p.is_dir()]],
                        key=lambda p: len(p.parts), reverse=True):
            sync_dir(p)
        temporary.rename(destination)
        sync_dir(root)
        sync_dir(saved)
        sync_dir(saved.parent)
        sync_dir(saved.parent.parent)
        # Keep two committed copies. A failed new save never removes an old one.
        committed = sorted((p for p in root.iterdir() if re.fullmatch(r'checkpoint-[1-9][0-9]*', p.name)),
                           key=lambda p: int(p.name.split('-')[1]), reverse=True)
        for old in committed[2:]:
            real_directory(old)
            shutil.rmtree(old)
        sync_dir(root)
        return hashlib.sha256(raw).hexdigest()
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify(directory, digest, signature):
    real_directory(directory)
    receipt_path = directory / 'receipt.json'
    if not stat.S_ISREG(receipt_path.lstat().st_mode) or receipt_path.stat().st_size > MAX_RECEIPT:
        raise RuntimeError('invalid checkpoint receipt')
    raw = receipt_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise RuntimeError('checkpoint receipt changed')
    receipt = json.loads(raw)
    if receipt['version'] != 1 or receipt['signature'] != signature:
        raise RuntimeError('checkpoint model, dataset, training settings or runtime version changed')
    files = manifest(directory / 'data')
    if files != receipt['files'] or sum(x['size_bytes'] for x in files) != receipt['size_bytes']:
        raise RuntimeError('checkpoint content changed')
    validate_state(directory / 'data', receipt['step'], receipt['max_steps'])
    return receipt


def copy_for_resume(source, target, digest, signature):
    receipt = verify(source, digest, signature)
    shutil.copytree(source / 'data', target, symlinks=True)
    if manifest(target) != receipt['files']:
        raise RuntimeError('checkpoint copy verification failed')
    return receipt


def prepare_context(spec, workspace, dataset_path, model_dir):
    import importlib.metadata
    import os
    metadata = spec['checkpoint_metadata']
    versions = {name: importlib.metadata.version(name) for name in
                ('axolotl', 'transformers', 'torch', 'peft', 'bitsandbytes', 'accelerate')}
    source_dir = Path(__file__).parent
    # Pin the actual downloaded model/data bytes and the translation/audit code.
    signature = hashlib.sha256(canonical(dict(model=manifest(model_dir),
        dataset=file_hash(dataset_path), training=metadata['training'], versions=versions,
        code={name: file_hash(source_dir / name) for name in
              ('config_adapter.py', 'checkpoint_store.py', 'checkpoint_train.py')}))).hexdigest()
    saved = Path(spec['output_dir']).parent
    real_directory(saved)
    resume = spec.get('resume') or {}
    resume_path = ''
    if resume:
        if not 0 < resume['checkpoint_step'] < metadata['training']['max_steps']:
            raise RuntimeError('checkpoint has no remaining training steps')
        resume_path = str(workspace / 'resume-source')
        receipt = copy_for_resume(Path(spec['resume_directory']), Path(resume_path),
                                  resume['checkpoint_sha256'], signature)
        if (receipt['training_run_id'] != resume['source_training_run_id'] or
                receipt['source_model_version_id'] != metadata['source_model_version_id'] or
                receipt['step'] != resume['checkpoint_step']):
            raise RuntimeError('checkpoint source identity mismatch')
    context = dict(metadata=metadata, saved_dir=str(saved), resume=resume,
                   resume_path=resume_path, signature=signature,
                   fail_after_save=os.environ.get('TRAINING_TEST_STOP_AFTER_CHECKPOINT_RUN_ID') == metadata['training_run_id'],
                   fixture_mode=os.environ.get('TRAINING_TEST_CHECKPOINT_MODE', 'failed'))
    (workspace / 'checkpoint-context.json').write_bytes(canonical(context))
    return resume_path
