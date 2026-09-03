"""America/New_York clocks. API payloads stay UTC; humans see Eastern."""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_ET_HUMAN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s+"
    r"(?P<ampm>AM|PM)\s+(?P<zone>EDT|EST|ET)$"
)


def use_eastern_process_tz() -> None:
    """Make libc localtime (and logging asctime fallbacks) Eastern."""
    os.environ["TZ"] = "America/New_York"
    if hasattr(time, "tzset"):
        time.tzset()


class EasternFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=ET)
        if datefmt:
            return dt.strftime(datefmt)
        hour = dt.strftime("%I").lstrip("0") or "12"
        zone = dt.tzname() or "ET"
        return f"{dt.strftime('%Y-%m-%d')} {hour}:{dt.strftime('%M:%S %p')} {zone}"


def configure_logging(level: int = logging.INFO) -> None:
    use_eastern_process_tz()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    formatter = EasternFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def to_et(now: datetime | None = None) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ET)


def hour_key(now: datetime | None = None) -> str:
    """Floor to the Eastern clock hour. Matches Kalshi hourly titles (4am EDT, …)."""
    local = to_et(now).replace(minute=0, second=0, microsecond=0)
    return local.isoformat()


def format_et(now: datetime | None = None) -> str:
    local = to_et(now)
    hour = local.strftime("%I").lstrip("0") or "12"
    zone = local.tzname() or "ET"
    return f"{local.strftime('%Y-%m-%d')} {hour}:{local.strftime('%M %p')} {zone}"


def parse_ts(value: object) -> datetime | None:
    """Kalshi/ISO timestamps. Naive values are treated as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_et(value)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        n = float(value)
        if n > 10_000_000_000:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc).astimezone(ET)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return to_et(dt)
    except ValueError:
        pass
    match = _ET_HUMAN.match(text)
    if not match:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("ampm") == "PM":
        hour += 12
    naive = datetime.fromisoformat(
        f"{match.group('date')}T{hour:02d}:{match.group('minute')}:00"
    )
    return naive.replace(tzinfo=ET)


def same_et_hour(value: object, now: datetime | None = None) -> bool:
    parsed = parse_ts(value)
    if parsed is None:
        return False
    return hour_key(parsed) == hour_key(now)


def same_et_day(value: object, now: datetime | None = None) -> bool:
    parsed = parse_ts(value)
    if parsed is None:
        return False
    return to_et(parsed).date() == to_et(now).date()
