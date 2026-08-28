from __future__ import annotations

from typing import Any

from kalshibot.campaign.playbook import Playbook, playbook_from_settings
from kalshibot.campaign.tracker import Tracker


def parse_money(raw: object) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    return float(text)


def parse_percent(raw: object) -> float | None:
    """Accept 5, 5%, or 0.05 and return a fraction."""
    if raw is None:
        return None
    text = str(raw).strip().replace("%", "")
    if not text:
        return None
    value = float(text)
    if value > 1:
        value = value / 100.0
    if value <= 0 or value > 1:
        raise ValueError(f"risk percent out of range: {raw}")
    return value


def parse_yes_no(raw: object) -> bool | None:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"", "keep", "same"}:
        return None
    if text in {"yes", "true", "1", "on"}:
        return True
    if text in {"no", "false", "0", "off"}:
        return False
    raise ValueError(f"follow_kalshi must be yes or no, not {raw!r}")


def cash_from_balance(payload: dict[str, Any]) -> float | None:
    raw = payload.get("balance_dollars")
    if raw not in (None, ""):
        return float(raw)
    cents = payload.get("balance")
    if isinstance(cents, (int, float)):
        return float(cents) / 100.0
    return None


def playbook_from_sizing(cfg: object, sizing: dict[str, Any] | None) -> Playbook:
    book = playbook_from_settings(cfg)
    if not sizing:
        return book
    values = book.as_status()
    risk = sizing.get("risk_percent")
    if risk is not None:
        frac = parse_percent(risk)
        values["typical_risk_max"] = frac
        values["typical_risk_min"] = min(float(values["typical_risk_min"]), frac)
        values["risk_cap"] = min(frac, float(values["risk_hard_max"]))
    cap = sizing.get("risk_cap_percent")
    if cap is not None:
        values["risk_cap"] = parse_percent(cap)
    kelly = sizing.get("kelly_fraction")
    if kelly is not None:
        values["kelly_fraction"] = float(kelly)
    return Playbook(**values)


def apply_phone_overrides(
    tracker: Tracker,
    *,
    bankroll: object = None,
    follow: object = None,
    maker_auto: object = None,
    risk_percent: object = None,
    risk_cap_percent: object = None,
) -> list[str]:
    """Save iPhone/Actions knobs onto the campaign tracker. Blank means keep."""
    tracker.load()
    sizing = dict(tracker.state.get("sizing") or {})
    notes: list[str] = []
    follow_val = parse_yes_no(follow)
    if follow_val is not None:
        sizing["follow_kalshi_cash"] = follow_val
        notes.append(f"Follow Kalshi cash: {'yes' if follow_val else 'no'}.")
    maker_auto_val = parse_yes_no(maker_auto)
    if maker_auto_val is not None:
        sizing["maker_auto"] = maker_auto_val
        notes.append(
            "Maker auto is ON. It will rest 74–93¢ bids every 15-minute window until you set this to no."
            if maker_auto_val
            else "Maker auto is OFF. No new last-3-min bids until you set maker_auto to yes."
        )
    money = parse_money(bankroll)
    if money is not None:
        if money <= 0:
            raise ValueError("bankroll must be positive")
        sizing["bankroll_cap"] = money
        tracker.set_bankroll(money)
        notes.append(f"Campaign book set to ${money:.2f}.")
    risk = parse_percent(risk_percent) if str(risk_percent or "").strip() else None
    if risk is not None:
        sizing["risk_percent"] = round(100 * risk, 4)
        notes.append(f"Risk per idea set to {100 * risk:.1f}%.")
    cap = parse_percent(risk_cap_percent) if str(risk_cap_percent or "").strip() else None
    if cap is not None:
        sizing["risk_cap_percent"] = round(100 * cap, 4)
        notes.append(f"Risk cap set to {100 * cap:.1f}%.")
    sizing.setdefault("follow_kalshi_cash", True)
    sizing.setdefault("maker_auto", True)
    tracker.state["sizing"] = sizing
    tracker.save()
    return notes
