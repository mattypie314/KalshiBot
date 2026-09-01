import asyncio

from kalshibot.charts import market_chart, pick_interval, point_from_candle


def test_point_prefers_last_trade_then_mid():
    point = point_from_candle(
        {
            "end_period_ts": 1_700_000_000,
            "price": {"close_dollars": "0.62"},
            "yes_bid": {"close_dollars": "0.60"},
            "yes_ask": {"close_dollars": "0.64"},
        }
    )
    assert point is not None
    assert point["yes"] == 0.62
    assert point["bid"] == 0.60
    assert point["ask"] == 0.64

    mid = point_from_candle(
        {
            "end_period_ts": 1_700_000_060,
            "price": {},
            "yes_bid": {"close_dollars": "0.40"},
            "yes_ask": {"close_dollars": "0.50"},
        }
    )
    assert mid is not None
    assert mid["yes"] == 0.45


def test_point_skips_empty_book():
    assert point_from_candle({"end_period_ts": 1, "price": {}, "yes_bid": {"close_dollars": "0.0000"}, "yes_ask": {"close_dollars": "0.0000"}}) is None
    assert point_from_candle({"end_period_ts": 1, "price": {}, "yes_bid": {}, "yes_ask": {}}) is None


def test_pick_interval():
    assert pick_interval(2) == 1
    assert pick_interval(24) == 60
    assert pick_interval(72) == 1440


class _FakeKalshi:
    async def get_json(self, path, params=None):
        if "candlesticks" in path:
            return {
                "ticker": "KXBTC-TEST",
                "candlesticks": [
                    {
                        "end_period_ts": 1_700_000_000,
                        "price": {"close_dollars": "0.55"},
                        "yes_bid": {"close_dollars": "0.54"},
                        "yes_ask": {"close_dollars": "0.56"},
                    }
                ],
            }
        return {"market": {"yes_bid_dollars": "0.57", "yes_ask_dollars": "0.59", "status": "active", "title": "BTC"}}


def test_market_chart_appends_live_mid():
    data = asyncio.run(market_chart(_FakeKalshi(), "KXBTC", "KXBTC-TEST", hours=4))
    assert data["ticker"] == "KXBTC-TEST"
    assert data["interval"] == 1
    assert len(data["points"]) >= 2
    assert data["points"][0]["yes"] == 0.55
    assert data["live"]["yes"] == 0.58
    assert data["change"] is not None
