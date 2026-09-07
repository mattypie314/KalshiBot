"""Termius scoreboards: paper/live split, 15m + hourly, combined totals."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.fifteen.main import normalize_argv as fifteen_normalize
from src.fifteen.regime import CHOP_VETO_PHRASE
from src.journal import new_trade_row, write_trades
from src.main import normalize_argv as hourly_normalize
from src.paper import new_paper_row
from src.scoreboard import (
    assert_tape_path,
    format_combined_board,
    format_single_board,
    gate_status,
    is_journal_backfill,
    is_live_play_row,
    load_bot_tape,
    main as scoreboard_main,
    render_board,
)

ET = ZoneInfo("America/New_York")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_trades(path, rows)


def _write_env(root: Path, text: str) -> None:
    (root / ".env").write_text(text)


def _live_win(**kwargs) -> dict:
    row = new_trade_row(
        ticker="KXBTC15M-PLAY",
        asset="BTC",
        side="Yes",
        strike=64000.0,
        spot=65000.0,
        minutes_left=12.0,
        fair=0.62,
        kalshi_price=0.54,
        limit_price=0.54,
        contracts=2,
        risk_dollars=1.08,
        hourly_vol=0.004,
        source="cfbenchmarks",
        order_id="live-play",
        fill_status="filled",
    )
    row["result"] = "win"
    row["pnl"] = 0.92
    row["ts_iso"] = "2026-09-07T10:16:00-04:00"
    row["resolved_ts_iso"] = "2026-09-07T10:15:00-04:00"
    row.update(kwargs)
    return row


def _backfill_loss() -> dict:
    return {
        "kind": "backfill",
        "ticker": "KXBTC15M-RECON",
        "asset": "BTC",
        "side": "yes",
        "result": "loss",
        "pnl": -0.0,
        "fill_status": "filled",
        "ts_iso": "2026-09-07T10:14:00-04:00",
    }


def _paper_win(**kwargs) -> dict:
    row = new_paper_row(
        ticker="KXBTC15M-WIN",
        asset="BTC",
        side="Yes",
        strike=64000.0,
        spot=65000.0,
        spot_source="BRTI",
        minutes_left=12.0,
        fair=0.62,
        kalshi_price=0.54,
        limit_price=0.54,
        contracts=2,
        risk_dollars=1.08,
        net_edge=0.08,
        close_time="2026-09-07T10:15:00-04:00",
    )
    row["result"] = "win"
    row["pnl"] = 0.92
    row["fill_status"] = "assumed-maker-fill"
    row["ts_iso"] = "2026-09-07T10:16:00-04:00"
    row["resolved_ts_iso"] = "2026-09-07T10:15:00-04:00"
    row.update(kwargs)
    return row


def _paper_pending() -> dict:
    row = new_paper_row(
        ticker="KXETH15M-PEND",
        asset="ETH",
        side="No",
        strike=2400.0,
        spot=2390.0,
        spot_source="ETHUSD_RTI",
        minutes_left=12.0,
        fair=0.58,
        kalshi_price=0.41,
        limit_price=0.41,
        contracts=2,
        risk_dollars=0.82,
        net_edge=0.08,
        close_time="2026-09-07T10:45:00-04:00",
    )
    row["ts_iso"] = "2026-09-07T10:31:00-04:00"
    return row


def _hourly_live_loss() -> dict:
    row = new_trade_row(
        ticker="KXETHD-HOUR",
        asset="ETH",
        side="No",
        strike=2400.0,
        spot=2390.0,
        minutes_left=40.0,
        fair=0.61,
        kalshi_price=0.48,
        limit_price=0.48,
        contracts=3,
        risk_dollars=1.44,
        hourly_vol=0.005,
        source="cfbenchmarks",
        order_id="hourly-live",
        fill_status="filled",
    )
    row["result"] = "loss"
    row["pnl"] = -1.44
    row["ts_iso"] = "2026-09-07T11:04:00-04:00"
    row["resolved_ts_iso"] = "2026-09-07T12:00:00-04:00"
    return row


def _hourly_paper_win() -> dict:
    row = new_paper_row(
        ticker="KXBTCD-PAPER",
        asset="BTC",
        side="Yes",
        strike=64000.0,
        spot=65000.0,
        spot_source="BRTI",
        minutes_left=40.0,
        fair=0.62,
        kalshi_price=0.51,
        limit_price=0.51,
        contracts=3,
        risk_dollars=1.53,
        net_edge=0.08,
        close_time="2026-09-07T12:00:00-04:00",
    )
    row["result"] = "win"
    row["pnl"] = 1.47
    row["fill_status"] = "assumed-maker-fill"
    row["ts_iso"] = "2026-09-07T11:06:00-04:00"
    row["resolved_ts_iso"] = "2026-09-07T12:00:00-04:00"
    return row


def _fifteen_checkout(tmp: Path) -> Path:
    root = tmp / "KalshiBot15"
    arts = root / "artifacts"
    arts.mkdir(parents=True)
    _write_env(
        root,
        "HALTED=false\nLIVE_TRADING=true\nCONFIRM_LIVE=YES\n"
        "FIFTEEN_POT_START=5\nFIFTEEN_POT_DOUBLE=10\n",
    )
    (arts / "fifteen_pot.json").write_text(
        json.dumps({"balance": 5.92, "realized_pnl": 0.92, "stopped": False}) + "\n"
    )
    _write_jsonl(arts / "fifteen_trade_log.jsonl", [_live_win(), _backfill_loss()])
    _write_jsonl(arts / "fifteen_paper_log.jsonl", [_paper_win(), _paper_pending(), _backfill_loss()])
    _write_jsonl(
        arts / "fifteen_scan_log.jsonl",
        [
            {
                "ts": "2026-09-07 10:01 AM EDT",
                "window_id": "2026-09-07T10:00:00-04:00",
                "ideas": [],
                "notes": [
                    f"KXBTC15M-CHOP: {CHOP_VETO_PHRASE} (CHOP ADX 12.0)",
                    "outside entry window (minute 8; want 2-4)",
                ],
            }
        ],
    )
    return root


def _hourly_checkout(tmp: Path) -> Path:
    root = tmp / "KalshiBot"
    arts = root / "artifacts"
    arts.mkdir(parents=True)
    _write_env(root, "HALTED=true\nLIVE_TRADING=false\nCONFIRM_LIVE=NO\nBANKROLL=40\n")
    _write_jsonl(arts / "trade_log.jsonl", [_hourly_live_loss()])
    _write_jsonl(arts / "paper_log.jsonl", [_hourly_paper_win()])
    _write_jsonl(
        arts / "scan_log.jsonl",
        [
            {
                "ts": "2026-09-07 11:01 AM EDT",
                "ideas": [],
                "nearby": ["KXETHD close strike / net edge too thin"],
            }
        ],
    )
    return root


def test_gate_status_reads_env_not_hardcoded_off():
    halted = gate_status(halted=True, live_trading=True, confirm_live="YES")
    assert halted["label"] == "HALTED"
    armed = gate_status(halted=False, live_trading=True, confirm_live="YES")
    assert armed["label"] == "LIVE"
    assert armed["armed"] is True
    confirm = gate_status(halted=False, live_trading=True, confirm_live="NO")
    assert confirm["label"] == "CONFIRM"
    off = gate_status(halted=False, live_trading=False, confirm_live="NO")
    assert off["label"] == "OFF"


def test_backfill_is_not_a_live_play():
    assert is_journal_backfill(_backfill_loss()) is True
    assert is_live_play_row(_backfill_loss()) is False
    assert is_live_play_row(_live_win()) is True
    paper = _paper_win()
    assert is_live_play_row(paper) is False


def test_assert_tape_path_refuses_cross_journal():
    try:
        assert_tape_path(Path("artifacts/trade_log.jsonl"), paper=True)
        raise AssertionError("expected paper board to refuse live log")
    except ValueError as exc:
        assert "live journal" in str(exc)
    try:
        assert_tape_path(Path("artifacts/fifteen_paper_log.jsonl"), paper=False)
        raise AssertionError("expected live board to refuse paper log")
    except ValueError as exc:
        assert "paper journal" in str(exc)


def test_fifteen_live_skips_backfill_and_shows_armed_gates(tmp_path: Path):
    root = _fifteen_checkout(tmp_path)
    text = render_board(
        "livescore",
        fifteen_root=root,
        color=False,
        now=datetime(2026, 9, 7, 16, 0, tzinfo=ET),
    )
    assert "KB 15m LIVE SCOREBOARD" in text
    assert "KXBTC15M-PLAY" in text
    assert "KXBTC15M-RECON" not in text
    assert "Live OFF (halted)" not in text
    assert "HALTED=false" in text
    assert "LIVE_TRADING=true" in text
    assert "CONFIRM_LIVE=YES" in text
    assert "→  LIVE" in text
    assert "1W-0L" in text
    assert "skipped 1 kind=backfill" in text
    assert "chop veto" in text
    assert "process win" in text
    assert "+$0.92" in text
    assert "0W-1L" not in text
    assert "1W-1L" not in text
    assert "fifteen_paper_log" not in text
    assert "PAPER tape" not in text
    assert "not paper" in text


def test_fifteen_paper_shows_pending_and_omits_live_cash(tmp_path: Path):
    root = _fifteen_checkout(tmp_path)
    text = render_board(
        "score",
        fifteen_root=root,
        color=False,
        now=datetime(2026, 9, 7, 16, 0, tzinfo=ET),
    )
    assert "KB 15m PAPER SCOREBOARD" in text
    assert "KXETH15M-PEND" in text
    assert "PENDING" in text
    assert "chop veto" in text
    assert "fifteen_trade_log" not in text
    assert "LIVE tape" not in text
    assert "not live cash" in text
    assert "1W-0L" in text


def test_hourly_live_and_paper_stay_split(tmp_path: Path):
    root = _hourly_checkout(tmp_path)
    live = render_board(
        "livescore-hourly",
        hourly_root=root,
        color=False,
        now=datetime(2026, 9, 7, 16, 0, tzinfo=ET),
    )
    paper = render_board(
        "score-hourly",
        hourly_root=root,
        color=False,
        now=datetime(2026, 9, 7, 16, 0, tzinfo=ET),
    )
    assert "KB HOURLY LIVE SCOREBOARD" in live
    assert "KXETHD-HOUR" in live
    assert "KXBTCD-PAPER" not in live
    assert "paper_log" not in live
    assert "HALTED=true" in live
    assert "0W-1L" in live
    assert "-$1.44" in live

    assert "KB HOURLY PAPER SCOREBOARD" in paper
    assert "KXBTCD-PAPER" in paper
    assert "KXETHD-HOUR" not in paper
    assert "trade_log" not in paper
    assert "1W-0L" in paper
    assert "+$1.47" in paper


def test_combined_live_tags_bots_and_totals(tmp_path: Path):
    fifteen = _fifteen_checkout(tmp_path)
    hourly = _hourly_checkout(tmp_path)
    text = render_board(
        "livescore-all",
        fifteen_root=fifteen,
        hourly_root=hourly,
        color=False,
        now=datetime(2026, 9, 7, 16, 0, tzinfo=ET),
    )
    assert "KB COMBINED LIVE SCOREBOARD" in text
    assert "not paper" in text
    assert "paper_log" not in text
    assert "15m     pot $5.92" in text or "15m     pot $5.92" in text.replace("  ", " ")
    assert "hourly  pot $38.56" in text or "hourly" in text and "$38.56" in text
    assert "TOTAL   pot $44.48" in text or "TOTAL" in text and "$44.48" in text
    assert "1W-1L" in text
    assert "15m" in text
    assert "hourly" in text
    assert "KXBTC15M-PLAY" in text
    assert "KXETHD-HOUR" in text
    assert "KXBTC15M-RECON" not in text
    assert "KXBTCD-PAPER" not in text


def test_combined_paper_excludes_live_fills(tmp_path: Path):
    fifteen = _fifteen_checkout(tmp_path)
    hourly = _hourly_checkout(tmp_path)
    text = render_board(
        "scoreall",
        fifteen_root=fifteen,
        hourly_root=hourly,
        color=False,
        now=datetime(2026, 9, 7, 16, 0, tzinfo=ET),
    )
    assert "KB COMBINED PAPER SCOREBOARD" in text
    assert "not live cash" in text
    assert "KXBTC15M-WIN" in text
    assert "KXBTCD-PAPER" in text
    assert "KXETHD-HOUR" not in text
    assert "fifteen_trade_log" not in text
    assert "+$0.92" in text
    assert "+$1.47" in text
    assert "2W-0L" in text


def test_combined_refuses_mixed_tapes(tmp_path: Path):
    fifteen = load_bot_tape(bot="15m", paper=True, root=_fifteen_checkout(tmp_path))
    hourly = load_bot_tape(bot="hourly", paper=False, root=_hourly_checkout(tmp_path))
    try:
        format_combined_board([fifteen, hourly], paper=True, color=False)
        raise AssertionError("expected mixed paper/live to fail")
    except ValueError as exc:
        assert "mixed" in str(exc)


def test_single_board_empty_checkout_is_valid(tmp_path: Path):
    root = tmp_path / "empty15"
    root.mkdir()
    _write_env(root, "HALTED=true\n")
    tape = load_bot_tape(bot="15m", paper=False, root=root)
    text = format_single_board(tape, color=False, now=datetime(2026, 9, 7, 16, 0, tzinfo=ET))
    assert "0W-0L" in text
    assert "no PLAY or notable SIT" in text


def test_scoreboard_cli_smoke(tmp_path: Path, capsys):
    fifteen = _fifteen_checkout(tmp_path)
    hourly = _hourly_checkout(tmp_path)
    code = scoreboard_main(
        [
            "livescore-all",
            "--fifteen-root",
            str(fifteen),
            "--hourly-root",
            str(hourly),
            "--no-color",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "COMBINED LIVE" in out
    assert "15m" in out
    assert "hourly" in out


def test_kb15_score_is_termius_not_eval(tmp_path: Path, monkeypatch, capsys):
    from src.fifteen.main import main as fifteen_main

    root = _fifteen_checkout(tmp_path)
    monkeypatch.chdir(root)
    assert fifteen_main(["score"]) == 0
    out = capsys.readouterr().out
    assert "KB 15m PAPER SCOREBOARD" in out
    assert "=== 15m eval" not in out
    assert fifteen_main(["livescore"]) == 0
    live = capsys.readouterr().out
    assert "KB 15m LIVE SCOREBOARD" in live
    assert "=== 15m livescore" not in live


def test_hourly_kb_score_is_termius_not_eval(tmp_path: Path, monkeypatch, capsys):
    from src.main import main as hourly_main

    root = _hourly_checkout(tmp_path)
    monkeypatch.chdir(root)
    assert hourly_main(["score"]) == 0
    out = capsys.readouterr().out
    assert "KB HOURLY PAPER SCOREBOARD" in out
    assert "# Hourly BTC/ETH evaluation" not in out
    assert hourly_main(["livescore"]) == 0
    live = capsys.readouterr().out
    assert "KB HOURLY LIVE SCOREBOARD" in live
    assert "bankroll $40.00" in live


def test_cli_aliases_normalize():
    assert fifteen_normalize(["score"]) == ["score"]
    assert fifteen_normalize(["livescore"]) == ["livescore"]
    assert hourly_normalize(["score"]) == ["score"]
    assert hourly_normalize(["livescore"]) == ["livescore"]
    assert hourly_normalize(["score-hourly"]) == ["score"]
    assert hourly_normalize(["livescore-hourly"]) == ["livescore"]
