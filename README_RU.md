# BGINFO bot (@bginfosu_bot)

Telegram-бот предварительного подбора оснований для проживания в Болгарии и веб-админка базы знаний.
Бот задаёт короткую анкету (кнопками или свободным текстом), сверяет ответы с правилами из базы знаний
и показывает, какие основания для ВНЖ/ПМЖ предварительно подходят, что нужно уточнить и что для каждого основания надо сделать.

> Это предварительный подбор по сообщённым данным, а не юридическое решение и не гарантия решения ведомства.

Английская версия (основная): [README.md](README.md).

## Что делает проект

- **Бот** (`python -m bginfo.bot`) ведёт анкету в личном чате: гражданство, занятость, семья, жильё, планы. Ответы можно давать кнопками или своими словами.
- **Движок** по правилам базы знаний считает статус каждого основания: «Предварительно подходит», «Нужно уточнить», «Возможно после выполнения условия», «Не подходит по указанным данным», «Нужен индивидуальный разбор».
- **Админка** (`python -m bginfo.admin`) нужна владельцу для ручного ведения базы знаний: основания, общие показатели (суммы), факты, вопросы, источники; проверка, тестовая анкета, публикация, история и откат.
- База знаний обновляется только вручную через админку. Автоматического чтения сайтов и обновления правил нет.

## Стек

Python 3.12, aiogram 3 (Telegram, long polling), FastAPI + Jinja2 + uvicorn (админка), SQLite (WAL), pydantic / pydantic-settings,
httpx (OpenRouter), argon2-cffi (пароль админки), Docker Compose (развёртывание на VPS Ubuntu). Тесты: pytest, pytest-asyncio; линтер: ruff.

## Архитектура

### Роли кода, JEV и генеративной модели

| Кто | За что отвечает | Чего не делает |
|---|---|---|
| **Код** (`engine/`, `kb/`) | Все юридически значимые решения: статусы оснований, условия, суммы, сроки, ссылки, тексты правил. Всё берётся только из базы знаний и кода. | — |
| **JEV** (`ai/interpret.py`) | Разбирает свободный текст человека на атомарные «да / нет / не сказано / противоречие» (typed-вопросы `choice`). Распознанное только предвыбирается и подтверждается человеком. | Не принимает решений, не исключает основания. «Явно нет» тоже требует подтверждения. |
| **Генеративная модель** (`ai/explain.py`, `ai/interpret.py`) | (1) вытаскивает названные суммы из текста в строгую JSON-схему; (2) пишет персональное «почему» для уже посчитанных оснований. | Не меняет статусы, не добавляет основания, суммы, сроки, ссылки; проверяется программно, при сомнении подставляется шаблон. |

Подробно о запросах: [PROMPTS.md](PROMPTS.md).

### Версии базы знаний

- Работа идёт в **черновике**. Публикация создаёт неизменяемый **выпуск** (release) со временем вступления в силу (сразу или по дате).
- Бот при каждом расчёте берёт выпуск, действующий на текущий момент; выпуск с будущей датой начинает действовать сам, без перезапуска.
- Откат создаёт новый выпуск с содержимым старого, история не переписывается.
- Публикация невозможна, пока есть ошибки проверки: основание «опубликовано» требует подтверждённых требований с источниками, даты и автора проверки, проверенных значений показателей.
- Начальное наполнение — **черновики** с `verified=false` (см. [docs/KB_GAPS.md](docs/KB_GAPS.md)). Демо-выпуск существует только в отдельной `data/demo.db`; production-бот отказывается работать на демо-выпуске.

### Два сервиса

- `bot` — получает обновления Telegram long polling-ом (без webhook), хранит анкеты, вызывает OpenRouter.
- `admin` — веб-интерфейс. По умолчанию открыт напрямую: `http://IP_СЕРВЕРА:8091` (`[admin] host = 0.0.0.0`, `port = 8091` в `config.ini`; в Docker `network_mode: host`). Защита: вход по паролю, CSRF, ограничение попыток (5 за 15 минут). Если снаружи не открывается: `sudo ufw allow 8091/tcp`. Чтобы пускать только через SSH-туннель, поставьте `host = 127.0.0.1` (страница админки «Доступ к админке» или правка `config.ini` и `docker compose restart admin`), затем: `ssh -L 8080:127.0.0.1:8091 пользователь@IP_СЕРВЕРА` и откройте `http://127.0.0.1:8080`.
- Оба используют одну SQLite-базу (volume `/data` в Docker). Миграции применяются автоматически при старте.

### Telegram Mini App (анкета в виде формы)

