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

6. Получите секрет сессии. Скопируйте вывод.

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Ожидается: случайная строка (около 64 символов).

7. Откройте `.env` и заполните обязательные переменные.

   ```powershell
   notepad .env
   ```

   ```ini
   TELEGRAM_BOT_TOKEN=токен-ТЕСТОВОГО-бота
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=ваш_пароль
   ADMIN_SESSION_SECRET=случайная-строка-из-шага-6
   ADMIN_COOKIE_SECURE=false
   ```

   - `ADMIN_PASSWORD` — любой не пустой пароль входа в админку, без кавычек. Админка сама хеширует его при запуске.
   - Откуда взять каждое значение (токен у @BotFather, `CONSULT_CHAT_ID`, ключ OpenRouter, пароль админки и секрет) — подробно в части B, шаг 6, подпункты с именами переменных. Не дублируем здесь.
   - `OPENROUTER_API_KEY` и `CONSULT_CHAT_ID` — по желанию. Без ключа бот работает только кнопками.
   - Проверка заполнения (значения секретов не печатаются; в конце должно быть «Итог: всё готово»):

     ```powershell
     python -m bginfo.ops.check_env
     ```
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
    curl.exe -s http://127.0.0.1:8091/healthz
    ```

    Ожидается: `{"ok":true}`. Затем откройте <http://127.0.0.1:8091>, войдите с логином `admin` и паролем из шага 6. На главной должен быть «Действующий выпуск №1».

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

2. Docker и Docker Compose на сервере уже установлены — ничего устанавливать не нужно, переходите к шагу 3.

3. Создайте каталог и склонируйте код (репозиторий публичный).

   ```bash
   sudo mkdir -p /opt/bginfo-bot
   sudo chown "$USER":"$USER" /opt/bginfo-bot
   cd /opt/bginfo-bot
   git clone https://github.com/Lightwell-bg/bginfosu_bot.git .
   git log --oneline -1
   ```

   Ожидается: одна строка с последним коммитом.

   Дальше `.env` можно подготовить двумя способами:

   - **Способ 1 (рекомендуемый): готовите `.env` у себя на компьютере и загружаете на сервер** — шаг 4А, после него сразу шаг 7.
   - **Способ 2: правите `.env` прямо на сервере в nano** — шаги 4–6.

4А. Подготовьте `.env` на своём компьютере и загрузите на сервер.

   **4А.1. Проверьте порт админки** (на сервере). Порт задан в `config.ini` (`[admin] port = 8091`); на сервере 8080 и 8081 заняты другими сервисами. Проверьте, что 8091 свободен:

   ```bash
   ss -ltn | grep -q ':8091 ' && echo "8091 занят: поменяйте port в config.ini" || echo "8091 свободен"
   ```

   Ожидается: `8091 свободен`. Порт в `.env` не вписывается.

   **4А.2. Получите секрет сессии** (на своём компьютере, PowerShell, в папке проекта с активированным venv — часть A, шаги 2–4).

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   - Команда выдаёт случайную строку около 64 символов — это секрет сессии. Скопируйте её.
   - Пароль входа в админку придумайте сами (любой не пустой) и **запомните его**: он пойдёт в `ADMIN_PASSWORD`.

   **4А.3. Создайте файл вне папки проекта** (чтобы он случайно не попал в git):

   ```powershell
   notepad $HOME\bginfo-server.env
   ```

   Блокнот спросит «создать новый файл?» — нажмите «Да». Вставьте целиком текст ниже и замените значения по таблице:

   ```ini
   # --- Telegram ---
   # Токен бота от @BotFather
   TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   # Чат/пользователь Telegram, куда уходят заявки на консультацию (числовой id)
   CONSULT_CHAT_ID=

   # --- OpenRouter (JEV и объяснения) ---
   # Ключ: https://openrouter.ai/keys
   OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

   # --- Админка ---
   ADMIN_USERNAME=admin
   # Пароль входа в админку (любой, не пустой)
   ADMIN_PASSWORD=ваш_пароль
   # Длинная случайная строка для подписи сессий
   ADMIN_SESSION_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   # true при работе за HTTPS; false при входе по http (по IP сервера, SSH-туннель, локально)
   ADMIN_COOKIE_SECURE=false

   # --- Прочее ---
   DB_PATH=data/app.db
   # production | demo | test
   APP_MODE=production
   ```

   | Строка | Что вписать | Откуда взять |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | токен бота, без кавычек | Боевой @bginfosu_bot — **только если старый бот уже остановлен** (иначе два приложения будут конфликтовать): [@BotFather](https://t.me/BotFather) → `/mybots` → @bginfosu_bot → «API Token». Кнопку «Revoke» **не нажимать**. Или на сервере из старого контейнера: `docker inspect bginfo-bot --format '{{range .Config.Env}}{{println .}}{{end}}' \| grep -i token`. Для пробы — токен тестового бота (`/newbot`). |
   | `CONSULT_CHAT_ID` | число или пусто | Ваш id: написать [@userinfobot](https://t.me/userinfobot). Получатель должен сам нажать `/start` у бота. Пусто — заявки отключены. |
   | `OPENROUTER_API_KEY` | `sk-or-v1-...` или пусто | <https://openrouter.ai/keys> → Create Key; пополнить <https://openrouter.ai/settings/credits>. Пусто — бот работает только кнопками. |
   | `ADMIN_USERNAME` | логин латиницей | придумать, например `admin` |
   | `ADMIN_PASSWORD` | ваш пароль, без кавычек | придумать самому (любой, не пустой) |
   | `ADMIN_SESSION_SECRET` | строка без кавычек | вывод команды шага 4А.2 |
   | `ADMIN_COOKIE_SECURE` | `false` | вход по http (по IP сервера или через SSH-туннель); `true` только за Caddy с HTTPS |
   | `DB_PATH` | не менять | в Docker перекрывается на `/data/app.db` |
   | `APP_MODE` | `production` | — |

   Правила: без пробелов вокруг `=`; кавычки не нужны; каждое значение в одну строку. Сохраните (`Ctrl+S`) и закройте Блокнот.

   **4А.4. Загрузите файл на сервер** (на своём компьютере, PowerShell). Вместо `АДРЕС_СЕРВЕРА` — адрес, по которому вы заходите по SSH; `vlad` — ваш пользователь на сервере.

   ```powershell
   scp $HOME\bginfo-server.env vlad@АДРЕС_СЕРВЕРА:/opt/bginfo-bot/.env
   ```

   Ожидается строка `bginfo-server.env  100% ...`.

   **4А.5. На сервере: исправьте переводы строк, закройте доступ и соберите образ.** Блокнот Windows сохраняет переводы строк как `\r\n`, из-за этого в значения попадает лишний символ — первая команда это убирает.

   ```bash
   cd /opt/bginfo-bot
   sed -i 's/\r$//' .env
   chmod 600 .env
   docker compose build
   ```

   Ожидается: сборка без `ERROR`. Дальше переходите к **шагу 7** (проверка `.env`), шаги 4–6 пропустите.

4. Создайте `.env` и соберите образ (способ 2 — правка на сервере).

   ```bash
   cd /opt/bginfo-bot
   cp .env.example .env
   chmod 600 .env
   docker compose build
   ```

   Что делает каждая команда:

   - `cp .env.example .env` — копирует шаблон в рабочий файл `.env` (пока с пустыми значениями).
   - `chmod 600 .env` — права «читать и писать может только владелец»: ключи и токены не увидят другие пользователи сервера.
   - `docker compose build` — собирает образ приложения, занимает 1–3 минуты. `.env` нужен уже на этом этапе: `docker-compose.yml` требует файл (`env_file: .env`) при любой команде `docker compose`.

   Ожидается: в конце вывода строки вида `Built` или `naming to docker.io/library/bginfo-bot:latest`, без `ERROR`.

   Типичные ошибки:

   - `permission denied ... /var/run/docker.sock` — у вашего пользователя нет прав на Docker: запустите команду с `sudo`.
   - `env file ... .env not found` — не выполнена команда `cp .env.example .env` или вы не в каталоге `/opt/bginfo-bot` (`cd /opt/bginfo-bot`).
   - `no space left on device` — на сервере закончилось место. Посмотрите `df -h`, освободите диск (например, `docker system prune`) и повторите сборку.

5. Получите секрет сессии. Скопируйте вывод в блокнот: он понадобится в шаге 6.

   ```bash
   docker compose run --rm admin python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   - `docker compose run --rm admin ...` запускает временный контейнер, выполняет команду и сразу удаляет контейнер (`--rm`). На работу сервисов это не влияет.
   - Команда выдаёт случайную строку около 64 символов (секрет подписи сессий).
   - Как скопировать из SSH-терминала: в Windows Terminal или PowerShell выделите текст мышью и нажмите `Ctrl+Shift+C` (или правый клик); в PuTTY выделение мышью копирует сразу, ничего нажимать не нужно.

