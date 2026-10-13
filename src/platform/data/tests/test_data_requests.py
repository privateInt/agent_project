"""Durable exact-input recovery, receipt lookup and failure-before-submit gates."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(os.environ.get('DATA_CONSOLE_SOURCE_DIR', Path(__file__).resolve().parents[1] / 'console'))
sys.path.insert(0, str(ROOT))
from data_requests import RequestStore, RequestStoreError, RETENTION_SECONDS
from data_state import ticket

BASE = 'http://data-gateway:8000'
SOURCE = '814748f8-9b33-442f-95f6-0e584760d00a'
OP = '8ced838f-f59e-4054-b90f-e54c84e8853e'


class RequestStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RequestStore(self.tmp.name)
        self.body = {'name': '한글 문서', 'filename': '문서.txt'}
        self.raw = b'\x00original\xff\r\n'
        self.t = ticket('upload', self.body, BASE, self.raw)

    def prepare(self):
        return self.store.prepare(self.t, 'upload', self.body, BASE, self.raw)

    def test_new_store_restores_exact_filename_and_binary_without_original_session(self):
        self.assertIsNone(self.prepare())
        restored = RequestStore(self.tmp.name).load(self.t, BASE)
        self.assertEqual(restored, {'payload': ('upload', self.body, self.raw)})
        self.assertEqual((Path(self.tmp.name) / 'requests.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_changed_inputs_cannot_replace_saved_request(self):
        self.prepare()
        for body, raw in [(dict(self.body, name='changed'), self.raw), (self.body, b'changed')]:
            with self.assertRaises(ValueError): self.store.prepare(self.t, 'upload', body, BASE, raw)
        self.assertEqual(self.store.load(self.t, BASE)['payload'][2], self.raw)

    def test_changed_gateway_or_ticket_cannot_retrieve_saved_file(self):
        self.prepare()
        with self.assertRaises(ValueError): self.store.load(self.t, 'http://another:8000')
        with self.assertRaises(ValueError): self.store.load(dict(self.t, digest='0' * 64), BASE)

    def test_corrupt_disk_payload_fails_closed(self):
        self.prepare()
        with sqlite3.connect(Path(self.tmp.name) / 'requests.sqlite3') as db:
            db.execute('UPDATE requests SET file=?', (b'corrupt',))
        with self.assertRaises(RequestStoreError): self.store.load(self.t, BASE)
        with self.assertRaises(RequestStoreError): self.prepare()

    def test_accepted_receipt_releases_inputs_and_does_not_recreate_pending_payload(self):
        self.prepare()
        accepted = {'document_id': SOURCE}
        self.store.complete(self.t, accepted)
        self.assertEqual(RequestStore(self.tmp.name).load(self.t, BASE), {'accepted': accepted})
        self.assertEqual(self.prepare(), accepted)
        with sqlite3.connect(Path(self.tmp.name) / 'requests.sqlite3') as db:
            self.assertEqual(db.execute('SELECT body,file FROM requests').fetchone(), (None, None))

    def test_unknown_or_expired_ticket_has_no_guessed_payload(self):
        self.assertIsNone(self.store.load(self.t, BASE))
        self.prepare()
        with sqlite3.connect(Path(self.tmp.name) / 'requests.sqlite3') as db:
            db.execute('UPDATE requests SET created=?', (time.time() - RETENTION_SECONDS - 1,))
        self.assertIsNone(self.store.load(self.t, BASE))

    def test_unwritable_location_fails_before_prepare_success(self):
        path = Path(self.tmp.name) / 'a-file'
        path.write_text('occupied')
        with self.assertRaises(RequestStoreError):
            RequestStore(path).prepare(self.t, 'upload', self.body, BASE, self.raw)

    def test_invalid_receipt_cannot_clear_pending_bytes(self):
        self.prepare()
        with self.assertRaises(ValueError): self.store.complete(self.t, {'document_id': 'wrong'})
        self.assertEqual(self.store.load(self.t, BASE)['payload'][2], self.raw)


if __name__ == '__main__':
    unittest.main()
