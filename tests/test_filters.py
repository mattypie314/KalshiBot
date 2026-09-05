"""Hard filters: 99¢ favorites out, executable prices only."""

from datetime import datetime, timedelta, timezone

from dataclasses import replace

from src.filters import (
    TURBO_PREFERRED_RISK_DOLLARS,
    FilterConfig,
    evaluate_market,
    maker_limit,
    rank_actionable_ideas,
)
from src.journal import TURBO_LABEL, forced_ticket_fields, new_trade_row
from src.markets import HourlyMarket
from src.paper import paper_row_from_idea


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


def test_filters_sit_when_spot_is_exchange_proxy():
    market = _market(threshold=77000.0, yes_bid=0.80, yes_ask=0.82, no_bid=0.18, no_ask=0.20)
    result = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=CFG,
        settlement_index=False,
    )
    assert result.idea is None
    assert any("proxy" in r.lower() or "brti" in r.lower() for r in result.avoid_reasons)


def test_filters_sit_when_cannot_rest_maker():
    # Locked book: would have to lift. Do not cross for a 6% edge.
    market = _market(
        threshold=77000.0,
        yes_bid=0.82,
        yes_ask=0.82,
        no_bid=0.18,
        no_ask=0.18,
        yes_sub_title="$77,000 or above",
    )
    result = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=CFG,
    )
    assert result.idea is None
    assert any("will not cross" in r.lower() or "maker" in r.lower() for r in result.avoid_reasons)


def test_filters_sit_on_news_pause_hook():
    market = _market(threshold=77000.0)
    cfg = replace(CFG, news_pause=True)
    result = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=cfg,
    )
    assert result.idea is None
    assert any("news_pause" in r.lower() for r in result.avoid_reasons)


def test_maker_limit_never_sits_on_or_through_the_ask():
    assert maker_limit("Yes", 0.12, 0.14) == 0.13
    assert maker_limit("Yes", 0.13, 0.14) == 0.13
    assert maker_limit("Yes", 0.13, 0.13) == 0.12
    assert maker_limit("Yes", 0.14, 0.13) == 0.12
    assert maker_limit("No", 0.86, 0.88) == 0.87


def _close_strike_eth():
    return _market(
        asset="ETH",
        threshold=2375.0,
        yes_sub_title="$2,375 or above",
        yes_bid=0.62,
        yes_ask=0.64,
        no_bid=0.36,
        no_ask=0.38,
    )


def test_force_near_rule_default_off_still_rejects_close_strike():
    result = evaluate_market(
        _close_strike_eth(),
        spot=2390.0,
        hourly_vol=0.005,
        now=datetime.now(timezone.utc),
        cfg=CFG,
        vol_fallback=0.005,
    )
    assert CFG.force_near_rule is False
    assert result.idea is None
    assert any("close strike" in r.lower() for r in result.avoid_reasons)


def test_force_near_rule_makes_close_strike_actionable_maker():
    cfg = replace(CFG, force_near_rule=True)
    result = evaluate_market(
        _close_strike_eth(),
        spot=2390.0,
        hourly_vol=0.005,
        now=datetime.now(timezone.utc),
        cfg=cfg,
        vol_fallback=0.005,
    )
    assert result.idea is not None
    assert result.idea.post_maker is True
    assert result.idea.limit_price < result.idea.entry_price
    assert result.idea.forced is True
    assert result.idea.force_near_rule is True
    assert result.idea.risk_dollars <= TURBO_PREFERRED_RISK_DOLLARS + 1e-9
    assert result.idea.risk_dollars <= cfg.max_risk_dollars + 1e-9
    assert any(TURBO_LABEL in item for item in result.idea.rationale)


def test_force_near_rule_still_never_crosses():
    market = _market(
        threshold=77000.0,
        yes_bid=0.82,
        yes_ask=0.82,
        no_bid=0.18,
        no_ask=0.18,
        yes_sub_title="$77,000 or above",
    )
    result = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=replace(CFG, force_near_rule=True),
    )
    assert result.idea is None
    assert any("will not cross" in r.lower() or "maker" in r.lower() for r in result.avoid_reasons)


