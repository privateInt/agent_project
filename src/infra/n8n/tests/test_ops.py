import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ops'))
import configure as c
import lifecycle as l
from common import redact


class Configure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base/'src/infra/n8n'; self.root.mkdir(parents=True)
        self.legacy = self.base/'src/platform/agent/n8n'; self.legacy.mkdir(parents=True)
    def tearDown(self): self.tmp.cleanup()
    def legacy_config(self):
        (self.legacy/'.env').write_text('N8N_PORT=16200\nN8N_BIND_HOST=0.0.0.0\nN8N_AGENT_NETWORK=ops-infra\nN8N_PUBLIC_URL=http://simsys4090:16200\nN8N_SECURE_COOKIE=false\n')
        (self.legacy/'.secrets').mkdir(mode=0o700)
        for i,n in enumerate(c.SECRET_NAMES): (self.legacy/'.secrets'/n).write_bytes((str(i)*64+'\n').encode())
    def test_import_preserves_settings_and_every_key_byte(self):
        self.legacy_config()
        original = (self.legacy/'.env').read_bytes()
        state = c.configure(self.root,legacy=self.legacy)
        self.assertEqual(state['origin'],'a07'); self.assertEqual(state['volumes'],c.OLD_VOLUMES)
        self.assertTrue((self.root/'.env').read_bytes().startswith(original))
        for n in c.SECRET_NAMES: self.assertEqual((self.root/'.secrets'/n).read_bytes(),(self.legacy/'.secrets'/n).read_bytes())
        self.assertEqual(c.configure(self.root,port=17000,legacy=self.legacy),state)
        self.assertEqual((self.legacy/'.env').read_bytes(),original)
    def test_fresh_config_independent_keys_and_idempotent(self):
        state = c.configure(self.root,legacy=self.legacy)
        self.assertEqual(state['origin'],'new')
        before = {n:(self.root/'.secrets'/n).read_bytes() for n in c.SECRET_NAMES}
        self.assertEqual(len(set(before.values())),3)
        c.configure(self.root,legacy=self.legacy)
        self.assertEqual(before,{n:(self.root/'.secrets'/n).read_bytes() for n in c.SECRET_NAMES})
    def test_partial_legacy_creates_no_new_keys(self):
        (self.legacy/'.env').write_text('partial')
        with self.assertRaises(ValueError): c.configure(self.root,legacy=self.legacy)
        self.assertEqual(list(self.root.iterdir()),[])
    def test_changed_volume_rejected(self):
        c.configure(self.root,legacy=self.legacy)
        p=self.root/'.env';p.write_text(p.read_text().replace('ops-n8n-db','different-db'))
        with self.assertRaises(ValueError): c.configure(self.root,legacy=self.legacy)
    def test_symlink_secret_rejected(self):
        self.legacy_config();p=self.legacy/'.secrets/db-app';p.unlink();p.symlink_to(self.legacy/'.secrets/db-admin')
        with self.assertRaises(ValueError): c.configure(self.root,legacy=self.legacy)
        self.assertFalse((self.root/'.env').exists())
    def test_redaction_removes_values_and_bearer(self):
        self.legacy_config()
        value=(self.legacy/'.secrets/db-app').read_text().strip()
        text=redact(value+' Bearer abcd-test postgres://user:secret@host password=hidden',[self.legacy])
        for secret in (value,'abcd-test','secret@','hidden'): self.assertNotIn(secret,text)


