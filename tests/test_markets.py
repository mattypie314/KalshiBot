from datetime import datetime, timedelta, timezone

from src.markets import (
    in_current_or_next_hour,
    market_from_api,
    parse_threshold,
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
