from __future__ import annotations

from dataclasses import asdict, dataclass

from kalshibot.campaign.rules import held_bid, take_price
from kalshibot.fees import TAKER_K, fee_points


@dataclass(frozen=True)
class Playbook:
    """Small-account knobs. Change these (or the matching env vars) as the book grows.

    Typical path: keep the percents, raise `campaign_bankroll` / tracker `bankroll`.
    """

    min_net_edge: float = 0.04
    target_net_edge: float = 0.06
    model_buffer: float = 0.025
    kelly_fraction: float = 0.33
    typical_risk_min: float = 0.03
    typical_risk_max: float = 0.05
    risk_cap: float = 0.08
    risk_hard_max: float = 0.10
    small_bankroll: float = 20.0
    small_bankroll_risk: float = 0.03
    max_join: float = 0.96
    min_join: float = 0.04
    revenge_seconds: float = 15 * 60
    min_time_seconds: float = 3 * 60
    max_open_ideas: int = 2
    max_new_ideas_per_fire: int = 1
    min_stake: float = 0.25
    thin_spread: float = 0.03
    edge_decay_floor: float = 0.02
    tick: float = 0.01
    maker_join_min: float = 0.74
    maker_join_max: float = 0.93
    maker_min_seconds: float = 15.0
    maker_max_seconds: float = 180.0
    maker_min_spread: float = 0.01
    maker_max_new: int = 2
    maker_risk_cap: float = 0.03
    maker_taker_net_min: float = -0.02

    def as_status(self) -> dict[str, float | int]:
        return asdict(self)

    def required_net_edge(self, *, spread: float, equity: float) -> float:
        if equity < self.small_bankroll or spread >= self.thin_spread:
            return self.target_net_edge
        return self.min_net_edge

    def risk_limit(self, equity: float) -> float:
        if equity < self.small_bankroll:
            return min(self.small_bankroll_risk, self.risk_hard_max)
        return min(self.risk_cap, self.risk_hard_max)

    def inside_join(self, side: str, yes_bid: float, yes_ask: float) -> float:
        """Post inside the spread when there is room; otherwise join the bid."""
        spread = yes_ask - yes_bid
        if side == "yes":
            if spread >= 2 * self.tick:
                return round(yes_bid + self.tick, 2)
            return yes_bid
        if spread >= 2 * self.tick:
            return round(yes_ask - self.tick, 2)
        return yes_ask

    def spread_eats_edge(self, spread: float, gross_edge: float) -> bool:
        if gross_edge <= 0:
            return True
        return spread > 0.5 * gross_edge

    def evaluate(
        self,
        *,
        yes_bid: float,
        yes_ask: float,
        model_yes: float,
        sigma: float,
        secs_left: float,
        equity: float,
    ) -> Idea:
        if yes_bid <= 0 or yes_ask <= 0 or yes_ask < yes_bid:
            return _reject("unusable book", yes_bid, yes_ask, model_yes, sigma)
        mid = (yes_bid + yes_ask) / 2.0
        yes_edge = model_yes - mid
        no_model = 1.0 - model_yes
        no_mid = 1.0 - mid
        no_edge = no_model - no_mid
        if yes_edge >= no_edge:
            side = "yes"
            model = model_yes
            market = mid
        else:
            side = "no"
            model = no_model
            market = no_mid
        join = self.inside_join(side, yes_bid, yes_ask)
        take = take_price(side, yes_bid, yes_ask)
        hbid = held_bid(side, yes_bid, yes_ask)
        spread = yes_ask - yes_bid
        gross = model - market
        taker_pts = fee_points(market, TAKER_K)
        net = gross - taker_pts
        need = self.required_net_edge(spread=spread, equity=equity)
        sit_out = None
        if secs_left < self.min_time_seconds:
            sit_out = f"only {secs_left / 60:.1f}m left (need {self.min_time_seconds / 60:.0f}m)"
        elif join <= self.min_join or join >= self.max_join or take >= 0.99:
            sit_out = "lock / lottery book"
        elif gross <= taker_pts + self.model_buffer:
            sit_out = f"model {model:.2f} vs mkt {market:.2f} does not cover fees+buffer"
        elif self.spread_eats_edge(spread, gross):
            sit_out = f"spread {spread:.2f} eats edge {gross:.2f}"
        elif net < need:
            sit_out = f"net edge {100 * net:.1f}% < {100 * need:.0f}% after fees"
        rationale = (
            f"fair {model:.2f} vs mid {market:.2f} · net {100 * net:+.1f}% after fees "
            f"· {sigma:.2f}σ · spread {spread:.2f}"
        )
        return Idea(
            side=side,
            join_price=join,
            take_price=take,
            held_bid=hbid,
            model_prob=model,
            market_mid=market,
            spread=spread,
            gross_edge=gross,
            fee_points=taker_pts,
            net_edge=net,
            sigma=sigma,
            sit_out=sit_out,
            rationale=rationale,
        )

    def kelly_stake(self, equity: float, model_prob: float, price: float) -> float:
        """Fractional Kelly cost, clipped to small-account 3–8% (hard cap 10%)."""
        if equity <= 0 or price <= 0 or price >= 1 or model_prob <= price:
            return 0.0
        full = (model_prob - price) / (1.0 - price)
        raw = full * self.kelly_fraction
        frac = _clip(raw, 0.0, self.risk_limit(equity))
        return round(frac * equity, 2)

    def edge_decayed(self, idea: Idea) -> bool:
        if idea.sit_out:
            return True
        return idea.net_edge < self.edge_decay_floor


