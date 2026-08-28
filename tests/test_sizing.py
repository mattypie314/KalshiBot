from kalshibot.campaign.playbook import Playbook
from kalshibot.campaign.sizing import (
    apply_phone_overrides,
    cash_from_balance,
    parse_money,
    parse_percent,
    parse_yes_no,
    playbook_from_sizing,
)
from kalshibot.config import Settings


def test_parse_money_and_percent():
    assert parse_money("$35") == 35
    assert parse_money(" 1,200 ") == 1200
    assert parse_money("") is None
    assert parse_percent("5") == 0.05
    assert parse_percent("5%") == 0.05
    assert parse_percent("0.04") == 0.04
    assert parse_yes_no("yes") is True
    assert parse_yes_no("no") is False
    assert parse_yes_no("keep") is None


def test_cash_from_balance_prefers_dollars():
    assert cash_from_balance({"balance_dollars": "32.50", "balance": 3250}) == 32.5
    assert cash_from_balance({"balance": 1800}) == 18.0


def test_phone_override_saves_on_tracker(tmp_path):
    from kalshibot.campaign.tracker import Tracker

    path = tmp_path / "crypto-campaign.json"
    tracker = Tracker(path, 15.0)
    notes = apply_phone_overrides(tracker, bankroll="35", follow="yes", risk_percent="5")
    state = tracker.load()
    assert state["bankroll"] == 35.0
    assert state["sizing"]["follow_kalshi_cash"] is True
    assert state["sizing"]["bankroll_cap"] == 35.0
    assert state["sizing"]["risk_percent"] == 5.0
    assert any("35" in n for n in notes)


def test_playbook_risk_percent_becomes_the_cap():
    book = playbook_from_sizing(Settings(), {"risk_percent": 5})
    assert book.typical_risk_max == 0.05
    assert book.risk_cap == 0.05
    assert book.kelly_fraction == Playbook().kelly_fraction
