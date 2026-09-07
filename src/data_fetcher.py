"""Exchange OHLCV + pandas-ta signals for the 15m bot.

Pulls finalized 15-minute candles via CCXT (Binance by default) and appends
RSI, MACD, Bollinger Bands, and ADX. Returns a compact payload for the tape
gate — public market data only (no keys required for reads).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Lazy imports so unit tests / hosts without ccxt+pandas-ta still import src.
_ccxt = None
_pd = None
_ta = None


def _load_ta_stack() -> tuple[Any, Any, Any]:
    global _ccxt, _pd, _ta
    if _ccxt is not None and _pd is not None and _ta is not None:
        return _ccxt, _pd, _ta
    import ccxt  # type: ignore
    import pandas as pd  # type: ignore
    import pandas_ta as ta  # type: ignore  # noqa: F401 — registers df.ta

    _ccxt, _pd, _ta = ccxt, pd, ta
    return _ccxt, _pd, _ta



def _col(row: Any, *candidates: str, prefixes: tuple[str, ...] = ()) -> float:
    """Read a pandas-ta column that may differ slightly by library version."""
    for name in candidates:
        if name in row.index:
            try:
                return float(row[name])
            except (TypeError, ValueError):
                continue
    for prefix in prefixes:
        for name in row.index:
            if str(name).startswith(prefix):
                try:
                    return float(row[name])
                except (TypeError, ValueError):
                    continue
    return 0.0

ASSET_SYMBOLS = {
    "BTC": ("BTC/USDT", "BTC/USD"),
    "ETH": ("ETH/USDT", "ETH/USD"),
}
DEFAULT_EXCHANGES = ("binance", "coinbase", "kraken")


class DataFetcher:
    """Public OHLCV fetcher. Defaults to Binance spot; swap exchange_id as needed."""

    def __init__(self, exchange_id: str = "binance") -> None:
        ccxt, _pd, _ta = _load_ta_stack()
        exchange_cls = getattr(ccxt, exchange_id, None)
        if exchange_cls is None:
            raise ValueError(f"unknown ccxt exchange_id {exchange_id!r}")
        # Public reads only. For live execution, pass apiKey/secret here.
        self.exchange = exchange_cls(
            {
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        self.exchange_id = exchange_id

    def close(self) -> None:
        closer = getattr(self.exchange, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    def fetch_15m_signals(
        self,
        symbol: str = "BTC/USDT",
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Latest *completed* 15m candle + RSI/MACD/BB/ADX for the tape gate."""
        _ccxt, pd, _ta = _load_ta_stack()
        try:
            raw = self.exchange.fetch_ohlcv(symbol, timeframe="15m", limit=max(40, int(limit)))
            columns = ["timestamp", "open", "high", "low", "close", "volume"]
            df = pd.DataFrame(raw, columns=columns)
            if df.empty or len(df) < 30:
                logger.info("ccxt returned too few 15m candles for %s", symbol)
                return {}

            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

            # pandas-ta indicator suite (column names are library-defined).
            df.ta.rsi(length=14, append=True)
            df.ta.macd(fast=12, slow=26, signal=9, append=True)
            df.ta.bbands(length=20, std=2, append=True)
            df.ta.adx(length=14, append=True)
            df.dropna(inplace=True)
            if len(df) < 2:
                logger.info("not enough finalized 15m rows after indicator warm-up for %s", symbol)
                return {}

            # iloc[-2] = last fully closed candle (avoid the in-progress bar).
            latest = df.iloc[-2]
            bb_upper = _col(latest, "BBU_20_2.0", "BBU_20_2", prefixes=("BBU_",))
            bb_lower = _col(latest, "BBL_20_2.0", "BBL_20_2", prefixes=("BBL_",))
            bb_middle = _col(latest, "BBM_20_2.0", "BBM_20_2", prefixes=("BBM_",))
            close_px = float(latest["close"])
            width = bb_upper - bb_lower
            # Always compute fraction bandwidth; pandas-ta BBB_* is percent (×100).
            bandwidth = (width / bb_middle) if bb_middle > 0 else 0.0
            percent_b = _col(latest, prefixes=("BBP_",))
            if percent_b == 0.0 and width > 1e-12:
                percent_b = (close_px - bb_lower) / width

            return {
                "symbol": symbol,
                "exchange": self.exchange_id,
                "timeframe": "15m",
                "timestamp": str(latest["timestamp"]),
                "close_price": close_px,
                "rsi_14": round(_col(latest, "RSI_14", prefixes=("RSI_",)), 2),
                "macd_line": round(_col(latest, "MACD_12_26_9", prefixes=("MACD_12",)), 4),
                "macd_signal": round(_col(latest, "MACDs_12_26_9", prefixes=("MACDs_",)), 4),
                "macd_hist": round(_col(latest, "MACDh_12_26_9", prefixes=("MACDh_",)), 4),
                "bb_upper": round(bb_upper, 2),
                "bb_lower": round(bb_lower, 2),
                "bb_middle": round(bb_middle, 2),
                "bb_bandwidth": round(float(bandwidth), 6),
                "percent_b": round(float(percent_b), 4),
                "adx_14": round(_col(latest, "ADX_14", prefixes=("ADX_",)), 2),
            }
        except Exception as exc:  # noqa: BLE001
            logger.info("Error fetching 15m signals for %s: %s", symbol, exc)
            return {}


def signals_for_asset(
    asset: str,
    *,
    exchange_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """BTC/ETH → 15m RSI/MACD/BB/ADX. Tries Binance, then Coinbase, then Kraken."""
    symbols = ASSET_SYMBOLS.get(str(asset or "").upper())
    if not symbols:
        return {}
    exchanges = (exchange_id,) if exchange_id else DEFAULT_EXCHANGES
    last_err = ""
    for ex_id in exchanges:
        if not ex_id:
            continue
        try:
            fetcher = DataFetcher(exchange_id=ex_id)
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            logger.info("skip exchange %s: %s", ex_id, exc)
            continue
        try:
            for symbol in symbols:
                payload = fetcher.fetch_15m_signals(symbol, limit=limit)
                if payload:
                    return payload
        finally:
            fetcher.close()
    if last_err:
        logger.info("no 15m signals for %s (%s)", asset, last_err)
    return {}


if __name__ == "__main__":
    fetcher = DataFetcher(exchange_id="binance")
    print("Fetching active market data...")
    try:
        print("Data context for AI agents:\n", fetcher.fetch_15m_signals("BTC/USDT"))
    finally:
        fetcher.close()
