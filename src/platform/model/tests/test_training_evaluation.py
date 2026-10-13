import copy
from contextlib import nullcontext
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'jobs/training/axolotl'

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

data = load('evaluation_data')
with patch.dict(sys.modules, evaluation_data=data):
    worker = load('evaluation_train')


def row(question='Q', answer='A'):
    return {'messages': [{'role':'user','content':question}, {'role':'assistant','content':answer}]}


def pin_file(path, rows):
    raw = b''.join(data.canonical(r) + b'\n' for r in rows)
    path.write_bytes(raw)
    return dict(source_type='version', dataset_version_id=path.name, size_bytes=len(raw),
                sha256=data.sha(raw), record_count=len(rows))


class Tokenizer:
    def apply_chat_template(self, conversation, tokenize, add_generation_prompt, return_dict):
        assert tokenize and not return_dict
        ids = [1]
        for item in conversation:
            ids += [2 if item['role']=='user' else 3]
            ids += [ord(c) for c in item['content'].lower()]
            ids += [4]
        if add_generation_prompt: ids += [3]
        return ids


class EvaluationDataTests(unittest.TestCase):
    def test_full_pinned_file_hash_and_count_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'data';rows = [row('Q'+str(i)) for i in range(30)]
            pin = pin_file(path, rows)
            self.assertEqual(data.read_pinned(path,pin), rows)
            for change in ('hash','count','bytes','source','late-invalid'):
                changed = dict(pin)
                if change=='hash': changed['sha256']='0'*64
                if change=='count': changed['record_count']=29
                if change=='bytes': changed['size_bytes']+=1
                if change=='source': changed['source_type']='s3'
                if change=='late-invalid': changed=pin_file(path, rows+[{'messages':[]}])
                with self.subTest(change=change), self.assertRaises(ValueError): data.read_pinned(path,changed)

    def test_raw_prompts_cannot_leak_through_different_answers_or_normalization(self):
        for train, evaluation in ((row(' Q ','old'),row('Q','new')), (row('é'),row('e\u0301'))):
            with self.subTest(train=train), self.assertRaisesRegex(ValueError,'overlap'):
                data.check_disjoint([train],[evaluation])
        data.check_disjoint([row('train')],[row('eval')])

    def test_earlier_evaluation_turn_cannot_have_been_trained(self):
        multi={'messages':row('train','answer')['messages']+row('new','reference')['messages']}
        with self.assertRaisesRegex(ValueError,'overlap'): data.check_disjoint([row('train')],[multi])
        with self.assertRaisesRegex(ValueError,'duplicate'): data.check_disjoint([row('other')],[row('Q'),row('Q','different')])

    def test_roles_empty_reference_and_duplicate_json_fields_rejected(self):
        invalid=[{'messages':[]},row('Q',' '),{'messages':[{'role':'assistant','content':'first'}, {'role':'user','content':'last'}]},
                 {'messages':row()['messages']+[{'role':'user','content':'unanswered'}]}, {'Messages':row()['messages']},
                 {'messages':[{'role':'user','content':'Q','name':'extra'}, {'role':'assistant','content':'A'}]}]
        for value in invalid:
            with self.subTest(value=value),self.assertRaises(ValueError):data.messages(value)
        with self.assertRaises(ValueError):json.loads('{"messages":[],"messages":[]}',object_pairs_hook=data.unique_object)

    def test_tokenized_overlap_and_duplicate_eval_prompts_rejected(self):
        tokenizer=Tokenizer()
        for train,evaluation in (([row('TRAIN')],[row('train')]),([row('other')],[row('EVAL'),row('eval')])):
            data.check_disjoint(train,evaluation) # Raw strings differ; tokenizer folds case.
            with self.assertRaisesRegex(ValueError,'tokenization'):data.encode_evaluation(tokenizer,train,evaluation,256)

    def test_final_answer_suffix_covers_closing_token_without_truncation(self):
        encoded=data.encode_evaluation(Tokenizer(),[row('train')],[row('eval','AB')],256)[0]
        self.assertEqual(encoded['input_ids'][encoded['prefix_tokens']:],[ord('a'),ord('b'),4])
        for limit in (2,len(encoded['input_ids'])-1):
            with self.assertRaisesRegex(ValueError,'truncation'):data.encode_evaluation(Tokenizer(),[],[row('eval','AB')],limit)
        class Broken(Tokenizer):
            def apply_chat_template(self,*args,**kw):
                ids=super().apply_chat_template(*args,**kw)
                return [99]+ids if kw['add_generation_prompt'] else ids
        with self.assertRaisesRegex(ValueError,'suffix'):data.encode_evaluation(Broken(),[],[row()],256)

    def test_prepare_keeps_eval_out_of_axolotl_training_dataset(self):
        adapter=load('config_adapter')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);model=root/'model';model.mkdir();(model/'config.json').write_text('{}')
            train=pin_file(root/'train',[row('train1'),row('train2')]);ev=pin_file(root/'eval',[row('eval1'),row('eval2')])
            spec=dict(model_dir=str(model),world_size=1,dataset=dict(source_type='version',local_path=str(root/'train'),format='chat_messages'),
                      evaluation=dict(spec={'dataset':ev},train_dataset=train,dataset_path=str(root/'eval'),training_run_id='run',source_model_version_id='base'),
                      training=dict(method='qlora',sequence_length=256,max_steps=2,micro_batch_size=1,gradient_accumulation_steps=1,learning_rate=.0002,lora=dict(r=8,alpha=16,dropout=0),sample_packing=False))
            with patch.dict(sys.modules,checkpoint_store=load('checkpoint_store')):context=data.prepare(spec,root)
            rendered=adapter.render_config(spec,root)
            config=Path(rendered['config_path']).read_text()
            self.assertIn(str(root/'train'),config)
            self.assertNotIn(str(root/'eval'),config)
            self.assertEqual(context['spec']['dataset'],ev)
            self.assertEqual(len(context['base_sha256']),64)


