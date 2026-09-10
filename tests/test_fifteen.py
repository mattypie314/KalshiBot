"""15m BTC/ETH edge-loop: windows, pass/fail, pot, gates, cancel isolation."""

from __future__ import annotations

import json
from dataclasses import replace
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
    labeled_join_price,
    net_edge_vs_join,
    news_blackout,
    next_et_midnight,
    pass_fail,
    record_fifteen_result,
    revenge_until_after_loss,
    seconds_until_entry_window,
    strike_decided,
    wait_for_entry_window,
    ENTRY_OFFSETS,
)
from src.fifteen.main import (
    collect_ideas,
    idea_from_pass,
    idea_fingerprint,
    journal_live_places,
    live_decision_for_window,
    live_is_armed,
    main,
    normalize_argv,
    paper_ideas_for_window,
    stamp_live_decision,
)
from src.sizer import economic_risk_dollars, maker_cost_per_contract, yes_book_price
from src.fifteen.pot import credit_pot, load_pot, save_pot, set_open_risk
from src.journal import load_trades, new_trade_row, write_trades
from src.fifteen.regime import CHOP_VETO_PHRASE
from src.spot import SpotSnapshot
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


def test_entry_window_is_minutes_two_to_six():
    assert in_fifteen_entry_window(_et(10, 2))
    assert in_fifteen_entry_window(_et(10, 3))
    assert in_fifteen_entry_window(_et(10, 4))
    assert in_fifteen_entry_window(_et(10, 5))
    assert in_fifteen_entry_window(_et(10, 6))
    assert in_fifteen_entry_window(_et(10, 17))
    assert in_fifteen_entry_window(_et(10, 18))
    assert in_fifteen_entry_window(_et(10, 33))
    assert in_fifteen_entry_window(_et(10, 50))
    assert in_fifteen_entry_window(_et(10, 51))
    assert not in_fifteen_entry_window(_et(10, 0))
    assert not in_fifteen_entry_window(_et(10, 1))
    assert not in_fifteen_entry_window(_et(10, 7))
    assert not in_fifteen_entry_window(_et(10, 12))


def test_seconds_until_entry_window_waits_early_and_skips_late():
    early = seconds_until_entry_window(_et(10, 1))
    assert early is not None and 50 <= early <= 60
    assert seconds_until_entry_window(_et(10, 2)) == 0.0
    assert seconds_until_entry_window(_et(10, 3)) == 0.0
    assert seconds_until_entry_window(_et(10, 6)) == 0.0
    assert seconds_until_entry_window(_et(10, 7)) is None
    assert seconds_until_entry_window(_et(10, 16)) is not None
    assert seconds_until_entry_window(_et(10, 22)) is None


def test_systemd_timer_fires_in_entry_window():
    """kalshi-15m.timer must land in ENTRY_OFFSETS (regression vs :01 fires)."""
    timer = Path(__file__).resolve().parents[1] / "scripts" / "kalshi-15m.timer"
    text = timer.read_text()
    assert "America/New_York" in text
    assert "OnCalendar=" in text
    assert "AccuracySec=1s" in text
    import re

    match = re.search(r"OnCalendar=\S+\s+\*:([0-9,]+):", text)
    assert match, text
    minutes = [int(part) for part in match.group(1).split(",") if part.strip()]
    assert 2 in minutes and 3 in minutes
    for minute in minutes:
        assert minute % 15 in ENTRY_OFFSETS, f"timer minute {minute} outside entry {ENTRY_OFFSETS}"


def test_systemd_service_allows_wait_for_entry_window():
    service = Path(__file__).resolve().parents[1] / "scripts" / "kalshi-15m.service"
    text = service.read_text()
    assert "TimeoutStartSec=300" in text


def test_wait_for_entry_window_sleeps_early_and_skips_late(monkeypatch):
    slept = []
    announced = []
    monkeypatch.setattr(
        "src.fifteen.edge.seconds_until_entry_window", lambda now=None: 47.0
    )
    out = wait_for_entry_window(sleeper=slept.append, announce=announced.append)
    assert out == 47.0
    assert slept == [47.0]
    assert announced == [47.0]

    monkeypatch.setattr(
        "src.fifteen.edge.seconds_until_entry_window", lambda now=None: None
    )
    assert wait_for_entry_window(sleeper=lambda _s: (_ for _ in ()).throw(AssertionError())) is None


def test_collect_ideas_waits_on_wall_clock(monkeypatch):
    from tests.test_regime import trending_ohlc

    waited = []
    now = _et(10, 3)
    market = _pass_market(now)
    _patch_collect(monkeypatch, trending_ohlc(), market)
    monkeypatch.setattr(
        "src.fifteen.main.wait_for_entry_window",
        lambda **_kwargs: waited.append(True) or 12.0,
    )
    monkeypatch.setattr("src.fifteen.main.to_et", lambda stamp=None: now)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, _notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=None,
        apply_chop_veto=True,
    )
    assert waited == [True]
    assert len(ideas) == 1


def test_collect_ideas_frozen_early_now_does_not_wait(monkeypatch):
    from tests.test_regime import trending_ohlc

    now = _et(10, 1)
    market = _pass_market(now)
    _patch_collect(monkeypatch, trending_ohlc(), market)

    def _boom(**_kwargs):
        raise AssertionError("frozen now must not wait")

    monkeypatch.setattr("src.fifteen.main.wait_for_entry_window", _boom)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    assert ideas == []
    assert any("outside entry window" in note and "minute 1" in note for note in notes)


def test_collect_ideas_minute_two_looks(monkeypatch):
    """Pi oneshot next-fire was :02 — that must not sit."""
    from tests.test_regime import trending_ohlc

    now = _et(10, 2)
    market = _pass_market(now)
    _patch_collect(monkeypatch, trending_ohlc(), market)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    assert len(ideas) == 1
    assert not any("outside entry window" in note for note in notes)