Та же анкета и тот же расчёт, что в боте, но в виде экранов формы внутри Telegram. Сервер прогоняет тот же `engine.dialog.Dialog`, клиент (`src/bginfo/webapp/static/`, чистые HTML/CSS/JS без сборки) только рисует экраны. Сессия общая с ботом: начатое в чате продолжается в форме и наоборот. Результат показывается в форме и копией приходит в личный чат пользователя с ботом (`[webapp] send_result_to_chat`).

- Сервисы: `webapp` (FastAPI, `python -m bginfo.webapp`, слушает `127.0.0.1:8092`, `network_mode: host`). HTTPS отдаёт обратный прокси перед ним: на боевом сервере это nginx хоста (конфиг `deploy/nginx/form.bginfo.eu.conf`) и certbot для сертификата Let's Encrypt. Сервис `caddy` в compose (`deploy/Caddyfile`) — альтернатива для серверов без своего веб-сервера, обычным `docker compose up -d` не запускается (профиль `caddy`).
- DNS: A-запись `form.bginfo.eu` -> `IP_СЕРВЕРА` уже работает.
- Выкатка с nginx хоста (шаги по порядку):

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
curl -s https://form.bginfo.eu/healthz
```

  certbot продлевает сертификат сам (systemd-таймер); проверка: `sudo certbot renew --dry-run`. Nginx — веб-сервер на хосте, certbot — получает и вписывает сертификат Let's Encrypt, Caddy — альтернативный веб-сервер. Альтернатива только при свободных портах 80/443 (`sudo ss -ltnp | grep -E ':(80|443)\b'`): `sudo docker compose --profile caddy up -d caddy`.
- `config.ini`, секция `[webapp]`: `host`, `port`, `public_url`, `init_data_ttl_hours`, `send_result_to_chat`. Новых переменных `.env` нет: подпись Telegram проверяется существующим `TELEGRAM_BOT_TOKEN`.
- Бот сам ставит кнопку меню «Анкета», если задан `public_url`, и добавляет в приветствие кнопку «Заполнить в форме». Настраивать BotFather (`/setmenubutton`) не нужно.
- Telegram открывает мини-аппы только по HTTPS, поэтому интерфейс локально можно посмотреть только через туннель (например, `cloudflared`); локально `python -m bginfo.webapp` полезен для API и тестов.

## Структура папок

```
src/bginfo/
  config.py          секреты (.env) и константы (config.ini)
  bot/               Telegram-адаптер (aiogram), хранилище анкет
  engine/            диалог, оценка условий, планировщик вопросов, формирование результата
  ai/                клиент OpenRouter, JEV-разбор текста, объяснение результата, учёт лимитов и стоимости
  kb/                модель базы знаний, проверка, выпуски (черновик/публикация/откат)
  admin/             FastAPI-админка (маршруты, шаблоны, статика)
  webapp/            Telegram Mini App: FastAPI API (проверка initData, шаги анкеты) и статический клиент
  seed/              начальное наполнение (YAML), загрузка, сборка демо-базы
  db/                подключение SQLite и миграции
  ops/               бэкап/восстановление, проверка .env
