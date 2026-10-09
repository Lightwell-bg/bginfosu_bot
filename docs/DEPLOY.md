# Развёртывание на VPS (Ubuntu + Docker Compose)

Целевая схема: VPS Ubuntu, Docker Compose, два сервиса (`bot` и `admin`) с общим томом `/data`, Telegram long polling без webhook. Админка по умолчанию слушает только `127.0.0.1:8080` на сервере.

> Docker-сборка и запуск на машине разработки не проверялись (Docker там не установлен). Первый запуск на сервере — это первая проверка образа; см. [PROGRESS.md](PROGRESS.md).

> Если на сервере уже работает старое приложение с токеном `@bginfosu_bot`, **не запускайте** новый `bot` с боевым токеном. Сначала проведите проверку на тестовом боте, затем переключение по [SWITCHOVER.md](SWITCHOVER.md) — только с разрешения владельца.

Все команды ниже выполняются на сервере по SSH с вашим пользователем (вход по ключу), если не сказано иное.

## 1. Установка Docker (официальный репозиторий)

По инструкции <https://docs.docker.com/engine/install/ubuntu/>:

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

Выйдите и войдите по SSH снова (чтобы группа `docker` применилась), затем проверьте:

```bash
docker --version && docker compose version && docker run --rm hello-world
```

## 2. Каталог и код

```bash
sudo mkdir -p /opt/bginfo-bot
sudo chown "$USER":"$USER" /opt/bginfo-bot
cd /opt/bginfo-bot
```

Получите код из GitHub (репозиторий публичный, ключ доступа не нужен):

```bash
git clone https://github.com/Lightwell-bg/bginfosu_bot.git .
git log --oneline -1
```

Для отката кода (раздел 11) каждую выкатываемую версию помечайте тегом. Это делается на вашем компьютере, в папке проекта, перед выкатом:

```powershell
git tag -a v0.1.0 -m "первый выпуск"
git push origin main --tags
```

## 3. `.env` на сервере

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Заполните (подробности — в [README_RU.md](../README_RU.md), раздел «Внешние сервисы и ключи»):

- `TELEGRAM_BOT_TOKEN` — токен бота. Боевой токен ставьте только на этапе переключения.
- `OPENROUTER_API_KEY` — ключ OpenRouter.
- `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` (в одинарных кавычках: `ADMIN_PASSWORD_HASH='$argon2id$...'`), `ADMIN_SESSION_SECRET` (от 32 символов).
- `CONSULT_CHAT_ID` — по желанию.
- `APP_MODE=production`. `DB_PATH` в Docker не имеет значения: `docker-compose.yml` задаёт `/data/app.db`.
- `ADMIN_COOKIE_SECURE`: `false` при доступе через SSH-туннель (раздел 6), `true` при доступе по HTTPS через Caddy (раздел 7).

Хеш пароля и секрет можно сгенерировать на сервере после первой сборки образа:

```bash
docker compose run --rm admin python -m bginfo.ops.hash_password
docker compose run --rm admin python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`config.ini` подключён к контейнерам `bot` и `admin` томом `./config.ini:/app/config.ini:ro` (копия в образе остаётся значением по умолчанию). Правка `config.ini` (модели, лимиты, срок хранения, `trust_proxy`) не требует пересборки: достаточно перезапустить контейнеры:

```bash
docker compose restart bot admin
```

**Адрес прослушивания админки в контейнере.** Для сервиса `admin` в `docker-compose.yml` задано `ADMIN_HOST=0.0.0.0`: эта переменная перекрывает `[admin] host` из `config.ini`, поэтому править `host` не нужно. Наружу порт открыт только на `127.0.0.1` сервера (`127.0.0.1:8080:8080`).

## 4. Сборка и запуск

```bash
cd /opt/bginfo-bot
docker compose build
docker compose up -d
docker compose ps
```

Оба сервиса имеют `restart: unless-stopped`; в логах включена ротация (3 файла по 10 МБ).

## 5. Первичное наполнение базы знаний

Загрузите начальные карточки в **черновик** (выпуск не публикуется, одобрение за владельцем):

```bash
docker compose run --rm bot python -m bginfo.seed seed-draft
```

Ожидаемый вывод: `Черновик заполнен: /data/app.db. Выпуск не опубликован...`. Если черновик уже не пуст, команда откажется; замена существующего черновика — только с `--force`.

Пока выпуска нет, бот отвечает, что база ещё не готова. Дальше владелец проверяет карточки по [KB_GAPS.md](KB_GAPS.md) и [KB_MANUAL_UPDATE.md](KB_MANUAL_UPDATE.md) и публикует выпуск в админке. Демо-выпуск в production-базу не загружать: бот с `APP_MODE=production` на нём не запустится.

## 6. Доступ к админке (по умолчанию: SSH-туннель)

На **своём компьютере**:

```powershell
ssh -L 8080:127.0.0.1:8080 user@server
```

(`user@server` — ваша обычная строка входа на сервер по ключу.) Пока сессия открыта, админка доступна на <http://127.0.0.1:8080>. В `.env` на сервере при этом `ADMIN_COOKIE_SECURE=false` (трафик идёт внутри SSH); после изменения `.env` пересоздайте сервис: `docker compose up -d admin`.

## 7. Доступ к админке снаружи (Caddy с HTTPS)

Только если нужен постоянный внешний доступ. Админка защищена паролем и ограничением попыток входа, но SSH-туннель надёжнее. Нужен домен с DNS A-записью на сервер и открытые порты 80 и 443.

Установка Caddy (<https://caddyserver.com/docs/install#debian-ubuntu-raspbian>):

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
sudo apt-get install -y caddy
```

