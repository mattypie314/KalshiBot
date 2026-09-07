"""Short-horizon tape helpers for the 15m bot: Bollinger, RSI, ADX.

Computed from 1-minute OHLC. Pure math — no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class TapeReading:
    """Latest BB / RSI / ADX (/ optional MACD) snapshot used to gate 15m entries."""

    rsi: float | None
    adx: float | None
    bb_mid: float | None
    bb_upper: float | None
    bb_lower: float | None
    bb_bandwidth: float | None
    percent_b: float | None
    bars: int
    macd_hist: float | None = None
    macd_line: float | None = None
    macd_signal: float | None = None
    source: str = "1m"

    @property
    def ok(self) -> bool:
        return self.bars > 0


# Defaults tuned for 1m crypto tape on a 15m binary.
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
BB_PERIOD = 20
BB_STD = 2.0
# Bandwidth = (upper - lower) / mid. Sub-0.15% on 1m BTC is dead chop.
BB_TIGHT_BANDWIDTH = 0.0015
ADX_PERIOD = 14
ADX_TREND_MIN = 25.0
# Require MACD histogram to agree with side when 15m signals are present.
MACD_HIST_EPS = 0.0


def sma(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def stdev(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    var = sum((item - mean) ** 2 for item in window) / period
    return math.sqrt(max(var, 0.0))


def rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Wilder RSI on closes. Needs period+1 samples."""
    if period <= 0 or len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for prev, nxt in zip(closes[-(period + 1) : -1], closes[-period:]):
        delta = nxt - prev
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger(
    closes: list[float],
    *,
    period: int = BB_PERIOD,
    num_std: float = BB_STD,
) -> tuple[float, float, float, float, float] | None:
    """Return (mid, upper, lower, bandwidth, %B) or None."""
    mid = sma(closes, period)
    dev = stdev(closes, period)
    if mid is None or dev is None or mid <= 0:
        return None
    upper = mid + num_std * dev
    lower = mid - num_std * dev
    width = upper - lower
    bandwidth = width / mid if mid else 0.0
    last = closes[-1]
    percent_b = (last - lower) / width if width > 1e-12 else 0.5
    return mid, upper, lower, bandwidth, percent_b


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    out: list[float] = []
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for item in values[period:]:
        prev = (prev * (period - 1) + item) / period
        out.append(prev)
    return out


def adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = ADX_PERIOD,
) -> float | None:
    """Wilder ADX. Needs roughly 2*period bars of OHLC."""
    n = min(len(highs), len(lows), len(closes))
    if period <= 0 or n < period + 2:
        return None
    highs = highs[-n:]
    lows = lows[-n:]
    closes = closes[-n:]

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_list: list[float] = []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

    if len(tr_list) < period:
        return None
    smooth_tr = _wilder_smooth(tr_list, period)
    smooth_plus = _wilder_smooth(plus_dm, period)
    smooth_minus = _wilder_smooth(minus_dm, period)
    if not smooth_tr or not smooth_plus or not smooth_minus:
        return None

    dx_vals: list[float] = []
    for tr_v, p_v, m_v in zip(smooth_tr, smooth_plus, smooth_minus):
        if tr_v <= 1e-12:
            dx_vals.append(0.0)
            continue
        plus_di = 100.0 * p_v / tr_v
        minus_di = 100.0 * m_v / tr_v
        denom = plus_di + minus_di
        dx_vals.append(0.0 if denom <= 1e-12 else 100.0 * abs(plus_di - minus_di) / denom)

    if len(dx_vals) < period:
        return None
    adx_series = _wilder_smooth(dx_vals, period)
    return adx_series[-1] if adx_series else None


def read_tape(candles: list[Candle]) -> TapeReading:
    """Build a TapeReading from oldest→newest 1m candles."""
    if not candles:
        return TapeReading(None, None, None, None, None, None, None, 0)
    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    rsi_v = rsi(closes)
    adx_v = adx(highs, lows, closes)
    bb = bollinger(closes)
    if bb is None:
        mid = upper = lower = bandwidth = percent_b = None
    else:
        mid, upper, lower, bandwidth, percent_b = bb
    return TapeReading(
        rsi=rsi_v,
        adx=adx_v,
        bb_mid=mid,
        bb_upper=upper,
        bb_lower=lower,
        bb_bandwidth=bandwidth,
        percent_b=percent_b,
        bars=len(candles),
    )