def test_run_scan_does_not_wait_before_journal():
    text = Path(collect_ideas.__code__.co_filename).read_text()
    assert "Do not wait here. Journal / balance / exits" in text
    assert "collect_ideas waits until 2–6" in text


def test_install_pi_15m_units_copies_timer():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "scripts" / "install-pi-15m-units.sh").read_text()
    assert "kalshi-15m.timer" in installer
    assert "kalshi-hourly.timer" in installer
    assert "daemon-reload" in installer
    assert "does not enable" in installer.lower() or "still disabled" in installer


def test_fifteen_example_env_risk_and_paths_bind(monkeypatch, tmp_path):
    """Operator knobs in .env.fifteen.example must reach FifteenSettings."""
    monkeypatch.setenv("PREFERRED_RISK_DOLLARS", "0.85")
    monkeypatch.setenv("MAX_RISK_DOLLARS", "1.50")
    monkeypatch.setenv("FIFTEEN_POT_ASK", "10")
    monkeypatch.setenv("FIFTEEN_VOL_LOOKBACK_MINUTES", "90")
    monkeypatch.setenv("FIFTEEN_VOL_FALLBACK_BTC", "0.0041")
    monkeypatch.setenv("POT_PATH", str(tmp_path / "fifteen_pot.json"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "fifteen_state.json"))
    settings = FifteenSettings()
    assert settings.preferred_risk_dollars == 0.85
    assert settings.max_risk_dollars == 1.50
    assert settings.pot_double == 10.0
    assert settings.vol_lookback_minutes == 90
    assert settings.hourly_vol_fallback_btc == 0.0041
    assert settings.pot_path.endswith("fifteen_pot.json")
    assert settings.state_path.endswith("fifteen_state.json")


def test_settlement_and_window_id():
    assert in_fifteen_settlement(_et(10, 0))
    assert in_fifteen_settlement(_et(10, 15))
    assert not in_fifteen_settlement(_et(10, 2))
    assert fifteen_window_start(_et(10, 17)) == _et(10, 15)
    assert "10:15:00" in fifteen_window_id(_et(10, 17))


def test_pass_when_fair_clears_join_plus_fee_by_four_cents():
    decision = pass_fail(
        model_yes=0.62, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4
    )
    assert decision.passed
    assert decision.side == "yes"
    assert decision.join_price == 0.54
    assert "vs join 0.54" in decision.line
    assert "vs mid" not in decision.line
    assert decision.line.startswith("PASS")
    yes_net = net_edge_vs_join(0.62, labeled_join_price("yes", 0.54, 0.56))
    assert yes_net == pytest.approx(decision.edge)
    assert yes_net >= 0.04


def test_pass_no_joins_yes_ask():
    decision = pass_fail(
        model_yes=0.38, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4
    )
    assert decision.passed
    assert decision.side == "no"
    assert decision.join_price == 0.56
    no_net = net_edge_vs_join(0.62, labeled_join_price("no", 0.54, 0.56))
    assert no_net == pytest.approx(-decision.edge)


def _idea_from_decision(decision, *, now=None, **settings_kw):
    now = now or _et(10, 3)
    market = _pass_market(now, yes_bid=0.22, yes_ask=0.24)
    defaults = dict(
        _env_file=None,
        preferred_risk_dollars=1.50,
        max_risk_dollars=1.50,
        pot_start=5.00,
        kelly_mult=0.25,
    )
    defaults.update(settings_kw)
    return idea_from_pass(
        market,
        decision,
        spot=2300.0,
        vol=0.005,
        bankroll=40.0,
        room=5.0,
        settings=FifteenSettings(**defaults),
        now=now,
    )


def test_idea_from_pass_no_via_sell_yes_never_exceeds_max_risk():
    """Cheap Yes ask must not size as if each No contract only costs that ask."""
    decision = pass_fail(
        model_yes=0.18, yes_bid=0.22, yes_ask=0.24, secs_left=12 * 60, sigma=0.4
    )
    assert decision.passed
    assert decision.side == "no"
    assert decision.join_price == 0.24
    idea = _idea_from_decision(decision)
    assert idea is not None
    cost = maker_cost_per_contract("No", yes_book=decision.join_price)
    assert cost == 0.76
    assert idea.limit_price == pytest.approx(0.76)
    assert yes_book_price(idea.side, idea.limit_price) == pytest.approx(0.24)
    assert idea.contracts * cost <= 1.50 + 1e-9
    assert idea.risk_dollars == economic_risk_dollars(idea.contracts, cost)
    assert idea.risk_dollars <= 1.50 + 1e-9
    # Old bug: 4 × 0.24 = $0.96 journaled while Kalshi locked 4 × 0.76.
    assert idea.contracts * 0.24 < idea.risk_dollars or idea.contracts <= 1


def test_idea_from_pass_yes_via_buy_yes_sizes_on_limit():
    decision = pass_fail(
        model_yes=0.62, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4
    )
    assert decision.passed and decision.side == "yes"
    now = _et(10, 3)
    market = _pass_market(now, yes_bid=0.54, yes_ask=0.56)
    idea = idea_from_pass(
        market,
        decision,
        spot=65000.0,
        vol=0.004,
        bankroll=40.0,
        room=5.0,
        settings=FifteenSettings(
            _env_file=None,
            preferred_risk_dollars=1.50,
            max_risk_dollars=1.50,
            pot_start=5.00,
        ),
        now=now,
    )
    assert idea is not None
    assert idea.side == "Yes"
    assert idea.limit_price == pytest.approx(0.54)
    assert idea.risk_dollars == economic_risk_dollars(idea.contracts, 0.54)
    assert idea.risk_dollars <= 1.50 + 1e-9
    assert idea.contracts == 2


def test_fail_within_four_cents_and_wide_spread():
    tight = pass_fail(
        model_yes=0.56, yes_bid=0.54, yes_ask=0.56, secs_left=12 * 60, sigma=0.4
    )
    assert not tight.passed
    assert "FAIL" in tight.line
    # 10¢ book: mid looks like +4¢ (0.57 vs 0.53) but join+fee net < spread.
    wide = pass_fail(
        model_yes=0.57, yes_bid=0.48, yes_ask=0.58, secs_left=12 * 60, sigma=0.4
    )
    assert not wide.passed
    assert "spread" in wide.line.lower()