def test_force_near_rule_softens_min_edge():
    # Far enough to clear the close-strike bar, but Yes ask is rich: ~4% net < 6%.
    market = _market(
        threshold=77650.0,
        yes_bid=0.90,
        yes_ask=0.94,
        no_bid=0.06,
        no_ask=0.10,
        yes_sub_title="$77,650 or above",
    )
    off = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=CFG,
        vol_fallback=0.004,
    )
    on = evaluate_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=replace(CFG, force_near_rule=True),
        vol_fallback=0.004,
    )
    assert off.idea is None
    assert any("net edge" in r.lower() for r in off.avoid_reasons)
    assert on.idea is not None
    assert on.idea.side == "Yes"
    assert on.idea.post_maker is True
    assert on.idea.forced is True
    assert on.idea.limit_price < on.idea.entry_price


def test_force_near_rule_picks_nearest_strike():
    near = evaluate_market(
        _close_strike_eth(),
        spot=2390.0,
        hourly_vol=0.005,
        now=datetime.now(timezone.utc),
        cfg=replace(CFG, force_near_rule=True),
        vol_fallback=0.005,
    )
    far = evaluate_market(
        _market(
            threshold=77600.0,
            yes_sub_title="$77,600 or above",
            yes_bid=0.18,
            yes_ask=0.20,
            no_bid=0.80,
            no_ask=0.82,
        ),
        spot=76800.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=replace(CFG, force_near_rule=True),
        vol_fallback=0.004,
    )
    assert near.idea is not None and far.idea is not None
    ranked = rank_actionable_ideas([far.idea, near.idea], force_near_rule=True)
    assert ranked[0].strike_distance_pct <= ranked[1].strike_distance_pct
    strict = rank_actionable_ideas([near.idea, far.idea], force_near_rule=False)
    assert strict[0].net_edge >= strict[1].net_edge


def test_force_near_rule_labels_journal_and_paper():
    cfg = replace(CFG, force_near_rule=True)
    result = evaluate_market(
        _close_strike_eth(),
        spot=2390.0,
        hourly_vol=0.005,
        now=datetime.now(timezone.utc),
        cfg=cfg,
        vol_fallback=0.005,
    )
    assert result.idea is not None
    row = new_trade_row(
        ticker=result.idea.market.ticker,
        asset=result.idea.market.asset,
        side=result.idea.side,
        strike=result.idea.market.threshold,
        spot=result.idea.spot,
        minutes_left=result.idea.minutes_left,
        fair=result.idea.fair,
        kalshi_price=result.idea.entry_price,
        limit_price=result.idea.limit_price,
        contracts=result.idea.contracts,
        risk_dollars=result.idea.risk_dollars,
        hourly_vol=0.005,
        source="cfbenchmarks",
        forced=result.idea.forced,
        force_near_rule=result.idea.force_near_rule,
    )
    assert row["forced"] is True
    assert row["turbo"] is True
    assert row["force_near_rule"] is True
    assert row["label"] == TURBO_LABEL
    paper = paper_row_from_idea(result.idea, spot_source="cfbenchmarks")
    assert paper["forced"] is True
    assert paper["turbo"] is True
    assert paper["force_near_rule"] is True
    assert paper["label"] == TURBO_LABEL
    fields = forced_ticket_fields(forced=False)
    assert fields["forced"] is False
    assert fields["label"] == ""
    from src.config import HourlySettings
    from src.main import scan_log_row
    from src.spot import SpotSnapshot

    scan = scan_log_row(
        now=datetime.now(timezone.utc),
        spots=SpotSnapshot(prices={"ETH": 2390.0}, sources={"ETH": "cfbenchmarks"}, hourly_vol={"ETH": 0.005}),
        markets=[result.idea.market],
        ideas=[result.idea],
        nearby=[],
        avoided=[],
        settings=HourlySettings(_env_file=None, force_near_rule=True, halted=True),
        action="scan",
    )
    assert scan["force_near_rule"] is True
    assert scan["forced"] is True
    assert scan["label"] == TURBO_LABEL
    assert scan["ideas"][0]["forced"] is True
    assert scan["ideas"][0]["label"] == TURBO_LABEL
