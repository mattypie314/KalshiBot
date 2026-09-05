"""15m BTC/ETH edge-loop: windows, pass/fail, pot, gates, cancel isolation."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.executor import execute_ideas, is_fifteen_rest, is_hourly_rest
from src.fifteen.config import EXIT_CONFIG, FifteenSettings
from src.fifteen.edge import (
    CPI_DATES,
    enough_room,
    fifteen_session_date,
    fifteen_stake,
    fifteen_stopped,
    fifteen_window_id,
    fifteen_window_start,
    fifteen_working,
    half_sigma_move,
    in_fifteen_entry_window,
    in_fifteen_revenge,
    in_fifteen_settlement,
    news_blackout,
    next_et_midnight,
    pass_fail,
    record_fifteen_result,
    revenge_until_after_loss,
    strike_decided,
)
from src.fifteen.main import live_is_armed, main, normalize_argv
from src.fifteen.pot import credit_pot, load_pot, save_pot, set_open_risk
from src.filters import Idea
from src.markets import (
    FIFTEEN_BY_ASSET,
    FIFTEEN_SERIES,
    HourlyMarket,
    MarketDiscovery,
    in_current_or_next_15m,
)

ET = ZoneInfo("America/New_York")


def _et(hour: int, minute: int, day: int = 28, month: int = 8, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def test_entry_window_is_minutes_three_to_five():
    assert not in_fifteen_entry_window(_et(10, 2))
    assert in_fifteen_entry_window(_et(10, 3))
    assert in_fifteen_entry_window(_et(10, 4))
    assert in_fifteen_entry_window(_et(10, 5))
    assert in_fifteen_entry_window(_et(10, 18))
    assert in_fifteen_entry_window(_et(10, 33))
    assert in_fifteen_entry_window(_et(10, 50))
    assert not in_fifteen_entry_window(_et(10, 0))
    assert not in_fifteen_entry_window(_et(10, 1))
    assert not in_fifteen_entry_window(_et(10, 6))
    assert not in_fifteen_entry_window(_et(10, 12))


def test_settlement_and_window_id():
    assert in_fifteen_settlement(_et(10, 0))
    assert in_fifteen_settlement(_et(10, 15))
    assert not in_fifteen_settlement(_et(10, 2))
    assert fifteen_window_start(_et(10, 17)) == _et(10, 15)
    assert "10:15:00" in fifteen_window_id(_et(10, 17))


def test_pass_when_fair_clears_mid_by_four_cents():
    decision = pass_fail(
        model_yes=0.62, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4
    )
    assert decision.passed
    assert decision.side == "yes"
    assert decision.join_price == 0.54
    assert decision.line.startswith("PASS")


def test_pass_no_joins_yes_ask():
    decision = pass_fail(
        model_yes=0.38, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4
    )
    assert decision.passed
    assert decision.side == "no"
    assert decision.join_price == 0.56


def test_fail_within_four_cents_and_wide_spread():
    tight = pass_fail(
        model_yes=0.56, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4
    )
    assert not tight.passed
    assert "FAIL" in tight.line
    wide = pass_fail(
        model_yes=0.60, yes_bid=0.48, yes_ask=0.58, secs_left=12 * 60, sigma=0.4
    )
    assert not wide.passed
    assert "spread" in wide.line.lower()


def test_fail_under_eight_minutes_unless_decided():
    early = pass_fail(
        model_yes=0.70, yes_bid=0.54, yes_ask=0.56, secs_left=6 * 60, sigma=0.4
    )
    assert not early.passed
    decided = pass_fail(
        model_yes=0.98, yes_bid=0.90, yes_ask=0.92, secs_left=5 * 60, sigma=2.4
    )
    assert decided.passed
    assert strike_decided(0.98, 0.4)
    assert strike_decided(0.50, 2.0)


def test_fail_news_and_calendar_blackout():
    decision = pass_fail(
        model_yes=0.70,
        yes_bid=0.54,
        yes_ask=0.56,
        secs_left=12 * 60,
        sigma=0.4,
        news="CPI",
    )
    assert not decision.passed
    assert "CPI" in decision.line
    assert (2026, 9, 11) in CPI_DATES
    assert news_blackout(datetime(2026, 9, 11, 8, 30, tzinfo=ET)) == "CPI"
    assert news_blackout(datetime(2026, 9, 11, 10, 0, tzinfo=ET)) is None
    assert news_blackout(datetime(2026, 9, 16, 14, 0, tzinfo=ET)) == "FOMC"
    with patch.dict("os.environ", {"NEWS_BLACKOUT": "1"}):
        assert news_blackout(_et(10, 3)) == "NEWS_BLACKOUT"


def test_revenge_and_three_loss_session_stop():
    loss_at = _et(10, 8)
    state: dict = {}
    assert record_fifteen_result(state, -0.40, loss_at) is None
    assert in_fifteen_revenge(state, _et(10, 17))
    assert in_fifteen_revenge(state, _et(10, 29))
    assert not in_fifteen_revenge(state, _et(10, 32))
    assert revenge_until_after_loss(loss_at) == _et(10, 30)

    state = {}
    assert record_fifteen_result(state, -0.2, loss_at) is None
    assert record_fifteen_result(state, -0.2, loss_at + timedelta(minutes=30)) is None
    msg = record_fifteen_result(state, -0.2, loss_at + timedelta(minutes=60))
    assert msg is not None
    assert "15m" in msg and "stopped" in msg.lower()
    assert fifteen_stopped(state, loss_at + timedelta(minutes=61))
    assert not fifteen_stopped(state, next_et_midnight(loss_at))
    assert fifteen_session_date(loss_at) == "2026-08-28"


def test_win_resets_streak_and_working_blocks_window():
    now = _et(10, 8)
    state: dict = {}
    record_fifteen_result(state, -0.2, now)
    record_fifteen_result(state, -0.2, now)
    record_fifteen_result(state, 0.10, now)
    assert int(state.get("fifteen_loss_streak") or 0) == 0

    wid = fifteen_window_id(_et(10, 3))
    working = {
        "tickets": [{"status": "open", "loop": "fifteen", "window_id": wid}],
        "rests": [],
    }
    assert fifteen_working(working, _et(10, 3))
    working["tickets"][0]["status"] = "flat"
    assert not fifteen_working(working, _et(10, 3))
    working["rests"] = [{"status": "open", "loop": "fifteen", "window_id": wid}]
    assert fifteen_working(working, _et(10, 3))


def test_size_room_and_half_sigma():
    assert fifteen_stake(100.0, 100.0) == pytest.approx(4.0)
    assert fifteen_stake(100.0, 2.0) == pytest.approx(2.0)
    assert enough_room(3.0, 100.0)
    assert not enough_room(2.0, 100.0)
    assert not half_sigma_move(100.0, 100.0, 0.0045)
    assert half_sigma_move(100.5, 100.0, 0.0045)


def test_pot_double_ask_and_empty_stop(tmp_path: Path):
    path = tmp_path / "fifteen_pot.json"
    pot = load_pot(path)
    assert pot.balance == pytest.approx(5.0)
    assert pot.room == pytest.approx(5.0)
    msg = credit_pot(pot, 5.5)
    assert pot.ask_to_continue
    assert msg is not None
    save_pot(pot, path)
    reloaded = load_pot(path)
    assert reloaded.balance == pytest.approx(10.5)
    set_open_risk(reloaded, 2.0)
    assert reloaded.room == pytest.approx(8.5)
    empty_msg = credit_pot(reloaded, -20.0)
    assert reloaded.stopped
    assert empty_msg is not None


def test_fifteen_series_and_window_filter():
    assert set(FIFTEEN_SERIES) == {"KXBTC15M", "KXETH15M"}
    assert FIFTEEN_BY_ASSET["BTC"] == ("KXBTC15M",)
    assert FIFTEEN_BY_ASSET["ETH"] == ("KXETH15M",)
    now = _et(10, 3)
    assert in_current_or_next_15m(_et(10, 15), now)
    assert in_current_or_next_15m(_et(10, 30), now)
    assert not in_current_or_next_15m(_et(11, 0), now)


def test_discover_fifteen_only_loads_15m_series():
    now = _et(10, 3)
    close = _et(10, 15)

    class Client:
        def open_events(self, series, limit=20):
            series = str(series).upper()
            if series in {"KXBTCD", "KXETHD"}:
                raise AssertionError("hourly series must not be requested")
            if not series.endswith("15M"):
                return []
            return [
                {
                    "event_ticker": f"{series}-TEST",
                    "series_ticker": series,
                    "title": "BTC above" if "BTC" in series else "ETH above",
                    "markets": [
                        {
                            "ticker": f"{series}-TEST-T64000",
                            "event_ticker": f"{series}-TEST",
                            "series_ticker": series,
                            "status": "active",
                            "close_time": close.isoformat(),
                            "yes_bid_dollars": "0.54",
                            "yes_ask_dollars": "0.56",
                            "no_bid_dollars": "0.44",
                            "no_ask_dollars": "0.46",
                            "floor_strike": 64000,
                            "strike_type": "greater",
                            "yes_sub_title": "$64,000 or above",
                            "title": series,
                            "rules_primary": "CF Benchmarks BRTI",
                        }
                    ],
                }
            ]

    found = MarketDiscovery(Client()).discover_fifteen(["BTC"], now=now)
    assert found
    assert all(m.series_ticker.endswith("15M") for m in found)
    assert all(in_current_or_next_15m(m.close_time, now) for m in found)


def test_rest_filters_do_not_cross_bots():
    assert is_fifteen_rest({"ticker": "KXBTC15M-26SEP051015-T64000"})
    assert is_fifteen_rest({"series_ticker": "KXETH15M"})
    assert not is_fifteen_rest({"ticker": "KXBTCD-26SEP0510-T64000"})
    assert is_hourly_rest({"ticker": "KXBTCD-26SEP0510-T64000"})
    assert not is_hourly_rest({"ticker": "KXBTC15M-26SEP051015-T64000"})


def _idea() -> Idea:
    market = HourlyMarket(
        ticker="KXBTC15M-26SEP051015-T64000",
        event_ticker="KXBTC15M-26SEP051015",
        series_ticker="KXBTC15M",
        asset="BTC",
        title="BTC 15m",
        yes_sub_title="$64,000 or above",
        threshold=64000.0,
        strike_type="greater",
        close_time=_et(10, 15),
        status="active",
        yes_bid=0.54,
        yes_ask=0.56,
        no_bid=0.44,
        no_ask=0.46,
        yes_bid_size=10,
        yes_ask_size=10,
        no_bid_size=10,
        no_ask_size=10,
        rules_primary="",
        rules_secondary="",
        settlement_source="CF Benchmarks",
        exchange_index=2,
    )
    return Idea(
        market=market,
        side="Yes",
        entry_price=0.54,
        limit_price=0.54,
        fair=0.62,
        gross_edge=0.08,
        net_edge=0.08,
        fee_per_contract=0.02,
        fee_total=0.02,
        z=0.4,
        hours_left=0.2,
        contracts=2,
        risk_dollars=1.08,
        max_loss=1.08,
        rationale=["unit test"],
        post_maker=True,
    )


def test_fifteen_live_cancel_skips_hourly_rests(tmp_path: Path):
    client = MagicMock()
    client.get_orders.return_value = [
        {
            "order_id": "hourly-1",
            "ticker": "KXBTCD-26SEP0510-T64000",
            "client_order_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        },
        {
            "order_id": "fifteen-1",
            "ticker": "KXBTC15M-26SEP050945-T64000",
            "client_order_id": "ffffffff-1111-2222-3333-444444444444",
        },
    ]
    client.create_order.return_value = {
        "order": {"order_id": "new-15", "fill_count": "0.00", "remaining_count": "2.00"}
    }
    out = execute_ideas(
        [_idea()],
        client=client,
        artifacts_dir=tmp_path,
        live=True,
        confirm_live=True,
        cancel_stale=True,
        rest_filter=is_fifteen_rest,
    )
    canceled = {row["order_id"] for row in out.get("canceled", [])}
    assert canceled == {"fifteen-1"}


def test_fifteen_once_manages_open_positions(monkeypatch, tmp_path):
    called: dict = {}

    def fake_manage(*args, **kwargs):
        called["live"] = kwargs.get("live")
        called["series"] = set(kwargs.get("series") or [])
        return {"signals": [], "placed": [], "errors": [], "dry_run": [], "journal": []}

    class Client:
        can_trade = False

        def get_balance(self):
            return {"total_value": 5}

        def get_fills(self, limit=50):
            return []

    monkeypatch.setattr("src.fifteen.main.manage_open_positions", fake_manage)
    monkeypatch.setattr("src.fifteen.main.collect_ideas", lambda *a, **k: ([], ["sit"], None))
    monkeypatch.setattr("src.fifteen.main._client", lambda settings: Client())
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    from src.fifteen.main import run_scan

    settings = FifteenSettings(
        _env_file=None,
        artifacts_dir=str(tmp_path),
        state_path=str(tmp_path / "fifteen_state.json"),
        pot_path=str(tmp_path / "fifteen_pot.json"),
        trade_log_path=str(tmp_path / "fifteen_trade_log.jsonl"),
        paper_log_path=str(tmp_path / "fifteen_paper_log.jsonl"),
        scan_log_path=str(tmp_path / "fifteen_scan_log.jsonl"),
        halted=True,
    )
    assert run_scan(settings, asset=None, place=True, force_live=False) == 0
    assert called["live"] is False
    assert "KXBTC15M" in called["series"]


def test_cli_normalize_and_live_gates():
    assert normalize_argv(["s"]) == ["scan"]
    assert normalize_argv(["o"]) == ["once"]
    assert normalize_argv(["l"]) == ["live"]
    assert normalize_argv([]) == ["scan"]

    halted = FifteenSettings(halted=True, live_trading=True, confirm_live="YES")
    assert live_is_armed(halted, confirm="LIVE", isatty=True) is False
    env = FifteenSettings(halted=False, live_trading=True, confirm_live="YES")
    assert live_is_armed(env, confirm="", isatty=False) is True
    prompt = FifteenSettings(halted=False, live_trading=False, confirm_live="NO")
    assert live_is_armed(prompt, confirm="LIVE", isatty=True) is True
    assert live_is_armed(prompt, confirm="LIVE", isatty=False) is False
    assert main(["live", "--confirm", "LIVE"]) == EXIT_CONFIG


def test_pass_fail_rejects_overbought_rsi_on_yes():
    from src.indicators import TapeReading

    tape = TapeReading(
        rsi=82.0,
        adx=40.0,
        bb_mid=100.0,
        bb_upper=101.0,
        bb_lower=99.0,
        bb_bandwidth=0.02,
        percent_b=0.9,
        bars=40,
    )
    decision = pass_fail(
        model_yes=0.62, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4, tape=tape
    )
    assert not decision.passed
    assert "RSI overbought" in (decision.fail_reason or "")


def test_pass_fail_rejects_adx_chop():
    from src.indicators import TapeReading

    tape = TapeReading(
        rsi=50.0,
        adx=12.0,
        bb_mid=100.0,
        bb_upper=101.0,
        bb_lower=99.0,
        bb_bandwidth=0.02,
        percent_b=0.5,
        bars=40,
    )
    decision = pass_fail(
        model_yes=0.62, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4, tape=tape
    )
    assert not decision.passed
    assert "ADX chop" in (decision.fail_reason or "")