def test_pass_fail_wide_spread_mid_illusion_vs_join():
    """Wide book: mid can show ~4¢ Yes edge you cannot rest as a maker.

    bid 0.48 / ask 0.58 → mid 0.53. model 0.57 is +4¢ vs mid (old Pass bar)
    but is not restable. Yes join is 0.48; after the taker-fee haircut the
    net is still under the spread, so Pass fails the spread≤edge gate.
    """
    model_yes, yes_bid, yes_ask = 0.57, 0.48, 0.58
    mid = (yes_bid + yes_ask) / 2.0
    assert mid == pytest.approx(0.53)
    assert model_yes - mid == pytest.approx(0.04)

    yes_net = net_edge_vs_join(model_yes, labeled_join_price("yes", yes_bid, yes_ask))
    spread = yes_ask - yes_bid
    assert yes_net > 0
    assert spread > yes_net

    decision = pass_fail(
        model_yes=model_yes,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        secs_left=12 * 60,
        sigma=0.4,
    )
    assert not decision.passed
    assert decision.side == "yes"
    assert decision.join_price == yes_bid
    assert "vs join 0.48" in decision.line
    assert "spread" in decision.line.lower()
    assert "vs mid" not in decision.line


def test_pass_fail_fee_haircut_rejects_four_cent_mid_edge():
    """Tight book: +4¢ vs mid, but join + taker fee is under the 4¢ net bar."""
    model_yes, yes_bid, yes_ask = 0.59, 0.54, 0.56
    mid = (yes_bid + yes_ask) / 2.0
    assert model_yes - mid == pytest.approx(0.04)
    yes_net = net_edge_vs_join(model_yes, labeled_join_price("yes", yes_bid, yes_ask))
    assert yes_net < 0.04

    decision = pass_fail(
        model_yes=model_yes,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        secs_left=12 * 60,
        sigma=0.4,
    )
    assert not decision.passed
    assert decision.join_price == yes_bid
    assert decision.fail_reason == "within 4 cents"


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


def test_canceled_resting_entry_still_blocks_window():
    """Matt canceling a rest is respected: we do not immediately re-fire that coin."""
    wid = fifteen_window_id(_et(10, 3))
    state = {
        "tickets": [
            {
                "status": "open",
                "loop": "fifteen",
                "window_id": wid,
                "ticker": "KXBTC15M-1",
                "side": "Yes",
            }
        ]
    }
    assert fifteen_working(state, _et(10, 3))
    assert fifteen_working(state, _et(10, 8))
    assert fifteen_working(state, _et(10, 3), asset="BTC")
    assert not fifteen_working(state, _et(10, 3), asset="ETH")


def test_fifteen_working_is_per_asset():
    wid = fifteen_window_id(_et(10, 3))
    btc = {
        "tickets": [
            {
                "status": "open",
                "loop": "fifteen",
                "window_id": wid,
                "ticker": "KXBTC15M-1",
                "asset": "BTC",
            }
        ],
        "rests": [],
    }
    assert fifteen_working(btc, _et(10, 3), asset="BTC")
    assert not fifteen_working(btc, _et(10, 3), asset="ETH")
    both = {
        "tickets": [
            {
                "status": "open",
                "loop": "fifteen",
                "window_id": wid,
                "ticker": "KXBTC15M-1",
                "asset": "BTC",
            },
            {
                "status": "open",
                "loop": "fifteen",
                "window_id": wid,
                "ticker": "KXETH15M-1",
                "asset": "ETH",
            },
        ]
    }
    assert fifteen_working(both, _et(10, 3), asset="BTC")
    assert fifteen_working(both, _et(10, 3), asset="ETH")
    assert fifteen_working(both, _et(10, 3))


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
    save_pot(reloaded, path)
    again = load_pot(path)
    assert again.stopped
    assert again.balance <= 0
    # Empty pot stays empty. Operator refills; load/save must not reset to $5.
    assert again.balance != 5.0


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


def _eth_idea() -> Idea:
    idea = _idea()
    return replace(
        idea,
        market=replace(
            idea.market,
            ticker="KXETH15M-26SEP070630-30",
            event_ticker="KXETH15M-26SEP070630",
            series_ticker="KXETH15M",
            asset="ETH",
            title="ETH 15m",
            yes_sub_title="$2,400 or above",
            threshold=2400.0,
        ),
        spot=2400.0,
    )


def _spots() -> SpotSnapshot:
    return SpotSnapshot(
        prices={"BTC": 65000.0, "ETH": 2400.0},
        hourly_vol={"BTC": 0.004, "ETH": 0.005},
        sources={"BTC": "cfbenchmarks", "ETH": "cfbenchmarks"},
        source="cfbenchmarks",
    )


def _idea_named(ticker: str) -> Idea:
    idea = _idea()
    return replace(idea, market=replace(idea.market, ticker=ticker))


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
    assert normalize_argv(["livescore"]) == ["livescore"]
    assert normalize_argv(["score"]) == ["score"]
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

def _pass_market(
    now: datetime,
    ticker: str = "KXBTC15M-TEST-T64000",
    *,
    asset: str = "BTC",
    threshold: float | None = None,
    yes_bid: float = 0.54,
    yes_ask: float = 0.56,
) -> HourlyMarket:
    close = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) + timedelta(
        minutes=15
    )
    series = "KXETH15M" if asset == "ETH" else "KXBTC15M"
    strike = 2300.0 if asset == "ETH" else 64000.0
    if threshold is not None:
        strike = threshold
    return HourlyMarket(
        ticker=ticker,
        event_ticker=f"{series}-TEST",
        series_ticker=series,
        asset=asset,
        title=f"{asset} 15m",
        yes_sub_title=f"${strike:,.0f} or above",
        threshold=strike,
        strike_type="greater",
        close_time=close,
        status="active",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=max(0.01, 1.0 - yes_ask),
        no_ask=max(0.02, 1.0 - yes_bid),
        yes_bid_size=10,
        yes_ask_size=10,
        no_bid_size=10,
        no_ask_size=10,
        rules_primary="CF Benchmarks BRTI",
        rules_secondary="",
        settlement_source="CF Benchmarks",
        exchange_index=2,
    )


