from bginfo.config import reset_config_cache
from bginfo.ops import check_env

TOKEN = "123456789:AAEhBOweik6ad9r_QXbbbbbbbbbbbbbbbbb"
HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaGhhc2hoYXNoaGFzaA"
SESSION = "s" * 48
KEY = "sk-or-v1-supersecretkeyvalue"


def _setup(monkeypatch, tmp_path, **env):
    monkeypatch.chdir(tmp_path)  # не подхватывать реальный .env
    for name in ("TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "ADMIN_PASSWORD_HASH",
                 "ADMIN_SESSION_SECRET", "CONSULT_CHAT_ID", "ADMIN_HOST"):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reset_config_cache()


def test_ok_and_no_secret_leak(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, TELEGRAM_BOT_TOKEN=TOKEN, OPENROUTER_API_KEY=KEY,
           ADMIN_PASSWORD_HASH=HASH, ADMIN_SESSION_SECRET=SESSION, CONSULT_CHAT_ID="987654321")
    assert check_env.main() == 0
    out = capsys.readouterr().out
    for secret in (TOKEN, KEY, HASH, SESSION, "987654321", "AAEhBOweik6ad9r"):
        assert secret not in out
    assert "Итог: всё готово" in out
    reset_config_cache()


def test_broken_hash_fails(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path, TELEGRAM_BOT_TOKEN=TOKEN,
           ADMIN_PASSWORD_HASH="argon2idv19m65536", ADMIN_SESSION_SECRET=SESSION)
    assert check_env.main() == 1
    out = capsys.readouterr().out
    assert "ИСПОРЧЕН" in out and "Итог: исправьте отмеченное" in out
    assert "argon2idv19m65536" not in out
    reset_config_cache()


def test_empty_fails(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    assert check_env.main() == 1
    out = capsys.readouterr().out
    assert "ПУСТО" in out and "короче 32" in out
    reset_config_cache()
