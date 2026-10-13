"""Prometheus timestamp compatibility and explicit freshness exclusion diagnostics."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from src.infra.observability.ops import manage as infra_manage
from src.ops.platform.ops import discovery_metrics as metrics
from src.ops.platform.ops.discover import discover
from src.ops.platform.ops.discovery_compose import ROLES
from src.ops.platform.tests.test_discovery import Fixture, DockerFixture


class LegacyISO(datetime):
    """Model the old ISO parser's precision limit, not a Python 3.10 interpreter."""
    @classmethod
    def fromisoformat(cls, value):
        fraction = re.search(r'\.(\d+)', value)
        if fraction and len(fraction[1]) not in (3, 6):
            raise ValueError('Invalid isoformat string')
        return super().fromisoformat(value)


class NoISO(datetime):
    @classmethod
    def fromisoformat(cls, value):
        raise AssertionError('Do not use version-dependent ISO parsing for lastScrape')


class DiscoveryTimestampTests(unittest.TestCase):
    now = datetime(2026, 9, 28, 1, 51, 48, tzinfo=timezone.utc)

    def target(self, stamp, health='up'):
        return {'labels': {'job': 'arbitrary-job', 'instance': 'fixture:9999', 'cluster': 'c1'},
                'lastScrape': stamp, 'health': health, 'lastError': 'synthetic-private-sentinel',
                'scrapeUrl': 'http://synthetic-private-sentinel/metrics',
                'discoveredLabels': {'private': 'synthetic-private-sentinel'}}

    def test_original_rejection_reproduced_then_fixed_parser_accepts_nine_digits(self):
        stamp = '2026-09-28T01:51:47.158859873+00:00'
        with self.assertRaises(ValueError): LegacyISO.fromisoformat(stamp)
        with patch.object(infra_manage.dt, 'datetime', LegacyISO):
            eligible, diagnostic = metrics.eligible_targets([self.target(stamp)], self.now)
        self.assertEqual(len(eligible), 1)
        self.assertEqual(diagnostic['targets'][0]['timestamp_status'], 'fresh')
        self.assertEqual(diagnostic['targets'][0]['age_seconds'], .841141)

    def test_all_fraction_widths_and_timezones_without_iso_parser(self):
        with patch.object(infra_manage.dt, 'datetime', NoISO):
            for digits in range(10):
                fraction = '123456789'[:digits]
                for hour, zone in (('01', 'Z'), ('01', '+00:00'), ('10', '+09:00')):
                    stamp = '2026-09-28T' + hour + ':51:47' + ('.' + fraction if fraction else '') + zone
                    with self.subTest(digits=digits, zone=zone):
                        eligible, d = metrics.eligible_targets([self.target(stamp)], self.now)
                        self.assertEqual(len(eligible), 1)
                        self.assertEqual(d['targets'][0]['last_scrape'], stamp)
                        self.assertEqual(d['exclusions'], dict.fromkeys(d['exclusions'], 0))

    def test_negative_timezone_is_not_treated_as_local_time(self):
        eligible, d = metrics.eligible_targets([self.target('2026-09-27T20:21:47.123456789-05:30')], self.now)
        self.assertEqual(len(eligible), 1)
        self.assertEqual(d['targets'][0]['age_seconds'], .876544)

    def test_stale_future_and_90_second_boundary_remain_excluded(self):
        for age, wanted in ((0, 'fresh'), (89.999, 'fresh'), (90, 'stale'), (120, 'stale'), (-1, 'future')):
            stamp = (self.now - timedelta(seconds=age)).isoformat()
            with self.subTest(age=age):
                eligible, d = metrics.eligible_targets([self.target(stamp)], self.now)
                self.assertEqual(len(eligible), int(wanted == 'fresh'))
                self.assertEqual(d['targets'][0]['timestamp_status'], wanted)

    def test_invalid_missing_and_timezone_free_timestamps_are_explicit_and_redacted(self):
        for stamp in (None, '', 'synthetic-private-sentinel', '2026-09-28T01:51:47',
                      '2026-09-28T01:51:47.1234567890Z', '2026-02-30T01:51:47Z',
                      '2026-09-28T01:51:47+24:00', '2026-09-28T01:51:47+00:60'):
            with self.subTest(stamp=stamp):
                eligible, d = metrics.eligible_targets([self.target(stamp)], self.now)
                self.assertFalse(eligible)
                self.assertEqual(d['exclusions']['invalid_timestamp'], 1)
                self.assertIsNone(d['targets'][0]['last_scrape'])
                self.assertIsNone(d['targets'][0]['age_seconds'])
                self.assertNotIn('synthetic-private-sentinel', json.dumps(d))

    def test_health_and_timestamp_reasons_are_reported_independently(self):
        rows = [self.target(self.now.isoformat(), 'down'), self.target('invalid', 'down'),
                self.target(self.now.isoformat(), 'unknown')]
        eligible, d = metrics.eligible_targets(rows, self.now)
        self.assertFalse(eligible)
        self.assertEqual(d['health_up_count'], 0)
        self.assertEqual(d['exclusions']['not_up'], 3)
        self.assertEqual(d['exclusions']['invalid_timestamp'], 1)

    def test_five_targets_nanoseconds_generate_overlay_with_legacy_iso_disabled(self):
        fixture = Fixture(); self.addCleanup(fixture.close)
        original = fixture.response
        def response(path):
            body = original(path)
            if path == '/api/v1/targets?state=active':
                stamp = fixture.timestamp.strftime('%Y-%m-%dT%H:%M:%S.%f') + '873Z'
                for row in body['data']['activeTargets']: row['lastScrape'] = stamp
                extra = deepcopy(body['data']['activeTargets'][0])
                extra['labels'] = {'job': 'alertmanager-other', 'instance': 'alertmanager:9999'}
                body['data']['activeTargets'].append(extra)
            return body
        fixture.response = response
        root = Path('/synthetic/project')
        rows = [{'id': format(n + 1, '064x'), 'state': 'running', 'project': 'renamed-' + role,
                 'service': service, 'working_dir': str(root / folder),
                 'config_files': str(root / folder / 'compose.yml'), 'network_mode': 'bridge',
                 'ports': {'9000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(fixture.server.server_port)}]}}
                for n, (role, (service, folder)) in enumerate(ROLES.items())]
        cfg = {'timeout': 1, 'platforms': {role: {'url': fixture.url} for role in ('model', 'data', 'knowledge', 'agent')}}
        with patch.object(metrics, 'datetime', NoISO), patch.object(infra_manage.dt, 'datetime', NoISO):
            result = discover(cfg, root, DockerFixture(rows))
        self.assertEqual(result['result'], 'discovered', result)
        self.assertEqual(result['metrics']['fresh_healthy_target_count'], 5)
        self.assertEqual(result['metrics']['target_diagnostics']['health_up_count'], 5)
        self.assertTrue(result['connection_overlay'])
        self.assertTrue(all(method == 'GET' and auth is None for method, path, auth in fixture.calls))
        self.assertNotIn('synthetic-private-sentinel', json.dumps(result))


if __name__ == '__main__': unittest.main()
