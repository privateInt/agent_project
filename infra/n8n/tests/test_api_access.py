"""Focused password validation, stage reporting, and non-mutating diagnosis."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('infra_api_access', Path(__file__).resolve().parents[1] / 'ops/api_access.py')
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


def owner(password='Aa123456'):
    return {'email': 'check@example.invalid', 'firstName': '승훈', 'lastName': '신', 'password': password}


class AccessTests(unittest.TestCase):
    def test_password_rules_and_javascript_length(self):
        for password in ['Aa123456', 'A1' + '가' * 62, 'A1' + '😀' * 31]:
            access.validate_setup(owner(password), 8)
        for password in ['Aa12345', 'a1234567', 'Abcdefgh', 'A١abcdef', 'Ａ1abcdef',
                         'A1' + '가' * 63, 'A1' + '😀' * 32]:
            with self.subTest(case=len(password)), self.assertRaises(ValueError):
                access.validate_setup(owner(password), 8)
        with self.assertRaises(ValueError): access.validate_setup(owner(), 16)
        access.validate_setup(owner('A1' + 'a' * 14), 16)

    def test_server_policy_and_unknown_registration_state(self):
        self.assertEqual(access.password_minimum({}), 8)
        self.assertEqual(access.password_minimum({'userManagement': {'passwordMinLength': 16}}), 16)
        for value in [True, '16', 7, 65]:
            with self.assertRaises(ValueError):
                access.password_minimum({'userManagement': {'passwordMinLength': value}})
        with self.assertRaises(ValueError): access.setup_required({})

    def test_invalid_setup_sends_no_request(self):
        class NoRequests:
            def request(self, *args, **kwargs): raise AssertionError('unexpected HTTP request')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'key.json'
            with self.assertRaises(ValueError): access.connect(NoRequests(), path, setup=owner('short'))
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_known_safe_validation_details(self):
        cases = [({'path': ['password'], 'code': 'too_small'}, ('password', 'minimum')),
                 ({'path': ['password'], 'message': 'Password must contain at least 1 uppercase letter.'}, ('password', 'password_uppercase')),
                 ({'path': ['password'], 'message': 'Password must contain at least 1 number.'}, ('password', 'password_number')),
                 ({'path': ['email'], 'code': 'invalid_string'}, ('email', 'format')),
                 ({'message': 'Instance owner already setup'}, (None, 'owner_exists')),
                 ({'message': 'Invalid scopes for user role'}, ('scopes', 'invalid_scopes'))]
        for body, expected in cases:
            self.assertEqual(access.safe_problem(json.dumps(body)), expected)

    def test_remote_messages_and_values_never_echoed(self):
        secret = 'DO-NOT-ECHO-SECRET'
        for body in [{'message': secret, 'stack': secret},
                     {'path': [secret], 'code': secret},
                     {'path': ['password'], 'code': {}, 'message': secret, 'input': secret},
                     [secret], secret, None]:
            error = access.HTTPFailure(400, 'owner_setup', json.dumps(body))
            self.assertNotIn(secret, str(error))
            self.assertIn('owner_setup', str(error))
        self.assertEqual(access.safe_problem(b'not JSON'), (None, None))

    def test_http_error_maps_stage_without_leaking_body(self):
        class Response(io.BytesIO): status = 400
        class Opener:
            def open(self, *args, **kwargs):
                return Response(b'{"path":["password"],"code":"too_small","message":"secret-value"}')
        client = access.Client('http://127.0.0.1:16200'); client.opener = Opener()
        for route, stage in [('/rest/owner/setup', 'owner_setup'), ('/rest/login', 'login'),
                             ('/rest/api-keys', 'api_key_create'), ('/rest/settings', 'settings'),
                             ('/api/v1/workflows?limit=1', 'public_api')]:
            with self.assertRaises(access.HTTPFailure) as caught: client.request('POST', route, {})
            self.assertEqual(caught.exception.stage, stage)
            self.assertNotIn('secret-value', str(caught.exception))

    def test_diagnosis_get_only_and_no_local_changes(self):
        calls = []
        class Client:
            def request(self, method, route):
                calls.append((method, route))
                return {'data': {'userManagement': {'showSetupOnFirstLoad': True, 'passwordMinLength': 16}}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'absent/key.json'
            result = access.diagnose(Client(), path)
            self.assertEqual(result['next_step'], 'register_owner')
            self.assertEqual(result['password_min_length'], 16)
            self.assertFalse(result['changes_made'])
            self.assertFalse(path.parent.exists())
        self.assertEqual(calls, [('GET', '/rest/settings')])

    def test_cli_diagnosis_does_not_prompt_or_create_lock(self):
        class Client:
            def __init__(self, url): pass
            def request(self, *args): return {'data': {'userManagement': {'showSetupOnFirstLoad': False}}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'missing/key.json'; output = io.StringIO()
            with patch.object(access, 'Client', Client), patch('sys.argv', ['access', '--diagnose', '--key-file', str(path)]), \
                    patch('builtins.input', side_effect=AssertionError('prompted')), redirect_stdout(output):
                access.main()
            self.assertEqual(json.loads(output.getvalue())['next_step'], 'login_existing_account')
            self.assertFalse(path.parent.exists())

    def test_login_uses_existing_password_and_rejected_key_leaves_no_pending(self):
        calls = []
        class Client:
            url = 'http://127.0.0.1:16200'
            def request(self, method, route, body=None):
                calls.append((method, route, body))
                if route == '/rest/settings': return {'data': {'versionCli': access.VERSION}}
                if route == '/rest/api-keys':
                    raise access.HTTPFailure(400, 'api_key_create', b'{"message":"Invalid scopes for user role"}')
                return {}
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / 'key.json'
            with self.assertRaises(access.HTTPFailure) as caught:
                access.connect(Client(), key, login={'emailOrLdapLoginId': 'owner', 'password': 'legacy'})
            self.assertEqual(caught.exception.stage, 'api_key_create')
            self.assertEqual(calls[0][1], '/rest/login')
            self.assertEqual(calls[0][2]['password'], 'legacy')
            self.assertFalse(key.with_suffix('.pending.json').exists())

    def test_lost_key_response_preserves_pending(self):
        class Client:
            url = 'http://127.0.0.1:16200'
            def request(self, method, route, body=None):
                if route == '/rest/settings': return {'data': {'versionCli': access.VERSION}}
                if route == '/rest/api-keys': raise ValueError('lost response')
                return {}
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / 'key.json'
            with self.assertRaises(ValueError): access.connect(Client(), key, login={'password': 'fixture'})
            self.assertTrue(key.with_suffix('.pending.json').is_file())
            self.assertFalse(key.exists())


if __name__ == '__main__': unittest.main()
