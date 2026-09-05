"""99¢ cash-out beats +2¢ TP. Live oneshots flatten; 0.98 does not unless fill+2¢."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.exits import (
    CASH_OUT_LABEL,
    Holding,
    TAKE_PROFIT_LABEL,
    collect_holdings,
    exit_reason,
    flatten_payload,
    manage_open_positions,
    place_flatten,
    post_only_would_take_exit,
    should_cash_out_99,
    should_take_profit,
    signal_for_holding,
)
from src.config import HourlySettings
from src.fifteen.config import FifteenSettings
from src.fifteen_filters import should_cash_out_99 as fifteen_cash_out
from src.fifteen_filters import should_take_profit as fifteen_take_profit
from src.journal import load_trades


def test_cash_out_bid_default_is_99_cents():
    hourly = HourlySettings(_env_file=None)
    fifteen = FifteenSettings(_env_file=None)
    assert hourly.cash_out_bid == 0.99
    assert hourly.take_profit_cents == 0.02
    assert fifteen.cash_out_bid == 0.99
    assert fifteen.take_profit_cents == 0.02


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


def _client(*, yes_bid: float, yes_ask: float, no_bid: float, position: str = "2.00"):
    client = MagicMock()
    client.get_positions.return_value = [
        {"ticker": "KXBTCD-26SEP0510-T64000", "position_fp": position, "exchange_index": 2}
    ]
    client.get_market.return_value = {
        "yes_bid_dollars": f"{yes_bid:.4f}",
        "yes_ask_dollars": f"{yes_ask:.4f}",
        "no_bid_dollars": f"{no_bid:.4f}",
        "no_ask_dollars": f"{1.0 - yes_bid:.4f}",
    }
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
