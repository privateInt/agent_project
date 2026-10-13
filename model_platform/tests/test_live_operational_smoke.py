import copy
import unittest
from unittest.mock import patch
import live_operational_smoke as smoke


class OperationalSmokeTests(unittest.TestCase):
    def pages(self, api, path):
        if path == '/health':
            return {'status': 'ok'}
        collection = path.split('/')[2].split('?')[0]
        key = smoke.COLLECTIONS[collection]
        row = {key: '11111111-1111-4111-8111-111111111111', 'name': 'fixture', 'updated_at': 'volatile'}
        if collection in ('operations', 'training-runs'):
            row['status'] = 'succeeded'
        if collection == 'deployments':
            row['status'] = 'stopped'
        if collection == 'model-versions':
            row['state'] = 'ready'
        return {'datasets' if collection == 'datasets' else 'items': [row],
                'next_after_id' if collection == 'datasets' else 'next_cursor': ''}

    def test_capture_compares_durable_fields(self):
        with patch.object(smoke, 'api_get', side_effect=self.pages):
            before = smoke.capture('http://127.0.0.1:13000')
            after = smoke.capture('http://127.0.0.1:13000')
        smoke.compare(before, after)
        self.assertNotIn('updated_at', str(before))

    def test_status_and_missing_history_are_not_success(self):
        with patch.object(smoke, 'api_get', side_effect=self.pages):
            before = smoke.capture('http://127.0.0.1:13000')
        for change in ('status', 'missing'):
            after = copy.deepcopy(before)
            if change == 'status':
                next(iter(after['collections']['operations'].values()))['status'] = 'failed'
            else:
                after['collections']['training-runs'].clear()
            with self.assertRaises(smoke.OpsError):
                smoke.compare(before, after)

    def test_running_operation_blocks_snapshot(self):
        def running(api, path):
            result = self.pages(api, path)
            if path.startswith('/api/operations?'):
                result['items'][0]['status'] = 'running'
            return result
        with patch.object(smoke, 'api_get', side_effect=running):
            with self.assertRaisesRegex(smoke.OpsError, 'active work'):
                smoke.capture('http://127.0.0.1:13000')

    def test_repeated_cursor_and_duplicate_rows_fail(self):
        def repeat(api, path):
            result = self.pages(api, path)
            if path != '/health':
                result['next_cursor'] = 'repeat'
            return result
        with patch.object(smoke, 'api_get', side_effect=repeat):
            with self.assertRaises(smoke.OpsError):
                smoke.capture('http://127.0.0.1:13000')


if __name__ == '__main__':
    unittest.main()
