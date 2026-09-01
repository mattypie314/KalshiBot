import pytest

from src.config import EXIT_CONFIG, HourlySettings
from src.main import live_is_armed, main, normalize_argv


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


def test_live_is_armed_by_prompt_or_flag():
    dry = HourlySettings(live_trading=False, confirm_live="NO")
    assert live_is_armed(dry, confirm="", isatty=False) is False
    assert live_is_armed(dry, confirm="LIVE", isatty=False) is True
    assert live_is_armed(dry, confirm="no", isatty=True, prompt=lambda _: "LIVE") is True
    assert live_is_armed(dry, confirm="YES", isatty=False) is False
    assert live_is_armed(dry, confirm="", isatty=True, prompt=lambda _: "nope") is False
    env = HourlySettings(live_trading=True, confirm_live="YES")
    assert live_is_armed(env, confirm="", isatty=False) is True


def test_live_confirm_flag_does_not_need_env(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("CONFIRM_LIVE", "NO")
    monkeypatch.setattr("src.main.run_scan", lambda *args, **kwargs: 0)
    assert main(["live", "--confirm", "LIVE"]) == 0


def test_live_enabled_requires_both_flags():
    assert HourlySettings(live_trading=False, confirm_live="YES").live_enabled is False
    assert HourlySettings(live_trading=True, confirm_live="NO").live_enabled is False
    assert HourlySettings(live_trading=True, confirm_live="YES").live_enabled is True


def test_key_id_strips_quotes():
    settings = HourlySettings(kalshi_api_key_id='  "abc-123"  ', kalshi_private_key_path="/tmp/not-the-home-pem")
    assert settings.kalshi_api_key_id == "abc-123"


def test_default_bankroll_is_forty(monkeypatch):
    monkeypatch.delenv("BANKROLL", raising=False)
    settings = HourlySettings(_env_file=None, kalshi_api_key_id="", kalshi_private_key_path="/tmp/not-the-home-pem")
    assert settings.bankroll == 40.00
