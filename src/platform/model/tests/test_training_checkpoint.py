import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1] / 'jobs/training/axolotl'

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

store = load('checkpoint_store')
with patch.dict(sys.modules, checkpoint_store=store):
    worker = load('checkpoint_train')
adapter = load('config_adapter')


def metadata():
    result = {name: str(uuid.uuid4()) for name in ('training_run_id', 'operation_id', 'dispatch_token',
              'execution_instance_id', 'source_model_version_id')}
    result.update(execution_node='node-a', training={'max_steps': 8, 'checkpoint_steps': 2},
                  dataset={'source_type': 'synthetic'})
    return result


def source(path, step=2, max_steps=8):
    path.mkdir(parents=True, exist_ok=True)
    for name in store.REQUIRED:
        (path / name).write_bytes(b'private checkpoint data')
    (path / 'trainer_state.json').write_text(json.dumps({'global_step': step, 'max_steps': max_steps}))
    return path


class CheckpointStoreTests(unittest.TestCase):
    def test_sealed_copy_hashes_and_resume_is_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);meta = metadata();src = source(root / 'output' / 'checkpoint-2')
            digest = store.seal(root / 'saved', src, meta, 2, 'a' * 64)
            saved = root / 'saved/checkpoints/checkpoint-2'
            r = store.copy_for_resume(saved, root / 'resume', digest, 'a' * 64)
            self.assertEqual(r['step'], 2)
            (root / 'resume/optimizer.pt').write_bytes(b'changed by a new run')
            store.verify(saved, digest, 'a' * 64)
            with self.assertRaises(RuntimeError):
                store.verify(saved, digest, 'b' * 64)
            (saved / 'data/optimizer.pt').write_bytes(b'corruption')
            with self.assertRaises(RuntimeError):
                store.copy_for_resume(saved, root / 'another', digest, 'a' * 64)

    def test_incomplete_save_preserves_old_and_latest_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);meta = metadata()
            for step in (2, 4):
                store.seal(root / 'saved', source(root / str(step), step), meta, step, 'a'*64)
            with patch.object(store.shutil, 'copytree', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    store.seal(root / 'saved', source(root / '6', 6), meta, 6, 'a'*64)
            self.assertEqual(sorted(p.name for p in (root / 'saved/checkpoints').iterdir()), ['checkpoint-2','checkpoint-4'])
            store.seal(root / 'saved', root / '6', meta, 6, 'a'*64)
            self.assertEqual(sorted(p.name for p in (root / 'saved/checkpoints').iterdir()), ['checkpoint-4','checkpoint-6'])

    def test_missing_state_wrong_step_and_symlinks_rejected(self):
        for scenario in ('missing_optimizer', 'wrong_step', 'symlink_file', 'symlink_parent'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);src=source(root/'source');meta=metadata()
                if scenario=='missing_optimizer': (src/'optimizer.pt').unlink()
                if scenario=='wrong_step': (src/'trainer_state.json').write_text('{"global_step":1,"max_steps":8}')
                if scenario=='symlink_file':
                    (src/'optimizer.pt').unlink();(src/'optimizer.pt').symlink_to(src/'scheduler.pt')
                if scenario=='symlink_parent':
                    (root/'alias').symlink_to(src, target_is_directory=True);src=root/'alias'
                with self.assertRaises(RuntimeError): store.seal(root/'saved',src,meta,2,'a'*64)

    def test_receipt_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);digest=store.seal(root/'saved',source(root/'source'),metadata(),2,'a'*64)
            p=root/'saved/checkpoints/checkpoint-2'
            (p/'receipt.json').write_bytes((p/'receipt.json').read_bytes()+b' ')
            with self.assertRaises(RuntimeError): store.verify(p,digest,'a'*64)