deploy/              конфиг nginx и Caddyfile (альтернатива) для мини-аппа (form.bginfo.eu)
scripts/             живые проверки OpenRouter (smoke_live.py, jev_eval.py)
tests/               тесты (pytest), набор примеров для оценки JEV (tests/jev_eval/cases.yaml)
docs/                документация (см. ниже)
config.ini           некритичные константы (модели, лимиты, срок хранения)
.env.example         шаблон секретов
Dockerfile, docker-compose.yml
PROMPTS.md           все запросы к JEV и генеративной модели
```

## Установка и активация виртуального окружения (Windows)

PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Git Bash:

```bash
py -3.12 -m venv .venv
source .venv/Scripts/activate
pip install -e ".[dev]"
```

Если PowerShell запрещает запуск скриптов: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Нужен именно Python 3.12 (`requires-python = ">=3.12,<3.13"`, как в образе `python:3.12-slim`).

## Внешние сервисы и ключи

Все секреты лежат в `.env` (в git не попадает). Шаблон — `.env.example`: скопируйте и заполните.

```powershell
Copy-Item .env.example .env
```

`.env` и `.env.example` должны оставаться структурно одинаковыми (те же переменные в том же порядке), различаются только значения. Некритичные константы живут в `config.ini`, не в `.env`.

### Telegram: `TELEGRAM_BOT_TOKEN`

1. В Telegram откройте [@BotFather](https://t.me/BotFather), команда `/newbot`, задайте имя и username (должен заканчиваться на `bot`).
2. BotFather пришлёт токен вида `123456789:AA...`. Вставьте в `.env`: `TELEGRAM_BOT_TOKEN=...`.
3. **Для разработки и проверок создайте отдельного тестового бота.** Production-токен `@bginfosu_bot` в разработке не используйте: два приложения с одним токеном конфликтуют, а боевого бота нельзя трогать до разрешённого переключения ([docs/SWITCHOVER.md](docs/SWITCHOVER.md)).
4. Бот работает только в личных чатах и использует long polling. Если у бота настроен webhook, бот при старте сообщит об этом и завершится (сам webhook он не удаляет).

Документация: <https://core.telegram.org/bots#how-do-i-create-a-bot>.

### OpenRouter: `OPENROUTER_API_KEY`

Через OpenRouter идут и JEV (разбор свободного текста), и генеративная модель (суммы и объяснение результата).

1. Зарегистрируйтесь на <https://openrouter.ai>, создайте ключ на <https://openrouter.ai/keys>.
2. Пополните баланс (Credits): вызовы платные. Дневной лимит расходов бота задаёт `[limits] daily_budget_usd` в `config.ini` (по умолчанию 2.0 USD, сутки по UTC).
   `[limits] unknown_cost_usd = 0.01` — консервативная оценка стоимости AI-вызова, если OpenRouter не вернул `usage.cost`; она учитывается в дневном бюджете и резервируется перед вызовом.
3. Вставьте ключ в `.env`: `OPENROUTER_API_KEY=...`.
4. Без ключа бот работает, но только кнопками: свободный текст не разбирается, пояснения идут по шаблону.
5. Проверить, что реально видит процесс в `.env` (значения секретов не печатаются): `python -m bginfo.ops.check_env` (в Docker: `docker compose run --rm admin python -m bginfo.ops.check_env`).

Документация: JEV — <https://openrouter.ai/docs/guides/community/jev>, запрос System One — <https://openrouter.ai/docs/api/api-reference/systemone/submit-a-system-one-request>.
Модели задаются в `config.ini` (`[ai] jev_model`, `llm_model`), см. [PROMPTS.md](PROMPTS.md).

### Админка: `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_SESSION_SECRET`, `ADMIN_COOKIE_SECURE`

Без пароля и секрета сессии админка не запустится (сообщит, чего не хватает).

- `ADMIN_USERNAME` — логин (по умолчанию `admin`).
- `ADMIN_PASSWORD` — пароль входа в админку (любой, но не пустой). Пишется как есть: `ADMIN_PASSWORD=ваш_пароль`. Админка сама хеширует его (argon2) в памяти при запуске; отдельной команды нет.
- `ADMIN_SESSION_SECRET` — случайная строка не короче 32 символов для подписи сессий:

  ```powershell
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```

- `ADMIN_COOKIE_SECURE` — `true` при работе по HTTPS (production, за Caddy), `false` при доступе по обычному `http://` (напрямую по IP сервера, локально, через SSH-туннель). Пустое значение читается как `true`.

### Остальное: `CONSULT_CHAT_ID`, `DB_PATH`, `APP_MODE`

