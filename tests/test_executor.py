"""Dry-run must never place an order."""

from pathlib import Path
from unittest.mock import MagicMock

from src.executor import execute_ideas, is_post_only_cross, step_more_passive
from src.filters import Idea
from src.markets import HourlyMarket
from datetime import datetime, timedelta, timezone


def _idea() -> Idea:
    market = HourlyMarket(
        ticker="KXBTCD-FAKE-T78099.99",
        event_ticker="KXBTCD-FAKE",
        series_ticker="KXBTCD",
        asset="BTC",
        title="fake",
        yes_sub_title="$78,100 or above",
        threshold=78099.99,
        strike_type="greater",
        close_time=datetime.now(timezone.utc) + timedelta(minutes=20),
        status="active",
        yes_bid=0.50,
        yes_ask=0.52,
        no_bid=0.48,
        no_ask=0.50,
        yes_bid_size=10,
        yes_ask_size=10,
        no_bid_size=10,
        no_ask_size=10,
        rules_primary="test",
        rules_secondary="",
        settlement_source="CF Benchmarks BRTI",
        exchange_index=2,
    )
    return Idea(
        market=market,
        side="Yes",
        entry_price=0.52,
        limit_price=0.51,
        fair=0.62,
        gross_edge=0.10,
        net_edge=0.08,
        fee_per_contract=0.0175,
        fee_total=0.07,
        z=0.1,
        hours_left=0.3,
        contracts=4,
        risk_dollars=2.08,
        max_loss=2.08,
        rationale=["unit test"],
        post_maker=True,
    )


def test_dry_run_never_calls_create_order(tmp_path: Path):
    client = MagicMock()
    out = execute_ideas(
        [_idea()],
        client=client,
        artifacts_dir=tmp_path,
        live=False,
        run_id="test-run",
    )
    client.create_order.assert_not_called()
    client.create_order_v2.assert_not_called()
    assert out["mode"] == "dry_run"
    assert (tmp_path / "last_run.json").is_file()
    assert out["orders"][0]["ticker"] == "KXBTCD-FAKE-T78099.99"
    assert out["orders"][0]["count"] == "4.00"
    assert isinstance(out["orders"][0]["count"], str)
    assert out["orders"][0]["side"] == "bid"
    assert out["orders"][0]["price"] == "0.5100"
    assert len(out["orders"][0]["client_order_id"]) == 36


def test_live_without_confirm_stays_dry(tmp_path: Path):
    client = MagicMock()
    out = execute_ideas(
        [_idea()],
        client=client,
        artifacts_dir=tmp_path,
        live=False,
        confirm_live=True,
        run_id="test-run",
    )
    client.create_order.assert_not_called()
    assert out["mode"] == "dry_run"


def test_live_cancels_stale_daily_rests_with_uuid_client_ids(tmp_path: Path):
    """V2 client_order_id is a UUID — cancel must not require an hourly- prefix."""
    client = MagicMock()
    client.get_orders.return_value = [
        {
            "order_id": "old-eth",
            "ticker": "KXETHD-26SEP0117-T2399.99",
            "client_order_id": "4a7e012f-50c1-4af4-909f-192e400f8de0",
        },
        {
            "order_id": "campaign-15m",
            "ticker": "KXBTC15M-26SEP011615-30",
            "client_order_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        },
        {
            "order_id": "legacy-hourly",
            "ticker": "KXBTCD-26SEP0116-T77249.99",
            "client_order_id": "hourly-old-run-KXBTCD-yes",
        },
    ]
    client.create_order.return_value = {
        "order": {"order_id": "new-1", "fill_count": "0.00", "remaining_count": "4.00"}
    }
    out = execute_ideas(
        [_idea()],
        client=client,
        artifacts_dir=tmp_path,
        live=True,
        confirm_live=True,
        run_id="test-run",
    )
    canceled = {row["order_id"] for row in out["canceled"]}
    assert canceled == {"old-eth", "legacy-hourly"}
    assert out["placed"][0]["order_id"] == "new-1"
    client.cancel_order.assert_any_call("old-eth", ticker="KXETHD-26SEP0117-T2399.99")
    campaign_calls = [
        args for args in client.cancel_order.call_args_list if args.args and args.args[0] == "campaign-15m"
    ]
    assert campaign_calls == []


def test_step_more_passive_never_lifts():
    down = step_more_passive({"side": "bid", "price": "0.1300", "client_order_id": "old"})
    assert down is not None
    assert down["price"] == "0.1200"
    assert down["client_order_id"] != "old"
    assert down["post_only"] is True
    up = step_more_passive({"side": "ask", "price": "0.8700", "client_order_id": "old"})
    assert up is not None
    assert up["price"] == "0.8800"
    assert step_more_passive({"side": "bid", "price": "0.0100"}) is None
    assert is_post_only_cross(RuntimeError('400: {"details":"post only cross"}'))
    assert is_post_only_cross(RuntimeError("invalid_order POST_ONLY_CROSS"))
    assert not is_post_only_cross(RuntimeError("400 invalid_order"))


def test_live_retries_post_only_cross_one_tick_more_passive(tmp_path: Path):
    client = MagicMock()
    client.get_orders.return_value = []
    client.get_market.side_effect = RuntimeError("skip refresh")
    prices: list[str] = []
    ids: list[str] = []

    def create(payload):
        prices.append(payload["price"])
        ids.append(payload["client_order_id"])
        if payload["price"] == "0.5100":
            raise RuntimeError('400 on /portfolio/events/orders: {"error":{"details":"post only cross"}}')
        return {"order": {"order_id": "rested", "fill_count": "0.00", "remaining_count": "4.00"}}

    client.create_order.side_effect = create
    out = execute_ideas(
        [_idea()],
        client=client,
        artifacts_dir=tmp_path,
        live=True,
        confirm_live=True,
        run_id="test-run",
    )
    assert prices == ["0.5100", "0.5000"]
    assert ids[0] != ids[1]
    assert out["placed"][0]["order_id"] == "rested"
    assert out["orders"][0]["price"] == "0.5000"
    assert out["orders"][0]["post_only"] is True


def test_live_requotes_from_fresh_book_before_post(tmp_path: Path):
    client = MagicMock()
    client.get_orders.return_value = []
    client.get_market.return_value = {
        "yes_bid_dollars": "0.1200",
        "yes_ask_dollars": "0.1300",
        "no_bid_dollars": "0.8700",
        "no_ask_dollars": "0.8800",
    }
    client.create_order.return_value = {
        "order": {"order_id": "fresh", "fill_count": "0.00", "remaining_count": "4.00"}
    }
    out = execute_ideas(
        [_idea()],
        client=client,
        artifacts_dir=tmp_path,
        live=True,
        confirm_live=True,
        run_id="test-run",
    )
    sent = client.create_order.call_args.args[0]
    # 0.12/0.13 is 1¢ wide → join the bid, do not post 0.13 (that takes).
    assert sent["price"] == "0.1200"
    assert sent["post_only"] is True
    assert out["placed"][0]["order_id"] == "fresh"