class EvaluationMetricTests(unittest.TestCase):
    def test_loss_is_weighted_by_tokens_including_unequal_answers(self):
        result=worker.aggregate([2.,12.],[1,3])
        self.assertEqual(result['loss'],3.5) # Mean per-record loss would incorrectly be 3.
        self.assertEqual(result['nll_sum'],14)
        self.assertAlmostEqual(result['perplexity'],math.exp(3.5))
        self.assertEqual(worker.aggregate([701.],[1])['perplexity'],None)
        self.assertTrue(worker.aggregate([701.],[1])['perplexity_overflow'])

    def test_invalid_or_missing_tokens_and_nonfinite_loss_fail(self):
        for losses,counts in (([],[]),([1],[0]),([1],[True]),([1,2],[1]),([float('nan')],[1]),([float('inf')],[1]),([-1],[1])):
            with self.subTest(losses=losses,counts=counts),self.assertRaises(ValueError):worker.aggregate(losses,counts)

    def test_causal_shift_only_scores_answer_and_restores_model_mode(self):
        seen={}
        class Values(list):
            def numel(self):return len(self)
        class IDs:
            shape=(1,5)
            def __getitem__(self,key):return Values([10,11,12,13,14][key[1]])
        class Logits:
            ndim=3;shape=(1,5,30)
            def __getitem__(self,key):
                seen['logit_positions']=list(range(5))[key[1]]
                return types.SimpleNamespace(float=lambda:'float32-logits')
        class Model:
            training=True
            def eval(self):self.training=False
            def train(self,value):self.training=value
            def get_input_embeddings(self):return types.SimpleNamespace(weight=types.SimpleNamespace(device=types.SimpleNamespace(type='cuda')))
            def __call__(self,**kw):
                self.assert_inputs=kw;self.was_eval=not self.training
                return types.SimpleNamespace(logits=Logits())
        def cross_entropy(logits,targets,reduction):
            self.assertEqual(logits,'float32-logits');self.assertEqual(reduction,'sum');seen['targets']=list(targets)
            return types.SimpleNamespace(item=lambda:6.)
        functional=types.ModuleType('torch.nn.functional');functional.cross_entropy=cross_entropy
        nn=types.ModuleType('torch.nn');nn.functional=functional
        torch=types.ModuleType('torch');torch.nn=nn;torch.long='long';torch.bfloat16='bf16'
        torch.tensor=lambda *a,**kw:IDs();torch.ones_like=lambda x:'mask';torch.autocast=lambda **kw:nullcontext()
        torch.random=types.SimpleNamespace(fork_rng=lambda **kw:nullcontext());torch.inference_mode=nullcontext
        model=Model();trainer=types.SimpleNamespace(model=model,state=types.SimpleNamespace(global_step=2),args=types.SimpleNamespace(bf16=True,world_size=1))
        context=dict(max_steps=2,training_run_id='run',source_model_version_id='base',base_sha256='a'*64,sequence_length=256,spec={'dataset':{'sha256':'b'*64,'dataset_version_id':'eval'}})
        with patch.dict(sys.modules,{'torch':torch,'torch.nn':nn,'torch.nn.functional':functional}),patch.object(worker.importlib.metadata,'version',return_value='fixture'):
            result=worker.evaluate(trainer,context,[{'input_ids':[10,11,12,13,14],'prefix_tokens':3}])
            self.assertEqual(seen,{'logit_positions':[2,3],'targets':[13,14]})
            self.assertEqual(result['loss'],3.)
            self.assertTrue(model.training and model.was_eval)
            with patch.object(functional,'cross_entropy',side_effect=RuntimeError('OOM')):
                with self.assertRaisesRegex(RuntimeError,'OOM'):worker.evaluate(trainer,context,[{'input_ids':[10,11,12,13,14],'prefix_tokens':3}])
            self.assertTrue(model.training)


