from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _default_state(fifteen_bankroll: float, hourly_bankroll: float) -> dict[str, Any]:
    return {
        "pots": {
            "fifteen": {
                "bankroll": fifteen_bankroll,
                "realized": 0.0,
                "stopped": False,
                "stop_reason": None,
            },
            "hourly": {
                "bankroll": hourly_bankroll,
                "realized": 0.0,
                "stopped": False,
                "stop_reason": None,
            },
        },
        "tickets": [],
        "rests": [],
        "log": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


class Tracker:
    def __init__(self, path: str | Path, fifteen_bankroll: float = 5.0, hourly_bankroll: float = 10.0) -> None:
        self.path = Path(path).expanduser()
        self.fifteen_bankroll = fifteen_bankroll
        self.hourly_bankroll = hourly_bankroll
        self.state = _default_state(fifteen_bankroll, hourly_bankroll)

    def load(self) -> dict[str, Any]:
        default = _default_state(self.fifteen_bankroll, self.hourly_bankroll)
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
        self.state = default
        self.state.update(loaded)
        self.state.setdefault("pots", default["pots"])
        self.state.setdefault("tickets", [])
        self.state.setdefault("rests", [])
        self.state.setdefault("log", [])
        return self.state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
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
