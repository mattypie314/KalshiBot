from src.spot import PROXY_NOTE, SpotSnapshot, is_settlement_index


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
