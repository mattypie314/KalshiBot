"""Offline evaluation of the hourly scanner. Never places orders.

Quantifies only what is in the local journal / scan log / checked-in fixtures.
Does not assume profitability. Empty data is a valid result.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import EXIT_OK, HourlySettings
from src.filters import FilterConfig, evaluate_market
from src.journal import counts_as_filled, load_trades
from src.markets import HourlyMarket
from src.paper import format_paper_section, load_paper, summarize_paper, try_settle_paper

# Checked-in GitHub scan actionables from before the close-strike cut.
# No fills or official settlements exist in this repo for these rows.
HISTORICAL_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "historical_actionables.json"

MIN_SETTLED_FOR_RATE = 20


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def summarize_trades(rows: list[dict[str, Any]]) -> dict[str, Any]:
    filled_settled = [
        row for row in rows if row.get("result") in {"win", "loss"} and counts_as_filled(row)
    ]
    wins = [row for row in filled_settled if row.get("result") == "win"]
    losses = [row for row in filled_settled if row.get("result") == "loss"]
    unfilled = [row for row in rows if row.get("result") == "unfilled"]
    pending = [row for row in rows if row.get("result") not in {"win", "loss", "unfilled"}]
    pnl = sum(float(row.get("pnl") or 0) for row in filled_settled)
    by_bucket: dict[str, dict[str, Any]] = {}
    for row in filled_settled:
        bucket = str(row.get("bucket") or "unknown")
        slot = by_bucket.setdefault(bucket, {"n": 0, "wins": 0, "pnl": 0.0})
        slot["n"] += 1
        slot["wins"] += 1 if row.get("result") == "win" else 0
        slot["pnl"] += float(row.get("pnl") or 0)
    by_asset: dict[str, int] = dict(Counter(str(row.get("asset") or "?") for row in filled_settled))
    enough = len(filled_settled) >= MIN_SETTLED_FOR_RATE
    return {
        "n_rows": len(rows),
        "n_filled_settled": len(filled_settled),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_unfilled": len(unfilled),
        "n_pending": len(pending),
        "pnl": round(pnl, 4),
        "hit_rate": (len(wins) / len(filled_settled)) if filled_settled else None,
        "enough_for_rate": enough,
        "by_bucket": by_bucket,
        "by_asset": by_asset,
    }


def summarize_scans(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ideas = 0
    sits = 0
    for row in rows:
        n = len(row.get("ideas") or [])
        ideas += n
        if n == 0:
            sits += 1
    return {
        "n_scans": len(rows),
        "n_scans_with_idea": len(rows) - sits,
        "n_sits": sits,
        "n_recorded_ideas": ideas,
    }


def _market_from_historical(row: dict[str, Any], now: datetime) -> HourlyMarket:
    hours = float(row.get("hours_left") or 0.5)
    close = now + timedelta(seconds=max(hours, 0.05) * 3600)
    no_ask = float(row.get("no_ask") or 0.5)
    yes_ask = float(row.get("yes_ask") or max(0.01, 1.0 - no_ask + 0.01))
    no_bid = float(row.get("no_bid") or max(0.01, no_ask - 0.01))
    yes_bid = float(row.get("yes_bid") or max(0.01, yes_ask - 0.01))
    return HourlyMarket(
        ticker=str(row.get("ticker") or "KXBTCD-HIST"),
        event_ticker="HIST",
        series_ticker="KXETHD" if str(row.get("asset") or "").upper() == "ETH" else "KXBTCD",
        asset=str(row.get("asset") or "BTC").upper(),
        title="historical fixture",
        yes_sub_title=str(row.get("yes_sub_title") or f"${row.get('threshold')} or above"),
        threshold=float(row["threshold"]),
        strike_type="greater",
        close_time=close,
        status="active",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_bid_size=20,
        yes_ask_size=20,
        no_bid_size=20,
        no_ask_size=20,
        rules_primary="fixture",
        rules_secondary="",
        settlement_source="fixture (not a live settlement)",
        exchange_index=2,
    )


def replay_historical_actionables(
    path: Path | None = None,
    cfg: FilterConfig | None = None,
) -> dict[str, Any]:
    """Replay GitHub-issue actionables through the current filter.

    These were printed as actionable by an older scanner. They are not fills
    and not official settlements. The replay only answers: would today's
    rules still take the same ticket?
    """
    fixture = path or HISTORICAL_FIXTURE
    if not fixture.is_file():
        return {"n": 0, "still_actionable": 0, "rejected": [], "source": "", "limitations": []}
    blob = json.loads(fixture.read_text())
    cfg = cfg or FilterConfig()
    now = datetime.now(timezone.utc)
    rejected: list[dict[str, Any]] = []
    kept = 0
    for row in blob.get("scans") or []:
        market = _market_from_historical(row, now)
        result = evaluate_market(
            market,
            spot=float(row["spot"]),
            hourly_vol=float(row["hourly_vol"]),
            now=now,
            cfg=cfg,
            vol_fallback=float(row.get("vol_fallback") or row["hourly_vol"]),
        )
        distance = abs(float(row["threshold"]) - float(row["spot"])) / float(row["spot"])
        info = {
            "issue": row.get("issue"),
            "ticker": row.get("ticker"),
            "side": row.get("side"),
            "distance_pct": round(distance, 6),
            "reported_net_edge": row.get("reported_net_edge"),
            "now_actionable": bool(result.idea),
            "now_side": result.idea.side if result.idea else None,
            "avoid": result.avoid_reasons,
        }
        if result.idea:
            kept += 1
        rejected.append(info)
    return {
        "n": len(rejected),
        "still_actionable": kept,
        "rejected": rejected,
        "source": str(blob.get("source") or ""),
        "limitations": list(blob.get("limitations") or []),
    }


def format_eval_report(
    *,
    trades: dict[str, Any],
    scans: dict[str, Any],
    historical: dict[str, Any],
    paper: dict[str, Any] | None = None,
) -> str:
    paper = paper if paper is not None else summarize_paper([])
    lines = [
        "# Hourly BTC/ETH evaluation",
        "",
        "Not financial advice. This is a bookkeeping report, not a claim of edge.",
        "",
        *format_paper_section(paper),
        "",
        "## Local live journal (`artifacts/trade_log.jsonl`)",
        f"- Rows: {trades['n_rows']}",
        f"- Filled and settled: {trades['n_filled_settled']} "
        f"({trades['n_wins']} win / {trades['n_losses']} loss)",
        f"- Unfilled (not scored): {trades['n_unfilled']}",
        f"- Pending / unknown fill: {trades['n_pending']}",
        f"- Filled PnL: ${trades['pnl']:.2f}",
    ]
    if trades["hit_rate"] is None:
        lines.append("- Hit rate: n/a (no filled settlements)")
    elif not trades["enough_for_rate"]:
        lines.append(
            f"- Hit rate: {trades['hit_rate']:.1%} on n={trades['n_filled_settled']} "
            f"(below {MIN_SETTLED_FOR_RATE}; do not treat as a live edge)"
        )
    else:
        lines.append(f"- Hit rate: {trades['hit_rate']:.1%} on n={trades['n_filled_settled']}")
    if trades["by_bucket"]:
        lines.append("- By bucket:")
        for name, slot in sorted(trades["by_bucket"].items()):
            lines.append(f"  - {name}: n={slot['n']} wins={slot['wins']} pnl=${slot['pnl']:.2f}")
    if trades["by_asset"]:
        lines.append(f"- By asset: {trades['by_asset']}")
    if trades["n_filled_settled"] == 0:
        lines.append("- Insufficient live data to measure profitability or calibration.")

    lines.extend(
        [
            "",
            "## Local scan log (`artifacts/scan_log.jsonl`)",
            f"- Scans: {scans['n_scans']} ({scans['n_sits']} sit / {scans['n_scans_with_idea']} with an idea)",
            f"- Recorded ideas: {scans['n_recorded_ideas']}",
        ]
    )
    if scans["n_scans"] == 0:
        lines.append("- No scan snapshots yet. Each `scan` / `once` / `live` run appends one line.")

    lines.extend(
        [
            "",
            "## Historical GitHub actionables vs current filters",
            f"- Source: {historical.get('source') or 'none'}",
            f"- Old actionables replayed: {historical['n']}",
            f"- Still actionable under current rules: {historical['still_actionable']}",
        ]
    )
    for row in historical.get("rejected") or []:
        status = "KEEP" if row.get("now_actionable") else "SIT"
        avoid = "; ".join(row.get("avoid") or []) or "n/a"
        lines.append(
            f"- #{row.get('issue')} {row.get('ticker')} {row.get('side')} "
            f"dist {100 * float(row.get('distance_pct') or 0):.2f}% → {status} ({avoid})"
        )
    if historical.get("limitations"):
        lines.append("- Limitations:")
        for item in historical["limitations"]:
            lines.append(f"  - {item}")

    lines.extend(
        [
            "",
            "## What this cannot measure",
            "- Maker fill rate (limits may rest and expire unfilled).",
            "- Slippage vs the executable ask the filter used.",
            "- BRTI/ERTI vs Coinbase basis on the historical scans.",
            "- BTC/ETH correlation of simultaneous tickets (exposure cap is 1, or 2 opposite-side).",
            "- A price-only backtest would invent Kalshi books; that is not run here.",
            "",
        ]
    )
    return "\n".join(lines)


def run_eval(settings: HourlySettings) -> int:
    artifacts = Path(settings.artifacts_dir)
    try_settle_paper(settings)
    trades = summarize_trades(load_trades(artifacts / "trade_log.jsonl"))
    scans = summarize_scans(load_jsonl(Path(settings.scan_log_path)))
    historical = replay_historical_actionables()
    paper = summarize_paper(load_paper(Path(settings.paper_log_path)))
    print(format_eval_report(trades=trades, scans=scans, historical=historical, paper=paper))
    return EXIT_OK
