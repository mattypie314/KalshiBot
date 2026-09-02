from src.journal import (
    bucket_underwater,
    estimate_pnl,
    new_trade_row,
    resolve_pending,
    strike_distance_pct,
    trade_bucket,
)


def test_close_no_bucket_and_kill_switch():
    assert trade_bucket("No", 0.004) == "close_no"
    assert trade_bucket("No", 0.02) == "far_no"
    assert strike_distance_pct(2390.0, 2375.0) < 0.007

    rows = [
        {"bucket": "close_no", "result": "loss", "pnl": -0.90},
        {"bucket": "close_no", "result": "loss", "pnl": -2.09},
        {"bucket": "close_no", "result": "win", "pnl": 0.36},
    ]
    assert bucket_underwater(rows, "close_no") is True
    assert bucket_underwater(rows[:2], "close_no") is False


def test_resolve_pending_marks_settlement():
    rows = [
        new_trade_row(
            ticker="KXETHD-1",
            asset="ETH",
            side="No",
            strike=2375.0,
            spot=2390.0,
            minutes_left=40,
            fair=0.62,
            kalshi_price=0.38,
            limit_price=0.37,
            contracts=2,
            risk_dollars=0.76,
            hourly_vol=0.005,
            source="cfbenchmarks",
        )
    ]

    def get_market(ticker):
        return {"result": "yes", "status": "determined"}

    def is_loss(market, side):
        return str(market.get("result")) != side.lower()

    out = resolve_pending(rows, get_market, is_loss)
    assert out[0]["result"] == "loss"
    assert out[0]["pnl"] == estimate_pnl(won=False, contracts=2, entry_price=0.38, risk_dollars=0.76)
    assert out[0]["bucket"] == "close_no"
