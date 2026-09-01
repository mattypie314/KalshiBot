import pytest

from src.config import EXIT_CONFIG, HourlySettings
from src.main import main, normalize_argv


def test_normalize_expands_shortcuts():
    assert normalize_argv(["s"]) == ["scan"]
    assert normalize_argv(["o"]) == ["once"]
    assert normalize_argv(["a"]) == ["auth"]
    assert normalize_argv(["l"]) == ["live"]
    assert normalize_argv(["1", "--asset", "BTC"]) == ["scan", "--asset", "BTC"]
    assert normalize_argv(["scan", "--asset", "ETH"]) == ["scan", "--asset", "ETH"]


def test_normalize_menu_pick_on_tty():
    assert normalize_argv([], isatty=True, prompt=lambda _: "2") == ["once"]
    assert normalize_argv([], isatty=True, prompt=lambda _: "") == ["scan"]
    with pytest.raises(SystemExit):
        normalize_argv([], isatty=True, prompt=lambda _: "nope")


def test_normalize_defaults_to_scan_when_not_a_tty():
    assert normalize_argv([], isatty=False) == ["scan"]


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
