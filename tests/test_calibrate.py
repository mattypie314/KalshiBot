from datetime import datetime
from pathlib import Path

from src.calibrate import (
    MIN_N_FOR_RATE,
    apply_settlements,
    collect_limitations,
    dedupe_last_per_window,
    expand_scan_snapshot,
    expand_scans,
    fetch_missing_prints,
    format_calibration_report,
    ingest_settlement_row,
    load_scan_snapshots,
    load_settlements,
    majority_call_hit,
    run_calibration,
    summarize_buckets,
)
from src.cfindex import official_yes
from src.clock import parse_ts
from src.evaluate import MIN_SETTLED_FOR_RATE
from src.model import fair_prob, hours_left, model_z

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "calibration"


def _scored_row(*, model_prob: float, settled_yes: bool, abs_z: float = 0.4, proxy: bool = False) -> dict:
    return {
        "model_prob": model_prob,
        "settled_yes": settled_yes,
        "win_vs_model": majority_call_hit(model_prob=model_prob, settled_yes=settled_yes),
        "abs_z": abs_z,
        "taken_or_not": False,
        "settlement_ok": not proxy,
        "pnl": 999.0,
    }


def test_min_n_matches_eval_discipline():
    assert MIN_N_FOR_RATE == MIN_SETTLED_FOR_RATE == 20


def test_expand_scan_log_emits_every_strike_taken_or_not():
    snapshots = load_scan_snapshots(FIXTURES / "scan_log.jsonl")
    rows = expand_scans(snapshots)
    assert len(rows) == 6
    first = [row for row in rows if row["ts"].startswith("2026-09-09 4:20")]
    tickers = {row["ticker"] for row in first}
    assert tickers == {
        "KXBTCD-26SEP0917-T77250",
        "KXBTCD-26SEP0917-T76500",
        "KXETHD-26SEP0917-T2399",
    }
    taken = {row["ticker"]: row["taken_or_not"] for row in first}
    assert taken["KXBTCD-26SEP0917-T76500"] is True
    assert taken["KXBTCD-26SEP0917-T77250"] is False
    no_side = next(row for row in first if row["ticker"].endswith("T76500"))
    assert no_side["side"] == "No"
    assert no_side["market_price"] == 0.28
    assert no_side["horizon"] == "hourly"
    assert no_side["hours_left"] is not None
    assert no_side["minutes_left"] == 40.0

    scan_at = parse_ts("2026-09-09 4:20 PM EDT")
    close = parse_ts("2026-09-09T17:00:00-04:00")
    hrs = hours_left((close - scan_at).total_seconds())
    yes_row = next(row for row in first if row["ticker"].endswith("T77250"))
    assert yes_row["model_prob"] == round(fair_prob(77000.0, 77249.99, 0.004, hrs), 6)
    assert yes_row["z"] == round(model_z(77000.0, 77249.99, 0.004, hrs), 6)
    assert yes_row["join_price"] is not None


def test_taken_no_fair_fallback_is_converted_to_yes_prob():
    snapshot = {
        "ts": "2026-09-09 4:20 PM EDT",
        "spots": {"BTC": 77000.0},
        "spot_sources": {"BTC": "cfbenchmarks"},
        "vol": {},
        "settlement_ok": {"BTC": True},
        "markets": [
            {
                "ticker": "KXBTCD-NO",
                "asset": "BTC",
                "threshold": 76500.0,
                "yes_bid": 0.70,
                "yes_ask": 0.72,
                "no_bid": 0.27,
                "no_ask": 0.29,
                "close_time": "2026-09-09T17:00:00-04:00",
            }
        ],
        "ideas": [{"ticker": "KXBTCD-NO", "side": "No", "fair": 0.20, "kalshi_price": 0.29}],
    }
    rows = expand_scan_snapshot(snapshot)
    assert rows[0]["model_prob"] == 0.80
    assert rows[0]["side"] == "No"
    assert rows[0]["taken_or_not"] is True


def test_last_run_ticker_list_does_not_expand():
    snapshots = load_scan_snapshots(Path("/no/such.jsonl"), last_run=FIXTURES / "last_run.json")
    assert snapshots == []
    assert expand_scan_snapshot(
        {
            "ts": "x",
            "markets": ["KXBTCD-26SEP0917-T77250"],
            "spots": {"BTC": 77000},
        }
    ) == []


