"""Paper journal: dry-scan tape, PROXY sit, BRTI/ERTI settle. Never live."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.cfindex import average_settlement_window, official_yes, parse_cf_history_ticks
from src.config import HourlySettings
from src.evaluate import format_eval_report, summarize_scans, summarize_trades
from src.filters import FilterResult, Idea
from src.main import run_scan
from src.journal import estimate_pnl, load_trades, new_trade_row
from src.markets import HourlyMarket
from src.paper import (
    FILL_ASSUMED_MAKER,
    FILL_SIT_UNSCORED,
    FILL_UNFILLED,
    LIVE_TRADE_LOG_NAME,
    append_paper_ticket,
    assert_paper_path,
    fetch_official_print,
    new_paper_row,
    paper_pnl,
    paper_row_from_idea,
    paper_won,
    record_printed_ideas,
    resolve_paper,
    settle_paper_row,
    summarize_paper,
)


def _close(hours: float = -0.1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _market(**kwargs) -> HourlyMarket:
    defaults = dict(
        ticker="KXBTCD-26SEP0314-T77249.99",
        event_ticker="KXBTCD-26SEP0314",
        series_ticker="KXBTCD",
        asset="BTC",
        title="BTC price",
        yes_sub_title="$77,250 or above",
        threshold=77249.99,
        strike_type="greater",
        close_time=_close(0.4),
        status="active",
        yes_bid=0.40,
        yes_ask=0.42,
        no_bid=0.58,
        no_ask=0.60,
        yes_bid_size=20,
        yes_ask_size=20,
        no_bid_size=20,
        no_ask_size=20,
        rules_primary="CF Benchmarks BRTI average.",
        rules_secondary="",
        settlement_source="CF Benchmarks BRTI",
        exchange_index=2,
    )
    defaults.update(kwargs)
    return HourlyMarket(**defaults)


def _idea(**kwargs) -> Idea:
    market = kwargs.pop("market", None) or _market()
    defaults = dict(
        market=market,
        side="No",
        entry_price=0.38,
        limit_price=0.37,
        fair=0.62,
        gross_edge=0.24,
        net_edge=0.09,
        fee_per_contract=0.016,
        fee_total=0.08,
        z=1.8,
        hours_left=0.4,
        contracts=4,
        risk_dollars=1.48,
        max_loss=1.56,
        rationale=["test"],
        post_maker=True,
        strike_distance_pct=0.012,
        spot=78100.0,
        minutes_left=24.0,
        bucket="far_no",
    )
    defaults.update(kwargs)
    return Idea(**defaults)


def _ticket(**kwargs) -> dict:
    row = new_paper_row(
        ticker=kwargs.pop("ticker", "KXBTCD-26SEP0314-T77249.99"),
        asset=kwargs.pop("asset", "BTC"),
        side=kwargs.pop("side", "No"),
        strike=kwargs.pop("strike", 77249.99),
        spot=kwargs.pop("spot", 78100.0),
        spot_source=kwargs.pop("spot_source", "cfbenchmarks"),
        minutes_left=kwargs.pop("minutes_left", 24.0),
        fair=kwargs.pop("fair", 0.62),
        kalshi_price=kwargs.pop("kalshi_price", 0.38),
        limit_price=kwargs.pop("limit_price", 0.37),
        contracts=kwargs.pop("contracts", 4),
        risk_dollars=kwargs.pop("risk_dollars", 1.48),
        net_edge=kwargs.pop("net_edge", 0.09),
        close_time=kwargs.pop("close_time", _close(-0.1)),
        fill_model=kwargs.pop("fill_model", FILL_ASSUMED_MAKER),
    )
    row.update(kwargs)
    return row


def test_defaults_never_arm_live():
    settings = HourlySettings(
        _env_file=None,
        kalshi_api_key_id="",
        kalshi_private_key_path="/tmp/not-the-home-pem",
    )
    assert settings.halted is True
    assert settings.live_enabled is False
    assert settings.live_trading is False
    assert settings.force_near_rule is False
    assert settings.paper_fill_model == FILL_ASSUMED_MAKER
    assert Path(settings.paper_log_path).name != LIVE_TRADE_LOG_NAME


def test_assert_paper_path_refuses_live_journal(tmp_path):
    try:
        assert_paper_path(tmp_path / "trade_log.jsonl")
    except ValueError as exc:
        assert "trade_log.jsonl" in str(exc)
    else:
        raise AssertionError("expected refuse")


def test_append_paper_ticket_fields(tmp_path):
    path = tmp_path / "paper_log.jsonl"
    live = tmp_path / "trade_log.jsonl"
    idea = _idea()
    written = record_printed_ideas(
        path,
        [idea],
        sources={"BTC": "cfbenchmarks"},
        fill_model=FILL_ASSUMED_MAKER,
    )
    assert len(written) == 1
    row = written[0]
    assert row["kind"] == "paper"
    assert row["ticker"] == idea.market.ticker
    assert row["side"] == "No"
    assert row["strike"] == idea.market.threshold
    assert row["spot"] == idea.spot
    assert row["spot_source"] == "BRTI"
    assert row["minutes_left"] == idea.minutes_left
    assert row["fair"] == round(idea.fair, 4)
    assert row["kalshi_price"] == idea.entry_price
    assert row["limit_price"] == idea.limit_price
    assert row["size"] == idea.contracts
    assert row["risk_dollars"] == idea.risk_dollars
    assert row["net_edge"] == idea.net_edge
    assert row["fill_model"] == FILL_ASSUMED_MAKER
    assert row["result"] == "pending"
    assert "assumed-maker-fill" in row["note"]
    assert "not a real fill" in row["note"]
    assert row["forced"] is False
    assert row["turbo"] is False
    assert row["force_near_rule"] is False
    assert row["label"] == ""
    assert path.is_file()
    assert not live.exists()
    # Dedup: second scan of the same ticker does not double-count.
    again = record_printed_ideas(path, [idea], sources={"BTC": "cfbenchmarks"})
    assert again == []
    assert len(load_trades(path)) == 1


def test_proxy_and_missing_index_are_sit_unscored(tmp_path):
    path = tmp_path / "paper_log.jsonl"
    proxy = paper_row_from_idea(_idea(market=_market(asset="ETH", ticker="KXETHD-1")), spot_source="coinbase")
    missing = new_paper_row(
        ticker="KXBTCD-MISSING",
        asset="BTC",
        side="No",
        strike=77000.0,
        spot=78000.0,
        spot_source="unknown",
        minutes_left=20,
        fair=0.6,
        kalshi_price=0.4,
        limit_price=0.39,
        contracts=3,
        risk_dollars=1.17,
        net_edge=0.08,
        close_time=_close(-1),
    )
    append_paper_ticket(path, proxy)
    append_paper_ticket(path, missing)
    assert proxy["spot_source"] == "PROXY"
    assert proxy["fill_model"] == FILL_SIT_UNSCORED
    assert proxy["result"] == "sit"
    assert missing["fill_model"] == FILL_SIT_UNSCORED
    summary = summarize_paper(load_trades(path))
    assert summary["n_sit_unscored"] == 2
    assert summary["n_wins"] == 0
    assert summary["n_losses"] == 0
    assert summary["assumed_fill_pnl"] == 0.0
    assert summary["live"] is False
    # Settling must not promote a PROXY row into a scored fill.
    out = resolve_paper(load_trades(path), lambda asset, close: 79000.0)
    assert out[0]["result"] == "sit"
    assert out[0]["pnl"] in (None, 0.0)


def test_brti_erti_settlement_math():
    assert official_yes(settlement_print=77300.0, strike=77249.99) is True
    assert official_yes(settlement_print=77249.99, strike=77249.99) is False
    assert official_yes(settlement_print=77200.0, strike=77249.99) is False
    # BTC Yes wins only if BRTI 60s average finishes above the line.
    assert paper_won(side="Yes", settlement_print=77300.0, strike=77249.99) is True
    assert paper_won(side="No", settlement_print=77300.0, strike=77249.99) is False
    # ETH No wins at or below the ERTI print.
    assert paper_won(side="No", settlement_print=2399.0, strike=2399.99) is True
    assert paper_won(side="Yes", settlement_print=2399.0, strike=2399.99) is False

    btc = _ticket(asset="BTC", side="No", strike=77249.99, limit_price=0.37, contracts=4, risk_dollars=1.48)
    settle_paper_row(btc, settlement_print=77300.0)
    assert btc["settlement_result"] == "yes"
    assert btc["result"] == "loss"
    assert btc["pnl"] == paper_pnl(won=False, contracts=4, fill_price=0.37, risk_dollars=1.48)
    assert btc["pnl"] == estimate_pnl(won=False, contracts=4, entry_price=0.37, risk_dollars=1.48)

    eth = _ticket(
        ticker="KXETHD-26SEP0314-T2399.99",
        asset="ETH",
        side="No",
        strike=2399.99,
        spot=2410.0,
        spot_source="cfbenchmarks",
        limit_price=0.41,
        contracts=4,
        risk_dollars=1.64,
    )
    settle_paper_row(eth, settlement_print=2395.10)
    assert eth["spot_source"] == "ERTI"
    assert eth["settlement_result"] == "no"
    assert eth["result"] == "win"
    assert eth["pnl"] == paper_pnl(won=True, contracts=4, fill_price=0.41, risk_dollars=1.64)
    assert eth["fill_model"] == FILL_ASSUMED_MAKER


def test_settlement_uses_official_60s_average_not_coinbase():
    close = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
    ticks = []
    for second in range(60):
        when = close - timedelta(seconds=60 - second)
        ticks.append((when, 77000.0 + second))
    # A Coinbase-like last tick outside the window must not move the print.
    ticks.append((close + timedelta(seconds=1), 99999.0))
    ticks.append((close - timedelta(seconds=61), 1000.0))
    average = average_settlement_window(ticks, close)
    assert average is not None
    assert abs(average - (77000.0 + 59.0 / 2.0)) < 1e-6


def test_unsettled_tickets_stay_pending():
    future = _ticket(close_time=_close(2.0), result="pending", pnl=None)
    past_missing_print = _ticket(ticker="KXBTCD-OPEN", close_time=_close(-1.0))
    out = resolve_paper(
        [future, past_missing_print],
        lambda asset, close: None,
        now=datetime.now(timezone.utc),
    )
    assert out[0]["result"] == "pending"
    assert out[0]["pnl"] is None
    assert out[1]["result"] == "pending"
    assert out[1]["pnl"] is None


def test_stricter_unfilled_mode_is_not_scored():
    row = _ticket(fill_model=FILL_UNFILLED)
    assert row["result"] == "unfilled"
    assert row["fill_model"] == FILL_UNFILLED
    settle_paper_row(row, settlement_print=79000.0)
    assert row["result"] == "unfilled"
    summary = summarize_paper([row])
    assert summary["n_unfilled"] == 1
    assert summary["n_wins"] == 0
    assert summary["assumed_fill_pnl"] == 0.0


def test_eval_summary_separates_paper_from_live():
    paper_rows = [
        _ticket(ticker="P1", result="win", fill_model=FILL_ASSUMED_MAKER, pnl=0.63),
        _ticket(ticker="P2", result="loss", fill_model=FILL_ASSUMED_MAKER, pnl=-1.48),
        _ticket(ticker="P3", result="pending", fill_model=FILL_ASSUMED_MAKER, pnl=None),
        _ticket(ticker="P4", spot_source="coinbase"),
    ]
    paper = summarize_paper(paper_rows)
    assert paper["n_tickets"] == 4
    assert paper["n_wins"] == 1
    assert paper["n_losses"] == 1
    assert paper["assumed_fill_pnl"] == -0.85
    assert paper["n_pending"] == 1
    assert paper["n_sit_unscored"] == 1
    live = [
        new_trade_row(
            ticker="LIVE-1",
            asset="BTC",
            side="No",
            strike=77000,
            spot=78000,
            minutes_left=20,
            fair=0.6,
            kalshi_price=0.4,
            limit_price=0.39,
            contracts=2,
            risk_dollars=0.8,
            hourly_vol=0.004,
            source="cfbenchmarks",
            fill_status="filled",
        )
    ]
    live[0]["result"] = "win"
    live[0]["pnl"] = 1.2
    text = format_eval_report(
        trades=summarize_trades(live),
        scans=summarize_scans([]),
        historical={"n": 0, "still_actionable": 0, "rejected": [], "source": "", "limitations": []},
        paper=paper,
    )
    assert "Not live profitability" in text
    assert "assumed-maker-fill" in text
    assert "Sit / unscored" in text
    assert "Assumed-fill PnL: $-0.85" in text
    assert "paper_log.jsonl" in text
    assert "trade_log.jsonl" in text
    assert "Filled PnL: $1.20" in text
    assert "This is not live profitability" in text


def test_fetch_official_print_never_uses_coinbase():
    class Client:
        can_trade = True

        def get_cf_history(self, index_id, *, timestamp, timespan):
            assert index_id in {"BRTI", "ETHUSD_RTI", "ERTI"}
            close = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
            payload = [
                {"time": (close - timedelta(seconds=60 - i)).isoformat(), "value": str(2400 + i)}
                for i in range(60)
            ]
            return {"data": {"payload": payload}}

        def coinbase(self):
            raise AssertionError("must not touch Coinbase")

    close = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
    print_value = fetch_official_print(Client(), "ETH", close)
    assert print_value is not None
    assert abs(print_value - (2400 + 59 / 2)) < 1e-6


def test_dry_scan_writes_paper_row_not_live_journal(monkeypatch, tmp_path, capsys):
    idea = _idea()
    from src.spot import SpotSnapshot

    spots = SpotSnapshot(
        prices={"BTC": 78100.0},
        sources={"BTC": "cfbenchmarks"},
        source="cfbenchmarks",
        hourly_vol={"BTC": 0.004},
    )

    class FakeClient:
        can_trade = False

        def get_fills(self, limit=50):
            return []

        def get_market(self, ticker):
            return {}

        def close(self):
            pass

    class FakeSpots:
        def snapshot(self, *args, **kwargs):
            return spots

        def close(self):
            pass

    class FakeDiscovery:
        def discover(self, *args, **kwargs):
            return [idea.market]

        def next_settlements(self, markets):
            return []

    monkeypatch.setattr("src.main.KalshiClient", lambda *a, **k: FakeClient())
    monkeypatch.setattr("src.main.SpotService", lambda *a, **k: FakeSpots())
    monkeypatch.setattr("src.main.MarketDiscovery", lambda *a, **k: FakeDiscovery())
    monkeypatch.setattr(
        "src.main.evaluate_market",
        lambda *a, **k: FilterResult(market=idea.market, idea=idea),
    )
    monkeypatch.setattr("src.main.open_hourly_tickets", lambda *a, **k: [])
    monkeypatch.setattr("src.main.blocks_new_idea", lambda *a, **k: None)

    settings = HourlySettings(
        _env_file=None,
        artifacts_dir=str(tmp_path),
        state_path=str(tmp_path / "state.json"),
        paper_log_path=str(tmp_path / "paper_log.jsonl"),
        halted=True,
        live_trading=False,
        confirm_live="NO",
    )
    assert settings.live_enabled is False
    assert run_scan(settings, asset="BTC", place=False, force_live=False) == 0
    rows = load_trades(tmp_path / "paper_log.jsonl")
    assert len(rows) == 1
    assert rows[0]["fill_model"] == FILL_ASSUMED_MAKER
    assert rows[0]["spot_source"] == "BRTI"
    live_rows = load_trades(tmp_path / "trade_log.jsonl")
    assert live_rows == []
    assert all(row.get("kind") == "paper" for row in rows)
    out = capsys.readouterr().out
    assert "assumed-maker-fill" in out
    assert "not a real fill" in out


def test_parse_cf_history_ticks_kalshi_envelope():
    blob = {
        "data": {
            "payload": [
                {"id": "BRTI", "time": "2026-09-03T17:59:01Z", "value": "77343.72"},
                {"id": "BRTI", "time": "2026-09-03T17:59:02Z", "value": "77344.10"},
            ]
        }
    }
    ticks = parse_cf_history_ticks(blob)
    assert len(ticks) == 2
    assert ticks[0][1] == 77343.72
