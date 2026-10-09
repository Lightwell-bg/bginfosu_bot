from bginfo.config import reset_config_cache
from bginfo.ops import check_env

TOKEN = "123456789:AAEhBOweik6ad9r_QXbbbbbbbbbbbbbbbbb"
PASSWORD = "my-admin-password"
SESSION = "s" * 48
KEY = "sk-or-v1-supersecretkeyvalue"


def _setup(monkeypatch, tmp_path, **env):
    monkeypatch.chdir(tmp_path)  # не подхватывать реальный .env
    for name in ("TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "ADMIN_PASSWORD",
                 "ADMIN_SESSION_SECRET", "CONSULT_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_config_cache()


def test_ok_and_no_secret_leak(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, TELEGRAM_BOT_TOKEN=TOKEN, OPENROUTER_API_KEY=KEY,
           ADMIN_PASSWORD=PASSWORD, ADMIN_SESSION_SECRET=SESSION, CONSULT_CHAT_ID="987654321")
    assert check_env.main() == 0
    out = capsys.readouterr().out
    for secret in (TOKEN, KEY, PASSWORD, SESSION, "987654321", "AAEhBOweik6ad9r"):
        assert secret not in out
    assert "Итог: всё готово" in out
    reset_config_cache()


def test_empty_password_fails(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, TELEGRAM_BOT_TOKEN=TOKEN, ADMIN_SESSION_SECRET=SESSION)
    assert check_env.main() == 1
    out = capsys.readouterr().out
    assert "ADMIN_PASSWORD: ПУСТО (обязательно)" in out and "Итог: исправьте отмеченное" in out
    reset_config_cache()