class EvaluationWrapperTests(unittest.TestCase):
    def exercise(self, checkpoint=False, overlap=False, train_failure=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);events=[]
            train=pin_file(root/'train',[row('TRAIN1'),row('TRAIN2')])
            ev=pin_file(root/'eval',[row('train1' if overlap else 'eval1'),row('eval2')])
            context=dict(training_path=str(root/'train'),train_dataset=train,dataset_path=str(root/'eval'),spec={'dataset':ev},sequence_length=256,tracking_enabled=False)
            path=root/'evaluation-context.json';path.write_text(json.dumps(context))
            if checkpoint:(root/'checkpoint-context.json').write_text('{}')
            class Trainer:
                processing_class=Tokenizer()
                def train(self,*a,**kw):
                    events.append('train')
                    if train_failure:raise RuntimeError('training failed')
                    return 'trained'
            original=Trainer.train
            def axolotl(*a,**kw):
                self.assertEqual(Trainer().train(),'trained');events.append('save-final')
            def checkpoint_main():
                # The existing checkpoint wrapper nests around the patched method.
                outer=Trainer.train
                def audited(trainer,*a,**kw):
                    result=outer(trainer,*a,**kw);events.append('checkpoint-audit');return result
                Trainer.train=audited
                try:axolotl()
                finally:Trainer.train=outer
            torch=types.SimpleNamespace(cuda=types.SimpleNamespace(device_count=lambda:1))
            def score(*a):events.append('eval');return {'loss':1}
            with patch.dict(sys.modules,torch=torch,transformers=types.SimpleNamespace(Trainer=Trainer),checkpoint_train=types.SimpleNamespace(main=checkpoint_main)),patch.object(worker.runpy,'run_module',side_effect=axolotl),patch.object(worker,'evaluate',side_effect=score),patch.object(worker,'log_metrics',side_effect=lambda *a:events.append('metrics')),patch.object(sys,'argv',['evaluation_train','--config','config','--context',str(path)]):
                if overlap or train_failure:
                    with self.assertRaises((ValueError,RuntimeError)):worker.main()
                    self.assertFalse((root/'evaluation-result.json').exists())
                    self.assertNotIn('eval',events)
                    if overlap:self.assertNotIn('train',events)
                else:
                    worker.main();self.assertEqual(json.loads((root/'evaluation-result.json').read_text()),{'loss':1})
                    self.assertEqual(events,['train','eval','metrics']+(['checkpoint-audit'] if checkpoint else [])+['save-final'])
            self.assertIs(Trainer.train,original)

    def test_after_training_before_save_and_checkpoint_wrapper_compatible(self):
        self.exercise();self.exercise(checkpoint=True)

    def test_overlap_fails_before_any_training_and_train_failure_never_evaluates(self):
        self.exercise(overlap=True);self.exercise(train_failure=True)


