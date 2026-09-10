"""Combined live scoreboard: two pots, labeled plays, no paper mix-in."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.scoreboard_all import (
    BOT_15M,
    BOT_1H,
    filter_live_rows,
    find_hourly_pot,
    format_combined_scoreboard,
    is_live_play_row,
    is_paper_row,
    last_scan_lines,
    load_last_scan_row,
    load_snapshots,
    main,
    resolve_pot,
    snapshot_bot,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 7, 15, 30, tzinfo=ET)


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")
    return path


def _live(
    *,
    ticker: str,
    asset: str,
    side: str = "Yes",
    result: str = "win",
    pnl: float | None = 0.5,
    fill: str = "filled",
    ts: str = "2026-09-07T14:15:00-04:00",
    **extra: object,
) -> dict:
    row = {
        "ts_iso": ts,
        "ticker": ticker,
        "asset": asset,
        "side": side,
        "limit_price": 0.42,
        "fill_status": fill,
        "result": result,
        "pnl": pnl,
    }
    row.update(extra)
    return row


def test_is_live_play_row_skips_paper_exit_and_backfill():
    assert is_paper_row({"kind": "paper", "pnl": 99})
    assert not is_live_play_row({"kind": "paper", "result": "win", "pnl": 99})
    assert not is_live_play_row({"action": "exit", "ticker": "KXBTCD-1"})
    assert not is_live_play_row({"kind": "backfill", "pnl": 0, "result": "loss"})
    assert not is_live_play_row({"spot_source": "kalshi-fill-backfill", "pnl": 0})
    assert is_live_play_row(_live(ticker="KXBTC15M-1", asset="BTC"))


def test_filter_live_rows_counts_skipped_paper():
    rows = [
        _live(ticker="KXBTC15M-1", asset="BTC"),
        {"kind": "paper", "ticker": "PAPER-1", "result": "win", "pnl": 50, "fill_status": "filled"},
        {"action": "exit", "ticker": "KXBTC15M-1"},
    ]
    live, paper, other = filter_live_rows(rows)
    assert [row["ticker"] for row in live] == ["KXBTC15M-1"]
    assert paper == 1
    assert other == 1


def test_resolve_pot_reads_fifteen_balance_and_hourly_balance_usd(tmp_path):
    fifteen = tmp_path / "fifteen_pot.json"
    _write_json(fifteen, {"balance": 6.25, "start": 5.0, "realized_pnl": 1.25, "double_at": 10})
    pot, start, ask, realized, source, missing = resolve_pot(
        pot_path=fifteen,
        start_default=5.0,
        ask_default=10.0,
        filled_pnl=1.25,
    )
    assert (pot, start, ask, realized, missing) == (6.25, 5.0, 10.0, 1.25, False)
    assert source == "fifteen_pot.json"

    hourly = tmp_path / "hourly_pot.json"
    _write_json(hourly, {"balance_usd": 41.5, "start_usd": 40.0, "realized_pnl_usd": 1.5})
    pot, start, ask, realized, source, missing = resolve_pot(
        pot_path=hourly,
        start_default=40.0,
        ask_default=80.0,
        filled_pnl=9.9,
    )
    assert pot == 41.5
    assert start == 40.0
    assert realized == 1.5
    assert source == "hourly_pot.json"
    assert missing is False


def test_resolve_pot_reconstructs_from_bankroll_when_file_missing(tmp_path):
    pot, start, ask, realized, source, missing = resolve_pot(
        pot_path=tmp_path / "hourly_pot.json",
        start_default=40.0,
        ask_default=80.0,
        filled_pnl=1.75,
        env_bankroll=40.0,
    )
    assert pot == 41.75
    assert start == 40.0
    assert realized == 1.75
    assert missing is True
    assert source == "BANKROLL+live pnl"


def test_find_hourly_pot_prefers_hourly_pot_json(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    assert find_hourly_pot(artifacts) is None
    (artifacts / "pot.json").write_text('{"balance": 1}\n')
    assert find_hourly_pot(artifacts).name == "pot.json"
    (artifacts / "hourly_pot.json").write_text('{"balance_usd": 40}\n')
    assert find_hourly_pot(artifacts).name == "hourly_pot.json"


def test_combined_board_labels_bots_and_totals_pots(tmp_path):
    fifteen_root = tmp_path / "KalshiBot15"
    hourly_root = tmp_path / "KalshiBot"
    _write_jsonl(
        fifteen_root / "artifacts" / "fifteen_trade_log.jsonl",
        [
            _live(
                ticker="KXBTC15M-26SEP071130-30",
                asset="BTC",
                side="Yes",
                result="win",
                pnl=0.58,
                ts="2026-09-07T11:15:00-04:00",
            ),
            _live(
                ticker="KXETH15M-26SEP071130-15",
                asset="ETH",
                side="No",
                result="loss",
                pnl=-0.75,
                ts="2026-09-07T11:20:00-04:00",
            ),
            {
                "kind": "paper",
                "ticker": "PAPER-SHOULD-NOT-SHOW",
                "asset": "BTC",
                "result": "win",
                "pnl": 99.0,
                "fill_status": "filled",
                "ts_iso": "2026-09-07T11:00:00-04:00",
            },
        ],
    )
    _write_json(
        fifteen_root / "artifacts" / "fifteen_pot.json",
        {"balance": 4.83, "start": 5.0, "realized_pnl": -0.17, "double_at": 10},
    )
    _write_jsonl(
        fifteen_root / "artifacts" / "fifteen_scan_log.jsonl",
        [
            {
                "ts": "2026-09-07 11:03 AM EDT",
                "mode": "live",
                "ideas": [
                    {
                        "ticker": "KXBTC15M-26SEP071130-30",
                        "side": "Yes",
                        "limit": 0.48,
                    }
                ],
                "notes": ["KXETH15M-26SEP071130-15: ETH Yes @19¢ under 45¢"],
            }
        ],
    )
    (fifteen_root / ".env").write_text("HALTED=true\nLIVE_TRADING=false\nCONFIRM_LIVE=NO\n")

    _write_jsonl(
        hourly_root / "artifacts" / "trade_log.jsonl",
        [
            _live(
                ticker="KXETHD-26SEP0715-20",
                asset="ETH",
                side="No",
                result="win",
                pnl=1.20,
                ts="2026-09-07T11:03:00-04:00",
            ),
            _live(
                ticker="KXBTCD-26SEP0716-30",
                asset="BTC",
                side="Yes",
                result="pending",
                pnl=None,
                fill="resting",
                ts="2026-09-07T15:03:00-04:00",
            ),
            {
                "kind": "paper",
                "ticker": "HOURLY-PAPER-NO",
                "asset": "BTC",
                "result": "win",
                "pnl": 40.0,
                "fill_status": "filled",
            },
        ],
    )
    _write_json(hourly_root / "artifacts" / "hourly_pot.json", {"balance_usd": 41.20, "start_usd": 40.0})
    _write_jsonl(
        hourly_root / "artifacts" / "scan_log.jsonl",
        [
            {
                "ts": "2026-09-07 11:01 AM EDT",
                "action": "scan",
                "ideas": [],
                "nearby": ["KXETHD close strike / net edge too thin"],
            }
        ],
    )
    (hourly_root / ".env").write_text("HALTED=true\nBANKROLL=40\nLIVE_TRADING=false\nCONFIRM_LIVE=NO\n")

    # Paper files exist but must not be opened / mixed in.
    _write_jsonl(
        fifteen_root / "artifacts" / "fifteen_paper_log.jsonl",
        [{"kind": "paper", "ticker": "FILE-PAPER-15", "result": "win", "pnl": 8}],
    )
    _write_jsonl(
        hourly_root / "artifacts" / "paper_log.jsonl",
        [{"kind": "paper", "ticker": "FILE-PAPER-1H", "result": "win", "pnl": 8}],
    )

    fifteen, hourly = load_snapshots(
        fifteen_root=fifteen_root,
        hourly_root=hourly_root,
        now=NOW,
    )
    assert fifteen.label == BOT_15M
    assert hourly.label == BOT_1H
    assert fifteen.pot == 4.83
    assert hourly.pot == 41.20
    assert fifteen.skipped_paper == 1
    assert hourly.skipped_paper == 1
    assert {event.ticker for event in fifteen.plays} == {
        "KXBTC15M-26SEP071130-30",
        "KXETH15M-26SEP071130-15",
    }
    assert "PAPER-SHOULD-NOT-SHOW" not in {event.ticker for event in fifteen.plays}
    assert "HOURLY-PAPER-NO" not in {event.ticker for event in hourly.plays}

    text = format_combined_scoreboard(fifteen, hourly, now=NOW, color=False)
    assert "KB COMBINED LIVE SCOREBOARD" in text
    assert "LIVE cash only" in text
    assert "ALL   pot $46.03" in text
    assert "15M  pot $4.83" in text
    assert "1H   pot $41.20" in text
    assert "PLAY 15M BTC" in text or "PLAY 15M" in text
    assert "PLAY 1H  ETH" in text or "PLAY 1H" in text
    assert "KXBTC15M-26SEP071130-30" in text
    assert "KXETHD-26SEP0715-20" in text
    assert "PAPER-SHOULD-NOT-SHOW" not in text
    assert "HOURLY-PAPER-NO" not in text
    assert "FILE-PAPER-15" not in text
    assert "FILE-PAPER-1H" not in text
    assert "fifteen_trade_log.jsonl" in text
    assert "hourly_pot.json" in text
    assert "HALTED" in text
    assert "paper tapes not opened" in text
    assert "LAST TICK  Pass / Sit" in text
    assert "ETH Yes @19¢ under 45¢" in text
    assert "KXBTC15M-26SEP071130-30" in text
    assert "Yes @0.48" in text
    assert "KXETHD close strike / net edge too thin" in text
    assert "score / livescore" in text


def test_hourly_snapshot_uses_bankroll_when_pot_file_absent(tmp_path):
    root = tmp_path / "KalshiBot"
    _write_jsonl(
        root / "artifacts" / "trade_log.jsonl",
        [_live(ticker="KXBTCD-1", asset="BTC", pnl=1.75, result="win")],
    )
    (root / ".env").write_text("BANKROLL=40\nHALTED=true\n")
    snap = snapshot_bot(
        label=BOT_1H,
        title="hourly",
        journal_path=root / "artifacts" / "trade_log.jsonl",
        pot_path=None,
        env_path=root / ".env",
        start_default=40.0,
        ask_default=80.0,
        bankroll_env_key="BANKROLL",
        now=NOW,
    )
    assert snap.pot == 41.75
    assert snap.pot_source == "BANKROLL+live pnl"
    assert snap.pot_file_missing is True


def test_missing_artifacts_do_not_crash(tmp_path):
    fifteen, hourly = load_snapshots(
        fifteen_root=tmp_path / "missing15",
        hourly_root=tmp_path / "missing1h",
        now=NOW,
    )
    text = format_combined_scoreboard(fifteen, hourly, now=NOW, color=False)
    assert "KB COMBINED LIVE SCOREBOARD" in text
    assert "no live PLAY rows yet" in text
    assert "LAST TICK  Pass / Sit" in text
    assert "no scan log yet" in text
    assert fifteen.journal_missing is True
    assert hourly.journal_missing is True
    assert fifteen.last_scan is None
    assert hourly.last_scan is None


def test_pi_wrappers_point_at_combined_module():
    root = Path(__file__).resolve().parents[1]
    wrapper = (root / "scripts" / "kbscore-all").read_text()
    installer = (root / "scripts" / "install-kbscore-all.sh").read_text()
    assert "src.scoreboard_all" in wrapper
    assert "FORCE_COLOR" in wrapper
    assert "kbscore-all" in installer
    assert "scoreall" in installer
    assert "kbscore-live" in installer
    assert "Does NOT replace" in installer or "Left alone" in installer


def test_cli_smoke_prints_board(tmp_path, capsys):
    fifteen_root = tmp_path / "15"
    hourly_root = tmp_path / "1h"
    _write_jsonl(
        fifteen_root / "artifacts" / "fifteen_trade_log.jsonl",
        [_live(ticker="KXBTC15M-X", asset="BTC", pnl=0.4)],
    )
    _write_json(fifteen_root / "artifacts" / "fifteen_pot.json", {"balance": 5.4})
    _write_jsonl(
        hourly_root / "artifacts" / "trade_log.jsonl",
        [_live(ticker="KXBTCD-X", asset="BTC", pnl=1.0)],
    )
    _write_json(hourly_root / "artifacts" / "hourly_pot.json", {"balance_usd": 41.0})
    code = main(
        [
            "--fifteen-root",
            str(fifteen_root),
            "--hourly-root",
            str(hourly_root),
            "--no-color",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "KB COMBINED LIVE SCOREBOARD" in out
    assert "ALL   pot $46.40" in out
    assert "KXBTC15M-X" in out
    assert "KXBTCD-X" in out


def test_load_last_scan_row_reads_latest_object(tmp_path):
    path = tmp_path / "fifteen_scan_log.jsonl"
    _write_jsonl(
        path,
        [
            {"ts": "2026-09-07 10:01 AM EDT", "mode": "scan", "ideas": [], "notes": ["old"]},
            {"ts": "2026-09-07 11:03 AM EDT", "mode": "live", "ideas": [{"ticker": "KXETH15M-1", "side": "Yes", "limit": 0.19}], "notes": ["ETH Yes @19¢ under 45¢"]},
        ],
    )
    row = load_last_scan_row(path)
    assert row is not None
    assert row["mode"] == "live"
    assert row["notes"] == ["ETH Yes @19¢ under 45¢"]
    assert load_last_scan_row(tmp_path / "missing.jsonl") is None


def test_last_scan_lines_show_pass_and_sit():
    fifteen = {
        "ts": "2026-09-07 11:03 AM EDT",
        "mode": "live",
        "ideas": [{"ticker": "KXBTC15M-1", "side": "Yes", "limit": 0.48}],
        "notes": ["KXETH15M-1: ETH Yes @19¢ under 45¢"],
    }
    hourly = {
        "ts": "2026-09-07 11:01 AM EDT",
        "action": "scan",
        "ideas": [],
        "nearby": ["KXETHD close strike / net edge too thin"],
    }
    text = "\n".join(last_scan_lines(fifteen, hourly, color=False))
    assert "LAST TICK  Pass / Sit" in text
    assert "live" in text
    assert "scan" in text
    assert "KXBTC15M-1 Yes @0.48" in text
    assert "ETH Yes @19¢ under 45¢" in text
    assert "KXETHD close strike / net edge too thin" in text
    assert "score / livescore" in text
    empty = "\n".join(last_scan_lines(None, None, color=False))
    assert "no scan log yet" in empty
