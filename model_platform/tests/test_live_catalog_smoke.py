import copy
import unittest
from urllib.parse import parse_qs, urlsplit

import live_catalog_smoke as smoke


def item(collection, n):
    row = {smoke.COLLECTIONS[collection]: f'10000000-0000-0000-0000-{n:012}',
           'created_at': '2026-09-09T08:00:00.123456Z'}
    if collection == 'deployments':
        row.update(runtime_state='stopped', desired_state='stopped', endpoint='')
    return row


class ReadOnlyAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, path):
        if method != 'GET':
            raise AssertionError('smoke attempted a mutation')
        self.calls.append(path)
        parsed = urlsplit(path)
        q = parse_qs(parsed.query)
        collection = parsed.path.rsplit('/', 1)[-1]
        invalid = q.get('cursor') == ['invalid!'] or q.get('limit') in (['0'], ['101']) or 'limt' in q
        invalid |= len(q.get('q', [''])[0]) > 200
        invalid |= q.get('cursor') == ['next'] and bool(set(q) - {'limit', 'cursor'})
        if invalid:
            raise smoke.APIError(400, 'invalid query')
        if collection == 'datasets':
            return {'datasets': [], 'next_after_id': ''}
        if 'q' in q:
            return {'items': [], 'next_cursor': ''}
        if q.get('cursor') == ['next']:
            return {'items': [item(collection, 1)], 'next_cursor': ''}
        return {'items': [item(collection, 3), item(collection, 2)], 'next_cursor': 'next'}


class CatalogSmokeTest(unittest.TestCase):
    def test_exercise_is_read_only_and_observes_next_pages(self):
        client, record = ReadOnlyAPI(), {}
        smoke.exercise(client, record, lambda _: None)
        self.assertTrue(record['passed'])
        self.assertEqual(len(record['collections']), 5)
        self.assertTrue(all(v['pagination_observed'] for v in record['collections'].values()))

    def test_duplicate_or_reversed_page_is_rejected(self):
        for rows in ([item('models', 2), item('models', 2)],
                     [item('models', 1), item('models', 2)]):
            with self.assertRaises(RuntimeError):
                smoke.check_page({'items': rows, 'next_cursor': ''}, 'models', 2, set())

    def test_invalid_envelopes_are_rejected(self):
        for page in ({'items': None, 'next_cursor': ''}, {'items': [], 'next_cursor': 'next'},
                     {'items': [item('models', 1)]}):
            with self.assertRaises(RuntimeError):
                smoke.check_page(page, 'models', 2, set())

    def test_variable_fractions_across_pages_preserve_order(self):
        newer, older = item('model-versions', 1), item('model-versions', 2)
        newer['created_at'] = '2026-09-09T05:44:21.92866Z'
        older['created_at'] = '2026-09-09T05:44:21.928659Z'
        seen = set()
        previous = smoke.check_page({'items': [newer], 'next_cursor': 'next'}, 'model-versions', 1, seen)
        smoke.check_page({'items': [older], 'next_cursor': ''}, 'model-versions', 1, seen, previous)

    def test_submicrosecond_order_is_not_replaced_by_uuid_order(self):
        newer, older = item('models', 1), item('models', 2)
        newer['created_at'] = '2026-09-09T05:44:21.000000002Z'
        older['created_at'] = '2026-09-09T05:44:21.000000001Z'
        smoke.check_page({'items': [newer, older], 'next_cursor': ''}, 'models', 2, set())
        with self.assertRaisesRegex(RuntimeError, 'order changed'):
            smoke.check_page({'items': [older, newer], 'next_cursor': ''}, 'models', 2, set())

    def test_equivalent_fraction_widths_still_use_uuid_tie_break(self):
        newer, older = item('models', 2), item('models', 1)
        newer['created_at'] = '2026-09-09T05:44:21.9Z'
        older['created_at'] = '2026-09-09T05:44:21.900000Z'
        smoke.check_page({'items': [newer, older], 'next_cursor': ''}, 'models', 2, set())
        with self.assertRaisesRegex(RuntimeError, 'order changed'):
            smoke.check_page({'items': [older, newer], 'next_cursor': ''}, 'models', 2, set())

    def test_public_summary_rejects_secrets_results_and_stale_endpoints(self):
        for collection, field, value in [('operations', 'dispatch_token', 'secret'),
                                         ('training-runs', 'result_json', '{}'),
                                         ('deployments', 'endpoint', 'http://stale/v1')]:
            row = copy.deepcopy(item(collection, 1))
            row[field] = value
            with self.assertRaises(RuntimeError):
                smoke.check_page({'items': [row], 'next_cursor': ''}, collection, 2, set())

    def test_empty_collection_does_not_claim_pagination(self):
        class Empty(ReadOnlyAPI):
            def call(self, method, path):
                response = super().call(method, path)
                if 'items' in response:
                    return {'items': [], 'next_cursor': ''}
                return response
        record = {}
        smoke.exercise(Empty(), record, lambda _: None)
        self.assertTrue(record['passed'])
        self.assertFalse(any(v['pagination_observed'] for v in record['collections'].values()))


if __name__ == '__main__':
    unittest.main()
