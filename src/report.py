"""Stdout report in the Grok/email shape."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.filters import FilterResult, Idea
from src.markets import HourlyMarket
from src.spot import SpotSnapshot

ET = ZoneInfo("America/New_York")


def format_report(
    *,
    now: datetime,
    spots: SpotSnapshot,
    markets: list[HourlyMarket],
    ideas: list[Idea],
    nearby: list[FilterResult],
    avoided: list[FilterResult],
    settlements: list[str],
) -> str:
    stamp = now.astimezone(ET).isoformat(timespec="seconds")
    btc = spots.prices.get("BTC")
    eth = spots.prices.get("ETH")
    btc_vol = spots.hourly_vol.get("BTC")
    eth_vol = spots.hourly_vol.get("ETH")

    def px(value: float | None) -> str:
        return f"${value:,.2f}" if value else "n/a"

    def vol(value: float | None) -> str:
        return f"{100 * value:.2f}%" if value else "n/a"

    lines = [
        f"# BTC/ETH Hourly — {stamp}",
        f"- Spot: BTC {px(btc)} | ETH {px(eth)}  ({spots.source}; {spots.note})",
        f"- Vol 1h: BTC {vol(btc_vol)} | ETH {vol(eth_vol)}",
        f"- Next settlements: {'; '.join(settlements) if settlements else 'none in current/next hour'}",
        "",
        "## Actionable",
    ]
    if not ideas:
        lines.append("NO_ACTIONABLE_EDGE")
    for idea in ideas:
        m = idea.market
        lines.extend(
            [
                f"**Market:** {m.title} — {m.yes_sub_title} (`{m.ticker}`)",
                f"**Side:** {idea.side}",
                f"**Entry target:** {idea.limit_price:.2f} limit"
                f"{' (maker)' if idea.post_maker else ''} / executable ask {idea.entry_price:.2f}",
                f"**Model fair:** {idea.fair:.1%}",
                f"**Edge after fees:** {idea.net_edge:.1%} (taker fee on size ${idea.fee_total:.2f})",
                f"**Size:** {idea.contracts} contracts | ${idea.risk_dollars:.2f} risked",
                f"**Max loss:** ${idea.max_loss:.2f}",
                "**Rationale:**",
                *[f"- {item}" for item in idea.rationale],
                "**Exit:** hold to settle unless next run shows edge < 3%",
                "",
            ]
        )

    lines.append("## Nearby watch")
    if not nearby:
        lines.append("- (none)")
    for row in nearby[:8]:
        m = row.market
        label = f"{m.asset} {m.yes_sub_title}" if m else "book"
        lines.append(f"- {label}: {row.watch_note}")

    lines.append("")
    lines.append("## Avoid")
    if not avoided:
        lines.append("- (none)")
    for row in avoided[:12]:
        label = f"{row.market.yes_sub_title} " if row.market else ""
        reason = "; ".join(row.avoid_reasons[:2]) or "filtered"
        lines.append(f"- {label}{reason}")

    if not ideas:
        lines.append("NO_ACTIONABLE_EDGE")
    return "\n".join(lines).rstrip() + "\n"
