"""15m BTC/ETH edge-loop: windows, pass/fail, pot, gates, cancel isolation."""

from __future__ import annotations

import json
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
from src.fifteen.main import collect_ideas, live_is_armed, main, normalize_argv
from src.fifteen.pot import credit_pot, load_pot, save_pot, set_open_risk
from src.journal import (
    FILL_BACKFILL_SOURCE,
    KIND_BACKFILL,
    late_place_rate,
    load_trades,
    new_trade_row,
    summarize_entry_timing,
    write_trades,
)
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


def test_entry_window_is_minutes_two_to_four():
    assert in_fifteen_entry_window(_et(10, 2))
    assert in_fifteen_entry_window(_et(10, 3))
    assert in_fifteen_entry_window(_et(10, 4))
    assert in_fifteen_entry_window(_et(10, 17))
    assert in_fifteen_entry_window(_et(10, 32))
    assert in_fifteen_entry_window(_et(10, 49))
    assert not in_fifteen_entry_window(_et(10, 0))
    assert not in_fifteen_entry_window(_et(10, 1))
    assert not in_fifteen_entry_window(_et(10, 5))
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
    markets = [market, *(extra_markets or [])]

    class Discovery:
        def __init__(self, client):
            self.client = client

        def discover_fifteen(self, assets, **kwargs):
            want = {str(name).upper() for name in assets}
            return [row for row in markets if row.asset in want]

    monkeypatch.setattr("src.fifteen.main.MarketDiscovery", Discovery)


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


def test_run_scan_live_journals_place_not_paper(monkeypatch, tmp_path):
    idea = _idea()

    def fake_collect(*args, **kwargs):
        return [idea], [], None

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
    assert load_trades(tmp_path / "fifteen_paper_log.jsonl") == []
    state = json.loads((tmp_path / "fifteen_state.json").read_text())
    assert state["tickets"][0]["ticker"] == idea.market.ticker
    assert state["tickets"][0]["order_id"] == "live-15"


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


def test_refresh_live_journal_writes_kind_backfill_for_unmatched_fill(tmp_path):
    from src.fifteen.main import refresh_live_journal
    from src.fifteen.pot import load_pot

    fill = {
        "ticker": "KXBTC15M-26SEP071015-T64000",
        "order_id": "ord-residual",
        "fill_id": "fill-residual",
        "side": "yes",
        "action": "sell",
        "count": "1.00",
        "yes_price": "0.0100",
        "created_time": "2026-09-07T14:14:00Z",
    }
    settings = _fifteen_settings(tmp_path)
    client = _quiet_scan_client(fills=[fill], market={"status": "active"})
    pot = load_pot(settings.pot_path)
    state: dict = {"tickets": [], "rests": []}
    refresh_live_journal(settings, client=client, state=state, pot=pot)
    rows = load_trades(tmp_path / "fifteen_trade_log.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == KIND_BACKFILL
    assert row["backfill"] is True
    assert row["spot_source"] == FILL_BACKFILL_SOURCE
    assert row["spot"] == 0.0
    assert row["strike"] == 0.0
    assert row["hourly_vol"] == 0.0
    assert row["fill_ts_iso"]
    assert row["order_id"] == "ord-residual"
    hourly = {
        "ticker": "KXBTCD-26SEP0710-T64000",
        "order_id": "hourly-1",
        "side": "yes",
        "count": "2.00",
        "yes_price": "0.40",
        "created_time": "2026-09-07T14:03:00Z",
    }
    client = _quiet_scan_client(fills=[fill, hourly], market={"status": "active"})
    refresh_live_journal(settings, client=client, state=state, pot=pot)
    assert len(load_trades(tmp_path / "fifteen_trade_log.jsonl")) == 1


def test_backfill_settles_in_journal_but_not_on_score_or_pot(tmp_path):
    from src.evaluate import summarize_trades
    from src.fifteen.main import refresh_live_journal
    from src.fifteen.edge import fifteen_session_date
    from src.fifteen.pot import load_pot
    from src.clock import to_et

    fill = {
        "ticker": "KXBTC15M-26SEP071015-T64000",
        "order_id": "ord-residual",
        "fill_id": "fill-residual",
        "side": "yes",
        "action": "sell",
        "count": "0.00",
        "count_fp": "2.00",
        "yes_price": "0.0100",
        "created_time": "2026-09-07T14:14:00Z",
    }
    settings = _fifteen_settings(tmp_path)
    client = _quiet_scan_client(
        fills=[fill],
        market={"result": "yes", "status": "determined"},
    )
    pot = load_pot(settings.pot_path)
    state = {
        "tickets": [],
        "rests": [],
        "fifteen_loss_streak": 2,
        "fifteen_session_date": fifteen_session_date(to_et()),
    }
    refresh_live_journal(settings, client=client, state=state, pot=pot)
    rows = load_trades(tmp_path / "fifteen_trade_log.jsonl")
    assert rows[0]["kind"] == KIND_BACKFILL
    assert rows[0]["contracts"] == 2
    assert rows[0]["filled_contracts"] == 2.0
    assert rows[0]["risk_dollars"] == 0.02
    assert rows[0]["result"] == "win"
    assert rows[0]["pnl"] == pytest.approx(1.98)
    assert pot.realized_pnl == pytest.approx(0.0)
    assert pot.balance == pytest.approx(5.0)
    assert int(state.get("fifteen_loss_streak") or 0) == 2
    scored = summarize_trades(rows)
    assert scored["n_backfills"] == 1
    assert scored["n_filled_settled"] == 0
    assert scored["n_wins"] == 0
    assert scored["pnl"] == pytest.approx(0.0)
    timing = summarize_entry_timing(rows)
    assert timing["n"] == 0
    assert timing["n_backfills"] == 1
    assert timing["late_place_rate"] == 0.0
    assert late_place_rate(rows) == 0.0


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
    assert load_trades(tmp_path / "fifteen_trade_log.jsonl") == []


def test_livescore_play_feed_skips_backfill_rows(monkeypatch, tmp_path, capsys):
    from src.fifteen.main import run_eval
    from src.journal import new_backfill_row

    live = new_trade_row(
        ticker="KXBTC15M-PLAY",
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
        order_id="live-play",
        fill_status="filled",
    )
    live["result"] = "win"
    live["pnl"] = 0.92
    backfill = new_backfill_row(
        fill={
            "ticker": "KXBTC15M-RECON",
            "order_id": "ord-recon",
            "fill_id": "fill-recon",
            "side": "yes",
            "count": "0.00",
            "count_fp": "2.00",
            "yes_price": "0.0100",
            "created_time": "2026-09-07T14:14:00Z",
        }
    )
    backfill["result"] = "loss"
    backfill["pnl"] = -0.0
    backfill["fill_status"] = "filled"
    write_trades(tmp_path / "fifteen_trade_log.jsonl", [live, backfill])
    monkeypatch.setattr("src.fifteen.main.try_settle_paper", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.fifteen.main._client",
        lambda settings: _quiet_scan_client(can_trade=False, market={"status": "active"}),
    )
    settings = _fifteen_settings(tmp_path)
    assert run_eval(settings) == 0
    out = capsys.readouterr().out
    assert "KXBTC15M-PLAY" in out
    assert "KXBTC15M-RECON" not in out
    assert "live filled PnL: $0.92" in out
    assert "PLAY streak: 1W" in out
    assert "kind=backfill recon rows from score" in out
    assert "play-only pot $5.92" in out
    assert "PLAY feed (live Passes only)" in out
