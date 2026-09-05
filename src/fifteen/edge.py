"""15m edge loop: first 2–4 minutes of each ET window, Pass/Fail vs mid, one idea.

Maker (last 3 min 74–93¢) and the hourly scanner stay separate.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

ENTRY_OFFSETS = frozenset({2, 3, 4})
MIN_EDGE = 0.04
MIN_TIME_SECONDS = 8 * 60
DECIDED_SIGMA = 2.0
DECIDED_YES = 0.96
DECIDED_NO = 0.04
SIZE_TYPICAL = 0.04
SIZE_MIN = 0.03
SIZE_MAX = 0.05
HALF_SIGMA = 0.5
SESSION_STOP_LOSSES = 3
CPI_WINDOW_MINUTES = 15
FOMC_WINDOW_MINUTES = 45

CPI_DATES = frozenset(
    {
        (2026, 1, 14),
        (2026, 2, 12),
        (2026, 3, 12),
        (2026, 4, 10),
        (2026, 5, 13),
        (2026, 6, 11),
        (2026, 7, 15),
        (2026, 8, 12),
        (2026, 9, 11),
        (2026, 10, 14),
        (2026, 11, 10),
        (2026, 12, 10),
    }
)
FOMC_DATES = frozenset(
    {
        (2026, 1, 28),
        (2026, 3, 18),
        (2026, 4, 29),
        (2026, 6, 17),
        (2026, 7, 29),
        (2026, 9, 16),
        (2026, 10, 28),
        (2026, 12, 9),
    }
)


@dataclass(frozen=True)
class FifteenDecision:
    passed: bool
    line: str
    side: str
    join_price: float
    model_yes: float
    model_prob: float
    mid: float
    edge: float
    spread: float
    fail_reason: str | None = None


def join_price(side: str, yes_bid: float, yes_ask: float) -> float:
    """Yes joins the Yes bid; No joins the Yes ask (maker buy of No)."""
    if str(side or "").lower() in {"yes", "y"}:
        return float(yes_bid)
    return float(yes_ask)


def now_et(now: datetime | None = None) -> datetime:
    stamp = now or datetime.now(ET)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ET)
    return stamp.astimezone(ET)


def fifteen_window_start(now: datetime | None = None) -> datetime:
    local = now_et(now)
    minute = (local.minute // 15) * 15
    return local.replace(minute=minute, second=0, microsecond=0)


def fifteen_window_id(now: datetime | None = None) -> str:
    return fifteen_window_start(now).isoformat()


def in_fifteen_entry_window(now: datetime | None = None) -> bool:
    return now_et(now).minute % 15 in ENTRY_OFFSETS


def in_fifteen_settlement(now: datetime | None = None) -> bool:
    return now_et(now).minute % 15 == 0


def fifteen_session_date(now: datetime | None = None) -> str:
    return now_et(now).date().isoformat()


def next_et_midnight(now: datetime | None = None) -> datetime:
    local = now_et(now)
    return (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def revenge_until_after_loss(now: datetime | None = None) -> datetime:
    """Skip the next 15m window after a losing 15m ticket."""
    return fifteen_window_start(now) + timedelta(minutes=30)


def _parse_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def in_fifteen_revenge(state: dict, now: datetime | None = None) -> bool:
    until = _parse_ts(state.get("fifteen_revenge_until"))
    if until is None:
        return False
    return now_et(now) < until.astimezone(ET)


def fifteen_stopped(state: dict, now: datetime | None = None) -> bool:
    until = _parse_ts(state.get("fifteen_stopped_until"))
    if until is None:
        return False
    return now_et(now) < until.astimezone(ET)


def fifteen_working(state: dict, now: datetime | None = None) -> bool:
    """True if a 15m ticket or rest is already working this window."""
    wid = fifteen_window_id(now)
    for ticket in state.get("tickets") or []:
        if ticket.get("status") != "open" or ticket.get("loop") != "fifteen":
            continue
        if ticket.get("window_id") in (None, "", wid):
            return True
    for rest in state.get("rests") or []:
        if rest.get("status") != "open" or rest.get("loop") != "fifteen":
            continue
        if rest.get("window_id") in (None, "", wid):
            return True
    return False


def strike_decided(model_yes: float, sigma: float) -> bool:
    return abs(sigma) >= DECIDED_SIGMA or model_yes >= DECIDED_YES or model_yes <= DECIDED_NO


def half_sigma_move(
    spot_now: float,
    spot_then: float,
    hour_vol: float,
    threshold: float = HALF_SIGMA,
) -> bool:
    if spot_now <= 0 or spot_then <= 0 or hour_vol <= 0:
        return False
    return abs(math.log(spot_now / spot_then)) / hour_vol >= threshold


def fifteen_stake(total_value: float, room: float) -> float:
    if total_value <= 0 or room <= 0:
        return 0.0
    frac = max(SIZE_MIN, min(SIZE_MAX, SIZE_TYPICAL))
    return min(frac * total_value, room)


def enough_room(room: float, total_value: float) -> bool:
    if total_value <= 0:
        return False
    return room + 1e-9 >= SIZE_MIN * total_value


def _cents(value: float) -> str:
    return f"{int(round(100 * value))}¢"


def news_blackout(now: datetime | None = None) -> str | None:
    """CPI / FOMC calendar, plus NEWS_BLACKOUT=1 for a headline candle."""
    flag = os.environ.get("NEWS_BLACKOUT", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return "NEWS_BLACKOUT"
    local = now_et(now)
    key = (local.year, local.month, local.day)
    if key in CPI_DATES:
        start = local.replace(hour=8, minute=30, second=0, microsecond=0) - timedelta(
            minutes=CPI_WINDOW_MINUTES
        )
        end = local.replace(hour=8, minute=30, second=0, microsecond=0) + timedelta(
            minutes=CPI_WINDOW_MINUTES
        )
        if start <= local <= end:
            return "CPI"
    if key in FOMC_DATES:
        start = local.replace(hour=14, minute=0, second=0, microsecond=0) - timedelta(
            minutes=FOMC_WINDOW_MINUTES
        )
        end = local.replace(hour=14, minute=0, second=0, microsecond=0) + timedelta(
            minutes=FOMC_WINDOW_MINUTES
        )
        if start <= local <= end:
            return "FOMC"
    return None


def pass_fail(
    *,
    model_yes: float,
    yes_bid: float,
    yes_ask: float,
    secs_left: float,
    sigma: float,
    news: str | None = None,
) -> FifteenDecision:
    """Pass/Fail vs the live mid. Fail means skip — do not scalp it."""
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask < yes_bid:
        return FifteenDecision(
            passed=False,
            line="FAIL unusable book",
            side="yes",
            join_price=max(yes_bid, 0.0),
            model_yes=model_yes,
            model_prob=model_yes,
            mid=0.5,
            edge=0.0,
            spread=max(0.0, yes_ask - yes_bid),
            fail_reason="unusable book",
        )
    mid = (yes_bid + yes_ask) / 2.0
    spread = yes_ask - yes_bid
    edge = model_yes - mid
    if edge >= 0:
        side = "yes"
        model_prob = model_yes
    else:
        side = "no"
        model_prob = 1.0 - model_yes
    abs_edge = abs(edge)
    join = join_price(side, yes_bid, yes_ask)
    fair_vs_mid = f"fair {model_yes:.2f} vs mid {mid:.2f}"

    if news:
        return FifteenDecision(
            passed=False,
            line=f"FAIL {fair_vs_mid} · news candle ({news})",
            side=side,
            join_price=join,
            model_yes=model_yes,
            model_prob=model_prob,
            mid=mid,
            edge=edge,
            spread=spread,
            fail_reason=f"news candle ({news})",
        )
    if abs_edge < MIN_EDGE - 1e-12:
        return FifteenDecision(
            passed=False,
            line=f"FAIL {fair_vs_mid} · only {_cents(abs_edge)}",
            side=side,
            join_price=join,
            model_yes=model_yes,
            model_prob=model_prob,
            mid=mid,
            edge=edge,
            spread=spread,
            fail_reason="within 4 cents",
        )
    if spread > abs_edge + 1e-12:
        return FifteenDecision(
            passed=False,
            line=f"FAIL {fair_vs_mid} · spread {_cents(spread)} > edge {_cents(abs_edge)}",
            side=side,
            join_price=join,
            model_yes=model_yes,
            model_prob=model_prob,
            mid=mid,
            edge=edge,
            spread=spread,
            fail_reason="spread wider than edge",
        )
    if secs_left < MIN_TIME_SECONDS and not strike_decided(model_yes, sigma):
        return FifteenDecision(
            passed=False,
            line=f"FAIL {fair_vs_mid} · {secs_left / 60:.0f}m left (need 8m unless decided)",
            side=side,
            join_price=join,
            model_yes=model_yes,
            model_prob=model_prob,
            mid=mid,
            edge=edge,
            spread=spread,
            fail_reason="under 8 minutes",
        )
    sign = "+" if edge >= 0 else ""
    return FifteenDecision(
        passed=True,
        line=f"PASS {fair_vs_mid} · edge {sign}{_cents(edge)}",
        side=side,
        join_price=join,
        model_yes=model_yes,
        model_prob=model_prob,
        mid=mid,
        edge=edge,
        spread=spread,
    )


def record_fifteen_result(state: dict, pnl: float, now: datetime | None = None) -> str | None:
    """Update 15m streak / revenge / session stop after a ticket result.

    Returns a tell-Matt message when the 15m loop stops for the rest of the ET day.
    """
    local = now_et(now)
    today = fifteen_session_date(local)
    if state.get("fifteen_session_date") != today:
        state["fifteen_loss_streak"] = 0
        state["fifteen_session_date"] = today
    if pnl < 0:
        state["fifteen_loss_streak"] = int(state.get("fifteen_loss_streak") or 0) + 1
        state["fifteen_revenge_until"] = revenge_until_after_loss(local).isoformat()
        if int(state["fifteen_loss_streak"]) >= SESSION_STOP_LOSSES:
            state["fifteen_stopped_until"] = next_et_midnight(local).isoformat()
            return (
                f"Three 15m losses in a row this session. "
                f"15m loop stopped until {next_et_midnight(local).strftime('%Y-%m-%d %H:%M')} ET."
            )
        return None
    state["fifteen_loss_streak"] = 0
    return None
