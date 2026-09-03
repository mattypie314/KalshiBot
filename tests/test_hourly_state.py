from datetime import datetime, timezone
from pathlib import Path

from src.main import (
    _hour_key,
    fill_is_loss,
    load_state,
    market_result_is_loss,
    save_state,
    _resolve_settled_ticket,
)


def test_fill_is_loss_reads_pnl_aliases():
    assert fill_is_loss({"is_confirmed_loss": True})
    assert fill_is_loss({"pnl": "-1.2"})
    assert fill_is_loss({"realized_pnl_dollars": -0.5})
    assert not fill_is_loss({"pnl": "0"})
    assert not fill_is_loss({"ticker": "KXETHD-1"})


def test_market_result_is_loss():
    assert market_result_is_loss({"result": "no"}, "Yes") is True
    assert market_result_is_loss({"result": "yes"}, "Yes") is False
    assert market_result_is_loss({"status": "open"}, "Yes") is None


def test_load_state_keeps_ticket_across_hour_roll(tmp_path: Path):
    path = tmp_path / "state.json"
    older = datetime(2026, 9, 2, 3, 30, tzinfo=timezone.utc)
    save_state(
        path,
        {
            "hour_key": _hour_key(older),
            "last_ticker": "KXETHD-26SEP0200-T2424.99",
            "last_side": "Yes",
            "last_contracts": 7,
            "loss_this_hour": False,
            "kill_close_no": True,
        },
    )
    loaded = load_state(path)
    assert loaded["last_ticker"] == "KXETHD-26SEP0200-T2424.99"
    assert loaded["last_side"] == "Yes"
    assert loaded["last_contracts"] == 7
    assert loaded["hour_key"] == _hour_key(datetime.now(timezone.utc))
    assert loaded["kill_close_no"] is True
    assert "loss_this_hour" not in loaded


def test_resolve_settled_ticket_skips_loss_when_unfilled():
    class Client:
        def get_market(self, ticker):
            return {"result": "no", "status": "determined"}

    state = {"last_ticker": "KXETHD-1", "last_side": "Yes", "last_contracts": 7}
    out = _resolve_settled_ticket(Client(), state, fills=[], fills_available=True)
    assert "loss_this_hour" not in out
    assert "last_ticker" not in out


def test_resolve_settled_ticket_marks_loss():
    class Client:
        def get_market(self, ticker):
            assert ticker == "KXETHD-1"
            return {"result": "no", "status": "determined"}

    state = {"last_ticker": "KXETHD-1", "last_side": "Yes", "last_contracts": 7}
    out = _resolve_settled_ticket(Client(), state)
    assert out["loss_this_hour"] is True
    assert "last_ticker" not in out
    assert out["last_contracts"] == 7
