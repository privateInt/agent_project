"""Audit real single-GPU checkpoint restoration and commit completed saves."""
import argparse
import json
from pathlib import Path
import runpy
import sys

from checkpoint_store import canonical, real_directory, seal


class CheckpointAudit:
    def __init__(self, context):
        self.context = context
        self.start = context['resume'].get('checkpoint_step', 0)
        self.total = context['metadata']['training']['max_steps']
        self.steps = 0
        self.started = False
        self.loaded = {'model': False, 'optimizer': False, 'scheduler': False, 'rng': False}

    def matches(self, path):
        return bool(path) and Path(path).resolve() == Path(self.context['resume_path']).resolve()

    def begin(self, trainer, state):
        if self.started or state.global_step != self.start or state.max_steps != self.total:
            raise RuntimeError('Trainer did not start at the pinned checkpoint step')
        self.started = True
        if self.start:
            if not self.loaded['model'] or not self.loaded['optimizer'] or not trainer.optimizer.state:
                raise RuntimeError('checkpoint model or optimizer state was not restored')
            if int(trainer.lr_scheduler.state_dict().get('last_epoch', -1)) != self.start:
                raise RuntimeError('checkpoint scheduler step was not restored')
            self.loaded['scheduler'] = True

    def step(self, state):
        if not self.started or state.global_step != self.start + self.steps + 1:
            raise RuntimeError('Trainer skipped or repeated an optimizer step')
        self.steps += 1

    def evidence(self, state):
        if not self.started or state.global_step != self.total or self.steps != self.total - self.start:
            raise RuntimeError('Trainer did not finish the remaining steps')
        if self.start and not all(self.loaded.values()):
            raise RuntimeError('checkpoint restoration was incomplete')
        return dict(version=1, training_run_id=self.context['metadata']['training_run_id'],
                    source_training_run_id=self.context['resume'].get('source_training_run_id', ''),
                    checkpoint_sha256=self.context['resume'].get('checkpoint_sha256', ''),
                    start_step=self.start, final_step=state.global_step, executed_steps=self.steps,
                    **{key + '_loaded': value for key, value in self.loaded.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--context', required=True)
    args = parser.parse_args()
    context = json.loads(Path(args.context).read_text())
    import os
    import torch
    from transformers import Trainer, TrainerCallback
    if torch.cuda.device_count() != 1 or int(os.environ.get('WORLD_SIZE', '1')) != 1:
        raise RuntimeError('checkpoint training requires exactly one local GPU')
    audit = CheckpointAudit(context)
    original_train = Trainer.train
    observed = {}

    def audited_train(trainer, *positional, **keywords):
        if observed:
            raise RuntimeError('expected exactly one Trainer.train invocation')
        resume_arg = keywords.get('resume_from_checkpoint', positional[0] if positional else None)
        if audit.start and not audit.matches(resume_arg):
            raise RuntimeError('Axolotl did not pass the pinned resume checkpoint to Trainer')
        if not audit.start and resume_arg:
            raise RuntimeError('unexpected automatic checkpoint resume')
        restored = []
        if audit.start:
            for method, kind in [('_load_from_checkpoint', 'model'),
                                 ('_load_optimizer_and_scheduler', 'optimizer'),
                                 ('_load_rng_state', 'rng')]:
                original = getattr(trainer, method)
                own = trainer.__dict__.get(method)
                def wrapped(path, *p, _original=original, _kind=kind, **kw):
                    if not audit.matches(path):
                        raise RuntimeError('Trainer attempted to load an unpinned checkpoint')
                    result = _original(path, *p, **kw)
                    audit.loaded[_kind] = True
                    return result
                setattr(trainer, method, wrapped)
                restored.append((method, own))

        class Callback(TrainerCallback):
            def on_train_begin(self, args, state, control, **kwargs):
                if args.save_only_model or args.ignore_data_skip:
                    raise RuntimeError('checkpoint resume requires optimizer state and data skipping')
                audit.begin(trainer, state)
            def on_step_end(self, args, state, control, **kwargs):
                audit.step(state)
            def on_save(self, args, state, control, **kwargs):
                if Path(args.output_dir).resolve() != (Path(context['saved_dir']) / 'output').resolve():
                    raise RuntimeError('Trainer checkpoint output path changed')
                source = Path(args.output_dir) / f'checkpoint-{state.global_step}'
                real_directory(source)
                digest = seal(Path(context['saved_dir']), source, context['metadata'],
                              state.global_step, context['signature'])
                print('[model-platform-checkpoint] ' + json.dumps({'step': state.global_step,
                       'checkpoint_sha256': digest}), flush=True)
                if context.get('fail_after_save') and state.global_step < audit.total:
                    if context.get('fixture_mode') == 'cancelled':
                        import time
                        print('MODEL_PLATFORM_CHECKPOINT_PAUSE_FIXTURE', flush=True)
                        while True:
                            time.sleep(1)
                    raise RuntimeError('MODEL_PLATFORM_CHECKPOINT_STOP_FIXTURE')
        callback = Callback()
        trainer.add_callback(callback)
        try:
            result = original_train(trainer, *positional, **keywords)
            observed.update(audit.evidence(trainer.state))
            return result
        finally:
            trainer.remove_callback(callback)
            for method, own in restored:
                if own is None:
                    delattr(trainer, method)
                else:
                    setattr(trainer, method, own)

    Trainer.train = audited_train
    try:
        sys.argv = ['axolotl.cli.train', args.config]
        try:
            runpy.run_module('axolotl.cli.train', run_name='__main__')
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
        if not observed:
            raise RuntimeError('Axolotl returned without audited training')
        target = Path(args.context).parent / 'checkpoint-evidence.json'
        temporary = target.with_suffix('.pending')
        temporary.write_bytes(canonical(observed))
        temporary.replace(target)
        print('[model-platform-checkpoint-complete] ' + json.dumps(observed), flush=True)
    finally:
        Trainer.train = original_train


if __name__ == '__main__':
    main()