class _FakeSpotService:
    def __init__(self, candles, **_kwargs):
        self._candles = candles
        self._prices = {"BTC": 65000.0, "ETH": 2400.0}

    def snapshot(self, assets, fallbacks=None):
        return SpotSnapshot(
            prices=dict(self._prices),
            hourly_vol={"BTC": 0.004, "ETH": 0.005},
            sources={"BTC": "cfbenchmarks", "ETH": "cfbenchmarks"},
            source="cfbenchmarks",
            candles={name: list(self._candles) for name in self._prices},
        )

    def close(self):
        return None


def _patch_collect(monkeypatch, candles, market, extra_markets=None):
    monkeypatch.setattr(
        "src.fifteen.main.SpotService",
        lambda **kwargs: _FakeSpotService(candles),
    )
    # Unit tests inject OHLC via FakeSpotService; do not pull live CCXT tape.
    monkeypatch.setattr("src.fifteen.main.signals_for_asset", lambda *a, **k: {})
    markets = [market, *(extra_markets or [])]

    class Discovery:
        def __init__(self, client):
            self.client = client

        def discover_fifteen(self, assets, **kwargs):
            want = {str(name).upper() for name in assets}
            return [row for row in markets if row.asset in want]

    monkeypatch.setattr("src.fifteen.main.MarketDiscovery", Discovery)


def _stopped_state(now: datetime) -> dict:
    state: dict = {}
    record_fifteen_result(state, -0.2, now)
    record_fifteen_result(state, -0.2, now + timedelta(minutes=15))
    msg = record_fifteen_result(state, -0.2, now + timedelta(minutes=30))
    assert msg is not None
    assert fifteen_stopped(state, now + timedelta(minutes=31))
    return state


def test_collect_ideas_session_stop_does_not_wipe_passes(monkeypatch):
    from tests.test_regime import trending_ohlc

    # Third loss is at 10:33 → revenge until 11:00, session stop until midnight.
    # Collect after revenge expires so only the (disabled) session stop is in play.
    loss_at = _et(10, 3)
    now = _et(11, 3)
    market = _pass_market(now)
    _patch_collect(monkeypatch, trending_ohlc(), market)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    state = _stopped_state(loss_at)
    assert fifteen_stopped(state, now)
    assert not in_fifteen_revenge(state, now)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state=state,
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    assert len(ideas) == 1
    assert ideas[0].market.ticker == market.ticker
    assert not any("session stopped" in note.lower() for note in notes)


def test_collect_ideas_revenge_sits_live_and_paper(monkeypatch):
    from tests.test_regime import trending_ohlc

    loss_at = _et(10, 3)
    now = _et(10, 18)
    market = _pass_market(now)
    _patch_collect(monkeypatch, trending_ohlc(), market)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    state: dict = {}
    record_fifteen_result(state, -0.40, loss_at)
    assert in_fifteen_revenge(state, now)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state=state,
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    assert ideas == []
    assert any("revenge window after a loser" in note for note in notes)


def test_collect_ideas_chops_veto_after_pass(monkeypatch):
    from tests.test_regime import choppy_ohlc

    now = _et(10, 3)
    market = _pass_market(now)
    _patch_collect(monkeypatch, choppy_ohlc(), market)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    assert ideas == []
    assert any(CHOP_VETO_PHRASE in note for note in notes)


def test_collect_ideas_trend_still_passes(monkeypatch):
    from tests.test_regime import trending_ohlc

    now = _et(10, 3)
    market = _pass_market(now)
    _patch_collect(monkeypatch, trending_ohlc(), market)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    assert len(ideas) == 1
    assert ideas[0].market.ticker == market.ticker
    assert not any(CHOP_VETO_PHRASE in note for note in notes)


def test_collect_ideas_scanned_markets_includes_sits(monkeypatch):
    from tests.test_regime import choppy_ohlc

    now = _et(10, 3)
    market = _pass_market(now)
    _patch_collect(monkeypatch, choppy_ohlc(), market)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    scanned: list[HourlyMarket] = []
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
        scanned_markets=scanned,
    )
    assert ideas == []
    assert any(CHOP_VETO_PHRASE in note for note in notes)
    assert [row.ticker for row in scanned] == [market.ticker]


def test_collect_ideas_chop_override_false_still_passes(monkeypatch):
    from tests.test_regime import choppy_ohlc

    now = _et(10, 3)
    market = _pass_market(now)
    _patch_collect(monkeypatch, choppy_ohlc(), market)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=False,
    )
    assert len(ideas) == 1
    assert not any(CHOP_VETO_PHRASE in note for note in notes)


def test_collect_ideas_default_uses_settings_chop_veto(monkeypatch):
    from tests.test_regime import choppy_ohlc

    now = _et(10, 3)
    market = _pass_market(now)
    _patch_collect(monkeypatch, choppy_ohlc(), market)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
    )
    assert ideas == []
    assert any(CHOP_VETO_PHRASE in note for note in notes)


def test_collect_ideas_both_assets_pass(monkeypatch):
    from tests.test_regime import trending_ohlc

    now = _et(10, 3)
    btc = _pass_market(now, "KXBTC15M-TEST-T64000", asset="BTC")
    eth = _pass_market(now, "KXETH15M-TEST-T2300", asset="ETH")
    _patch_collect(monkeypatch, trending_ohlc(), btc, extra_markets=[eth])
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    assert {idea.market.asset for idea in ideas} == {"BTC", "ETH"}
    assert not any(CHOP_VETO_PHRASE in note for note in notes)