- `CONSULT_CHAT_ID` — числовой id чата или пользователя, куда бот пересылает заявки на консультацию. Необязательно: без него кнопка «Заказать консультацию» сообщит, что онлайн-заявка не настроена (а контакт можно показывать из `config.ini` `[bot] consult_contact`).
  Как узнать id: напишите нужному аккаунту/чату боту [@userinfobot](https://t.me/userinfobot) (для личного id) или добавьте своего бота в групповой чат, отправьте там сообщение и откройте `https://api.telegram.org/bot<токен>/getUpdates` — в `message.chat.id` будет id (у групп он отрицательный). Получатель должен хотя бы раз написать боту `/start`, иначе Telegram не даст боту писать первым. Не публикуйте URL с токеном.
- `DB_PATH` — путь к файлу SQLite. Локально `data/app.db`; в Docker переопределяется на `/data/app.db` самим `docker-compose.yml`.
- `APP_MODE` — `production` | `demo` | `test`. В режиме `production` бот не стартует, если действующий выпуск помечен демо; админка при этом показывает предупреждение. Для демо-базы ставьте `demo`.

## Запуск локально

Активируйте venv (см. выше), заполните `.env`.

```powershell
# Бот (нужен ТЕСТОВЫЙ токен в .env)
python -m bginfo.bot

# Админка на http://127.0.0.1:8091 (адрес и порт — config.ini [admin])
python -m bginfo.admin

# API мини-аппа на http://127.0.0.1:8092 (config.ini [webapp]); Telegram открывает его только по HTTPS
python -m bginfo.webapp

# Черновик рабочего наполнения в рабочую базу (DB_PATH); публикация остаётся за владельцем
python -m bginfo.seed seed-draft            # --force заменяет существующий черновик

# Отдельная демо-база с опубликованным тестовым выпуском (по умолчанию data/demo.db)
python -m bginfo.seed --db data/demo.db build-demo      # --force пересоздаёт файл
```

Команда `build-demo` отказывается писать в базу, совпадающую с `DB_PATH`. Работа с демо-базой: задайте `DB_PATH=data/demo.db` и `APP_MODE=demo`, например `$env:DB_PATH = "data/demo.db"; $env:APP_MODE = "demo"` в текущей сессии PowerShell. Пошагово — [docs/QUICK_START.md](docs/QUICK_START.md).

## Статистика

Пункт меню админки **Статистика** (`/stats`) показывает за 7 / 30 / 90 дней или всё время: новых пользователей, начатые и завершённые анкеты, заявки на консультацию, ошибки бота, расходы на AI, активность по дням, где бросают анкету, итоги, как часто каждое основание проверялось первым, разрезы по признакам и самые типовые ситуации (по клику видны отдельные результаты). Данные обезличены: хранятся только категории (например, «другая страна, пенсия») без имён, сумм, свободного текста и Telegram ID, поэтому они остаются и после удаления данных пользователем.

Страница **Запуски бота** (`/stats/launches`, кнопка вверху «Статистики») показывает, кто и когда запускал бота: имя и ссылку в Telegram, состояние анкеты и подобранное основание (по 50 на страницу).

## Тесты и проверки

```powershell
pytest                 # обычные тесты; живые (marker live) по умолчанию отключены
ruff check .
```

### Живые проверки (платные)

Обращаются к настоящему OpenRouter и **тратят деньги** (десятые доли цента за прогон). Нужен ключ в переменной окружения процесса; скрипты читают `OPENROUTER_API_KEY` из окружения, а не из `.env`:

```powershell
$env:OPENROUTER_API_KEY = "ключ-из-openrouter"
python scripts/smoke_live.py            # полный путь анкеты с живыми JEV и LLM на демо-бандле в памяти, -v — подробности
python scripts/jev_eval.py              # оценка разбора текста на tests/jev_eval/cases.yaml, пишет docs/JEV_EVAL.md
```

Telegram эти скрипты не используют. Последний результат оценки — [docs/JEV_EVAL.md](docs/JEV_EVAL.md).

## Развёртывание

Репозиторий: https://github.com/Lightwell-bg/bginfosu_bot. Клонирование: `git clone https://github.com/Lightwell-bg/bginfosu_bot.git`

VPS Ubuntu + Docker Compose: [docs/DEPLOY.md](docs/DEPLOY.md). `config.ini` подключён к контейнерам томом, его правка требует только перезапуска. Docker-образ и compose-файл на машине разработки не проверялись (см. [docs/PROGRESS.md](docs/PROGRESS.md)).

## Документация

| Документ | О чём |
|---|---|
| [docs/QUICK_START.md](docs/QUICK_START.md) | Быстрый запуск на демо-базе по шагам |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Развёртывание на VPS, обновление, бэкапы, откат |
| [docs/SWITCHOVER.md](docs/SWITCHOVER.md) | Переключение @bginfosu_bot со старого приложения |
| [docs/KB_MANUAL_UPDATE.md](docs/KB_MANUAL_UPDATE.md) | Ручное обновление базы знаний через админку |
| [docs/KB_GAPS.md](docs/KB_GAPS.md) | Что проверить в карточках перед публикацией |
| [docs/PRIVACY.md](docs/PRIVACY.md) | Какие данные хранятся и куда отправляются |
| [docs/TEST_SCENARIOS.md](docs/TEST_SCENARIOS.md) | Сценарии ручной проверки на демо-базе |
| [docs/PROGRESS.md](docs/PROGRESS.md) | Состояние проекта, что проверено и что нет |
| [docs/JEV_EVAL.md](docs/JEV_EVAL.md) | Результаты оценки разбора текста JEV |
| [docs/research/KB_RESEARCH.md](docs/research/KB_RESEARCH.md) | Исследование источников для начального наполнения |
| [PROMPTS.md](PROMPTS.md) | Запросы к JEV и генеративной модели |
| [README.md](README.md) | Английская версия этого README (основная) |
