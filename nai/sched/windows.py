"""SCH-8: scheduling windows and the one-off --until time. All times are local wall-clock time."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_RANGE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(s: str) -> tuple[int, int]:
    """'22:00-06:00' -> (minutes since midnight at start, at end). A window may cross midnight."""
    m = _RANGE.match(s)
    if not m:
        raise ValueError(f"bad window {s!r}; expected HH:MM-HH:MM")
    h1, m1, h2, m2 = map(int, m.groups())
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
        raise ValueError(f"bad window {s!r}: hour or minute out of range")
    return h1 * 60 + m1, h2 * 60 + m2


def in_windows(windows: list[str], now: datetime) -> bool:
    """True when `now` falls in any window; no windows means always. start == end means the whole day."""
    if not windows:
        return True
    cur = now.hour * 60 + now.minute
    for w in windows:
        a, b = parse_window(w)
        if a == b or (a < b and a <= cur < b) or (a > b and (cur >= a or cur < b)):
            return True
    return False


def parse_until(s: str, now: datetime) -> datetime:
    """'HH:MM' -> the next such time after `now`; or an ISO date-time such as 2026-11-17T06:30."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", s)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)
    try:
        return datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"bad --until value {s!r}; use HH:MM or an ISO date-time") from e
