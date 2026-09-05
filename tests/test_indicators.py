"""BB / RSI / ADX tape helpers for the 15m bot."""

from __future__ import annotations

from src.indicators import (
    ADX_TREND_MIN,
    BB_TIGHT_BANDWIDTH,
    Candle,
    TapeReading,
    bollinger,
    read_tape,
    rsi,
    tape_fail_reason,
)


def _trend_candles(n: int = 40, step: float = 0.5) -> list[Candle]:
    price = 100.0
    out: list[Candle] = []
    for _ in range(n):
        price += step
        out.append(Candle(open=price - step / 2, high=price + 0.2, low=price - 0.2, close=price))
    return out


def _flat_candles(n: int = 40) -> list[Candle]:
    out: list[Candle] = []
    for i in range(n):
        wobble = 0.01 if i % 2 == 0 else -0.01
        px = 100.0 + wobble
        out.append(Candle(open=100.0, high=px + 0.005, low=px - 0.005, close=px))
    return out


def test_rsi_overbought_on_strong_up_tape():
    closes = [c.close for c in _trend_candles(30, step=1.0)]
    value = rsi(closes)
    assert value is not None
    assert value >= 70


def test_bollinger_bandwidth_tight_on_flat_tape():
    closes = [c.close for c in _flat_candles(30)]
    bb = bollinger(closes)
    assert bb is not None
    _mid, _up, _lo, bandwidth, _pb = bb
    assert bandwidth < BB_TIGHT_BANDWIDTH * 2


def test_tape_fail_reason_rules():
    chop = TapeReading(
        rsi=50.0,
        adx=ADX_TREND_MIN - 5,
        bb_mid=100.0,
        bb_upper=100.05,
        bb_lower=99.95,
        bb_bandwidth=BB_TIGHT_BANDWIDTH / 2,
        percent_b=0.5,
        bars=40,
    )
    assert "ADX chop" in (tape_fail_reason("yes", chop) or "")

    tight = TapeReading(
        rsi=50.0,
        adx=40.0,
        bb_mid=100.0,
        bb_upper=100.05,
        bb_lower=99.95,
        bb_bandwidth=BB_TIGHT_BANDWIDTH / 2,
        percent_b=0.5,
        bars=40,
    )
    assert "BB tight" in (tape_fail_reason("no", tight) or "")

    hot = TapeReading(
        rsi=75.0,
        adx=40.0,
        bb_mid=100.0,
        bb_upper=101.0,
        bb_lower=99.0,
        bb_bandwidth=0.02,
        percent_b=0.9,
        bars=40,
    )
    assert "RSI overbought" in (tape_fail_reason("yes", hot) or "")
    assert tape_fail_reason("no", hot) is None