def test_collect_ideas_two_btc_passes_keeps_best(monkeypatch):
    from tests.test_regime import trending_ohlc

    now = _et(10, 3)
    better = _pass_market(
        now,
        "KXBTC15M-TEST-T64000",
        asset="BTC",
        threshold=64000.0,
        yes_bid=0.50,
        yes_ask=0.52,
    )
    worse = _pass_market(
        now,
        "KXBTC15M-TEST-T63000",
        asset="BTC",
        threshold=63000.0,
        yes_bid=0.78,
        yes_ask=0.80,
    )
    _patch_collect(monkeypatch, trending_ohlc(), better, extra_markets=[worse])
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    assert [idea.market.ticker for idea in ideas] == ["KXBTC15M-TEST-T64000"]
    assert any("KXBTC15M-TEST-T63000" in note and "held back" in note for note in notes)


def test_collect_ideas_btc_working_still_allows_eth(monkeypatch):
    from tests.test_regime import trending_ohlc

    now = _et(10, 3)
    wid = fifteen_window_id(now)
    btc = _pass_market(now, "KXBTC15M-TEST-T64000", asset="BTC")
    eth = _pass_market(now, "KXETH15M-TEST-T2300", asset="ETH")
    _patch_collect(monkeypatch, trending_ohlc(), btc, extra_markets=[eth])
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    ideas, notes, _spots = collect_ideas(
        settings,
        client=MagicMock(),
        state={
            "tickets": [
                {
                    "status": "open",
                    "loop": "fifteen",
                    "window_id": wid,
                    "ticker": "KXBTC15M-1",
                    "asset": "BTC",
                }
            ],
            "rests": [],
        },
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    assert [idea.market.asset for idea in ideas] == ["ETH"]
    assert any("already working" in note and "BTC" in note for note in notes)


def test_run_scan_live_and_paper_share_chop_veto(monkeypatch, tmp_path):
    seen: list[bool | None] = []

    def fake_collect(*args, **kwargs):
        seen.append(kwargs.get("apply_chop_veto"))
        return [], ["sit"], None

    class Client:
        can_trade = False

        def get_balance(self):
            return {"total_value": 5}

        def get_fills(self, limit=50):
            return []

    monkeypatch.setattr("src.fifteen.main.collect_ideas", fake_collect)
    monkeypatch.setattr("src.fifteen.main._client", lambda settings: Client())
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.fifteen.main.manage_open_positions",
        lambda *a, **k: {"signals": [], "placed": [], "errors": [], "dry_run": [], "journal": []},
    )
    from src.fifteen.main import run_scan

    settings = FifteenSettings(
        _env_file=None,
        artifacts_dir=str(tmp_path),
        state_path=str(tmp_path / "fifteen_state.json"),
        pot_path=str(tmp_path / "fifteen_pot.json"),
        trade_log_path=str(tmp_path / "fifteen_trade_log.jsonl"),
        paper_log_path=str(tmp_path / "fifteen_paper_log.jsonl"),
        scan_log_path=str(tmp_path / "fifteen_scan_log.jsonl"),
        halted=False,
        chop_veto=True,
    )
    assert run_scan(settings, asset=None, place=True, force_live=True, armed=False) == 0
    assert run_scan(settings, asset=None, place=False, force_live=False) == 0
    assert seen == [True, True]


def _fifteen_settings(tmp_path: Path, **kwargs) -> FifteenSettings:
    defaults = dict(
        _env_file=None,
        artifacts_dir=str(tmp_path),
        state_path=str(tmp_path / "fifteen_state.json"),
        pot_path=str(tmp_path / "fifteen_pot.json"),
        trade_log_path=str(tmp_path / "fifteen_trade_log.jsonl"),
        paper_log_path=str(tmp_path / "fifteen_paper_log.jsonl"),
        scan_log_path=str(tmp_path / "fifteen_scan_log.jsonl"),
        halted=False,
        chop_veto=True,
    )
    defaults.update(kwargs)
    return FifteenSettings(**defaults)


def _quiet_scan_client(*, fills=None, market=None, can_trade=True):
    class Client:
        def get_balance(self):
            return {"total_value": 5}

        def get_fills(self, limit=50):
            return list(fills or [])

        def get_market(self, ticker):
            return dict(market or {"status": "active"})

    Client.can_trade = can_trade
    return Client()


def test_run_scan_live_refuses_when_pot_empty(monkeypatch, tmp_path, capsys):
    """Empty pot blocks new live risk and must not auto-refill to $5."""
    idea = _idea()
    executed: list = []

    def fake_collect(*args, **kwargs):
        return [idea], [], _spots()

    def fake_execute(*args, **kwargs):
        executed.append(kwargs)
        return {"placed": [{"order_id": "should-not-fire"}], "orders": [], "errors": []}

    pot_path = tmp_path / "fifteen_pot.json"
    pot = load_pot(pot_path)
    credit_pot(pot, -5.0)
    save_pot(pot, pot_path)
    assert pot.stopped
    assert pot.balance <= 0

    monkeypatch.setattr("src.fifteen.main.collect_ideas", fake_collect)
    monkeypatch.setattr("src.fifteen.main.execute_ideas", fake_execute)
    monkeypatch.setattr("src.fifteen.main._client", lambda settings: _quiet_scan_client())
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.fifteen.main.manage_open_positions",
        lambda *a, **k: {"signals": [], "placed": [], "errors": [], "dry_run": [], "journal": []},
    )
    from src.fifteen.main import run_scan

    settings = _fifteen_settings(tmp_path)
    assert run_scan(settings, asset=None, place=True, force_live=True, armed=True) == 0
    assert executed == []
    assert load_trades(tmp_path / "fifteen_trade_log.jsonl") == []
    out = capsys.readouterr().out
    assert "pot empty" in out.lower()
    assert "not auto-refill" in out.lower()
    reloaded = load_pot(pot_path)
    assert reloaded.stopped
    assert reloaded.balance <= 0
    assert reloaded.balance != 5.0


