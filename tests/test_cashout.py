"""Early cash-out when held-side bid is already 99¢."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.cashout import (
    CASHOUT_BID,
    exit_order_payload,
    filled_contracts_for,
    held_side_bid,
    manage_open_cashouts,
    should_cashout,
)


def test_held_side_bid_and_threshold():
    assert held_side_bid("Yes", 0.99, 0.01) == 0.99
    assert held_side_bid("No", 0.01, 0.99) == 0.99
    assert should_cashout("Yes", 0.99, 0.01)
    assert should_cashout("No", 0.02, 0.99)
    assert not should_cashout("Yes", 0.98, 0.02)
    assert CASHOUT_BID == 0.99


def test_exit_payload_yes_asks_at_99_not_post_only():
    payload = exit_order_payload("KXBTCD-FAKE", "Yes", 3, exchange_index=2)
    assert payload["ticker"] == "KXBTCD-FAKE"
    assert payload["side"] == "ask"
    assert payload["price"] == "0.9900"
    assert payload["count"] == "3.00"
    assert payload["post_only"] is False
    assert payload["time_in_force"] == "immediate_or_cancel"
    assert payload["exchange_index"] == 2


def test_exit_payload_no_bids_yes_at_1c():
    payload = exit_order_payload("KXETH15M-FAKE", "No", 2)
    assert payload["side"] == "bid"
    assert payload["price"] == "0.0100"
    assert payload["count"] == "2.00"
    assert payload["post_only"] is False


def test_filled_contracts_for_sums_ticker():
    fills = [
        {"ticker": "AAA", "count": "2.00"},
        {"ticker": "BBB", "count": "9.00"},
        {"market_ticker": "AAA", "count_fp": "1.00"},
    ]
    assert filled_contracts_for(fills, "AAA") == 3.0
    assert filled_contracts_for(fills, "CCC") == 0.0


def test_manage_skips_when_bid_below_99():
    client = MagicMock()
    client.get_market.return_value = {
        "yes_bid_dollars": "0.90",
        "yes_ask_dollars": "0.92",
        "no_bid_dollars": "0.08",
        "no_ask_dollars": "0.10",
    }
    client.get_fills.return_value = [{"ticker": "KXBTCD-T", "count": "2"}]
    out = manage_open_cashouts(
        client,
        [{"ticker": "KXBTCD-T", "side": "Yes", "contracts": 2}],
        live=True,
        exchange_index=2,
    )
    assert len(out) == 1
    assert out[0]["action"] == "skip"
    client.create_order.assert_not_called()


def test_manage_skips_without_fill_even_at_99():
    client = MagicMock()
    client.get_market.return_value = {
        "yes_bid_dollars": "0.99",
        "yes_ask_dollars": "1.00",
        "no_bid_dollars": "0.00",
        "no_ask_dollars": "0.01",
    }
    client.get_fills.return_value = []
    out = manage_open_cashouts(
        client,
        [{"ticker": "KXBTCD-T", "side": "Yes", "contracts": 2}],
        live=True,
    )
    assert out[0]["action"] == "skip"
    assert "no fill" in out[0]["reason"]
    client.create_order.assert_not_called()


def test_manage_dry_run_reports_would_cashout():
    client = MagicMock()
    client.get_market.return_value = {
        "yes_bid_dollars": "0.01",
        "yes_ask_dollars": "0.02",
        "no_bid_dollars": "0.99",
        "no_ask_dollars": "1.00",
    }
    client.get_fills.return_value = [{"ticker": "KXETH15M-T", "count": "1"}]
    out = manage_open_cashouts(
        client,
        [{"ticker": "KXETH15M-T", "side": "No", "contracts": 1}],
        live=False,
        exchange_index=2,
    )
    assert out[0]["action"] == "would_cashout"
    assert out[0]["payload"]["side"] == "bid"
    assert out[0]["payload"]["price"] == "0.0100"
    client.create_order.assert_not_called()


def test_manage_live_places_ioc_exit():
    client = MagicMock()
    client.get_market.return_value = {
        "yes_bid_dollars": "0.99",
        "yes_ask_dollars": "1.00",
        "no_bid_dollars": "0.00",
        "no_ask_dollars": "0.01",
    }
    client.get_fills.return_value = [{"ticker": "KXBTCD-T", "count": "4"}]
    client.get_orders.return_value = []
    client.create_order.return_value = {"order": {"order_id": "exit-1"}}
    out = manage_open_cashouts(
        client,
        [{"ticker": "KXBTCD-T", "side": "Yes", "contracts": 4}],
        live=True,
        exchange_index=2,
    )
    assert out[0]["action"] == "cashed_out"
    sent = client.create_order.call_args.args[0]
    assert sent["side"] == "ask"
    assert sent["price"] == "0.9900"
    assert sent["post_only"] is False
    assert sent["time_in_force"] == "immediate_or_cancel"
    assert sent["count"] == "4.00"
    assert sent["exchange_index"] == 2