@dataclass(frozen=True)
class Idea:
    side: str
    join_price: float
    take_price: float
    held_bid: float
    model_prob: float
    market_mid: float
    spread: float
    gross_edge: float
    fee_points: float
    net_edge: float
    sigma: float
    sit_out: str | None
    rationale: str


DEFAULT = Playbook()

# Back-compat aliases so tests and older imports keep working.
MIN_NET_EDGE = DEFAULT.min_net_edge
TARGET_NET_EDGE = DEFAULT.target_net_edge
MODEL_BUFFER = DEFAULT.model_buffer
KELLY_FRACTION = DEFAULT.kelly_fraction
RISK_CAP = DEFAULT.risk_cap
RISK_HARD_MAX = DEFAULT.risk_hard_max
SMALL_BANKROLL = DEFAULT.small_bankroll
SMALL_BANKROLL_RISK = DEFAULT.small_bankroll_risk
MAX_JOIN = DEFAULT.max_join
MIN_JOIN = DEFAULT.min_join
REVENGE_SECONDS = DEFAULT.revenge_seconds
MIN_TIME_SECONDS = DEFAULT.min_time_seconds
MAX_OPEN_IDEAS = DEFAULT.max_open_ideas


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def required_net_edge(*, spread: float, equity: float, book: Playbook | None = None) -> float:
    return (book or DEFAULT).required_net_edge(spread=spread, equity=equity)


def spread_eats_edge(spread: float, gross_edge: float, book: Playbook | None = None) -> bool:
    return (book or DEFAULT).spread_eats_edge(spread, gross_edge)


def evaluate_idea(
    *,
    yes_bid: float,
    yes_ask: float,
    model_yes: float,
    sigma: float,
    secs_left: float,
    equity: float,
    book: Playbook | None = None,
) -> Idea:
    return (book or DEFAULT).evaluate(
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        model_yes=model_yes,
        sigma=sigma,
        secs_left=secs_left,
        equity=equity,
    )


def kelly_stake(
    equity: float,
    model_prob: float,
    price: float,
    book: Playbook | None = None,
) -> float:
    return (book or DEFAULT).kelly_stake(equity, model_prob, price)


def edge_decayed(idea: Idea, equity: float = 0.0, book: Playbook | None = None) -> bool:
    return (book or DEFAULT).edge_decayed(idea)


def _reject(reason: str, yes_bid: float, yes_ask: float, model_yes: float, sigma: float) -> Idea:
    mid = (yes_bid + yes_ask) / 2.0 if yes_bid > 0 and yes_ask >= yes_bid else 0.5
    return Idea(
        side="yes",
        join_price=yes_bid,
        take_price=yes_ask,
        held_bid=yes_bid,
        model_prob=model_yes,
        market_mid=mid,
        spread=max(0.0, yes_ask - yes_bid),
        gross_edge=0.0,
        fee_points=0.0,
        net_edge=0.0,
        sigma=sigma,
        sit_out=reason,
        rationale=reason,
    )


def playbook_from_settings(cfg: object) -> Playbook:
    """Build a Playbook from Settings-like attributes, ignoring unknown fields."""
    fields = Playbook.__dataclass_fields__
    kwargs = {}
    for name in fields:
        if hasattr(cfg, name):
            kwargs[name] = getattr(cfg, name)
    return Playbook(**kwargs)
