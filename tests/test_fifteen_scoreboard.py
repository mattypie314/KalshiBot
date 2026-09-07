"""Termius 15m scoreboards: skip backfills, show pending, real live gates."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.fifteen.config import FifteenSettings
from src.fifteen.main import main, normalize_argv
from src.fifteen.pot import FifteenPot, save_pot
from src.fifteen.regime import CHOP_VETO_PHRASE
from src.fifteen.scoreboard import (
    classify_sit_note,
    format_scoreboard,
    gate_status,
    is_pending_ticket,
    merge_timeline,
    play_events_from_journal,
    run_live_score,
    run_paper_score,
    sit_events_from_scans,
)
from src.journal import new_backfill_row, new_trade_row, play_pot_equity, write_trades
from src.paper import new_paper_row

ET = ZoneInfo("America/New_York")


def _settings(tmp_path: Path, **kwargs) -> FifteenSettings:
    defaults = dict(
        _env_file=None,
        artifacts_dir=str(tmp_path),
        state_path=str(tmp_path / "fifteen_state.json"),
        pot_path=str(tmp_path / "fifteen_pot.json"),
        trade_log_path=str(tmp_path / "fifteen_trade_log.jsonl"),
        paper_log_path=str(tmp_path / "fifteen_paper_log.jsonl"),
        scan_log_path=str(tmp_path / "fifteen_scan_log.jsonl"),
        halted=True,
        live_trading=False,
        confirm_live="NO",
        pot_start=5.0,
        pot_double=10.0,
    )
    defaults.update(kwargs)
    return FifteenSettings(**defaults)


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
    row = new_backfill_row(
        fill={
            "ticker": "KXBTC15M-RECON",
            "order_id": "ord-recon",
            "fill_id": "fill-recon",
            "side": "yes",
            "count": "0.00",
            "count_fp": "2.00",
            "yes_price": "0.0100",
            "created_time": "2026-09-07T14:14:00Z",
        }
    )
    row["result"] = "loss"
    row["pnl"] = -0.0
    row["fill_status"] = "filled"
    return row


def test_gate_status_reads_env_not_hardcoded_off():
    halted = gate_status(halted=True, live_trading=True, confirm_live="YES")
    assert halted["label"] == "HALTED"
    assert halted["armed"] is False
    armed = gate_status(halted=False, live_trading=True, confirm_live="YES")
    assert armed["label"] == "LIVE"
    assert armed["armed"] is True
    confirm = gate_status(halted=False, live_trading=True, confirm_live="NO")
    assert confirm["label"] == "CONFIRM"
    off = gate_status(halted=False, live_trading=False, confirm_live="NO")
    assert off["label"] == "OFF"


def test_live_board_skips_backfill_and_shows_armed_gates():
    settings = _settings(
        Path("/tmp"),
        halted=False,
        live_trading=True,
        confirm_live="YES",
    )
    live = _live_win()
    backfill = _backfill_loss()
    scans = [
        {
            "ts": "2026-09-07 10:01 AM EDT",
            "window_id": "2026-09-07T10:00:00-04:00",
            "ideas": [],
            "notes": [f"KXBTC15M-CHOP: {CHOP_VETO_PHRASE} (CHOP ADX 12.0)"],
        }
    ]
    text = format_scoreboard(
        paper=False,
        settings=settings,
        journal_rows=[live, backfill],
        scan_rows=scans,
        color=False,
        now=datetime(2026, 9, 7, 16, 0, tzinfo=ET),
    )
    assert "KB15 LIVE SCOREBOARD" in text
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
    # Fake $0 recon loss must not become a play loss.
    assert "0W-1L" not in text
    assert "1W-1L" not in text


def test_paper_board_shows_pending_and_chop_sit_win():
    pending = new_paper_row(
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
    pending["ts_iso"] = "2026-09-07T10:31:00-04:00"
    won = new_paper_row(
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
    won["result"] = "win"
    won["pnl"] = 0.92
    won["fill_status"] = "assumed-maker-fill"
    won["ts_iso"] = "2026-09-07T10:16:00-04:00"
    won["resolved_ts_iso"] = "2026-09-07T10:15:00-04:00"
    backfill = _backfill_loss()
    scans = [
        {
            "ts": "2026-09-07 10:01 AM EDT",
            "window_id": "2026-09-07T10:00:00-04:00",
            "notes": [
                f"KXBTC15M-CHOP: {CHOP_VETO_PHRASE} (CHOP)",
                "outside entry window (minute 8; want 2-4)",
            ],
        }
    ]
    settings = _settings(Path("/tmp"), halted=True, live_trading=False, confirm_live="NO")
    text = format_scoreboard(
        paper=True,
        settings=settings,
        journal_rows=[won, pending, backfill],
        scan_rows=scans,
        color=False,
        now=datetime(2026, 9, 7, 16, 0, tzinfo=ET),
    )
    assert "KB15 PAPER SCOREBOARD" in text
    assert "PENDING" in text
    assert "KXETH15M-PEND" in text
    assert "waiting on the official BRTI / ETHUSD_RTI" in text
    assert "KXBTC15M-WIN" in text
    assert "KXBTC15M-RECON" not in text
    assert "outside entry window" not in text
    assert "chop veto" in text
    assert "→  HALTED" in text
    assert is_pending_ticket(pending) is True
    assert is_pending_ticket(won) is False
    assert is_pending_ticket(backfill) is False


def test_classify_sit_note_keeps_chop_drops_timer_noise():
    assert classify_sit_note(f"KXBTC15M-1: {CHOP_VETO_PHRASE} (CHOP)") == "chop"
    assert classify_sit_note("BTC: PROXY spot — sit") == "proxy"
    assert classify_sit_note("outside entry window (minute 8; want 2-4)") is None
    assert classify_sit_note("already working a 15m ticket this window") is None


def test_play_events_skip_backfills():
    events = play_events_from_journal([_live_win(), _backfill_loss()], paper=False)
    assert [event.ticker for event in events] == ["KXBTC15M-PLAY"]
    window = events[0].window_id
    sits = sit_events_from_scans(
        [
            {
                "ts": "2026-09-07 10:16 AM EDT",
                "window_id": window,
                "notes": ["KXBTC15M-PLAY: PASS but chop veto"],
            }
        ]
    )
    merged = merge_timeline(events, sits)
    # Same ticker+window as a PLAY is not double-counted as a sit.
    assert [event.kind for event in merged] == ["PLAY"]


def test_run_paper_and_live_score_cli(tmp_path, capsys):
    settings = _settings(tmp_path, halted=False, live_trading=True, confirm_live="YES")
    write_trades(tmp_path / "fifteen_paper_log.jsonl", [_live_win(kind="paper")])
    write_trades(tmp_path / "fifteen_trade_log.jsonl", [_live_win(), _backfill_loss()])
    (tmp_path / "fifteen_scan_log.jsonl").write_text(
        '{"ts":"2026-09-07 10:01 AM EDT","window_id":"2026-09-07T10:00:00-04:00",'
        f'"notes":["KXETH15M-CHOP: {CHOP_VETO_PHRASE}"]}}\n'
    )
    assert run_paper_score(settings, color=False) == 0
    paper_out = capsys.readouterr().out
    assert "KB15 PAPER SCOREBOARD" in paper_out
    assert run_live_score(settings, color=False) == 0
    live_out = capsys.readouterr().out
    assert "KB15 LIVE SCOREBOARD" in live_out
    assert "KXBTC15M-RECON" not in live_out
    assert "→  LIVE" in live_out
    assert "KXETH15M-CHOP" in live_out or "chop veto" in live_out


def test_cli_aliases_dispatch_to_boards(monkeypatch, tmp_path, capsys):
    settings = _settings(tmp_path, halted=True, live_trading=False, confirm_live="NO")
    monkeypatch.setattr("src.fifteen.main.load_fifteen_settings", lambda: settings)
    assert normalize_argv(["kbscore"]) == ["score"]
    assert normalize_argv(["paper-score"]) == ["score"]
    assert normalize_argv(["kbscore-live"]) == ["livescore"]
    assert normalize_argv(["live-score"]) == ["livescore"]
    assert main(["score"]) == 0
    assert "KB15 PAPER SCOREBOARD" in capsys.readouterr().out
    assert main(["livescore"]) == 0
    live_out = capsys.readouterr().out
    assert "KB15 LIVE SCOREBOARD" in live_out
    assert "HALTED=true" in live_out
    assert "Live OFF (halted)" not in live_out


def test_live_board_is_read_only_and_follows_play_pot_when_file_drifts(tmp_path, capsys):
    settings = _settings(tmp_path, halted=False, live_trading=True, confirm_live="YES")
    rows = [_live_win(), _backfill_loss()]
    write_trades(tmp_path / "fifteen_trade_log.jsonl", rows)
    drifted = FifteenPot(balance=9.99, start=5.0, realized_pnl=4.99, stopped=False)
    save_pot(drifted, settings.pot_path)
    before_journal = (tmp_path / "fifteen_trade_log.jsonl").read_text()
    before_pot = Path(settings.pot_path).read_text()
    play_only = play_pot_equity(rows, start=5.0)
    assert play_only == 5.92
    assert run_live_score(settings, color=False) == 0
    out = capsys.readouterr().out
    assert f"Pot     ${play_only:.2f}" in out
    assert "fifteen_pot.json $9.99" in out
    assert "display uses play-only $5.92" in out
    assert "file not changed" in out
    assert (tmp_path / "fifteen_trade_log.jsonl").read_text() == before_journal
    assert Path(settings.pot_path).read_text() == before_pot
    assert '"kind": "backfill"' in before_journal
    assert "KXBTC15M-RECON" not in out
