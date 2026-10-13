"""Independent loginctl option semantics, metadata-only failures and optional native CLI probe.

The native fixture is an isolated loopback D-Bus protocol peer, not logind or a user manager.
Only tests inject its TCP address into the CLI environment. Product command environments do not.
"""
import json
import os
from pathlib import Path
import socketserver
import struct
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops_platform.ops import lifecycle_probe as probe
from ops_platform.tests import test_auto_refresh as prior


class CompatibilityTests(unittest.TestCase):
    def reader(self, fail=None, env=None):
        c = prior.Commands({}); c.fail = fail
        return probe.Reader(env=env or {}, runner=c, which=lambda name, **kw: '/fixture/' + name), c

    def test_loginctl_uses_separate_properties_and_includes_empty_values(self):
        r,c = self.reader()
        result = probe.systemd(r, None)
        self.assertEqual(result['login']['status'], 'ok'); self.assertIsNone(result['login']['runtime_path'])
        args = next(args for tool,args,_ in c.calls if tool == 'loginctl')
        self.assertEqual([s for s in args if s.startswith('--property=')],
                         ['--property=UID','--property=Linger','--property=RuntimePath','--property=State'])
        self.assertIn('--all', args)

    def test_allowlist_refuses_comma_loginctl_and_other_user_or_control(self):
        self.assertFalse(probe.allow('loginctl', ['--no-pager','--no-ask-password','show-user',str(os.getuid()),
                                                '--property=UID,Linger,RuntimePath,State']))
        args = probe.system_user_args(); self.assertTrue(probe.allow('systemctl', args))
        for index,new in ((-1, 'user@'+str(os.getuid()+1)+'.service'), (3, 'start'), (4, '--all')):
            bad=list(args); bad[index]=new; self.assertFalse(probe.allow('systemctl',bad))

    def test_empty_logind_output_has_missing_property_diagnostics(self):
        r,c = self.reader(); original = r.runner
        r.runner = lambda args, **kw: (SimpleNamespace(returncode=0, stdout='', stderr='')
                                      if Path(args[0]).name == 'loginctl' else original(args,**kw))
        result = probe.systemd(r,None)
        self.assertEqual(result['linger'], 'unknown')
        self.assertEqual(result['login']['missing_properties'], ['Linger','State','UID'])
        self.assertEqual(result['login']['observed_properties'], [])

    def test_active_system_user_unit_does_not_claim_user_bus_is_reachable(self):
        r,c = self.reader(fail=lambda tool,args: tool=='systemctl' and args[0]=='--user')
        result = probe.systemd(r,None)
        self.assertEqual(result['status'], 'unavailable'); self.assertTrue(result['linger'])
        self.assertEqual(result['system_user_service']['active_state'], 'active')
        self.assertEqual(result['environment']['xdg_runtime']['status'], 'not_set')

    def test_system_query_failure_is_independent_and_sanitized(self):
        r,c = self.reader(fail=lambda tool,args: tool=='systemctl' and args[0] in ('--user','--system'))
        result=probe.systemd(r,None)
        self.assertEqual(result['system_user_service']['code'],'INVALID_SYSTEMD_RESPONSE')
        self.assertEqual(result['login']['status'],'ok'); self.assertTrue(result['linger'])
        self.assertNotIn('private-secret-not-for-output',json.dumps(result))

    def test_missing_bus_and_explicit_path_are_observed_without_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            before=dict(os.environ)
            r,c=self.reader(env={'XDG_RUNTIME_DIR':tmp},fail=lambda tool,args:tool=='systemctl' and args[0]=='--user')
            c.runtime_path=tmp;c.linger='no'
            result=probe.systemd(r,None)
            self.assertEqual(result['environment']['xdg_runtime']['bus']['code'],'USER_BUS_SOCKET_MISSING')
            self.assertTrue(result['environment']['xdg_matches_logind'])
            self.assertEqual(result['environment']['fallback'],'explicit_environment_preserved')
            self.assertFalse(result['linger']);self.assertEqual(dict(os.environ),before)
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_invalid_runtime_does_not_read_outside_owned_directory(self):
        self.assertEqual(probe.runtime_metadata('relative/path')['code'],'INVALID_PATH')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'shared';path.mkdir(mode=0o755)
            result=probe.runtime_metadata(str(path))
            self.assertEqual(result['code'],'PRIVATE_OWNER_MODE_MISMATCH')
            self.assertEqual(result['bus']['status'],'not_checked')
            link=Path(tmp)/'link';link.symlink_to(path)
            self.assertEqual(probe.runtime_metadata(str(link))['code'],'SYMLINK_REFUSED')

    def test_inactive_unit_metadata_and_no_extra_query_on_healthy_manager(self):
        r,c=self.reader();probe.systemd(r,None)
        self.assertFalse(any(args[0]=='--system' for tool,args,_ in c.calls if tool=='systemctl'))
        original=r.runner
        def inactive(args,**kw):
            if args[1]=='--system':
                return SimpleNamespace(returncode=0,stderr='',stdout='Id=user@'+str(os.getuid())+'.service\nLoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success')
            return original(args,**kw)
        r.runner=inactive
        self.assertEqual(probe.system_user_metadata(r)['active_state'],'inactive')