6. Заполните `.env`.

   ```bash
   nano .env
   ```

   Заполняйте переменные строго в порядке файла. У каждой: что это, откуда взять, как вставить, обязательность.

   **`TELEGRAM_BOT_TOKEN`** — токен бота, обязательно.

   - Откуда: в Telegram откройте [@BotFather](https://t.me/BotFather), отправьте `/newbot`, введите имя бота, затем username (он должен оканчиваться на `bot`, например `bginfo_test_bot`). BotFather ответит токеном вида `123456789:AA...`.
   - Как вставить: после `=`, без кавычек и пробелов.
   - Нужен именно **тестовый** бот. Боевой `@bginfosu_bot` — только в части D.

   **`CONSULT_CHAT_ID`** — чат, куда приходят заявки на консультацию, по желанию.

   - Личный id: напишите [@userinfobot](https://t.me/userinfobot), он ответит `Id: 123456789`. Вставьте число.
   - Важно: получатель должен сам нажать `/start` у вашего тестового бота, иначе бот не сможет ему писать.
   - Группа: добавьте бота в группу; id группы отрицательный (`-100...`). Узнать его можно, переслав сообщение из группы в @userinfobot, или найти в логах бота.
   - Можно оставить пустым: тогда кнопка консультации ответит, что она не настроена.

   **`OPENROUTER_API_KEY`** — ключ для ИИ-разбора свободного текста, по желанию.

   - Откуда: на <https://openrouter.ai> нажмите Sign in, затем откройте [Keys](https://openrouter.ai/keys) → Create Key. Скопируйте ключ `sk-or-v1-...` (показывается один раз) и пополните баланс на [Credits](https://openrouter.ai/settings/credits).
   - Ориентир стоимости: около $0.001 за анкету; дневной бюджет задаётся в `config.ini`, секция `[limits]`, параметр `daily_budget_usd`.
   - Без ключа бот работает только кнопками.

   **`ADMIN_USERNAME`** — логин админки, обязательно. Любой логин латиницей, например `admin`.

   **`ADMIN_PASSWORD`** — пароль входа в админку, обязательно.

   - Придумайте любой не пустой пароль и впишите после `=` как есть, без кавычек. Запомните его.
   - Админка сама хеширует пароль при запуске.

   **`ADMIN_SESSION_SECRET`** — случайная строка из шага 5, обязательно. Без кавычек.

   **`ADMIN_COOKIE_SECURE`** — `false`, если заходите в админку по http (напрямую по `http://IP_СЕРВЕРА:8091` или через SSH-туннель, как в этой инструкции). За Caddy с HTTPS ставят `true` (см. [DEPLOY.md](DEPLOY.md)).

   Адрес и порт админки в `.env` не задаются: они в `config.ini`, секция `[admin]` (`host = 0.0.0.0`, `port = 8091`).

   **`DB_PATH`** — не меняйте: в Docker `docker-compose.yml` задаёт `/data/app.db`.

   **`APP_MODE`** — `production`.

   Полный образец заполненного `.env` (все значения вымышленные):

   ```ini
   # --- Telegram ---
   # Токен бота от @BotFather
   TELEGRAM_BOT_TOKEN=123456789:AAEhBOweik6ad9r_QXjfsbxSb5cpWk3dEa0
   # Чат/пользователь Telegram, куда уходят заявки на консультацию (числовой id)
   CONSULT_CHAT_ID=987654321

   # --- OpenRouter (JEV и объяснения) ---
   # Ключ: https://openrouter.ai/keys
   OPENROUTER_API_KEY=sk-or-v1-0123456789abcdef0123456789abcdef0123456789abcdef

   # --- Админка ---
   ADMIN_USERNAME=admin
   # Пароль входа в админку (любой, не пустой)
   ADMIN_PASSWORD=MyAdminPassword
   # Длинная случайная строка для подписи сессий
   ADMIN_SESSION_SECRET=Kq8v2mP0xR7tYw3nZ5bC1dF9gH4jL6sA8eU0iO2pQ4rT6yV8wX1zB3cN5mD7fG9h
   # true при работе за HTTPS; false при входе по http (по IP сервера, SSH-туннель, локально)
   ADMIN_COOKIE_SECURE=false

   # --- Прочее ---
   DB_PATH=data/app.db
   # production | demo | test
   APP_MODE=production
   ```

   Работа в nano:

   - Перемещайтесь по строкам стрелками.
   - Вставка из буфера: правый клик или `Ctrl+Shift+V` (Windows Terminal), `Shift+Insert` (PuTTY).
   - Сохранить: `Ctrl+O`, затем `Enter`. Выйти: `Ctrl+X`.

   Нельзя:

   - ставить пробелы вокруг `=` (верно: `ADMIN_USERNAME=admin`);
   - добавлять кавычки к значениям;
   - переносить строку внутри значения: каждое значение в одной строке.

   Секреты (токен, ключ, пароль админки, секрет сессии, содержимое `.env`) никому не пересылайте и не вставляйте в чаты.

7. Проверьте `.env`.

   ```bash
   docker compose run --rm admin python -m bginfo.ops.check_env
   ```

   Команда показывает, что именно видит приложение, и не печатает значения секретов. Ожидаемый вывод при правильной настройке:

   ```text
   TELEGRAM_BOT_TOKEN: задан, формат OK
   OPENROUTER_API_KEY: задан (начинается с sk-or-)
   ADMIN_USERNAME: admin
   ADMIN_PASSWORD: задан
   ADMIN_SESSION_SECRET: OK
   ADMIN_COOKIE_SECURE: false
   CONSULT_CHAT_ID: задан (число)
   DB_PATH: /data/app.db
   APP_MODE: production
   Итог: всё готово
   ```

   Что делать при пометках:

   - `TELEGRAM_BOT_TOKEN: ПУСТО` — токен не вставлен: вернитесь к `TELEGRAM_BOT_TOKEN` в шаге 6.
   - `TELEGRAM_BOT_TOKEN: ... похоже на ошибку формата` — токен скопирован не целиком или с лишними символами/кавычками; скопируйте у @BotFather заново.
   - `OPENROUTER_API_KEY: пусто — бот будет работать только кнопками` — не ошибка; вставьте ключ, если нужен ИИ-разбор.
   - `ADMIN_PASSWORD: ПУСТО (обязательно)` — пароль админки не задан: впишите его после `ADMIN_PASSWORD=` в шаге 6.
   - `ADMIN_SESSION_SECRET: короче 32 символов` — вставьте строку из шага 5 целиком.
   - `CONSULT_CHAT_ID: пусто — заявки на консультацию отключены` — не ошибка; заполните, если нужны заявки.

   Если итог `Итог: исправьте отмеченное` — поправьте `.env` (`nano .env`) и повторите проверку, пока не будет `Итог: всё готово` (код выхода 0).

8. Запустите сервисы.

   ```bash
   docker compose up -d --build
   docker compose ps
   ```

   Ожидается: сервисы `bot` и `admin` в состоянии `Up`, у `admin` со временем `(healthy)`.

9. Загрузите черновики рабочей базы знаний (выпуск не публикуется).

   ```bash
   docker compose run --rm bot python -m bginfo.seed seed-draft
   ```

   Ожидается: `Черновик заполнен: /data/app.db. Выпуск не опубликован — одобрение и публикация за владельцем.` Если черновик уже не пуст, команда откажется (замена — только с `--force`).

10. Откройте админку в браузере: `http://IP_СЕРВЕРА:8091` (вместо `IP_СЕРВЕРА` — адрес вашего сервера). Если снаружи не открывается, откройте порт на сервере:

   ```bash
   sudo ufw allow 8091/tcp
   ```

   Админка защищена паролем, CSRF и ограничением попыток входа (5 за 15 минут). Сменить доступ можно на странице «Доступ к админке» (меню «Ещё») или правкой `config.ini` и `docker compose restart admin`.

   Если нужен доступ только через SSH-туннель (`host = 127.0.0.1` в `config.ini`), на **своём компьютере** (окно оставьте открытым) выполните и откройте <http://127.0.0.1:8080>:

   ```powershell
   ssh -L 8080:127.0.0.1:8091 root@IP_СЕРВЕРА
   ```

11. Проверьте админку (на сервере).

    ```bash
    curl -s http://127.0.0.1:8091/healthz
    ```

    Ожидается: `{"ok":true}`. Откройте админку (шаг 10) и войдите с логином (`ADMIN_USERNAME`) и паролем из шага 5.

12. Одобрите и опубликуйте базу. Два способа, достаточно одного:

    - в админке на главной нажмите «Одобрить всё и опубликовать», впишите своё имя, поставьте галочку и подтвердите;
    - или командой на сервере:

    ```bash
    cd /opt/bginfo-bot
    docker compose run --rm bot python -m bginfo.seed approve-and-publish --by "Ваше имя"
    ```

    Ожидается вывод: `База одобрена и опубликована: версия №1.`, затем `Опубликовано: N — …` и `Требует проверки (бот предложит индивидуальный разбор): M — …`. Условия, которые не подтверждены официальным источником, бот не считает надёжными и предлагает по ним индивидуальный разбор. Правила — [KB_MANUAL_UPDATE.md](KB_MANUAL_UPDATE.md), пробелы в данных — [KB_GAPS.md](KB_GAPS.md). После этого `/start` показывает приветствие; до публикации бот отвечает «сервис готовится».

13. Проверьте бота: в Telegram откройте тестового бота и отправьте `/start`.

    Ожидается: после публикации выпуска приветствие с кнопками; до публикации — ответ «сервис готовится».

14. Смотрите логи при необходимости (на сервере; выход — `Ctrl+C`).

    ```bash
    cd /opt/bginfo-bot
    docker compose logs -f bot
    ```

    Строка `A webhook is configured for this bot` означает, что у бота настроен webhook и он остановился (см. [SWITCHOVER.md](SWITCHOVER.md)).

15. Сделайте разовый бэкап.

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
curl -s http://127.0.0.1:8091/healthz   # порт: [admin] port в config.ini
```

Ожидается: `bot`, `admin`, `webapp` в состоянии `Up`, `admin` и `webapp` `(healthy)`, ответ `{"ok":true}`. Если в `.env.example` добавились переменные, внесите их в серверный `.env` до `up -d` (проверка в [DEPLOY.md](DEPLOY.md), раздел 9). Перед обновлением полезен разовый бэкап (часть B, шаг 15).

---

## Часть C2. Мини-апп Telegram (форма) на form.bginfo.eu

Новых переменных `.env` нет. Коротко: nginx — веб-сервер на хосте (держит порты 80/443), certbot — получает бесплатный сертификат Let's Encrypt и вписывает его в nginx, Caddy — альтернативный веб-сервер, нужен только если своего веб-сервера на 80/443 нет. DNS (A-запись `form.bginfo.eu` -> `IP_СЕРВЕРА`) уже работает.

1. Выкатите и поднимите HTTPS через nginx хоста (по порядку):

   ```bash
   cd /opt/bginfo-bot && git pull
   sudo docker compose --profile ops run --rm backup
   sudo docker compose up -d --build
   curl -s http://127.0.0.1:8092/healthz
   sudo cp deploy/nginx/form.bginfo.eu.conf /etc/nginx/sites-available/form.bginfo.eu
   sudo ln -sf /etc/nginx/sites-available/form.bginfo.eu /etc/nginx/sites-enabled/form.bginfo.eu
   sudo nginx -t && sudo systemctl reload nginx
   certbot --version || sudo apt-get install -y certbot python3-certbot-nginx
   sudo certbot --nginx -d form.bginfo.eu --redirect
   curl -sI https://form.bginfo.eu | head -1
   ```

   Ожидается: `{"ok":true}` и `HTTP/2 200`. В боте появится кнопка меню «Анкета». Certbot продлевает сертификат сам (systemd-таймер); проверка: `sudo certbot renew --dry-run`.

2. Альтернатива только для сервера со свободными 80/443 (`sudo ss -ltnp | grep -E ':(80|443)b'` выводит пусто): `sudo docker compose --profile caddy up -d caddy`. Подробности: [DEPLOY.md](DEPLOY.md), пункт 7a.

3. Локально (Windows, PowerShell, venv активирован) API мини-аппа запускается так:

   ```powershell
   python -m bginfo.webapp
   ```

   Ожидается: сервер на `http://127.0.0.1:8092`, `http://127.0.0.1:8092/healthz` отвечает `{"ok":true}`. Telegram открывает мини-аппы только по HTTPS, поэтому интерфейс локально смотрится через HTTPS-туннель (например, `cloudflared tunnel --url http://127.0.0.1:8092`, его адрес временно вписать в `[webapp] public_url`); без туннеля проверяйте API тестами: `pytest tests/test_webapp_api.py`.

---

## Часть D. Переключение на @bginfosu_bot

Только с разрешения владельца и строго по [SWITCHOVER.md](SWITCHOVER.md); два приложения с одним токеном не запускать.
