from src.exposure import blocks_new_idea, open_hourly_tickets, ticket_asset
from src.filters import Idea
from src.markets import HourlyMarket
from datetime import datetime, timedelta, timezone


def _idea(asset: str, side: str, ticker: str) -> Idea:
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
        gross_edge=0.1,
        net_edge=0.08,
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


def test_blocks_stacked_same_direction_btc_and_eth_nos():
    """2026-09-02 losing card: BTC No + ETH No in one hour."""
    eth_no = [{"ticker": "KXETHD-1", "side": "No", "asset": "ETH"}]
    assert blocks_new_idea(eth_no, _idea("BTC", "No", "KXBTCD-2"))


def test_blocks_same_direction_and_allows_opposite_other_coin():
    eth_no = [{"ticker": "KXETHD-1", "side": "No", "asset": "ETH"}]
    assert blocks_new_idea(eth_no, _idea("ETH", "No", "KXETHD-2"))
    assert blocks_new_idea(eth_no, _idea("BTC", "No", "KXBTCD-2"))
    assert blocks_new_idea(eth_no, _idea("ETH", "Yes", "KXETHD-2"))
    assert blocks_new_idea(eth_no, _idea("BTC", "Yes", "KXBTCD-2")) is None


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
