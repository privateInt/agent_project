"""Evaluate final trained weights on pinned reference answers, without updates."""
import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import runpy
import sys
import time

from evaluation_data import PROTOCOL, canonical, sha, read_pinned, check_disjoint, encode_evaluation


def aggregate(nlls, counts):
    if not nlls or len(nlls) != len(counts) or any(type(n) is not int or n <= 0 for n in counts):
        raise ValueError('evaluation has no scored answer tokens')
    if any(not math.isfinite(n) or n < 0 for n in nlls):
        raise ValueError('evaluation returned invalid token losses')
    total = sum(counts)
    nll = math.fsum(nlls)
    loss = nll / total
    if not math.isfinite(nll) or not math.isfinite(loss):
        raise ValueError('evaluation loss is not finite')
    return dict(nll_sum=nll, scored_tokens=total, loss=loss,
                perplexity=math.exp(loss) if loss <= 700 else None,
                perplexity_overflow=loss > 700)


def evaluate(trainer, context, encoded):
    import torch
    import torch.nn.functional as F
    if trainer.state.global_step != context['max_steps']:
        raise RuntimeError('evaluation requires the final requested training step')
    if not trainer.args.bf16 or trainer.args.world_size != 1:
        raise RuntimeError('evaluation requires single-GPU BF16 training')
    model = trainer.model
    device = model.get_input_embeddings().weight.device
    if device.type != 'cuda':
        raise RuntimeError('evaluation requires the trained CUDA model')
    was_training = model.training
    model.eval()
    nlls, counts = [], []
    print('[model-platform-evaluation] running held-out final-answer scoring', flush=True)
    try:
        with torch.random.fork_rng(devices=[device]), torch.inference_mode():
            for row in encoded:
                ids = torch.tensor([row['input_ids']], dtype=torch.long, device=device)
                with torch.autocast(device_type='cuda', dtype=torch.bfloat16):
                    outputs = model(input_ids=ids, attention_mask=torch.ones_like(ids), use_cache=False, return_dict=True)
                logits = outputs.logits
                if logits.ndim != 3 or logits.shape[0] != 1 or logits.shape[1] != ids.shape[1]:
                    raise RuntimeError('model did not return all causal token logits')
                start = row['prefix_tokens']
                targets = ids[0, start:]
                # Position t-1 predicts token t. Prompt tokens are never scored.
                answer_logits = logits[0, start-1:-1, :].float()
                loss_sum = F.cross_entropy(answer_logits, targets, reduction='sum')
                nlls.append(float(loss_sum.item()))
                counts.append(int(targets.numel()))
                del outputs, logits, answer_logits, loss_sum, ids, targets
    finally:
        model.train(was_training)
    values = aggregate(nlls, counts)
    pin = context['spec']['dataset']
    tokenized_hash = sha(canonical(encoded))
    versions = {name: importlib.metadata.version(name) for name in ('axolotl','transformers','torch','peft','bitsandbytes','accelerate')}
    source = Path(__file__).parent
    compatibility = sha(canonical(dict(protocol=PROTOCOL, source_model_version_id=context['source_model_version_id'],
        base_sha256=context['base_sha256'], dataset_sha256=pin['sha256'], tokenized_sha256=tokenized_hash,
        sequence_length=context['sequence_length'], precision='qlora_bf16_logits_fp32', versions=versions,
        code={name:sha((source/name).read_bytes()) for name in ('evaluation_data.py','evaluation_train.py','evaluation_config.py')})))
    return dict(values, protocol=PROTOCOL, compatibility_sha256=compatibility,
                training_run_id=context['training_run_id'], source_model_version_id=context['source_model_version_id'],
                dataset_version_id=pin['dataset_version_id'], dataset_sha256=pin['sha256'],
                record_count=len(encoded), sequence_length=context['sequence_length'],
                trained_steps=trainer.state.global_step, tokenized_sha256=tokenized_hash)


def log_metrics(context, result, run_id):
    if not context['tracking_enabled']:
        return
    if not run_id:
        raise RuntimeError('evaluation MLflow run is not linked')
    from mlflow.tracking import MlflowClient
    from mlflow.entities import Metric, RunTag
    now = int(time.time()*1000)
    names = ['loss', 'scored_tokens', 'record_count']
    if result['perplexity'] is not None:
        names.append('perplexity')
    MlflowClient().log_batch(run_id, metrics=[Metric('heldout_'+name, float(result[name]), now, result['trained_steps']) for name in names],
        tags=[RunTag('heldout_protocol', result['protocol']), RunTag('heldout_dataset_version_id',result['dataset_version_id']),
              RunTag('heldout_compatibility_sha256',result['compatibility_sha256'])])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--context', required=True)
    args = parser.parse_args()
    context = json.loads(Path(args.context).read_text())
    # MLflow consumes MLFLOW_RUN_ID when resuming the linked run. Keep the
    # launch-time identity before importing/invoking Trainer and its callbacks.
    mlflow_run_id = os.environ.get('MLFLOW_RUN_ID') if context['tracking_enabled'] else None
    if context['tracking_enabled'] and not mlflow_run_id:
        raise RuntimeError('evaluation MLflow run is not linked before training')
    import torch
    from transformers import Trainer
    if torch.cuda.device_count() != 1 or int(os.environ.get('WORLD_SIZE','1')) != 1:
        raise RuntimeError('evaluation requires exactly one local GPU')
    original_train = Trainer.train
    observed = {}
    called = False

    def evaluated_train(trainer, *positional, **keywords):
        nonlocal called
        if called:
            raise RuntimeError('expected one training invocation for held-out evaluation')
        called = True
        tokenizer = getattr(trainer, 'processing_class', None) or getattr(trainer, 'tokenizer', None)
        if tokenizer is None or not callable(getattr(tokenizer, 'apply_chat_template', None)):
            raise RuntimeError('trained tokenizer is unavailable')
        train = read_pinned(context['training_path'], context['train_dataset'])
        evaluation = read_pinned(context['dataset_path'], context['spec']['dataset'])
        check_disjoint(train, evaluation)
        encoded = encode_evaluation(tokenizer, train, evaluation, context['sequence_length'])
        result = original_train(trainer, *positional, **keywords)
        observed.update(evaluate(trainer, context, encoded))
        log_metrics(context, observed, mlflow_run_id)
        return result

    Trainer.train = evaluated_train
    try:
        checkpoint = Path(args.context).parent/'checkpoint-context.json'
        try:
            if checkpoint.exists():
                import checkpoint_train
                sys.argv = ['checkpoint_train', '--config', args.config, '--context', str(checkpoint)]
                checkpoint_train.main()
            else:
                sys.argv = ['axolotl.cli.train', args.config]
                runpy.run_module('axolotl.cli.train', run_name='__main__')
        except SystemExit as exc:
            if exc.code not in (None,0):
                raise
        if not observed:
            raise RuntimeError('Axolotl returned without held-out evaluation')
        target = Path(args.context).parent/'evaluation-result.json'
        temp = target.with_suffix('.pending')
        temp.write_bytes(canonical(observed));temp.replace(target)
        print('[model-platform-evaluation-complete] '+json.dumps(observed,allow_nan=False),flush=True)
    finally:
        Trainer.train = original_train


if __name__ == '__main__':
    main()