def test_run_scan_paper_still_collects_when_pot_empty(monkeypatch, tmp_path):
    idea = _idea()

    def fake_collect(*args, **kwargs):
        return [idea], [], _spots()

    pot_path = tmp_path / "fifteen_pot.json"
    pot = load_pot(pot_path)
    credit_pot(pot, -5.0)
    save_pot(pot, pot_path)

    monkeypatch.setattr("src.fifteen.main.collect_ideas", fake_collect)
    monkeypatch.setattr("src.fifteen.main._client", lambda settings: _quiet_scan_client(can_trade=False))
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    from src.fifteen.main import run_scan

    settings = _fifteen_settings(tmp_path)
    assert run_scan(settings, asset=None, place=False, force_live=False) == 0
    paper = load_trades(tmp_path / "fifteen_paper_log.jsonl")
    assert [row["ticker"] for row in paper] == [idea.market.ticker]
    assert load_trades(tmp_path / "fifteen_trade_log.jsonl") == []
    reloaded = load_pot(pot_path)
    assert reloaded.stopped
    assert reloaded.balance <= 0


def test_run_scan_live_journals_place_and_shadows_paper(monkeypatch, tmp_path):
    idea = _idea()
    spots = _spots()

    def fake_collect(*args, **kwargs):
        return [idea], [], spots

    def fake_execute(*args, **kwargs):
        return {
            "placed": [
                {
                    "order_id": "live-15",
                    "ticker": idea.market.ticker,
                    "fill_count": "0.00",
                    "remaining_count": "2.00",
                    "client_order_id": "cid-15",
                }
            ],
            "orders": [{"ticker": idea.market.ticker, "client_order_id": "cid-15"}],
            "errors": [],
        }

    monkeypatch.setattr("src.fifteen.main.collect_ideas", fake_collect)
    monkeypatch.setattr("src.fifteen.main.execute_ideas", fake_execute)
    monkeypatch.setattr("src.fifteen.main._client", lambda settings: _quiet_scan_client())
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.fifteen.main.manage_open_positions",
        lambda *a, **k: {"signals": [], "placed": [], "errors": [], "dry_run": [], "journal": []},
    )
    from src.fifteen.main import run_scan

    settings = _fifteen_settings(tmp_path)
    assert run_scan(settings, asset=None, place=True, force_live=True, armed=True) == 0
    rows = load_trades(tmp_path / "fifteen_trade_log.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == idea.market.ticker
    assert row["side"] == idea.side
    assert row["order_id"] == "live-15"
    assert row["client_order_id"] == "cid-15"
    assert row["fill_status"] == "resting"
    assert row["result"] == "pending"
    assert row["limit_price"] == idea.limit_price
    assert row["risk_dollars"] == idea.risk_dollars
    assert row.get("kind") != "paper"
    paper = load_trades(tmp_path / "fifteen_paper_log.jsonl")
    assert len(paper) == 1
    assert paper[0]["kind"] == "paper"
    assert paper[0]["ticker"] == idea.market.ticker
    assert paper[0]["side"] == idea.side
    assert paper[0]["limit_price"] == idea.limit_price
    assert paper[0]["contracts"] == idea.contracts
    assert paper[0]["fill_status"] == "assumed-maker-fill"
    assert paper[0]["shadow"] == "live"
    assert paper[0]["window_id"] == row["window_id"]
    state = json.loads((tmp_path / "fifteen_state.json").read_text())
    assert state["tickets"][0]["ticker"] == idea.market.ticker
    assert state["tickets"][0]["order_id"] == "live-15"
    assert state["live_decision"]["tickers"] == [idea.market.ticker]


def test_run_scan_resolves_live_journal_fill_and_settlement(monkeypatch, tmp_path):
    ticker = "KXBTC15M-26SEP051015-T64000"
    write_trades(
        tmp_path / "fifteen_trade_log.jsonl",
        [
            new_trade_row(
                ticker=ticker,
                asset="BTC",
                side="Yes",
                strike=64000.0,
                spot=65000.0,
                minutes_left=12.0,
                fair=0.62,
                kalshi_price=0.54,
                limit_price=0.54,
                contracts=2,
                risk_dollars=1.08,
                hourly_vol=0.004,
                source="cfbenchmarks",
                order_id="live-15",
                fill_status="resting",
            )
        ],
    )
    save_state = tmp_path / "fifteen_state.json"
    save_state.write_text(
        json.dumps(
            {
                "tickets": [
                    {
                        "status": "open",
                        "loop": "fifteen",
                        "ticker": ticker,
                        "side": "Yes",
                        "contracts": 2,
                        "order_id": "live-15",
                    }
                ],
                "rests": [],
            }
        )
    )

    monkeypatch.setattr(
        "src.fifteen.main.collect_ideas", lambda *a, **k: ([], ["sit"], None)
    )
    monkeypatch.setattr(
        "src.fifteen.main._client",
        lambda settings: _quiet_scan_client(
            fills=[{"ticker": ticker}],
            market={"result": "yes", "status": "determined"},
        ),
    )
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.fifteen.main.manage_open_positions",
        lambda *a, **k: {"signals": [], "placed": [], "errors": [], "dry_run": [], "journal": []},
    )
    from src.fifteen.main import run_scan

    settings = _fifteen_settings(tmp_path)
    assert run_scan(settings, asset=None, place=True, force_live=False) == 0
    rows = load_trades(tmp_path / "fifteen_trade_log.jsonl")
    assert len(rows) == 1
    assert rows[0]["fill_status"] == "filled"
    assert rows[0]["result"] == "win"
    assert rows[0]["settlement_result"] == "yes"
    assert rows[0]["pnl"] == pytest.approx(0.92)
    assert load_trades(tmp_path / "fifteen_paper_log.jsonl") == []
    pot = load_pot(tmp_path / "fifteen_pot.json")
    assert pot.realized_pnl == pytest.approx(0.92)
    assert pot.balance == pytest.approx(5.92)
    state = json.loads(save_state.read_text())
    assert state["tickets"][0]["status"] == "settled"
    assert state["tickets"][0]["result"] == "win"