def pad(buf, size):
    buf.extend(b'\0' * (-len(buf) % size))


def basic(buf, kind, value):
    if kind == 'g':
        raw=value.encode();buf.extend(bytes([len(raw)])+raw+b'\0')
    elif kind in ('s','o'):
        pad(buf,4);raw=value.encode();buf.extend(struct.pack('<I',len(raw))+raw+b'\0')
    elif kind in ('u','b'):
        pad(buf,4);buf.extend(struct.pack('<I',value))
    else:raise AssertionError(kind)


def dictionary(fields):
    buf=bytearray(b'\0'*8)
    for key,kind,value in fields:
        pad(buf,8);basic(buf,'s',key);basic(buf,'g',kind);basic(buf,kind,value)
    struct.pack_into('<I',buf,0,len(buf)-8)
    return buf


def reply(serial, signature, body):
    fields=bytearray()
    for key,kind,value in ((5,'u',serial),(8,'g',signature)):
        pad(fields,8);fields.append(key);basic(fields,'g',kind);basic(fields,kind,value)
    header=bytearray(struct.pack('<BBBBIII',ord('l'),2,0,1,len(body),serial,len(fields)))+fields
    pad(header,8)
    return header+body


def read_fields(header):
    values={};pos=0
    while pos<len(header):
        pos=(pos+7)//8*8
        if pos>=len(header):break
        key=header[pos];length=header[pos+1];kind=header[pos+2:pos+2+length].decode();pos+=length+3
        if kind in ('s','o'):
            pos=(pos+3)//4*4;size=struct.unpack_from('<I',header,pos)[0];pos+=4
            value=header[pos:pos+size].decode();pos+=size+1
        elif kind=='g':
            size=header[pos];pos+=1;value=header[pos:pos+size].decode();pos+=size+1
        elif kind=='u':
            pos=(pos+3)//4*4;value=struct.unpack_from('<I',header,pos)[0];pos+=4
        else:raise AssertionError(kind)
        values[key]=value
    return values


