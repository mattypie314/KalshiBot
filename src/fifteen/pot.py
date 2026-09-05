"""Dedicated $5 crypto-shard pot for the 15m BTC/ETH bot."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_POT_START = 5.0
DEFAULT_POT_DOUBLE = 10.0
DEFAULT_POT_EMPTY = 0.0


@dataclass
class FifteenPot:
    balance: float = DEFAULT_POT_START
    start: float = DEFAULT_POT_START
    double_at: float = DEFAULT_POT_DOUBLE
    empty_at: float = DEFAULT_POT_EMPTY
    realized_pnl: float = 0.0
    open_risk: float = 0.0
    stopped: bool = False
    ask_to_continue: bool = False
    updated_at: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def room(self) -> float:
        """Cash still available for a new idea: balance − open risk (floored at 0)."""
        return max(0.0, float(self.balance) - float(self.open_risk))

    def refresh_flags(self) -> str | None:
        """Update stop / double flags. Returns a message for Matt when something changes."""
        msg: str | None = None
        if self.balance <= self.empty_at + 1e-9:
            if not self.stopped:
                self.stopped = True
                msg = f"15m pot empty (${self.balance:.2f}). Quitting live 15m — tell Matt."
            self.stopped = True
        elif self.balance + 1e-9 >= self.double_at:
            if not self.ask_to_continue:
                self.ask_to_continue = True
                msg = (
                    f"15m pot hit ${self.balance:.2f} (double ≥ ${self.double_at:.2f}). "
                    "Ask Matt whether to keep going."
                )
        else:
            # Between empty and double: clear the ask flag only; never auto-unstop.
            self.ask_to_continue = False
        return msg


def default_pot_path(artifacts_dir: str | Path = "artifacts") -> Path:
    return Path(artifacts_dir) / "fifteen_pot.json"


def load_pot(path: str | Path | None = None) -> FifteenPot:
    dest = Path(path) if path else default_pot_path()
    if not dest.is_file():
        pot = FifteenPot()
        pot.updated_at = datetime.now(timezone.utc).isoformat()
        return pot
    try:
        raw = json.loads(dest.read_text())
    except (json.JSONDecodeError, OSError):
        pot = FifteenPot()
        pot.updated_at = datetime.now(timezone.utc).isoformat()
        pot.notes.append("reset: corrupt pot file")
        return pot
    if not isinstance(raw, dict) or not raw:
        pot = FifteenPot()
        pot.updated_at = datetime.now(timezone.utc).isoformat()
        return pot
    pot = FifteenPot(
        balance=float(raw.get("balance", DEFAULT_POT_START)),
        start=float(raw.get("start", DEFAULT_POT_START)),
        double_at=float(raw.get("double_at", DEFAULT_POT_DOUBLE)),
        empty_at=float(raw.get("empty_at", DEFAULT_POT_EMPTY)),
        realized_pnl=float(raw.get("realized_pnl", 0.0)),
        open_risk=float(raw.get("open_risk", 0.0)),
        stopped=bool(raw.get("stopped", False)),
        ask_to_continue=bool(raw.get("ask_to_continue", False)),
        updated_at=str(raw.get("updated_at") or ""),
        notes=list(raw.get("notes") or []),
    )
    pot.refresh_flags()
    return pot


def save_pot(pot: FifteenPot, path: str | Path | None = None) -> Path:
    dest = Path(path) if path else default_pot_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    pot.updated_at = datetime.now(timezone.utc).isoformat()
    pot.refresh_flags()
    payload: dict[str, Any] = asdict(pot)
    dest.write_text(json.dumps(payload, indent=2) + "\n")
    return dest


def credit_pot(pot: FifteenPot, pnl: float, *, note: str = "") -> str | None:
    """Apply a settled live 15m PnL to the pot. Returns an optional Matt message."""
    pot.balance = round(float(pot.balance) + float(pnl), 4)
    pot.realized_pnl = round(float(pot.realized_pnl) + float(pnl), 4)
    pot.open_risk = max(0.0, float(pot.open_risk))
    if note:
        pot.notes.append(note)
    return pot.refresh_flags()


def set_open_risk(pot: FifteenPot, dollars: float) -> None:
    pot.open_risk = max(0.0, float(dollars))
    pot.refresh_flags()
