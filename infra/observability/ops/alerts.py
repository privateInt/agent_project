#!/usr/bin/env python3
"""O04 guarded deployment: test first, add Alertmanager, replace only Prometheus once."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import manage as op
import restart_check as retained
from alert_probe import lifecycle

sys.dont_write_bytecode=True
sys.path.insert(0,str(op.ROOT.parents[1]))
from ops_platform.alerts import Alerts, RULES, SCOPE

TAG='quay.io/prometheus/alertmanager:v0.32.2'
PROJECT='infrastructure-observability-alerts'
SERVICE='obs-alertmanager'
GPU_QUERY='{__name__=~"DCGM_FI_DEV_(GPU_UTIL|FB_TOTAL|FB_USED|FB_FREE|GPU_TEMP|POWER_USAGE)",job="gpu"}[3m]'
FILES=('docker-compose.yml','prometheus/docker-compose.yml','gpu/prometheus.yml','gpu/prometheus-overlay.yml',
       'alerts/prometheus.yml','alerts/o04.rules.yml','alerts/alertmanager.yml','alerts/docker-compose.yml',
       'alerts/prometheus-overlay.yml','alerts/rules_test.yml','ops/alerts.py','ops/alert_probe.py','ops/manage.py')


def stamp(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')


def environment(state, host):
    values={'OBS_ALERTS_IMAGE':state['image']['digest'],'OBS_CONFIG_ID':state['config_id'],
            'OBS_UID':host['uid'],'OBS_GID':host['gid'],'OBS_ALERTS_DATA':state['data_dir']}
    op.require(all(not any(c in str(v) for c in "'\n\r\0") for v in values.values()),'INVALID_PATH')
    return ''.join(k+"='"+str(v)+"'\n" for k,v in values.items())


def manager_containers(docker, state):
    ids=docker.run(['ps','-aq','--filter','label=com.docker.compose.project='+PROJECT]).split()
    items=docker.json(['inspect',*ids]) if ids else []
    op.require(len(items)<=1, 'ALERTMANAGER_DUPLICATE_CONTAINER')
    for item in items:
        labels=item['Config'].get('Labels') or {}
        op.require(labels.get(op.OWNER)==state['config_id']
                   and labels.get('com.docker.compose.service')==SERVICE,'ALERTMANAGER_OWNER_MISMATCH')
        op.require(item['Image']==state['image']['image_id'] and item['Config']['Image']==state['image']['digest'],
                   'ALERTMANAGER_IMAGE_MISMATCH')
    return items


def manager_compose(runtime, args, **kwargs):
    root=runtime.root/'alerts'
    return runtime.docker.run(['compose','--project-name',PROJECT,'--env-file',str(root/'.env'),
                               '-f',str(root/'docker-compose.yml'),*args],**kwargs)


def config_guard(runtime, state):
    root=runtime.root
    op.require((root/'alerts/.env').read_text()==environment(state,runtime.state),'ALERT_ENV_CHANGED')
    config=json.loads(manager_compose(runtime,['config','--format','json']))
    op.require(set(config['services'])=={SERVICE},'ALERT_COMPOSE_SCOPE_CHANGED')
    service=config['services'][SERVICE]
    op.require(service['image']==state['image']['digest'] and service.get('network_mode')=='host'
               and service.get('user')==str(runtime.state['uid'])+':'+str(runtime.state['gid'])
               and service.get('labels',{}).get(op.OWNER)==state['config_id']
               and service.get('read_only') is True and not service.get('ports'), 'ALERT_COMPOSE_BOUNDARY_CHANGED')
    expected=['--config.file=/etc/alertmanager/alertmanager.yml','--web.listen-address=127.0.0.1:16330',
              '--cluster.listen-address=','--storage.path=/alertmanager','--data.retention=120h']
    op.require(service['command']==expected and service.get('tmpfs')==['/tmp:size=16777216,mode=1777'],
               'ALERT_COMPOSE_COMMAND_CHANGED')
    mounts=service['volumes']
    op.require(len(mounts)==2 and {(m['source'],m['target'],bool(m.get('read_only'))) for m in mounts}=={
        (str(root/'alerts/alertmanager.yml'),'/etc/alertmanager/alertmanager.yml',True),
        (state['data_dir'],'/alertmanager',False)},'ALERT_STORAGE_CHANGED')
    am=json.loads((root/'alerts/alertmanager.yml').read_text())
    op.require(am=={'global':{'resolve_timeout':'5m'},'route':{'receiver':'ops-local-only',
        'group_by':['alertname','job','instance'],'group_wait':'30s','group_interval':'5m','repeat_interval':'4h'},
        'receivers':[{'name':'ops-local-only'}]},'EXTERNAL_DELIVERY_OR_CONFIG_CHANGE_REFUSED')


def binding(runtime, state):
    data=op.no_symlinks(Path(runtime.state['data_dir'])); info=data.stat()
    return {'config_id':runtime.state['config_id'],'prometheus_image':runtime.state['images']['obs-prometheus'],
            'alertmanager_image':state['image'],'data_dir':str(data),'device':info.st_dev,'inode':info.st_ino,
            'files':{f:retained.file_hash(runtime.root/f) for f in (*FILES,'.env','.state.json','gpu/.state.json','alerts/.env')},
            'data_owner':retained.file_hash(data.parent/'.owner.json')}


def prepare(runtime):
    root=runtime.root; path=root/'alerts/.state.json'
    op.require(op.gpu_enabled(root),'O04_REQUIRES_VERIFIED_GPU_CONFIGURATION')
    if path.exists() or path.is_symlink():
        state=op.read_private(path)
        op.require(state.get('schema')==1 and state.get('config_id')==runtime.state['config_id']
                   and state.get('image',{}).get('tag')==TAG,'ALERT_STATE_MISMATCH')
    else:
        # Refuse an unowned stack/occupied listener before pulling or creating state.
        op.require(not runtime.docker.run(['ps','-aq','--filter','label=com.docker.compose.project='+PROJECT]).strip(),
                   'UNOWNED_ALERTMANAGER_EXISTS')
        with socket.socket() as sock: sock.bind(('127.0.0.1',16330))
        op.check_disks(runtime.state['data_dir'],runtime.host['docker_root'])
        runtime.docker.run(['pull',TAG],visible=True,timeout=900)
        image=runtime.docker.json(['image','inspect',TAG])[0]
        digests=[d for d in image.get('RepoDigests',[]) if d.startswith(TAG.rsplit(':',1)[0]+'@sha256:')]
        op.require(len(digests)==1 and re.fullmatch('sha256:[0-9a-f]{64}',image['Id']),'ALERT_IMAGE_PIN_UNAVAILABLE')
        data=op.no_symlinks(Path(runtime.state['data_dir']).parent/'alertmanager')
        op.require(not data.exists(),'ALERT_DATA_ALREADY_EXISTS')
        data.mkdir(mode=0o700)
        op.write_private(data/'.owner.json',{'config_id':runtime.state['config_id']})
        state={'schema':1,'config_id':runtime.state['config_id'],'enabled':False,'data_dir':str(data),
               'image':{'tag':TAG,'digest':digests[0],'image_id':image['Id']}}
        retained.write_record(path,state)
        op.write_private(root/'alerts/.env',environment(state,runtime.state))
    op.require(re.fullmatch(re.escape(TAG.rsplit(':',1)[0])+r'@sha256:[0-9a-f]{64}',state['image']['digest']),
               'ALERT_IMAGE_PIN_INVALID')
    data=op.no_symlinks(Path(state['data_dir']))
    op.require(data==Path(runtime.state['data_dir']).parent/'alertmanager' and data.is_dir()
               and data.stat().st_uid==os.getuid() and not data.stat().st_mode&0o077
               and op.read_private(data/'.owner.json')=={'config_id':runtime.state['config_id']},'ALERT_DATA_OWNER_MISMATCH')
    manager_containers(runtime.docker,state); config_guard(runtime,state)
    return state


def native_tests(runtime, state):
    root=runtime.root
    image=runtime.state['images']['obs-prometheus']['digest']
    common=['run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--user',str(os.getuid())+':'+str(os.getgid()),'--memory','256m','--memory-swap','256m',
            '--cpus','1','--pids-limit','64','--tmpfs','/tmp:size=16777216,mode=1777']
    mount=['--mount','type=bind,src='+str(root/'alerts')+',dst=/etc/prometheus/rules,readonly',
           '--mount','type=bind,src='+str(root/'alerts/prometheus.yml')+',dst=/etc/prometheus/prometheus.yml,readonly']
    print('O04 preflight: promtool config/rules, amtool config, isolated Alertmanager lifecycle.',flush=True)
    runtime.docker.run(common+mount+['--entrypoint','/bin/promtool',image,'check','config','/etc/prometheus/prometheus.yml'],visible=True)
    runtime.docker.run(common+mount+['--workdir','/etc/prometheus/rules','--entrypoint','/bin/promtool',image,
                                   'test','rules','rules_test.yml'],visible=True)
    runtime.docker.run(common+['--mount','type=bind,src='+str(root/'alerts/alertmanager.yml')+',dst=/config.yml,readonly',
                              '--entrypoint','/bin/amtool',state['image']['digest'],'check-config','/config.yml'],visible=True)
    probe=lifecycle(runtime.docker,state['image']['digest'])
    return {'promtool_config':'passed','promtool_rules':'passed','amtool_config':'passed','alertmanager_lifecycle':probe}


def protected(runtime):
    ids=runtime.docker.run(['ps','-q']).split()
    result={}
    for item in runtime.docker.json(['inspect',*ids]) if ids else []:
        labels=item['Config'].get('Labels') or {}
        if (labels.get('com.docker.compose.project')==op.PROJECT and labels.get('com.docker.compose.service')=='obs-prometheus') or labels.get('com.docker.compose.project')==PROJECT:
            continue
        if item['HostConfig'].get('RestartPolicy',{}).get('Name') in ('always','unless-stopped'):
            result[item['Id']]=item['State']['StartedAt']
    return result


def read_record(path):
    record=op.read_private(path)
    op.require(record.get('schema')==1 and record.get('kind')=='O04-deploy'
               and record.get('baseline_sha256')==retained.digest(record['baseline']),'O04_RECORD_INVALID')
    return record


def assert_bound(runtime,state,record):
    op.require(record['baseline']['binding']==binding(runtime,state),'O04_CONFIG_CHANGED_KEEP_BASELINE')


def attach(runtime,state,record,path):
    before=record['baseline']['containers']; current=runtime.containers()
    if record.get('prometheus_intent'):
        return  # Reconcile with read-only verification; never issue an automatic second replacement.
    op.require(current==before,'O04_CONTAINERS_CHANGED_BEFORE_ATTACH')
    prior=state['enabled']
    state['enabled']=True; retained.write_record(runtime.root/'alerts/.state.json',state)
    try:
        op.config_check(runtime.docker,runtime.root,runtime.state)
    except BaseException:
        # No runtime command has been requested yet. Restore the selection flag.
        state['enabled']=prior;retained.write_record(runtime.root/'alerts/.state.json',state)
        raise
    record['prometheus_intent']={'at':stamp(),'old_id':before['obs-prometheus']['id']}
    retained.write_record(path,record)
    op.compose(runtime.docker,runtime.root,['up','-d','--no-deps','--no-build','--pull','never',
               '--force-recreate','--wait','--wait-timeout','180','obs-prometheus'],visible=True,timeout=240)
    record['prometheus_command_completed']=True; retained.write_record(path,record)


def verify(runtime,state,record):
    assert_bound(runtime,state,record)
    op.require(state.get('enabled') is True and record.get('prometheus_intent'),'O04_ATTACH_NOT_REQUESTED')
    current=runtime.containers(); before=record['baseline']['containers'];retained.healthy(current)
    op.require(current['obs-prometheus']['id']!=before['obs-prometheus']['id'],'O04_PROMETHEUS_NOT_REPLACED')
    for name in ('obs-node-exporter','obs-cadvisor'):
        op.require(current[name]==before[name],'O04_OTHER_COLLECTOR_CHANGED')
    now=protected(runtime)
    op.require(all(now.get(k)==v for k,v in record['baseline']['protected'].items()),'O04_PROTECTED_CONTAINER_CHANGED')
    info=op.project_containers(runtime.docker,runtime.state['config_id'])
    prom=next(i for i in info if i['Config']['Labels']['com.docker.compose.service']=='obs-prometheus')
    actual={(m['Source'],m['Destination'],m['RW']) for m in prom['Mounts'] if m['Destination'].startswith('/etc/prometheus/')}
    op.require(actual=={(str(runtime.root/'alerts/prometheus.yml'),'/etc/prometheus/prometheus.yml',False),
                        (str(runtime.root/'alerts/o04.rules.yml'),'/etc/prometheus/rules/o04.rules.yml',False)},'O04_PROMETHEUS_MOUNT_MISMATCH')
    items=manager_containers(runtime.docker,state)
    op.require(len(items)==1 and items[0]['State']['Running'] and items[0]['State'].get('Health',{}).get('Status')=='healthy',
               'O04_ALERTMANAGER_NOT_HEALTHY')
    am=items[0]
    op.require(am['HostConfig'].get('ReadonlyRootfs') is True and am['HostConfig'].get('NetworkMode')=='host'
               and '--web.listen-address=127.0.0.1:16330' in am['Config']['Cmd'], 'O04_ALERTMANAGER_BOUNDARY_MISMATCH')
    mounts={(m['Source'],m['Destination'],m['RW']) for m in am['Mounts'] if m['Type']=='bind'}
    op.require(mounts=={(str(runtime.root/'alerts/alertmanager.yml'),'/etc/alertmanager/alertmanager.yml',False),
                       (state['data_dir'],'/alertmanager',True)},'O04_ALERTMANAGER_STORAGE_MISMATCH')
    end=record['baseline']['query_time']
    history=retained.history(runtime.prom,end)
    history[GPU_QUERY]=retained.matrix(runtime.prom,GPU_QUERY,end)
    op.require(history==record['baseline']['history'],'O04_FIXED_HISTORY_CHANGED')
    samples=runtime.prom.query('up{job=~"prometheus|host|containers|gpu|alertmanager"}')
    op.require(len(samples)==5 and {s['metric']['job'] for s in samples}=={'prometheus','host','containers','gpu','alertmanager'}
               and all(float(s['value'][1])==1 for s in samples),'O04_TARGET_NOT_UP')
    age=runtime.prom.query('time() - timestamp(up{job=~"prometheus|host|containers|gpu|alertmanager"})')
    op.require(len(age)==5 and all(0<=float(s['value'][1])<90 for s in age),'O04_TARGET_STALE')
    rules=Alerts('prometheus','http://127.0.0.1:16310',{}).rules()
    active=Alerts('alertmanager','http://127.0.0.1:16330',{}).active()
    op.require(rules['status']=='ok' and active['status']=='ok','O04_RULE_OR_ALERT_READBACK_FAILED')
    managers=runtime.prom.get('/api/v1/alertmanagers')
    op.require([a.get('url') for a in managers.get('activeAlertmanagers',[])]==['http://127.0.0.1:16330/api/v2/alerts'],
               'O04_DELIVERY_TARGET_MISMATCH')
    return {'schema':1,'stage':'O04-alert-engine-acceptance','result':'passed','checked_at':stamp(),
            'rule_count':len(RULES),'active_rule_alerts':rules['data']['active_count'],
            'alertmanager_active_count':active['data']['count'],'external_delivery':'disabled_empty_receiver',
            'prometheus_same_image_and_tsdb':True,'historical_samples_exactly_preserved':retained.counts(history),
            'protected_containers_unchanged':len(record['baseline']['protected']),
            'target_jobs':5,'tests':record['tests'],'prometheus_command_completion_recorded':bool(record.get('prometheus_command_completed')),
            'business_api_requests':0,'inference_requested':False,'browser_acceptance':'deferred_by_user',
            'ops_api_acceptance':'separate_check_alerts.py','production_fault_injection':False}


def deploy(runtime):
    state=prepare(runtime); path=Path(runtime.state['data_dir']).parent/'alerts-deployment-o04.json'
    if path.exists() or path.is_symlink():
        record=read_record(path);assert_bound(runtime,state,record)
    else:
        op.require(not state['enabled'],'O04_BASELINE_MISSING')
        prom=next(i for i in op.project_containers(runtime.docker,runtime.state['config_id'])
                  if i['Config']['Labels']['com.docker.compose.service']=='obs-prometheus')
        config_mount=[m for m in prom['Mounts'] if m['Destination']=='/etc/prometheus/prometheus.yml']
        op.require(len(config_mount)==1 and config_mount[0]['Source']==str(runtime.root/'gpu/prometheus.yml')
                   and not config_mount[0]['RW'],'O04_EXPECTED_ORIGINAL_GPU_CONFIG_MOUNT')
        tests=native_tests(runtime,state)
        before=runtime.containers();retained.healthy(before)
        end='%.3f'%(time.time()-15)
        history=retained.history(runtime.prom,end); history[GPU_QUERY]=retained.matrix(runtime.prom,GPU_QUERY,end)
        core={'binding':binding(runtime,state),'containers':before,'query_time':end,'history':history,'protected':protected(runtime)}
        op.require(runtime.containers()==before,'O04_BASELINE_CHANGED')
        record={'schema':1,'kind':'O04-deploy','baseline':core,'baseline_sha256':retained.digest(core),
                'tests':tests,'manager_intent':False,'prometheus_intent':None}
        retained.write_record(path,record)
    if record.get('report'):
        # Existing evidence remains valid only for the accepted containers/configuration.
        op.require(record['accepted_containers']==runtime.containers(),'O04_ACCEPTED_CONTAINERS_CHANGED')
        am=manager_containers(runtime.docker,state)
        op.require(len(am)==1 and am[0]['Id']==record['accepted_manager']['id']
                   and am[0]['State']['StartedAt']==record['accepted_manager']['started_at']
                   and am[0]['State']['Running'] and am[0]['State'].get('Health',{}).get('Status')=='healthy',
                   'O04_ACCEPTED_MANAGER_CHANGED')
        return dict(record['report'],evidence_reused=True)
    if not record['manager_intent']:
        record['manager_intent']=True;retained.write_record(path,record)
        manager_compose(runtime,['up','-d','--no-deps','--no-build','--pull','never','--wait','--wait-timeout','90',SERVICE],
                        visible=True,timeout=120)
    items=manager_containers(runtime.docker,state)
    op.require(len(items)==1 and items[0]['State']['Running'] and items[0]['State'].get('Health',{}).get('Status')=='healthy',
               'O04_MANAGER_START_INCOMPLETE_NO_AUTOMATIC_RETRY')
    attach(runtime,state,record,path)
    report=None; last=None; deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        try: report=verify(runtime,state,record);break
        except (ValueError,OSError) as exc: last=exc;time.sleep(2)
    if report is None: raise ValueError('O04_VERIFY_INCOMPLETE: '+str(last))
    record['report']=report;record['accepted_containers']=runtime.containers()
    am=manager_containers(runtime.docker,state)[0]
    record['accepted_manager']={'id':am['Id'],'started_at':am['State']['StartedAt']}
    retained.write_record(path,record)
    return dict(report,evidence_reused=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('deploy','check'))
    args=parser.parse_args()
    # Share the observation stack lock with O01/GPU lifecycle commands.
    lock=op.no_symlinks(op.ROOT/'.ops.lock')
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        runtime=retained.Runtime(op.Docker())
        if args.action=='deploy': report=deploy(runtime)
        else:
            state=op.read_private(runtime.root/'alerts/.state.json');config_guard(runtime,state)
            report=verify(runtime,state,read_record(Path(runtime.state['data_dir']).parent/'alerts-deployment-o04.json'))
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally: os.close(fd)


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,TypeError,subprocess.TimeoutExpired) as exc:
        print(json.dumps({'result':'error','stage':'O04-alert-engine-acceptance',
            'reason':str(exc).replace('. 기존 서비스는 변경하지 않습니다.','.'),
            'instruction':'원본 기준 파일을 보존하세요. deploy 재실행은 이미 기록된 제어 명령을 반복하지 않습니다.'},ensure_ascii=False))
        raise SystemExit(1)
