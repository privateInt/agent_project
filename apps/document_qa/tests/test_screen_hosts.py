"""LAN listener regressions using two actual loopback interfaces and disposable state."""
import http.client
import json
import socket
import select
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from apps.document_qa.api import additional_listeners, make_server
from apps.document_qa.intake import UploadError
from apps.document_qa.tests.test_uploads import FakeData, running
from apps.document_qa.uploads import UploadStore


class ScreenHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = UploadStore.initialize(Path(self.temp.name) / 'state')
        self.upstream = FakeData()

    def request(self, address, port, path='/console', *, auth=False, headers=None, method='GET'):
        connection = http.client.HTTPConnection(address, port, timeout=4)
        self.addCleanup(connection.close)
        fields = {'Authorization': 'Bearer ' + self.store.token_path.read_text().strip()} if auth else {}
        fields.update(headers or {})
        connection.request(method, path, headers=fields)
        response = connection.getresponse()
        return response.status, response.read()

    def test_both_interfaces_use_same_handler_key_store_and_port(self):
        before = self.store.db_path.read_bytes(), self.store.token_path.read_bytes()
        server = make_server(self.store, self.upstream, port=0)
        with running(server) as port, additional_listeners(server, ['127.0.0.2']) as extra:
            self.assertIs(extra[0].RequestHandlerClass, server.RequestHandlerClass)
            self.assertEqual(extra[0].server_address, ('127.0.0.2', port))
            for address in ('127.0.0.1', '127.0.0.2'):
                self.assertEqual(self.request(address, port)[0], 200)
                self.assertEqual(self.request(address, port, '/console/questions.js')[0], 200)
                self.assertEqual(self.request(address, port, '/api/library')[0], 401)
                code, raw = self.request(address, port, '/api/library', auth=True)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(raw)['library_id'], self.store.library()['library_id'])
        self.assertEqual((self.store.db_path.read_bytes(), self.store.token_path.read_bytes()), before)
        self.assertEqual(self.upstream.calls, [])

    def test_each_listener_keeps_exact_host_port_and_origin_checks(self):
        server = make_server(self.store, self.upstream, port=0)
        with running(server) as port, additional_listeners(server, ['127.0.0.2']):
            for address, other in (('127.0.0.1', '127.0.0.2'), ('127.0.0.2', '127.0.0.1')):
                host = address + ':' + str(port)
                self.assertEqual(self.request(address, port, '/api/library', auth=True,
                    headers={'Host': host, 'Origin': 'http://' + host})[0], 200)
                for path in ('/console', '/api/library'):
                    for bad in ('attacker.example', other + ':' + str(port), address + ':1'):
                        code, raw = self.request(address, port, path, auth=True, headers={'Host': bad})
                        self.assertEqual((code, json.loads(raw)['code']), (403, 'HOST_NOT_ALLOWED'))
                for origin in ('null', 'http://attacker.example', 'http://' + other + ':' + str(port)):
                    code, raw = self.request(address, port, '/api/uploads', auth=True, method='POST',
                                             headers={'Host': host, 'Origin': origin})
                    self.assertEqual((code, json.loads(raw)['code']), (403, 'ORIGIN_NOT_ALLOWED'))
        self.assertEqual(self.store.jobs(), [])
        self.assertEqual(self.upstream.calls, [])

    def test_legacy_hostless_HTTP_1_0_private_read_still_works_on_both(self):
        server = make_server(self.store, self.upstream, port=0)
        token = self.store.token_path.read_text().strip()
        with running(server) as port, additional_listeners(server, ['127.0.0.2']):
            for address in ('127.0.0.1', '127.0.0.2'):
                with socket.create_connection((address, port), timeout=4) as connection:
                    connection.sendall(('GET /api/library HTTP/1.0\r\nAuthorization: Bearer ' + token + '\r\n\r\n').encode())
                    self.assertIn(b' 200 ', connection.recv(4096).split(b'\r\n')[0])

    def test_connection_limit_is_shared_across_interfaces(self):
        server = make_server(self.store, self.upstream, port=0)
        server.slots = threading.BoundedSemaphore(1)
        with running(server) as port, additional_listeners(server, ['127.0.0.2']):
            self.assertTrue(server.slots.acquire(blocking=False))
            try:
                for address in ('127.0.0.1', '127.0.0.2'):
                    self.assertEqual(self.request(address, port)[0], 503)
            finally:
                server.slots.release()
            self.assertEqual(self.request('127.0.0.2', port, '/api/library', auth=True)[0], 200)

    def test_extra_listener_closes_without_closing_primary(self):
        server = make_server(self.store, self.upstream, port=0)
        with running(server) as port:
            with additional_listeners(server, ['127.0.0.2']):
                self.assertEqual(self.request('127.0.0.2', port)[0], 200)
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.2', port), timeout=1)
            self.assertEqual(self.request('127.0.0.1', port)[0], 200)

    def test_invalid_extra_addresses_do_not_write_state_or_keep_partial_listener(self):
        before = self.store.db_path.read_bytes(), self.store.token_path.read_bytes()
        server = make_server(self.store, self.upstream, port=0)
        with running(server) as port:
            cases = [(['127.0.0.1'], 'DUPLICATE_BIND_ADDRESS'),
                     (['127.0.0.2', '127.0.0.2'], 'DUPLICATE_BIND_ADDRESS'),
                     (['0.0.0.0'], 'PRIVATE_BIND_REQUIRED'),
                     (['8.8.8.8'], 'PRIVATE_BIND_REQUIRED'),
                     (['localhost'], 'BIND_ADDRESS_INVALID'),
                     ([None], 'BIND_ADDRESS_INVALID'),
                     (['127.0.0.2'] * 5, 'ADDITIONAL_BIND_LIMIT'),
                     (['127.0.0.2', '0.0.0.0'], 'PRIVATE_BIND_REQUIRED')]
            for addresses, expected in cases:
                with self.subTest(addresses=addresses), self.assertRaises(UploadError) as caught:
                    with additional_listeners(server, addresses):
                        self.fail('invalid listener accepted')
                self.assertEqual(caught.exception.code, expected)
            with additional_listeners(server, ['127.0.0.2']):
                self.assertEqual(self.request('127.0.0.2', port)[0], 200)
        self.assertEqual((self.store.db_path.read_bytes(), self.store.token_path.read_bytes()), before)

    def test_conflicting_port_closes_earlier_extra_listener(self):
        server = make_server(self.store, self.upstream, port=0)
        with running(server) as port, socket.socket() as occupied:
            occupied.bind(('127.0.0.3', port))
            occupied.listen()
            with self.assertRaises(OSError):
                with additional_listeners(server, ['127.0.0.2', '127.0.0.3']):
                    self.fail('conflicting port accepted')
            with additional_listeners(server, ['127.0.0.2']):
                self.assertEqual(self.request('127.0.0.2', port)[0], 200)
            self.assertEqual(self.request('127.0.0.1', port)[0], 200)

    def test_primary_still_rejects_wildcard_and_public_bind(self):
        for address in ('0.0.0.0', '8.8.8.8'):
            with self.subTest(address=address), self.assertRaises(UploadError) as caught:
                make_server(self.store, self.upstream, port=0, bind=address)
            self.assertEqual(caught.exception.code, 'PRIVATE_BIND_REQUIRED')

    def test_real_CLI_two_addresses_and_SIGINT_keep_existing_state_and_policy(self):
        from apps.document_qa.tests.test_question_head import Fixture
        fixture = Fixture()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.store = fixture.uploads
        before = self.store.db_path.read_bytes(), self.store.token_path.read_bytes()
        settings = Path(self.temp.name) / 'settings.json'
        settings.write_text(json.dumps({'schema': 1, 'library_name': '문서 자료함', 'defaults': {},
            'platforms': {role: {'url': 'http://127.0.0.1:1', **(
                {'token_file': str(self.store.token_path)} if role == 'agent' else {})}
                for role in ('data', 'model', 'knowledge', 'agent')}}))
        settings.chmod(0o600)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-B', '-m', 'apps.document_qa.api', 'serve',
            '--state-dir', str(self.store.root), '--settings', str(settings),
            '--bind', '127.0.0.1', '--additional-bind', '127.0.0.2', '--port', str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stop():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'CLI listening output timed out')
        report = json.loads(process.stdout.readline())
        self.assertEqual(report['result'], 'listening', report)
        self.assertEqual(report['additional_binds'], ['127.0.0.2'])
        self.assertEqual(report['screen_version'], 'q08.1')
        for key in ('indexing_inference_enabled', 'question_inference_enabled',
                    'head_activation_enabled', 'question_ready'):
            self.assertIs(report[key], False)
        for address in ('127.0.0.1', '127.0.0.2'):
            self.assertEqual(self.request(address, port)[0], 200)
            self.assertEqual(self.request(address, port, '/api/library', auth=True)[0], 200)
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual((self.store.db_path.read_bytes(), self.store.token_path.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
