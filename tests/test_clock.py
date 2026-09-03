import logging
from datetime import datetime, timezone

from src.clock import EasternFormatter, format_et, hour_key, same_et_day, same_et_hour, to_et
from src.filters import news_blackout_active
from src.markets import MarketDiscovery, market_from_api


def test_hour_key_floors_to_eastern():
    now = datetime(2026, 9, 2, 7, 30, tzinfo=timezone.utc)  # 3:30 AM EDT
    key = hour_key(now)
    assert "T03:00:00" in key
    assert "-04:00" in key or "EDT" in key


def test_format_et_says_eastern():
    now = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)  # 4:00 AM EDT
    text = format_et(now)
    assert "4:00 AM" in text
    assert "EDT" in text or "ET" in text


def test_format_et_winter_is_est():
    now = datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)  # 8:00 AM EST
    text = format_et(now)
    assert "8:00 AM" in text
    assert "EST" in text


def test_parse_ts_reads_human_eastern_report_stamp():
    from src.clock import parse_ts

    parsed = parse_ts("2026-09-02 2:24 PM EDT")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 9 and parsed.day == 2
    assert parsed.hour == 14 and parsed.minute == 24
    assert same_et_day("2026-09-02 2:24 PM EDT", parsed)


def test_same_et_day_crosses_utc_midnight():
    now = datetime(2026, 9, 2, 3, 30, tzinfo=timezone.utc)  # 11:30 PM EDT Sep 1
    assert same_et_day("2026-09-02T02:00:00Z", now)
    assert not same_et_day("2026-09-02T06:00:00Z", now)


def test_same_et_hour_parses_utc_zulu():
    now = datetime(2026, 9, 2, 7, 22, tzinfo=timezone.utc)  # 3:22 AM EDT
    assert same_et_hour("2026-09-02T07:10:00Z", now)
    assert not same_et_hour("2026-09-02T08:10:00Z", now)


def test_to_et_naive_is_utc():
    naive = datetime(2026, 9, 2, 12, 0)
    local = to_et(naive)
    assert local.tzname() in {"EDT", "EST"}


def test_eastern_log_line_uses_civil_clock():
    record = logging.LogRecord(
        "src.clock",
        logging.INFO,
        __file__,
        0,
        "hello",
        (),
        None,
    )
    record.created = datetime(2026, 9, 2, 8, 0, 7, tzinfo=timezone.utc).timestamp()
    stamp = EasternFormatter().formatTime(record)
    assert "4:00:07 AM" in stamp
    assert "EDT" in stamp


def test_cpi_blackout_uses_eastern_clock():
    # 8:30 AM EDT on 2026-09-11 CPI = 12:30 UTC
    inside = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
    outside = datetime(2026, 9, 11, 13, 0, tzinfo=timezone.utc)
    assert news_blackout_active(inside)
    assert not news_blackout_active(outside)


def test_next_settlements_print_eastern():
    event = {
        "event_ticker": "KXBTCD-26SEP0107",
        "series_ticker": "KXBTCD",
        "title": "BTC price on Sep 1, 2026 at 7am EDT?",
        "settlement_sources": [{"name": "CF Benchmarks"}],
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
        "rules_primary": "CF Benchmarks BRTI",
        "exchange_index": 2,
    }
    market = market_from_api(raw, event)
    assert market is not None
    rows = MarketDiscovery(client=None).next_settlements([market])
    assert rows
    assert "UTC" not in rows[0]
    assert "7:00 AM" in rows[0]
    assert "EDT" in rows[0]
