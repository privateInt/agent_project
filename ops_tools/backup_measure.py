"""Size-only backup survey. UID helpers never read file bodies or stop services."""
from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from ops_tools import backup_setup as setup
from ops_candidates.backup_generation_20261001 import generation as g
from ops_candidates.cold_storage_20260930 import storage as s

require=setup.require
MAGIC='backup-size-survey-v1'
LIVE_FORMAT='[{{json .Id}},{{json .State.Running}},{{json .State.StartedAt}}]'
PHASES={'pre_helper_baseline','helper_create_guard','helper_attach_guard','post_helper_baseline',
        'file_metadata_guard','image_metadata_guard','helper_cleanup','connected_metadata','unavailable'}
REASONS={'protected_set_changed','list_command_failed','listed_ids_invalid','inspect_command_failed',
         'inspect_rows_invalid','listed_container_not_running'}
COUNT_FIELDS={'expected_protected_count','observed_protected_count','listed_running_count',
              'added_count','removed_count','started_at_changed_count','registered_helper_count'}
CONTAINER_FORMAT=s.FORMAT.replace('{{if .State.Health}}{{json .State.Health.Status}}',
                                '{{with index .State "Health"}}{{json .Status}}')
IMAGE_FORMAT='[{{json .Id}},{{json (index .Config "Volumes")}}]'
QUERY_OPERATIONS={'host_info','image_inspect','container_inspect','container_list','unknown'}
QUERY_REASONS={'TEMPLATE_FIELD_MISSING','TEMPLATE_PARSE_ERROR','CONNECTION_FAILED','ACCESS_DENIED',
               'IMAGE_NOT_FOUND','CONTAINER_NOT_FOUND','CLI_UNAVAILABLE','COMMAND_TIMEOUT',
               'RESPONSE_LIMIT','INVALID_JSON','UNKNOWN'}


def safe_query_diagnostic(value):
    fields={'schema','phase','operation','outcome','reason','exit_code','template_field',
            'values_returned','retry_requested'}
    if not isinstance(value,dict) or set(value)!=fields:return None
    choices={'phase':PHASES,'operation':QUERY_OPERATIONS,'outcome':{'failed','unknown'},
             'reason':QUERY_REASONS}
    if any(not isinstance(value[k],str) or value[k] not in options for k,options in choices.items()):return None
    rc=value['exit_code'];field=value['template_field']
    if (type(value['schema']) is not int or value['schema']!=1
        or rc is not None and (type(rc) is not int or not -255<=rc<=255)
        or field is not None and (not isinstance(field,str) or field not in ('Health','Volumes'))
        or value['values_returned'] is not False or value['retry_requested'] is not False):return None
    return deepcopy(value)


def query_failure(backend,args,code,rc=None,stderr=b''):
    operation='unknown'
    if args[:2]==['info','--format']:operation='host_info'
    elif args[:2]==['image','inspect']:operation='image_inspect'
    elif args[:2]==['container','inspect']:operation='container_inspect'
    elif args[:1]==['ps']:operation='container_list'
    raw=stderr[:32768].lower() if isinstance(stderr,bytes) else b''
    reason='UNKNOWN';field=None
    if code=='COLD_DOCKER_OUTCOME_UNKNOWN':reason='COMMAND_TIMEOUT'
    elif code=='COLD_DOCKER_RESPONSE_LIMIT':reason='RESPONSE_LIMIT'
    elif code=='COLD_DOCKER_METADATA_INVALID':reason='INVALID_JSON'
    elif code=='BACKUP_DOCKER_CLI_UNAVAILABLE':reason='CLI_UNAVAILABLE'
    elif b'map has no entry for key' in raw:
        reason='TEMPLATE_FIELD_MISSING'
        for name in ('Health','Volumes'):
            if ('key "'+name.lower()+'"').encode() in raw:field=name
    elif b'template parsing error' in raw or b'template:' in raw:reason='TEMPLATE_PARSE_ERROR'
    elif b'permission denied' in raw or b'access is denied' in raw:reason='ACCESS_DENIED'
    elif any(x in raw for x in (b'cannot connect',b'connection refused',b'error during connect')):reason='CONNECTION_FAILED'
    elif b'no such image' in raw:reason='IMAGE_NOT_FOUND'
    elif b'no such container' in raw or b'no such object' in raw:reason='CONTAINER_NOT_FOUND'
    phase=getattr(backend,'phase','unavailable')
    if not isinstance(phase,str) or phase not in PHASES:phase='unavailable'
    error=setup.Failure(code)
    error.docker_query_diagnostic=safe_query_diagnostic(dict(schema=1,phase=phase,operation=operation,
        outcome='unknown' if code=='COLD_DOCKER_OUTCOME_UNKNOWN' else 'failed',reason=reason,
        exit_code=rc if type(rc) is int and -255<=rc<=255 else None,template_field=field,
        values_returned=False,retry_requested=False))
    return error


