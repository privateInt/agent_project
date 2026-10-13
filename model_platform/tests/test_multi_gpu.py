import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import live_dataset_smoke as smoke

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load('distributed_train', ROOT / 'jobs/training/axolotl/distributed_train.py')
adapter = load('config_adapter_multi', ROOT / 'jobs/training/axolotl/config_adapter.py')


class DistributedWorkerTests(unittest.TestCase):
    def test_local_rank_validation(self):
        env = dict(WORLD_SIZE='2', LOCAL_WORLD_SIZE='2', RANK='1', LOCAL_RANK='1')
        self.assertEqual(worker.rank_settings(env, 2, 2), (1, 1, 2))
        for changed, visible in [({'LOCAL_RANK': '0'}, 2), ({'WORLD_SIZE': '4'}, 2), ({}, 1), ({'LOCAL_WORLD_SIZE': '1'}, 2)]:
            with self.subTest(changed=changed, visible=visible), self.assertRaises(RuntimeError):
                worker.rank_settings(dict(env, **changed), 2, visible)

    def exercise(self, directory, *, wrapped=True, steps=1, skip=False):
        class DDP:
            pass

        class Trainer:
            def train(self):
                self.model_wrapped = DDP() if wrapped else object()
                self.state = types.SimpleNamespace(global_step=steps)
                return object()

        distributed = types.ModuleType('torch.distributed')
        distributed.is_initialized = lambda: True
        distributed.init_process_group = lambda **kwargs: None
        distributed.destroy_process_group = lambda: None
        distributed.all_reduce = lambda value: None
        distributed.barrier = lambda: None
        distributed.get_world_size = lambda: 2
        distributed.get_rank = lambda: 0
        torch = types.ModuleType('torch')
        torch.distributed = distributed
        torch.cuda = types.SimpleNamespace(device_count=lambda: 2, set_device=lambda rank: None,
                                          get_device_properties=lambda rank: types.SimpleNamespace(uuid='GPU-a'))
        torch.nn = types.SimpleNamespace(parallel=types.SimpleNamespace(DistributedDataParallel=DDP))
        torch.ones = lambda *args, **kwargs: types.SimpleNamespace(item=lambda: 2)
        transformers = types.ModuleType('transformers')
        transformers.Trainer = Trainer
        modules = {'torch': torch, 'torch.distributed': distributed, 'transformers': transformers}
        argv = ['distributed_train.py', '--config', '/fake/config.yml', '--evidence-dir', directory,
                '--world-size', '2', '--training-run-id', 'test-run', '--max-steps', '1']
        env = dict(WORLD_SIZE='2', LOCAL_WORLD_SIZE='2', RANK='0', LOCAL_RANK='0')
        with patch.dict(sys.modules, modules), patch.object(sys, 'argv', argv), patch.dict(worker.os.environ, env), \
                patch.object(worker.runpy, 'run_module', side_effect=lambda *a, **kw: None if skip else Trainer().train()):
            worker.main()

    def test_only_completed_ddp_writes_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            self.exercise(directory)
            result = json.loads((Path(directory) / 'rank-0.json').read_text())
            self.assertTrue(result['ddp_wrapped'])
            self.assertEqual(result['global_step'], 1)
            self.assertEqual(result['training_run_id'], 'test-run')

    def test_unwrapped_unfinished_or_skipped_training_has_no_success_file(self):
        for options in ({'wrapped': False}, {'steps': 0}, {'skip': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(RuntimeError):
                    self.exercise(directory, **options)
                self.assertFalse((Path(directory) / 'rank-0.json').exists())


class MultiGPUConfigTests(unittest.TestCase):
    def test_single_and_ddp_configs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = root / 'model'
            model.mkdir()
            (model / 'config.json').write_text('{"model_type":"gemma3_text"}')
            spec = dict(model_dir=str(model), dataset=dict(source_type='synthetic', format='chat_messages'),
                        training=dict(method='qlora', sequence_length=256, sample_packing=False,
                                      lora=dict(r=8, alpha=16, dropout=0), gradient_accumulation_steps=1,
                                      micro_batch_size=1, max_steps=1, learning_rate=0.0002))
            for world in (1, 2):
                spec['world_size'] = world
                result = adapter.render_config(spec, root)
                config = Path(result['config_path']).read_text()
                self.assertIn('ddp_find_unused_parameters: ' + ('true' if world == 1 else 'false'), config)
                self.assertEqual('use_reentrant: false' in config, world == 2)
                self.assertIn('\nlogging_steps: 1\n', config)
                self.assertNotIn('fsdp:', config)
                self.assertNotIn('deepspeed:', config)
            spec['training']['strategy'] = 'fsdp'
            with self.assertRaises(RuntimeError):
                adapter.render_config(spec, root)


class ParallelSmokeTests(unittest.TestCase):
    def valid_run(self):
        ranks = [dict(rank=i, local_rank=i, world_size=2, training_run_id='run', device_uuid=f'GPU-{i}',
                      initialized=True, ddp_wrapped=True, completed=True, global_step=1) for i in range(2)]
        return dict(resources=dict(gpu_count=2, gpu_ids=[1, 3]), training=dict(strategy='ddp'),
                    result=dict(parallelism=dict(strategy='ddp', world_size=2, effective_batch_size=2, ranks=ranks)))

    def test_accepts_observed_local_ddp(self):
        smoke.check_parallelism(self.valid_run(), [1, 3], 'run')

    def test_rejects_single_gpu_fallback_missing_and_duplicate_ranks(self):
        for mutate in (lambda p: p.update(world_size=1), lambda p: p.update(ranks=p['ranks'][:1]),
                       lambda p: p['ranks'][1].update(device_uuid='GPU-0'),
                       lambda p: p['ranks'][1].update(ddp_wrapped=False),
                       lambda p: p['ranks'][1].update(training_run_id='old')):
            run = copy.deepcopy(self.valid_run())
            mutate(run['result']['parallelism'])
            with self.assertRaises(RuntimeError):
                smoke.check_parallelism(run, [1, 3], 'run')


if __name__ == '__main__':
    unittest.main()
