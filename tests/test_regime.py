"""ADX / Bollinger regime: synthetic chop sits, synthetic trend does not."""

from __future__ import annotations

import math

from src.fifteen.config import FifteenSettings
from src.fifteen.regime import (
    CHOP,
    CHOP_VETO_PHRASE,
    TREND,
    UNKNOWN,
    Candle,
    chop_veto_note,
    classify_regime,
)


def choppy_ohlc(n: int = 60, start: float = 100.0, amplitude: float = 0.12) -> list[Candle]:
    """Tight oscillation — low ADX and a narrow Bollinger band."""
    bars: list[Candle] = []
    prev = start
    for i in range(n):
        close = start + amplitude * math.sin(i * 0.7)
        high = max(prev, close) + 0.015
        low = min(prev, close) - 0.015
        bars.append(Candle(high=high, low=low, close=close, open=prev))
        prev = close
    return bars


def trending_ohlc(n: int = 60, start: float = 100.0, drift: float = 0.45) -> list[Candle]:
    """Steady climb — high ADX."""
    bars: list[Candle] = []
    price = start
    for _ in range(n):
        nxt = price + drift
        high = max(price, nxt) + 0.04
        low = min(price, nxt) - 0.04
        bars.append(Candle(high=high, low=low, close=nxt, open=price))
        price = nxt
    return bars


def test_choppy_series_is_chop():
    regime = classify_regime(choppy_ohlc())
    assert regime.label == CHOP
    assert regime.is_chop
    assert regime.adx is not None and regime.adx < 20
    assert regime.bb_bandwidth is not None and regime.bb_bandwidth < 0.012


def test_trending_series_is_trend():
    regime = classify_regime(trending_ohlc())
    assert regime.label == TREND
    assert not regime.is_chop
    assert regime.adx is not None and regime.adx >= 25


def test_short_or_empty_series_is_unknown():
    assert classify_regime([]).label == UNKNOWN
    assert classify_regime(None).label == UNKNOWN
    assert classify_regime(choppy_ohlc(n=10)).label == UNKNOWN
    assert classify_regime([100.0, 100.1, 99.9]).label == UNKNOWN


def test_close_only_chop_and_trend():
    chop_closes = [bar.close for bar in choppy_ohlc()]
    trend_closes = [bar.close for bar in trending_ohlc()]
    assert classify_regime(chop_closes).label == CHOP
    assert classify_regime(trend_closes).label == TREND


def test_chop_veto_note_only_when_chop():
    chop = classify_regime(choppy_ohlc())
    trend = classify_regime(trending_ohlc())
    note = chop_veto_note("KXBTC15M-TEST", chop)
    assert note is not None
    assert CHOP_VETO_PHRASE in note
    assert "KXBTC15M-TEST" in note
    assert chop_veto_note("KXBTC15M-TEST", trend) is None
    assert chop_veto_note("KXBTC15M-TEST", classify_regime([]) ) is None


def test_chop_veto_defaults_on_and_env_disables(monkeypatch):
    monkeypatch.delenv("FIFTEEN_CHOP_VETO", raising=False)
    assert FifteenSettings(_env_file=None).chop_veto is True
    monkeypatch.setenv("FIFTEEN_CHOP_VETO", "false")
    assert FifteenSettings(_env_file=None).chop_veto is False
    monkeypatch.setenv("FIFTEEN_CHOP_VETO", "true")
    assert FifteenSettings(_env_file=None).chop_veto is True
