"""Offline calibration of the hourly (and 15m, when logs allow) fair-value model.

Uses every scanned strike from scan logs — taken or not. Settlement truth is
the official CF Benchmarks 60-second average (BRTI / ETHUSD_RTI), never
Coinbase last tick and never paper assumed-maker-fill PnL.

Hourly ``artifacts/scan_log.jsonl`` stores ``markets[]`` (ticker, strike, book)
plus spots/vol on every scan/live tick. ``last_run.json`` only lists ticker
strings, so it cannot expand strikes on its own.

15m ``fifteen_scan_log.jsonl`` now writes the same ``markets[]`` objects (every
scanned strike) plus the Pass-idea / notes fields boards already read. Older
idea-only lines still cannot expand.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from src.cfindex import official_yes
from src.clock import parse_ts, to_et
from src.config import EXIT_OK, EXIT_CONFIG, HourlySettings
from src.evaluate import MIN_SETTLED_FOR_RATE, load_jsonl
from src.filters import maker_limit
from src.model import fair_prob, hours_left, model_z
from src.paper import hour_has_closed
from src.spot import is_settlement_index

MIN_N_FOR_RATE = MIN_SETTLED_FOR_RATE

PROB_BUCKET_WIDTH = 0.10
Z_BUCKETS = (
    (0.0, 0.5, "0.00-0.50"),
    (0.5, 1.0, "0.50-1.00"),
    (1.0, 1.5, "1.00-1.50"),
    (1.5, 2.0, "1.50-2.00"),
    (2.0, 3.0, "2.00-3.00"),
    (3.0, float("inf"), "3.00+"),
)

DEFAULT_HOURLY_SCAN = "scan_log.jsonl"
DEFAULT_FIFTEEN_SCAN = "fifteen_scan_log.jsonl"
DEFAULT_SETTLEMENT_FILES = (
    "settlements.jsonl",
    "settlement_prints.jsonl",
)
HOURLY_JOURNAL_FILES = ("trade_log.jsonl", "paper_log.jsonl")
FIFTEEN_JOURNAL_FILES = ("fifteen_trade_log.jsonl", "fifteen_paper_log.jsonl")

# Journals are read for settlement_print / settlement_result only — never pnl.


def load_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        blob = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return blob if isinstance(blob, dict) else None


def load_scan_snapshots(
    scan_log: Path,
    *,
    last_run: Path | None = None,
) -> list[dict[str, Any]]:
    """Load scan snapshots. last_run.json is appended only if it has market objects."""
    rows = list(load_jsonl(scan_log))
    if last_run is not None:
        blob = load_json_object(last_run)
        if blob and _snapshot_has_market_objects(blob):
            if not any(_same_snapshot(blob, existing) for existing in rows):
                rows.append(blob)
    return rows


def _snapshot_has_market_objects(row: dict[str, Any]) -> bool:
    markets = row.get("markets") or []
    return bool(markets) and isinstance(markets[0], dict)


def _same_snapshot(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return str(left.get("ts") or "") == str(right.get("ts") or "") and str(
        left.get("action") or left.get("mode") or ""
    ) == str(right.get("action") or right.get("mode") or "")


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"yes", "true", "1", "y"}:
        return True
    if text in {"no", "false", "0", "n"}:
        return False
    return None


def window_key(asset: str, close: datetime | str | None) -> tuple[str, str] | None:
    parsed = parse_ts(close) if not isinstance(close, datetime) else to_et(close)
    if parsed is None or not asset:
        return None
    floored = parsed.replace(second=0, microsecond=0)
    return (str(asset).upper(), floored.isoformat())


def infer_horizon(snapshot: dict[str, Any], market: dict[str, Any] | None = None) -> str:
    ticker = ""
    if market:
        ticker = str(market.get("ticker") or "")
    if not ticker:
        ideas = snapshot.get("ideas") or []
        if ideas and isinstance(ideas[0], dict):
            ticker = str(ideas[0].get("ticker") or "")
    upper = ticker.upper()
    if "15M" in upper:
        return "15m"
    mode = str(snapshot.get("mode") or snapshot.get("window_id") or "")
    if mode:
        return "15m"
    return "hourly"


def favored_side(model_prob: float | None) -> str:
    if model_prob is None:
        return "Yes"
    return "Yes" if model_prob >= 0.5 else "No"


def majority_call_hit(*, model_prob: float | None, settled_yes: bool | None) -> bool | None:
    if model_prob is None or settled_yes is None:
        return None
    predicted_yes = model_prob >= 0.5
    return predicted_yes == bool(settled_yes)


def prob_bucket(model_prob: float) -> str:
    if model_prob >= 1.0:
        return "0.90-1.00"
    low = int(model_prob / PROB_BUCKET_WIDTH) * 10
    low = min(max(low, 0), 90)
    return f"{low / 100:.2f}-{low / 100 + PROB_BUCKET_WIDTH:.2f}"


def z_bucket(abs_z: float) -> str:
    for lo, hi, name in Z_BUCKETS:
        if lo <= abs_z < hi:
            return name
    return Z_BUCKETS[-1][2]


def _ideas_by_ticker(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for idea in snapshot.get("ideas") or []:
        if not isinstance(idea, dict):
            continue
        ticker = str(idea.get("ticker") or "").upper()
        if ticker:
            out[ticker] = idea
    return out


def _spot_for(snapshot: dict[str, Any], asset: str) -> tuple[float | None, str, bool | None]:
    spots = snapshot.get("spots") or {}
    sources = snapshot.get("spot_sources") or snapshot.get("sources") or {}
    ok_map = snapshot.get("settlement_ok") or {}
    spot = _as_float(spots.get(asset))
    source = str(sources.get(asset) or snapshot.get("spot_source") or "")
    ok = ok_map.get(asset)
    if ok is None:
        ok = is_settlement_index(source) if source else None
    return spot, source, bool(ok) if ok is not None else None


def _vol_for(snapshot: dict[str, Any], asset: str) -> float | None:
    vols = snapshot.get("vol") or snapshot.get("hourly_vol") or {}
    if isinstance(vols, dict):
        return _as_float(vols.get(asset))
    return _as_float(vols)


def expand_scan_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per scanned strike in a snapshot. Ideas-only logs yield nothing."""
    if not _snapshot_has_market_objects(snapshot):
        return []
    ideas = _ideas_by_ticker(snapshot)
    ts = snapshot.get("ts")
    scan_at = parse_ts(ts)
    rows: list[dict[str, Any]] = []
    for market in snapshot.get("markets") or []:
        if not isinstance(market, dict):
            continue
        ticker = str(market.get("ticker") or "")
        if not ticker:
            continue
        asset = str(market.get("asset") or "").upper()
        if not asset:
            if "ETH" in ticker.upper():
                asset = "ETH"
            elif "BTC" in ticker.upper():
                asset = "BTC"
        strike = _as_float(market.get("threshold") if market.get("threshold") is not None else market.get("strike"))
        spot, spot_source, settlement_ok = _spot_for(snapshot, asset)
        if spot is None:
            spot = _as_float(market.get("spot"))
        vol = _vol_for(snapshot, asset)
        if vol is None:
            vol = _as_float(market.get("hourly_vol") or market.get("vol"))
        close_raw = market.get("close_time")
        close = parse_ts(close_raw)
        secs = None
        if close is not None and scan_at is not None:
            secs = (close - scan_at).total_seconds()
        hrs = hours_left(secs) if secs is not None else None
        if hrs is None and secs is not None and secs <= 0:
            hrs = 0.0
        minutes = (hrs * 60.0) if hrs is not None else None

        model_p: float | None = None
        z: float | None = None
        if spot and strike and spot > 0 and strike > 0 and vol and vol > 0 and hrs is not None:
            model_p = fair_prob(spot, strike, vol, hrs)
            z = model_z(spot, strike, vol, hrs) if hrs > 0 else 0.0

        idea = ideas.get(ticker.upper())
        taken = idea is not None
        if taken and model_p is None:
            raw = _as_float(idea.get("model_prob") if idea.get("model_prob") is not None else (idea.get("fair") or idea.get("model_pct")))
            z = _as_float(idea.get("z"))
            if raw is not None and idea.get("model_prob") is None and str(idea.get("side") or "").lower() == "no":
                # Idea.fair is P(side). Calibration always stores P(Yes).
                model_p = 1.0 - raw
            else:
                model_p = raw

        side = str((idea or {}).get("side") or favored_side(model_p) or "Yes")
        yes_ask = _as_float(market.get("yes_ask"))
        no_ask = _as_float(market.get("no_ask"))
        yes_bid = _as_float(market.get("yes_bid"))
        no_bid = _as_float(market.get("no_bid"))
        if taken:
            market_price = _as_float(
                idea.get("kalshi_price") or idea.get("entry_price") or idea.get("limit") or idea.get("limit_price")
            )
        elif side.lower() == "no":
            market_price = no_ask
        else:
            market_price = yes_ask
        bid = no_bid if side.lower() == "no" else yes_bid
        ask = no_ask if side.lower() == "no" else yes_ask
        join = None
        if bid is not None and ask is not None:
            join = maker_limit(side, bid, ask)
        if market_price is None:
            market_price = join

        horizon = infer_horizon(snapshot, market)
        rows.append(
            {
                "ts": ts,
                "horizon": horizon,
                "asset": asset,
                "ticker": ticker,
                "strike": strike,
                "spot": spot,
                "hours_left": round(hrs, 6) if hrs is not None else None,
                "minutes_left": round(minutes, 4) if minutes is not None else None,
                "vol": vol,
                "z": round(z, 6) if z is not None else None,
                "abs_z": round(abs(z), 6) if z is not None else None,
                "model_prob": round(model_p, 6) if model_p is not None else None,
                "market_price": round(market_price, 4) if market_price is not None else None,
                "market_price_yes": round(yes_ask, 4) if yes_ask is not None else None,
                "join_price": round(join, 4) if join is not None else None,
                "side": side,
                "taken_or_not": taken,
                "settled_yes": None,
                "win_vs_model": None,
                "settlement_print": None,
                "settlement_source": None,
                "close_time": close.isoformat() if close is not None else close_raw,
                "spot_source": spot_source,
                "settlement_ok": settlement_ok,
                "window_id": snapshot.get("window_id"),
            }
        )
    return rows


