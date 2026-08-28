from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LEGACY_FLATTEN_CUTOFF = datetime(2026, 8, 27, 3, 0, tzinfo=ET)

MAKER_MINUTES = {12, 13, 14, 27, 28, 29, 42, 43, 44, 57, 58, 59}
# Extra minutes so a late GitHub job still catches the 15-minute window.
MAKER_SCAN_MINUTES = MAKER_MINUTES | {11, 15, 26, 30, 41, 45, 56, 0}


@dataclass(frozen=True)
class Favorite:
    side: str  # yes | no
    conviction: str  # thin | real | fat
    take_price: float
    join_price: float
    held_bid: float
    model_side: float
    rationale: str

    @property
    def is_real_or_better(self) -> bool:
        return self.conviction in {"real", "fat"}


def dollars(value: float) -> str:
    return f"{value:.2f}"


def cents4(value: float) -> str:
    return f"{value:.4f}"


def contracts_for_budget(budget: float, price: float) -> float:
    if price <= 0 or budget <= 0:
        return 0.0
    return max(0.01, round(budget / price, 2))


def size_for_conviction(loop: str, conviction: str) -> float:
    if loop == "hourly":
        return {"thin": 1.0, "real": 3.5, "fat": 5.0}.get(conviction, 0.0)
    return {"thin": 0.50, "real": 1.75, "fat": 2.50}.get(conviction, 0.0)


def maker_size(conviction: str) -> float:
    return 1.0 if conviction == "fat" else 0.50


def room(bankroll: float, realized: float, open_cost: float) -> float:
    return bankroll + realized - open_cost


def open_cost(tickets: list[dict]) -> float:
    return sum(float(t.get("cost") or 0) for t in tickets if t.get("status") == "open")


def flatten_pct(filled_at: str | None, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if not filled_at:
        return 0.10
    raw = filled_at[:-1] + "+00:00" if filled_at.endswith("Z") else filled_at
    try:
        when = datetime.fromisoformat(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.10
    if when.astimezone(ET) < LEGACY_FLATTEN_CUTOFF:
        return 0.18
    return 0.10


def held_bid(side: str, yes_bid: float, yes_ask: float) -> float:
    if side == "yes":
        return yes_bid
    return max(0.0, 1.0 - yes_ask)


def take_price(side: str, yes_bid: float, yes_ask: float) -> float:
    if side == "yes":
        return yes_ask
    return max(0.0, 1.0 - yes_bid)


def join_price(side: str, yes_bid: float, yes_ask: float) -> float:
    if side == "yes":
        return yes_bid
    return yes_ask


def ticket_unrealized(ticket: dict, yes_bid: float, yes_ask: float) -> float:
    fill = float(ticket["fill"])
    count = float(ticket["count"])
    bid = held_bid(ticket["side"], yes_bid, yes_ask)
    return (bid - fill) * count


def flatten_reason(ticket: dict, yes_bid: float, yes_ask: float, now: datetime | None = None) -> str | None:
    fill = float(ticket["fill"])
    count = float(ticket["count"])
    bid = held_bid(ticket["side"], yes_bid, yes_ask)
    pnl = (bid - fill) * count
    if bid >= 0.99:
        return "bid_99"
    if bid >= fill + 0.02:
        return "take_profit_2c"
    if pnl <= -0.50:
        return "down_50c"
    pct = flatten_pct(ticket.get("filled_at"), now)
    if fill > 0 and (fill - bid) / fill >= pct:
        return "down_pct"
    return None


def classify_favorite(
    *,
    spot: float,
    strike: float,
    yes_bid: float,
    yes_ask: float,
    model_yes: float,
) -> Favorite | None:
    if yes_bid <= 0 or yes_ask <= 0 or yes_ask < yes_bid:
        return None
    side = "yes" if spot >= strike else "no"
    model_side = model_yes if side == "yes" else (1.0 - model_yes)
    mid = (yes_bid + yes_ask) / 2.0
    book_yes = mid >= 0.50
    book_agrees = book_yes if side == "yes" else (not book_yes)
    if not book_agrees:
        return None
    take = take_price(side, yes_bid, yes_ask)
    join = join_price(side, yes_bid, yes_ask)
    hbid = held_bid(side, yes_bid, yes_ask)
    if hbid >= 0.90:
        conviction = "fat"
    elif model_side >= 0.74 and take >= 0.74 and take <= model_side + 0.03:
        conviction = "real"
    else:
        conviction = "thin"
    return Favorite(
        side=side,
        conviction=conviction,
        take_price=take,
        join_price=join,
        held_bid=hbid,
        model_side=model_side,
        rationale=f"spot {spot:.4f} vs target {strike:.4f}; book {side} {hbid:.2f}/{take:.2f}",
    )


def maker_contract_price(favorite: Favorite) -> float:
    """Dollar cost of the resting maker bid (yes price or 1 − yes price for no)."""
    if favorite.side == "yes":
        return favorite.join_price
    return max(0.0, 1.0 - favorite.join_price)


def taker_net_edge(favorite: Favorite) -> float:
    """Model minus the ask, minus taker fees. ~0 means the favorite is taker break-even."""
    from kalshibot.fees import TAKER_K, fee_points

    return favorite.model_side - favorite.take_price - fee_points(favorite.take_price, TAKER_K)


def already_there(favorite: Favorite) -> bool:
    """Skip IOC locks at 99¢–$1.00."""
    return favorite.take_price >= 0.99 or favorite.held_bid >= 0.99


def in_pay_band(favorite: Favorite) -> bool:
    """Taker loop only pays 74–96¢."""
    return 0.74 <= favorite.take_price <= 0.96


def maker_join_ok(favorite: Favorite, join_min: float = 0.74, join_max: float = 0.93) -> bool:
    """Rest maker bids on favorites priced 74–93¢ (contract cost, either side)."""
    return join_min <= maker_contract_price(favorite) <= join_max


def maker_spread_ok(
    favorite: Favorite,
    yes_bid: float,
    yes_ask: float,
    *,
    join_min: float = 0.74,
    join_max: float = 0.93,
    min_spread: float = 0.01,
    taker_net_min: float = -0.02,
) -> bool:
    """Last-3-min maker: confirmed favorite, 74–93¢, edge is the spread not a taker misprice."""
    if not maker_join_ok(favorite, join_min, join_max):
        return False
    if yes_ask - yes_bid < min_spread:
        return False
    if taker_net_edge(favorite) < taker_net_min:
        return False
    cost = maker_contract_price(favorite)
    if favorite.model_side + 0.01 < cost:
        return False
    return True


def in_maker_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    local = now.astimezone(ET)
    return local.minute in MAKER_SCAN_MINUTES


def hourly_scan_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    return now.astimezone(ET).minute in {57, 58, 59}
