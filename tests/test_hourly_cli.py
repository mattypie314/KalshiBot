import pytest

from src.config import EXIT_CONFIG, HourlySettings
from src.main import apply_host_flags, live_is_armed, main, normalize_argv, run_auth


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


def _ok_balance(*args, **kwargs):
    return True, {"balance": 40}


def test_live_refused_without_flags(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("CONFIRM_LIVE", "NO")
    monkeypatch.setattr("src.main.probe_balance", _ok_balance)
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
    monkeypatch.setattr("src.main.probe_balance", _ok_balance)
    monkeypatch.setattr("src.main.run_scan", lambda *args, **kwargs: 0)
    assert main(["live", "--confirm", "LIVE"]) == 0


def test_apply_prod_flag_disables_demo():
    settings = HourlySettings(_env_file=None, use_demo=True)
    args = type("A", (), {"prod": True, "demo": False})()
    apply_host_flags(settings, args)
    assert settings.use_demo is False
    assert "demo-api" not in settings.trading_base_url


def test_auth_retries_other_host_on_401(monkeypatch, capsys):
    monkeypatch.setattr(
        "src.main.HourlySettings.ensure_private_key_file",
        lambda self: self.kalshi_private_key_path,
    )

    class Fake:
        def __init__(self, *a, **k):
            self.trading_base_url = k.get("trading_base_url", "")

        def auth_status(self):
            return {
                "can_trade": True,
                "key_id_set": True,
                "key_id_len": 36,
                "pem_path": "/tmp/k.pem",
                "pem_exists": True,
                "pem_looks_private": True,
                "trading_host": self.trading_base_url,
            }

        def close(self):
            pass

    monkeypatch.setattr("src.main._host_client", lambda settings, use_demo: Fake(trading_base_url="x"))

    def probe(settings, *, use_demo):
        if use_demo:
            return False, "401 authentication_error NOT_FOUND"
        return True, {"balance": 12.5}

    monkeypatch.setattr("src.main.probe_balance", probe)
    settings = HourlySettings(_env_file=None, use_demo=True, kalshi_api_key_id="abc", kalshi_private_key_path="/tmp/k.pem")
    assert run_auth(settings) == EXIT_CONFIG
    out = capsys.readouterr().out
    assert "AUTH OK on PROD" in out
    assert "./kb live --prod" in out


def test_live_refuses_demo_401_and_hints_prod(monkeypatch, capsys):
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("CONFIRM_LIVE", "NO")

    def probe(settings, *, use_demo):
        if use_demo:
            return False, "401 on /portfolio/balance"
        return True, {"balance": 40}

    monkeypatch.setattr("src.main.probe_balance", probe)
    monkeypatch.setattr("src.main.load_settings", lambda: HourlySettings(_env_file=None, use_demo=True))
    assert main(["live", "--confirm", "LIVE"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "LIVE refused: auth failed on DEMO" in err
    assert "./kb live --prod" in err


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
