"""CCXT + pandas-ta 15m signal fetcher."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from src.data_fetcher import DataFetcher, signals_for_asset
from src.indicators import tape_fail_reason, tape_from_15m_signals


def _fake_ohlcv(n: int = 80) -> list[list[float]]:
    rng = np.random.default_rng(42)
    rows: list[list[float]] = []
    price = 100.0
    ts = 1_700_000_000_000
    for i in range(n):
        price += float(rng.normal(0.05, 0.4))
        high = price + abs(float(rng.normal(0, 0.3)))
        low = price - abs(float(rng.normal(0, 0.3)))
        rows.append([ts + i * 900_000, price, high, low, price, 10.0])
    return rows


def test_fetch_15m_signals_builds_payload_from_closed_candle():
    pytest.importorskip("ccxt")
    pytest.importorskip("pandas")
    pytest.importorskip("pandas_ta")

    fake = MagicMock()
    fake.fetch_ohlcv.return_value = _fake_ohlcv()
    fetcher = object.__new__(DataFetcher)
    fetcher.exchange = fake
    fetcher.exchange_id = "binance"

    payload = fetcher.fetch_15m_signals("BTC/USDT", limit=80)

    assert payload["symbol"] == "BTC/USDT"
    assert payload["timeframe"] == "15m"
    assert payload["close_price"] > 0
    assert 0 <= payload["rsi_14"] <= 100
    assert payload["adx_14"] >= 0
    assert payload["bb_upper"] >= payload["bb_lower"]
    assert "macd_hist" in payload
    assert payload["bb_bandwidth"] < 1.0  # fraction, not percent
    args, kwargs = fake.fetch_ohlcv.call_args
    assert "BTC/USDT" in args
    assert kwargs.get("timeframe", args[1] if len(args) > 1 else None) == "15m"


def test_tape_from_15m_signals_feeds_macd_gate():
    payload = {
        "rsi_14": 55.0,
        "adx_14": 40.0,
        "bb_middle": 100.0,
        "bb_upper": 102.0,
        "bb_lower": 98.0,
        "bb_bandwidth": 0.04,
        "percent_b": 0.5,
        "close_price": 100.0,
        "macd_hist": -0.5,
        "macd_line": -0.2,
        "macd_signal": 0.3,
    }
    tape = tape_from_15m_signals(payload)
    assert tape is not None
    assert tape.source == "15m"
    assert tape.macd_hist == -0.5
    assert "MACD hist against Yes" in (tape_fail_reason("yes", tape) or "")
    assert tape_fail_reason("no", tape) is None


def test_signals_for_asset_unknown_returns_empty():
    assert signals_for_asset("DOGE") == {}
