from datetime import datetime, timedelta, timezone

from src.markets import (
    MarketDiscovery,
    HourlyMarket,
    in_current_or_next_hour,
    market_from_api,
    parse_threshold,
    rank_hourly_markets,
)


def test_parse_threshold_from_or_above_and_floor():
    floor, kind = parse_threshold(
        {"floor_strike": "78099.99", "strike_type": "greater", "yes_sub_title": "$78,100 or above"}
    )
    assert floor == 78099.99
    assert kind == "greater"
    floor, kind = parse_threshold({"ticker": "KXBTCD-26SEP0107-T78199.99", "yes_sub_title": ""})
    assert floor == 78199.99


def test_in_current_or_next_hour_keeps_this_hours_print():
    now = datetime(2026, 9, 1, 10, 43, tzinfo=timezone.utc)
    this_close = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    next_close = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc)
    assert in_current_or_next_hour(this_close, now)
    assert in_current_or_next_hour(next_close, now)
    assert not in_current_or_next_hour(later, now)
    assert not in_current_or_next_hour(now - timedelta(minutes=1), now)


def test_market_from_api_reads_dollar_quotes_and_cf_benchmarks():
    event = {
        "event_ticker": "KXBTCD-26SEP0107",
        "series_ticker": "KXBTCD",
        "title": "BTC price on Sep 1, 2026 at 7am EDT?",
        "settlement_sources": [{"name": "CF Benchmarks", "url": "https://www.cfbenchmarks.com/data/indices/BRTI"}],
        "strike_date": "2026-09-01T11:00:00Z",
    }
    raw = {
        "ticker": "KXBTCD-26SEP0107-T78099.99",
        "event_ticker": "KXBTCD-26SEP0107",
        "status": "active",
        "close_time": "2026-09-01T11:00:00Z",
        "yes_sub_title": "$78,100 or above",
        "strike_type": "greater",
        "floor_strike": 78099.99,
        "yes_bid_dollars": "0.5300",
        "yes_ask_dollars": "0.5400",
        "no_bid_dollars": "0.4600",
        "no_ask_dollars": "0.4700",
        "yes_ask_size_fp": "8.00",
        "yes_bid_size_fp": "2134.54",
        "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI)",
        "exchange_index": 2,
    }
    market = market_from_api(raw, event)
    assert market is not None
    assert market.asset == "BTC"
    assert market.threshold == 78099.99
    assert market.yes_ask == 0.54
    assert market.no_ask == 0.47
    assert "CF Benchmarks" in market.settlement_source


def test_discover_does_not_load_15m_when_hourly_empty():
    class Client:
        def open_events(self, series, limit=20):
            if str(series).endswith("15M"):
                raise AssertionError("15m series should not be requested")
            return []

    found = MarketDiscovery(Client()).discover(["BTC", "ETH"], allow_15m_fallback=True)
    assert found == []


def _book(threshold: float, ticker: str) -> HourlyMarket:
    return HourlyMarket(
        ticker=ticker,
        event_ticker="KXBTCD-X",
        series_ticker="KXBTCD",
        asset="BTC",
        title="t",
        yes_sub_title=f"${threshold} or above",
        threshold=threshold,
        strike_type="greater",
        close_time=datetime.now(timezone.utc) + timedelta(minutes=40),
        status="active",
        yes_bid=0.4,
        yes_ask=0.42,
        no_bid=0.58,
        no_ask=0.60,
        yes_bid_size=10,
        yes_ask_size=10,
        no_bid_size=10,
        no_ask_size=10,
        rules_primary="",
        rules_secondary="",
        settlement_source="CF",
        exchange_index=2,
    )


def test_rank_hourly_markets_keeps_far_strikes_when_close_ones_fill_the_book():
    spot = 100_000.0
    # 12 close strikes inside 0.50%, plus 3 far ones the filter could actually use.
    close = [_book(100_000 + 40 * i, f"C{i}") for i in range(12)]
    far = [_book(101_000, "F1"), _book(99_000, "F2"), _book(102_000, "F3")]
    ranked = rank_hourly_markets(
        close + far,
        spot,
        limit=12,
        min_distance_pct=0.005,
        watch_slots=3,
    )
    tickers = {m.ticker for m in ranked}
    assert {"F1", "F2", "F3"} <= tickers
    assert len(ranked) == 12
    # Far strikes take priority; leftover slots stay nearby for the watch list.
    assert sum(1 for m in ranked if m.ticker.startswith("C")) == 9