Caddyfile (домен вводится при выполнении команды):

```bash
read -r -p "Домен админки: " ADMIN_DOMAIN
sudo tee /etc/caddy/Caddyfile > /dev/null <<EOF
${ADMIN_DOMAIN} {
    reverse_proxy 127.0.0.1:8080
}
EOF
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager | head -5
```

Затем:

1. В `.env`: `ADMIN_COOKIE_SECURE=true`.
2. В `config.ini`: в секции `[admin]` поставить `trust_proxy = true` (админка тогда берёт IP клиента из `X-Forwarded-For` для ограничения попыток входа; включать **только** за собственным прокси, иначе IP подделывается).
3. Пересобрать и перезапустить: `docker compose build && docker compose up -d`.
4. Проверка: `curl -sI "https://${ADMIN_DOMAIN}/login" | head -1` (ожидается `HTTP/2 200`). Порт 8080 наружу не открывайте.

## 8. Логи, состояние, проверка

```bash
docker compose ps                         # состояние; у admin должно быть (healthy)
docker compose logs -f bot                # логи бота (Ctrl+C — выйти)
docker compose logs --tail=100 admin      # логи админки
curl -s http://127.0.0.1:8080/healthz     # {"ok":true}
```

Healthcheck есть только у `admin` (`/healthz` внутри контейнера). Здоровье бота смотрите по логам и по ответу на `/start`. В логах нет текстов пользователей и токенов. Строка `A webhook is configured for this bot` значит, что у бота настроен webhook и он остановился (см. [SWITCHOVER.md](SWITCHOVER.md)).

Автозапуск после перезагрузки сервера: `restart: unless-stopped` в compose плюс запуск самого Docker.

```bash
sudo systemctl enable docker
systemctl is-enabled docker
```

## 9. Обновление

```bash
cd /opt/bginfo-bot
git pull
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=50 bot
```

Миграции базы применяются автоматически при старте сервисов. Новые переменные `.env` (если добавлены в `.env.example`) нужно внести в серверный `.env` до `up -d`; сверка списка переменных:

```bash
diff <(sed -E 's/=.*/=<V>/' .env) <(sed -E 's/=.*/=<V>/' .env.example)
```

Вывод должен быть пустым.

## 10. Резервное копирование

Бэкап делает консистентную копию SQLite (онлайн, безопасно для работающей базы), проверяет её (`integrity_check`) и удаляет копии старше `[privacy] backup_retention_days` (по умолчанию 14 дней). Файлы лежат в томе: `/data/backups/app-ГГГГММДД-ЧЧММСС.db`. Страховочные копии, которые делает восстановление (`app-prerestore-ГГГГММДД-ЧЧММСС.db`), лежат там же и ротируются по тому же сроку.

Разовый бэкап:

```bash
cd /opt/bginfo-bot
docker compose --profile ops run --rm backup
```

Ежедневно в 03:15 (cron текущего пользователя, состоящего в группе docker):

