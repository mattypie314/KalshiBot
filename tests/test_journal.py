from src.clock import to_et
from src.journal import (
    bucket_underwater,
    daily_loss_reason,
    estimate_pnl,
    fill_status_from_order,
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

    out = resolve_pending(
        rows,
        get_market,
        is_loss,
        fills=[{"ticker": "KXETHD-1"}],
        fills_available=True,
    )
    assert out[0]["result"] == "loss"
    assert out[0]["pnl"] == estimate_pnl(won=False, contracts=2, entry_price=0.38, risk_dollars=0.76)
    assert out[0]["bucket"] == "close_no"
    assert out[0]["fill_status"] == "filled"
    assert out[0]["settlement_result"] == "yes"
    assert out[0]["model_pct"] == 0.62


def test_resolve_pending_does_not_score_unfilled_rest():
    rows = [
        new_trade_row(
            ticker="KXBTCD-1",
            asset="BTC",
            side="No",
            strike=77600.0,
            spot=76800.0,
            minutes_left=40,
            fair=0.70,
            kalshi_price=0.82,
            limit_price=0.81,
            contracts=2,
            risk_dollars=1.64,
            hourly_vol=0.004,
            source="cfbenchmarks",
        )
    ]

    out = resolve_pending(
        rows,
        lambda ticker: {"result": "yes", "status": "determined"},
        lambda market, side: str(market.get("result")) != side.lower(),
        fills=[],
        fills_available=True,
    )
    assert out[0]["result"] == "unfilled"
    assert out[0]["pnl"] == 0.0


def test_resolve_pending_leaves_unknown_fill_when_fills_unavailable():
    rows = [
        new_trade_row(
            ticker="KXBTCD-1",
            asset="BTC",
            side="No",
            strike=77600.0,
            spot=76800.0,
            minutes_left=40,
            fair=0.70,
            kalshi_price=0.82,
            limit_price=0.81,
            contracts=2,
            risk_dollars=1.64,
            hourly_vol=0.004,
            source="cfbenchmarks",
        )
    ]
    out = resolve_pending(
        rows,
        lambda ticker: {"result": "yes"},
        lambda market, side: True,
        fills_available=False,
    )
    assert out[0]["result"] == "pending"
    assert out[0]["pnl"] is None


def test_daily_loss_reason_sits_after_two_filled_losses():
    now = to_et()
    rows = [
        {
            "result": "loss",
            "fill_status": "filled",
            "pnl": -1.75,
            "resolved_ts": now.isoformat(),
        },
        {
            "result": "loss",
            "fill_status": "filled",
            "pnl": -1.75,
            "resolved_ts": now.isoformat(),
        },
    ]
    reason = daily_loss_reason(rows, now, max_dollars=4.00, max_losses=2)
    assert reason is not None
    assert "2 filled losses" in reason
    assert daily_loss_reason(rows[:1], now, max_dollars=4.00, max_losses=2) is None
    unfilled = [{"result": "unfilled", "pnl": 0, "resolved_ts": now.isoformat()}]
    assert daily_loss_reason(unfilled, now, max_dollars=4.00, max_losses=2) is None


def test_fill_status_from_order():
    assert fill_status_from_order({"fill_count": "0.00", "remaining_count": "3.00"}) == "resting"
    assert fill_status_from_order({"fill_count": "3.00", "remaining_count": "0.00"}) == "filled"
    assert fill_status_from_order({"fill_count": "1.00", "remaining_count": "2.00"}) == "partial"
    assert fill_status_from_order({"status": "canceled", "fill_count": "0"}) == "canceled"
