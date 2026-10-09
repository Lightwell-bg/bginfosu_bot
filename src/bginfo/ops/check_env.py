"""``python -m bginfo.ops.check_env``: проверить настройки, которые реально видит процесс.

Значения секретов никогда не печатаются: только статус («задан», «ОК», «ошибка формата»).
Код выхода 0, если обязательные настройки в порядке, иначе 1.
"""

from __future__ import annotations

import re

from bginfo.config import get_settings

_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")


def _line(name: str, text: str) -> str:
    return f"{name}: {text}"


def run_checks() -> tuple[list[str], bool]:
    """Вернуть (строки отчёта, все ли обязательные настройки в порядке)."""
    s = get_settings()
    lines: list[str] = []
    ok = True

    token = s.TELEGRAM_BOT_TOKEN.get_secret_value().strip() if s.TELEGRAM_BOT_TOKEN else ""
    if not token:
        lines.append(_line("TELEGRAM_BOT_TOKEN", "ПУСТО (обязательно)"))
        ok = False
    elif _TOKEN_RE.match(token):
        lines.append(_line("TELEGRAM_BOT_TOKEN", "задан, формат OK"))
    else:
        lines.append(_line("TELEGRAM_BOT_TOKEN", "задан, но похоже на ошибку формата"))
        ok = False

    key = s.OPENROUTER_API_KEY.get_secret_value().strip() if s.OPENROUTER_API_KEY else ""
    if not key:
        lines.append(_line("OPENROUTER_API_KEY", "пусто — бот будет работать только кнопками"))
    elif key.startswith("sk-or-"):
        lines.append(_line("OPENROUTER_API_KEY", "задан (начинается с sk-or-)"))
    else:
        lines.append(_line("OPENROUTER_API_KEY", "задан, но не начинается с sk-or- (проверьте ключ)"))

    lines.append(_line("ADMIN_USERNAME", s.ADMIN_USERNAME))

    if s.ADMIN_PASSWORD and s.ADMIN_PASSWORD.get_secret_value():
        lines.append(_line("ADMIN_PASSWORD", "задан"))
    else:
        lines.append(_line("ADMIN_PASSWORD", "ПУСТО (обязательно)"))
        ok = False

    secret = s.ADMIN_SESSION_SECRET.get_secret_value() if s.ADMIN_SESSION_SECRET else ""
    if len(secret) >= 32:
        lines.append(_line("ADMIN_SESSION_SECRET", "OK"))
    else:
        lines.append(_line("ADMIN_SESSION_SECRET", "короче 32 символов или пуст"))
        ok = False

    lines.append(_line("ADMIN_COOKIE_SECURE", str(s.ADMIN_COOKIE_SECURE).lower()))

    if s.CONSULT_CHAT_ID is not None:
        lines.append(_line("CONSULT_CHAT_ID", "задан (число)"))
    else:
        lines.append(_line("CONSULT_CHAT_ID", "пусто — заявки на консультацию отключены"))

    lines.append(_line("DB_PATH", s.DB_PATH))
    lines.append(_line("APP_MODE", s.APP_MODE))
    lines.append(_line("ADMIN_HOST", s.ADMIN_HOST or "(пусто)"))
    return lines, ok


def main() -> int:
    lines, ok = run_checks()
    for line in lines:
        print(line)
    print("Итог: всё готово" if ok else "Итог: исправьте отмеченное")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
