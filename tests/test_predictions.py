from kalshibot.assets import identify_asset
from kalshibot.models import (
    digital_call_prob,
    parse_strike,
    price_threshold_prob,
    devig_probs,
)
from kalshibot.money import mid_price, parse_dollars
from kalshibot.scanner import Scanner, _executable_edge


def test_identify_crypto_and_commodities():
    btc = identify_asset("KXBTC15M", "BTC 15 min")
    assert btc is not None and btc.key == "BTC"
    gold = identify_asset("KXGOLD15M", "Gold 15-minute")
    assert gold is not None and gold.key == "GOLD"
    brent = identify_asset("KXBRENTMON", "Brent crude oil price")
    assert brent is not None and brent.key == "BRENT"
    shib = identify_asset("KXSHIBAD", "Shiba Inu price")
    assert shib is not None and shib.key == "SHIB"


def test_digital_call_known_cases():
    assert digital_call_prob(100, 50, 1.0, 0.2) > 0.95
    assert digital_call_prob(50, 100, 1.0, 0.2) < 0.05
    atm = digital_call_prob(100, 100, 0.01, 0.6)
    assert 0.45 < atm < 0.55


def test_parse_strike_from_custom_and_subtitle():
    spec = parse_strike(
        {
            "custom_strike": {"floor_strike": "70.99", "strike_type": "greater"},
            "strike_type": "greater",
            "yes_sub_title": "Above $70.99",
            "ticker": "KXBRENTMON-26AUG3117-T70.99",
        }
    )
    assert spec.kind == "greater"
    assert spec.floor == 70.99

    shib = parse_strike(
        {
            "custom_strike": {"floor_strike": "0.000000499", "strike_type": "greater"},
            "yes_sub_title": "$0.0000005 or above",
            "ticker": "KXSHIBAD-26AUG2717-T0.000000499",
        }
    )
    assert shib.floor == 0.000000499


def test_price_threshold_uses_spot_vs_strike():
    spec = parse_strike({"yes_sub_title": "Above $80,000", "strike_type": "greater", "custom_strike": {"floor_strike": "80000"}})
    high = price_threshold_prob(spec, 90000, 7 / 365, 0.6)
    low = price_threshold_prob(spec, 50000, 7 / 365, 0.6)
    assert high is not None and low is not None
    assert high > low


def test_devig_two_way_moneyline():
    fair = devig_probs([0.48, 0.56])
    assert abs(sum(fair) - 1.0) < 1e-9
    assert fair[1] > fair[0]


def test_executable_edge_prefers_cheap_side():
    edge, side = _executable_edge(0.7, yes_bid=0.40, yes_ask=0.42)
    assert side == "YES"
    assert edge == 0.7 - 0.42
    edge_no, side_no = _executable_edge(0.2, yes_bid=0.40, yes_ask=0.42)
    assert side_no == "NO"
    assert abs(edge_no - ((1 - 0.2) - (1 - 0.40))) < 1e-9


def test_mid_and_dollars():
    assert parse_dollars("0.4700") == 0.47
    assert mid_price(0.47, 0.48) == 0.475


def test_sports_mutex_predictions():
    from kalshibot.config import Settings

    scanner = Scanner.__new__(Scanner)
    scanner.cfg = Settings()
    series = {"ticker": "KXNBAGAME"}
    event = {
        "event_ticker": "KXNBAGAME-BOSDET",
        "title": "Boston vs Detroit",
        "mutually_exclusive": True,
    }
    markets = [
        {
            "ticker": "BOS",
            "title": "Boston",
            "yes_sub_title": "Boston",
            "status": "active",
            "yes_bid_dollars": "0.4400",
            "yes_ask_dollars": "0.4900",
            "volume_24h_fp": "1000",
            "volume_fp": "1000",
            "liquidity_dollars": "0",
            "close_time": "2026-10-20T00:00:00Z",
        },
        {
            "ticker": "DET",
            "title": "Detroit",
            "yes_sub_title": "Detroit",
            "status": "active",
            "yes_bid_dollars": "0.4600",
            "yes_ask_dollars": "0.5500",
            "volume_24h_fp": "800",
            "volume_fp": "800",
            "liquidity_dollars": "0",
            "close_time": "2026-10-20T00:00:00Z",
        },
    ]
    preds = scanner._sports_mutex("sports", series, event, markets)
    assert len(preds) == 2
    assert abs(sum(p.model_prob or 0 for p in preds) - 1.0) < 1e-9
    assert all(p.method == "devig" for p in preds)
