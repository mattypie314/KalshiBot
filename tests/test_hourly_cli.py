from src.config import EXIT_CONFIG, HourlySettings
from src.main import main


def test_kalshibot_hourly_alias_refuses_live(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("CONFIRM_LIVE", "NO")
    from kalshibot.__main__ import main as kalshi_main
    import sys

    previous = sys.argv
    sys.argv = ["kalshibot", "hourly", "live"]
    try:
        try:
            kalshi_main()
        except SystemExit as exc:
            assert exc.code == EXIT_CONFIG
        else:
            raise AssertionError("hourly live should SystemExit")
    finally:
        sys.argv = previous


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
