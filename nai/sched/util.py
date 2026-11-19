"""Small helpers shared by the scheduler modules."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any


class SystemClock:
    """Injectable clock: tests replace it so windows, caps and backoff are deterministic."""

    def now(self) -> datetime:          # aware UTC, for stored timestamps
        return datetime.now(timezone.utc)

    def local(self) -> datetime:        # naive local time, for scheduling windows (SCH-8)
        return datetime.now()

    def mono(self) -> float:            # monotonic seconds, for caps, intervals and backoff
        return time.monotonic()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def as_obj(v: Any, default: Any = None) -> Any:
    """JSON columns may arrive parsed or as text; return the parsed value."""
    if v is None:
        return default
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v


def is_invalid_transition(e: BaseException) -> bool:
    """nai.store.InvalidTransition, matched by name so this package does not import the store module."""
    return type(e).__name__ == "InvalidTransition"


def is_transient(e: BaseException) -> bool:
    """Model-server-unreachable errors: the job is put back in the queue instead of failing."""
    return type(e).__name__ in {"APIConnectionError", "APITimeoutError", "ConnectError", "ConnectionRefusedError",
                                "ConnectTimeout", "ServerUnavailable"}
