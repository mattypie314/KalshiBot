from src.evaluate import (
    format_eval_report,
    replay_historical_actionables,
    summarize_scans,
    summarize_trades,
)
from src.main import SCAN_LOG_SECRET_KEYS, scan_log_row
from src.spot import SpotSnapshot
from datetime import datetime, timezone
from src.config import HourlySettings


def test_summarize_trades_empty_is_insufficient():
    summary = summarize_trades([])
    assert summary["n_filled_settled"] == 0
    assert summary["enough_for_rate"] is False
    assert summary["hit_rate"] is None
    assert summary["pnl"] == 0.0


def test_summarize_trades_ignores_unfilled():
    rows = [
        {"result": "unfilled", "pnl": 0, "fill_status": "unfilled", "bucket": "far_no"},
        {"result": "win", "pnl": 1.2, "fill_status": "filled", "bucket": "far_no", "asset": "BTC"},
        {"result": "loss", "pnl": -1.5, "fill_status": "filled", "bucket": "far_no", "asset": "BTC"},
    ]
    summary = summarize_trades(rows)
    assert summary["n_unfilled"] == 1
    assert summary["n_filled_settled"] == 2
    assert summary["n_wins"] == 1
    assert summary["pnl"] == -0.3
    assert summary["enough_for_rate"] is False


def test_summarize_scans_counts_sits():
    rows = [{"ideas": []}, {"ideas": [{"ticker": "X"}]}, {"ideas": []}]
    summary = summarize_scans(rows)
    assert summary["n_scans"] == 3
    assert summary["n_sits"] == 2
    assert summary["n_recorded_ideas"] == 1


def test_historical_actionables_are_rejected_by_current_filters():
    replay = replay_historical_actionables()
    assert replay["n"] == 6
    assert replay["still_actionable"] == 0
    for row in replay["rejected"]:
        assert row["now_actionable"] is False
        assert row["distance_pct"] < 0.005
        blob = " ".join(row["avoid"]).lower()
        assert "close strike" in blob or "net edge" in blob


def test_eval_report_states_insufficient_data():
    text = format_eval_report(
        trades=summarize_trades([]),
        scans=summarize_scans([]),
        historical=replay_historical_actionables(),
    )
    assert "Insufficient live data" in text
    assert "Still actionable under current rules: 0" in text
    assert "Not financial advice" in text
    assert "Not live profitability" in text
    assert "assumed-maker-fill" in text
    assert "paper_log.jsonl" in text


def test_scan_log_row_has_no_secret_keys():
    spots = SpotSnapshot(prices={"BTC": 77000.0}, sources={"BTC": "coinbase"}, hourly_vol={"BTC": 0.004})
    row = scan_log_row(
        now=datetime.now(timezone.utc),
        spots=spots,
        markets=[],
        ideas=[],
        nearby=[],
        avoided=[],
        settings=HourlySettings(_env_file=None, halted=True),
        action="scan",
    )
    keys = {str(key).lower() for key in row}
    assert not (SCAN_LOG_SECRET_KEYS & keys)
    assert "kalshi_api_key_id" not in keys
    assert row["halted"] is True
    assert row["live_enabled"] is False
    assert "settlement_ok" in row
    assert row["force_near_rule"] is False
    assert row["forced"] is False
    assert row["label"] == ""
