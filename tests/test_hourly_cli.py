import pytest

from src.config import EXIT_CONFIG, HourlySettings
from src.main import apply_host_flags, live_is_armed, main, normalize_argv, run_auth


def test_normalize_expands_shortcuts():
    assert normalize_argv(["s"]) == ["scan"]
    assert normalize_argv(["o"]) == ["once"]
    assert normalize_argv(["a"]) == ["auth"]
    assert normalize_argv(["l"]) == ["live"]
    assert normalize_argv(["e"]) == ["env"]
    assert normalize_argv(["5"]) == ["env"]
    assert normalize_argv(["1", "--asset", "BTC"]) == ["scan", "--asset", "BTC"]
    assert normalize_argv(["scan", "--asset", "ETH"]) == ["scan", "--asset", "ETH"]


def test_normalize_menu_pick_on_tty():
    assert normalize_argv([], isatty=True, prompt=lambda _: "2") == ["once"]
    assert normalize_argv([], isatty=True, prompt=lambda _: "5") == ["env"]
    assert normalize_argv([], isatty=True, prompt=lambda _: "") == ["scan"]
    with pytest.raises(SystemExit):
        normalize_argv([], isatty=True, prompt=lambda _: "nope")


def test_normalize_defaults_to_scan_when_not_a_tty():
    assert normalize_argv([], isatty=False) == ["scan"]


def _ok_balance(*args, **kwargs):
    return True, {"balance": 40}


def test_live_refused_without_flags(monkeypatch):
    monkeypatch.setenv("HALTED", "false")
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("CONFIRM_LIVE", "NO")
    monkeypatch.setattr("src.main.probe_balance", _ok_balance)
    assert main(["live"]) == EXIT_CONFIG


def test_halted_blocks_confirm_live(monkeypatch, capsys):
    monkeypatch.setenv("HALTED", "true")
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("CONFIRM_LIVE", "YES")
    monkeypatch.setattr("src.main.probe_balance", _ok_balance)
    called = {"scan": False}

    def _scan(*args, **kwargs):
        called["scan"] = True
        return 0

    monkeypatch.setattr("src.main.run_scan", _scan)
    assert main(["live", "--confirm", "LIVE"]) == EXIT_CONFIG
    assert called["scan"] is False
    assert "HALTED" in capsys.readouterr().err


def test_live_is_armed_by_prompt_or_flag():
    dry = HourlySettings(halted=False, live_trading=False, confirm_live="NO")
    assert live_is_armed(dry, confirm="", isatty=False) is False
    assert live_is_armed(dry, confirm="LIVE", isatty=False) is True
    assert live_is_armed(dry, confirm="no", isatty=True, prompt=lambda _: "LIVE") is True
    assert live_is_armed(dry, confirm="YES", isatty=False) is False
    assert live_is_armed(dry, confirm="", isatty=True, prompt=lambda _: "nope") is False
    env = HourlySettings(halted=False, live_trading=True, confirm_live="YES")
    assert live_is_armed(env, confirm="", isatty=False) is True
    halted = HourlySettings(halted=True, live_trading=True, confirm_live="YES")
    assert live_is_armed(halted, confirm="LIVE", isatty=False) is False


def test_live_confirm_flag_does_not_need_env(monkeypatch):
    monkeypatch.setenv("HALTED", "false")
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
    monkeypatch.setenv("HALTED", "false")
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("CONFIRM_LIVE", "NO")

    def probe(settings, *, use_demo):
        if use_demo:
            return False, "401 on /portfolio/balance"
        return True, {"balance": 40}

    monkeypatch.setattr("src.main.probe_balance", probe)
    monkeypatch.setattr(
        "src.main.load_settings",
        lambda: HourlySettings(_env_file=None, use_demo=True, halted=False),
    )
    assert main(["live", "--confirm", "LIVE"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "LIVE refused: auth failed on DEMO" in err
    assert "./kb live --prod" in err


def test_live_enabled_requires_both_flags():
    assert HourlySettings(halted=False, live_trading=False, confirm_live="YES").live_enabled is False
    assert HourlySettings(halted=False, live_trading=True, confirm_live="NO").live_enabled is False
    assert HourlySettings(halted=False, live_trading=True, confirm_live="YES").live_enabled is True
    assert HourlySettings(halted=True, live_trading=True, confirm_live="YES").live_enabled is False


def test_halted_defaults_true(monkeypatch):
    monkeypatch.delenv("HALTED", raising=False)
    settings = HourlySettings(_env_file=None, kalshi_api_key_id="", kalshi_private_key_path="/tmp/not-the-home-pem")
    assert settings.halted is True


def test_key_id_strips_quotes():
    settings = HourlySettings(kalshi_api_key_id='  "abc-123"  ', kalshi_private_key_path="/tmp/not-the-home-pem")
    assert settings.kalshi_api_key_id == "abc-123"


def test_upsert_dotenv_sets_use_demo(tmp_path):
    from src.main import upsert_dotenv

    path = tmp_path / ".env"
    path.write_text("USE_DEMO=true\nBANKROLL=40\n")
    upsert_dotenv(path, "USE_DEMO", "false")
    text = path.read_text()
    assert "USE_DEMO=false" in text
    assert "BANKROLL=40" in text
    assert text.count("USE_DEMO=") == 1


def test_env_prod_writes_dotenv(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("USE_DEMO=true\n")
    monkeypatch.setattr("src.main.load_settings", lambda: HourlySettings(_env_file=None, use_demo=True))
    assert main(["env", "--prod"]) == 0
    assert "USE_DEMO=false" in (tmp_path / ".env").read_text()
    assert "PROD" in capsys.readouterr().out


def test_env_show_prints_nano_path(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.main.load_settings", lambda: HourlySettings(_env_file=None, use_demo=True))
    assert main(["env"]) == 0
    out = capsys.readouterr().out
    assert "DEMO" in out
    assert "nano" in out


def test_default_bankroll_is_forty(monkeypatch):
    monkeypatch.delenv("BANKROLL", raising=False)
    settings = HourlySettings(_env_file=None, kalshi_api_key_id="", kalshi_private_key_path="/tmp/not-the-home-pem")
    assert settings.bankroll == 40.00
    assert settings.max_risk_dollars == 2.00
    assert settings.preferred_risk_dollars == 1.75
    assert settings.min_net_edge == 0.06
    assert settings.soft_net_edge == 0.06
    assert settings.min_strike_distance_pct == 0.005


def test_apply_kalshi_shell_env_reads_export_and_home(tmp_path, monkeypatch):
    from src.config import apply_kalshi_shell_env

    monkeypatch.setenv("HOME", "/home/mkubit")
    path = tmp_path / "env"
    path.write_text(
        "export KALSHI_API_KEY_ID=abc-123\n"
        "export KALSHI_PRIVATE_KEY_PATH=$HOME/.kalshi/kalshi_private_key.pem\n"
    )
    dest: dict[str, str] = {"HOME": "/home/mkubit"}
    loaded = apply_kalshi_shell_env(path, dest)
    assert loaded["KALSHI_API_KEY_ID"] == "abc-123"
    assert dest["KALSHI_PRIVATE_KEY_PATH"] == "/home/mkubit/.kalshi/kalshi_private_key.pem"
