from src.config import HourlySettings
from src.exposure import (
    blocks_new_idea,
    open_hourly_tickets,
    select_ideas_per_asset,
    ticket_asset,
)
from src.fifteen.config import FifteenSettings
from src.filters import Idea
from src.markets import HourlyMarket
from datetime import datetime, timedelta, timezone


def _idea(asset: str, side: str, ticker: str, net_edge: float = 0.08) -> Idea:
    market = HourlyMarket(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        series_ticker="KXETHD" if asset == "ETH" else "KXBTCD",
        asset=asset,
        title="t",
        yes_sub_title="$1 or above",
        threshold=100.0,
        strike_type="greater",
        close_time=datetime.now(timezone.utc) + timedelta(minutes=20),
        status="active",
        yes_bid=0.5,
        yes_ask=0.52,
        no_bid=0.48,
        no_ask=0.5,
        yes_bid_size=10,
        yes_ask_size=10,
        no_bid_size=10,
        no_ask_size=10,
        rules_primary="",
        rules_secondary="",
        settlement_source="CF",
        exchange_index=2,
    )
    return Idea(
        market=market,
        side=side,
        entry_price=0.5,
        limit_price=0.49,
        fair=0.6,
        gross_edge=net_edge,
        net_edge=net_edge,
        fee_per_contract=0.01,
        fee_total=0.02,
        z=1.0,
        hours_left=0.3,
        contracts=3,
        risk_dollars=1.5,
        max_loss=1.5,
        rationale=[],
        post_maker=True,
    )


def test_max_ideas_per_run_defaults_to_two():
    assert HourlySettings.model_fields["max_ideas_per_run"].default == 2
    assert FifteenSettings.model_fields["max_ideas_per_run"].default == 2


def test_select_both_assets_pass_returns_both():
    btc = _idea("BTC", "Yes", "KXBTCD-1", 0.12)
    eth = _idea("ETH", "Yes", "KXETHD-1", 0.10)
    chosen, extra = select_ideas_per_asset([btc, eth], max_per_asset=1, max_ideas=2)
    assert {row.market.asset for row in chosen} == {"BTC", "ETH"}
    assert extra == []


def test_select_two_btc_passes_keeps_only_best_btc():
    best = _idea("BTC", "Yes", "KXBTCD-1", 0.14)
    worse = _idea("BTC", "No", "KXBTCD-2", 0.11)
    eth = _idea("ETH", "Yes", "KXETHD-1", 0.09)
    chosen, extra = select_ideas_per_asset(
        [best, worse, eth],
        max_per_asset=1,
        max_ideas=2,
    )
    assert [row.market.ticker for row in chosen] == ["KXBTCD-1", "KXETHD-1"]
    assert [row.market.ticker for row in extra] == ["KXBTCD-2"]


def test_open_btc_yes_does_not_block_eth_yes():
    btc_yes = [{"ticker": "KXBTCD-1", "side": "Yes", "asset": "BTC"}]
    assert blocks_new_idea(btc_yes, _idea("ETH", "Yes", "KXETHD-2")) is None


def test_open_btc_still_blocks_second_btc():
    btc_yes = [{"ticker": "KXBTCD-1", "side": "Yes", "asset": "BTC"}]
    assert blocks_new_idea(btc_yes, _idea("BTC", "No", "KXBTCD-2"))
    assert blocks_new_idea(btc_yes, _idea("BTC", "Yes", "KXBTCD-3"))


def test_blocks_same_coin_and_allows_other_coin_any_side():
    eth_no = [{"ticker": "KXETHD-1", "side": "No", "asset": "ETH"}]
    assert blocks_new_idea(eth_no, _idea("ETH", "No", "KXETHD-2"))
    assert blocks_new_idea(eth_no, _idea("ETH", "Yes", "KXETHD-2"))
    assert blocks_new_idea(eth_no, _idea("BTC", "No", "KXBTCD-2")) is None
    assert blocks_new_idea(eth_no, _idea("BTC", "Yes", "KXBTCD-2")) is None


def test_two_open_hourly_tickets_block_a_third():
    open_two = [
        {"ticker": "KXBTCD-1", "side": "Yes", "asset": "BTC"},
        {"ticker": "KXETHD-1", "side": "No", "asset": "ETH"},
    ]
    assert blocks_new_idea(open_two, _idea("BTC", "No", "KXBTCD-3"))


def test_open_hourly_tickets_merges_state_and_rests():
    class Client:
        def get_orders(self, status="resting"):
            return [
                {"order_id": "r1", "ticker": "KXBTCD-26SEP0211-T77600", "side": "ask"},
                {"order_id": "x", "ticker": "KXBTC15M-1", "side": "bid"},
            ]

    tickets = open_hourly_tickets(
        Client(),
        {"last_ticker": "KXETHD-26SEP0213-T2375", "last_side": "No"},
    )
    tickers = {row["ticker"] for row in tickets}
    assert "KXETHD-26SEP0213-T2375" in tickers
    assert "KXBTCD-26SEP0211-T77600" in tickers
    assert "KXBTC15M-1" not in tickers
    assert ticket_asset("KXETHD-1") == "ETH"