class Handoff(unittest.TestCase):
    def state(self): return {'origin':'a07','config_id':'id','volumes':c.OLD_VOLUMES}
    def test_missing_old_volume_must_not_create_replacement(self):
        with patch.object(l,'volume_inventory',return_value=set()),patch.object(l,'run') as run:
            with self.assertRaisesRegex(ValueError,'Missing original'): l.storage_plan(self.state(),Path('/old'))
            run.assert_not_called()
    def test_foreign_volume_owner_rejected(self):
        result=type('Result',(),{'stdout':json.dumps([{'Labels':{'owner':'other'}}])})()
        with patch.object(l,'volume_inventory',return_value=set(c.OLD_VOLUMES.values())),patch.object(l,'run',return_value=result):
            with self.assertRaisesRegex(ValueError,'ownership mismatch'): l.storage_plan(self.state(),Path('/old'))
    def test_unrelated_volume_consumer_rejected(self):
        with self.assertRaisesRegex(ValueError,'unrelated container'):
            l.validate_container({'Config':{'Labels':{'com.docker.compose.project':'knowledge-platform'}}},self.state(),Path('/old'))
    def test_legacy_container_keys_must_match_imported_keys(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'src/infra/n8n';legacy=Path(td)/'src/platform/agent/n8n'
            for folder in (root,legacy):(folder/'.secrets').mkdir(parents=True)
            for folder in (root,legacy):
                for n in ('db-app','encryption-key'):(folder/'.secrets'/n).write_text('identical-key')
            container={'Config':{'Labels':{'com.docker.compose.project':'agent-flows','com.docker.compose.service':'n8n','com.docker.compose.project.working_dir':str(legacy)}},'Mounts':[
                {'Type':'volume','Name':c.OLD_VOLUMES['flows-data'],'Destination':'/home/node/.n8n'},
                {'Type':'bind','Source':str(legacy/'.secrets/db-app'),'Destination':'/run/secrets/flows-db-app'},
                {'Type':'bind','Source':str(legacy/'.secrets/encryption-key'),'Destination':'/run/secrets/flows-encryption'}]}
            with patch.object(l,'ROOT',root):
                self.assertEqual(l.validate_container(container,self.state(),legacy),('agent-flows','n8n'))
                (root/'.secrets/encryption-key').write_text('rotated-key')
                with self.assertRaisesRegex(ValueError,'credentials differ'):l.validate_container(container,self.state(),legacy)
    def test_start_stops_legacy_before_up_and_never_deletes(self):
        state=self.state();events=[]
        old=[{'Id':'old-n8n','Name':'old-n8n','HostConfig':{'RestartPolicy':{'Name':'unless-stopped'}},'State':{'Running':True}}]
        own={'networks':{'flows-api':{'name':'ops-infra'}}}
        class Result:
            stdout='';returncode=0
        def run(args,**kw):
            events.append(args)
            r=Result()
            if args[:3]==['docker','network','ls']:r.stdout='ops-infra\n'
            if args[:2]==['docker','inspect']:r.stdout=json.dumps([{'State':{'Running':False},'HostConfig':{'RestartPolicy':{'Name':'no'}}}])
            return r
        def compose(args,**kw):events.append(['compose',*args]);return Result()
        with tempfile.TemporaryDirectory() as td,patch.object(l,'ROOT',Path(td)/'src/infra/n8n'),patch.object(l,'volume_inventory',return_value=set()),patch.object(l,'configure',return_value=state),patch.object(l,'config',return_value=own),patch.object(l,'verify'),patch.object(l,'storage_plan',return_value=([],old)),patch.object(l,'save_handoff'),patch.object(l,'containers',return_value=[]),patch.object(l,'run',side_effect=run),patch.object(l,'compose',side_effect=compose):
            l.ROOT.mkdir(parents=True)
            l.start(n8n_only=True)
        stop=next(i for i,e in enumerate(events) if e[:2]==['docker','stop'])
        update=next(i for i,e in enumerate(events) if e[:3]==['docker','update','--restart=no'])
        up=next(i for i,e in enumerate(events) if e[:2]==['compose','up'])
        self.assertLess(update,stop);self.assertLess(stop,up)
        repair=next(i for i,e in enumerate(events) if '/opt/n8n/repair-privileges.sql' in e)
        check=next(i for i,e in enumerate(events) if '/opt/n8n/check-db.sh' in e)
        self.assertLess(up,repair);self.assertLess(repair,check)
        self.assertLess(check,len(events)-1)
        self.assertFalse(any('rm' in e or 'down' in e or '-v' in e for e in events))
    def test_previously_provisioned_fresh_volume_missing_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as td,patch.object(l,'ROOT',Path(td)),patch.object(l,'volume_inventory',return_value=set()):
            state={'origin':'new','config_id':'id','volumes':c.NEW_VOLUMES}
            (Path(td)/'.storage.json').write_text(json.dumps({'config_id':'id','volumes':c.NEW_VOLUMES}))
            with self.assertRaisesRegex(ValueError,'Missing original'):l.storage_plan(state,Path('/old'))
    def test_fresh_keys_cannot_adopt_or_ignore_legacy_storage(self):
        with patch.object(l,'volume_inventory',return_value=set(c.OLD_VOLUMES.values())):
            with self.assertRaisesRegex(ValueError,'A07 storage still exists'):
                l.storage_plan({'origin':'new','config_id':'id','volumes':c.NEW_VOLUMES},Path('/old'))
    def test_unhealthy_existing_n8n_is_stopped_before_up_recovers_it(self):
        with patch.object(l,'containers',return_value=[{'State':{'Running':True,'Health':{'Status':'unhealthy'}}}]),patch.object(l,'compose') as compose:
            l.recover_unhealthy_n8n()
            self.assertEqual(compose.call_args.args[0],['stop','--timeout','30','n8n'])
    def test_healthy_or_absent_n8n_is_not_stopped(self):
        for current in ([],[{'State':{'Running':True,'Health':{'Status':'healthy'}}}],[{'State':{'Running':False,'Health':{'Status':'unhealthy'}}}]):
            with patch.object(l,'containers',return_value=current),patch.object(l,'compose') as compose:
                l.recover_unhealthy_n8n();compose.assert_not_called()
    def test_pull_failure_does_not_stop_running_containers(self):
        own={'networks':{'flows-api':{'name':'ops-infra'}}}
        with patch.object(l,'volume_inventory',return_value=set()),patch.object(l,'configure',return_value=self.state()),patch.object(l,'config',return_value=own),patch.object(l,'verify'),patch.object(l,'storage_plan',return_value=([],[])),patch.object(l,'run') as run,patch.object(l,'compose',side_effect=ValueError('pull failed')):
            with self.assertRaisesRegex(ValueError,'pull failed'):l.start(n8n_only=True)
            run.assert_not_called()

if __name__=='__main__': unittest.main()