def test_dedupe_keeps_last_scan_per_window():
    snapshots = load_scan_snapshots(FIXTURES / "scan_log.jsonl")
    rows = dedupe_last_per_window(expand_scans(snapshots))
    assert len(rows) == 3
    btc_high = next(row for row in rows if row["ticker"].endswith("T77250"))
    assert btc_high["ts"].startswith("2026-09-09 4:40")
    assert btc_high["spot"] == 77100.0
    assert btc_high["taken_or_not"] is False


def test_join_official_print_not_paper_pnl():
    snapshots = load_scan_snapshots(FIXTURES / "scan_log.jsonl")
    rows = dedupe_last_per_window(expand_scans(snapshots))
    index = load_settlements(
        [FIXTURES / "settlements.jsonl", FIXTURES / "paper_log.jsonl"]
    )
    apply_settlements(rows, index)
    by_ticker = {row["ticker"]: row for row in rows}

    high = by_ticker["KXBTCD-26SEP0917-T77250"]
    assert high["settlement_print"] == 77300.0
    assert high["settled_yes"] is True
    assert official_yes(settlement_print=77300.0, strike=77249.99) is True
    assert high["win_vs_model"] == majority_call_hit(
        model_prob=high["model_prob"], settled_yes=True
    )

    fade = by_ticker["KXBTCD-26SEP0917-T76500"]
    assert fade["settled_yes"] is True
    # Paper tape called this a $999 assumed-maker win. Calibration must not
    # treat that PnL as a Yes/No outcome — only the official print.
    assert fade["settlement_print"] == 77300.0

    eth = by_ticker["KXETHD-26SEP0917-T2399"]
    assert eth["settlement_print"] == 2395.10
    assert eth["settled_yes"] is False
    assert official_yes(settlement_print=2395.10, strike=2399.99) is False


def test_print_equals_strike_is_no():
    index = load_settlements([])
    ingest_settlement_row(
        index,
        {"ticker": "T", "settlement_print": 100.5},
        source="fixture",
    )
    rows = [
        {
            "ticker": "T",
            "asset": "BTC",
            "strike": 100.5,
            "model_prob": 0.4,
            "settled_yes": None,
        }
    ]
    apply_settlements(rows, index)
    assert rows[0]["settled_yes"] is False
    assert rows[0]["win_vs_model"] is True


def test_paper_pnl_alone_is_not_a_settlement():
    index = load_settlements([])
    ingest_settlement_row(
        index,
        {"ticker": "T", "result": "win", "pnl": 12.5, "fill_model": "assumed-maker-fill"},
        source="paper_log.jsonl",
    )
    assert index.by_ticker == {}


def test_buckets_withhold_rates_under_20():
    thin = [_scored_row(model_prob=0.12, settled_yes=True) for _ in range(19)]
    fat = [_scored_row(model_prob=0.33, settled_yes=i < 8, abs_z=1.2) for i in range(20)]
    summary = summarize_buckets(thin + fat)
    assert summary["used_paper_pnl"] is False
    assert summary["n_scored"] == 39
    assert summary["enough_for_rate"] is True
    by_p = {slot["bucket"]: slot for slot in summary["by_model_prob"]}
    assert by_p["0.10-0.20"]["n"] == 19
    assert by_p["0.10-0.20"]["thin"] is True
    assert by_p["0.10-0.20"]["yes_rate"] is None
    assert by_p["0.10-0.20"]["majority_hit_rate"] is None
    assert by_p["0.30-0.40"]["n"] == 20
    assert by_p["0.30-0.40"]["enough_for_rate"] is True
    assert by_p["0.30-0.40"]["yes_rate"] == 0.4
    assert by_p["0.30-0.40"]["mean_model_prob"] == 0.33
    by_z = {slot["bucket"]: slot for slot in summary["by_abs_z"]}
    assert by_z["0.00-0.50"]["thin"] is True
    assert by_z["1.00-1.50"]["enough_for_rate"] is True
    assert by_z["1.00-1.50"]["yes_rate"] == 0.4


def test_overall_rate_withheld_under_20():
    rows = [_scored_row(model_prob=0.55, settled_yes=True) for _ in range(19)]
    summary = summarize_buckets(rows)
    assert summary["enough_for_rate"] is False
    assert summary["overall_yes_rate"] is None
    assert summary["overall_majority_hit_rate"] is None
    text = format_calibration_report(summary, artifacts=Path("artifacts"))
    assert "below 20" in text
    assert "not paper PnL" in text
    assert "thin (need 20" in text


