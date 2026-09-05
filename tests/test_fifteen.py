"""Dedicated 15m BTC/ETH bot: settlement id, skips, pot, gates, maker, series."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.cfindex import fifteen_index_id_for, index_id_for
from src.clock import fifteen_window_key
from src.config import EXIT_CONFIG
from src.executor import execute_ideas, is_fifteen_rest, is_hourly_rest
from src.fifteen import HALTED_MESSAGE, live_is_armed, main, parse_total_value
from src.fifteen_config import FifteenSettings
from src.fifteen_filters import (
    FifteenFilterConfig,
    classify_phase,
    evaluate_fifteen_market,
    live_mid,
    model_near_mid,
    should_stop_ticket,
    should_take_profit,
    spread_wider_than_edge,
)
from src.fifteen_pot import apply_pnl, empty_pot, load_pot, pot_should_halt, remaining_room
from src.filters import Idea, maker_limit
from src.markets import (
    FIFTEEN_SERIES,
    HourlyMarket,
    MarketDiscovery,
    fifteen_market_from_api,
    is_btc_eth_fifteen_series,
)

ET = ZoneInfo("America/New_York")


def _close(minutes: float = 13) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _market(**kwargs) -> HourlyMarket:
    defaults = dict(
        ticker="KXBTC15M-26SEP051200-T78099.99",
        event_ticker="KXBTC15M-26SEP051200",
        series_ticker="KXBTC15M",
        asset="BTC",
        title="BTC 15m up/down",
        yes_sub_title="Up",
        threshold=78099.99,
        strike_type="greater",
        close_time=_close(13),
        status="active",
        yes_bid=0.40,
        yes_ask=0.42,
        no_bid=0.58,
        no_ask=0.60,
        yes_bid_size=20,
        yes_ask_size=20,
        no_bid_size=20,
        no_ask_size=20,
        rules_primary="CF Benchmarks BRTI 60-second average.",
        rules_secondary="",
        settlement_source="CF Benchmarks BRTI",
        exchange_index=2,
    )
    defaults.update(kwargs)
    return HourlyMarket(**defaults)


def _edge_cfg(**kwargs) -> FifteenFilterConfig:
    defaults = dict(
        mid_tolerance=0.04,
        min_minutes_left=8.0,
        edge_loop_min_into=0.0,
        edge_loop_max_into=20.0,
        last_minute_maker=False,
        require_settlement_index=True,
        require_maker=True,
        pot_room=5.0,
        shard2_cash=5.0,
        bankroll=40.0,
        preferred_risk_dollars=0.85,
        max_risk_dollars=1.50,
        min_risk_dollars=0.10,
    )
    defaults.update(kwargs)
    return FifteenFilterConfig(**defaults)


def test_hourly_eth_index_stays_erti():
    assert index_id_for("ETH") == "ERTI"
    assert index_id_for("BTC") == "BRTI"


def test_fifteen_eth_index_is_ethusd_rti_never_erti():
    assert fifteen_index_id_for("ETH") == "ETHUSD_RTI"
    assert fifteen_index_id_for("ETH") != "ERTI"
    assert fifteen_index_id_for("BTC") == "BRTI"


def test_series_filter_excludes_hourly():
    assert is_btc_eth_fifteen_series("KXBTC15M")
    assert is_btc_eth_fifteen_series("KXETH15M")
    assert not is_btc_eth_fifteen_series("KXBTCD")
    assert not is_btc_eth_fifteen_series("KXETHD")
    assert not is_fifteen_rest({"ticker": "KXBTCD-26SEP0512-T78099.99"})
    assert is_hourly_rest({"ticker": "KXBTCD-26SEP0512-T78099.99"})
    assert is_fifteen_rest({"ticker": "KXBTC15M-26SEP051200"})
    hourly = fifteen_market_from_api(
        {
            "ticker": "KXBTCD-26SEP0512-T78099.99",
            "series_ticker": "KXBTCD",
            "status": "active",
            "close_time": (datetime.now(timezone.utc) + timedelta(minutes=12)).isoformat(),
            "floor_strike": 78099.99,
            "strike_type": "greater",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.42",
        },
        {"event_ticker": "KXBTCD-26SEP0512", "series_ticker": "KXBTCD", "title": "BTC hourly"},
    )
    assert hourly is None


def test_discover_fifteen_does_not_request_hourly_series():
    requested: list[str] = []

    class Client:
        def open_events(self, series, limit=20):
            requested.append(str(series))
            if str(series) in {"KXBTCD", "KXETHD"}:
                raise AssertionError("hourly series must not be requested")
            return []

    found = MarketDiscovery(Client()).discover_fifteen(["BTC", "ETH"])
    assert found == []
    assert set(requested) <= set(FIFTEEN_SERIES)


def test_hard_skip_under_8m_unless_last_minute():
    now = datetime(2026, 9, 5, 16, 8, tzinfo=ET)  # 7m left in 16:00–16:15
    cfg = FifteenFilterConfig(min_minutes_left=8, last_minute_maker=False)
    phase = classify_phase(now, cfg)
    assert phase.allow_edge is False
    assert "under 8" in phase.skip_reason

    last = FifteenFilterConfig(min_minutes_left=8, last_minute_maker=True, last_minute_minutes=3)
    late = datetime(2026, 9, 5, 16, 13, tzinfo=ET)
    phase_last = classify_phase(late, last)
    assert phase_last.allow_last_minute is True


def test_hard_skip_spread_wider_than_edge_and_near_mid():
    assert spread_wider_than_edge(0.06, 0.03) is True
    assert spread_wider_than_edge(0.02, 0.05) is False
    assert model_near_mid(0.51, 0.50, 0.04) is True
    assert model_near_mid(0.60, 0.50, 0.04) is False
    assert live_mid(0.40, 0.42) == 0.41

    market = _market(yes_bid=0.48, yes_ask=0.52, no_bid=0.48, no_ask=0.52, threshold=78100.0)
    result = evaluate_fifteen_market(
        market,
        spot=78100.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=_edge_cfg(),
        settlement_index=True,
    )
    assert result.idea is None
    assert any("within" in r and "mid" in r for r in result.avoid_reasons)


def test_hard_skip_news_revenge_three_losses():
    market = _market(threshold=77000.0, yes_bid=0.20, yes_ask=0.22, no_bid=0.78, no_ask=0.80)
    now = datetime.now(timezone.utc)
    news = evaluate_fifteen_market(
        market, spot=78120.0, hourly_vol=0.010, now=now, cfg=_edge_cfg(vol_pause_mult=2.0), vol_fallback=0.004
    )
    assert news.idea is None
    assert any("news candle" in r.lower() or "vol" in r.lower() for r in news.avoid_reasons)

    revenge = evaluate_fifteen_market(
        market, spot=78120.0, hourly_vol=0.004, now=now, cfg=_edge_cfg(revenge=True), vol_fallback=0.004
    )
    assert revenge.idea is None
    assert any("revenge" in r.lower() for r in revenge.avoid_reasons)

    three = evaluate_fifteen_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=now,
        cfg=_edge_cfg(daily_losses=3, max_daily_losses=3),
        vol_fallback=0.004,
    )
    assert three.idea is None
    assert any("three 15m losses" in r for r in three.avoid_reasons)


def test_one_idea_per_window():
    market = _market(threshold=77000.0, yes_bid=0.20, yes_ask=0.22, no_bid=0.78, no_ask=0.80)
    result = evaluate_fifteen_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=_edge_cfg(idea_this_window=True),
        vol_fallback=0.004,
    )
    assert result.idea is None
    assert any("one idea per 15m window" in r for r in result.avoid_reasons)


def test_proxy_index_sits_the_coin():
    market = _market(threshold=77000.0, yes_bid=0.20, yes_ask=0.22, no_bid=0.78, no_ask=0.80)
    result = evaluate_fifteen_market(
        market,
        spot=78120.0,
        hourly_vol=0.004,
        now=datetime.now(timezone.utc),
        cfg=_edge_cfg(),
        settlement_index=False,
    )
    assert result.idea is None
    assert any("ETHUSD_RTI" in r or "proxy" in r.lower() for r in result.avoid_reasons)


def test_pot_empty_sets_halted(tmp_path: Path):
    path = tmp_path / "fifteen_pot.json"
    pot = load_pot(path, start=5.0, ask=10.0)
    assert pot["pot"] == 5.0
    assert pot_should_halt(pot) is False
    apply_pnl(pot, -5.0)
    assert pot["pot"] == 0.0
    assert pot["halted"] is True
    assert pot_should_halt(pot) is True
    assert remaining_room(pot) == 0.0
    apply_pnl(empty_pot(start=5, ask=10), 5.5)
    grown = empty_pot(start=5, ask=10)
    apply_pnl(grown, 5.5)
    assert grown["pot"] == 10.5
    assert grown["halted"] is False
    assert grown["ask_notified"] is True


def test_dual_live_gates_and_confirm_live_cannot_override_halted(monkeypatch, capsys):
    monkeypatch.setenv("HALTED", "true")
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("CONFIRM_LIVE", "YES")
    monkeypatch.setattr("src.fifteen.load_fifteen_settings", lambda: FifteenSettings(_env_file=None, halted=True))
    monkeypatch.setattr("src.fifteen.probe_balance", lambda *a, **k: (True, {"balance": 40}))
    called = {"scan": False}

    def _scan(*args, **kwargs):
        called["scan"] = True
        return 0

    monkeypatch.setattr("src.fifteen.run_scan", _scan)
    assert main(["live", "--confirm", "LIVE"]) == EXIT_CONFIG
    assert called["scan"] is False
    assert "HALTED" in capsys.readouterr().err
    assert "cannot override" in HALTED_MESSAGE

    dry = FifteenSettings(_env_file=None, halted=False, live_trading=False, confirm_live="NO")
    assert live_is_armed(dry, confirm="LIVE", isatty=False) is False
    assert live_is_armed(dry, confirm="LIVE", isatty=True) is True
    env = FifteenSettings(_env_file=None, halted=False, live_trading=True, confirm_live="YES")
    assert live_is_armed(env, confirm="", isatty=False) is True
    one = FifteenSettings(_env_file=None, halted=False, live_trading=True, confirm_live="NO")
    assert live_is_armed(one, confirm="", isatty=False) is False
    halted = FifteenSettings(_env_file=None, halted=True, live_trading=True, confirm_live="YES")
    assert halted.live_enabled is False
    assert live_is_armed(halted, confirm="LIVE", isatty=False) is False


def test_halted_defaults_true_for_fifteen():
    settings = FifteenSettings(_env_file=None, kalshi_api_key_id="", kalshi_private_key_path="/tmp/not-pem")
    assert settings.halted is True
    assert settings.live_enabled is False
    assert settings.fifteen_pot_start == 5.0
    assert settings.max_risk_dollars == 1.50
    assert settings.series == "KXBTC15M,KXETH15M"
    assert settings.exchange_index == 2
    assert settings.require_maker is True
    assert settings.require_settlement_index is True


def _idea() -> Idea:
    return Idea(
        market=_market(),
        side="Yes",
        entry_price=0.42,
        limit_price=0.41,
        fair=0.55,
        gross_edge=0.10,
        net_edge=0.08,
        fee_per_contract=0.01,
        fee_total=0.01,
        z=0.2,
        hours_left=0.2,
        contracts=2,
        risk_dollars=0.84,
        max_loss=0.84,
        rationale=["unit"],
        post_maker=True,
    )


def test_post_only_never_crosses_and_does_not_cancel_hourly(tmp_path: Path):
    assert maker_limit("Yes", 0.40, 0.42) == 0.41
    assert maker_limit("Yes", 0.41, 0.42) == 0.41
    idea = _idea()
    idea.post_maker = False
    client = MagicMock()
    out = execute_ideas(
        [idea],
        client=client,
        artifacts_dir=tmp_path,
        live=True,
        confirm_live=True,
        rest_matcher=is_fifteen_rest,
        exchange_index=2,
    )
    client.create_order.assert_not_called()
    assert any("refused to cross" in err for err in out["errors"])

    client = MagicMock()
    client.get_orders.return_value = [
        {"order_id": "hourly-rest", "ticker": "KXBTCD-26SEP0512-T78099.99"},
        {"order_id": "fifteen-rest", "ticker": "KXETH15M-26SEP051200"},
    ]
    client.create_order.return_value = {"order": {"order_id": "new-15", "fill_count": "0.00"}}
    out = execute_ideas(
        [_idea()],
        client=client,
        artifacts_dir=tmp_path,
        live=True,
        confirm_live=True,
        rest_matcher=is_fifteen_rest,
        exchange_index=2,
    )
    canceled = {row["order_id"] for row in out["canceled"]}
    assert canceled == {"fifteen-rest"}
    assert out["orders"][0]["post_only"] is True
    assert out["orders"][0]["exchange_index"] == 2


def test_ticket_stop_and_tp_helpers():
    assert should_take_profit(fill_price=0.80, bid=0.82) is True
    assert should_take_profit(fill_price=0.80, bid=0.99) is True
    assert should_take_profit(fill_price=0.80, bid=0.81) is False
    assert should_stop_ticket(fill_price=0.80, mark=0.70, risk_dollars=1.00) is True
    assert should_stop_ticket(fill_price=0.80, mark=0.79, risk_dollars=1.00) is False


def test_parse_total_value_and_window_key():
    assert parse_total_value({"total_value": 12.5}) == 12.5
    assert parse_total_value({"balance": 2500}) == 25.0
    now = datetime(2026, 9, 5, 16, 7, tzinfo=ET)
    assert fifteen_window_key(now).startswith("2026-09-05T16:00:00")


def test_kb_fifteen_dispatch(monkeypatch):
    monkeypatch.setattr("src.fifteen.main", lambda argv: 0 if argv == ["scan"] else 7)
    from src.main import main as hourly_main

    assert hourly_main(["fifteen", "scan"]) == 0
