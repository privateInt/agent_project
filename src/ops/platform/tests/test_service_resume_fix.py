"""Stopped-unit resume and error attribution; all manager responses are synthetic."""
from copy import deepcopy
import json
import os
from pathlib import Path
import socketserver
import struct
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import service_lifecycle as service, worker_state as ws, service_systemd as adapter
from src.ops.platform.ops import service_acceptance as acceptance, lifecycle_probe as probe
from src.ops.platform.tests import test_service_lifecycle as existing
from src.ops.platform.tests import test_lifecycle_systemd_compat as wire


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.f=existing.ServiceTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_clean_stop_resume_does_not_reset_an_unloaded_inactive_unit(self):
        self.f.good('start');self.f.good('stop')
        def reset(): raise ReadFailure('SERVICE_COMMAND_FAILED')
        self.f.manager.reset_failed=reset
        self.f.good('resume')
        self.assertEqual(self.f.manager.starts,2)

    def test_failed_unit_still_requires_reset_after_live_recovery(self):
        self.f.good('start');self.f.good('stop')
        old=self.f.manager.inspect;failed=True;order=[]
        def inspect():
            value=old()
            if failed:value.update(ActiveState='failed',SubState='failed',Result='exit-code',ExecMainStatus='2')
            return value
        def recovery(*a,**kw):order.append('recovery');return self.f.recover(*a,**kw)
        def reset():
            nonlocal failed
            order.append('reset');failed=False
        self.f.manager.inspect=inspect;self.f.manager.reset_failed=reset
        self.f.good('resume',recovery_fn=recovery)
        self.assertEqual(order,['recovery','reset']);self.assertEqual(self.f.manager.starts,2)

    def test_failed_reset_is_not_ignored_or_followed_by_start(self):
        self.f.good('install');old=self.f.manager.inspect
        def inspect():
            value=old();value.update(ActiveState='failed',SubState='failed');return value
        self.f.manager.inspect=inspect
        def reset():raise ReadFailure('SERVICE_COMMAND_FAILED')
        self.f.manager.reset_failed=reset
        result=self.f.call('resume')
        self.assertEqual(result['code'],'SERVICE_COMMAND_FAILED');self.assertEqual(self.f.manager.starts,0)
        self.assertEqual(ws.Store(self.f.directory).control()['desired'],'paused')

    def test_unit_edited_during_live_recovery_cannot_be_reset_or_started(self):
        self.f.good('install')
        def recovery(*a,**kw):
            self.f.recover(*a,**kw);self.f.manager.path.write_text('external change')
        result=self.f.call('resume',recovery_fn=recovery)
        self.assertEqual(result['code'],'SERVICE_UNIT_CHANGED');self.assertEqual(self.f.manager.resets,0)
        self.assertEqual(self.f.manager.starts,0)

    def test_recovery_exit_check_rejects_a_worker_that_reappears(self):
        self.f.good('install')
        def recovery(*a,**kw):self.f.recover(*a,**kw);self.f.manager.active=True;self.f.manager.pid='987'
        result=self.f.call('resume',recovery_fn=recovery)
        self.assertEqual(result['code'],'SERVICE_STILL_RUNNING');self.assertEqual(self.f.manager.starts,0)
        self.assertEqual(self.f.manager.resets,0)

    def test_rejected_command_survives_lifecycle_result_audit_and_acceptance_boundary(self):
        self.f.good('install');self.f.good('start');self.f.good('stop')
        def start():raise adapter.CommandFailure('SERVICE_COMMAND_FAILED','start','nonzero_exit',1)
        self.f.manager.start=start
        result=self.f.call('resume')
        self.assertEqual(result['service_command'],{'operation':'start','outcome':'nonzero_exit','returncode':1})
        audit=ws.read(service.manifest_path(self.f.directory).parent/'service-operation.json')
        self.assertEqual(audit['service_command'],result['service_command'])
        controller=object.__new__(acceptance.Controller);controller.call=lambda _:result
        with self.assertRaises(ReadFailure) as caught:controller.good('resume')
        self.assertEqual(acceptance.error_report(caught.exception)['failure_context'],
                         {'action':'resume','service_command':result['service_command']})


class DiagnosticTests(unittest.TestCase):
    def backend(self,runner):
        r=probe.Reader(env={'PATH':os.defpath},runner=runner);r.paths['systemctl']='/usr/bin/systemctl'
        return adapter.Systemd(r,{'status':'ok','environment':{}},'agent-project-ops-refresh-'+'a'*32+'.service')

    def test_each_rejected_control_command_has_fixed_operation_without_stderr(self):
        for method in ('start','stop','reload','reset_failed'):
            with self.subTest(method=method):
                b=self.backend(lambda *a,**kw:SimpleNamespace(returncode=1,stdout='',stderr='secret key user path'))
                with self.assertRaises(ReadFailure) as caught:getattr(b,method)()
                detail=caught.exception.service_command
                self.assertEqual(detail['operation'],'daemon_reload' if method=='reload' else method)
                self.assertEqual(detail['returncode'],1);self.assertEqual(detail['outcome'],'nonzero_exit')
                self.assertNotIn('secret',json.dumps(detail));self.assertNotIn('path',json.dumps(detail))

    def test_timeout_remains_unknown_and_has_no_returncode(self):
        def fail(*a,**kw):raise subprocess.TimeoutExpired('secret command',10,stderr='secret')
        b=self.backend(fail)
        with self.assertRaises(ReadFailure) as caught:b.stop()
        self.assertEqual(caught.exception.code,'SERVICE_COMMAND_OUTCOME_UNKNOWN')
        self.assertEqual(caught.exception.service_command,{'operation':'stop','outcome':'timeout'})

    def test_diagnostic_sanitizer_discards_unknown_or_unbounded_fields(self):
        self.assertEqual(acceptance.failure_context({'action':'resume','stderr':'secret',
            'service_command':{'operation':'reset_failed','outcome':'nonzero_exit','returncode':1,'stderr':'secret'}}),
            {'action':'resume','service_command':{'operation':'reset_failed','outcome':'nonzero_exit','returncode':1}})
        for value in (None,[],{'operation':[],'outcome':'timeout'},
                      {'operation':'arbitrary','outcome':'timeout'},{'operation':'stop','outcome':{}}):
            self.assertIsNone(adapter.command_details(value))


