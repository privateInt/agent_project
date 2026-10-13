"""Inactive explicit leased foreign-UID materializer; no production CLI.

UIDHelper binds only a new private restore leaf, never original storage or a
key. All helper IDs are created in this invocation and checked before control.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import uuid

from ops_candidates.backup_restore_20261001 import restore as r
from ops_candidates.backup_capture_20260930 import capture as c, age_tool as a
from . import writer

require=a.require
LABEL='org.agent-project.backup-uid-restore'
SPEC_LABEL=LABEL+'.spec'
ACCEPT_LABEL='org.agent-project.ops.acceptance'


def recipe(path,uid,gid,detail,image,owner,script,mode,*,seconds=300):
    require(mode in ('init','extract','verify') and type(uid) is int and type(gid) is int and 0<=uid<2**31 and 0<=gid<2**31,
            'UID_RECIPE_INVALID')
    path=Path(path);require(path.is_absolute() and ',' not in str(path),'UID_RESTORE_PATH_INVALID')
    require(not path.is_symlink() and path.is_dir(),'UID_RESTORE_ROOT_INVALID')
    value={'source':str(path),'identity':writer.identity(path),'uid':uid,'gid':gid,'image':image,'owner':owner,
           'script':script,'mode':mode,'seconds':seconds,'detail':deepcopy(detail)}
    value['token']=uuid.uuid4().hex
    value['name']='ops-uid-restore-'+value['token']
    return dict(value,spec_sha256=c.fingerprint(value))


def config(spec):
    return {'schema':1,'role':spec['detail']['role'],'uid':spec['uid'],'gid':spec['gid'],'identity':spec['identity'],
            'seconds':spec['seconds'],'detail':{k:v for k,v in spec['detail'].items() if k!='role'}}


def command(spec):
    value=deepcopy(spec);digest=value.pop('spec_sha256')
    require(c.fingerprint(value)==digest,'UID_RECIPE_CHANGED')
    require(spec['mode']!='init' or spec['identity'][2:]==[os.getuid(),os.getgid(),0o700],'UID_INITIALIZER_OWNER_CHANGED')
    user='0:0' if spec['mode']=='init' else f"{spec['uid']}:{spec['gid']}"
    args=['container','create','--name',spec['name'],'--pull','never','--network','none','--read-only','--restart','no',
          '--log-driver','none','--cap-drop','ALL','--security-opt','no-new-privileges=true','--user',user,'--ipc','none',
          '--pids-limit','32','--memory','256m','--cpus','1','--no-healthcheck',
          '--label',LABEL+'='+spec['owner'],'--label',SPEC_LABEL+'='+digest,'--label',ACCEPT_LABEL+'='+spec['owner'],
          '--mount','type=bind,src='+spec['source']+',dst=/restore'+(',readonly' if spec['mode']=='verify' else '')+',bind-recursive=disabled',
          '--entrypoint','python3']
    if spec['mode']=='init':args+=['--cap-add','CHOWN','--cap-add','DAC_OVERRIDE']
    if spec['mode']!='init':args+=['--interactive']
    return args+[spec['image'],'-I','-B','-c',spec['script'],spec['mode'],json.dumps(config(spec),sort_keys=True)]


class UIDHelper:
    def __init__(self,backend,scope,image,owner,script,guard,audit=lambda specs:None):
        self.backend,self.scope,self.image,self.owner,self.script,self.guard,self.audit=backend,scope,image,owner,script,guard,audit
        self.owned={};self.history=[]
    def gate(self):
        self.guard();require(self.scope.active and type(self.scope.descriptor) is int,'UID_LIVE_SCOPE_REQUIRED')
        os.fstat(self.scope.descriptor);self.scope.checkpoint();self.backend.verify_host()
    def inspect(self,ident,spec):
        require(self.owned.get(ident)==spec,'UID_HELPER_NOT_THIS_INVOCATION')
        value=self.backend.query(['container','inspect','--format','{{json .}}',ident])
        cfg=value['Config'];host=value['HostConfig'];mounts=value['Mounts'];state=value['State']
        caps=['CHOWN','DAC_OVERRIDE'] if spec['mode']=='init' else None
        expected=config(spec);labels=cfg.get('Labels') or {}
        require(value['Id']==ident and value['Image']==spec['image'] and labels.get(LABEL)==spec['owner']
                and labels.get(SPEC_LABEL)==spec['spec_sha256'] and labels.get(ACCEPT_LABEL)==spec['owner']
                and cfg['User']==('0:0' if spec['mode']=='init' else f"{spec['uid']}:{spec['gid']}")
                and cfg['Entrypoint']==['python3'] and cfg['Cmd']==['-I','-B','-c',spec['script'],spec['mode'],json.dumps(expected,sort_keys=True)]
                and cfg['OpenStdin']==(spec['mode']!='init') and (cfg.get('Healthcheck') or {}).get('Test')==['NONE']
                and host['NetworkMode']=='none' and host['ReadonlyRootfs'] is True and host['Privileged'] is False
                and host['CapDrop']==['ALL'] and {x.removeprefix('CAP_') for x in host.get('CapAdd') or []}==set(caps or [])
                and host['SecurityOpt']==['no-new-privileges=true'] and host['RestartPolicy']['Name']=='no'
                and host['LogConfig']['Type']=='none' and host['IpcMode']=='none' and host.get('UsernsMode','') in ('','host')
                and host['PidsLimit']==32 and host['Memory']==268435456 and host['NanoCpus']==1000000000
                and len(mounts)==1 and mounts[0]['Type']=='bind' and mounts[0]['Source']==spec['source']
                and mounts[0]['Destination']=='/restore' and mounts[0]['RW']==(spec['mode']!='verify'), 'UID_HELPER_SECURITY_CHANGED')
        return state
    def start(self,spec):
        self.gate();require(spec['image']==self.image and spec['owner']==self.owner and spec['script']==self.script,'UID_HELPER_BOUND_RECIPE_CHANGED')
        require(writer.identity(Path(spec['source']))==spec['identity'],'UID_ROOT_CHANGED')
        image,volumes=self.backend.query(['image','inspect','--format','[{{json .Id}},{{json (index .Config "Volumes")}}]',spec['image']])
        require(image==spec['image'] and not volumes,'UID_HELPER_IMAGE_OR_VOLUME_CHANGED')
        self.history.append(deepcopy(spec));self.audit(self.history)
        rc,out,_=self.backend.call(command(spec));require(rc==0,'UID_HELPER_CREATE_UNKNOWN')
        ident=out.decode().strip();require(len(ident)==64 and all(x in '0123456789abcdef' for x in ident),'UID_HELPER_ID_INVALID')
        self.owned[ident]=deepcopy(spec);self.inspect(ident,spec);self.gate()
        child=subprocess.Popen(self.backend.args(['container','start','--attach',*(['--interactive'] if spec['mode']!='init' else []),ident]),
            stdin=subprocess.PIPE if spec['mode']!='init' else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            env={'PATH':os.defpath,'LC_ALL':'C','HOME':'/nonexistent'},pass_fds=(self.scope.descriptor,),start_new_session=True)
        return ident,child
    def remove(self,ident,abort=False):
        spec=self.owned[ident];old=self.backend.until;self.backend.until=max(old,time.monotonic()+15)
        try:
            state=self.inspect(ident,spec)
            if state['Running']:
                require(abort,'UID_HELPER_EXIT_UNCONFIRMED');rc,_,_=self.backend.call(['container','kill',ident]);require(rc==0,'UID_HELPER_KILL_UNKNOWN')
                state=self.inspect(ident,spec)
            require(state['Running'] is False and state['Pid']==0 and state['Status'] in ('created','exited')
                    and (abort or state['ExitCode']==0 and not state['OOMKilled']),'UID_HELPER_EXIT_UNCONFIRMED')
            rc,_,_=self.backend.call(['container','rm',ident]);require(rc==0,'UID_HELPER_REMOVE_UNKNOWN')
            rc,out,_=self.backend.call(['ps','-aq','--no-trunc','--filter','label='+SPEC_LABEL+'='+spec['spec_sha256']])
            require(rc==0 and ident not in out.decode().split(),'UID_HELPER_ABSENCE_UNCONFIRMED');self.owned.pop(ident)
        finally:self.backend.until=old
    def finish(self,ident,child,until):
        data=bytearray();pipe=a.Pipe(child.stdout.fileno(),'read',until,self.guard,read_limit=2**20)
        while True:
            block=pipe.read(65536)
            if not block:break
            data.extend(block)
        a.Age.finish(child,until);state=self.inspect(ident,self.owned[ident])
        require(state['ExitCode']==0 and not state['OOMKilled'] and state['Pid']==0,'UID_HELPER_FAILED')
        report=r.image.json_value(data);self.remove(ident);return report
    def invoke(self,spec,raw=None,until=None):
        ident=None;child=None;until=until or self.backend.until
        try:
            ident,child=self.start(spec)
            if child.stdin is not None:
                if raw is not None:a.Pipe(child.stdin.fileno(),'write',until,self.guard).write(raw)
                child.stdin.close()
            return self.finish(ident,child,until)
        finally:
            a.Age.stop(child)
            if ident in self.owned:self.remove(ident,True)
    def abort_all(self):
        for ident in list(self.owned): self.remove(ident,True)
    def empty(self):require(not self.owned,'UID_HELPER_NOT_REMOVED')


class Restore(r.Restore):
    def __init__(self,proposal,tool,helper,stores,**kwargs):
        super().__init__(proposal,tool,**kwargs);self.helper=helper;self.stores=deepcopy(stores);self.foreign={}
    def guard(self):
        super().guard();self.helper.backend.until=self.until;self.helper.scope.checkpoint()
    def decrypt(self,role,consume,limit):
        fd=self.input(role)
        try:
            self.process=self.tool.spawn(['-d','-i',str(self.key),'-o','-'],input_fd=fd,guard=self.guard,
                                         lease=(self.helper.scope.descriptor,))
            pipe=a.Pipe(self.process.stdout.fileno(),'read',self.until,self.guard,read_limit=limit)
            result=consume(pipe);require(not pipe.read(1),'RESTORE_CONSUMER_EOF_REQUIRED')
            self.tool.finish(self.process,self.until);self.guard()
            path=self.folder/self.p['receipt']['artifacts'][role]['path']
            require(c.signature(a.private(path,False).stat())==self.p['input_signatures'][role],'RESTORE_INPUT_CHANGED')
            self.process.stdout.close();self.process=None;return result
        finally:
            os.close(fd);self.tool.stop(self.process);self.process=None
    def tree(self,role,detail,roots,kind,ident):
        if not role.startswith('storage-'):return super().tree(role,detail,roots,kind,ident)
        name=role[8:];expected=self.stores[name]
        if expected['uid']==os.getuid() and expected['gid']==os.getgid():return super().tree(role,detail,roots,kind,ident)
        require(set(roots)=={name} and detail['entry_count']<=self.p['max_entries']-self.entries
                and detail['logical_bytes']<=self.p['max_bytes']-self.bytes,'UID_RESTORE_BUDGET')
        target=self.stage/role;target.mkdir(mode=0o700);(target/'data').mkdir(mode=0o700);leaf=target/'data'/name;leaf.mkdir(mode=0o700)
        self.helper.guard=self.guard;self.helper.backend.until=self.until
        extra=dict(detail,role=name)
        def spec(mode):return recipe(leaf,expected['uid'],expected['gid'],extra,self.helper.image,self.helper.owner,self.helper.script,mode,
                                      seconds=max(1,min(600,int(self.until-time.monotonic()))))
        init=self.helper.invoke(spec('init'),until=self.until)
        require(init.get('identity')==[*writer.identity(leaf)[:2],expected['uid'],expected['gid'],0o700],'UID_INITIALIZER_PROOF_INVALID')
        pair={};report=None
        def consume(pipe):
            nonlocal report
            spec_value=spec('extract');pair['ident'],pair['child']=self.helper.start(spec_value)
            writer_pipe=a.Pipe(pair['child'].stdin.fileno(),'write',self.until,self.guard)
            while True:
                data=pipe.read(65536)
                if not data:break
                writer_pipe.write(data)
            pair['child'].stdin.close();report=self.helper.finish(pair['ident'],pair['child'],self.until)
        try:self.decrypt(role,consume,detail['logical_bytes']+detail['entry_count']*8192+c.MAX_MANIFEST+65536)
        finally:
            a.Age.stop(pair.get('child'))
            if pair.get('ident') in self.helper.owned:self.helper.remove(pair['ident'],True)
        require(report.get('manifest_sha256')==detail['manifest_sha256'] and report.get('entries')==detail['entry_count']
                and report.get('logical_bytes')==detail['logical_bytes'] and report.get('filesystem_metadata_and_hashes_verified') is True,
                'UID_EXTRACT_REPORT_INVALID')
        raw=json.dumps(report['manifest'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        # Preserve original manifest bytes through the helper's digest; JSON
        # serialization must match the capture writer's canonical representation.
        require(hashlib.sha256(raw).hexdigest()==detail['manifest_sha256'],'UID_MANIFEST_REPORT_CHANGED')
        self.foreign[leaf]={'identity':writer.identity(leaf),'detail':extra,'manifest':raw}
        self.entries+=detail['entry_count'];self.account(detail['logical_bytes']+len(raw));self.helper.empty()
    def staged_set(self):
        expected={self.stage/'restore.json'}|set(self.materialized)|set(self.image_files)|set(self.foreign)
        for path in tuple(expected):expected.update(p for p in path.parents if p.is_relative_to(self.stage) and p!=self.stage)
        actual=set()
        for folder,dirs,files in os.walk(self.stage,followlinks=False):
            self.guard()
            for name in dirs+files:
                path=Path(folder)/name;s=path.lstat();actual.add(path)
                if path in self.foreign:
                    require(writer.identity(path)==self.foreign[path]['identity'],'UID_RESTORED_ROOT_CHANGED');dirs.remove(name)
                else:require((stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)) and s.st_uid==os.getuid()
                             and (stat.S_ISDIR(s.st_mode) or s.st_nlink==1),'RESTORE_STAGED_TYPE_OR_OWNER_CHANGED')
        require(actual==expected,'RESTORE_STAGED_SET_CHANGED')
        for leaf,row in self.foreign.items():
            spec=recipe(leaf,row['identity'][2],row['identity'][3],row['detail'],self.helper.image,self.helper.owner,self.helper.script,'verify',
                        seconds=max(1,min(600,int(self.until-time.monotonic()))))
            report=self.helper.invoke(spec,row['manifest'],self.until)
            require(report.get('manifest_sha256')==row['detail']['manifest_sha256'] and report.get('filesystem_metadata_and_hashes_verified') is True,
                    'UID_READONLY_VERIFICATION_FAILED')
        for path,(signature,digest) in self.image_files.items():
            require(c.signature(a.private(path,False).stat())==signature and a.sha(path)==digest,'RESTORE_STAGED_IMAGE_CHANGED')
        self.helper.empty()
    def run(self):
        result=super().run();self.helper.empty()
        result.update(foreign_uid_materialization=bool(self.foreign),foreign_uid_readonly_verification=bool(self.foreign))
        # The base audit/receipt made only the narrower actor-UID claim. Upgrade
        # it explicitly after all verified helpers exit, without service start.
        prior=r.read_json(Path(result['directory'])/'receipt.json')
        c.atomic_json(Path(result['directory']),'uid-receipt.json',result)
        require(prior['id']==result['id'],'UID_RESTORE_RESULT_CHANGED')
        return result
