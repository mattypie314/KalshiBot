from src.spot import PROXY_NOTE, SpotService, SpotSnapshot, is_settlement_index


def test_only_cfbenchmarks_is_settlement_truth():
    assert is_settlement_index("cfbenchmarks")
    assert is_settlement_index("CFBENCHMARKS")
    assert not is_settlement_index("coinbase")
    assert not is_settlement_index("binance")
    assert not is_settlement_index("unknown")
    assert not is_settlement_index("")


def test_snapshot_settlement_ok_is_per_asset():
    snap = SpotSnapshot(
        prices={"BTC": 77000.0, "ETH": 2400.0},
        sources={"BTC": "cfbenchmarks", "ETH": "coinbase"},
        source="BTC=cfbenchmarks ETH=coinbase",
        note=PROXY_NOTE,
    )
    assert snap.settlement_ok("BTC") is True
    assert snap.settlement_ok("ETH") is False
    assert "PROXY" in snap.note
    assert "Coinbase" not in snap.note or "not the Kalshi settlement" in snap.note or "PROXY" in snap.note


def test_eth_cf_price_queries_ethusd_rti_not_erti():
    called: list[str] = []

    class Kalshi:
        can_trade = True

        def get_cf_values(self, index_id):
            called.append(index_id)
            if index_id == "ERTI":
                raise RuntimeError("Unknown id")
            return {"payload": [{"id": index_id, "value": "2513.24"}]}

    svc = SpotService(kalshi=Kalshi(), preferred="cfbenchmarks")
    try:
        assert svc._cf_price("ETH") == 2513.24
    finally:
        svc.close()
    assert called[0] == "ETHUSD_RTI"


def test_eth_cf_price_falls_back_if_primary_unknown():
    called: list[str] = []

    class Kalshi:
        can_trade = True

        def get_cf_values(self, index_id):
            called.append(index_id)
            if index_id == "ETHUSD_RTI":
                raise RuntimeError("Unknown id")
            return {"payload": [{"id": index_id, "value": "2513.24"}]}

    svc = SpotService(kalshi=Kalshi(), preferred="cfbenchmarks")
    try:
        assert svc._cf_price("ETH") == 2513.24
    finally:
        svc.close()
    assert called == ["ETHUSD_RTI", "ERTI"]
