"""Hourly pot writer: win/loss apply and ledger idempotency."""

from __future__ import annotations

import json
from pathlib import Path

from src.config import HourlySettings
from src.evaluate import run_eval
from src.hourly_pot import (
    apply_filled_trades,
    load_pot,
    pot_path_from_settings,
    reconcile_hourly_pot,
    save_pot,
    trade_key,
)
from src.journal import estimate_pnl, new_trade_row, write_trades
from src.main import run_scan


def _filled(result: str, pnl: float, **kwargs) -> dict:
    row = new_trade_row(
        ticker=kwargs.pop("ticker", "KXBTCD-26SEP0917-T77300"),
        asset=kwargs.pop("asset", "BTC"),
        side=kwargs.pop("side", "Yes"),
        strike=77300.0,
        spot=78100.0,
        minutes_left=40.0,
        fair=0.62,
        kalshi_price=0.48,
        limit_price=0.48,
        contracts=kwargs.pop("contracts", 1),
        risk_dollars=kwargs.pop("risk_dollars", 0.48),
        hourly_vol=0.004,
        source="cfbenchmarks",
        order_id=kwargs.pop("order_id", "ord-win"),
        fill_status="filled",
        filled_contracts=1,
    )
    row["result"] = result
    row["pnl"] = pnl
    row["resolved_ts"] = "2026-09-09 06:00 PM EDT"
    row["resolved_ts_iso"] = "2026-09-09T18:00:00-04:00"
    row.update(kwargs)
    return row


def test_apply_win_credits_balance_and_ledger(tmp_path: Path):
    path = tmp_path / "hourly_pot.json"
    save_pot(
        {
            "balance_usd": 5.0,
            "start_usd": 5.0,
            "realized_pnl_usd": 0.0,
            "status": "ok",
            "ledger": [],
            "notes": ["fresh-start"],
            "risk_max_usd": 2.0,
            "risk_open_usd": 0.0,
        },
        path,
    )
    row = _filled("win", 0.52, order_id="fill-52")
    pot, applied = reconcile_hourly_pot(path, [row], start_usd=5.0)
    assert len(applied) == 1
    assert pot["balance_usd"] == 5.52
    assert pot["start_usd"] == 5.0
    assert pot["realized_pnl_usd"] == 0.52
    assert pot["ledger"][0]["id"] == trade_key(row)
    assert pot["ledger"][0]["pnl"] == 0.52
    assert pot["notes"][-1] == "KXBTCD-26SEP0917-T77300 win +0.52"
    reloaded = load_pot(path, start_usd=5.0)
    assert reloaded["balance_usd"] == 5.52
    assert reloaded["risk_max_usd"] == 2.0
    assert reloaded["risk_open_usd"] == 0.0
    assert reloaded["status"] == "ok"


def test_apply_loss_debits_balance(tmp_path: Path):
    path = tmp_path / "hourly_pot.json"
    save_pot({"balance_usd": 5.0, "start_usd": 5.0, "ledger": [], "notes": []}, path)
    row = _filled("loss", -1.75, order_id="ord-loss", risk_dollars=1.75)
    pot, applied = reconcile_hourly_pot(path, [row], start_usd=5.0)
    assert len(applied) == 1
    assert pot["balance_usd"] == 3.25
    assert pot["realized_pnl_usd"] == -1.75
    assert pot["notes"][-1] == "KXBTCD-26SEP0917-T77300 loss -1.75"


def test_same_trade_is_not_double_counted(tmp_path: Path):
    path = tmp_path / "hourly_pot.json"
    save_pot({"balance_usd": 5.0, "start_usd": 5.0, "ledger": [], "notes": []}, path)
    row = _filled("win", 0.52, order_id="same-fill")
    reconcile_hourly_pot(path, [row], start_usd=5.0)
    pot, applied = reconcile_hourly_pot(path, [row], start_usd=5.0)
    assert applied == []
    assert pot["balance_usd"] == 5.52
    assert len(pot["ledger"]) == 1

    pot2 = load_pot(path, start_usd=5.0)
    again = apply_filled_trades(pot2, [row, row])
    assert again == []
    assert pot2["balance_usd"] == 5.52


def test_unfilled_and_paper_and_exit_are_ignored(tmp_path: Path):
    path = tmp_path / "hourly_pot.json"
    save_pot({"balance_usd": 5.0, "start_usd": 5.0, "ledger": [], "notes": []}, path)
    rows = [
        _filled("unfilled", 0.0, order_id="no-fill"),
        {**_filled("win", 0.99, order_id="paper-1"), "kind": "paper"},
        {**_filled("win", 0.40, order_id="back-1"), "kind": "backfill"},
        {**_filled("win", 0.33, order_id="exit-1"), "action": "exit"},
        {**_filled("win", 1.11, order_id="recon-1"), "spot_source": "kalshi-fill-backfill"},
    ]
    rows[0]["result"] = "unfilled"
    rows[0]["fill_status"] = "unfilled"
    pot, applied = reconcile_hourly_pot(path, rows, start_usd=5.0)
    assert applied == []
    assert pot["balance_usd"] == 5.0
    assert pot["ledger"] == []


def test_legacy_win_without_fill_status_still_credits():
    pot = {"balance_usd": 5.0, "start_usd": 5.0, "realized_pnl_usd": 0.0, "ledger": [], "notes": []}
    row = {
        "ticker": "KXETHD-LEGACY",
        "result": "win",
        "pnl": 0.52,
        "order_id": "legacy-1",
        "ts": "2026-09-09 05:03 PM EDT",
    }
    applied = apply_filled_trades(pot, [row])
    assert len(applied) == 1
    assert pot["balance_usd"] == 5.52