class ResetBus(socketserver.ThreadingTCPServer):
    """D-Bus error fixture only: it is not a user manager or a GC simulation."""
    allow_reuse_address=True
    daemon_threads=True
    def __init__(self):
        self.calls=[];self.errors=[];self.unit='agent-project-ops-refresh-'+'a'*32+'.service'
        self.loaded=False;outer=self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                try:
                    while True:
                        line=self.rfile.readline().strip(b'\x00\r\n')
                        if line.startswith(b'AUTH'):self.wfile.write((b'DATA' if len(line.split())<3 else b'OK '+b'1'*32)+b'\r\n')
                        elif line.startswith(b'DATA'):self.wfile.write(b'OK '+b'1'*32+b'\r\n')
                        elif line==b'BEGIN':break
                        elif line==b'NEGOTIATE_UNIX_FD':self.wfile.write(b'ERROR unsupported\r\n')
                        else:raise AssertionError('unexpected auth')
                    while True:
                        head=self.rfile.read(16)
                        if not head:return
                        endian,kind,flags,version,size,serial,hlen=struct.unpack('<BBBBIII',head)
                        assert endian==ord('l') and kind==1 and version==1
                        fields=wire.read_fields(self.rfile.read(hlen));self.rfile.read(-(16+hlen)%8)
                        body=self.rfile.read(size);member=fields.get(3);outer.calls.append(member)
                        if member=='Hello':
                            payload=bytearray();wire.basic(payload,'s',':1.42');response=wire.reply(serial,'s',payload)
                        elif member=='ResetFailedUnit':
                            length=struct.unpack_from('<I',body)[0];assert body[4:4+length].decode()==outer.unit
                            if outer.loaded:response=wire.reply(serial,'',b'')
                            else:
                                payload=bytearray();wire.basic(payload,'s','Unit '+outer.unit+' is not loaded.')
                                header_fields=bytearray()
                                for key,typ,value in ((4,'s','org.freedesktop.systemd1.NoSuchUnit'),(5,'u',serial),(8,'g','s')):
                                    wire.pad(header_fields,8);header_fields.append(key);wire.basic(header_fields,'g',typ);wire.basic(header_fields,typ,value)
                                response=bytearray(struct.pack('<BBBBIII',ord('l'),3,0,1,len(payload),serial,len(header_fields)))+header_fields
                                wire.pad(response,8);response+=payload
                        else:raise AssertionError('unexpected method '+str(member))
                        self.wfile.write(response)
                except Exception as exc:outer.errors.append(type(exc).__name__+': '+str(exc))
        super().__init__(('127.0.0.1',0),Handler)


@unittest.skipUnless(os.environ.get('SERVICE_SYSTEMCTL_TEST_BINARY'),'optional native systemctl with isolated protocol fixture')
class NativeResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wire.NativeLoginctlTests.setUpClass.__func__(cls)

    def setUp(self):
        self.bus=ResetBus();thread=threading.Thread(target=self.bus.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.bus.server_close);self.addCleanup(self.bus.shutdown)
        self.binary=os.environ['SERVICE_SYSTEMCTL_TEST_BINARY']
        def run(args,**kw):
            # Product still permits only an owned Unix user bus. The fixture
            # alone substitutes its loopback TCP peer for native client testing.
            env=dict(kw['env'],LD_PRELOAD=self.library,
                     DBUS_SESSION_BUS_ADDRESS='tcp:host=127.0.0.1,port='+str(self.bus.server_address[1]))
            env.pop('XDG_RUNTIME_DIR',None)
            return subprocess.run(args,**dict(kw,env=env))
        reader=probe.Reader(env={'PATH':os.defpath},runner=run);reader.paths['systemctl']=self.binary
        self.backend=adapter.Systemd(reader,{'status':'ok','environment':{}},self.bus.unit)

    def test_native_cli_nosuchunit_is_attributed_to_reset_failed(self):
        with self.assertRaises(ReadFailure) as caught:self.backend.reset_failed()
        self.assertEqual(caught.exception.code,'SERVICE_COMMAND_FAILED')
        self.assertEqual(caught.exception.service_command['operation'],'reset_failed')
        self.assertEqual(caught.exception.service_command['outcome'],'nonzero_exit')
        self.assertIn('ResetFailedUnit',self.bus.calls);self.assertEqual(self.bus.errors,[])

    def test_native_cli_successful_reset_still_accepted(self):
        self.bus.loaded=True;self.backend.reset_failed()
        self.assertIn('ResetFailedUnit',self.bus.calls);self.assertEqual(self.bus.errors,[])


if __name__=='__main__':unittest.main()