def safe_cleanup_diagnostic(value):
    if not isinstance(value,dict) or set(value)!={'result','helpers_remaining','docker_query_diagnostic'}:return None
    count=value['helpers_remaining'];query=value['docker_query_diagnostic']
    if (value['result']!='attention_required' or type(count) is not int or not 1<=count<=160
        or query is not None and safe_query_diagnostic(query) is None):return None
    return deepcopy(value)


def safe_diagnostic(value):
    """Accept fixed phases/reasons/bounded counts only; no IDs, paths or values."""
    if not isinstance(value,dict) or set(value)!={'schema','phase','reason','counts','values_returned','retry_requested'}:
        return None
    counts=value['counts']
    if (type(value['schema']) is not int or value['schema']!=1
        or not isinstance(value['phase'],str) or value['phase'] not in PHASES
        or not isinstance(value['reason'],str) or value['reason'] not in REASONS or value['values_returned'] is not False
        or value['retry_requested'] is not False or not isinstance(counts,dict) or set(counts)!=COUNT_FIELDS
        or any(v is not None and (type(v) is not int or not 0<=v<=160) for v in counts.values())):
        return None
    return deepcopy(value)


def diagnostic_failure(backend,code,reason,observed=None,listed=None):
    expected=backend.contract['protected']
    counts=dict(expected_protected_count=len(expected),observed_protected_count=None,
                listed_running_count=listed if type(listed) is int and 0<=listed<=160 else None,
                added_count=None,removed_count=None,started_at_changed_count=None,
                registered_helper_count=len(backend.owned_helpers))
    if observed is not None:
        counts.update(observed_protected_count=len(observed),added_count=len(set(observed)-set(expected)),
                      removed_count=len(set(expected)-set(observed)),
                      started_at_changed_count=sum(observed[k]!=expected[k] for k in set(expected)&set(observed)))
    value=dict(schema=1,phase=getattr(backend,'phase','unavailable'),reason=reason,counts=counts,
               values_returned=False,retry_requested=False)
    if value['phase'] not in PHASES:value['phase']='unavailable'
    exc=setup.Failure(code);exc.measure_diagnostic=safe_diagnostic(value);return exc