class AuditTests(unittest.TestCase):
    def context(self):
        return dict(metadata=dict(training_run_id='child',training=dict(max_steps=4)),
                    resume=dict(source_training_run_id='parent',checkpoint_step=2,checkpoint_sha256='a'*64),
                    resume_path='/pinned')

    def test_remaining_steps_and_all_restore_components_required(self):
        for missing in (None,'model','optimizer','scheduler','rng'):
            audit=worker.CheckpointAudit(self.context())
            audit.loaded={name: True for name in audit.loaded}
            if missing: audit.loaded[missing]=False
            trainer=types.SimpleNamespace(optimizer=types.SimpleNamespace(state={'saved':1}),
                         lr_scheduler=types.SimpleNamespace(state_dict=lambda: {'last_epoch':2 if missing!='scheduler' else 0}))
            state=types.SimpleNamespace(global_step=2,max_steps=4)
            def complete():
                audit.begin(trainer,state)
                for step in (3,4): state.global_step=step;audit.step(state)
                return audit.evidence(state)
            if missing:
                with self.subTest(missing=missing),self.assertRaises(RuntimeError):complete()
            else:
                result=complete();self.assertEqual(result['executed_steps'],2);self.assertEqual(result['start_step'],2)

    def test_start_from_zero_or_skip_cannot_pass_resume(self):
        for start,step in ((0,3),(2,4)):
            audit=worker.CheckpointAudit(self.context());audit.loaded={key:True for key in audit.loaded}
            trainer=types.SimpleNamespace(optimizer=types.SimpleNamespace(state={1:1}),lr_scheduler=types.SimpleNamespace(state_dict=lambda:{'last_epoch':2}))
            state=types.SimpleNamespace(global_step=start,max_steps=4)
            with self.assertRaises(RuntimeError):
                audit.begin(trainer,state);state.global_step=step;audit.step(state);audit.evidence(state)

    def test_wrapper_invokes_audited_trainer_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);context=self.context();context['resume_path']=str(root/'resume')
            path=root/'checkpoint-context.json';path.write_text(json.dumps(context))
            class Trainer:
                def __init__(self):
                    self.callbacks=[];self.state=types.SimpleNamespace(global_step=0,max_steps=4)
                    self.optimizer=types.SimpleNamespace(state={});self.lr_scheduler=types.SimpleNamespace(state_dict=lambda:{'last_epoch':2})
                def _load_from_checkpoint(self,path):pass
                def _load_optimizer_and_scheduler(self,path):self.optimizer.state={'loaded':1}
                def _load_rng_state(self,path):pass
                def add_callback(self,c):self.callbacks.append(c)
                def remove_callback(self,c):self.callbacks.remove(c)
                def train(self,resume_from_checkpoint=None):
                    self._load_from_checkpoint(resume_from_checkpoint)
                    self._load_optimizer_and_scheduler(resume_from_checkpoint)
                    self.state.global_step=2
                    args=types.SimpleNamespace(save_only_model=False,ignore_data_skip=False)
                    for c in self.callbacks:c.on_train_begin(args,self.state,None)
                    self._load_rng_state(resume_from_checkpoint)
                    for step in (3,4):
                        self.state.global_step=step
                        for c in self.callbacks:c.on_step_end(args,self.state,None)
                    return object()
            torch=types.SimpleNamespace(cuda=types.SimpleNamespace(device_count=lambda:1))
            transformers=types.SimpleNamespace(Trainer=Trainer,TrainerCallback=object)
            original=Trainer.train
            def run_module(*args,**kw):Trainer().train(resume_from_checkpoint=context['resume_path'])
            with patch.dict(sys.modules,torch=torch,transformers=transformers),patch.object(worker.runpy,'run_module',side_effect=run_module),patch.object(sys,'argv',['checkpoint_train','--config',str(root/'config'),'--context',str(path)]):
                worker.main()
            self.assertIs(Trainer.train,original)
            evidence=json.loads((root/'checkpoint-evidence.json').read_text())
            self.assertEqual(evidence['executed_steps'],2)
            self.assertTrue(evidence['rng_loaded'])

    def test_config_pins_resume_and_disables_model_only_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);model=root/'model';model.mkdir();(model/'config.json').write_text('{}')
            output=root/'saved/output';output.mkdir(parents=True)
            spec=dict(model_dir=str(model),output_dir=str(output),dataset=dict(source_type='synthetic',format='chat_messages'),
                 training=dict(method='qlora',sequence_length=256,max_steps=4,micro_batch_size=1,gradient_accumulation_steps=1,learning_rate=.0002,lora=dict(r=8,alpha=16,dropout=0),sample_packing=False,checkpoint_steps=2))
            fake=types.SimpleNamespace(prepare_context=lambda *args:'/pinned/checkpoint')
            with patch.dict(sys.modules,checkpoint_store=fake):result=adapter.render_config(spec,root)
            config=Path(result['config_path']).read_text()
            for text in ('resume_from_checkpoint: "/pinned/checkpoint"','save_steps: 2','max_steps: 4','save_only_model: false','auto_resume_from_checkpoints: false','ignore_data_skip: false'):
                self.assertIn(text,config)
            spec['world_size']=2;spec['training']['strategy']='ddp'
            with self.assertRaises(RuntimeError):adapter.render_config(spec,root)


