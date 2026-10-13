"""New UID writer/process/security gates; local actor UID, no daemon control."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from src.ops.candidates.backup_restore_uid_20261001 import writer as w, restore_uid as u
from src.ops.candidates.cold_storage_20260930 import reader
from src.ops.candidates.backup_capture_20260930 import capture as c, age_tool as a


class Writer(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.src=self.base/'source';self.src.mkdir(mode=0o700);(self.src/'value').write_bytes(b'fixture\x00\xff'*128);(self.src/'value').chmod(0o600)
        ns=1760000000123456789;os.utime(self.src/'value',ns=(ns,ns));os.utime(self.src,ns=(ns,ns))
        cfg={'schema':1,'id':str(uuid.uuid4()),'role':'storage','source':str(self.src),'identity':c.identity(self.src.stat()),
             'uid':os.getuid(),'gid':os.getgid(),'seconds':10,'max_bytes':65536,'max_entries':16,'quiescence_identity':'a'*64}
        stream=io.BytesIO();summary=io.BytesIO();result=reader.stream(cfg,stream,summary)
        self.raw=stream.getvalue().split(b'\n',1)[1]
        self.detail={'schema':1,'kind':c.KIND,'id':cfg['id'],'entry_count':result['entry_count'],
                     'logical_bytes':result['logical_bytes'],'manifest_sha256':result['manifest_sha256']}
        with tarfile.open(fileobj=io.BytesIO(self.raw)) as tar:self.manifest=tar.extractfile('manifest.json').read()
        self.root=self.base/'restore';self.root.mkdir(mode=0o700)
        self.cfg={'schema':1,'role':'storage','uid':os.getuid(),'gid':os.getgid(),'identity':w.identity(self.root),'seconds':10,'detail':self.detail}
    def extract(self,raw=None,cfg=None):return w.run(cfg or self.cfg,'extract',self.root,io.BytesIO(self.raw if raw is None else raw))
    def deny(self,fn):
        with self.assertRaises((RuntimeError,ValueError,OSError,tarfile.TarError)):fn()
    def repack(self,edit):
        out=io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(self.raw)) as src,tarfile.open(fileobj=out,mode='w',format=tarfile.PAX_FORMAT) as dst:
            for member in src:
                body=src.extractfile(member).read() if member.isfile() else None
                changed=edit(member,body)
                if changed is None:continue
                member,body=changed;dst.addfile(member,io.BytesIO(body) if body is not None else None)
        return out.getvalue()
    def test_real_reader_roundtrip_preserves_bytes_uid_modes_ns_and_exact_set(self):
        report=self.extract();self.assertTrue(report['filesystem_metadata_and_hashes_verified'])
        self.assertEqual((self.root/'value').read_bytes(),(self.src/'value').read_bytes())
        for name in ('','value'):
            x=(self.src/name).stat();y=(self.root/name).stat()
            self.assertEqual((x.st_uid,x.st_gid,x.st_mode&0o777,x.st_mtime_ns),(y.st_uid,y.st_gid,y.st_mode&0o777,y.st_mtime_ns))
        self.assertEqual(report['manifest_sha256'],hashlib.sha256(self.manifest).hexdigest())
    def test_separate_readonly_verifier_detects_changed_payload(self):
        self.extract();(self.root/'value').write_bytes(b'changed');self.deny(lambda:w.run(self.cfg,'verify',self.root,io.BytesIO(self.manifest)))
    def test_separate_readonly_verifier_detects_extra_file(self):
        self.extract();(self.root/'extra').touch();self.deny(lambda:w.run(self.cfg,'verify',self.root,io.BytesIO(self.manifest)))
    def test_readonly_verifier_detects_mode_and_timestamp(self):
        self.extract();(self.root/'value').chmod(0o400);self.deny(lambda:w.run(self.cfg,'verify',self.root,io.BytesIO(self.manifest)))
    def test_existing_destination_refused_without_overwriting(self):
        (self.root/'value').write_bytes(b'keep');self.deny(self.extract);self.assertEqual((self.root/'value').read_bytes(),b'keep')
    def test_changed_root_inode_or_owner_identity_refused(self):
        cfg=deepcopy(self.cfg);cfg['identity'][1]+=1;self.deny(lambda:self.extract(cfg=cfg));self.assertEqual(list(self.root.iterdir()),[])
    def test_different_numeric_uid_is_never_silently_mapped(self):
        cfg=deepcopy(self.cfg);cfg['uid']+=1;self.deny(lambda:self.extract(cfg=cfg));self.assertEqual(list(self.root.iterdir()),[])
    def test_untrusted_manifest_hash_refused(self):
        cfg=deepcopy(self.cfg);cfg['detail']['manifest_sha256']='b'*64;self.deny(lambda:self.extract(cfg=cfg))
    def test_wrong_generation_id_refused(self):
        cfg=deepcopy(self.cfg);cfg['detail']['id']=str(uuid.uuid4());self.deny(lambda:self.extract(cfg=cfg))
    def test_path_traversal_link_fifo_and_extra_role_refused(self):
        for typ,name in ((tarfile.REGTYPE,'../escaped'),(tarfile.SYMTYPE,'data/storage/link'),
                         (tarfile.FIFOTYPE,'data/storage/fifo'),(tarfile.REGTYPE,'data/other/value')):
            def edit(m,b):
                if m.name.endswith('/value'):m.name=name;m.type=typ;m.linkname='value' if typ==tarfile.SYMTYPE else '';m.size=len(b) if typ==tarfile.REGTYPE else 0;b=b if m.size else None
                return m,b
            self.deny(lambda:self.extract(self.repack(edit)))
            for path in self.root.iterdir():path.unlink()
        self.assertFalse((self.base/'escaped').exists())
    def test_nonzero_trailing_tar_data_refused(self):self.deny(lambda:self.extract(self.raw+b'nonzero'))
    def test_manifest_missing_is_not_accepted(self):self.deny(lambda:self.extract(self.repack(lambda m,b:None if m.name=='manifest.json' else (m,b))))
    def test_duplicate_member_is_not_accepted(self):
        out=io.BytesIO()
        with tarfile.open(fileobj=out,mode='w') as tar:
            for _ in range(2):m=tarfile.TarInfo('data/storage');m.type=tarfile.DIRTYPE;m.mode=0o700;m.uid=os.getuid();m.gid=os.getgid();tar.addfile(m)
        self.deny(lambda:self.extract(out.getvalue()))
    def test_unbounded_pax_extension_is_refused_before_payload_read(self):
        m=tarfile.TarInfo('pax');m.type=tarfile.XHDTYPE;m.size=65537
        self.deny(lambda:self.extract(m.tobuf()+b'\0'*1024))
    def test_init_requires_empty_and_touches_only_reviewed_root(self):
        if os.getuid()!=0:self.skipTest('root initializer unavailable locally')
        before=w.identity(self.src);report=w.run(self.cfg,'init',self.root)
        self.assertEqual(report['identity'],self.cfg['identity']);self.assertEqual(w.identity(self.src),before)
        (self.root/'value').touch();self.deny(lambda:w.run(self.cfg,'init',self.root))
    def test_real_stdin_process_extract_and_verify(self):
        path=Path(w.__file__);script=path.read_text()
        for mode,raw in (('extract',self.raw),('verify',self.manifest)):
            value=subprocess.run([sys.executable,'-I','-B','-c',script,mode,json.dumps(self.cfg)],input=raw,
                                 capture_output=True,timeout=12,env={'PATH':os.defpath})
            # The production script fixes /restore. For host tests replace only
            # the default root; its parser and pipe/EOF remain actual processes.
            if value.returncode!=0:
                script=script.replace("root=Path('/restore')",'root=Path('+repr(str(self.root))+')')
                value=subprocess.run([sys.executable,'-I','-B','-c',script,mode,json.dumps(self.cfg)],input=raw,capture_output=True,timeout=12)
            self.assertEqual(value.returncode,0,value.stderr.decode());self.assertTrue(json.loads(value.stdout)['filesystem_metadata_and_hashes_verified'])
    def test_real_script_alarm_bounds_a_blocked_pipe_without_parent(self):
        cfg=deepcopy(self.cfg);cfg['seconds']=1;script=Path(w.__file__).read_text().replace("root=Path('/restore')",'root=Path('+repr(str(self.root))+')')
        p=subprocess.Popen([sys.executable,'-I','-B','-c',script,'extract',json.dumps(cfg)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            p.wait(timeout=4);self.assertEqual(p.returncode,2);self.assertEqual(p.stderr.read().strip(),b'UID_MATERIALIZER_FAILED')
        finally:
            p.stdin.close();p.stdout.close();p.stderr.close()
    def test_duplicate_manifest_json_keys_refused(self):self.deny(lambda:w.document(b'{"schema":1,"schema":2}'))


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.root.chmod(0o700);self.scope=SimpleNamespace(active=True,descriptor=os.open(self.root,os.O_RDONLY),checkpoint=lambda:None)
        self.addCleanup(os.close,self.scope.descriptor);self.image='sha256:'+'a'*64
        self.spec=u.recipe(self.root,os.getuid(),os.getgid(),{'role':'storage'},self.image,'fixture-owner','SCRIPT','extract')
    def helper(self):
        backend=SimpleNamespace(until=time.monotonic()+10,verify_host=lambda:None)
        return u.UIDHelper(backend,self.scope,self.image,'fixture-owner','SCRIPT',lambda:None)
    def row(self,spec):
        return {'Id':'b'*64,'Image':spec['image'],'Config':{'Labels':{u.LABEL:spec['owner'],u.SPEC_LABEL:spec['spec_sha256'],u.ACCEPT_LABEL:spec['owner']},
            'User':f"{spec['uid']}:{spec['gid']}",'Entrypoint':['python3'],'Cmd':['-I','-B','-c',spec['script'],spec['mode'],json.dumps(u.config(spec),sort_keys=True)],
            'OpenStdin':True,'Healthcheck':{'Test':['NONE']}},'HostConfig':{'NetworkMode':'none','ReadonlyRootfs':True,'Privileged':False,
            'CapDrop':['ALL'],'CapAdd':None,'SecurityOpt':['no-new-privileges=true'],'RestartPolicy':{'Name':'no'},'LogConfig':{'Type':'none'},
            'IpcMode':'none','UsernsMode':'','PidsLimit':32,'Memory':268435456,'NanoCpus':1000000000},
            'Mounts':[{'Type':'bind','Source':spec['source'],'Destination':'/restore','RW':True}],
            'State':{'Running':False,'Pid':0,'Status':'exited','ExitCode':0,'OOMKilled':False}}
    def test_exact_recipe_one_bind_no_key_source_network_pull_or_privilege(self):
        args=u.command(self.spec);self.assertEqual(args.count('--mount'),1);self.assertIn('none',args);self.assertNotIn('--privileged',args)
        self.assertEqual(args[args.index('--pull')+1],'never');self.assertIn('bind-recursive=disabled',args[args.index('--mount')+1])
        self.assertNotIn('--cap-add',args);self.assertNotIn('load',args);self.assertNotIn('exec',args)
    def test_root_initializer_has_only_chown_capabilities_new_empty_bind(self):
        spec=u.recipe(self.root,12345,os.getgid(),{'role':'storage'},self.image,'fixture-owner','SCRIPT','init')
        args=u.command(spec);self.assertEqual(args[args.index('--user')+1],'0:0');self.assertEqual(args.count('--cap-add'),2)
        self.assertIn('CHOWN',args);self.assertIn('DAC_OVERRIDE',args);self.assertNotIn('FOWNER',args)
    def test_verify_bind_is_readonly_and_unprivileged_numeric_uid(self):
        spec=u.recipe(self.root,os.getuid(),os.getgid(),{'role':'storage'},self.image,'fixture-owner','SCRIPT','verify')
        args=u.command(spec);self.assertIn(',readonly,',args[args.index('--mount')+1]);self.assertNotIn('--cap-add',args)
    def test_changed_recipe_never_returns_create_arguments(self):
        spec=deepcopy(self.spec);spec['source']='/production'
        with self.assertRaises(a.Failure):u.command(spec)
    def test_foreign_id_cannot_be_inspected_or_removed(self):
        helper=self.helper();helper.backend.query=lambda args:self.row(self.spec)
        with self.assertRaises(a.Failure):helper.inspect('b'*64,self.spec)
    def test_changed_mount_image_uid_network_or_capabilities_refused(self):
        helper=self.helper();helper.owned['b'*64]=self.spec
        for field in ('image','mount','user','network','caps','command','labels'):
            row=self.row(self.spec)
            if field=='image':row['Image']='sha256:'+'c'*64
            if field=='mount':row['Mounts'][0]['Source']='/production'
            if field=='user':row['Config']['User']='0:0-extra'
            if field=='network':row['HostConfig']['NetworkMode']='host'
            if field=='caps':row['HostConfig']['CapAdd']=['SYS_ADMIN']
            if field=='command':row['Config']['Cmd'].append('extra')
            if field=='labels':row['Config']['Labels'][u.LABEL]='foreign'
            helper.backend.query=lambda args:row
            with self.assertRaises(a.Failure):helper.inspect('b'*64,self.spec)
    def test_live_scope_and_pause_guard_before_create(self):
        helper=self.helper();helper.scope.active=False
        with self.assertRaises(a.Failure):helper.gate()
        helper.scope.active=True;helper.guard=lambda:(_ for _ in ()).throw(a.Failure('PAUSED'))
        with self.assertRaises(a.Failure):helper.gate()
    def test_create_verification_failure_keeps_live_id_for_explicit_abort(self):
        helper=self.helper();helper.backend.query=lambda args:[self.image,None] if args[0]=='image' else self.row(self.spec)
        helper.backend.call=lambda args:(0,('b'*64).encode(),b'');helper.backend.args=lambda args:args
        with patch.object(helper,'inspect',side_effect=a.Failure('CHANGED')):
            with self.assertRaises(a.Failure):helper.start(self.spec)
        self.assertIn('b'*64,helper.owned)
        helper.inspect=lambda *args:{'Running':False,'Pid':0,'Status':'created','ExitCode':0,'OOMKilled':False}
        helper.backend.call=lambda args:(0,b'',b'');helper.abort_all();helper.empty()
    def test_oom_or_unknown_exit_never_claims_normal_removal(self):
        helper=self.helper();helper.owned['b'*64]=self.spec
        helper.inspect=lambda *args:{'Running':False,'Pid':0,'Status':'exited','ExitCode':0,'OOMKilled':True}
        helper.backend.call=lambda args:(0,b'',b'')
        with self.assertRaises(a.Failure):helper.remove('b'*64)
        self.assertIn('b'*64,helper.owned)
    def test_absence_required_before_releasing_helper_exception(self):
        helper=self.helper();helper.owned['b'*64]=self.spec;helper.inspect=lambda *args:self.row(self.spec)['State']
        helper.backend.call=lambda args:(0,('b'*64).encode() if args[0]=='ps' else b'',b'')
        with self.assertRaises(a.Failure):helper.remove('b'*64)
        self.assertIn('b'*64,helper.owned)

class HostHelper:
    """Test provider: actual writer pipes as actor UID, never a Docker claim."""
    def __init__(self,scope,image):
        self.scope=scope;self.image=image;self.owner='synthetic';self.script=Path(w.__file__).read_text();self.backend=SimpleNamespace(until=None)
        self.guard=lambda:None;self.owned={};self.calls=[]
    def invoke(self,spec,raw=None,until=None):
        self.calls.append(spec['mode']);return w.run(u.config(spec),spec['mode'],Path(spec['source']),io.BytesIO(raw or b''))
    def start(self,spec):
        self.calls.append(spec['mode']);script=spec['script'].replace("root=Path('/restore')",'root=Path('+repr(spec['source'])+')')
        child=subprocess.Popen([sys.executable,'-I','-B','-c',script,spec['mode'],json.dumps(u.config(spec))],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,pass_fds=(self.scope.descriptor,),start_new_session=True)
        ident=uuid.uuid4().hex;self.owned[ident]=child;return ident,child
    def finish(self,ident,child,until):
        raw=bytearray();pipe=a.Pipe(child.stdout.fileno(),'read',until,self.guard)
        while True:
            data=pipe.read(65536)
            if not data:break
            raw.extend(data)
        a.Age.finish(child,until);self.owned.pop(ident);return json.loads(raw)
    def remove(self,ident,abort=False):a.Age.stop(self.owned.pop(ident))
    def empty(self):assert not self.owned


class NativeBridge(unittest.TestCase):
    def setUp(self):
        from src.ops.candidates.backup_restore_20261001.tests.test_restore import RestoreTests
        self.f=RestoreTests('test_complete_generation_materializes_files_cold_store_and_image_archive');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.helper=HostHelper(self.f.scope,self.f.ident)
        base=u.Restore
        class TestRestore(base):
            def tree(inner,role,*args):
                if not role.startswith('storage-'):return super().tree(role,*args)
                # Select the new branch without claiming a host UID namespace
                # unavailable in this local executor. Writer remains actor UID.
                proxy=SimpleNamespace(**{name:getattr(os,name) for name in dir(os)});proxy.getuid=lambda:os.getuid()+1
                with patch.object(u,'os',proxy):return super().tree(role,*args)
        self.restore=TestRestore(self.f.restore_plan(),self.f.codec,self.helper,self.f.gen.p['cold']['stores'])
    def test_native_age_complete_generation_through_new_pipe_and_readonly_writer(self):
        spawn=self.f.codec.spawn;leases=[]
        def checked(args,**kwargs):
            leases.append(kwargs.get('lease',()));return spawn(args,**kwargs)
        with patch.object(self.f.codec,'spawn',side_effect=checked):result=self.restore.run()
        self.assertEqual(self.helper.backend.until,self.restore.until);self.assertTrue(leases and all(self.f.scope.descriptor in row for row in leases));self.assertTrue(result['foreign_uid_materialization']);self.assertTrue(result['crypto_eof_authenticated'])
        self.assertEqual(self.helper.calls,['init','extract','verify']);self.helper.empty()
        folder=Path(result['directory']);self.assertEqual((folder/'storage-data/data/data/record').read_bytes(),(self.f.src/'record').read_bytes())
        self.assertTrue((folder/'uid-receipt.json').exists());self.assertFalse(result['backup_ready'])
    def test_truncated_age_after_plaintext_prefix_never_publishes_new_restore(self):
        role='storage-data';path=self.f.gen.folder/self.f.trusted['artifacts'][role]['path'];path.write_bytes(path.read_bytes()[:-8])
        receipt=deepcopy(self.f.trusted);row=receipt['artifacts'][role];row['ciphertext_sha256']=a.sha(path);row['ciphertext_bytes']=path.stat().st_size
        rec=json.loads((self.f.gen.folder/'generation.json').read_text());rec['receipt_sha256']=c.fingerprint(receipt)
        rec['artifacts'][role].update(ciphertext_sha256=row['ciphertext_sha256'],ciphertext_bytes=row['ciphertext_bytes'])
        c.atomic_json(self.f.gen.folder,'receipt.json',receipt,(self.f.gen.folder/'receipt.json').read_bytes());c.atomic_json(self.f.gen.folder,'generation.json',rec,(self.f.gen.folder/'generation.json').read_bytes())
        self.restore.p=u.r.plan(self.f.gen.folder,receipt,self.f.target,self.f.identity)
        with self.assertRaises((a.Failure,RuntimeError,OSError)):self.restore.run()
        self.assertFalse((self.f.target/('restored-'+self.f.gen.p['id'])).exists());self.helper.empty()
    def test_modified_foreign_payload_before_readonly_gate_prevents_publication(self):
        invoke=self.helper.invoke
        def changed(spec,raw=None,until=None):
            if spec['mode']=='verify':(Path(spec['source'])/'record').write_bytes(b'changed')
            return invoke(spec,raw,until)
        self.helper.invoke=changed
        with self.assertRaises((a.Failure,RuntimeError)):self.restore.run()
        self.assertFalse((self.f.target/('restored-'+self.f.gen.p['id'])).exists());self.helper.empty()