def survey(config):
    """Standalone helper entry. Read directory entries/stat, never file content."""
    def insist(ok):
        if not ok:raise RuntimeError('BACKUP_SURVEY_REFUSED')
    def identity(info):
        return [info.st_dev,info.st_ino,info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode)]
    insist(set(config)=={'schema','id','role','source','identity','uid','gid','seconds','max_bytes','max_entries','quiescence_identity'})
    insist(config['schema']==1 and str(uuid.UUID(config['id']))==config['id'])
    insist(os.getuid()==config['uid'] and os.getgid()==config['gid'] and config['source']=='/source')
    insist(type(config['seconds']) is int and 1<=config['seconds']<=120)
    deadline=time.monotonic()+config['seconds'];count=0;total=0
    root=os.open('/source',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        insist(identity(os.fstat(root))==config['identity'])
        device=os.fstat(root).st_dev
        def walk(fd):
            nonlocal count,total
            insist(time.monotonic()<deadline)
            info=os.fstat(fd);count+=1
            insist(info.st_dev==device and not info.st_mode&0o7000 and count<=65536 and not os.listxattr(fd))
            for name in sorted(os.listdir(fd)):
                insist(name not in ('.','..') and '/' not in name and len(name.encode())<=255)
                info=os.stat(name,dir_fd=fd,follow_symlinks=False);count+=1
                insist(time.monotonic()<deadline and info.st_dev==device and not info.st_mode&0o7000 and count<=65536)
                if stat.S_ISDIR(info.st_mode):
                    count-=1
                    child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    try:
                        insist(identity(os.fstat(child))==identity(info));walk(child)
                    finally:os.close(child)
                else:
                    insist(stat.S_ISREG(info.st_mode) and info.st_nlink==1)
                    child=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                    try:
                        # Prove this exact UID can open the file and that the
                        # capture reader supports its metadata; never read it.
                        current=os.fstat(child)
                        insist(identity(current)==identity(info) and current.st_size==info.st_size
                               and stat.S_ISREG(current.st_mode) and current.st_nlink==1
                               and not os.listxattr(child))
                    finally:os.close(child)
                    total+=info.st_size;insist(total<=2**40)
        walk(root)
        insist(identity(os.fstat(root))==config['identity'])
        return dict(schema=1,magic='backup-size-survey-v1',id=config['id'],role=config['role'],
                    identity=config['identity'],uid=os.getuid(),gid=os.getgid(),
                    entries=count,logical_bytes=total,file_bodies_read=False,
                    archive_size_verified=False,application_consistency_verified=False)
    finally:os.close(root)


def script():
    code='import json,os,stat,sys,time,uuid\n'+inspect.getsource(survey)
    code+='\ntry:\n print(json.dumps(survey(json.loads(sys.argv[1])),sort_keys=True))\n'
    code+='except BaseException:\n print(json.dumps({"result":"attention_required","code":"BACKUP_SURVEY_FAILED"}));raise SystemExit(2)\n'
    require(len(code.encode())<=32768,'BACKUP_SURVEY_SCRIPT_LIMIT')
    return code


def recipe(cold,role,identifier,seconds):
    require(type(seconds) is int and 1<=seconds<=120,'BACKUP_SURVEY_SECONDS_INVALID')
    # bind() retains the validated plan body without review_id. Normalize the
    # reviewed input to that same body before hashing helper configuration.
    body=deepcopy(cold);digest=body.pop('review_id',None)
    require(digest is None or g.c.fingerprint(body)==digest,'BACKUP_MEASURE_COLD_REVIEW_CHANGED')
    value=s.recipe(body,role,identifier,seconds,True)
    value.pop('spec_sha256');value['name']='ops-measure-'+identifier.replace('-','')
    value['script']=script()
    return dict(value,spec_sha256=g.c.fingerprint(value))


class MetadataDocker(s.Docker):
    """Project optional map fields without changing the reviewed helper gates."""
    def metadata(self,args):
        try:rc,out,err=self.call(args)
        except g.a.Failure as exc:
            if exc.code not in ('COLD_DOCKER_OUTCOME_UNKNOWN','COLD_DOCKER_RESPONSE_LIMIT'):raise
            raise query_failure(self,args,exc.code) from None
        except OSError:raise query_failure(self,args,'BACKUP_DOCKER_CLI_UNAVAILABLE') from None
        if len(out)>1024**2 or len(err)>32768:raise query_failure(self,args,'COLD_DOCKER_RESPONSE_LIMIT')
        if rc!=0:raise query_failure(self,args,'COLD_DOCKER_METADATA_FAILED',rc,err) from None
        return out

    def query(self,args):
        if len(args)==5 and args[:3]==['container','inspect','--format'] and args[3]==s.FORMAT:
            require(s.HEX.fullmatch(args[4]),'COLD_CONTAINER_IDENTITY_INVALID')
            args=[*args[:3],CONTAINER_FORMAT,args[4]]
        elif len(args)==5 and args[:3]==['image','inspect','--format'] and args[3]==s.IMAGE_FORMAT:
            require(re.fullmatch('sha256:[0-9a-f]{64}',args[4]),'BACKUP_IMAGE_IDENTITY_INVALID')
            args=[*args[:3],IMAGE_FORMAT,args[4]]
        out=self.metadata(args)
        try:return json.loads(out)
        except (ValueError,UnicodeError):raise query_failure(self,args,'COLD_DOCKER_METADATA_INVALID',0) from None

    def running(self):
        out=self.metadata(['ps','-q','--no-trunc'])
        try:ids=out.decode().split()
        except UnicodeError:raise query_failure(self,['ps'],'COLD_DOCKER_METADATA_INVALID',0) from None
        require(len(ids)<=160 and len(ids)==len(set(ids)) and all(s.HEX.fullmatch(x) for x in ids),'COLD_RUNNING_SCOPE_LIMIT')
        result={}
        for ident in ids:
            value=self.query(['container','inspect','--format','[{{json .Id}},{{json .State.StartedAt}}]',ident])
            require(isinstance(value,list) and len(value)==2 and value[0]==ident
                    and isinstance(value[1],str) and 1<=len(value[1])<=128,'COLD_DOCKER_METADATA_INVALID')
            result[ident]=value[1]
        return result


class Docker(MetadataDocker):
    """Same constrained helper controls; all service stop/start calls refused."""
    def inspect(self,expected):
        value=super().inspect(expected)
        value['mounts']=sorted(value['mounts'],key=lambda x:json.dumps(x,sort_keys=True,separators=(',',':')))
        return value

    def mutation(self,row=None,spec=None):
        require(row is None,'BACKUP_MEASURE_SERVICE_CONTROL_REFUSED')
        require(self.contract is not None and self.scope is not None and self.scope.active
                and type(self.scope.descriptor) is int,'BACKUP_MEASURE_LIVE_SCOPE_REQUIRED')
        os.fstat(self.scope.descriptor);self.scope.checkpoint()
        require(time.monotonic()<self.until,'BACKUP_MEASURE_DEADLINE')
        if spec is not None:
            config=spec['config'];role=config['role']
            require(role in self.contract['stores'] and
                    recipe(self.contract,role,config['id'],config['seconds'])==spec,
                    'BACKUP_MEASURE_RECIPE_CHANGED')

    def stop(self,*a,**kw):raise setup.Failure('BACKUP_MEASURE_SERVICE_CONTROL_REFUSED')
    def start(self,*a,**kw):raise setup.Failure('BACKUP_MEASURE_SERVICE_CONTROL_REFUSED')

    def running(self):
        # Keep the strict running/protected contract. Use one inspect batch to
        # shorten the observation interval; Docker metadata is still sampled.
        rc,out,_=self.call(['ps','-q','--no-trunc'])
        if rc:raise diagnostic_failure(self,'BACKUP_MEASURE_RUNNING_SNAPSHOT_CHANGED','list_command_failed')
        try:ids=out.decode().split()
        except UnicodeDecodeError:
            raise diagnostic_failure(self,'BACKUP_MEASURE_RUNNING_SNAPSHOT_CHANGED','listed_ids_invalid') from None
        if len(ids)>160 or len(set(ids))!=len(ids) or any(not s.HEX.fullmatch(x) for x in ids):
            raise diagnostic_failure(self,'BACKUP_MEASURE_RUNNING_SNAPSHOT_CHANGED','listed_ids_invalid',listed=len(ids))
        if not ids:return {}
        rc,out,_=self.call(['container','inspect','--format',LIVE_FORMAT,*sorted(ids)])
        if rc:raise diagnostic_failure(self,'BACKUP_MEASURE_RUNNING_SNAPSHOT_CHANGED','inspect_command_failed',listed=len(ids))
        try:
            lines=out.decode().splitlines();rows=[json.loads(x) for x in lines]
        except (UnicodeDecodeError,json.JSONDecodeError):
            raise diagnostic_failure(self,'BACKUP_MEASURE_RUNNING_SNAPSHOT_CHANGED','inspect_rows_invalid',listed=len(ids)) from None
        result={}
        if len(rows)!=len(ids):
            raise diagnostic_failure(self,'BACKUP_MEASURE_RUNNING_SNAPSHOT_CHANGED','inspect_rows_invalid',listed=len(ids))
        for row in rows:
            if (not isinstance(row,list) or len(row)!=3 or not isinstance(row[0],str)
                or row[0] not in ids or row[0] in result or type(row[1]) is not bool
                or not isinstance(row[2],str) or not 1<=len(row[2])<=128):
                raise diagnostic_failure(self,'BACKUP_MEASURE_RUNNING_SNAPSHOT_CHANGED','inspect_rows_invalid',listed=len(ids))
            if row[1] is not True:
                raise diagnostic_failure(self,'BACKUP_MEASURE_RUNNING_SNAPSHOT_CHANGED','listed_container_not_running',listed=len(ids))
            result[row[0]]=row[2]
        return result

    def baseline(self):
        self.verify_host()
        for row in self.contract['services'].values():
            current=self.inspect(row)
            require(s.fixed(current)==s.fixed(row) and current['state']['Running'] is True
                    and current['state']['StartedAt']==row['state']['StartedAt']
                    and current['health'] in ('healthy','none'),'BACKUP_MEASURE_SERVICE_CHANGED')
        for ident,spec in self.owned_helpers.items():self.helper(ident,spec)
        excluded={r['container_id'] for r in self.contract['services'].values()}|set(self.owned_helpers)
        running=self.running()
        require(all(r['container_id'] in running and running[r['container_id']]==r['state']['StartedAt']
                    for r in self.contract['services'].values()),'BACKUP_MEASURE_SERVICE_CHANGED')
        protected={k:v for k,v in running.items() if k not in excluded}
        if protected!=self.contract['protected']:
            raise diagnostic_failure(self,'BACKUP_MEASURE_PROTECTED_CHANGED','protected_set_changed',
                                     observed=protected,listed=len(running))


class Scope:
    def __init__(self,descriptor,guard):self.descriptor,self.guard,self.active=descriptor,guard,True
    def checkpoint(self):
        require(self.active,'BACKUP_MEASURE_SCOPE_CLOSED');os.fstat(self.descriptor);self.guard()


def cold_size(backend,cold,role,seconds):
    backend.phase='pre_helper_baseline';backend.baseline()
    identifier=str(uuid.uuid4());spec=recipe(cold,role,identifier,seconds)
    ident=None;process=None;failure=None
    try:
        backend.phase='helper_create_guard';ident=backend.create(spec);backend.helper(ident,spec)
        backend.phase='helper_attach_guard';process=backend.attach(ident,spec)
        out,err=process.communicate(timeout=seconds+5)
        require(process.returncode==0 and not err and len(out)<=4096,'BACKUP_MEASURE_HELPER_FAILED')
        value=json.loads(out)
        required={'schema','magic','id','role','identity','uid','gid','entries','logical_bytes',
                  'file_bodies_read','archive_size_verified','application_consistency_verified'}
        row=cold['stores'][role]
        require(set(value)==required and value['schema']==1 and value['magic']==MAGIC
                and value['id']==identifier and value['role']==role and value['identity']==row['identity']
                and value['uid']==row['uid'] and value['gid']==row['gid']
                and type(value['entries']) is int and 1<=value['entries']<=65536
                and type(value['logical_bytes']) is int and 0<=value['logical_bytes']<=2**40
                and all(value[k] is False for k in ('file_bodies_read','archive_size_verified','application_consistency_verified')),
                'BACKUP_MEASURE_SUMMARY_INVALID')
        state=backend.helper(ident,spec)
        require(state['Running'] is False and state['Pid']==0 and state['ExitCode']==0
                and state['Status']=='exited' and not state['OOMKilled'],'BACKUP_MEASURE_EXIT_UNCONFIRMED')
        backend.remove(ident,spec);ident=None;backend.phase='post_helper_baseline';backend.baseline()
        return {k:value[k] for k in ('logical_bytes','entries')}
    except BaseException as exc:
        failure=exc;raise
    finally:
        g.a.Age.stop(process)
        if process is not None and process.stderr is not None:process.stderr.close()
        if ident is not None:
            backend.phase='helper_cleanup'
            try:backend.abort(ident,spec)
            except Exception as exc:
                if failure is None:raise
                failure.measure_cleanup_diagnostic=safe_cleanup_diagnostic(dict(result='attention_required',
                    helpers_remaining=len(backend.owned_helpers),
                    docker_query_diagnostic=safe_query_diagnostic(getattr(exc,'docker_query_diagnostic',None))))


def files_size(proposal,guard):
    """Use the same descriptor/exclusion rules as the aggregate file artifact."""
    broad=deepcopy(proposal);broad['max_file_bytes']=2**40;broad['max_entries']=65536
    with g.Selection(broad,guard) as selected:
        rows=selected.scan()
        return dict(logical_bytes=sum(row['size'] for row in rows),entries=len(rows))


def image_sizes(backend,images):
    exporter=g.ImageExporter(backend)
    return {ident:exporter.inspect(ident) for ident in images}


def limits(measurement,headroom=30):
    require(type(headroom) is int and 20<=headroom<=100,'BACKUP_EXECUTION_HEADROOM_INVALID')
    stores=measurement['stores'];files=measurement['files'];images=measurement['images']
    require(stores and images,'BACKUP_MEASUREMENT_INCOMPLETE')
    def row_valid(row):
        return (isinstance(row,dict) and set(row)=={'logical_bytes','entries'}
                and type(row['logical_bytes']) is int and 0<=row['logical_bytes']<=2**40
                and type(row['entries']) is int and 1<=row['entries']<=65536)
    require(all(row_valid(r) for r in stores.values()) and row_valid(files),'BACKUP_MEASUREMENT_INVALID')
    require(all(isinstance(v,list) and len(v)==3 and v[0]==k and g.IMAGE.fullmatch(k)
                and isinstance(v[1],str) and v[1] and type(v[2]) is int and 0<=v[2]<=2**40
                for k,v in images.items()),'BACKUP_MEASUREMENT_IMAGES_INVALID')
    grow=lambda n:(n*(100+headroom)+99)//100
    entry_count=max([files['entries'],*[x['entries'] for x in stores.values()]])
    require(grow(entry_count)+32<=65536,'BACKUP_MEASUREMENT_ENTRIES_TOO_LARGE')
    result=dict(per_storage_bytes=max(1024**2,grow(max(x['logical_bytes'] for x in stores.values()))),
                aggregate_file_bytes=max(1024**2,grow(files['logical_bytes'])),
                aggregate_image_export_bytes=max(1024**2,grow(sum(x[2] for x in images.values()))+len(images)*16*1024**2),
                max_entries=grow(entry_count)+32,headroom_percent=headroom)
    require(all(0<result[k]<=2**40 for k in ('per_storage_bytes','aggregate_file_bytes','aggregate_image_export_bytes')),
            'BACKUP_EXECUTION_BUDGET_TOO_LARGE')
    return result


def envelope(policy,store_count):
    # Files and image limits are aggregate in the pinned generation callback.
    # UID store limit is per store. Never multiply aggregate limits by roles.
    require(type(store_count) is int and 1<=store_count<=32,'BACKUP_EXECUTION_STORE_COUNT_INVALID')
    entries=policy['max_entries'];manifest=g.c.MAX_MANIFEST
    raw=(store_count*(policy['per_storage_bytes']+entries*8192+manifest+65536)
         +policy['aggregate_file_bytes']+entries*8192+manifest+65536
         +policy['aggregate_image_export_bytes']+manifest+65536)
    cipher=(raw*105+99)//100
    return dict(plaintext_upper_bytes=raw,ciphertext_upper_bytes=cipher,
                reserved_workspace_bytes=cipher*4+8*1024**2,
                generations=2,staging_copies=1,restore_copies=1,
                archive_size_verified=False,compression_savings_assumed=False)
