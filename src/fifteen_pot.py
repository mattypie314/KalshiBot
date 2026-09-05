"""Dedicated 15m pot. Own $5 — never hourly_pot / BANKROLL=40."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.clock import format_et, to_et


DEFAULT_START = 5.0
DEFAULT_ASK = 10.0


def empty_pot(*, start: float = DEFAULT_START, ask: float = DEFAULT_ASK) -> dict[str, Any]:
    return {
        "kind": "fifteen",
        "start": float(start),
        "pot": float(start),
        "ask_at": float(ask),
        "halted": False,
        "ask_notified": False,
        "updated": format_et(),
        "notes": [],
    }


def load_pot(path: Path, *, start: float = DEFAULT_START, ask: float = DEFAULT_ASK) -> dict[str, Any]:
    if not path.is_file():
        pot = empty_pot(start=start, ask=ask)
        save_pot(path, pot)
        return pot
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    pot = empty_pot(start=start, ask=ask)
    pot.update({key: data[key] for key in pot if key in data})
    pot["start"] = float(data.get("start") or start)
    pot["pot"] = float(data.get("pot") if data.get("pot") is not None else start)
    pot["ask_at"] = float(data.get("ask_at") or ask)
    pot["halted"] = bool(data.get("halted"))
    pot["ask_notified"] = bool(data.get("ask_notified"))
    if not isinstance(pot.get("notes"), list):
        pot["notes"] = []
    return pot


def save_pot(path: Path, pot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pot["updated"] = format_et()
    path.write_text(json.dumps(pot, indent=2, default=str))


def apply_pnl(pot: dict[str, Any], pnl: float, *, now: datetime | None = None) -> dict[str, Any]:
    """Apply a settled ticket to the pot. Empty → halted. $10 → notify, do not stop."""
    pot["pot"] = round(float(pot.get("pot") or 0) + float(pnl), 4)
    notes = list(pot.get("notes") or [])
    stamp = format_et(now or to_et())
    if pot["pot"] <= 0:
        pot["pot"] = 0.0
        pot["halted"] = True
        notes.append(f"{stamp}: pot empty — HALTED")
    ask_at = float(pot.get("ask_at") or DEFAULT_ASK)
    if pot["pot"] >= ask_at and not pot.get("ask_notified"):
        pot["ask_notified"] = True
        notes.append(f"{stamp}: pot ${pot['pot']:.2f} ≥ ${ask_at:.0f} — notify / ask (do not auto-stop)")
    pot["notes"] = notes[-20:]
    return pot


def remaining_room(pot: dict[str, Any]) -> float:
    if pot.get("halted"):
        return 0.0
    return max(0.0, float(pot.get("pot") or 0))


def pot_should_halt(pot: dict[str, Any]) -> bool:
    return bool(pot.get("halted")) or remaining_room(pot) <= 0
