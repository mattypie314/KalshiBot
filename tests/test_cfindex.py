from datetime import datetime, timedelta, timezone

from src.cfindex import (
    average_settlement_window,
    history_query_timestamp,
    index_id_for,
    official_yes,
    parse_cf_history_ticks,
    parse_cf_index_value,
)


def test_parse_cf_kalshi_envelope():
    blob = {
        "data": {
            "serverTime": "2026-09-02T13:00:00.000Z",
            "payload": [{"id": "BRTI", "value": "77343.72"}],
        }
    }
    assert parse_cf_index_value(blob) == 77343.72
    assert parse_cf_index_value({"payload": {"value": 2395.1}}) == 2395.1
    assert parse_cf_index_value({"error": "nope"}) is None
    assert index_id_for("BTC") == "BRTI"
    assert index_id_for("ETH") == "ETHUSD_RTI"
    assert "ERTI" in __import__("src.cfindex", fromlist=["index_ids_for"]).index_ids_for("ETH")


def test_history_ticks_and_60s_average():
    close = datetime(2026, 9, 3, 21, 0, tzinfo=timezone.utc)
    blob = {
        "data": {
            "payload": [
                {"time": (close - timedelta(seconds=60 - i)).isoformat(), "value": str(100 + i)}
                for i in range(60)
            ]
        }
    }
    ticks = parse_cf_history_ticks(blob)
    assert len(ticks) == 60
    assert average_settlement_window(ticks, close) == 100 + 59 / 2
    assert history_query_timestamp(close, timespan="MINUTE").endswith("20:59:00.000Z")
    assert official_yes(settlement_print=101.0, strike=100.5) is True
    assert official_yes(settlement_print=100.5, strike=100.5) is False