```bash
(crontab -l 2>/dev/null; echo '15 3 * * * cd /opt/bginfo-bot && /usr/bin/docker compose --profile ops run --rm backup >> /opt/bginfo-bot/backup.log 2>&1') | crontab -
crontab -l
```

Копирование бэкапов с сервера (бэкап на том же сервере не защитит от потери сервера). На сервере:

```bash
cd /opt/bginfo-bot
rm -rf backups-export
docker compose cp admin:/data/backups ./backups-export
ls -l backups-export
```

На своём компьютере (PowerShell; `user@server` — ваша строка входа):

```powershell
scp -r user@server:/opt/bginfo-bot/backups-export .\backups-from-server
```

Храните копии вне сервера, соблюдая срок хранения (см. ниже).

### Восстановление

1. Остановите бота и админку:

   ```bash
   cd /opt/bginfo-bot
   docker compose stop bot admin
   ```

2. Выберите файл и проверьте его:

   ```bash
   docker compose --profile ops run --rm backup ls -1 /data/backups
   read -r -p "Имя файла бэкапа: " BACKUP
   docker compose --profile ops run --rm backup python -m bginfo.ops.backup verify "/data/backups/${BACKUP}"
   ```

   Ожидается `OK`.

3. Восстановите. Источник и цель не должны совпадать (иначе команда откажется). Копия сначала разворачивается во временный файл рядом с рабочей базой и проверяется (`integrity_check`); если проверка не прошла, рабочая база не тронута. Прежняя база сохраняется в каталоге бэкапов как `app-prerestore-<время>.db` (каталог файла бэкапа или `--dir`), затем временный файл атомарно заменяет рабочую базу, а старые `-wal`/`-shm` удаляются:

   ```bash
   docker compose --profile ops run --rm backup python -m bginfo.ops.backup restore "/data/backups/${BACKUP}" --yes
   ```

4. Запустите сервисы и проверьте:

   ```bash
   docker compose up -d
   docker compose ps
   curl -s http://127.0.0.1:8080/healthz
   docker compose logs --tail=30 bot
   ```

   В админке на главной должен быть действующий выпуск.

Если нужная копия лежит на вашем компьютере, перед шагом 1 (пока `admin` работает) скопируйте её на сервер и положите в том:

```powershell
scp -r .\restore user@server:/opt/bginfo-bot/restore
```

```bash
cd /opt/bginfo-bot
docker compose cp ./restore/. admin:/data/backups/
docker compose --profile ops run --rm backup ls -1 /data/backups
```

(в папке `restore` лежат только файлы бэкапов, которые нужно загрузить).

## 11. Откат кода

```bash
cd /opt/bginfo-bot
git fetch --tags
git tag --list
```

Выберите предыдущий тег из списка и выкатите его (замените `v0.1.0` на нужный тег):

```bash
git checkout v0.1.0
docker compose build
docker compose up -d
docker compose logs --tail=30 bot
```

Чтобы вернуться на актуальную версию: `git checkout main && git pull`, затем снова `build` и `up -d`. Откат кода не откатывает базу: миграции схемы только добавляются; если новая версия изменила схему, предыдущий код с ней может не заработать. В этом случае восстановите базу из бэкапа, сделанного до обновления (поэтому перед каждым обновлением делайте разовый бэкап, раздел 10).

## 12. Откат базы знаний

Это не откат кода и не восстановление из бэкапа. В админке: «История» → строка нужного выпуска → «Откатить к этой версии». Создаётся новый выпуск с содержимым старого, он действует сразу. Подробнее — [KB_MANUAL_UPDATE.md](KB_MANUAL_UPDATE.md).

## 13. Срок хранения и ограничения

- Данные пользователей в рабочей базе: `[privacy] retention_days` (по умолчанию 90 дней с последней активности), очистка запускается ботом раз в 6 часов. Пользователь может удалить свои данные командой `/delete`.
- Бэкапы: `backup_retention_days` (по умолчанию 14 дней). **Анкета, удалённая пользователем или автоочисткой, остаётся во всех бэкапах, созданных до удаления, до их ротации.** При восстановлении из бэкапа удалённые ранее данные вернутся. Если после восстановления нужно повторить удаление, это делается вручную владельцем. Подробнее — [PRIVACY.md](PRIVACY.md).
- Бэкапы, скопированные с сервера, ротации не подвержены: удаляйте их сами в те же сроки.