def test_proxy_rows_excluded_from_rates_by_default():
    rows = [_scored_row(model_prob=0.35, settled_yes=True, proxy=True) for _ in range(20)]
    hidden = summarize_buckets(rows, require_index=True)
    assert hidden["n_scored"] == 0
    assert hidden["n_proxy_excluded"] == 20
    shown = summarize_buckets(rows, require_index=False)
    assert shown["n_scored"] == 20
    assert shown["overall_yes_rate"] == 1.0


def test_fifteen_ideas_only_log_cannot_expand_strikes():
    snapshots = load_scan_snapshots(FIXTURES / "fifteen_scan_log.jsonl")
    assert expand_scans(snapshots) == []
    notes = collect_limitations(snapshots, fifteen=True)
    assert any("markets[]" in note for note in notes)


def test_fifteen_scan_with_markets_uses_minutes():
    snapshots = load_scan_snapshots(FIXTURES / "fifteen_scan_with_markets.jsonl")
    rows = expand_scans(snapshots)
    assert len(rows) == 2
    assert all(row["horizon"] == "15m" for row in rows)
    assert all(row["minutes_left"] == 12.0 for row in rows)
    taken = {row["ticker"]: row["taken_or_not"] for row in rows}
    assert taken["KXBTC15M-26SEP091615-T76500"] is True
    assert taken["KXBTC15M-26SEP091615-T77250"] is False


def test_run_calibration_writes_rows_and_ignores_paper_pnl(tmp_path):
    arts = tmp_path / "artifacts"
    arts.mkdir()
    (arts / "scan_log.jsonl").write_text((FIXTURES / "scan_log.jsonl").read_text())
    (arts / "settlements.jsonl").write_text((FIXTURES / "settlements.jsonl").read_text())
    (arts / "paper_log.jsonl").write_text((FIXTURES / "paper_log.jsonl").read_text())
    (arts / "last_run.json").write_text((FIXTURES / "last_run.json").read_text())

    rows, summary, report = run_calibration(artifacts=arts)
    assert summary["used_paper_pnl"] is False
    assert summary["n_rows"] == 3
    assert summary["n_taken"] == 0  # last scan had no idea
    assert summary["n_scored"] == 3
    assert summary["enough_for_rate"] is False
    assert (arts / "calibration_rows.jsonl").is_file()
    assert "not paper PnL" in report
    assert "thin" in report or "below 20" in report
    written = [row for row in rows if row["ticker"].endswith("T77250")]
    assert written[0]["settled_yes"] is True
    assert "pnl" not in written[0]


def test_fetch_prints_fills_closed_windows_only():
    rows = [
        {
            "ticker": "KXBTCD-CLOSED",
            "asset": "BTC",
            "strike": 100.0,
            "model_prob": 0.4,
            "close_time": "2020-01-01T12:00:00-05:00",
            "settled_yes": None,
        },
        {
            "ticker": "KXBTCD-OPEN",
            "asset": "BTC",
            "strike": 100.0,
            "model_prob": 0.4,
            "close_time": "2099-01-01T12:00:00-05:00",
            "settled_yes": None,
        },
    ]
    calls: list[tuple[str, object]] = []

    def get_print(asset: str, close: object) -> float:
        calls.append((asset, close))
        return 101.0

    extra = fetch_missing_prints(rows, get_print, now=datetime.fromisoformat("2026-09-09T12:00:00-04:00"))
    apply_settlements(rows, extra)
    assert len(calls) == 1
    assert rows[0]["settled_yes"] is True
    assert rows[1]["settled_yes"] is None


def test_cli_calibrate_reads_artifacts(tmp_path, capsys):
    from src.main import main

    arts = tmp_path / "artifacts"
    arts.mkdir()
    (arts / "scan_log.jsonl").write_text((FIXTURES / "scan_log.jsonl").read_text())
    (arts / "settlements.jsonl").write_text((FIXTURES / "settlements.jsonl").read_text())
    assert main(["calibrate", "--artifacts", str(arts)]) == 0
    out = capsys.readouterr().out
    assert "Fair-value calibration" in out
    assert "Expanded rows: 3" in out
