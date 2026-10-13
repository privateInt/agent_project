"""Synthetic Alertmanager lifecycle probe; its webhook only reaches this process."""
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import uuid

import manage as op

PROBE_LABEL = 'org.agent-project.alerts.probe'


def request(port, path, body=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        data = None if body is None else json.dumps(body).encode()
        connection.request('GET' if body is None else 'POST', path, body=data,
                           headers={'Content-Type':'application/json','Connection':'close'})
        response = connection.getresponse()
        raw = response.read(1024*1024+1)
        op.require(response.status == 200 and len(raw) <= 1024*1024, 'PROBE_HTTP_FAILED')
        return json.loads(raw) if raw else None
    finally:
        connection.close()


def eventually(fn, timeout=30):
    deadline = time.monotonic()+timeout
    while True:
        try:
            if fn(): return
        except (OSError, http.client.HTTPException, ValueError): pass
        op.require(time.monotonic() < deadline, 'PROBE_EXPECTATION_TIMEOUT')
        time.sleep(0.2)


def lifecycle(docker, image):
    """Never uses 16330, production volumes, receiver config or upstream credentials."""
    probe = str(uuid.uuid4()); notices = []; guard = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = self.headers.get('Content-Length','')
            if self.path != '/'+probe or not length.isdecimal() or int(length)>65536:
                self.send_error(400); return
            try:
                value = json.loads(self.rfile.read(int(length)))
                valid = value['status'] in ('firing','resolved') and len(value['alerts']) == 1
                valid = valid and value['alerts'][0]['labels'] == {'alertname':'O04SyntheticProbe','scope':probe}
                op.require(valid, 'PROBE_UNEXPECTED_NOTICE')
            except (ValueError, KeyError, TypeError):
                self.send_error(400); return
            with guard: notices.append(value['status'])
            self.send_response(200); self.send_header('Content-Length','0'); self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    op.require(port != 16330, 'PROBE_PORT_CONFLICT')
    container = None
    try:
        with tempfile.TemporaryDirectory(prefix='o04-alert-probe-') as folder:
            path=Path(folder); (path/'data').mkdir(mode=0o700)
            config={'route':{'receiver':'fixture','group_by':['alertname','scope'],
                            'group_wait':'1s','group_interval':'1s','repeat_interval':'1h'},
                    'receivers':[{'name':'fixture','webhook_configs':[{
                        'url':'http://127.0.0.1:'+str(server.server_port)+'/'+probe,'send_resolved':True}]}]}
            (path/'config.json').write_text(json.dumps(config))
            args=['run','-d','--name','o04-probe-'+probe,'--label',PROBE_LABEL+'='+probe,
                  '--network','host','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
                  '--user',str(os.getuid())+':'+str(os.getgid()),'--memory','128m','--memory-swap','128m',
                  '--cpus','0.5','--pids-limit','64','--tmpfs','/tmp:size=16777216,mode=1777',
                  '--log-driver','none','--mount','type=bind,src='+str(path)+',dst=/probe',image,
                  '--config.file=/probe/config.json','--storage.path=/probe/data',
                  '--web.listen-address=127.0.0.1:'+str(port),'--cluster.listen-address=']
            try:
                container=docker.run(args).strip()
                eventually(lambda:bool(request(port,'/api/v2/status')))
                start=datetime.now(timezone.utc)-timedelta(seconds=10)
                alert={'labels':{'alertname':'O04SyntheticProbe','scope':probe},
                       'startsAt':start.isoformat(), 'endsAt':(start+timedelta(minutes=10)).isoformat()}
                request(port,'/api/v2/alerts',[alert])
                eventually(lambda:notices == ['firing'])
                for _ in range(3): request(port,'/api/v2/alerts',[alert])
                rows=request(port,'/api/v2/alerts')
                op.require(len(rows)==1, 'PROBE_DUPLICATE_ALERT')
                fingerprint=rows[0]['fingerprint']
                before=docker.json(['inspect',container])[0]
                docker.run(['restart','--time','10',container],timeout=30)
                eventually(lambda:bool(request(port,'/api/v2/status')))
                request(port,'/api/v2/alerts',[alert])
                eventually(lambda:len(request(port,'/api/v2/alerts'))==1)
                after=docker.json(['inspect',container])[0]
                op.require(before['Id']==after['Id'] and before['Image']==after['Image']
                           and before['Mounts']==after['Mounts']
                           and before['State']['StartedAt']!=after['State']['StartedAt'], 'PROBE_RESTART_IDENTITY')
                end=time.monotonic()+4
                while time.monotonic()<end:
                    op.require(notices==['firing'], 'PROBE_NOTIFICATION_DUPLICATED')
                    time.sleep(0.2)
                op.require(request(port,'/api/v2/alerts')[0]['fingerprint']==fingerprint, 'PROBE_FINGERPRINT_CHANGED')
                alert['endsAt']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
                request(port,'/api/v2/alerts',[alert])
                eventually(lambda:notices==['firing','resolved'])
                eventually(lambda:request(port,'/api/v2/alerts')==[])
                return {'result':'passed','synthetic_only':True,'duplicate_submissions':3,
                        'active_fingerprints':1,'firing_webhooks':1,'resolved_webhooks':1,
                        'notification_dedup_after_same_probe_container_restart':True,
                        'webhook_destination':'temporary_host_loopback_fixture',
                        'production_alerts_posted':0,'external_notifications':0}
            finally:
                # Recover a created probe even if `docker run -d` timed out before returning its ID.
                ids=docker.run(['ps','-aq','--filter','label='+PROBE_LABEL+'='+probe]).split()
                for ident in ids:
                    item=docker.json(['inspect',ident])[0]
                    op.require(item['Config']['Labels'].get(PROBE_LABEL)==probe, 'PROBE_OWNER_MISMATCH')
                    docker.run(['rm','-f',ident])
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