def test_missing_pot_file_is_created_from_bankroll_plus_pnl(tmp_path: Path):
    path = tmp_path / "hourly_pot.json"
    row = _filled("win", 0.52, order_id="first")
    pot, applied = reconcile_hourly_pot(path, [row], start_usd=40.0)
    assert path.is_file()
    assert len(applied) == 1
    assert pot["start_usd"] == 40.0
    assert pot["balance_usd"] == 40.52


def test_missing_pot_file_stays_missing_when_journal_empty(tmp_path: Path):
    path = tmp_path / "hourly_pot.json"
    pot, applied = reconcile_hourly_pot(path, [], start_usd=40.0)
    assert applied == []
    assert pot["balance_usd"] == 40.0
    assert not path.is_file()


def test_pot_path_follows_artifacts_dir():
    settings = HourlySettings(_env_file=None, artifacts_dir="/tmp/kb-arts")
    assert pot_path_from_settings(settings) == Path("/tmp/kb-arts/hourly_pot.json")


def test_eval_reconciles_already_settled_journal(tmp_path: Path, capsys):
    write_trades(tmp_path / "trade_log.jsonl", [_filled("win", 0.52, order_id="eval-1")])
    save_pot(
        {"balance_usd": 5.0, "start_usd": 5.0, "ledger": [], "notes": []},
        tmp_path / "hourly_pot.json",
    )
    settings = HourlySettings(
        _env_file=None,
        artifacts_dir=str(tmp_path),
        scan_log_path=str(tmp_path / "scan_log.jsonl"),
        paper_log_path=str(tmp_path / "paper_log.jsonl"),
        pot_path=str(tmp_path / "hourly_pot.json"),
        bankroll=5.0,
    )
    assert run_eval(settings) == 0
    pot = load_pot(tmp_path / "hourly_pot.json", start_usd=5.0)
    assert pot["balance_usd"] == 5.52
    out = capsys.readouterr().out
    assert "hourly pot" in out
    assert "+0.52" in out


def test_run_scan_settlement_credits_hourly_pot(monkeypatch, tmp_path: Path):
    ticker = "KXBTCD-26SEP0917-T77300"
    pending = new_trade_row(
        ticker=ticker,
        asset="BTC",
        side="Yes",
        strike=77300.0,
        spot=78100.0,
        minutes_left=40.0,
        fair=0.62,
        kalshi_price=0.48,
        limit_price=0.48,
        contracts=1,
        risk_dollars=0.48,
        hourly_vol=0.004,
        source="cfbenchmarks",
        order_id="live-hourly",
        fill_status="filled",
        filled_contracts=1,
    )
    write_trades(tmp_path / "trade_log.jsonl", [pending])
    save_pot(
        {"balance_usd": 5.0, "start_usd": 5.0, "ledger": [], "notes": []},
        tmp_path / "hourly_pot.json",
    )

    class FakeClient:
        can_trade = True

        def get_fills(self, limit=50):
            return [{"ticker": ticker}]

        def get_market(self, got):
            assert got == ticker
            return {"result": "yes", "status": "determined"}

        def close(self):
            pass

    class FakeSpots:
        prices = {"BTC": 78100.0}
        sources = {"BTC": "cfbenchmarks"}
        source = "cfbenchmarks"
        hourly_vol = {"BTC": 0.004}
        vol_source = {}
        note = ""

        def settlement_ok(self, asset):
            return True

        def snapshot(self, *args, **kwargs):
            return self

        def close(self):
            pass

    class FakeDiscovery:
        def discover(self, *args, **kwargs):
            return []

        def next_settlements(self, markets):
            return []

    monkeypatch.setattr("src.main.KalshiClient", lambda *a, **k: FakeClient())
    monkeypatch.setattr("src.main.SpotService", lambda *a, **k: FakeSpots())
    monkeypatch.setattr("src.main.MarketDiscovery", lambda *a, **k: FakeDiscovery())
    monkeypatch.setattr("src.main.try_settle_paper", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.main.manage_open_positions",
        lambda *a, **k: {"signals": [], "placed": [], "errors": [], "dry_run": [], "journal": []},
    )

    settings = HourlySettings(
        _env_file=None,
        artifacts_dir=str(tmp_path),
        state_path=str(tmp_path / "state.json"),
        paper_log_path=str(tmp_path / "paper_log.jsonl"),
        scan_log_path=str(tmp_path / "scan_log.jsonl"),
        pot_path=str(tmp_path / "hourly_pot.json"),
        bankroll=5.0,
        halted=True,
    )
    assert run_scan(settings, asset="BTC", place=False, force_live=False) == 0
    rows = json.loads((tmp_path / "trade_log.jsonl").read_text().splitlines()[0])
    assert rows["result"] == "win"
    expected = estimate_pnl(won=True, contracts=1, entry_price=0.48, risk_dollars=0.48)
    assert rows["pnl"] == expected
    pot = load_pot(tmp_path / "hourly_pot.json", start_usd=5.0)
    assert pot["balance_usd"] == round(5.0 + expected, 4)
    assert len(pot["ledger"]) == 1
    assert pot["ledger"][0]["result"] == "win"

    assert run_scan(settings, asset="BTC", place=False, force_live=False) == 0
    pot2 = load_pot(tmp_path / "hourly_pot.json", start_usd=5.0)
    assert pot2["balance_usd"] == pot["balance_usd"]
    assert len(pot2["ledger"]) == 1
