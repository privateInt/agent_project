"""Compare Go RFC3339Nano timestamps without version-dependent ISO parsers.

Return (UTC whole second, nanoseconds); never round timestamps through floats
or truncate them to Python datetime's microsecond precision.
"""
from datetime import datetime, timedelta, timezone
import re


_TIMESTAMP = re.compile(
    r'([0-9]{4})-([0-9]{2})-([0-9]{2})T'
    r'([0-9]{2}):([0-9]{2}):([0-9]{2})'
    r'(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})'
)


def timestamp_key(value):
    """Parse the explicit-offset timestamp form emitted by platform APIs.

    Fractional seconds are optional, otherwise 1..9 digits. Date-only/naive
    values, leap seconds, invalid calendar dates and invalid offsets fail.
    """
    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise RuntimeError('invalid timestamp; expected RFC3339 with a timezone and up to 9 fractional digits')
    year, month, day, hour, minute, second = (int(v) for v in match.groups()[:6])
    fraction, offset = match.group(7), match.group(8)
    offset_minutes = 0
    if offset != 'Z':
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        if hours > 23 or minutes > 59:
            raise RuntimeError('invalid timestamp timezone offset')
        offset_minutes = (hours * 60 + minutes) * (1 if offset[0] == '+' else -1)
    try:
        whole_second = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        whole_second -= timedelta(minutes=offset_minutes)
    except (ValueError, OverflowError) as exc:
        raise RuntimeError('invalid timestamp date/time: ' + str(exc)) from None
    nanoseconds = int((fraction or '').ljust(9, '0'))
    return whole_second, nanoseconds
