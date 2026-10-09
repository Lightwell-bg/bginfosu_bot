# Быстрый старт (Windows, PowerShell, демо-база)

Подробности и теория — в [README_RU.md](../README_RU.md). Здесь только шаги. Работаем на отдельной демо-базе `data/demo.db` с тестовыми карточками и **тестовым** ботом.

1. Перейдите в папку проекта.

   ```powershell
   cd D:\1PythonProjects\20261009bginfosu_bot\bginfosu_bot
   ```

2. Создайте и активируйте виртуальное окружение (Python 3.12).

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   В Git Bash вместо второй строки: `source .venv/Scripts/activate`.

3. Установите зависимости.

   ```powershell
   pip install -e ".[dev]"
   ```

4. Создайте `.env` из шаблона.

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

5. Заполните в `.env` обязательные переменные:

   - `ADMIN_USERNAME` — логин админки, например `admin`.
   - `ADMIN_PASSWORD_HASH` — хеш пароля (шаг 6), в одинарных кавычках.
   - `ADMIN_SESSION_SECRET` — случайная строка от 32 символов (шаг 6).
   - `ADMIN_COOKIE_SECURE=false` — локальный запуск идёт по http.
   - `TELEGRAM_BOT_TOKEN` — токен **тестового** бота из [@BotFather](https://t.me/BotFather) (`/newbot`). Токен боевого `@bginfosu_bot` не использовать.
   - `OPENROUTER_API_KEY` — по желанию (ключ с <https://openrouter.ai/keys>). Без него бот работает только кнопками.
   - `CONSULT_CHAT_ID` — по желанию. `DB_PATH` и `APP_MODE` ниже задаются в окне PowerShell.

6. Получите хеш пароля и секрет сессии и вставьте их в `.env`.

   ```powershell
   python -m bginfo.ops.hash_password
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Хеш запишите так: `ADMIN_PASSWORD_HASH='$argon2id$v=19$...'`.

7. Соберите демо-базу.

   ```powershell
   python -m bginfo.seed --db data/demo.db build-demo
   ```

   Ожидаемый вывод: `Демо-база готова: data\demo.db (выпуск №1)...`.

8. Запустите админку на демо-базе (окно PowerShell №1, venv активирован).

   ```powershell
   $env:DB_PATH = "data/demo.db"
   $env:APP_MODE = "demo"
   python -m bginfo.admin
   ```

9. Проверьте, что админка отвечает, и войдите: откройте <http://127.0.0.1:8080>, логин и пароль из шагов 5–6.

   ```powershell
   curl.exe -s http://127.0.0.1:8080/healthz
   ```

   Ожидаемый ответ: `{"ok":true}`. На главной должен быть «Действующий выпуск №1».

10. Запустите бота с тестовым токеном на той же демо-базе (окно PowerShell №2: перейдите в папку проекта и активируйте venv, как в шагах 1–2).

    ```powershell
    $env:DB_PATH = "data/demo.db"
    $env:APP_MODE = "demo"
    python -m bginfo.bot
    ```

    Если в логе `A webhook is configured for this bot`, у тестового бота остался webhook: создайте нового тестового бота.

11. В Telegram откройте тестового бота и отправьте `/start`. Должно прийти приветствие с кнопками «Подобрать вариант» и «Описать ситуацию самому», в результате будет плашка «ДЕМО-база». Сценарии для проверки — [TEST_SCENARIOS.md](TEST_SCENARIOS.md).

12. Остановите бота и админку (`Ctrl+C` в обоих окнах) и запустите тесты.

    ```powershell
    pytest
    ruff check .
    ```

    Живые платные проверки (по желанию, нужен ключ OpenRouter):

    ```powershell
    $env:OPENROUTER_API_KEY = "ваш-ключ"
    python scripts/smoke_live.py
    ```
