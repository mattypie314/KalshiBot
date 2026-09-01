from src.config import EXIT_CONFIG, HourlySettings
from src.main import main


def test_live_refused_without_flags(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("CONFIRM_LIVE", "NO")
    assert main(["live"]) == EXIT_CONFIG


def test_live_enabled_requires_both_flags():
    assert HourlySettings(live_trading=False, confirm_live="YES").live_enabled is False
    assert HourlySettings(live_trading=True, confirm_live="NO").live_enabled is False
    assert HourlySettings(live_trading=True, confirm_live="YES").live_enabled is True


def test_key_id_strips_quotes():
    settings = HourlySettings(kalshi_api_key_id='  "abc-123"  ', kalshi_private_key_path="/tmp/not-the-home-pem")
    assert settings.kalshi_api_key_id == "abc-123"
