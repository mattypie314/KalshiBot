"""Hourly live pot writer for artifacts/hourly_pot.json.

Mirrors 15m pot credit (src/fifteen/pot.py) on the Pi schema: balance_usd,
start_usd, ledger[], notes[], status, and any risk_* extras. Does not change
live gates, edge, sizing, or Turbo.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.journal import counts_as_filled


DEFAULT_POT_START = 40.0
HOURLY_POT_FILE = "hourly_pot.json"
DEFAULT_POT_REL = f"artifacts/{HOURLY_POT_FILE}"

CORE_KEYS = (
    "balance_usd",
    "start_usd",
    "realized_pnl_usd",
    "status",
    "updated_at",
    "ledger",
    "notes",
)


def default_pot_path(artifacts_dir: str | Path = "artifacts") -> Path:
    return Path(artifacts_dir) / HOURLY_POT_FILE


def pot_path_from_settings(settings: Any) -> Path:
    """Resolve hourly_pot.json. Stock relative path follows artifacts_dir."""
    raw = str(getattr(settings, "pot_path", "") or DEFAULT_POT_REL).strip()
    path = Path(raw)
    if path.is_absolute():
        return path
    artifacts = Path(getattr(settings, "artifacts_dir", "artifacts") or "artifacts")
    if raw in {DEFAULT_POT_REL, HOURLY_POT_FILE, str(Path("artifacts") / HOURLY_POT_FILE)}:
        return artifacts / HOURLY_POT_FILE
    return path


def trade_key(row: dict[str, Any]) -> str:
    """Stable id so the same filled trade is never applied twice."""
    ticker = str(row.get("ticker") or "").strip()
    order_id = str(row.get("order_id") or "").strip()
    if order_id:
        return f"{ticker}:{order_id}" if ticker else order_id
    client_id = str(row.get("client_order_id") or "").strip()
    if client_id:
        return f"{ticker}:{client_id}" if ticker else client_id
    stamp = str(
        row.get("resolved_ts_iso")
        or row.get("ts_iso")
        or row.get("resolved_ts")
        or row.get("ts")
        or ""
    ).strip()
    result = str(row.get("result") or "").strip()
    return f"{ticker}:{stamp}:{result}"


def is_live_filled_settled(row: dict[str, Any] | None) -> bool:
    """True for a live place that filled and settled win/loss. Paper/exits/backfills out."""
    if not isinstance(row, dict):
        return False
    if str(row.get("kind") or "").strip().lower() in {"paper", "backfill"}:
        return False
    if "backfill" in str(row.get("spot_source") or "").strip().lower():
        return False
    if str(row.get("action") or "").strip().lower() == "exit":
        return False
    if row.get("result") not in {"win", "loss"}:
        return False
    return counts_as_filled(row)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_float(payload: dict[str, Any], keys: tuple[str, ...], default: float) -> float:
    for key in keys:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return float(default)


def empty_pot(*, start_usd: float = DEFAULT_POT_START) -> dict[str, Any]:
    start = float(start_usd)
    return {
        "balance_usd": start,
        "start_usd": start,
        "realized_pnl_usd": 0.0,
        "status": "ok",
        "updated_at": "",
        "ledger": [],
        "notes": [],
    }


def load_pot(path: str | Path | None = None, *, start_usd: float = DEFAULT_POT_START) -> dict[str, Any]:
    dest = Path(path) if path else default_pot_path()
    if not dest.is_file():
        pot = empty_pot(start_usd=start_usd)
        pot["updated_at"] = _now_iso()
        return pot
    try:
        raw = json.loads(dest.read_text())
    except (json.JSONDecodeError, OSError):
        pot = empty_pot(start_usd=start_usd)
        pot["updated_at"] = _now_iso()
        pot["notes"].append("reset: corrupt pot file")
        return pot
    if not isinstance(raw, dict) or not raw:
        pot = empty_pot(start_usd=start_usd)
        pot["updated_at"] = _now_iso()
        return pot
    pot = empty_pot(start_usd=start_usd)
    pot["balance_usd"] = _first_float(raw, ("balance_usd", "balance", "bankroll"), start_usd)
    pot["start_usd"] = _first_float(raw, ("start_usd", "start", "starting_balance"), start_usd)
    pot["realized_pnl_usd"] = _first_float(raw, ("realized_pnl_usd", "realized_pnl", "pnl"), 0.0)
    pot["status"] = str(raw.get("status") or "ok")
    pot["updated_at"] = str(raw.get("updated_at") or "")
    pot["ledger"] = [entry for entry in (raw.get("ledger") or []) if isinstance(entry, dict)]
    pot["notes"] = [str(note) for note in (raw.get("notes") or [])]
    for key, value in raw.items():
        if key in CORE_KEYS:
            continue
        pot[key] = value
    return pot


def save_pot(pot: dict[str, Any], path: str | Path | None = None) -> Path:
    dest = Path(path) if path else default_pot_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    pot["updated_at"] = _now_iso()
    dest.write_text(json.dumps(pot, indent=2) + "\n")
    return dest


def _ledger_ids(pot: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    for entry in pot.get("ledger") or []:
        if not isinstance(entry, dict):
            continue
        for key_name in ("id", "trade_key", "order_id"):
            raw = str(entry.get(key_name) or "").strip()
            if raw:
                seen.add(raw)
    return seen


def apply_filled_trades(pot: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Credit/debit filled settled live trades not already in the ledger.

    Returns the new ledger entries. Unfilled, paper, backfill, and exit rows
    are ignored. Same trade_key is a no-op (idempotent).
    """
    applied: list[dict[str, Any]] = []
    seen = _ledger_ids(pot)
    stamp = _now_iso()
    for row in rows:
        if not is_live_filled_settled(row):
            continue
        key = trade_key(row)
        if not key or key in seen:
            continue
        try:
            pnl = float(row.get("pnl") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        ticker = str(row.get("ticker") or "")
        result = str(row.get("result") or "")
        pot["balance_usd"] = round(float(pot.get("balance_usd") or 0) + pnl, 4)
        pot["realized_pnl_usd"] = round(float(pot.get("realized_pnl_usd") or 0) + pnl, 4)
        entry = {
            "id": key,
            "ts": stamp,
            "ticker": ticker,
            "result": result,
            "pnl": round(pnl, 4),
        }
        order_id = str(row.get("order_id") or "").strip()
        if order_id:
            entry["order_id"] = order_id
        pot.setdefault("ledger", []).append(entry)
        pot.setdefault("notes", []).append(f"{ticker} {result} {pnl:+.2f}".strip())
        seen.add(key)
        applied.append(entry)
    return applied


def reconcile_hourly_pot(
    path: str | Path,
    rows: list[dict[str, Any]],
    *,
    start_usd: float = DEFAULT_POT_START,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load pot, apply any un-ledged filled settlements, save if anything changed.

    Creates the file when the first filled settlement is applied. Leaves a
    missing file alone when the journal has nothing to credit.
    """
    dest = Path(path)
    pot = load_pot(dest, start_usd=start_usd)
    applied = apply_filled_trades(pot, rows)
    if applied:
        save_pot(pot, dest)
    return pot, applied


def sync_hourly_pot(settings: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan/eval hook. Writes hourly_pot.json; does not touch live gates."""
    path = pot_path_from_settings(settings)
    start = float(getattr(settings, "bankroll", DEFAULT_POT_START) or DEFAULT_POT_START)
    pot, applied = reconcile_hourly_pot(path, rows, start_usd=start)
    for entry in applied:
        print(
            f"hourly pot {entry.get('ticker')} {entry.get('result')} "
            f"pnl={float(entry.get('pnl') or 0):+.2f} "
            f"(pot ${float(pot.get('balance_usd') or 0):.2f})"
        )
    return applied