def test_journal_no_sell_yes_risk_matches_economic_fill_cost(tmp_path):
    """Sell-Yes fill cost is complement × contracts, not the cheap No label."""
    idea = replace(
        _eth_idea(),
        side="No",
        entry_price=0.76,
        limit_price=0.76,
        contracts=1,
        risk_dollars=0.76,
    )
    settings = _fifteen_settings(tmp_path)
    result = {
        "placed": [
            {
                "order_id": "eth-no-1",
                "ticker": idea.market.ticker,
                "client_order_id": "cid-no",
                "side": "ask",
                "yes_price_dollars": "0.22",
                "average_fill_price": "0.22",
                "fill_count_fp": "1.00",
                "remaining_count_fp": "0.00",
                "maker_fill_cost_dollars": "0.78",
            }
        ],
        "orders": [
            {
                "ticker": idea.market.ticker,
                "client_order_id": "cid-no",
                "count": "1.00",
                "price": "0.2200",
                "side": "ask",
            }
        ],
    }
    written = journal_live_places(
        settings, ideas=[idea], result=result, spots=_spots(), state={"tickets": []}
    )
    assert len(written) == 1
    row = written[0]
    assert row["side"] == "No"
    assert row["contracts"] == 1
    assert row["risk_dollars"] == economic_risk_dollars(1, 0.78)
    assert row["risk_dollars"] == pytest.approx(0.78)
    assert row["risk_dollars"] == maker_cost_per_contract("No", yes_book=0.22)


def test_journal_live_places_two_v2_orders_write_two_rows(tmp_path):
    btc = _idea()
    eth = _eth_idea()
    settings = _fifteen_settings(tmp_path)
    result = {
        "placed": [
            {
                "order_id": "btc-1",
                "client_order_id": "cid-btc",
                "fill_count": "0.00",
                "remaining_count": "2.00",
            },
            {
                "order_id": "eth-1",
                "client_order_id": "cid-eth",
                "fill_count_fp": "2.00",
                "remaining_count_fp": "0.00",
            },
        ],
        "orders": [
            {
                "ticker": btc.market.ticker,
                "client_order_id": "cid-btc",
                "count": "2.00",
                "price": "0.5400",
                "side": "bid",
            },
            {
                "ticker": eth.market.ticker,
                "client_order_id": "cid-eth",
                "count": "2.00",
                "price": "0.5400",
                "side": "bid",
            },
        ],
    }
    written = journal_live_places(
        settings, ideas=[btc, eth], result=result, spots=_spots(), state={"tickets": []}
    )
    assert [row["ticker"] for row in written] == [btc.market.ticker, eth.market.ticker]
    assert [row["order_id"] for row in written] == ["btc-1", "eth-1"]
    assert written[0]["side"] == "Yes"
    assert written[1]["side"] == "Yes"
    assert written[0]["fill_status"] == "resting"
    assert written[1]["fill_status"] == "filled"
    assert written[1]["filled_contracts"] == 2.0
    assert all(row["result"] == "pending" for row in written)
    assert all(row.get("kind") != "paper" for row in written)
    rows = load_trades(tmp_path / "fifteen_trade_log.jsonl")
    assert len(rows) == 2


def test_journal_live_places_ticker_key_mismatch_still_journals(tmp_path, capsys):
    btc = _idea()
    eth = _eth_idea()
    settings = _fifteen_settings(tmp_path)
    result = {
        "placed": [
            {
                "order_id": "orphan-1",
                "market_ticker": "KXBTC15M-26SEP070630-30",
                "fill_count_fp": "2.00",
                "remaining_count_fp": "0.00",
                "outcome_side": "yes",
                "yes_price_dollars": "0.54",
            }
        ],
        "orders": [],
    }
    written = journal_live_places(
        settings, ideas=[btc, eth], result=result, spots=_spots(), state={"tickets": []}
    )
    assert len(written) == 1
    row = written[0]
    assert row["order_id"] == "orphan-1"
    assert row["ticker"] == "KXBTC15M-26SEP070630-30"
    assert row["asset"] == "BTC"
    assert row["side"] == "Yes"
    assert row["fill_status"] == "filled"
    assert row["filled_contracts"] == 2.0
    assert row["result"] == "pending"
    assert "no idea matched" in capsys.readouterr().out


def test_journal_live_places_new_order_id_not_blocked_by_pending_ticker(tmp_path):
    btc = _idea()
    settings = _fifteen_settings(tmp_path)
    prior = new_trade_row(
        ticker=btc.market.ticker,
        asset="BTC",
        side="Yes",
        strike=64000.0,
        spot=65000.0,
        minutes_left=12.0,
        fair=0.62,
        kalshi_price=0.54,
        limit_price=0.54,
        contracts=2,
        risk_dollars=1.08,
        hourly_vol=0.004,
        source="cfbenchmarks",
        order_id="old-order",
        fill_status="resting",
    )
    write_trades(tmp_path / "fifteen_trade_log.jsonl", [prior])
    result = {
        "placed": [
            {
                "order_id": "fresh-order",
                "ticker": btc.market.ticker,
                "fill_count": "0.00",
                "remaining_count": "2.00",
                "client_order_id": "cid-new",
            }
        ],
        "orders": [{"ticker": btc.market.ticker, "client_order_id": "cid-new"}],
    }
    written = journal_live_places(
        settings, ideas=[btc], result=result, spots=_spots(), state={"tickets": []}
    )
    assert len(written) == 1
    assert written[0]["order_id"] == "fresh-order"
    rows = load_trades(tmp_path / "fifteen_trade_log.jsonl")
    assert [row["order_id"] for row in rows] == ["old-order", "fresh-order"]