def tape_from_15m_signals(payload: dict | None) -> TapeReading | None:
    """Map a DataFetcher 15m payload onto TapeReading for the existing gate."""
    if not isinstance(payload, dict) or not payload:
        return None
    try:
        rsi_v = float(payload.get("rsi_14") or 0)
        adx_v = float(payload.get("adx_14") or 0)
        bb_mid = float(payload.get("bb_middle") or 0)
        bb_upper = float(payload.get("bb_upper") or 0)
        bb_lower = float(payload.get("bb_lower") or 0)
        bandwidth = payload.get("bb_bandwidth")
        if bandwidth is None and bb_mid > 0:
            bandwidth = (bb_upper - bb_lower) / bb_mid
        percent_b = payload.get("percent_b")
        if percent_b is None:
            width = bb_upper - bb_lower
            close_px = float(payload.get("close_price") or 0)
            percent_b = (close_px - bb_lower) / width if width > 1e-12 else 0.5
        return TapeReading(
            rsi=rsi_v,
            adx=adx_v,
            bb_mid=bb_mid or None,
            bb_upper=bb_upper or None,
            bb_lower=bb_lower or None,
            bb_bandwidth=float(bandwidth) if bandwidth is not None else None,
            percent_b=float(percent_b) if percent_b is not None else None,
            bars=max(1, int(payload.get("bars") or 1)),
            macd_hist=float(payload.get("macd_hist") or 0),
            macd_line=float(payload.get("macd_line") or 0),
            macd_signal=float(payload.get("macd_signal") or 0),
            source="15m",
        )
    except (TypeError, ValueError):
        return None


def tape_fail_reason(
    side: str,
    tape: TapeReading | None,
    *,
    adx_min: float = ADX_TREND_MIN,
    rsi_hi: float = RSI_OVERBOUGHT,
    rsi_lo: float = RSI_OVERSOLD,
    bb_tight: float = BB_TIGHT_BANDWIDTH,
    macd_eps: float = MACD_HIST_EPS,
) -> str | None:
    """Return a sit reason when the tape disagrees with a Pass, else None.

    Rules (ultra-short crypto binaries):
    - ADX < 25 → range-bound chop, sit.
    - Bollinger bandwidth very tight → low-vol chop, sit (wait for expansion).
    - RSI ≥ 70 against a Yes / RSI ≤ 30 against a No → short-term reversal risk.
    - MACD histogram against the side (when present from 15m CCXT signals) → sit.
    Missing tape data does not fail (fail-open) so a candle outage cannot freeze the bot.
    """
    if tape is None or not tape.ok:
        return None
    if tape.adx is not None and tape.adx < adx_min:
        return f"ADX chop ({tape.adx:.1f}<{adx_min:g})"
    if tape.bb_bandwidth is not None and tape.bb_bandwidth < bb_tight:
        return f"BB tight ({tape.bb_bandwidth:.4f}<{bb_tight})"
    side_l = str(side or "").strip().lower()
    if tape.rsi is not None:
        if side_l in {"yes", "y"} and tape.rsi >= rsi_hi:
            return f"RSI overbought ({tape.rsi:.1f}≥{rsi_hi:g})"
        if side_l in {"no", "n"} and tape.rsi <= rsi_lo:
            return f"RSI oversold ({tape.rsi:.1f}≤{rsi_lo:g})"
    if tape.macd_hist is not None:
        if side_l in {"yes", "y"} and tape.macd_hist < -abs(macd_eps):
            return f"MACD hist against Yes ({tape.macd_hist:.4f})"
        if side_l in {"no", "n"} and tape.macd_hist > abs(macd_eps):
            return f"MACD hist against No ({tape.macd_hist:.4f})"
    return None
