"""One worker per torchrun rank. Audit real Trainer DDP participation and steps.

Only the parent Go Job performs MLflow linkage and artifact publication. This
module uses Axolotl's documented module CLI, so it never nests another launcher.
"""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import runpy
import sys


def rank_settings(environ, expected, visible_count):
    world = int(environ.get('WORLD_SIZE', '0'))
    rank = int(environ.get('RANK', '-1'))
    local = int(environ.get('LOCAL_RANK', '-1'))
    local_world = int(environ.get('LOCAL_WORLD_SIZE', '0'))
    if not (2 <= expected <= 16 and world == expected == local_world == visible_count
            and 0 <= rank < world and local == rank):
        raise RuntimeError('DDP requires one local rank per visible GPU on exactly one host')
    return rank, local, world


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--evidence-dir', required=True)
    parser.add_argument('--world-size', type=int, required=True)
    parser.add_argument('--training-run-id', required=True)
    parser.add_argument('--max-steps', type=int, required=True)
    args = parser.parse_args()
    import torch
    import torch.distributed as dist
    from transformers import Trainer

    rank, local, world = rank_settings(os.environ, args.world_size, torch.cuda.device_count())
    torch.cuda.set_device(local)
    device_uuid = str(getattr(torch.cuda.get_device_properties(local), 'uuid', '')).strip()
    if not device_uuid:
        raise RuntimeError('CUDA device UUID is required to verify distinct DDP devices')
    dist.init_process_group(backend='nccl', timeout=timedelta(minutes=10))
    # Prove every worker joined the same group before loading the model.
    count = torch.ones(1, device=f'cuda:{local}')
    dist.all_reduce(count)
    if int(count.item()) != world:
        raise RuntimeError('DDP collective did not include the requested workers')

    observed = {}
    original_train = Trainer.train

    def audited_train(trainer, *positional, **keywords):
        result = original_train(trainer, *positional, **keywords)
        if observed:
            raise RuntimeError('expected exactly one Trainer.train call per rank')
        wrapped = isinstance(trainer.model_wrapped, torch.nn.parallel.DistributedDataParallel)
        if (not dist.is_initialized() or dist.get_world_size() != world
                or dist.get_rank() != rank or not wrapped
                or trainer.state.global_step != args.max_steps):
            raise RuntimeError('Trainer did not complete the requested DDP steps')
        observed.update(rank=rank, local_rank=local, world_size=world,
                        training_run_id=args.training_run_id, device_uuid=device_uuid,
                        initialized=True, ddp_wrapped=wrapped, completed=True,
                        global_step=trainer.state.global_step)
        return result

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
        # Write evidence only after the Axolotl module has also saved its output.
        dist.barrier()
        target = Path(args.evidence_dir) / f'rank-{rank}.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(observed, sort_keys=True) + '\n', encoding='utf-8')
        temporary.replace(target)
        print('[model-platform-ddp] ' + json.dumps(observed, sort_keys=True), flush=True)
    finally:
        Trainer.train = original_train
        if dist.is_initialized():
            dist.destroy_process_group()


if __name__ == '__main__':
    main()
