from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _default_state(bankroll: float) -> dict[str, Any]:
    return {
        "bankroll": bankroll,
        "realized": 0.0,
        "tickets": [],
        "rests": [],
        "log": [],
        "last_loss_at": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _fold_legacy_pots(loaded: dict[str, Any], default_bankroll: float) -> dict[str, Any]:
    """Old saves split money into $5 / $10 pots. Fold them into one book."""
    pots = loaded.pop("pots", None)
    if not isinstance(pots, dict):
        loaded.setdefault("bankroll", default_bankroll)
        loaded.setdefault("realized", 0.0)
        return loaded
    fifteen = pots.get("fifteen") or {}
    hourly = pots.get("hourly") or {}
    if "bankroll" not in loaded:
        loaded["bankroll"] = float(fifteen.get("bankroll") or 0) + float(hourly.get("bankroll") or 0) or default_bankroll
    if "realized" not in loaded:
        loaded["realized"] = float(fifteen.get("realized") or 0) + float(hourly.get("realized") or 0)
    loaded.pop("stopped", None)
    loaded.pop("stop_reason", None)
    return loaded


class Tracker:
    def __init__(self, path: str | Path, bankroll: float = 15.0) -> None:
        self.path = Path(path).expanduser()
        self.bankroll = bankroll
        self.state = _default_state(bankroll)

    def load(self) -> dict[str, Any]:
        default = _default_state(self.bankroll)
        if not self.path.exists() or self.path.stat().st_size == 0:
            self.state = default
            self.save()
            return self.state
        try:
            loaded = json.loads(self.path.read_text() or "{}")
        except json.JSONDecodeError:
            self.state = default
            self.save()
            return self.state
        if not isinstance(loaded, dict):
            loaded = {}
        loaded = _fold_legacy_pots(loaded, self.bankroll)
        self.state = default
        self.state.update(loaded)
        self.state.setdefault("tickets", [])
        self.state.setdefault("rests", [])
        self.state.setdefault("log", [])
        self.state.setdefault("bankroll", self.bankroll)
        self.state.setdefault("realized", 0.0)
        self.state.setdefault("last_loss_at", None)
        self.state.pop("pots", None)
        return self.state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.state.pop("pots", None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    def note(self, message: str, loop: str, quiet: bool = False) -> dict[str, Any]:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "loop": loop,
            "message": message,
            "tell_matt": not quiet,
        }
        log = self.state.setdefault("log", [])
        log.append(entry)
        self.state["log"] = log[-200:]
        return entry

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.state))

    def set_bankroll(self, bankroll: float) -> None:
        """Raise or lower the book size without touching realized P&L."""
        self.state["bankroll"] = float(bankroll)
