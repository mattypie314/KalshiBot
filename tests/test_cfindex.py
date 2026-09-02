from src.cfindex import index_id_for, parse_cf_index_value


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
    assert index_id_for("ETH") == "ERTI"
