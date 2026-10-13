"""Real loopback transport regression; no production n8n, Docker or GPU."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.workflows import EngineHTTP, canonical, signed_headers


KEY = 'fixture-private-key'
PRIVATE = 'PRIVATE_RESPONSE_SENTINEL'
WEBHOOK = '/webhook/document-qa-1892c2d1-c708-5e99-8a35-6a4c9976fe97'


@contextmanager
def engine(status=200, raw=None, content_type='application/json', strict_get=False):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            seen.append({'method': self.command, 'path': self.path, 'body': body,
                         'content_type': self.headers.get('Content-Type'),
                         'accept': self.headers.get('Accept'),
                         'key': self.headers.get('X-N8N-API-KEY'),
                         'authorization': self.headers.get('Authorization')})
            code = 400 if strict_get and self.command == 'GET' and self.headers.get('Content-Type') else status
            value = {'data': {}} if self.path == '/rest/settings' else {'data': []}
            payload = raw if raw is not None else json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            if 300 <= code < 400:
                self.send_header('Location', '/api/v1/workflows/redirect')
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = handle_request
        do_POST = handle_request
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield EngineHTTP('http://127.0.0.1:' + str(server.server_port), KEY), seen
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


class EngineHTTPTests(unittest.TestCase):
    def test_bodyless_GET_matches_successful_metadata_reader(self):
        with engine(strict_get=True) as (api, seen):
            self.assertEqual(api.request('GET', '/rest/settings'), {'data': {}})
            self.assertEqual(api.request('GET', '/api/v1/workflows?limit=100'), {'data': []})
            self.assertEqual([row['http_status'] for row in api.observations], [200, 200])
            self.assertTrue(all(row['response_format'] == 'json_object' for row in api.observations))
        self.assertEqual([row['key'] for row in seen], [None, KEY])
        self.assertTrue(all(row['body'] == b'' and row['content_type'] is None and
                            row['accept'] == 'application/json' for row in seen))

    def test_original_empty_JSON_header_is_rejected_by_strict_fixture(self):
        # This fixture models the header incompatibility, not a proven n8n error.
        with engine(strict_get=True) as (api, seen), self.assertRaises(UploadError):
            api.request('GET', '/rest/settings', headers={'Content-Type': 'application/json'})
        self.assertEqual(api.observations[0]['http_status'], 400)
        self.assertEqual(len(seen), 1)

    def test_API_POST_keeps_canonical_JSON_and_API_key(self):
        body = {'name': '한글', 'value': False}
        with engine(status=201) as (api, seen):
            api.request('POST', '/api/v1/credentials', body)
        self.assertEqual(seen[0]['body'], canonical(body).encode())
        self.assertEqual(seen[0]['content_type'], 'application/json')
        self.assertEqual(seen[0]['key'], KEY)
        self.assertEqual(api.observations[0]['http_status'], 201)

    def test_webhook_POST_keeps_signature_without_API_key(self):
        body = {'handoff_id': 'fixture-handoff', 'text': 'CPU only'}
        headers = signed_headers('fixture-signing-secret', WEBHOOK, body)
        with engine() as (api, seen):
            api.request('POST', WEBHOOK, body, headers)
        self.assertEqual(seen[0]['body'], canonical(body).encode())
        self.assertEqual(seen[0]['content_type'], 'application/json')
        self.assertEqual(seen[0]['authorization'], headers['Authorization'])
        self.assertIsNone(seen[0]['key'])
        self.assertNotIn(headers['Authorization'], json.dumps(api.observations))

    def test_HTTP_errors_keep_exact_status_without_reading_private_body(self):
        for status in (400, 404, 415, 422, 500, 401, 403):
            with self.subTest(status=status), engine(status, PRIVATE.encode()) as (api, seen):
                with self.assertRaises(UploadError) as error:
                    api.request('GET', '/rest/settings')
                expected = 'ENGINE_AUTH_REQUIRED' if status in (401, 403) else 'ENGINE_HTTP_UNCONFIRMED'
                self.assertEqual(error.exception.code, expected)
                self.assertEqual(api.observations[0]['http_status'], status)
                self.assertEqual(api.observations[0]['error_code'], expected)
                self.assertNotIn(PRIVATE, json.dumps(api.observations))
                self.assertNotIn(KEY, json.dumps(api.observations))
                self.assertEqual(len(seen), 1)

    def test_redirect_is_not_followed_and_status_is_retained(self):
        with engine(status=307) as (api, seen), self.assertRaises(UploadError) as error:
            api.request('GET', '/rest/settings')
        self.assertEqual(error.exception.code, 'ENGINE_REDIRECT_REFUSED')
        self.assertEqual(api.observations[0]['http_status'], 307)
        self.assertEqual(len(seen), 1)

    def test_invalid_JSON_and_media_type_retain_HTTP_status(self):
        for raw, content_type in ((b'{"data":1,"data":2}', 'application/json'),
                                  (b'{"data":[]}', 'text/html')):
            with self.subTest(content_type=content_type), engine(raw=raw, content_type=content_type) as (api, _):
                with self.assertRaises(UploadError) as error:
                    api.request('GET', '/rest/settings')
                self.assertEqual(error.exception.code, 'ENGINE_RESPONSE_INVALID')
                self.assertEqual(api.observations[0]['http_status'], 200)
                self.assertEqual(api.observations[0]['response_format'], 'unavailable')

    def test_size_limit_keeps_status_and_does_not_store_response(self):
        with engine(raw=b'x' * ((2 << 20) + 1)) as (api, _), self.assertRaises(UploadError) as error:
            api.request('GET', '/rest/settings')
        self.assertEqual(error.exception.code, 'ENGINE_RESPONSE_TOO_LARGE')
        self.assertEqual(api.observations[0]['http_status'], 200)
        self.assertLess(len(json.dumps(api.observations)), 300)

    def test_observations_omit_cursor_and_response_values(self):
        with engine(raw=json.dumps({'data': [{'name': PRIVATE}]}).encode()) as (api, _):
            api.request('GET', '/api/v1/workflows?limit=100&cursor=PRIVATE_CURSOR')
        value = json.dumps(api.observations)
        self.assertEqual(api.observations[0]['route'], '/api/v1/workflows')
        self.assertNotIn('PRIVATE', value)

    def test_connection_failure_is_sanitized_and_not_retried(self):
        api = EngineHTTP('http://127.0.0.1:1', KEY)
        with patch('src.apps.document_qa.workflows.build_opener') as opener:
            opener.return_value.open.side_effect = URLError(PRIVATE)
            with self.assertRaises(UploadError) as error:
                api.request('GET', '/rest/settings')
            self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertEqual(error.exception.code, 'ENGINE_OUTCOME_UNKNOWN')
        self.assertIsNone(api.observations[0]['http_status'])
        self.assertNotIn(PRIVATE, json.dumps(api.observations))


if __name__ == '__main__':
    unittest.main()
