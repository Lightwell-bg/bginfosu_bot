# Быстрый старт

Только шаги и команды. Теория и подробности — в [README_RU.md](../README_RU.md) (English: [README.md](../README.md)), развёртывание — [DEPLOY.md](DEPLOY.md).

Оглавление:

1. [Часть A. Локально (Windows, PowerShell): демо-база и тестовый бот](#часть-a-локально-windows-powershell-демо-база-и-тестовый-бот)
2. [Часть B. Сервер (Ubuntu VPS, Docker): первый запуск](#часть-b-сервер-ubuntu-vps-docker-первый-запуск)
3. [Часть C. Обновление на сервере](#часть-c-обновление-на-сервере)
4. [Часть D. Переключение на @bginfosu_bot](#часть-d-переключение-на-bginfosu_bot)

Везде используется **тестовый** бот (создаётся в [@BotFather](https://t.me/BotFather) командой `/newbot`). Токен боевого `@bginfosu_bot` не вставлять.

---

## Часть A. Локально (Windows, PowerShell): демо-база и тестовый бот

1. Проверьте Python 3.12. Если команда не находит версию, установите её с <https://www.python.org/downloads/>.

   ```powershell
   py -3.12 --version
   ```

   Ожидается: `Python 3.12.x`.

2. Склонируйте репозиторий и перейдите в него.

   ```powershell
   git clone https://github.com/Lightwell-bg/bginfosu_bot.git
   cd bginfosu_bot
   ```

3. Создайте и активируйте виртуальное окружение.

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   Ожидается: в начале строки приглашения `(.venv)`. В Git Bash вместо второй строки: `source .venv/Scripts/activate`.

4. Установите зависимости.

   ```powershell
   pip install -e ".[dev]"
   ```

   Ожидается: `Successfully installed ... bginfo-bot ...` без ошибок.

5. Создайте `.env` из шаблона.

   ```powershell
   Copy-Item .env.example .env
   ```

6. Получите хеш пароля админки (минимум 10 символов, пароль спросят дважды) и секрет сессии. Скопируйте оба вывода.

   ```powershell
   python -m bginfo.ops.hash_password
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Ожидается: строка `$argon2id$v=19$...` и случайная строка (около 64 символов).

7. Откройте `.env` и заполните обязательные переменные.

   ```powershell
   notepad .env
   ```

   ```ini
   TELEGRAM_BOT_TOKEN=токен-ТЕСТОВОГО-бота
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD_HASH='$argon2id$v=19$...'
   ADMIN_SESSION_SECRET=случайная-строка-из-шага-6
   ADMIN_COOKIE_SECURE=false
   ```

   - Хеш обязательно в **одинарных** кавычках (иначе `$...` будет испорчен).
   - `OPENROUTER_API_KEY` (ключ с <https://openrouter.ai/keys>) и `CONSULT_CHAT_ID` — по желанию. Без ключа бот работает только кнопками.
   - `DB_PATH` и `APP_MODE` для демо задаются в окнах PowerShell (шаги 9 и 11), в `.env` их не менять.

8. Соберите демо-базу.

   ```powershell
   python -m bginfo.seed --db data/demo.db build-demo
   ```

   Ожидается: `Демо-база готова: data\demo.db (выпуск №1). Тестовые данные, не для рабочей базы.` Если файл уже есть, добавьте `--force`.

9. Запустите админку на демо-базе (окно PowerShell №1, venv активирован).

   ```powershell
   $env:DB_PATH = "data/demo.db"
   $env:APP_MODE = "demo"
   python -m bginfo.admin
   ```

10. Проверьте админку (окно PowerShell №3 или любой другой терминал).

    ```powershell
    curl.exe -s http://127.0.0.1:8080/healthz
    ```

    Ожидается: `{"ok":true}`. Затем откройте <http://127.0.0.1:8080>, войдите с логином `admin` и паролем из шага 6. На главной должен быть «Действующий выпуск №1».

11. Запустите бота с тестовым токеном на той же демо-базе (окно PowerShell №2: `cd` в папку `bginfosu_bot`, активируйте venv, как в шаге 3).

    ```powershell
    $env:DB_PATH = "data/demo.db"
    $env:APP_MODE = "demo"
    python -m bginfo.bot
    ```

    Если в логе `A webhook is configured for this bot`, у тестового бота остался webhook: создайте нового тестового бота и поменяйте токен в `.env`.

12. В Telegram откройте тестового бота и отправьте `/start`.

    Ожидается: приветствие с кнопками «Подобрать вариант» и «Описать ситуацию самому», в результатах плашка «ДЕМО-база». Сценарии для проверки — [TEST_SCENARIOS.md](TEST_SCENARIOS.md).

13. Остановите бота и админку (`Ctrl+C` в обоих окнах) и запустите тесты и линтер.

    ```powershell
    pytest
    ruff check .
    ```

    Ожидается: все тесты `passed`, у ruff `All checks passed!`.

14. Живые платные проверки (по желанию; тратят деньги на OpenRouter). Ключ задаётся в окружении окна PowerShell: скрипты читают его из `OPENROUTER_API_KEY` окружения, а не из `.env`.

    ```powershell
    $env:OPENROUTER_API_KEY = "ваш-ключ"
    python scripts/smoke_live.py
    python scripts/jev_eval.py
    ```

    Ожидается: `smoke_live.py` печатает шаги анкеты, итог и «Фактическая стоимость (usage.cost)»; `jev_eval.py` прогоняет примеры из `tests/jev_eval/cases.yaml` и записывает отчёт в `docs/JEV_EVAL.md`. Без ключа оба скрипта сообщат, что `OPENROUTER_API_KEY` не задан.

---

## Часть B. Сервер (Ubuntu VPS, Docker): первый запуск

Все команды `bash` выполняются на сервере по SSH (вход по ключу). Команды `powershell` — на вашем компьютере. Бот на сервере запускается с токеном **тестового** бота; боевой токен ставится только в части D.

1. Подключитесь к серверу. Вместо `IP_СЕРВЕРА` подставьте адрес вашего сервера: это единственная подстановка во всей части B. Если вы заходите не под `root`, замените и `root` на своего пользователя.

   ```powershell
   ssh root@IP_СЕРВЕРА
   ```

2. Установите Docker (официальный репозиторий, как в [DEPLOY.md](DEPLOY.md)).

   ```bash
   sudo apt-get update
   sudo apt-get install -y ca-certificates curl
   sudo install -m 0755 -d /etc/apt/keyrings
   sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
   sudo chmod a+r /etc/apt/keyrings/docker.asc
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
   sudo apt-get update
   sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   sudo systemctl enable --now docker
   sudo usermod -aG docker "$USER"
   ```

   Выйдите (`exit`) и зайдите по SSH снова, чтобы группа `docker` применилась. Проверка:

   ```bash
   docker --version && docker compose version && docker run --rm hello-world
   ```

   Ожидается: версии Docker и Compose и сообщение `Hello from Docker!`.

3. Создайте каталог и склонируйте код (репозиторий публичный).

   ```bash
   sudo mkdir -p /opt/bginfo-bot
   sudo chown "$USER":"$USER" /opt/bginfo-bot
   cd /opt/bginfo-bot
   git clone https://github.com/Lightwell-bg/bginfosu_bot.git .
   git log --oneline -1
   ```

   Ожидается: одна строка с последним коммитом.

4. Создайте `.env` и соберите образ (нужен для генерации хеша в контейнере).

   ```bash
   cd /opt/bginfo-bot
   cp .env.example .env
   chmod 600 .env
   docker compose build
   ```

   Ожидается: сборка завершается без ошибок (`Built`/`naming to docker.io/library/bginfo-bot:latest`).

5. Получите хеш пароля админки и секрет сессии в контейнере (пароль минимум 10 символов, спросят дважды). Скопируйте оба вывода.

   ```bash
   docker compose run --rm admin python -m bginfo.ops.hash_password
   docker compose run --rm admin python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Ожидается: строка `$argon2id$v=19$...` и случайная строка. (Хеш можно получить и локально, как в части A, шаг 6.)

6. Заполните `.env`.

   ```bash
   nano .env
   ```

   ```ini
   TELEGRAM_BOT_TOKEN=токен-ТЕСТОВОГО-бота
   OPENROUTER_API_KEY=ключ-OpenRouter
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD_HASH='$argon2id$v=19$...'
   ADMIN_SESSION_SECRET=случайная-строка-из-шага-5
   ADMIN_COOKIE_SECURE=false
   APP_MODE=production
   ```

   - Хеш в **одинарных** кавычках.
   - `ADMIN_COOKIE_SECURE=false` нужно для доступа через SSH-туннель (по http).
   - `CONSULT_CHAT_ID` — по желанию. `DB_PATH` и `ADMIN_HOST` не трогать: в Docker их задаёт `docker-compose.yml`.
   - Сохранить в nano: `Ctrl+O`, `Enter`, `Ctrl+X`.

7. Запустите сервисы.

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

   Ожидается: сервисы `bot` и `admin` в состоянии `Up`, у `admin` со временем `(healthy)`.

8. Загрузите черновики рабочей базы знаний (выпуск не публикуется).

   ```bash
   docker compose run --rm bot python -m bginfo.seed seed-draft
   ```

   Ожидается: `Черновик заполнен: /data/app.db. Выпуск не опубликован — одобрение и публикация за владельцем.` Если черновик уже не пуст, команда откажется (замена — только с `--force`).

9. Откройте SSH-туннель к админке. На **своём компьютере** (оставьте окно открытым). Вместо `IP_СЕРВЕРА` подставьте адрес вашего сервера (и `root` — своего пользователя, если вход не под `root`).

   ```powershell
   ssh -L 8080:127.0.0.1:8080 root@IP_СЕРВЕРА
   ```

10. Проверьте админку через туннель (второе окно PowerShell на вашем компьютере).

    ```powershell
    curl.exe -s http://127.0.0.1:8080/healthz
    ```

    Ожидается: `{"ok":true}`. Откройте <http://127.0.0.1:8080> и войдите с логином и паролем из шагов 5–6.

11. В админке проверьте карточки черновика, у каждой отметьте «подтверждено», укажите автора и дату, затем опубликуйте выпуск. Порядок и правила — [KB_MANUAL_UPDATE.md](KB_MANUAL_UPDATE.md), пробелы в данных — [KB_GAPS.md](KB_GAPS.md). Пока ничего не опубликовано, бот отвечает «сервис готовится»: это нормально.

12. Проверьте бота: в Telegram откройте тестового бота и отправьте `/start`.

    Ожидается: после публикации выпуска приветствие с кнопками; до публикации — ответ «сервис готовится».

13. Смотрите логи при необходимости (на сервере; выход — `Ctrl+C`).

    ```bash
    cd /opt/bginfo-bot
    docker compose logs -f bot
    ```

    Строка `A webhook is configured for this bot` означает, что у бота настроен webhook и он остановился (см. [SWITCHOVER.md](SWITCHOVER.md)).

14. Сделайте разовый бэкап.

    ```bash
    cd /opt/bginfo-bot
    docker compose --profile ops run --rm backup
    ```

    Ожидается: `Бэкап создан: /data/backups/app-ГГГГММДД-ЧЧММСС.db`. Ежедневный бэкап по cron, копирование бэкапов с сервера и восстановление — [DEPLOY.md](DEPLOY.md), раздел 10.

---

## Часть C. Обновление на сервере

На сервере:

```bash
cd /opt/bginfo-bot && git pull
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8080/healthz
```

Ожидается: `bot` и `admin` в состоянии `Up`, `admin` `(healthy)`, ответ `{"ok":true}`. Если в `.env.example` добавились переменные, внесите их в серверный `.env` до `up -d` (проверка в [DEPLOY.md](DEPLOY.md), раздел 9). Перед обновлением полезен разовый бэкап (часть B, шаг 14).

---

## Часть D. Переключение на @bginfosu_bot

Только с разрешения владельца и строго по [SWITCHOVER.md](SWITCHOVER.md); два приложения с одним токеном не запускать.