def test_run_scan_paper_does_not_write_live_journal(monkeypatch, tmp_path):
    idea = _idea()

    class Spots:
        prices = {"BTC": 65000.0}
        hourly_vol = {"BTC": 0.004}
        sources = {"BTC": "cfbenchmarks"}
        source = "cfbenchmarks"

        def settlement_ok(self, _asset):
            return True

    def fake_collect(*args, **kwargs):
        return [idea], [], Spots()

    monkeypatch.setattr("src.fifteen.main.collect_ideas", fake_collect)
    monkeypatch.setattr("src.fifteen.main._client", lambda settings: _quiet_scan_client(can_trade=False))
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    from src.fifteen.main import run_scan

    settings = _fifteen_settings(tmp_path)
    assert run_scan(settings, asset=None, place=False, force_live=False) == 0
    paper = load_trades(tmp_path / "fifteen_paper_log.jsonl")
    assert len(paper) == 1
    assert paper[0]["kind"] == "paper"
    assert paper[0]["fill_status"] == "assumed-maker-fill"
    assert paper[0]["shadow"] == "scan"
    assert load_trades(tmp_path / "fifteen_trade_log.jsonl") == []


def test_paper_ideas_match_live_ideas_for_same_tick_inputs(monkeypatch):
    from tests.test_regime import trending_ohlc

    now = _et(10, 3)
    market = _pass_market(now)
    _patch_collect(monkeypatch, trending_ohlc(), market)
    settings = FifteenSettings(_env_file=None, chop_veto=True, require_settlement_index=True)
    kwargs = dict(
        client=MagicMock(),
        state={"tickets": [], "rests": []},
        pot_room=5.0,
        bankroll=5.0,
        now=now,
        apply_chop_veto=True,
    )
    live_ideas, _, _ = collect_ideas(settings, **kwargs)
    paper_ideas, _, _ = collect_ideas(settings, **kwargs)
    live_keys = [idea_fingerprint(idea) for idea in live_ideas]
    paper_keys = [idea_fingerprint(idea) for idea in paper_ideas]
    assert live_keys == paper_keys
    assert live_keys
    shadowed = paper_ideas_for_window(
        live_ideas, force_live=True, place=True, live_decided=False
    )
    classic = paper_ideas_for_window(
        paper_ideas, force_live=False, place=False, live_decided=False
    )
    assert [idea_fingerprint(idea) for idea in shadowed] == live_keys
    assert [idea_fingerprint(idea) for idea in classic] == live_keys
    later = paper_ideas_for_window(
        [_idea_named("KXBTC15M-LATER-T64000")],
        force_live=False,
        place=False,
        live_decided=True,
    )
    assert later == []
    assert paper_ideas_for_window([], force_live=True, place=True, live_decided=False) == []
    assert paper_ideas_for_window(
        live_ideas, force_live=False, place=True, live_decided=False
    ) == []


def test_later_scan_does_not_paper_a_different_pass(monkeypatch, tmp_path):
    live_idea = _idea()
    later_idea = _idea_named("KXBTC15M-LATER-T64000")
    calls = {"n": 0}

    def fake_collect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [live_idea], [], _spots()
        return [later_idea], [], _spots()

    def fake_execute(*args, **kwargs):
        return {
            "placed": [
                {
                    "order_id": "live-15",
                    "ticker": live_idea.market.ticker,
                    "fill_count": "0.00",
                    "remaining_count": "2.00",
                    "client_order_id": "cid-15",
                }
            ],
            "orders": [{"ticker": live_idea.market.ticker, "client_order_id": "cid-15"}],
            "errors": [],
        }

    monkeypatch.setattr("src.fifteen.main.collect_ideas", fake_collect)
    monkeypatch.setattr("src.fifteen.main.execute_ideas", fake_execute)
    monkeypatch.setattr("src.fifteen.main._client", lambda settings: _quiet_scan_client())
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.fifteen.main.manage_open_positions",
        lambda *a, **k: {"signals": [], "placed": [], "errors": [], "dry_run": [], "journal": []},
    )
    from src.fifteen.main import run_scan

    settings = _fifteen_settings(tmp_path)
    assert run_scan(settings, asset=None, place=True, force_live=True, armed=True) == 0
    assert run_scan(settings, asset=None, place=False, force_live=False) == 0
    paper = load_trades(tmp_path / "fifteen_paper_log.jsonl")
    live = load_trades(tmp_path / "fifteen_trade_log.jsonl")
    assert [row["ticker"] for row in live] == [live_idea.market.ticker]
    assert [row["ticker"] for row in paper] == [live_idea.market.ticker]
    assert later_idea.market.ticker not in {row["ticker"] for row in paper}


def test_live_sit_blocks_later_scan_paper(monkeypatch, tmp_path):
    later_idea = _idea()
    calls = {"n": 0}

    def fake_collect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [], ["sit"], _spots()
        return [later_idea], [], _spots()

    monkeypatch.setattr("src.fifteen.main.collect_ideas", fake_collect)
    monkeypatch.setattr("src.fifteen.main._client", lambda settings: _quiet_scan_client())
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.fifteen.main.manage_open_positions",
        lambda *a, **k: {"signals": [], "placed": [], "errors": [], "dry_run": [], "journal": []},
    )
    from src.fifteen.main import run_scan

    settings = _fifteen_settings(tmp_path)
    assert run_scan(settings, asset=None, place=True, force_live=True, armed=True) == 0
    assert run_scan(settings, asset=None, place=False, force_live=False) == 0
    assert load_trades(tmp_path / "fifteen_paper_log.jsonl") == []
    assert load_trades(tmp_path / "fifteen_trade_log.jsonl") == []
    state = json.loads((tmp_path / "fifteen_state.json").read_text())
    assert state["live_decision"]["n"] == 0
    assert state["live_decision"]["tickers"] == []


def test_live_decision_stamp_matches_window():
    state: dict = {}
    idea = _idea()
    wid = fifteen_window_id(_et(10, 3))
    stamp = stamp_live_decision(state, window_id=wid, ideas=[idea])
    assert stamp["tickers"] == [idea.market.ticker]
    assert live_decision_for_window(state, window_id=wid) is stamp
    assert live_decision_for_window(state, window_id=fifteen_window_id(_et(10, 17))) is None