class EvaluationTrackingTests(unittest.TestCase):
    def exercise(self, later_run_id=None, logging_failure=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            train = pin_file(root/'train', [row('train1'), row('train2')])
            evaluation = pin_file(root/'eval', [row('eval1'), row('eval2')])
            context = dict(training_path=str(root/'train'), train_dataset=train,
                           dataset_path=str(root/'eval'), spec={'dataset': evaluation},
                           sequence_length=256, tracking_enabled=True)
            path = root/'evaluation-context.json'
            path.write_text(json.dumps(context))
            metrics = dict(loss=2., perplexity=math.exp(2.), scored_tokens=6,
                           record_count=2, trained_steps=1, protocol=data.PROTOCOL,
                           dataset_version_id='eval', compatibility_sha256='a'*64)
            events, batches = [], []
            class Trainer:
                processing_class = Tokenizer()
                def train(self):
                    # mlflow.start_run resumes this run, then removes the env var.
                    self_test.assertEqual(os.environ.pop('MLFLOW_RUN_ID'), 'linked-run')
                    if later_run_id is not None:
                        os.environ['MLFLOW_RUN_ID'] = later_run_id
                    events.append('train')
                    return 'trained'
            self_test = self
            original = Trainer.train
            def axolotl(*args, **kwargs):
                self.assertEqual(Trainer().train(), 'trained')
                events.append('save-final')
            class Client:
                def log_batch(self, run_id, **kwargs):
                    batches.append((run_id, kwargs))
                    if logging_failure:
                        raise RuntimeError('tracking unavailable')
                    events.append('metrics')
            tracking = types.ModuleType('mlflow.tracking')
            tracking.MlflowClient = Client
            entities = types.ModuleType('mlflow.entities')
            entities.Metric = lambda key, value, timestamp, step: types.SimpleNamespace(key=key, value=value, timestamp=timestamp, step=step)
            entities.RunTag = lambda key, value: types.SimpleNamespace(key=key, value=value)
            modules = {'torch': types.SimpleNamespace(cuda=types.SimpleNamespace(device_count=lambda: 1)),
                       'transformers': types.SimpleNamespace(Trainer=Trainer),
                       'mlflow.tracking': tracking, 'mlflow.entities': entities}
            with patch.dict(sys.modules, modules), patch.dict(os.environ, MLFLOW_RUN_ID='linked-run', WORLD_SIZE='1'), \
                    patch.object(worker.runpy, 'run_module', side_effect=axolotl), \
                    patch.object(worker, 'evaluate', return_value=metrics), \
                    patch.object(sys, 'argv', ['evaluation_train', '--config', 'config', '--context', str(path)]):
                if logging_failure:
                    with self.assertRaisesRegex(RuntimeError, 'tracking unavailable'):
                        worker.main()
                    self.assertFalse((root/'evaluation-result.json').exists())
                    self.assertEqual(events, ['train'])
                else:
                    worker.main()
                    self.assertEqual(json.loads((root/'evaluation-result.json').read_text()), metrics)
                    self.assertEqual(events, ['train', 'metrics', 'save-final'])
                self.assertEqual(os.environ.get('MLFLOW_RUN_ID'), later_run_id)
            self.assertIs(Trainer.train, original)
            self.assertEqual(len(batches), 1)
            run_id, batch = batches[0]
            self.assertEqual(run_id, 'linked-run')
            self.assertEqual({m.key: m.value for m in batch['metrics']},
                             {'heldout_loss': 2., 'heldout_perplexity': math.exp(2.),
                              'heldout_scored_tokens': 6., 'heldout_record_count': 2.})
            self.assertEqual({m.step for m in batch['metrics']}, {1})
            self.assertTrue(all(m.timestamp > 0 for m in batch['metrics']))
            self.assertEqual({t.key: t.value for t in batch['tags']},
                             {'heldout_protocol': data.PROTOCOL, 'heldout_dataset_version_id': 'eval',
                              'heldout_compatibility_sha256': 'a'*64})

    def test_training_consumes_or_replaces_env_but_metrics_keep_linked_run(self):
        for later_run_id in (None, 'unrelated-run'):
            with self.subTest(later_run_id=later_run_id):
                self.exercise(later_run_id=later_run_id)

    def test_tracking_failure_does_not_publish_successful_evaluation(self):
        self.exercise(logging_failure=True)

    def test_missing_link_fails_before_training_or_loading_gpu_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'evaluation-context.json'
            path.write_text(json.dumps({'tracking_enabled': True}))
            with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, torch=None, transformers=None), \
                    patch.object(sys, 'argv', ['evaluation_train', '--config', 'config', '--context', str(path)]):
                with self.assertRaisesRegex(RuntimeError, 'not linked before training'):
                    worker.main()
            self.assertFalse((Path(tmp)/'evaluation-result.json').exists())

    def test_disabled_tracking_does_not_require_run_or_mlflow(self):
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {'mlflow.tracking': None, 'mlflow.entities': None}):
            worker.log_metrics({'tracking_enabled': False}, {}, None)


if __name__=='__main__':unittest.main()
