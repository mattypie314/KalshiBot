"""15m BTC/ETH hard skips and Pass/Fail vs live mid."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.clock import minutes_into_15m, minutes_left_in_15m, to_et
from src.fees import ev_per_contract, taker_fee_dollars
from src.filters import FilterResult, Idea, maker_limit, news_blackout_active
from src.journal import strike_distance_pct, trade_bucket
from src.markets import FIFTEEN_SERIES, HourlyMarket, is_btc_eth_fifteen_series
from src.model import fair_no, fair_prob, hours_left, model_z
from src.sizer import size_idea


@dataclass(frozen=True)
class FifteenFilterConfig:
    mid_tolerance: float = 0.04
    min_minutes_left: float = 8.0
    edge_loop_min_into: float = 1.0
    edge_loop_max_into: float = 4.0
    last_minute_maker: bool = True
    last_minute_minutes: float = 3.0
    last_minute_min_price: float = 0.74
    last_minute_max_price: float = 0.93
    last_minute_min_risk: float = 0.10
    last_minute_max_risk: float = 0.75
    stack_last_minute_with_edge: bool = False
    require_settlement_index: bool = True
    require_maker: bool = True
    news_pause: bool = False
    vol_pause_mult: float = 2.0
    min_risk_dollars: float = 0.10
    max_risk_dollars: float = 1.50
    preferred_risk_dollars: float = 0.85
    max_risk_pct: float = 0.05
    kelly_mult: float = 0.25
    bankroll: float = 5.00
    pot_room: float = 5.00
    shard2_cash: float = 5.00
    revenge: bool = False
    daily_losses: int = 0
    max_daily_losses: int = 3
    idea_this_window: bool = False
    half_sigma_recheck: bool = False
    decided_late: bool = False


@dataclass
class FifteenPhase:
    name: str
    minutes_into: float
    minutes_left: float
    allow_edge: bool
    allow_last_minute: bool
    skip_reason: str = ""


def live_mid(bid: float, ask: float) -> float | None:
    if bid <= 0 or ask <= 0:
        return None
    return round((bid + ask) / 2.0, 4)


def model_near_mid(fair: float, mid: float, tolerance: float = 0.04) -> bool:
    return abs(fair - mid) < tolerance - 1e-12


def spread_wider_than_edge(spread: float, edge: float) -> bool:
    return spread > edge + 1e-12


def classify_phase(
    now: datetime,
    cfg: FifteenFilterConfig,
    *,
    close: datetime | None = None,
) -> FifteenPhase:
    into = minutes_into_15m(now)
    if close is not None:
        left = max(0.0, (close - to_et(now)).total_seconds() / 60.0)
    else:
        left = minutes_left_in_15m(now)
    in_edge = cfg.edge_loop_min_into - 1e-9 <= into <= cfg.edge_loop_max_into + 1e-9
    if cfg.half_sigma_recheck and into > cfg.edge_loop_max_into:
        in_edge = True
    in_last = cfg.last_minute_maker and left <= cfg.last_minute_minutes + 1e-9
    if in_edge and left < cfg.min_minutes_left and not cfg.decided_late and not in_last:
        in_edge = False
    if in_edge:
        return FifteenPhase("edge", into, left, True, False)
    if in_last:
        if cfg.idea_this_window and not cfg.stack_last_minute_with_edge:
            return FifteenPhase(
                "last_minute",
                into,
                left,
                False,
                False,
                skip_reason="last-minute maker would stack with edge-loop same window",
            )
        return FifteenPhase("last_minute", into, left, False, True)
    if left < cfg.min_minutes_left and not cfg.decided_late:
        return FifteenPhase(
            "sit",
            into,
            left,
            False,
            False,
            skip_reason=f"under {cfg.min_minutes_left:g}m left and not a decided last-minute look",
        )
    return FifteenPhase(
        "sit",
        into,
        left,
        False,
        False,
        skip_reason=f"outside edge-loop ({cfg.edge_loop_min_into:g}–{cfg.edge_loop_max_into:g}m into window)",
    )


def hard_skip_reasons(cfg: FifteenFilterConfig, phase: FifteenPhase) -> list[str]:
    reasons: list[str] = []
    if cfg.news_pause:
        reasons.append("NEWS_PAUSE — operator sit (headline / war tape)")
    if cfg.revenge:
        reasons.append("revenge: skip after a filled 15m loss")
    if cfg.max_daily_losses > 0 and cfg.daily_losses >= cfg.max_daily_losses:
        reasons.append(
            f"three 15m losses same ET day ({cfg.daily_losses} ≥ {cfg.max_daily_losses})"
        )
    if cfg.idea_this_window and not (phase.allow_last_minute and cfg.stack_last_minute_with_edge):
        reasons.append("one idea per 15m window already taken")
    if phase.skip_reason:
        reasons.append(phase.skip_reason)
    if cfg.pot_room <= 0:
        reasons.append("15m pot is empty — HALTED")
    return reasons


def _cap_risk(cfg: FifteenFilterConfig, preferred: float, ceiling: float) -> float:
    room = min(cfg.pot_room, cfg.shard2_cash, cfg.bankroll, ceiling)
    return max(0.0, min(preferred, room))


def evaluate_fifteen_market(
    market: HourlyMarket,
    *,
    spot: float,
    hourly_vol: float,
    now: datetime,
    cfg: FifteenFilterConfig,
    vol_fallback: float | None = None,
    settlement_index: bool = True,
    phase: FifteenPhase | None = None,
) -> FilterResult:
    phase = phase or classify_phase(now, cfg, close=market.close_time)
    reasons = hard_skip_reasons(cfg, phase)
    if reasons and not (phase.allow_edge or phase.allow_last_minute):
        return FilterResult(market=market, avoid_reasons=reasons)
    if reasons and "one idea per 15m window" in " ".join(reasons):
        return FilterResult(market=market, avoid_reasons=reasons)
    if cfg.news_pause or cfg.revenge or cfg.daily_losses >= cfg.max_daily_losses:
        return FilterResult(market=market, avoid_reasons=reasons or ["hard skip"])
    if not is_btc_eth_fifteen_series(market.series_ticker):
        return FilterResult(
            market=market,
            avoid_reasons=[f"series {market.series_ticker} is not KXBTC15M/KXETH15M"],
        )
    if market.series_ticker not in FIFTEEN_SERIES:
        return FilterResult(market=market, avoid_reasons=["hourly / other series excluded"])
    if market.exchange_index is not None and market.exchange_index != 2:
        return FilterResult(
            market=market,
            avoid_reasons=[f"exchange_index {market.exchange_index} is not crypto shard 2"],
        )
    if market.status not in {"open", "active"}:
        return FilterResult(market=market, avoid_reasons=["market not open"])
    if cfg.require_settlement_index and not settlement_index:
        return FilterResult(
            market=market,
            avoid_reasons=[
                "spot is not CF Benchmarks BRTI/ETHUSD_RTI — Coinbase is a display proxy; sit"
            ],
        )
    if news_blackout_active(now):
        return FilterResult(market=market, avoid_reasons=["scheduled CPI/FOMC news window — sit"])

    fallback = vol_fallback if vol_fallback and vol_fallback > 0 else hourly_vol
    if fallback > 0 and hourly_vol >= cfg.vol_pause_mult * fallback:
        return FilterResult(
            market=market,
            avoid_reasons=[
                f"news candle: vol {hourly_vol:.2%} ≥ {cfg.vol_pause_mult:g}× typical {fallback:.2%}"
            ],
        )

    threshold = market.threshold
    if threshold <= 0:
        threshold = spot
    secs = (market.close_time - now).total_seconds()
    hrs = hours_left(secs)
    if hrs is None:
        return FilterResult(market=market, avoid_reasons=["close_time is not in the future"])
    minutes = secs / 60.0

    z = model_z(spot, threshold, hourly_vol, hrs)
    fair_yes = fair_prob(spot, threshold, hourly_vol, hrs)
    fair_n = fair_no(spot, threshold, hourly_vol, hrs)
    yes_mid = live_mid(market.yes_bid, market.yes_ask)
    no_mid = live_mid(market.no_bid, market.no_ask)
    spread = max(0.0, market.yes_ask - market.yes_bid) if market.yes_ask and market.yes_bid else 1.0

    if phase.allow_last_minute:
        return _evaluate_last_minute(
            market,
            cfg=cfg,
            spot=spot,
            hourly_vol=hourly_vol,
            minutes=minutes,
            hrs=hrs,
            z=z,
            fair_yes=fair_yes,
            fair_n=fair_n,
            spread=spread,
        )

    if not phase.allow_edge:
        return FilterResult(market=market, avoid_reasons=reasons or [phase.skip_reason or "not in edge loop"])

    candidates: list[tuple[str, float, float, float]] = []
    if yes_mid is not None:
        candidates.append(("Yes", market.yes_ask, market.yes_bid, fair_yes))
    if no_mid is not None:
        candidates.append(("No", market.no_ask, market.no_bid, fair_n))
    if not candidates:
        return FilterResult(market=market, avoid_reasons=["no live mid"])

    best: Idea | None = None
    nearby = ""
    local_reasons = list(reasons)
    for side, ask, bid, fair in candidates:
        mid = live_mid(bid, ask)
        if mid is None:
            continue
        if model_near_mid(fair, mid, cfg.mid_tolerance):
            local_reasons.append(
                f"{side}: model fair {fair:.2f} within {cfg.mid_tolerance:.2f} of mid {mid:.2f}"
            )
            continue
        edge_vs_mid = fair - mid
        if edge_vs_mid <= 0:
            local_reasons.append(f"{side}: fail vs mid (fair {fair:.2f} ≤ mid {mid:.2f})")
            continue
        if spread_wider_than_edge(spread, edge_vs_mid):
            local_reasons.append(
                f"{side}: spread {spread:.3f} wider than edge vs mid {edge_vs_mid:.3f}"
            )
            continue
        limit = maker_limit(side, bid, ask)
        if cfg.require_maker and (limit is None or abs(limit - ask) < 1e-9):
            local_reasons.append(f"{side}: cannot rest a maker limit — will not cross")
            continue
        ceiling = cfg.max_risk_dollars
        preferred = _cap_risk(cfg, cfg.preferred_risk_dollars, ceiling)
        if preferred < cfg.min_risk_dollars - 1e-12:
            local_reasons.append(f"{side}: remaining pot/shard-2 room ${preferred:.2f} below min risk")
            continue
        trial = size_idea(
            bankroll=cfg.bankroll,
            entry_price=ask,
            p_hat=fair,
            kelly_mult=cfg.kelly_mult,
            max_risk_pct=cfg.max_risk_pct,
            max_risk_dollars=min(ceiling, preferred, cfg.pot_room, cfg.shard2_cash),
            preferred_risk_dollars=preferred,
        )
        if trial.skip or trial.contracts < 1:
            local_reasons.append(f"{side}: sizer skip ({trial.reason})")
            continue
        if trial.risk_dollars + 1e-12 < cfg.min_risk_dollars and ask > cfg.max_risk_dollars:
            local_reasons.append(f"{side}: ticket below min risk")
            continue
        fee_total = taker_fee_dollars(trial.contracts, ask)
        fee_each = fee_total / max(trial.contracts, 1)
        gross, net = ev_per_contract(fair, ask, fee_each)
        distance = strike_distance_pct(spot, threshold)
        idea = Idea(
            market=market,
            side=side,
            entry_price=ask,
            limit_price=limit or ask,
            fair=fair,
            gross_edge=gross,
            net_edge=net,
            fee_per_contract=fee_each,
            fee_total=fee_total,
            z=z,
            hours_left=hrs,
            contracts=trial.contracts,
            risk_dollars=trial.risk_dollars,
            max_loss=trial.risk_dollars + fee_total,
            rationale=[
                f"15m edge loop: model fair {fair:.1%} vs live mid {mid:.2f} (pass).",
                f"Executable {side} ask {ask:.2f}; maker limit {limit or ask:.2f}.",
                f"z={z:.2f}, minutes_left={minutes:.1f}, hourly_vol={hourly_vol:.3%} "
                f"(15m lookback heuristic).",
                f"Settlement must be BRTI / ETHUSD_RTI. Source: {market.settlement_source}.",
            ],
            post_maker=bool(limit) and limit + 1e-9 < ask,
            strike_distance_pct=distance,
            spot=spot,
            minutes_left=minutes,
            bucket=trade_bucket(side, distance),
        )
        if best is None or idea.net_edge > best.net_edge:
            best = idea
        if net >= 0.02:
            nearby = f"{side} net {net:.1%} vs mid {mid:.2f}"

    if best:
        return FilterResult(market=market, idea=best)
    return FilterResult(
        market=market,
        nearby=bool(nearby),
        watch_note=nearby,
        avoid_reasons=local_reasons or ["no 15m pass vs mid"],
    )


def _evaluate_last_minute(
    market: HourlyMarket,
    *,
    cfg: FifteenFilterConfig,
    spot: float,
    hourly_vol: float,
    minutes: float,
    hrs: float,
    z: float,
    fair_yes: float,
    fair_n: float,
    spread: float,
) -> FilterResult:
    reasons: list[str] = []
    best: Idea | None = None
    for side, ask, bid, fair in (
        ("Yes", market.yes_ask, market.yes_bid, fair_yes),
        ("No", market.no_ask, market.no_bid, fair_n),
    ):
        if not (cfg.last_minute_min_price - 1e-9 <= ask <= cfg.last_minute_max_price + 1e-9):
            reasons.append(
                f"{side}: ask {ask:.2f} outside last-minute favorite band "
                f"{cfg.last_minute_min_price:.2f}–{cfg.last_minute_max_price:.2f}"
            )
            continue
        mid = live_mid(bid, ask)
        if mid is None:
            continue
        edge_vs_mid = fair - mid
        if spread_wider_than_edge(spread, max(edge_vs_mid, 0.01)):
            reasons.append(f"{side}: spread {spread:.3f} wider than last-minute room")
            continue
        limit = maker_limit(side, bid, ask)
        if cfg.require_maker and (limit is None or abs(limit - ask) < 1e-9):
            reasons.append(f"{side}: last-minute cannot rest maker — will not cross")
            continue
        preferred = _cap_risk(cfg, min(cfg.last_minute_max_risk, cfg.preferred_risk_dollars), cfg.last_minute_max_risk)
        if preferred < cfg.last_minute_min_risk - 1e-12:
            reasons.append(f"{side}: last-minute size room ${preferred:.2f} too small")
            continue
        trial = size_idea(
            bankroll=cfg.bankroll,
            entry_price=ask,
            p_hat=max(fair, ask + 0.02),
            kelly_mult=cfg.kelly_mult,
            max_risk_pct=cfg.max_risk_pct,
            max_risk_dollars=min(cfg.last_minute_max_risk, cfg.pot_room, cfg.shard2_cash),
            preferred_risk_dollars=max(cfg.last_minute_min_risk, preferred),
        )
        if trial.skip or trial.contracts < 1:
            reasons.append(f"{side}: last-minute sizer skip ({trial.reason})")
            continue
        fee_total = taker_fee_dollars(trial.contracts, ask)
        fee_each = fee_total / max(trial.contracts, 1)
        gross, net = ev_per_contract(fair, ask, fee_each)
        distance = strike_distance_pct(spot, market.threshold or spot)
        idea = Idea(
            market=market,
            side=side,
            entry_price=ask,
            limit_price=limit or ask,
            fair=fair,
            gross_edge=gross,
            net_edge=net,
            fee_per_contract=fee_each,
            fee_total=fee_total,
            z=z,
            hours_left=hrs,
            contracts=trial.contracts,
            risk_dollars=trial.risk_dollars,
            max_loss=trial.risk_dollars + fee_total,
            rationale=[
                f"Optional last-minute maker: favorite {ask:.2f} in "
                f"{cfg.last_minute_min_price:.2f}–{cfg.last_minute_max_price:.2f}.",
                f"Size capped ${cfg.last_minute_min_risk:.2f}–${cfg.last_minute_max_risk:.2f}.",
                f"minutes_left={minutes:.1f}. Maker only; never cross.",
            ],
            post_maker=bool(limit) and limit + 1e-9 < ask,
            strike_distance_pct=distance,
            spot=spot,
            minutes_left=minutes,
            bucket="last_minute",
        )
        if best is None or idea.risk_dollars < best.risk_dollars:
            best = idea
    if best:
        return FilterResult(market=market, idea=best)
    return FilterResult(market=market, avoid_reasons=reasons or ["last-minute no favorite in band"])


def should_stop_ticket(
    *,
    fill_price: float,
    mark: float,
    risk_dollars: float,
    stop_frac_of_risk: float = 0.25,
    stop_frac_from_fill: float = 0.10,
    stop_dollar_cap: float = 0.40,
) -> bool:
    """True if mark is ~25% of dollars risked or ~10% from fill (dollar stop ≤ $0.40)."""
    if fill_price <= 0 or mark < 0:
        return False
    from_fill = (fill_price - mark) * max(risk_dollars / fill_price, 1.0)
    pct_from_fill = (fill_price - mark) / fill_price if fill_price else 0.0
    dollar_stop = min(abs(risk_dollars) * stop_frac_of_risk, stop_dollar_cap)
    return pct_from_fill >= stop_frac_from_fill - 1e-12 or from_fill >= dollar_stop - 1e-12


def should_take_profit(*, fill_price: float, bid: float, take_profit_cents: float = 0.02) -> bool:
    if bid >= 0.99 - 1e-12:
        return True
    return bid + 1e-12 >= fill_price + take_profit_cents
