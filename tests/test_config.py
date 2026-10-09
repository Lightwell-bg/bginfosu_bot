from bginfo.config import AppConfig, Settings, get_config, load_config, reset_config_cache


def test_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.ini")
    assert cfg == AppConfig()
    assert cfg.ai.jev_model == "~typesafe/jev-latest" and cfg.limits.daily_budget_usd == 2.0
    assert cfg.admin.port == 8080 and cfg.bot.timezone == "Europe/Sofia"


def test_ini_override(tmp_path, monkeypatch):
    ini = tmp_path / "c.ini"
    ini.write_text(
        "[limits]\ndaily_budget_usd = 5.5\nuser_text_per_day = 7\n[bot]\nconsult_contact = @x\n[ai]\nretries = bad\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_INI", str(ini))
    reset_config_cache()
    cfg = get_config()
    assert cfg.limits.daily_budget_usd == 5.5 and cfg.limits.user_text_per_day == 7
    assert cfg.bot.consult_contact == "@x" and cfg.ai.retries == 2
    reset_config_cache()


def test_settings_env(monkeypatch):
    monkeypatch.setenv("CONSULT_CHAT_ID", "123")
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    s = Settings(_env_file=None)
    assert s.CONSULT_CHAT_ID == 123 and s.APP_MODE == "demo" and s.ADMIN_USERNAME == "admin"
    assert "secret" not in repr(s)
    assert s.ADMIN_COOKIE_SECURE is True


def test_admin_host_override(monkeypatch):
    from bginfo.config import Settings
    monkeypatch.delenv("ADMIN_HOST", raising=False)
    assert Settings(_env_file=None).ADMIN_HOST is None
    monkeypatch.setenv("ADMIN_HOST", "0.0.0.0")
    assert Settings(_env_file=None).ADMIN_HOST == "0.0.0.0"
    monkeypatch.setenv("ADMIN_HOST", "")
    assert Settings(_env_file=None).ADMIN_HOST is None
