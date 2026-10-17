"""Six local integration groups. Real SQLite/files + explicit fake CLI/history."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).absolute().parents[4]
sys.path.insert(0, str(ROOT))
from src.ops.tools import codex_owner_cli_controller as q
from src.ops.tools import codex_owner_turns as turns
from src.ops.tools import codex_daemon_websocket as transport

OBSERVATIONS, ALLOWED_STUBS = [], set()
REAL_POPEN = subprocess.Popen
ROLES = {'test_agent': '01a13fb0-1721-7af3-82e6-747eac4de411', 'reviewer': '01a13fb0-1abe-7623-a15e-cf07071ce54a'}
SECRET = 'SYNTHETIC_STDERR_SECRET_NEVER_PUBLIC'


def policy_module():
    path = ROOT / 'src/ops/tools/tests/fixtures/codex_unattended_admission/flex_gate.py.txt'
    loader = __import__('importlib.machinery', fromlist=['SourceFileLoader']).SourceFileLoader('_policy_fixture', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    result = importlib.util.module_from_spec(spec); loader.exec_module(result)
    return result


def connect(path):
    c = sqlite3.connect(path, isolation_level=None, timeout=0.1)
    c.execute('pragma synchronous=FULL'); c.execute('pragma busy_timeout=100')
    return c


class Fixture:
    def __init__(self, *, enabled=True, script=None):
        self.root = Path(tempfile.mkdtemp(prefix='agent-owner-cli-unit-', dir='/tmp')); self.root.chmod(0o700)
        self.records = self.root/'records'; self.records.mkdir(mode=0o700)
        self.cli = self.root/'codex'
        self.cli.write_text('#!/usr/bin/python3\n'+(script or "import sys\nprint('synthetic queue output, not a native ACK')\nprint('"+SECRET+"',file=sys.stderr)\n")); self.cli.chmod(0o700)
        ALLOWED_STUBS.add(str(self.cli))
        self.db = self.root/'intents.sqlite'; self.db.touch(mode=0o600)
        self.conn = connect(self.db)
        initialized = q.journal.initialize_intent_journal(self.conn)['report']
        assert initialized['reason']=='JOURNAL_READY' and initialized['database_commit_confirmed'] and initialized['transaction_closed']
        self.policy = policy_module()
        self.schedule = {'version':2,'default':{'enabled':True,'start':'00:00','end':'23:59','max_starts':3},'dates':{}}
        self.controller = self.make_controller(self.conn, enabled)
        self.prompt = 'fixed task; spaces\n`literal` $(not-shell)'
        self.intent = json.loads((ROOT/'src/ops/tools/tests/fixtures/codex_unattended_intent_journal/synthetic.json').read_text())['intent']
        self.intent.update(repository='privateInt/agent_project',dev_root=str(q.APPROVED_WORKSPACE),git_common_dir=str(q.APPROVED_WORKSPACE/'.git'),prompt_sha256=q.sha(self.prompt.encode()),settings_sha256=q.sha(q.canonical(self.controller.settings())))
        project={k:self.intent[k] for k in ('project_id','repository','dev_root','git_common_dir')};self.intent['project_sha256']=q.sha(q.canonical(project))
        self.raw=q.canonical(self.intent);self.pin=q.sha(self.raw);self.key=tuple(self.intent[k] for k in ('project_id','batch_id','task_id'))
        self.calls=[]
        OBSERVATIONS.append({'temporary_root':str(self.root),'synthetic_cli':True,'actual_native_calls':0})

    def make_controller(self, c, enabled=True):
        return q.OwnerCLIController(c,self.records,uid=os.geteuid(),cli_path=self.cli,cli_entry_sha256=q.sha(self.cli.read_bytes()),role_threads=ROLES,
                policy=self.policy,schedule=self.schedule,owner_endpoint=str(self.root/'existing-owner.sock'),submission_enabled=enabled,fixture=True)

    def runner(self, argv):
        self.calls.append(list(argv))
        return {'reason':'CLI_RETURNED','returncode':0,'stdout':b'synthetic queue output', 'stderr_sha256':q.sha(SECRET.encode()),'stderr_bytes':len(SECRET),'streams_complete':True}

    def submit(self, runner=None):
        return self.controller.submit(self.raw,self.pin,'test_agent',self.prompt,runner=runner or self.runner)

    def start(self):
        self.result=self.submit();assert self.result['status']=='SUBMISSION_UNKNOWN'
        self.request_pin=self.result['request_sha256'];return self

    def read_row(self):
        c=connect(self.db)
        try:return q.journal.read_intent(c,self.key,self.pin)['record']
        finally:c.close()

    def binding(self, **changes):
        x={'schema':1,'role':'test_agent','thread_id':ROLES['test_agent'],'turn_id':'synthetic-turn','user_message_item_id':'synthetic-user-item',
           'observed_client_id':None,'prompt_sha256':q.sha(self.prompt.encode()),'request_sha256':self.request_pin,'cli_receipt_sha256':self.result['cli_receipt_sha256']}
        x.update(changes);return x

    def turn(self, *, status='completed', prompt=None, client=None, items_view='full'):
        return {'id':'synthetic-turn','status':status,'itemsView':items_view,'error':None,
                'items':[{'type':'userMessage','id':'synthetic-user-item','clientId':client,'content':[{'type':'text','text':self.prompt if prompt is None else prompt}]},
                         {'type':'agentMessage','id':'synthetic-final','phase':'final_answer','text':'Synthetic completed turn; no test PASS claim'}]}

    def owner(self, pages=None, *, turn=None):
        data=pages if pages is not None else [{'data':[turn if turn is not None else self.turn()],'nextCursor':None}]
        class Owner:
            read_only=True;completion_reads=True;allowed_threads=frozenset((ROLES['test_agent'],))
            def __init__(self):self.calls=[]
            def read_turn_page(self, thread, cursor):
                self.calls.append((thread,cursor));value=data[len(self.calls)-1]
                if isinstance(value,Exception):raise value
                return copy.deepcopy(value)
        return Owner()

    def observe(self, owner=None, binding=None):
        return self.controller.observe(self.pin,self.request_pin,self.binding() if binding is None else binding,owner or self.owner())


class ControllerAcceptance(unittest.TestCase):
    def test_C01_claim_confirmation_competition_and_no_replay(self):
        f=Fixture();r=f.start().result;self.assertFalse(r['native_ack_verified']);self.assertEqual(len(f.calls),1)
        row=f.read_row();self.assertEqual((row['state'],row['revision']),('SUBMISSION_UNKNOWN',1));self.assertEqual(row['intent_bytes'],f.raw)
        self.assertEqual(f.submit(),r);self.assertEqual(len(f.calls),1);self.assertEqual(f.conn.execute('select 1').fetchone(),(1,))
        bad=f.controller.submit(f.raw,'f'*64,'test_agent',f.prompt,runner=f.runner);self.assertEqual(bad['status'],'UNCONFIRMED');self.assertEqual(len(f.calls),1)
        for after_commit in (False,True):
            g=Fixture();commit=q.journal._commit
            def lose(c):
                if after_commit:commit(c)
                raise RuntimeError(SECRET)
            # Reserve first so the injected fault covers claim, not reservation.
            g.controller.prepare(g.raw,g.pin,'test_agent',g.prompt)
            original_claim=q.journal.claim_intent
            def injected(*a):
                with mock.patch.object(q.journal,'_commit',side_effect=lose):return original_claim(*a)
            with mock.patch.object(q.journal,'claim_intent',side_effect=injected):out=g.submit()
            self.assertEqual(out['status'],'UNCONFIRMED');self.assertEqual(len(g.calls),0)
            self.assertEqual(g.read_row()['state'],'SUBMISSION_UNKNOWN' if after_commit else 'PREPARED')
            if after_commit:g.submit();self.assertEqual(len(g.calls),0)
        h=Fixture();barrier=threading.Barrier(2);all_calls=[];answers=[]
        def contender():
            c=connect(h.db);controller=h.make_controller(c)
            def once(argv):all_calls.append(argv);time.sleep(.04);return h.runner(argv)
            barrier.wait();answers.append(controller.submit(h.raw,h.pin,'test_agent',h.prompt,runner=once));c.close()
        workers=[threading.Thread(target=contender) for _ in range(2)]
        for t in workers:t.start()
        for t in workers:t.join(timeout=3);self.assertFalse(t.is_alive())
        self.assertEqual(len(all_calls),1);self.assertEqual(h.read_row()['revision'],1)

    def test_C02_cli_receipts_constraints_and_storage_failures(self):
        for script, timeout, expected in [(None,5,'CLI_RETURNED'),("import sys\nprint('exit7')\nsys.exit(7)\n",5,'CLI_RETURNED'),("import time\ntime.sleep(2)\n",.04,'CLI_TIMEOUT_UNKNOWN'),("import sys\nsys.stdout.write('x'*80000)\nsys.stdout.flush()\n",5,'CLI_STREAM_LIMIT_UNKNOWN')]:
            f=Fixture(script=script);captured=io.StringIO()
            with contextlib.redirect_stdout(captured),contextlib.redirect_stderr(captured):r=f.submit(lambda argv:q.queue_once(argv,timeout=timeout))
            self.assertEqual(r['cli_reason'],expected);self.assertFalse(r['native_ack_verified']);self.assertNotIn(SECRET,captured.getvalue())
            folder=f.records/f.pin;receipt=json.loads((folder/'cli-result.json').read_bytes());self.assertEqual(receipt['stdout_sha256'],q.sha((folder/'stdout.bin').read_bytes()))
            self.assertEqual((folder/'cli-result.json').stat().st_mode&0o777,0o600)
            self.assertNotIn(SECRET,(folder/'cli-result.json').read_text());self.assertEqual(f.read_row()['revision'],1)
            count=[];f.submit(lambda argv:count.append(argv));self.assertEqual(count,[])
        f=Fixture().start();argv=f.calls[0]
        self.assertEqual(argv,[str(f.cli),'queue','--remote','unix://'+str(f.root/'existing-owner.sock'),'--approve-for-me','-C',str(q.APPROVED_WORKSPACE),'--thread',ROLES['test_agent'],'--message',f.prompt])
        for filename in ('stdout.bin','cli-result.json'):
            g=Fixture();write=q.write_once
            def broken(p,*args):
                if Path(p).name==filename:raise OSError(SECRET)
                return write(p,*args)
            with mock.patch.object(q,'write_once',side_effect=broken):r=g.submit()
            self.assertEqual(r['status'],'UNCONFIRMED');self.assertEqual(g.read_row()['state'],'SUBMISSION_UNKNOWN');self.assertEqual(len(g.calls),1)
            g.submit();self.assertEqual(len(g.calls),1)
        g=Fixture();g.submit(lambda _argv:(_ for _ in ()).throw(RuntimeError(SECRET)));g.submit();self.assertEqual(g.read_row()['revision'],1)
        # A post-spawn selector failure must reap only the new synthetic CLI.
        g=Fixture(script="import time\ntime.sleep(2)\n");spawned=[]
        def tracked(argv,*a,**kw):
            child=guarded_popen(argv,*a,**kw);spawned.append(child);return child
        with mock.patch.object(subprocess,'Popen',side_effect=tracked),mock.patch.object(q.selectors,'DefaultSelector',side_effect=OSError(SECRET)):
            r=g.submit(lambda argv:q.queue_once(argv))
        self.assertEqual(r['cli_reason'],'CLI_IO_UNKNOWN');self.assertEqual(len(spawned),1)
        self.assertIsNotNone(spawned[0].poll());self.assertTrue(spawned[0].stdout.closed);self.assertTrue(spawned[0].stderr.closed)
        g=Fixture().start();before=(g.records/g.pin/'cli-result.json').read_bytes()
        with mock.patch.object(q.os,'fsync',side_effect=OSError(SECRET)):
            with self.assertRaises(OSError):g.controller.read(g.pin,g.request_pin)
            self.assertEqual(g.submit()['status'],'UNCONFIRMED')
        self.assertEqual(len(g.calls),1);self.assertEqual((g.records/g.pin/'cli-result.json').read_bytes(),before)
        with self.assertRaises(q.files.PackageError):q.queue_once(['/bin/echo','forbidden'])

        for which in ('cli','endpoint'):
            h=Fixture();foreign=Path(tempfile.mkdtemp(prefix='agent-owner-cli-foreign-',dir='/tmp'));foreign.chmod(0o700)
            outside=foreign/('codex' if which=='cli' else 'outside.sock');outside.write_text('synthetic outside target');outside.chmod(0o700)
            if which=='cli':h.cli.unlink();h.cli.symlink_to(outside)
            else:Path(h.controller.owner_endpoint).symlink_to(outside)
            with self.assertRaises(q.files.PackageError):h.make_controller(h.conn)
            self.assertEqual(h.submit()['reason'],'FIXTURE_CLIENT_SCOPE_INVALID');self.assertEqual(h.calls,[])
        disabled=Fixture(enabled=False);self.assertEqual(disabled.submit()['reason'],'SUBMISSION_DISABLED');self.assertEqual(disabled.calls,[])
        for which in range(3):
            h=Fixture();Path(h.controller.pause_paths[which]).touch(mode=0o600)
            self.assertEqual(h.submit()['reason'],'PAUSED');self.assertEqual(h.calls,[])
        for gate in ('schedule','cap','lock','admit-save'):
            h=Fixture()
            if gate=='schedule':h.controller.schedule['default']['enabled']=False;h.intent['settings_sha256']=q.sha(q.canonical(h.controller.settings()));h.raw=q.canonical(h.intent);h.pin=q.sha(h.raw)
            if gate=='cap':p=h.records/'admissions.json';p.write_bytes(q.canonical({'clock_floor_ms':0,'starts':[int(time.time()*1000)]*3}));p.chmod(0o600)
            if gate=='lock':
                with q.files.lock(h.records,os.geteuid()):r=h.submit()
            elif gate=='admit-save':
                with mock.patch.object(q.files,'atomic',side_effect=OSError(SECRET)):r=h.submit()
            else:r=h.submit()
            self.assertEqual(r['status'],'UNCONFIRMED');self.assertEqual(h.calls,[])

    def test_C03_explicit_history_scope_and_matching_transport(self):
        with self.assertRaises(AssertionError):transport._DeadlineSocket(None)
        with self.assertRaises(AssertionError):socket.socket()
        client=object.__new__(transport.UnixWebSocketRpc);client.read_only=True;client.completion_reads=True;client.allowed_threads=frozenset((ROLES['test_agent'],))
        client.deadline=time.monotonic()+10;client.request_timeout=5;client.next_id=1;client.completed_ids=set();client.notifications=0;client.stdout_bytes=0
        sent=[];client._send=lambda value:sent.append(q.canonical(value));client.sock=types.SimpleNamespace(_bounded=lambda:None)
        client.ws=types.SimpleNamespace(recv_data=lambda:(1,q.canonical({'id':1,'result':{'data':[],'nextCursor':None}})))
        client.close=mock.Mock();self.assertEqual(client.read_turn_page(ROLES['test_agent']),{'data':[],'nextCursor':None})
        payload=json.loads(sent[0]);self.assertEqual(payload['method'],'thread/turns/list');self.assertEqual(payload['params'],{'threadId':ROLES['test_agent'],'cursor':None,'limit':20,'itemsView':'full','sortDirection':'desc'})
        for method,params in [('thread/resume',{'threadId':ROLES['test_agent']}),('thread/queue/add',{}),('thread/items/list',{}),('thread/read',{'threadId':ROLES['test_agent'],'includeTurns':True})]:
            with self.assertRaises(transport.ProbeError):client._validate(method,params)
        full=payload['params']
        for key,value in [('threadId',ROLES['reviewer']),('limit',21),('cursor',''),('itemsView','summary'),('extra',True)]:
            changed=dict(full);changed[key]=value
            with self.assertRaises(transport.ProbeError):client._validate('thread/turns/list',changed)
        client.completion_reads=False
        with self.assertRaises(transport.ProbeError):client._validate('thread/turns/list',full)
        with self.assertRaises(transport.ProbeError):client.read_turn_page(ROLES['test_agent'])
        self.assertEqual(len(sent),1)

    def test_C04_owner_binding_identity_and_exact_prompt(self):
        f=Fixture().start();owner=f.owner();r=f.controller.observe(f.pin,f.request_pin,None,owner);self.assertEqual(r['status'],'UNBOUND');self.assertEqual(owner.calls,[])
        for changes in ({'request_sha256':'f'*64},{'role':'reviewer'},{'thread_id':ROLES['reviewer']},{'user_message_item_id':'wrong'},{'turn_id':'wrong'},{'observed_client_id':'wrong'}):
            g=Fixture().start();client=g.owner()
            try:r=g.observe(client,g.binding(**changes))
            except q.files.PackageError:r={'status':'REJECTED'}
            self.assertNotEqual(r['status'],'TURN_COMPLETED');self.assertFalse((g.records/g.pin/'terminal.json').exists())
        for actual in (None,'MISSING'):
            g=Fixture().start();turn=g.turn()
            if actual=='MISSING':del turn['items'][0]['clientId']
            self.assertEqual(g.observe(g.owner(turn=turn))['status'],'TURN_COMPLETED')
        g=Fixture().start();r=g.observe(g.owner(turn=g.turn(prompt=g.prompt+' ')))
        self.assertEqual(r['reason'],'PROMPT_MISMATCH');self.assertFalse((g.records/g.pin/'terminal.json').exists())
        g=Fixture().start();turn=g.turn();turn['items'].append(copy.deepcopy(turn['items'][0]));self.assertNotEqual(g.observe(g.owner(turn=turn))['status'],'TURN_COMPLETED')

    def test_C05_pagination_terminal_states_and_storage(self):
        f=Fixture().start();pages=[{'data':[],'nextCursor':'second'},{'data':[f.turn()],'nextCursor':None}];owner=f.owner(pages);r=f.observe(owner)
        self.assertEqual(r['status'],'TURN_COMPLETED');self.assertEqual(owner.calls,[(ROLES['test_agent'],None),(ROLES['test_agent'],'second')]);self.assertFalse(r['native_ready'])
        again=f.observe(f.owner([RuntimeError('must not read terminal again')]));self.assertEqual(again,r)
        for status in ('failed','interrupted','inProgress'):
            g=Fixture().start();r=g.observe(g.owner(turn=g.turn(status=status)));self.assertEqual(r['status'],{'failed':'TURN_FAILED','interrupted':'TURN_INTERRUPTED','inProgress':'TURN_IN_PROGRESS'}[status])
        for pages in ([{'data':[],'nextCursor':'again'}]*4,[{'data':[],'nextCursor':str(i)} for i in range(4)],[RuntimeError(SECRET)], [{'data':[]}], [{'data':[],'nextCursor':None,'extra':'bad'}]):
            g=Fixture().start();r=g.observe(g.owner(pages));self.assertEqual(r['status'],'UNCONFIRMED');self.assertFalse((g.records/g.pin/'terminal.json').exists())
        for view in ('summary','notLoaded'):
            g=Fixture().start();self.assertEqual(g.observe(g.owner(turn=g.turn(items_view=view)))['reason'],'TURN_ITEMS_INCOMPLETE')
        g=Fixture().start();turn=g.turn();self.assertEqual(g.observe(g.owner([{'data':[turn,turn],'nextCursor':None}]))['reason'],'TURN_AMBIGUOUS')
        g=Fixture().start();client=g.owner([{'data':[],'nextCursor':None,'backwardsCursor':'x'*1048576}]);self.assertEqual(g.observe(client)['reason'],'OBSERVATION_BYTE_LIMIT')
        g=Fixture().start();monotonic=iter((0,0,21))
        with mock.patch.object(turns.time,'monotonic',side_effect=lambda:next(monotonic)):r=g.observe(g.owner())
        self.assertEqual(r['reason'],'OBSERVATION_TIMEOUT')
        g=Fixture().start();write=q.write_once
        def break_terminal(p,*a):
            if Path(p).name=='terminal.json':raise OSError(SECRET)
            return write(p,*a)
        with mock.patch.object(q,'write_once',side_effect=break_terminal):
            with self.assertRaises(OSError):g.observe()
        self.assertFalse((g.records/g.pin/'terminal.json').exists());self.assertEqual(g.read_row()['state'],'SUBMISSION_UNKNOWN')

    def test_C06_bound_checks_and_draft_result_are_not_a_trusted_gate(self):
        f=Fixture().start();f.observe()
        expected={'repository':f.intent['repository'],'pr_number':25,'base_sha':f.intent['base_sha'],'head_sha':'b'*40,'release_sha256':'c'*64,'plan_sha256':f.intent['plan_sha256'],'evidence_sha256':'d'*64}
        apps={name:100+i for i,name in enumerate(q.audit.REQUIRED_CONTEXTS)}
        runs=[{'id':i+1,'name':name,'app':{'id':apps[name]},'head_sha':expected['head_sha'],'status':'completed','conclusion':'success'} for i,name in enumerate(apps)]
        evidence={'binding':expected,'binding_digest':q.audit.canonical_binding_digest(expected),'snapshot':{'total_count':len(runs),'check_runs':runs}}
        draft={'number':25,'state':'open','draft':True,'head':{'sha':expected['head_sha'],'repo':{'full_name':expected['repository']}},'base':{'sha':expected['base_sha'],'repo':{'full_name':expected['repository']}}}
        r=f.controller.record_delivery(f.pin,f.request_pin,expected,apps,evidence,draft);self.assertEqual(r['status'],'DELIVERY_EVIDENCE_RECORDED')
        self.assertFalse(r['native_ready']);self.assertFalse(r['independent_review_verified']);self.assertFalse(r['merge_authorized'])
        for changed in ('failure','missing-review','head','repo','not-draft','malformed-head','artifact-binding'):
            e,p=copy.deepcopy(evidence),copy.deepcopy(draft)
            if changed=='failure':e['snapshot']['check_runs'][0]['conclusion']='failure'
            if changed=='missing-review':e['snapshot']['check_runs'].pop(1);e['snapshot']['total_count']-=1
            if changed=='head':p['head']['sha']='e'*40
            if changed=='repo':p['head']['repo']['full_name']='other/agent_project'
            if changed=='not-draft':p['draft']=False
            if changed=='malformed-head':p['head']=None
            if changed=='artifact-binding':e['binding']=dict(expected,evidence_sha256='e'*64);e['binding_digest']=q.audit.canonical_binding_digest(e['binding'])
            self.assertEqual(f.controller.record_delivery(f.pin,f.request_pin,expected,apps,e,p)['status'],'DELIVERY_EVIDENCE_PENDING')
        bad=dict(expected);bad['plan_sha256']='f'*64
        with self.assertRaises(q.files.PackageError):f.controller.record_delivery(f.pin,f.request_pin,bad,apps,evidence,draft)
        OBSERVATIONS.append({'groups':'C01-C06','real_sqlite_and_private_files':True,'cli':'synthetic executable only','history':'fake existing owner transport','actual_codex_queue':0,'actual_rpc':0,'gate_authorized':False})


def guarded_popen(argv,*a,**kw):
    if type(argv) is not list or argv[0] not in ALLOWED_STUBS:
        raise AssertionError('REAL_SUBPROCESS_FORBIDDEN')
    return REAL_POPEN(argv,*a,**kw)


if __name__=='__main__':
    names=sys.argv[1:] or ['ControllerAcceptance']
    suite=unittest.defaultTestLoader.loadTestsFromNames(names,sys.modules[__name__])
    with mock.patch.object(socket,'socket',side_effect=AssertionError('LIVE_SOCKET_FORBIDDEN')),mock.patch.object(transport,'_DeadlineSocket',side_effect=AssertionError('LIVE_SOCKET_SUBCLASS_FORBIDDEN')),mock.patch.object(subprocess,'Popen',side_effect=guarded_popen):
        result=unittest.TextTestRunner(verbosity=2,failfast=True).run(suite)
    root=Path(tempfile.mkdtemp(prefix='agent-owner-cli-results-',dir='/tmp'));root.chmod(0o700);path=root/'result.json'
    with path.open('x') as stream:json.dump({'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skips':len(result.skipped),'observations':OBSERVATIONS,'actual_codex_queue':0,'native_rpc':0},stream,indent=2)
    path.chmod(0o600);print('RESULT_FILE='+str(path),flush=True)
    raise SystemExit(not result.wasSuccessful())