class SyntheticBus(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True

    def __init__(self):
        self.calls=[];self.errors=[];self.linger=False;self.runtime='/synthetic/runtime';outer=self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                try:
                    while True:
                        line=self.rfile.readline().strip(b'\x00\r\n')
                        if line.startswith(b'AUTH'):
                            self.wfile.write((b'DATA' if len(line.split()) < 3 else b'OK '+b'1'*32)+b'\r\n')
                        elif line.startswith(b'DATA'):self.wfile.write(b'OK '+b'1'*32+b'\r\n')
                        elif line==b'BEGIN':break
                        elif line==b'NEGOTIATE_UNIX_FD':self.wfile.write(b'ERROR unsupported\r\n')
                        else:raise AssertionError(line)
                    while True:
                        head=self.rfile.read(16)
                        if not head:return
                        endian,type_,flags,version,bodylen,serial,hlen=struct.unpack('<BBBBIII',head)
                        assert endian==ord('l') and type_==1 and version==1
                        fields=read_fields(self.rfile.read(hlen));self.rfile.read(-(16+hlen)%8)
                        body=self.rfile.read(bodylen);member=fields.get(3);outer.calls.append(member)
                        if member=='Hello':
                            payload=bytearray();basic(payload,'s',':1.42');sig='s'
                        elif member=='GetUser':
                            assert struct.unpack('<I',body)[0]==os.getuid()
                            payload=bytearray();basic(payload,'o','/org/freedesktop/login1/user/_current');sig='o'
                        elif member=='GetAll':
                            values = ([('Version','s','255'),('SystemState','s','running')]
                                      if fields.get(1)=='/org/freedesktop/systemd1' else
                                      [('UID','u',os.getuid()),('Linger','b',outer.linger),
                                       ('RuntimePath','s',outer.runtime),('State','s','active'),('Unselected','s','sentinel')])
                            payload=dictionary(values);sig='a{sv}'
                        else:raise AssertionError(member)
                        self.wfile.write(reply(serial,sig,payload))
                except Exception as exc:outer.errors.append(type(exc).__name__+': '+str(exc))
        super().__init__(('127.0.0.1',0),Handler)


@unittest.skipUnless(os.environ.get('LIFECYCLE_LOGINCTL_TEST_BINARY'),'optional native loginctl fixture')
class NativeLoginctlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The test container has no systemd PID 1. Simulate only its boot-marker existence for the native client;
        # actual D-Bus traffic still goes to the isolated protocol fixture, never the host system bus.
        cls.temp=tempfile.TemporaryDirectory(prefix='ops-loginctl-native-');cls.addClassCleanup(cls.temp.cleanup)
        source=Path(cls.temp.name)/'fixture.c';source.write_text('''#define _GNU_SOURCE
#include <dlfcn.h>
#include <string.h>
int faccessat(int dirfd, const char *path, int mode, int flags) {
    if (strcmp(path, "/run/systemd/system/")==0 || strcmp(path, "/run/systemd/system")==0) return 0;
    int (*original)(int,const char*,int,int) = dlsym(RTLD_NEXT, "faccessat");
    return original(dirfd,path,mode,flags);
}
''')
        cls.library=str(Path(cls.temp.name)/'fixture.so')
        subprocess.run(['cc','-shared','-fPIC','-o',cls.library,str(source),'-ldl'],capture_output=True,check=True,timeout=10)

    def setUp(self):
        self.bus=SyntheticBus();thread=threading.Thread(target=self.bus.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.bus.server_close);self.addCleanup(self.bus.shutdown)
        self.binary=os.environ['LIFECYCLE_LOGINCTL_TEST_BINARY']
        self.env=dict(os.environ,LC_ALL='C',LD_PRELOAD=self.library,
                      DBUS_SYSTEM_BUS_ADDRESS='tcp:host=127.0.0.1,port='+str(self.bus.server_address[1]))

    def run_cli(self,args):
        result=subprocess.run([self.binary,*args],env=self.env,text=True,capture_output=True,timeout=8)
        self.assertEqual(result.returncode,0,(result.stderr,self.bus.errors))
        self.assertEqual(self.bus.errors,[])
        return result.stdout.strip()

    def test_comma_selector_does_not_return_properties(self):
        out=self.run_cli(['show-user',str(os.getuid()),'--property=UID,Linger,RuntimePath,State'])
        self.assertEqual(out,'')
        self.assertIn('GetAll',self.bus.calls)

    def test_repeated_selector_returns_exact_fields_and_boolean(self):
        out=self.run_cli(probe.login_args());values=probe.properties(out,set(probe.LOGIN_PROPERTIES.split(',')))
        self.assertEqual(values,{'UID':str(os.getuid()),'Linger':'no','RuntimePath':self.bus.runtime,'State':'active'})
        self.assertNotIn('sentinel',out)

    def test_empty_runtime_and_true_linger_are_preserved(self):
        self.bus.runtime='';self.bus.linger=True
        values=probe.properties(self.run_cli(probe.login_args()),set(probe.LOGIN_PROPERTIES.split(',')))
        self.assertEqual(values['RuntimePath'],'');self.assertEqual(values['Linger'],'yes')

    def test_initial_selection_reaches_native_systemctl_without_error_text_gate(self):
        runtime=Path(self.temp.name)/'current-user-runtime';runtime.mkdir(mode=0o700,exist_ok=True)
        self.bus.runtime=str(runtime);manager_calls=[];before=dict(os.environ)
        def runner(args,**kw):
            environment=dict(kw['env'],LD_PRELOAD=self.library)
            if Path(args[0]).name=='loginctl':
                environment['DBUS_SYSTEM_BUS_ADDRESS']=self.env['DBUS_SYSTEM_BUS_ADDRESS']
            elif args[1:2]==['--user']:
                # Product selected the validated Unix path before the first call. Only this test substitutes TCP.
                self.assertEqual(environment['XDG_RUNTIME_DIR'],str(runtime))
                self.assertEqual(environment['DBUS_SESSION_BUS_ADDRESS'],'unix:path='+str(runtime)+'/bus')
                manager_calls.append(args)
                environment['DBUS_SESSION_BUS_ADDRESS']=self.env['DBUS_SYSTEM_BUS_ADDRESS']
            return subprocess.run(args,**dict(kw,env=environment))
        reader=probe.Reader(env={},runner=runner,which=lambda name,**kw:'/usr/bin/'+name)
        with patch.object(probe,'validate_runtime',side_effect=lambda path:str(runtime) if path==str(runtime) else self.fail('wrong runtime')):
            result=probe.systemd(reader,None)
        self.assertEqual(result['status'],'ok',result);self.assertEqual(result['manager_version'],255)
        self.assertEqual(result['environment']['selection'],'verified_logind_runtime_before_query')
        self.assertEqual(len(manager_calls),1);self.assertEqual(dict(os.environ),before)
        self.assertEqual(self.bus.errors,[])


if __name__=='__main__':unittest.main()
