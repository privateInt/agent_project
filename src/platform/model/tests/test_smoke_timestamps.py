from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import smoke_timestamps as timestamps


class SmokeTimestampTests(unittest.TestCase):
    def test_reported_five_digit_fraction_preserves_value(self):
        expected = (datetime(2026, 9, 9, 5, 44, 21, tzinfo=timezone.utc), 928660000)
        for suffix in ('Z', '+00:00'):
            with self.subTest(suffix=suffix):
                self.assertEqual(timestamps.timestamp_key('2026-09-09T05:44:21.92866' + suffix), expected)

    def test_all_fraction_widths_without_fromisoformat(self):
        class NoISOParser(datetime):
            @classmethod
            def fromisoformat(cls, value):
                raise AssertionError('version-dependent parser must not be called')

        with patch.object(timestamps, 'datetime', NoISOParser):
            for digits in range(10):
                fractional = '123456789'[:digits]
                for zone in ('Z', '+00:00', '+09:00'):
                    with self.subTest(digits=digits, zone=zone):
                        hour = '14' if zone == '+09:00' else '05'
                        value = '2026-09-09T' + hour + ':44:21' + ('.' + fractional if digits else '') + zone
                        expected_ns = int(fractional.ljust(9, '0'))
                        self.assertEqual(timestamps.timestamp_key(value),
                                         (datetime(2026, 9, 9, 5, 44, 21, tzinfo=timezone.utc), expected_ns))

    def test_offsets_and_trailing_zeroes_compare_as_same_instant(self):
        values = ('2026-09-09T00:00:00.1Z', '2026-09-09T00:00:00.100000000+00:00',
                  '2026-09-08T19:00:00.100-05:00', '2026-09-09T09:00:00.10+09:00')
        self.assertEqual(len(set(map(timestamps.timestamp_key, values))), 1)

    def test_nanosecond_order_and_second_boundary_are_exact(self):
        values = ('2026-09-09T00:00:00.000000001Z', '2026-09-09T00:00:00.000000002Z',
                  '2026-09-09T00:00:00.999999999Z', '2026-09-09T00:00:01Z')
        keys = list(map(timestamps.timestamp_key, values))
        self.assertTrue(all(a < b for a, b in zip(keys, keys[1:])))

    def test_invalid_or_naive_values_are_rejected(self):
        for value in (None, 123, '', '2026-09-09', '2026-09-09T05:44:21',
                      '2026-09-09T05:44:21.Z', '2026-09-09T05:44:21.1234567890Z',
                      '2026-02-30T05:44:21Z', '2026-09-09T25:44:21Z', '2026-09-09T05:44:60Z',
                      '2026-09-09T05:44:21+24:00', '2026-09-09T05:44:21+00:60',
                      '2026-09-09T05:44:21Zextra'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                timestamps.timestamp_key(value)