def expand_scans(snapshots: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if isinstance(snapshot, dict):
            rows.extend(expand_scan_snapshot(snapshot))
    return rows


def dedupe_last_per_window(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the last scan observation per (ticker, close window)."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    ordered = sorted(rows, key=lambda row: str(row.get("ts") or ""))
    for row in ordered:
        key = (str(row.get("ticker") or "").upper(), str(row.get("close_time") or ""))
        best[key] = row
    return list(best.values())


class SettlementIndex:
    """Official prints / Yes-No results keyed by ticker and by (asset, close)."""

    def __init__(self) -> None:
        self.by_ticker: dict[str, dict[str, Any]] = {}
        self.by_window: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        self,
        *,
        ticker: str = "",
        asset: str = "",
        close_time: object = None,
        settlement_print: float | None = None,
        settled_yes: bool | None = None,
        source: str = "",
    ) -> None:
        if settlement_print is not None and settlement_print <= 0:
            settlement_print = None
        if settlement_print is None and settled_yes is None:
            return
        payload = {
            "settlement_print": settlement_print,
            "settled_yes": settled_yes,
            "source": source,
        }
        if ticker:
            existing = self.by_ticker.get(ticker.upper())
            self.by_ticker[ticker.upper()] = _merge_settlement(existing, payload)
        key = window_key(asset, close_time) if asset and close_time else None
        if key is not None and settlement_print is not None:
            existing = self.by_window.get(key)
            self.by_window[key] = _merge_settlement(existing, payload)

    def lookup(self, row: dict[str, Any]) -> dict[str, Any] | None:
        ticker = str(row.get("ticker") or "").upper()
        if ticker and ticker in self.by_ticker:
            return self.by_ticker[ticker]
        key = window_key(str(row.get("asset") or ""), row.get("close_time"))
        if key is not None:
            return self.by_window.get(key)
        return None


def _merge_settlement(
    existing: dict[str, Any] | None, incoming: dict[str, Any]
) -> dict[str, Any]:
    if existing is None:
        return dict(incoming)
    merged = dict(existing)
    for key in ("settlement_print", "settled_yes", "source"):
        if incoming.get(key) is not None and merged.get(key) is None:
            merged[key] = incoming[key]
        elif incoming.get(key) is not None and key != "source":
            merged[key] = incoming[key]
            if incoming.get("source"):
                merged["source"] = incoming["source"]
    return merged


def ingest_settlement_row(index: SettlementIndex, row: dict[str, Any], *, source: str) -> None:
    """Take official print / Yes-No only. Ignores pnl / fill_model / paper result."""
    if not isinstance(row, dict):
        return
    print_value = _as_float(row.get("settlement_print") or row.get("print") or row.get("official_print"))
    settled = _as_bool(row.get("settled_yes"))
    if settled is None:
        result = str(row.get("settlement_result") or "").strip().lower()
        if result in {"yes", "no"}:
            settled = result == "yes"
    index.add(
        ticker=str(row.get("ticker") or ""),
        asset=str(row.get("asset") or ""),
        close_time=row.get("close_time"),
        settlement_print=print_value,
        settled_yes=settled,
        source=source,
    )


def load_settlements(
    paths: Iterable[Path],
    *,
    label: str | None = None,
) -> SettlementIndex:
    index = SettlementIndex()
    for path in paths:
        if not path.is_file():
            continue
        tag = label or path.name
        if path.suffix == ".json" and path.name == "last_run.json":
            continue
        text = path.read_text()
        stripped = text.lstrip()
        if stripped.startswith("["):
            try:
                blob = json.loads(text)
            except json.JSONDecodeError:
                blob = []
            if isinstance(blob, list):
                for row in blob:
                    if isinstance(row, dict):
                        ingest_settlement_row(index, row, source=tag)
            continue
        for row in load_jsonl(path):
            ingest_settlement_row(index, row, source=tag)
    return index


def apply_settlements(rows: list[dict[str, Any]], index: SettlementIndex) -> list[dict[str, Any]]:
    for row in rows:
        hit = index.lookup(row)
        if not hit:
            continue
        print_value = _as_float(hit.get("settlement_print"))
        settled = hit.get("settled_yes")
        if print_value is not None and row.get("strike") is not None:
            settled = official_yes(settlement_print=print_value, strike=float(row["strike"]))
            row["settlement_print"] = round(print_value, 4)
        elif isinstance(settled, bool):
            row["settlement_print"] = print_value
        else:
            continue
        row["settled_yes"] = bool(settled)
        row["settlement_source"] = hit.get("source")
        row["win_vs_model"] = majority_call_hit(
            model_prob=_as_float(row.get("model_prob")),
            settled_yes=bool(settled),
        )
    return rows


def fetch_missing_prints(
    rows: list[dict[str, Any]],
    get_print: Callable[[str, datetime | str], float | None],
    *,
    now: datetime | None = None,
) -> SettlementIndex:
    """Best-effort CF history join for closed windows still missing a print."""
    extra = SettlementIndex()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("settled_yes") is not None or row.get("settlement_print"):
            continue
        close = row.get("close_time")
        asset = str(row.get("asset") or "")
        if not close or not asset:
            continue
        if not hour_has_closed(close, now):
            continue
        key = window_key(asset, close)
        if key is None or key in seen:
            continue
        seen.add(key)
        try:
            print_value = get_print(asset, close)
        except Exception:  # noqa: BLE001
            continue
        if print_value is None or print_value <= 0:
            continue
        extra.add(
            ticker=str(row.get("ticker") or ""),
            asset=asset,
            close_time=close,
            settlement_print=float(print_value),
            source="cf_history",
        )
    return extra


def _empty_bucket(name: str) -> dict[str, Any]:
    return {
        "bucket": name,
        "n": 0,
        "n_yes": 0,
        "n_majority_hit": 0,
        "sum_model_prob": 0.0,
        "enough_for_rate": False,
        "thin": True,
        "yes_rate": None,
        "majority_hit_rate": None,
        "mean_model_prob": None,
    }


def finalize_bucket(slot: dict[str, Any]) -> dict[str, Any]:
    n = int(slot["n"])
    enough = n >= MIN_N_FOR_RATE
    mean = (slot["sum_model_prob"] / n) if n else None
    out = {
        "bucket": slot["bucket"],
        "n": n,
        "n_yes": int(slot["n_yes"]),
        "enough_for_rate": enough,
        "thin": not enough,
        "mean_model_prob": round(mean, 6) if mean is not None else None,
        "yes_rate": None,
        "majority_hit_rate": None,
    }
    if enough:
        out["yes_rate"] = round(slot["n_yes"] / n, 6)
        out["majority_hit_rate"] = round(slot["n_majority_hit"] / n, 6)
    return out


def _scoreable(row: dict[str, Any], *, require_index: bool) -> bool:
    if row.get("settled_yes") is None:
        return False
    if row.get("model_prob") is None:
        return False
    if require_index and row.get("settlement_ok") is False:
        return False
    return True


def summarize_buckets(
    rows: list[dict[str, Any]],
    *,
    require_index: bool = True,
) -> dict[str, Any]:
    """Calibration buckets. Rates only when n >= MIN_N_FOR_RATE (default 20)."""
    scored = [row for row in rows if _scoreable(row, require_index=require_index)]
    by_prob: dict[str, dict[str, Any]] = {}
    by_z: dict[str, dict[str, Any]] = {}
    for row in scored:
        p = float(row["model_prob"])
        name = prob_bucket(p)
        slot = by_prob.setdefault(name, _empty_bucket(name))
        slot["n"] += 1
        slot["sum_model_prob"] += p
        if row.get("settled_yes"):
            slot["n_yes"] += 1
        if row.get("win_vs_model"):
            slot["n_majority_hit"] += 1
        if row.get("abs_z") is not None:
            z_name = z_bucket(float(row["abs_z"]))
            z_slot = by_z.setdefault(z_name, _empty_bucket(z_name))
            z_slot["n"] += 1
            z_slot["sum_model_prob"] += p
            if row.get("settled_yes"):
                z_slot["n_yes"] += 1
            if row.get("win_vs_model"):
                z_slot["n_majority_hit"] += 1

    n_settled = len(scored)
    n_yes = sum(1 for row in scored if row.get("settled_yes"))
    n_majority = sum(1 for row in scored if row.get("win_vs_model"))
    enough = n_settled >= MIN_N_FOR_RATE
    mean_p = (
        sum(float(row["model_prob"]) for row in scored) / n_settled if n_settled else None
    )
    return {
        "n_rows": len(rows),
        "n_scored": n_settled,
        "n_pending_settlement": sum(1 for row in rows if row.get("settled_yes") is None),
        "n_taken": sum(1 for row in rows if row.get("taken_or_not")),
        "n_not_taken": sum(1 for row in rows if not row.get("taken_or_not")),
        "n_proxy_excluded": sum(
            1
            for row in rows
            if require_index
            and row.get("settlement_ok") is False
            and row.get("settled_yes") is not None
            and row.get("model_prob") is not None
        ),
        "enough_for_rate": enough,
        "overall_yes_rate": (n_yes / n_settled) if enough else None,
        "overall_majority_hit_rate": (n_majority / n_settled) if enough else None,
        "overall_mean_model_prob": round(mean_p, 6) if mean_p is not None else None,
        "min_n_for_rate": MIN_N_FOR_RATE,
        "by_model_prob": [finalize_bucket(by_prob[name]) for name in sorted(by_prob)],
        "by_abs_z": [finalize_bucket(by_z[name]) for name in _z_bucket_order(by_z)],
        "used_paper_pnl": False,
    }


def _z_bucket_order(by_z: dict[str, dict[str, Any]]) -> list[str]:
    names = [name for _lo, _hi, name in Z_BUCKETS]
    return [name for name in names if name in by_z] + [
        name for name in sorted(by_z) if name not in names
    ]


def format_calibration_report(
    summary: dict[str, Any],
    *,
    artifacts: Path,
    limitations: list[str] | None = None,
) -> str:
    lines = [
        "# Fair-value calibration (scan strikes vs official settlement)",
        "",
        "Not financial advice. This is not a trading hit rate and not paper PnL.",
        "Model Yes probabilities vs CF Benchmarks BRTI / ETHUSD_RTI 60-second average.",
        f"Source: `{artifacts}` — every scanned strike (taken or not), not assumed-maker-fill.",
        "",
        f"- Expanded rows: {summary['n_rows']}",
        f"- Scored (settled + model_prob): {summary['n_scored']}",
        f"- Pending official print: {summary['n_pending_settlement']}",
        f"- Taken / not taken: {summary['n_taken']} / {summary['n_not_taken']}",
    ]
    if summary.get("n_proxy_excluded"):
        lines.append(
            f"- PROXY-spot rows excluded from rates: {summary['n_proxy_excluded']}"
        )
    if summary["overall_yes_rate"] is None:
        if summary["n_scored"] == 0:
            lines.append("- Overall Yes rate: n/a (no settled forecasts)")
        else:
            lines.append(
                f"- Overall Yes rate: n/a on n={summary['n_scored']} "
                f"(below {summary['min_n_for_rate']}; do not treat as calibration)"
            )
    else:
        lines.append(
            f"- Overall Yes rate: {summary['overall_yes_rate']:.1%} "
            f"vs mean model {summary['overall_mean_model_prob']:.1%} "
            f"on n={summary['n_scored']}"
        )
        if summary.get("overall_majority_hit_rate") is not None:
            lines.append(
                f"- Majority-call hit (P(Yes)≥50%): {summary['overall_majority_hit_rate']:.1%} "
                f"(not a live edge; n≥{summary['min_n_for_rate']})"
            )

    lines.extend(["", "## By model P(Yes) (10% buckets)"])
    if not summary["by_model_prob"]:
        lines.append("- No scored rows.")
    for slot in summary["by_model_prob"]:
        lines.append(_format_bucket_line(slot))

    lines.extend(["", "## By |z|"])
    if not summary["by_abs_z"]:
        lines.append("- No scored rows with z.")
    for slot in summary["by_abs_z"]:
        lines.append(_format_bucket_line(slot))

    lines.extend(
        [
            "",
            "## What this cannot measure",
            "- Live or paper maker-fill hit rate / PnL (see `./kb eval`).",
            "- Whether a resting limit would have filled.",
            "- Edge floors, close-strike bans, Turbo, or live sizing — this file does not retune them.",
        ]
    )
    extra = list(limitations or [])
    extra.append("Settlement must be joined from journal prints, a settlements jsonl, or CF history.")
    if extra:
        lines.append("- Limitations:")
        for item in extra:
            lines.append(f"  - {item}")
    lines.append("")
    return "\n".join(lines)


def _format_bucket_line(slot: dict[str, Any]) -> str:
    if slot["enough_for_rate"]:
        return (
            f"- {slot['bucket']}: n={slot['n']} yes_rate={slot['yes_rate']:.1%} "
            f"mean_model={slot['mean_model_prob']:.1%} "
            f"majority={slot['majority_hit_rate']:.1%}"
        )
    return f"- {slot['bucket']}: n={slot['n']} thin (need {MIN_N_FOR_RATE}; rate withheld)"


def default_settlement_paths(artifacts: Path, *, fifteen: bool) -> list[Path]:
    names = list(DEFAULT_SETTLEMENT_FILES)
    names.extend(FIFTEEN_JOURNAL_FILES if fifteen else HOURLY_JOURNAL_FILES)
    # A mixed checkout may have both; extra missing files are skipped.
    names.extend(HOURLY_JOURNAL_FILES if fifteen else FIFTEEN_JOURNAL_FILES)
    seen: list[Path] = []
    for name in names:
        path = artifacts / name
        if path not in seen:
            seen.append(path)
    return seen


def collect_limitations(snapshots: list[dict[str, Any]], *, fifteen: bool) -> list[str]:
    notes: list[str] = []
    if not snapshots:
        notes.append("No scan snapshots found.")
        return notes
    expandable = sum(1 for row in snapshots if _snapshot_has_market_objects(row))
    if expandable == 0:
        if fifteen:
            notes.append(
                "15m scan log has no markets[] objects — only Pass ideas. "
                "Cannot emit every scanned strike from current fifteen_scan_log.jsonl."
            )
        else:
            notes.append(
                "Scan log has no markets[] objects. last_run.json ticker lists are not enough."
            )
    return notes


def _fetch_prints_index(
    rows: list[dict[str, Any]],
    get_print: Callable[[str, datetime | str], float | None] | None,
    now: datetime | None,
) -> SettlementIndex:
    if get_print is not None:
        return fetch_missing_prints(rows, get_print, now=now)
    from src.kalshi_client import KalshiClient
    from src.paper import fetch_official_print

    settings = HourlySettings()
    client = KalshiClient(
        settings.kalshi_base_url,
        timeout=settings.request_timeout_seconds,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        trading_base_url=settings.trading_base_url,
    )
    try:
        return fetch_missing_prints(
            rows,
            lambda asset, close: fetch_official_print(client, asset, close),
            now=now,
        )
    finally:
        closer = getattr(client, "close", None)
        if closer:
            closer()


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, default=str) + "\n" for row in rows))


