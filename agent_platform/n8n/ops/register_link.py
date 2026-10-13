#!/usr/bin/env python3
"""Record an existing n8n workflow reference in Agent; does not import or execute it."""
import argparse,hashlib,json,os,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'ops'))
from smoke_tools import API,ROOT

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--agent-url',default='http://127.0.0.1:16100');p.add_argument('--token-file',type=Path,default=ROOT/'.secrets/api-token');p.add_argument('--instance-url',required=True);p.add_argument('--workflow-id',required=True);p.add_argument('--agent-version-id',required=True);p.add_argument('--name',required=True);p.add_argument('--state-file',type=Path,required=True);a=p.parse_args()
 body={'display_name':a.name,'instance_url':a.instance_url,'workflow_id':a.workflow_id,'agent_version_id':a.agent_version_id}
 fingerprint=hashlib.sha256(json.dumps([a.agent_url,body],sort_keys=True).encode()).hexdigest();path=a.state_file.expanduser()
 if path.is_symlink():raise ValueError('State file must not be a symlink')
 if path.exists():
  state=json.loads(path.read_text())
  if state['fingerprint']!=fingerprint:raise ValueError('State belongs to different inputs; choose another state file')
 else:
  state={'fingerprint':fingerprint,'invocation_key':str(uuid.uuid4())};fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as f:json.dump(state,f);f.flush();os.fsync(f.fileno())
 api=API(a.agent_url,a.token_file.expanduser().read_text().strip());result=api.post('/api/workflow-links',body,expected=201,key=state['invocation_key'])
 if api.get('/api/workflow-links/'+result['workflow_link_id'])['workflow_link_id']!=result['workflow_link_id']:raise ValueError('Link lookup mismatch')
 print(json.dumps({'result':'passed','mode':'reference_only','workflow_link_id':result['workflow_link_id'],'edit_url':result['edit_url'],'agent_version_id':result['agent_version_id']},ensure_ascii=False,indent=2))
if __name__=='__main__':
 try:main()
 except (ValueError,OSError,KeyError) as e:raise SystemExit('FAIL: '+str(e))
