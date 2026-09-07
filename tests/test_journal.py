from src.clock import to_et
from src.evaluate import summarize_trades
from src.journal import (
    FILL_BACKFILL_SOURCE,
    KIND_BACKFILL,
    TURBO_LABEL,
    bucket_underwater,
    counts_for_entry_timing,
    counts_for_scoreboard,
    daily_loss_reason,
    day_filled_pnl,
    estimate_pnl,
    fill_already_journaled,
    fill_size_from_payload,
    fill_status_from_order,
    forced_ticket_fields,
    is_journal_backfill,
    late_place_rate,
    new_backfill_row,
    new_trade_row,
    play_pot_equity,
    resolve_pending,
    scoreboard_rows,
    seconds_into_window,
    strike_distance_pct,
    summarize_entry_timing,
    trade_bucket,
    win_loss_streak,
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


def test_new_trade_row_default_is_not_forced():
    row = new_trade_row(
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
    assert row["forced"] is False
    assert row["turbo"] is False
    assert row["force_near_rule"] is False
    assert row["label"] == ""


def test_new_trade_row_labels_turbo_force_near_rule():
    row = new_trade_row(
        ticker="KXETHD-1",
        asset="ETH",
        side="Yes",
        strike=2375.0,
        spot=2390.0,
        minutes_left=25,
        fair=0.90,
        kalshi_price=0.64,
        limit_price=0.63,
        contracts=2,
        risk_dollars=1.28,
        hourly_vol=0.005,
        source="cfbenchmarks",
        forced=True,
        force_near_rule=True,
    )
    assert row["forced"] is True
    assert row["turbo"] is True
    assert row["force_near_rule"] is True
    assert row["label"] == TURBO_LABEL
    assert forced_ticket_fields(forced=True)["label"] == TURBO_LABEL


def test_fill_status_from_order():
    assert fill_status_from_order({"fill_count": "0.00", "remaining_count": "3.00"}) == "resting"
    assert fill_status_from_order({"fill_count": "3.00", "remaining_count": "0.00"}) == "filled"
    assert fill_status_from_order({"fill_count": "1.00", "remaining_count": "2.00"}) == "partial"
    assert fill_status_from_order({"status": "canceled", "fill_count": "0"}) == "canceled"
    assert fill_status_from_order({"fill_count_fp": "2.00", "remaining_count_fp": "0.00"}) == "filled"


def test_fill_size_from_payload_prefers_count_fp():
    assert fill_size_from_payload({"count_fp": "2.00"}) == 2.0
    assert fill_size_from_payload({"count": "0.00", "count_fp": "2.00"}) == 2.0
    assert fill_size_from_payload({"count": 0, "filled_count_fp": "3.00"}) == 3.0
    assert fill_size_from_payload({"count": "1.00"}) == 1.0
    assert fill_size_from_payload({}) == 0.0


def _known_backfill_fingerprint(**kwargs):
    """The four '>4m into window' rows: fill recon, not a late Pass."""
    row = {
        "ts": "2026-09-07 10:14 AM EDT",
        "ts_iso": "2026-09-07T10:14:20-04:00",
        "backfill": True,
        "spot_source": FILL_BACKFILL_SOURCE,
        "kind": KIND_BACKFILL,
        "ticker": "KXBTC15M-26SEP071000-T64000",
        "asset": "BTC",
        "side": "Yes",
        "strike": 0.0,
        "spot": 0.0,
        "hourly_vol": 0.0,
        "minutes_left": 0.0,
        "action": "sell",
        "contracts": 1,
        "kalshi_price": 0.01,
        "fill_status": "filled",
        "result": "win",
        "pnl": 0.01,
        "fill_ts": "2026-09-07 10:14 AM EDT",
        "fill_ts_iso": "2026-09-07T10:14:00-04:00",
    }
    row.update(kwargs)
    return row


def test_backfill_writer_reads_count_fp_when_count_is_zero():
    row = new_backfill_row(
        fill={
            "ticker": "KXBTC15M-26SEP071015-T64000",
            "order_id": "ord-fp",
            "fill_id": "fill-fp",
            "side": "yes",
            "action": "buy",
            "count": "0.00",
            "count_fp": "2.00",
            "yes_price": "0.4000",
            "created_time": "2026-09-07T14:14:00Z",
        }
    )
    assert row["kind"] == KIND_BACKFILL
    assert row["contracts"] == 2
    assert row["filled_contracts"] == 2.0
    assert row["risk_dollars"] == 0.8
    assert row["kalshi_price"] == 0.4


def test_backfill_writer_emits_kind_backfill():
    row = new_backfill_row(
        fill={
            "ticker": "KXBTC15M-26SEP071015-T64000",
            "order_id": "ord-residual",
            "fill_id": "fill-residual",
            "side": "yes",
            "action": "sell",
            "count": "1.00",
            "yes_price": "0.0100",
            "created_time": "2026-09-07T14:14:00Z",
        }
    )
    assert row["kind"] == KIND_BACKFILL
    assert row["backfill"] is True
    assert row["spot_source"] == FILL_BACKFILL_SOURCE
    assert row["spot"] == 0.0
    assert row["strike"] == 0.0
    assert row["hourly_vol"] == 0.0
    assert row["minutes_left"] == 0.0
    assert row["action"] == "sell"
    assert row["fill_ts_iso"]
    assert row["fill_id"] == "fill-residual"
    assert row["order_id"] == "ord-residual"
    assert seconds_into_window(row) == 14 * 60
    assert seconds_into_window({**row, "ts_iso": "2026-09-07T10:14:00-04:00", "fill_ts_iso": None, "fill_ts": None}) is None


def test_timing_helpers_skip_backfills_and_scoreboard_skips_them_too():
    live = new_trade_row(
        ticker="KXBTC15M-26SEP071000-T65000",
        asset="BTC",
        side="Yes",
        strike=65000.0,
        spot=65200.0,
        minutes_left=12.5,
        fair=0.62,
        kalshi_price=0.54,
        limit_price=0.54,
        contracts=2,
        risk_dollars=1.08,
        hourly_vol=0.004,
        source="cfbenchmarks",
        fill_status="filled",
    )
    live["ts_iso"] = "2026-09-07T10:02:30-04:00"
    live["resolved_ts_iso"] = "2026-09-07T10:15:00-04:00"
    live["result"] = "win"
    live["pnl"] = 0.92
    backfills = [
        _known_backfill_fingerprint(ticker=f"KXBTC15M-26SEP071000-T{i}")
        for i in range(4)
    ]
    rows = [live, *backfills]
    assert all(is_journal_backfill(row) for row in backfills)
    assert counts_for_entry_timing(live) is True
    assert all(counts_for_entry_timing(row) is False for row in backfills)
    assert counts_for_scoreboard(live) is True
    assert all(counts_for_scoreboard(row) is False for row in backfills)
    assert scoreboard_rows(rows) == [live]
    pnl = summarize_trades(rows)
    assert pnl["n_journal_rows"] == 5
    assert pnl["n_backfills"] == 4
    assert pnl["n_rows"] == 1
    assert pnl["n_filled_settled"] == 1
    assert pnl["n_wins"] == 1
    assert pnl["n_losses"] == 0
    assert pnl["pnl"] == 0.92
    streak = win_loss_streak(rows)
    assert streak == {"result": "win", "n": 1}
    assert play_pot_equity(rows, start=5.0) == 5.92
    assert fill_already_journaled(rows, {"order_id": "unused", "ticker": live["ticker"]}) is False
    assert fill_already_journaled(backfills, {"order_id": None, "ticker": backfills[0]["ticker"]}) is True


def test_late_place_rate_is_zero_after_backfill_filter():
    live = {
        "ts": "2026-09-07 10:02 AM EDT",
        "ts_iso": "2026-09-07T10:02:40-04:00",
        "kind": "live",
        "spot_source": "cfbenchmarks",
        "ticker": "KXBTC15M-26SEP071000-T64000",
        "spot": 65000.0,
        "strike": 64000.0,
        "hourly_vol": 0.004,
        "minutes_left": 12.3,
        "result": "win",
        "fill_status": "filled",
        "pnl": 0.40,
    }
    backfills = [_known_backfill_fingerprint(ticker=f"KXETH15M-26SEP071000-T{i}") for i in range(4)]
    rows = [live, *backfills]
    unfiltered_late = sum(1 for row in rows if seconds_into_window(row) and seconds_into_window(row) > 4 * 60)
    assert unfiltered_late == 4
    timing = summarize_entry_timing(rows)
    assert timing["n"] == 1
    assert timing["n_late"] == 0
    assert timing["n_backfills"] == 4
    assert timing["late_place_rate"] == 0.0
    assert late_place_rate(rows) == 0.0
    assert 120 <= timing["seconds"][0] <= 200


def test_scoreboard_streak_and_day_pnl_skip_backfills():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 9, 7, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    live_a = {
        "kind": "live",
        "fill_status": "filled",
        "result": "win",
        "pnl": 0.40,
        "resolved_ts_iso": "2026-09-07T10:15:00-04:00",
    }
    live_b = {
        "kind": "live",
        "fill_status": "filled",
        "result": "win",
        "pnl": 0.50,
        "resolved_ts_iso": "2026-09-07T10:30:00-04:00",
    }
    backfill = _known_backfill_fingerprint(
        result="loss",
        pnl=-9.99,
        fill_status="filled",
        resolved_ts_iso="2026-09-07T10:45:00-04:00",
    )
    rows = [live_a, live_b, backfill]
    assert win_loss_streak(rows) == {"result": "win", "n": 2}
    assert day_filled_pnl(rows, now=now) == 0.9
    assert play_pot_equity(rows, start=5.0) == 5.9
    assert summarize_trades(rows)["pnl"] == 0.9
