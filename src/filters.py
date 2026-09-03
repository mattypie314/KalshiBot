"""Hard and soft filters for hourly BTC/ETH ideas."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from src.clock import to_et
from src.fees import ev_per_contract, taker_fee_dollars
from src.journal import strike_distance_pct, trade_bucket
from src.markets import HourlyMarket
from src.model import fair_no, fair_prob, hours_left, model_z
from src.sizer import size_idea

# Remaining 2026 CPI prints (8:30 AM ET) and FOMC statements (2:00 PM ET).
CPI_DATES = frozenset(
    {
        (2026, 1, 14),
        (2026, 2, 12),
        (2026, 3, 12),
        (2026, 4, 10),
        (2026, 5, 13),
        (2026, 6, 11),
        (2026, 7, 15),
        (2026, 8, 12),
        (2026, 9, 11),
        (2026, 10, 14),
        (2026, 11, 10),
        (2026, 12, 10),
    }
)
FOMC_DATES = frozenset(
    {
        (2026, 1, 28),
        (2026, 3, 18),
        (2026, 4, 29),
        (2026, 6, 17),
        (2026, 7, 29),
        (2026, 9, 16),
        (2026, 10, 28),
        (2026, 12, 9),
    }
)


@dataclass(frozen=True)
class FilterConfig:
    min_net_edge: float = 0.06
    soft_net_edge: float = 0.06
    max_spread: float = 0.06
    min_minutes_left: float = 3
    min_visible_depth: int = 5
    min_price: float = 0.05
    max_price: float = 0.95
    fat_tail_z: float = 2.5
    fat_tail_edge: float = 0.08
    news_blackout: bool = False
    bankroll: float = 40.00
    kelly_mult: float = 0.25
    max_risk_pct: float = 0.05
    max_risk_dollars: float = 2.00
    preferred_risk_dollars: float = 1.75
    last_loss_same_hour: bool = False
    last_contracts: int | None = None
    min_strike_distance_pct: float = 0.005
    min_strike_sigma: float = 1.5
    close_strike_edge: float = 0.10
    vol_pause_mult: float = 2.0
    vol_widen_mult: float = 1.25
    vol_widen_distance_pct: float = 0.0075
    kill_close_no: bool = False
    kill_close_yes: bool = False
    require_settlement_index: bool = True
    settlement_index: bool = True
    require_maker: bool = True
    news_pause: bool = False


@dataclass
class Idea:
    market: HourlyMarket
    side: str
    entry_price: float
    limit_price: float
    fair: float
    gross_edge: float
    net_edge: float
    fee_per_contract: float
    fee_total: float
    z: float
    hours_left: float
    contracts: int
    risk_dollars: float
    max_loss: float
    rationale: list[str]
    post_maker: bool
    strike_distance_pct: float = 0.0
    spot: float = 0.0
    minutes_left: float = 0.0
    bucket: str = ""


@dataclass
class FilterResult:
    market: HourlyMarket | None = None
    idea: Idea | None = None
    nearby: bool = False
    avoid_reasons: list[str] = field(default_factory=list)
    watch_note: str = ""


def news_blackout_active(now: datetime | None = None) -> bool:
    local = to_et(now)
    day = (local.year, local.month, local.day)
    if day in CPI_DATES:
        start = local.replace(hour=8, minute=15, second=0, microsecond=0)
        end = start + timedelta(minutes=30)
        if start <= local <= end:
            return True
    if day in FOMC_DATES:
        start = local.replace(hour=13, minute=45, second=0, microsecond=0)
        end = start + timedelta(minutes=60)
        if start <= local <= end:
            return True
    return False


def maker_limit(side: str, bid: float, ask: float) -> float | None:
    """A post-only price: one tick inside, or join the bid if the book is 1¢.

    Never returns a price at or through the ask (that would take / post-only-cross).
    A locked or crossed book rests one tick behind the bid, or None.
    """
    tick = 0.01
    if ask <= 0 or bid <= 0:
        return None

    def _join_behind(level: float) -> float | None:
        behind = round(level - tick, 2)
        return behind if behind >= tick - 1e-9 else None

    if ask < bid - 1e-9:
        return _join_behind(min(bid, ask))
    inside = round(bid + tick, 2)
    if inside < ask - 1e-9:
        return inside
    if bid + 1e-9 < ask:
        return round(bid, 2)
    return _join_behind(bid)


def evaluate_market(
    market: HourlyMarket,
    *,
    spot: float,
    hourly_vol: float,
    now: datetime,
    cfg: FilterConfig,
    vol_fallback: float | None = None,
    settlement_index: bool | None = None,
) -> FilterResult:
    reasons: list[str] = []
    if settlement_index is not None:
        cfg = replace(cfg, settlement_index=settlement_index)
    if cfg.news_pause:
        return FilterResult(
            market=market,
            avoid_reasons=["NEWS_PAUSE — operator sit (headline / war tape). Not a scraped news feed"],
        )
    if market.status not in {"open", "active"}:
        return FilterResult(market=market, avoid_reasons=["market not open"])
    if market.asset not in {"BTC", "ETH"}:
        return FilterResult(market=market, avoid_reasons=[f"asset {market.asset} not in BTC/ETH"])
    if cfg.require_settlement_index and not cfg.settlement_index:
        return FilterResult(
            market=market,
            avoid_reasons=[
                "spot is not CF Benchmarks BRTI/ERTI — Coinbase/Binance last tick is a proxy, not settlement; sit"
            ],
        )

    secs = (market.close_time - now).total_seconds()
    hrs = hours_left(secs)
    if hrs is None:
        return FilterResult(market=market, avoid_reasons=["close_time is not in the future"])
    minutes = secs / 60.0
    if minutes < cfg.min_minutes_left:
        return FilterResult(market=market, avoid_reasons=[f"only {minutes:.1f} minutes left (need {cfg.min_minutes_left:g})"])

    if cfg.news_blackout or news_blackout_active(now):
        return FilterResult(market=market, avoid_reasons=["scheduled CPI/FOMC news window — sit out"])

    fallback = vol_fallback if vol_fallback and vol_fallback > 0 else hourly_vol
    if fallback > 0 and hourly_vol >= cfg.vol_pause_mult * fallback:
        return FilterResult(
            market=market,
            avoid_reasons=[
                f"elevated vol {hourly_vol:.2%} ≥ {cfg.vol_pause_mult:g}× typical {fallback:.2%} — news tape, sit"
            ],
        )

    distance = strike_distance_pct(spot, market.threshold)
    sigma = hourly_vol * (hrs**0.5) if hourly_vol > 0 and hrs > 0 else 0.0

    z = model_z(spot, market.threshold, hourly_vol, hrs)
    fair_yes = fair_prob(spot, market.threshold, hourly_vol, hrs)
    fair_n = fair_no(spot, market.threshold, hourly_vol, hrs)

    yes_ask, no_ask = market.yes_ask, market.no_ask
    yes_bid, no_bid = market.yes_bid, market.no_bid
    spread = max(0.0, yes_ask - yes_bid) if yes_ask and yes_bid else 1.0

    candidates: list[tuple[str, float, float, float, float]] = []
    if cfg.min_price <= yes_ask <= cfg.max_price:
        candidates.append(("Yes", yes_ask, yes_bid, fair_yes, market.yes_ask_size))
    elif yes_ask > cfg.max_price or (yes_ask and yes_ask < cfg.min_price):
        reasons.append(f"Yes ask {yes_ask:.2f} outside {cfg.min_price:.2f}-{cfg.max_price:.2f} (reject 99¢ favorites)")
    if cfg.min_price <= no_ask <= cfg.max_price:
        candidates.append(("No", no_ask, no_bid, fair_n, market.no_ask_size or market.yes_bid_size))
    elif no_ask > cfg.max_price or (no_ask and no_ask < cfg.min_price):
        reasons.append(f"No ask {no_ask:.2f} outside {cfg.min_price:.2f}-{cfg.max_price:.2f}")

    if not candidates:
        return FilterResult(market=market, avoid_reasons=reasons or ["no executable price in 0.05-0.95"])

    best: Idea | None = None
    nearby_note = ""
    for side, ask, bid, p_hat, depth in candidates:
        post_maker = bid > 0 and ask > bid
        limit = maker_limit(side, bid, ask) if post_maker else None
        # Filter edge is always vs executable ask (never mid). Fee on recommended size.
        trial = size_idea(
            bankroll=cfg.bankroll,
            entry_price=ask,
            p_hat=p_hat,
            kelly_mult=cfg.kelly_mult,
            max_risk_pct=cfg.max_risk_pct,
            max_risk_dollars=cfg.max_risk_dollars,
            preferred_risk_dollars=cfg.preferred_risk_dollars,
            last_loss_same_hour=cfg.last_loss_same_hour,
            last_contracts=cfg.last_contracts,
        )
        # Net edge for the filter uses taker fee on that size (conservative).
        contracts = max(trial.contracts, 1)
        fee_total = taker_fee_dollars(contracts, ask)
        fee_each = fee_total / contracts
        gross, net = ev_per_contract(p_hat, ask, fee_each)

        if spread > cfg.max_spread and net < 0.10:
            reasons.append(f"{side}: spread {spread:.3f} > {cfg.max_spread} and edge {net:.3f} < 10%")
            continue

        needs_jump = (side == "Yes" and spot < market.threshold) or (
            side == "No" and spot > market.threshold
        )
        if needs_jump and abs(z) > 3.5:
            reasons.append(f"{side}: |z|={z:.2f} > 3.5 news-only jump, skip")
            continue
        if abs(z) > cfg.fat_tail_z and net < cfg.fat_tail_edge:
            reasons.append(
                f"{side}: |z|={z:.2f} > {cfg.fat_tail_z} and net edge {net:.3f} < {cfg.fat_tail_edge:.0%} fat-tail bar"
            )
            continue

        min_edge = max(cfg.min_net_edge, cfg.soft_net_edge)
        if net < min_edge:
            if net >= 0.02:
                nearby_note = f"{side} net {net:.1%} vs {ask:.2f} (need {min_edge:.0%})"
            reasons.append(f"{side}: net edge {net:.3f} < {min_edge:.2f} (fair {p_hat:.3f} ask {ask:.3f} fee {fee_each:.4f})")
            continue

        lifting = limit is None or abs(limit - ask) < 1e-9
        if lifting and cfg.require_maker:
            reasons.append(f"{side}: cannot rest a maker limit — will not cross for this edge")
            continue
        if lifting and depth < cfg.min_visible_depth:
            reasons.append(f"{side}: visible depth {depth:.0f} < {cfg.min_visible_depth} and cannot rest inside spread")
            continue

        if trial.skip:
            reasons.append(f"{side}: sizer skip ({trial.reason})")
            continue

        fading = (side == "No" and spot > market.threshold) or (
            side == "Yes" and spot < market.threshold
        )
        need = max(cfg.min_strike_distance_pct, cfg.min_strike_sigma * hourly_vol)
        if fading:
            need = max(need, cfg.min_strike_sigma * sigma)
        if fallback > 0 and hourly_vol >= cfg.vol_widen_mult * fallback:
            need = max(need, cfg.vol_widen_distance_pct)
        if distance < need:
            reasons.append(
                f"{side}: close strike {distance:.2%} from spot (need {need:.2%}; coin-flip / fade)"
            )
            continue

        bucket = trade_bucket(side, distance)
        if cfg.kill_close_no and bucket == "close_no":
            reasons.append(f"{side}: close-strike No bucket is underwater — rule off")
            continue
        if cfg.kill_close_yes and bucket == "close_yes":
            reasons.append(f"{side}: close-strike Yes bucket is underwater — rule off")
            continue

        # Prefer maker: reprice fee at 0 if we rest inside / at bid.
        using_maker = bool(limit) and limit + 1e-9 < ask
        taker_total = taker_fee_dollars(trial.contracts, ask)
        taker_each = taker_total / max(trial.contracts, 1)
        gross, net = ev_per_contract(p_hat, ask, taker_each)
        # Keep filter net at executable ask + taker fee; maker is a better fill, not the filter.
        rationale = [
            f"Executable {side} ask {ask:.2f}; model fair {p_hat:.1%}.",
            f"Net edge after taker fee on {trial.contracts} ct: {net:.1%}.",
            f"Strike {market.threshold:,.2f} is {distance:.2%} from spot {spot:,.2f} ({bucket}).",
            f"z={z:.2f}, minutes_left={minutes:.1f}, hourly_vol={hourly_vol:.3%}.",
            f"Settlement source: {market.settlement_source}.",
        ]
        if using_maker:
            rationale.append(f"Post limit at {limit:.2f} (maker, fee 0) inside {bid:.2f}/{ask:.2f}.")
        if market.used_15m_fallback:
            rationale.append("Hourly books missing; this is a 15-minute fallback.")

        idea = Idea(
            market=market,
            side=side,
            entry_price=ask,
            limit_price=limit or ask,
            fair=p_hat,
            gross_edge=gross,
            net_edge=net,
            fee_per_contract=taker_each,
            fee_total=taker_total,
            z=z,
            hours_left=hrs,
            contracts=trial.contracts,
            risk_dollars=trial.risk_dollars,
            max_loss=trial.risk_dollars + taker_total,
            rationale=rationale,
            post_maker=using_maker,
            strike_distance_pct=distance,
            spot=spot,
            minutes_left=minutes,
            bucket=bucket,
        )
        if best is None or idea.net_edge > best.net_edge:
            best = idea

    if best:
        return FilterResult(market=market, idea=best)
    return FilterResult(
        market=market,
        nearby=bool(nearby_note),
        watch_note=nearby_note,
        avoid_reasons=reasons,
    )
