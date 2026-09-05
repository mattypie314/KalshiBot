"""Tape regime for the 15m paper/scan path: TREND, CHOP, or UNKNOWN.

Chop = weak trend (low ADX) and a tight Bollinger band. Computed in Python
from recent OHLC (or closes). No LLM. Stdlib only — pandas is not a dep.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

CHOP = "CHOP"
TREND = "TREND"
UNKNOWN = "UNKNOWN"
CHOP_VETO_PHRASE = "PASS but chop veto"

ADX_PERIOD = 14
BB_PERIOD = 20
BB_K = 2.0
ADX_CHOP = 20.0
ADX_TREND = 25.0
# (upper - lower) / middle. ~1.2% on 1m bars is a quiet tape.
BB_CHOP = 0.012


@dataclass(frozen=True)
class Candle:
    high: float
    low: float
    close: float
    open: float = 0.0


@dataclass(frozen=True)
class Regime:
    label: str
    adx: float | None = None
    bb_bandwidth: float | None = None
    reason: str = ""

    @property
    def is_chop(self) -> bool:
        return self.label == CHOP

    @property
    def summary(self) -> str:
        bits = [self.label]
        if self.adx is not None:
            bits.append(f"ADX {self.adx:.1f}")
        if self.bb_bandwidth is not None:
            bits.append(f"BB {self.bb_bandwidth:.3f}")
        if self.reason:
            bits.append(self.reason)
        return " ".join(bits)


def candles_from_closes(closes: Sequence[float]) -> list[Candle]:
    """Close-only bars: high/low are the adjacent-close range."""
    out: list[Candle] = []
    prev: float | None = None
    for raw in closes:
        close = float(raw)
        if close <= 0:
            continue
        if prev is None:
            high = low = close
        else:
            high = max(prev, close)
            low = min(prev, close)
        out.append(Candle(high=high, low=low, close=close, open=prev or close))
        prev = close
    return out


def _as_candles(bars: Sequence[Candle] | Sequence[float] | None) -> list[Candle]:
    if not bars:
        return []
    first = bars[0]
    if isinstance(first, Candle):
        return [bar for bar in bars if isinstance(bar, Candle) and bar.close > 0]
    if isinstance(first, (int, float)):
        return candles_from_closes([float(x) for x in bars])  # type: ignore[arg-type]
    out: list[Candle] = []
    for item in bars:
        high = float(getattr(item, "high", 0.0) or 0.0)
        low = float(getattr(item, "low", 0.0) or 0.0)
        close = float(getattr(item, "close", 0.0) or 0.0)
        opened = float(getattr(item, "open", 0.0) or 0.0)
        if close > 0 and high > 0 and low > 0:
            out.append(Candle(high=high, low=low, close=close, open=opened))
    return out


def _wilder_avg(values: Sequence[float], period: int) -> list[float]:
    if period <= 0 or len(values) < period:
        return []
    avgs = [sum(values[:period]) / period]
    for value in values[period:]:
        avgs.append((avgs[-1] * (period - 1) + value) / period)
    return avgs


def adx_from_candles(bars: Sequence[Candle], period: int = ADX_PERIOD) -> float | None:
    """Wilder ADX. Needs about 2*period+1 bars. Last value only."""
    if period < 2 or len(bars) < 2 * period + 1:
        return None
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for prev, bar in zip(bars, bars[1:]):
        high, low, close = bar.high, bar.low, bar.close
        tr = max(high - low, abs(high - prev.close), abs(low - prev.close))
        up = high - prev.high
        down = prev.low - low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(tr, 0.0))
    atr = _wilder_avg(trs, period)
    sm_plus = _wilder_avg(plus_dm, period)
    sm_minus = _wilder_avg(minus_dm, period)
    if not atr or len(atr) != len(sm_plus) or len(atr) != len(sm_minus):
        return None
    dxs: list[float] = []
    for atr_i, plus_i, minus_i in zip(atr, sm_plus, sm_minus):
        if atr_i <= 0:
            dxs.append(0.0)
            continue
        pdi = 100.0 * plus_i / atr_i
        mdi = 100.0 * minus_i / atr_i
        denom = pdi + mdi
        dxs.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
    adxs = _wilder_avg(dxs, period)
    if not adxs:
        return None
    return max(0.0, min(100.0, adxs[-1]))


def bb_bandwidth(
    closes: Sequence[float],
    period: int = BB_PERIOD,
    k: float = BB_K,
) -> float | None:
    """(upper - lower) / middle using the last `period` closes."""
    if period < 2 or len(closes) < period:
        return None
    window = [float(x) for x in closes[-period:] if float(x) > 0]
    if len(window) < period:
        return None
    mean = sum(window) / period
    if mean <= 0:
        return None
    var = sum((item - mean) ** 2 for item in window) / period
    std = math.sqrt(max(var, 0.0))
    return (2.0 * k * std) / mean


def classify_regime(
    bars: Sequence[Candle] | Sequence[float] | None,
    *,
    adx_period: int = ADX_PERIOD,
    bb_period: int = BB_PERIOD,
    bb_k: float = BB_K,
    adx_chop: float = ADX_CHOP,
    adx_trend: float = ADX_TREND,
    bb_chop: float = BB_CHOP,
) -> Regime:
    """TREND | CHOP | UNKNOWN. Chop only when low ADX *and* tight bands."""
    candles = _as_candles(bars)
    closes = [bar.close for bar in candles]
    need = max(2 * adx_period + 1, bb_period)
    if len(candles) < need:
        return Regime(UNKNOWN, reason="not enough bars")
    adx = adx_from_candles(candles, period=adx_period)
    bandwidth = bb_bandwidth(closes, period=bb_period, k=bb_k)
    if adx is None or bandwidth is None:
        return Regime(UNKNOWN, adx=adx, bb_bandwidth=bandwidth, reason="indicators missing")
    if adx < adx_chop and bandwidth < bb_chop:
        return Regime(CHOP, adx=adx, bb_bandwidth=bandwidth, reason="low ADX + tight BB")
    if adx >= adx_trend:
        return Regime(TREND, adx=adx, bb_bandwidth=bandwidth, reason="ADX trend")
    return Regime(UNKNOWN, adx=adx, bb_bandwidth=bandwidth, reason="not clearly chop or trend")


def chop_veto_note(ticker: str, regime: Regime) -> str | None:
    if not regime.is_chop:
        return None
    return f"{ticker}: {CHOP_VETO_PHRASE} ({regime.summary})"