# Public smoke acceptance must distinguish resume from a successful fresh run.
class CheckpointSmokeAcceptanceTests(unittest.TestCase):
    def fixture(self):
        import live_training_checkpoint_smoke as smoke
        parent=dict(training_run_id='parent',operation_id='old-op',status='failed',source_model_version_id='base',
                    training=dict(max_steps=4,checkpoint_steps=2),dataset={'source_type':'synthetic'},resources={'gpu_ids':[0]},
                    result_json='{"original":true}',mlflow_run_id='old-track',output_model_version_id='')
        op=dict(operation_id='old-op',status='failed',phase='failed',attempt=1)
        selection=dict(source_training_run_id='parent',checkpoint_step=2,checkpoint_sha256='digest')
        evidence=dict(version=1,training_run_id='child',source_training_run_id='parent',checkpoint_sha256='digest',
                      start_step=2,final_step=4,executed_steps=2,model_loaded=True,optimizer_loaded=True,scheduler_loaded=True,rng_loaded=True)
        child=dict(parent,training_run_id='child',operation_id='new-op',status='succeeded',resume=selection,
                   mlflow_run_id='new-track',output_model_version_id='adapter',result={'checkpoint':evidence})
        child_op=dict(operation_id='new-op',status='succeeded')
        model=dict(model_version_id='adapter',training_run_id='child',source_model_version_id='base',state='ready',artifact_kind='adapter')
        record=dict(original_execution=smoke.execution_snapshot(parent,op),selection=selection)
        return smoke,record,parent,op,child,child_op,model

    def test_completed_resume_accepted(self):
        smoke,*args=self.fixture();smoke.assert_resumed(*args)

    def test_fresh_training_and_missing_restoration_rejected(self):
        for change in ({'start_step':0,'executed_steps':4},{'rng_loaded':False},{'optimizer_loaded':False},{'checkpoint_sha256':'other'}):
            smoke,record,parent,op,child,child_op,model=self.fixture();child['result']['checkpoint'].update(change)
            with self.subTest(change=change),self.assertRaises(RuntimeError):smoke.assert_resumed(record,parent,op,child,child_op,model)

    def test_parent_rewrite_or_schedule_override_rejected(self):
        for which in ('parent_status','schedule','tracking','model'):
            smoke,record,parent,op,child,child_op,model=self.fixture();child=copy.deepcopy(child)
            if which=='parent_status':parent['status']='succeeded'
            if which=='schedule':child['training']['max_steps']=8
            if which=='tracking':child['mlflow_run_id']='old-track'
            if which=='model':model['training_run_id']='parent'
            with self.subTest(which=which),self.assertRaises(RuntimeError):smoke.assert_resumed(record,parent,op,child,child_op,model)

    def test_prepare_does_not_create_job_while_dispatcher_running(self):
        import live_training_checkpoint_smoke as smoke
        with patch.object(smoke,'dispatcher_stopped',side_effect=RuntimeError('running')):
            with self.assertRaises(RuntimeError):smoke.prepare(None,{},lambda _:None)


class CheckpointContextTests(unittest.TestCase):
    def test_signature_is_path_independent_and_rejects_engine_or_data_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);meta=metadata();model=root/'model';model.mkdir();(model/'config.json').write_text('{}')
            data=root/'data.jsonl';data.write_text('{"messages":[]}')
            first=root/'first';first.mkdir();saved=root/'saved';(saved/'output').mkdir(parents=True)
            spec=dict(checkpoint_metadata=meta,output_dir=str(saved/'output'))
            with patch('importlib.metadata.version',return_value='fixture-version'):
                store.prepare_context(spec,first,data,model)
            context=json.loads((first/'checkpoint-context.json').read_text())
            digest=store.seal(saved,source(root/'source'),meta,2,context['signature'])
            child_meta=dict(meta,training_run_id=str(uuid.uuid4()))
            second=root/'second';second.mkdir();(root/'child/output').mkdir(parents=True)
            resumed=dict(checkpoint_metadata=child_meta,output_dir=str(root/'child/output'),
                 resume=dict(source_training_run_id=meta['training_run_id'],checkpoint_step=2,checkpoint_sha256=digest),
                 resume_directory=str(saved/'checkpoints/checkpoint-2'))
            with patch('importlib.metadata.version',return_value='fixture-version'):
                path=store.prepare_context(resumed,second,data,model)
            self.assertEqual(Path(path),second/'resume-source')
            for kind in ('engine','data'):
                work=root/kind;work.mkdir()
                if kind=='data':data.write_text('different records')
                with patch('importlib.metadata.version',return_value='changed' if kind=='engine' else 'fixture-version'):
                    with self.assertRaises(RuntimeError):store.prepare_context(resumed,work,data,model)


if __name__ == '__main__':unittest.main()
