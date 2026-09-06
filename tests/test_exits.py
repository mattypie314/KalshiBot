"""99¢ cash-out, then 95¢+≤10m, then +2¢ TP. Live oneshots flatten."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src.exits import (
    CASH_OUT_LABEL,
    EARLY_CASH_OUT_LABEL,
    Holding,
    MANUAL_FLATTEN_LABEL,
    TAKE_PROFIT_LABEL,
    collect_holdings,
    detect_manual_flattens,
    exit_reason,
    fill_is_close,
    flatten_payload,
    manage_open_positions,
    minutes_until_settlement,
    place_flatten,
    post_only_would_take_exit,
    should_cash_out_99,
    should_cash_out_early,
    should_take_profit,
    signal_for_holding,
)
from src.config import HourlySettings
from src.fifteen.config import FifteenSettings
from src.fifteen_filters import should_cash_out_99 as fifteen_cash_out
from src.fifteen_filters import should_cash_out_early as fifteen_cash_out_early
from src.fifteen_filters import should_take_profit as fifteen_take_profit
from src.journal import load_trades


def test_cash_out_bid_default_is_99_cents():
    hourly = HourlySettings(_env_file=None)
    fifteen = FifteenSettings(_env_file=None)
    assert hourly.cash_out_bid == 0.99
    assert hourly.take_profit_cents == 0.02
    assert hourly.early_cash_out_bid == 0.95
    assert hourly.early_cash_out_minutes == 10.0
    assert fifteen.cash_out_bid == 0.99
    assert fifteen.take_profit_cents == 0.02
    assert fifteen.early_cash_out_bid == 0.95
    assert fifteen.early_cash_out_minutes == 10.0


def test_fifteen_early_cash_out_env_aliases(monkeypatch):
    monkeypatch.setenv("FIFTEEN_EARLY_CASH_OUT_BID", "0.96")
    monkeypatch.setenv("FIFTEEN_EARLY_CASH_OUT_MINUTES", "8")
    fifteen = FifteenSettings(_env_file=None)
    assert fifteen.early_cash_out_bid == 0.96
    assert fifteen.early_cash_out_minutes == 8.0


def test_yes_bid_99_cashes_out_98_does_not():
    assert should_cash_out_99("Yes", yes_bid=0.99, yes_ask=1.00) is True
    assert should_cash_out_99("Yes", yes_bid=0.98, yes_ask=0.99) is False
    assert fifteen_cash_out("Yes", yes_bid=0.99, yes_ask=1.00) is True
    assert fifteen_cash_out("Yes", yes_bid=0.98, yes_ask=0.99) is False


def test_no_bid_99_or_yes_ask_01_cashes_out():
    assert should_cash_out_99("No", yes_bid=0.00, yes_ask=0.01, no_bid=0.99) is True
    assert should_cash_out_99("No", yes_bid=0.00, yes_ask=0.01, no_bid=None) is True
    assert should_cash_out_99("No", yes_bid=0.02, yes_ask=0.03, no_bid=0.97) is False
    assert should_cash_out_99("No", yes_bid=0.01, yes_ask=0.02, no_bid=0.98) is False


def test_take_profit_98_only_when_fill_plus_two_cents():
    assert (
        should_take_profit("Yes", fill_price=0.96, yes_bid=0.98, yes_ask=0.99) is True
    )
    assert (
        should_take_profit("Yes", fill_price=0.97, yes_bid=0.98, yes_ask=0.99) is False
    )
    assert fifteen_take_profit("Yes", fill_price=0.96, yes_bid=0.98, yes_ask=0.99) is True
    assert fifteen_take_profit("Yes", fill_price=0.97, yes_bid=0.98, yes_ask=0.99) is False


def test_cash_out_99_beats_take_profit_label():
    assert (
        exit_reason("Yes", fill_price=0.90, yes_bid=0.99, yes_ask=1.00)
        == CASH_OUT_LABEL
    )
    assert (
        exit_reason("Yes", fill_price=0.96, yes_bid=0.98, yes_ask=0.99)
        == TAKE_PROFIT_LABEL
    )
    assert exit_reason("Yes", fill_price=0.97, yes_bid=0.98, yes_ask=0.99) is None


def test_early_cash_out_yes_95_with_7_minutes():
    assert should_cash_out_early("Yes", yes_bid=0.95, yes_ask=0.96, minutes_left=7) is True
    assert fifteen_cash_out_early("Yes", yes_bid=0.95, yes_ask=0.96, minutes_left=7) is True
    assert (
        exit_reason("Yes", fill_price=0.90, yes_bid=0.95, yes_ask=0.96, minutes_left=7)
        == EARLY_CASH_OUT_LABEL
    )


def test_early_cash_out_no_95_with_7_minutes():
    assert (
        should_cash_out_early("No", yes_bid=0.04, yes_ask=0.05, no_bid=0.95, minutes_left=7)
        is True
    )
    assert (
        should_cash_out_early("No", yes_bid=0.00, yes_ask=0.05, no_bid=None, minutes_left=7)
        is True
    )
    assert (
        exit_reason(
            "No",
            fill_price=0.44,
            yes_bid=0.04,
            yes_ask=0.05,
            no_bid=0.95,
            minutes_left=7,
        )
        == EARLY_CASH_OUT_LABEL
    )


def test_early_cash_out_95_with_10_minutes_fires():
    assert should_cash_out_early("Yes", yes_bid=0.95, yes_ask=0.96, minutes_left=10) is True
    assert should_cash_out_early("No", yes_bid=0.04, yes_ask=0.05, no_bid=0.95, minutes_left=10) is True


def test_early_cash_out_95_with_11_minutes_does_not_fire():
    assert should_cash_out_early("Yes", yes_bid=0.95, yes_ask=0.96, minutes_left=11) is False
    assert should_cash_out_early("No", yes_bid=0.04, yes_ask=0.05, no_bid=0.95, minutes_left=11) is False
    assert (
        exit_reason("Yes", fill_price=0.94, yes_bid=0.95, yes_ask=0.96, minutes_left=11)
        is None
    )
    assert (
        exit_reason(
            "No",
            fill_price=0.94,
            yes_bid=0.04,
            yes_ask=0.05,
            no_bid=0.95,
            minutes_left=11,
        )
        is None
    )


def test_early_cash_out_missing_minutes_does_not_fire():
    assert should_cash_out_early("Yes", yes_bid=0.95, yes_ask=0.96, minutes_left=None) is False
    assert exit_reason("Yes", fill_price=0.94, yes_bid=0.95, yes_ask=0.96) is None


def test_early_cash_out_94_with_7_minutes_does_not_fire():
    assert should_cash_out_early("Yes", yes_bid=0.94, yes_ask=0.95, minutes_left=7) is False
    assert should_cash_out_early("No", yes_bid=0.05, yes_ask=0.06, no_bid=0.94, minutes_left=7) is False


def test_cash_out_99_fires_regardless_of_time():
    assert (
        exit_reason("Yes", fill_price=0.90, yes_bid=0.99, yes_ask=1.00, minutes_left=40)
        == CASH_OUT_LABEL
    )
    assert (
        exit_reason("Yes", fill_price=0.90, yes_bid=0.99, yes_ask=1.00, minutes_left=7)
        == CASH_OUT_LABEL
    )
    assert (
        exit_reason(
            "No",
            fill_price=0.44,
            yes_bid=0.00,
            yes_ask=0.01,
            no_bid=0.99,
            minutes_left=25,
        )
        == CASH_OUT_LABEL
    )


def test_exit_priority_99_then_early_then_tp():
    assert (
        exit_reason("Yes", fill_price=0.90, yes_bid=0.99, yes_ask=1.00, minutes_left=7)
        == CASH_OUT_LABEL
    )
    assert (
        exit_reason("Yes", fill_price=0.93, yes_bid=0.95, yes_ask=0.96, minutes_left=7)
        == EARLY_CASH_OUT_LABEL
    )
    assert (
        exit_reason("Yes", fill_price=0.93, yes_bid=0.95, yes_ask=0.96, minutes_left=20)
        == TAKE_PROFIT_LABEL
    )


def test_flatten_payload_yes_hits_99_bid_without_crossing_through():
    assert post_only_would_take_exit("Yes", yes_bid=0.99, yes_ask=1.00, yes_book_price=0.99)
    payload = flatten_payload(
        "KXBTCD-1", "Yes", 2, yes_book_price=0.99, post_only=False, exchange_index=2
    )
    assert payload["side"] == "ask"
    assert payload["price"] == "0.9900"
    assert payload["post_only"] is False
    assert payload["count"] == "2.00"
    assert payload["label"] == CASH_OUT_LABEL


def test_flatten_payload_no_is_yes_bid_at_one_cent():
    payload = flatten_payload(
        "KXETH15M-1", "No", 1, yes_book_price=0.01, post_only=False, exchange_index=2
    )
    assert payload["side"] == "bid"
    assert payload["price"] == "0.0100"
    assert payload["post_only"] is False


def test_signal_yes_99_is_cash_out_not_tp():
    holding = Holding(ticker="KXBTCD-1", side="Yes", contracts=2, fill_price=0.90)
    signal = signal_for_holding(holding, yes_bid=0.99, yes_ask=1.00, no_bid=0.00)
    assert signal is not None
    assert signal.reason == CASH_OUT_LABEL
    assert signal.post_only is False
    assert signal.payload["price"] == "0.9900"
    assert signal.payload["side"] == "ask"


def test_signal_98_without_tp_is_none():
    holding = Holding(ticker="KXBTCD-1", side="Yes", contracts=2, fill_price=0.97)
    assert signal_for_holding(holding, yes_bid=0.98, yes_ask=0.99, no_bid=0.01) is None


def test_signal_yes_95_with_7_minutes_is_early_cash_out():
    holding = Holding(ticker="KXBTCD-1", side="Yes", contracts=2, fill_price=0.90)
    signal = signal_for_holding(
        holding, yes_bid=0.95, yes_ask=0.96, no_bid=0.04, minutes_left=7
    )
    assert signal is not None
    assert signal.reason == EARLY_CASH_OUT_LABEL
    assert signal.exit_price == 0.95
    assert signal.payload["label"] == EARLY_CASH_OUT_LABEL
    assert signal.payload["side"] == "ask"


def test_signal_no_95_with_7_minutes_is_early_cash_out():
    holding = Holding(ticker="KXETH15M-1", side="No", contracts=1, fill_price=0.44)
    signal = signal_for_holding(
        holding, yes_bid=0.04, yes_ask=0.05, no_bid=0.95, minutes_left=7
    )
    assert signal is not None
    assert signal.reason == EARLY_CASH_OUT_LABEL
    assert signal.exit_price == 0.95
    assert signal.payload["side"] == "bid"
    assert signal.payload["price"] == "0.0500"


def test_signal_95_with_11_minutes_is_none_without_tp():
    holding = Holding(ticker="KXBTCD-1", side="Yes", contracts=2, fill_price=0.94)
    assert (
        signal_for_holding(holding, yes_bid=0.95, yes_ask=0.96, no_bid=0.04, minutes_left=11)
        is None
    )


def test_collect_holdings_from_positions_ignores_other_series():
    class Client:
        def get_positions(self, count_filter="position"):
            return [
                {"ticker": "KXBTCD-1", "position_fp": "2.00", "exchange_index": 2},
                {"ticker": "KXBTC15M-1", "position_fp": "-3.00", "exchange_index": 2},
            ]

    hourly = collect_holdings(Client(), state={}, series=("KXBTCD", "KXETHD"))
    assert len(hourly) == 1
    assert hourly[0].ticker == "KXBTCD-1"
    assert hourly[0].side == "Yes"
    assert hourly[0].contracts == 2

    fifteen = collect_holdings(Client(), state={}, series=("KXBTC15M", "KXETH15M"))
    assert len(fifteen) == 1
    assert fifteen[0].side == "No"
    assert fifteen[0].contracts == 3


def test_unfilled_rest_is_not_inventory():
    class Client:
        pass

    state = {"last_ticker": "KXBTCD-1", "last_side": "Yes", "last_contracts": 2}
    assert collect_holdings(Client(), state=state, series=("KXBTCD",)) == []
    filled = collect_holdings(
        Client(),
        state=state,
        trades=[
            {
                "ticker": "KXBTCD-1",
                "side": "Yes",
                "contracts": 2,
                "fill_status": "filled",
                "kalshi_price": 0.54,
                "result": "pending",
            }
        ],
        series=("KXBTCD",),
    )
    assert len(filled) == 1
    assert filled[0].fill_price == 0.54


def test_place_flatten_drops_post_only_on_cross():
    prices: list[bool] = []

    def create(payload):
        prices.append(payload["post_only"])
        if payload["post_only"]:
            raise RuntimeError('400: {"details":"post only cross"}')
        return {"order": {"order_id": "exit-1"}}

    placed, working = place_flatten(
        create,
        {"ticker": "KXBTCD-1", "price": "0.9900", "post_only": True, "label": CASH_OUT_LABEL},
    )
    assert prices == [True, False]
    assert working["post_only"] is False
    assert placed["order_id"] == "exit-1"


def _close_iso(minutes: float, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _client(
    *,
    yes_bid: float,
    yes_ask: float,
    no_bid: float,
    position: str = "2.00",
    close_minutes: float | None = None,
    now: datetime | None = None,
):
    client = MagicMock()
    client.get_positions.return_value = [
        {"ticker": "KXBTCD-26SEP0510-T64000", "position_fp": position, "exchange_index": 2}
    ]
    market = {
        "yes_bid_dollars": f"{yes_bid:.4f}",
        "yes_ask_dollars": f"{yes_ask:.4f}",
        "no_bid_dollars": f"{no_bid:.4f}",
        "no_ask_dollars": f"{1.0 - yes_bid:.4f}",
    }
    if close_minutes is not None:
        market["close_time"] = _close_iso(close_minutes, now)
    client.get_market.return_value = market
    client.create_order.return_value = {"order": {"order_id": "flat-1"}}
    return client


def test_live_manage_places_cash_out_99(tmp_path: Path, capsys):
    client = _client(yes_bid=0.99, yes_ask=1.00, no_bid=0.00)
    journal = tmp_path / "trade_log.jsonl"
    state: dict = {}
    out = manage_open_positions(
        client,
        state=state,
        trades=[],
        live=True,
        journal_path=journal,
        series=("KXBTCD", "KXETHD"),
        exchange_index=2,
    )
    assert out["placed"]
    sent = client.create_order.call_args.args[0]
    assert sent["side"] == "ask"
    assert sent["price"] == "0.9900"
    assert sent["post_only"] is False
    assert sent["label"] == CASH_OUT_LABEL
    rows = load_trades(journal)
    assert rows
    assert rows[-1]["exit_reason"] == CASH_OUT_LABEL
    assert rows[-1]["label"] == CASH_OUT_LABEL
    assert "CASH_OUT_99" in capsys.readouterr().out


def test_live_manage_skips_98_without_tp(tmp_path: Path):
    client = _client(yes_bid=0.98, yes_ask=0.99, no_bid=0.01)
    out = manage_open_positions(
        client,
        state={},
        trades=[
            {
                "ticker": "KXBTCD-26SEP0510-T64000",
                "side": "Yes",
                "fill_status": "filled",
                "kalshi_price": 0.97,
                "contracts": 2,
                "result": "pending",
            }
        ],
        live=True,
        journal_path=tmp_path / "trade_log.jsonl",
        series=("KXBTCD",),
        exchange_index=2,
    )
    client.create_order.assert_not_called()
    assert out["signals"] == []
    assert out["placed"] == []


def test_live_manage_98_fires_when_fill_plus_two(tmp_path: Path):
    client = _client(yes_bid=0.98, yes_ask=0.99, no_bid=0.01)
    out = manage_open_positions(
        client,
        state={},
        trades=[
            {
                "ticker": "KXBTCD-26SEP0510-T64000",
                "side": "Yes",
                "fill_status": "filled",
                "kalshi_price": 0.96,
                "contracts": 2,
                "result": "pending",
            }
        ],
        live=True,
        journal_path=tmp_path / "trade_log.jsonl",
        series=("KXBTCD",),
        exchange_index=2,
    )
    assert out["signals"][0]["reason"] == TAKE_PROFIT_LABEL
    sent = client.create_order.call_args.args[0]
    assert sent["price"] == "0.9800"
    assert sent["post_only"] is False


def test_dry_run_manage_does_not_place(tmp_path: Path):
    client = _client(yes_bid=0.99, yes_ask=1.00, no_bid=0.00)
    out = manage_open_positions(
        client,
        state={},
        trades=[],
        live=False,
        journal_path=tmp_path / "trade_log.jsonl",
        series=("KXBTCD",),
        exchange_index=2,
    )
    client.create_order.assert_not_called()
    assert out["dry_run"]
    assert out["placed"] == []
    assert out["journal"][0]["exit_reason"] == CASH_OUT_LABEL


def test_hourly_once_manages_even_without_new_ideas(monkeypatch, tmp_path):
    called: dict = {}

    def fake_manage(*args, **kwargs):
        called["live"] = kwargs.get("live")
        called["series"] = set(kwargs.get("series") or [])
        return {"signals": [], "placed": [], "errors": [], "dry_run": [], "journal": []}

    class FakeClient:
        can_trade = False

        def get_fills(self, limit=50):
            return []

        def get_market(self, ticker):
            return {}

        def close(self):
            pass

    class FakeSpots:
        prices = {"BTC": 78000.0}
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
    monkeypatch.setattr("src.main.manage_open_positions", fake_manage)
    monkeypatch.setattr("src.main.try_settle_paper", lambda *a, **k: None)

    settings = HourlySettings(
        _env_file=None,
        artifacts_dir=str(tmp_path),
        state_path=str(tmp_path / "state.json"),
        paper_log_path=str(tmp_path / "paper_log.jsonl"),
        halted=True,
    )
    from src.main import run_scan

    assert run_scan(settings, asset="BTC", place=True, force_live=False) == 0
    assert called["live"] is False
    assert "KXBTCD" in called["series"]


def test_minutes_until_settlement_from_close_time():
    now = datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc)
    assert minutes_until_settlement((now + timedelta(minutes=7)).isoformat(), now) == 7.0
    assert minutes_until_settlement((now + timedelta(minutes=11)).isoformat(), now) == 11.0


def test_live_manage_early_cash_out_yes_at_7_minutes(tmp_path: Path, capsys):
    now = datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc)
    client = _client(yes_bid=0.95, yes_ask=0.96, no_bid=0.04, close_minutes=7, now=now)
    out = manage_open_positions(
        client,
        state={},
        trades=[
            {
                "ticker": "KXBTCD-26SEP0510-T64000",
                "side": "Yes",
                "fill_status": "filled",
                "kalshi_price": 0.90,
                "contracts": 2,
                "result": "pending",
            }
        ],
        live=True,
        journal_path=tmp_path / "trade_log.jsonl",
        series=("KXBTCD",),
        exchange_index=2,
        now=now,
    )
    assert out["signals"][0]["reason"] == EARLY_CASH_OUT_LABEL
    sent = client.create_order.call_args.args[0]
    assert sent["label"] == EARLY_CASH_OUT_LABEL
    assert sent["price"] == "0.9500"
    assert "CASH_OUT_95_TIME" in capsys.readouterr().out
    rows = load_trades(tmp_path / "trade_log.jsonl")
    assert rows[-1]["exit_reason"] == EARLY_CASH_OUT_LABEL


def test_live_manage_early_cash_out_no_at_7_minutes(tmp_path: Path):
    now = datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc)
    client = MagicMock()
    client.get_positions.return_value = [
        {"ticker": "KXBTC15M-26SEP051015-T64000", "position_fp": "-1.00", "exchange_index": 2}
    ]
    client.get_market.return_value = {
        "yes_bid_dollars": "0.0400",
        "yes_ask_dollars": "0.0500",
        "no_bid_dollars": "0.9500",
        "no_ask_dollars": "0.9600",
        "close_time": _close_iso(7, now),
    }
    client.create_order.return_value = {"order": {"order_id": "flat-early-no"}}
    out = manage_open_positions(
        client,
        state={},
        trades=[],
        live=True,
        journal_path=tmp_path / "fifteen_trade_log.jsonl",
        series=("KXBTC15M", "KXETH15M"),
        exchange_index=2,
        now=now,
    )
    assert out["signals"][0]["reason"] == EARLY_CASH_OUT_LABEL
    sent = client.create_order.call_args.args[0]
    assert sent["side"] == "bid"
    assert sent["price"] == "0.0500"
    assert sent["label"] == EARLY_CASH_OUT_LABEL


def test_live_manage_95_with_11_minutes_does_not_early_cash_out(tmp_path: Path):
    now = datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc)
    client = _client(yes_bid=0.95, yes_ask=0.96, no_bid=0.04, close_minutes=11, now=now)
    out = manage_open_positions(
        client,
        state={},
        trades=[
            {
                "ticker": "KXBTCD-26SEP0510-T64000",
                "side": "Yes",
                "fill_status": "filled",
                "kalshi_price": 0.94,
                "contracts": 2,
                "result": "pending",
            }
        ],
        live=True,
        journal_path=tmp_path / "trade_log.jsonl",
        series=("KXBTCD",),
        exchange_index=2,
        now=now,
    )
    client.create_order.assert_not_called()
    assert out["signals"] == []
    assert out["placed"] == []


def test_live_manage_99_still_fires_with_40_minutes_left(tmp_path: Path):
    now = datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc)
    client = _client(yes_bid=0.99, yes_ask=1.00, no_bid=0.00, close_minutes=40, now=now)
    out = manage_open_positions(
        client,
        state={},
        trades=[],
        live=True,
        journal_path=tmp_path / "trade_log.jsonl",
        series=("KXBTCD",),
        exchange_index=2,
        now=now,
    )
    assert out["signals"][0]["reason"] == CASH_OUT_LABEL
    sent = client.create_order.call_args.args[0]
    assert sent["label"] == CASH_OUT_LABEL
    assert sent["price"] == "0.9900"


def test_fifteen_no_position_cashes_out_at_yes_ask_01(tmp_path: Path):
    client = MagicMock()
    client.get_positions.return_value = [
        {"ticker": "KXBTC15M-26SEP051015-T64000", "position_fp": "-1.00", "exchange_index": 2}
    ]
    client.get_market.return_value = {
        "yes_bid_dollars": "0.0000",
        "yes_ask_dollars": "0.0100",
        "no_bid_dollars": "0.9900",
        "no_ask_dollars": "1.0000",
    }
    client.create_order.return_value = {"order": {"order_id": "flat-no"}}
    state = {
        "tickets": [
            {
                "status": "open",
                "loop": "fifteen",
                "ticker": "KXBTC15M-26SEP051015-T64000",
                "side": "No",
                "contracts": 1,
                "limit": 0.44,
            }
        ]
    }
    out = manage_open_positions(
        client,
        state=state,
        trades=[],
        live=True,
        journal_path=tmp_path / "fifteen_trade_log.jsonl",
        series=("KXBTC15M", "KXETH15M"),
        exchange_index=2,
    )
    assert out["signals"][0]["reason"] == CASH_OUT_LABEL
    sent = client.create_order.call_args.args[0]
    assert sent["side"] == "bid"
    assert sent["price"] == "0.0100"
    assert state["tickets"][0]["status"] == "flat"
    assert state["tickets"][0]["exit_reason"] == CASH_OUT_LABEL


def test_fill_is_close_yes_and_no():
    assert fill_is_close({"action": "sell", "side": "yes"}, "Yes") is True
    assert fill_is_close({"action": "buy", "side": "yes"}, "Yes") is False
    assert fill_is_close({"action": "sell", "side": "no"}, "No") is True
    assert fill_is_close({"action": "buy", "side": "no"}, "No") is False
    assert fill_is_close({"outcome_side": "no"}, "Yes") is True
    assert fill_is_close({"outcome_side": "yes"}, "Yes") is False
    assert fill_is_close({"outcome_side": "yes"}, "No") is True


def _filled_trade(*, ticker: str, side: str, order_id: str = "entry-1", **extra):
    row = {
        "ticker": ticker,
        "side": side,
        "fill_status": "filled",
        "kalshi_price": 0.54,
        "contracts": 2,
        "result": "pending",
        "order_id": order_id,
    }
    row.update(extra)
    return row


def test_detect_manual_flatten_yes_when_position_gone_and_external_sell():
    trade = _filled_trade(ticker="KXBTCD-1", side="Yes")
    hits = detect_manual_flattens(
        [trade],
        fills=[
            {"ticker": "KXBTCD-1", "order_id": "entry-1", "action": "buy", "side": "yes"},
            {
                "ticker": "KXBTCD-1",
                "order_id": "app-sell-9",
                "action": "sell",
                "side": "yes",
                "yes_price_dollars": "0.9600",
            },
        ],
        fills_available=True,
        positions={},
        positions_available=True,
        series=("KXBTCD",),
    )
    assert len(hits) == 1
    assert hits[0]["ticker"] == "KXBTCD-1"
    assert hits[0]["order_id"] == "app-sell-9"
    assert hits[0]["exit_price"] == 0.96


def test_detect_manual_flatten_no_when_position_gone_and_external_sell():
    trade = _filled_trade(ticker="KXBTC15M-1", side="No")
    hits = detect_manual_flattens(
        [trade],
        fills=[
            {
                "ticker": "KXBTC15M-1",
                "order_id": "app-sell-no",
                "action": "sell",
                "side": "no",
                "no_price_dollars": "0.9700",
            }
        ],
        fills_available=True,
        positions={"KXBTC15M-1": 0.0},
        positions_available=True,
        series=("KXBTC15M", "KXETH15M"),
    )
    assert len(hits) == 1
    assert hits[0]["side"] == "No"
    assert hits[0]["exit_price"] == 0.97


def test_detect_skips_bot_placed_exit_order_id():
    trade = _filled_trade(ticker="KXBTCD-1", side="Yes", exit_order_id="bot-exit-1")
    hits = detect_manual_flattens(
        [trade],
        fills=[
            {
                "ticker": "KXBTCD-1",
                "order_id": "bot-exit-1",
                "action": "sell",
                "side": "yes",
                "yes_price_dollars": "0.9900",
            }
        ],
        fills_available=True,
        positions={},
        positions_available=True,
        series=("KXBTCD",),
    )
    assert hits == []


def test_detect_skips_when_still_holding():
    trade = _filled_trade(ticker="KXBTCD-1", side="Yes")
    hits = detect_manual_flattens(
        [trade],
        fills=[{"ticker": "KXBTCD-1", "order_id": "app-sell-9", "action": "sell", "side": "yes"}],
        fills_available=True,
        positions={"KXBTCD-1": 2.0},
        positions_available=True,
        series=("KXBTCD",),
    )
    assert hits == []


def test_detect_skips_when_only_entry_fill():
    trade = _filled_trade(ticker="KXBTCD-1", side="Yes")
    hits = detect_manual_flattens(
        [trade],
        fills=[{"ticker": "KXBTCD-1", "order_id": "entry-1", "action": "buy", "side": "yes"}],
        fills_available=True,
        positions={},
        positions_available=True,
        series=("KXBTCD",),
    )
    assert hits == []


def test_manage_journals_manual_flatten_and_does_not_place(tmp_path: Path, capsys):
    client = MagicMock()
    client.get_positions.return_value = []
    client.get_market.return_value = {
        "yes_bid_dollars": "0.9600",
        "yes_ask_dollars": "0.9700",
        "no_bid_dollars": "0.0300",
        "no_ask_dollars": "0.0400",
    }
    trades = [
        _filled_trade(ticker="KXBTCD-26SEP0510-T64000", side="Yes", order_id="entry-1")
    ]
    fills = [
        {"ticker": "KXBTCD-26SEP0510-T64000", "order_id": "entry-1", "action": "buy", "side": "yes"},
        {
            "ticker": "KXBTCD-26SEP0510-T64000",
            "order_id": "app-sell-9",
            "action": "sell",
            "side": "yes",
            "yes_price_dollars": "0.9600",
        },
    ]
    state = {
        "last_ticker": "KXBTCD-26SEP0510-T64000",
        "last_side": "Yes",
        "last_contracts": 2,
        "tickets": [
            {
                "status": "open",
                "ticker": "KXBTCD-26SEP0510-T64000",
                "side": "Yes",
                "contracts": 2,
            }
        ],
    }
    out = manage_open_positions(
        client,
        state=state,
        trades=trades,
        fills=fills,
        fills_available=True,
        live=True,
        journal_path=tmp_path / "trade_log.jsonl",
        series=("KXBTCD",),
        exchange_index=2,
    )
    client.create_order.assert_not_called()
    assert out["placed"] == []
    assert out["manual"][0]["reason"] == MANUAL_FLATTEN_LABEL
    assert trades[0]["exit_reason"] == MANUAL_FLATTEN_LABEL
    assert trades[0]["exit_order_id"] == "app-sell-9"
    assert state["tickets"][0]["status"] == "flat"
    assert state["last_exit_reason"] == MANUAL_FLATTEN_LABEL
    assert "MANUAL_FLATTEN" in capsys.readouterr().out
    rows = load_trades(tmp_path / "trade_log.jsonl")
    assert any(row.get("exit_reason") == MANUAL_FLATTEN_LABEL for row in rows)


def test_manage_bot_exit_order_not_relabeled_manual(tmp_path: Path):
    client = MagicMock()
    client.get_positions.return_value = []
    client.get_market.return_value = {
        "yes_bid_dollars": "0.9900",
        "yes_ask_dollars": "1.0000",
        "no_bid_dollars": "0.0000",
        "no_ask_dollars": "0.0100",
    }
    trades = [
        _filled_trade(
            ticker="KXBTCD-26SEP0510-T64000",
            side="Yes",
            order_id="entry-1",
            exit_order_id="bot-exit-1",
            exit_reason=CASH_OUT_LABEL,
        )
    ]
    fills = [
        {
            "ticker": "KXBTCD-26SEP0510-T64000",
            "order_id": "bot-exit-1",
            "action": "sell",
            "side": "yes",
            "yes_price_dollars": "0.9900",
        }
    ]
    out = manage_open_positions(
        client,
        state={"last_ticker": "KXBTCD-26SEP0510-T64000", "last_side": "Yes"},
        trades=trades,
        fills=fills,
        fills_available=True,
        live=True,
        journal_path=tmp_path / "trade_log.jsonl",
        series=("KXBTCD",),
        exchange_index=2,
    )
    client.create_order.assert_not_called()
    assert out["manual"] == []
    assert trades[0]["exit_reason"] == CASH_OUT_LABEL