def run_calibration(
    *,
    artifacts: Path,
    scan_log: Path | None = None,
    last_run: Path | None = None,
    settlement_paths: Iterable[Path] | None = None,
    fifteen: bool = False,
    all_scans: bool = False,
    require_index: bool = True,
    fetch_prints: bool = False,
    get_print: Callable[[str, datetime | str], float | None] | None = None,
    out_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    artifacts = Path(artifacts)
    scan_path = Path(scan_log) if scan_log else artifacts / (
        DEFAULT_FIFTEEN_SCAN if fifteen else DEFAULT_HOURLY_SCAN
    )
    last_path = Path(last_run) if last_run else artifacts / "last_run.json"
    snapshots = load_scan_snapshots(scan_path, last_run=last_path)
    limitations = collect_limitations(snapshots, fifteen=fifteen)
    rows = expand_scans(snapshots)
    if not all_scans:
        rows = dedupe_last_per_window(rows)

    paths = list(settlement_paths) if settlement_paths is not None else default_settlement_paths(
        artifacts, fifteen=fifteen
    )
    index = load_settlements(paths)
    apply_settlements(rows, index)

    if fetch_prints:
        extra = _fetch_prints_index(rows, get_print, now=now)
        apply_settlements(rows, extra)

    summary = summarize_buckets(rows, require_index=require_index)
    dest = Path(out_path) if out_path else artifacts / (
        "fifteen_calibration_rows.jsonl" if fifteen else "calibration_rows.jsonl"
    )
    write_jsonl(dest, rows)
    summary["out_path"] = str(dest)
    report = format_calibration_report(summary, artifacts=artifacts, limitations=limitations)
    return rows, summary, report


def run_calibrate_cli(
    settings: HourlySettings | None = None,
    *,
    fifteen: bool = False,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Calibrate model Yes probabilities from scan logs vs official "
            "BRTI / ETHUSD_RTI settlement. No orders."
        )
    )
    parser.add_argument(
        "--artifacts",
        default=None,
        help="Artifacts directory (default: settings / artifacts/)",
    )
    parser.add_argument("--scan-log", default=None, help="Override scan_log.jsonl path")
    parser.add_argument(
        "--settlements",
        action="append",
        default=None,
        help="Extra settlements jsonl (repeatable). Journals under artifacts/ are loaded by default.",
    )
    parser.add_argument("--out", default=None, help="Write expanded rows jsonl here")
    parser.add_argument(
        "--fifteen",
        action="store_true",
        default=fifteen,
        help="Read fifteen_scan_log.jsonl (needs markets[] on each row)",
    )
    parser.add_argument(
        "--all-scans",
        action="store_true",
        help="Keep every scan snapshot of a strike (default: last per ticker/close)",
    )
    parser.add_argument(
        "--include-proxy",
        action="store_true",
        help="Include PROXY-spot rows in rate buckets (default: exclude)",
    )
    parser.add_argument(
        "--fetch-prints",
        action="store_true",
        help="Fill missing closed-window prints from Kalshi CF history (needs key)",
    )
    args = parser.parse_args(argv)
    artifacts = Path(args.artifacts) if args.artifacts else Path(
        getattr(settings, "artifacts_dir", None) or "artifacts"
    )
    extra = [Path(p) for p in (args.settlements or [])]
    settlement_paths = default_settlement_paths(artifacts, fifteen=bool(args.fifteen))
    settlement_paths = extra + [p for p in settlement_paths if p not in extra]
    try:
        _rows, _summary, report = run_calibration(
            artifacts=artifacts,
            scan_log=Path(args.scan_log) if args.scan_log else None,
            fifteen=bool(args.fifteen),
            all_scans=bool(args.all_scans),
            require_index=not bool(args.include_proxy),
            fetch_prints=bool(args.fetch_prints),
            settlement_paths=settlement_paths,
            out_path=Path(args.out) if args.out else None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"calibrate failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(report)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    return run_calibrate_cli(argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
