import pytest

from bginfo.config import AppConfig, Settings, get_config, load_config, reset_config_cache


def test_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.ini")
    assert cfg == AppConfig()
    assert cfg.ai.jev_model == "~typesafe/jev-latest" and cfg.limits.daily_budget_usd == 2.0
    assert cfg.admin.port == 8091 and cfg.bot.timezone == "Europe/Sofia"


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


INI = """# header
[ai]
host = keep-me
port = 1

[admin]
# comment about host
host = 127.0.0.1
# comment about port
port = 8080
login_max_attempts = 5

[bot]
timezone = Europe/Sofia
"""


def test_update_admin_access_keeps_comments_and_sections(tmp_path):
    from bginfo.config import update_admin_access
    ini = tmp_path / "c.ini"
    ini.write_text(INI, encoding="utf-8")
    update_admin_access(ini, "0.0.0.0", 8091)
    out = ini.read_text(encoding="utf-8")
    assert out == INI.replace("host = 127.0.0.1", "host = 0.0.0.0").replace("port = 8080", "port = 8091")
    assert load_config(ini).admin.port == 8091 and load_config(ini).admin.host == "0.0.0.0"
    assert not (tmp_path / "c.ini.tmp").exists()


def test_update_admin_access_adds_missing_keys_and_section(tmp_path):
    from bginfo.config import update_admin_access
    ini = tmp_path / "c.ini"
    ini.write_text("[admin]\nlogin_max_attempts = 7\n[bot]\ntimezone = Europe/Sofia\n", encoding="utf-8")
    update_admin_access(ini, "127.0.0.1", 9000)
    cfg = load_config(ini)
    assert (cfg.admin.host, cfg.admin.port, cfg.admin.login_max_attempts) == ("127.0.0.1", 9000, 7)
    ini.write_text("[bot]\ntimezone = Europe/Sofia\n", encoding="utf-8")
    update_admin_access(ini, "0.0.0.0", 8091)
    assert load_config(ini).admin.port == 8091


@pytest.mark.parametrize("host,port", [("1.2.3.4", 8091), ("localhost", 8091), ("0.0.0.0", 80), ("0.0.0.0", 70000),
                                        ("0.0.0.0", "8091"), ("0.0.0.0", True)])
def test_update_admin_access_rejects_invalid(tmp_path, host, port):
    from bginfo.config import update_admin_access
    ini = tmp_path / "c.ini"
    ini.write_text(INI, encoding="utf-8")
    with pytest.raises(ValueError):
        update_admin_access(ini, host, port)
    assert ini.read_text(encoding="utf-8") == INI


def test_update_admin_access_falls_back_when_replace_fails(tmp_path, monkeypatch):
    import os

    from bginfo.config import update_admin_access
    ini = tmp_path / "c.ini"
    ini.write_text(INI, encoding="utf-8")

    def boom(*a, **k):
        raise OSError(16, "busy")

    monkeypatch.setattr(os, "replace", boom)
    update_admin_access(ini, "0.0.0.0", 8091)
    assert "port = 8091" in ini.read_text(encoding="utf-8")
    assert not (tmp_path / "c.ini.tmp").exists()


def test_update_admin_access_trust_proxy_only_when_passed(tmp_path):
    from bginfo.config import update_admin_access
    ini = tmp_path / "c.ini"
    ini.write_text("[admin]\nhost = 127.0.0.1\nport = 8080\ntrust_proxy = true\n", encoding="utf-8")
    update_admin_access(ini, "127.0.0.1", 8080)
    assert load_config(ini).admin.trust_proxy is True
    update_admin_access(ini, "0.0.0.0", 8091, trust_proxy=False)
    cfg = load_config(ini).admin
    assert cfg.trust_proxy is False and cfg.host == "0.0.0.0"


def test_update_admin_access_concurrent_calls_keep_file_intact(tmp_path):
    import configparser
    import threading

    from bginfo.config import update_admin_access
    ini = tmp_path / "c.ini"
    ini.write_text(INI, encoding="utf-8")
    errs: list[Exception] = []

    def work(i: int) -> None:
        try:
            for j in range(15):
                update_admin_access(ini, "0.0.0.0" if (i + j) % 2 else "127.0.0.1", 9000 + i, trust_proxy=bool(j % 2))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    before = configparser.ConfigParser()
    before.read_string(INI)
    after = configparser.ConfigParser()
    after.read_string(ini.read_text(encoding="utf-8"))
    assert set(after.sections()) == set(before.sections())
    assert list(tmp_path.glob("*.tmp")) == []


def test_update_admin_access_refuses_to_write_broken_ini(tmp_path):
    from bginfo.config import update_admin_access
    ini = tmp_path / "c.ini"
    bad = "[admin]\nhost = 127.0.0.1\n[admin]\nport = 1\n"  # дубликат секции: configparser строгий
    ini.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError):
        update_admin_access(ini, "0.0.0.0", 8091)
    assert ini.read_text(encoding="utf-8") == bad
