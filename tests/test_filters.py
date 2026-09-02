"""Hard filters: 99¢ favorites out, executable prices only."""

from datetime import datetime, timedelta, timezone

from src.filters import FilterConfig, evaluate_market, maker_limit
from src.markets import HourlyMarket


def _market(**kwargs) -> HourlyMarket:
    close = datetime.now(timezone.utc) + timedelta(minutes=25)
    defaults = dict(
        ticker="KXBTCD-26SEP0107-T78099.99",
        event_ticker="KXBTCD-26SEP0107",
        series_ticker="KXBTCD",
        asset="BTC",
        title="BTC price on Sep 1, 2026 at 7am EDT?",
        yes_sub_title="$78,100 or above",
        threshold=78099.99,
        strike_type="greater",
        close_time=close,
        status="active",
        yes_bid=0.52,
        yes_ask=0.54,
        no_bid=0.46,
        no_ask=0.48,
        yes_bid_size=20,
        yes_ask_size=20,
        no_bid_size=20,
        no_ask_size=20,
        rules_primary="CF Benchmarks BRTI average.",
        rules_secondary="",
        settlement_source="CF Benchmarks BRTI",
        exchange_index=2,
    )
    defaults.update(kwargs)
    return HourlyMarket(**defaults)


CFG = FilterConfig(
    min_net_edge=0.06,
    soft_net_edge=0.06,
    max_spread=0.06,
    min_minutes_left=3,
    min_visible_depth=5,
    min_price=0.05,
    max_price=0.95,
    fat_tail_z=2.5,
    fat_tail_edge=0.08,
    news_blackout=False,
)


def test_filters_reject_99_cent_favorites():
    market = _market(yes_bid=0.98, yes_ask=0.99, no_bid=0.01, no_ask=0.02)
    result = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=CFG,
    )
    assert result.idea is None
    assert any("0.05" in r or "0.95" in r or "favorite" in r.lower() or "price" in r.lower() for r in result.avoid_reasons)


def test_filters_reject_closed_or_too_little_time():
    market = _market(close_time=datetime.now(timezone.utc) + timedelta(minutes=2))
    result = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=CFG,
    )
    assert result.idea is None
    assert any("minute" in r.lower() or "time" in r.lower() for r in result.avoid_reasons)


def test_filters_use_ask_not_mid_for_edge():
    # Fair ~50% ATM. Mid would look flatter; ask at 0.70 has no 6% edge.
    market = _market(yes_bid=0.48, yes_ask=0.70, no_bid=0.30, no_ask=0.52, threshold=78120.0)
    result = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=CFG,
    )
    assert result.idea is None


def test_filters_pass_clear_yes_edge_on_executable_ask():
    # Spot well above threshold, ~25 minutes left, yes ask is cheap.
    market = _market(
        threshold=77000.0,
        yes_bid=0.80,
        yes_ask=0.82,
        no_bid=0.18,
        no_ask=0.20,
        yes_sub_title="$77,000 or above",
    )
    result = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=CFG,
    )
    assert result.idea is not None
    assert result.idea.side == "Yes"
    assert result.idea.entry_price == 0.82
    assert result.idea.net_edge >= 0.06


def test_filters_reject_close_strike_fade():
    # ETH No $2,375 while spot is $2,390 — 0.63% is a coin-flip fade.
    market = _market(
        asset="ETH",
        threshold=2375.0,
        yes_sub_title="$2,375 or above",
        yes_bid=0.62,
        yes_ask=0.64,
        no_bid=0.36,
        no_ask=0.38,
    )
    result = evaluate_market(
        market,
        spot=2390.0,
        hourly_vol=0.005,
        now=datetime.now(timezone.utc),
        cfg=CFG,
        vol_fallback=0.005,
    )
    assert result.idea is None
    assert any("close strike" in r.lower() for r in result.avoid_reasons)


def test_filters_keep_far_strike_with_edge():
    # BTC No $77,600 while spot is $76,800 — ~1.0% away, the keeper shape.
    market = _market(
        threshold=77600.0,
        yes_sub_title="$77,600 or above",
        yes_bid=0.18,
        yes_ask=0.20,
        no_bid=0.80,
        no_ask=0.82,
    )
    result = evaluate_market(
        market,
        spot=76800.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=CFG,
        vol_fallback=0.004,
    )
    assert result.idea is not None
    assert result.idea.side == "No"
    assert result.idea.strike_distance_pct >= 0.005


def test_filters_sit_on_elevated_vol():
    market = _market(threshold=77000.0)
    result = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.010,
        now=datetime.now(timezone.utc),
        cfg=CFG,
        vol_fallback=0.004,
    )
    assert result.idea is None
    assert any("elevated vol" in r.lower() for r in result.avoid_reasons)


def test_maker_limit_never_sits_on_or_through_the_ask():
    assert maker_limit("Yes", 0.12, 0.14) == 0.13
    assert maker_limit("Yes", 0.13, 0.14) == 0.13
    assert maker_limit("Yes", 0.13, 0.13) == 0.12
    assert maker_limit("Yes", 0.14, 0.13) == 0.12
    assert maker_limit("No", 0.86, 0.88) == 0.87
