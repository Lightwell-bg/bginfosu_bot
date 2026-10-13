# BGINFO bot (@bginfosu_bot)

A Telegram bot that gives a preliminary match of grounds for residence in Bulgaria, plus a web admin for the knowledge base.
The bot runs a short questionnaire (buttons or free text), checks the answers against the knowledge base rules
and shows which grounds for a residence permit (ВНЖ) or permanent residence (ПМЖ) fit preliminarily, what must be clarified and what to do for each ground.

> This is a preliminary match based on the data the user reported. It is not a legal decision and not a guarantee of an authority's decision.

Russian version: [README_RU.md](README_RU.md). Most documents in `docs/` and [PROMPTS.md](PROMPTS.md) are in Russian.

## What the project does

- **The bot** (`python -m bginfo.bot`) runs the questionnaire in a private chat: citizenship, employment, family, housing, plans. Answers can be given with buttons or in the user's own words.
- **The engine** computes the status of every ground from the knowledge base rules: "Fits preliminarily", "Needs clarification", "Possible after a condition is met", "Not eligible based on the data given", "Individual review needed".
- **The admin** (`python -m bginfo.admin`) is for the owner to maintain the knowledge base by hand: grounds, shared parameters (amounts), facts, questions, sources; validation, a test questionnaire, publishing, history and rollback.
- The knowledge base is updated by hand only, through the admin. There is no automatic site reading or rule updating.

## Stack

Python 3.12, aiogram 3 (Telegram, long polling), FastAPI + Jinja2 + uvicorn (admin), SQLite (WAL), pydantic / pydantic-settings,
httpx (OpenRouter), argon2-cffi (admin password), Docker Compose (deployment to an Ubuntu VPS). Tests: pytest, pytest-asyncio; linter: ruff.

## Architecture

### Roles of code, JEV and the generative model

| Who | Responsible for | Does not do |
|---|---|---|
| **Code** (`engine/`, `kb/`) | Every legally relevant decision: ground statuses, conditions, amounts, dates, links, rule texts. Everything comes only from the knowledge base and code. | - |
| **JEV** (`ai/interpret.py`) | Parses the user's free text into atomic "yes / no / not mentioned / contradiction" answers (typed `choice` questions). Recognised values are only pre-selected and must be confirmed by the user. | Makes no decisions, excludes no grounds. An "explicit no" also needs confirmation. |
| **Generative model** (`ai/explain.py`, `ai/interpret.py`) | (1) extracts named amounts from the text into a strict JSON schema; (2) writes a personal "why" for grounds that are already computed. | Does not change statuses and does not add grounds, amounts, dates or links; output is checked by code and replaced with a template on any doubt. |

Details about the requests: [PROMPTS.md](PROMPTS.md) (Russian).

### Knowledge base versions

- Work happens in a **draft**. Publishing creates an immutable **release** with an effective time (immediately or at a given date).
- On every calculation the bot uses the release in force at that moment; a release with a future date takes effect by itself, without a restart.
- A rollback creates a new release with the old content; history is never rewritten.
- Publishing is impossible while validation errors exist: a "published" ground requires confirmed requirements with sources, the date and author of the review, and verified parameter values.
- The initial content consists of **drafts** with `verified=false` (see [docs/KB_GAPS.md](docs/KB_GAPS.md)). A demo release exists only in a separate `data/demo.db`; the production bot refuses to run on a demo release.

### Two services

- `bot` receives Telegram updates by long polling (no webhook), stores questionnaires and calls OpenRouter.
- `admin` is a web interface. By default it is reachable directly at `http://SERVER_IP:8091` (`[admin] host = 0.0.0.0`, `port = 8091` in `config.ini`; in Docker it uses `network_mode: host`). Protection: password login, CSRF, login attempt limit (5 per 15 minutes). If the port is not reachable from outside: `sudo ufw allow 8091/tcp`. To restrict access to an SSH tunnel only, set `host = 127.0.0.1` (admin page "Admin access" or edit `config.ini` and run `docker compose restart admin`), then: `ssh -L 8080:127.0.0.1:8091 user@SERVER_IP` and open `http://127.0.0.1:8080`.
- Both use one SQLite database (volume `/data` in Docker). Migrations are applied automatically on start.

### Telegram Mini App (questionnaire as a form)

The same questionnaire and calculation as in the bot, shown as form screens inside Telegram. The server runs the same `engine.dialog.Dialog`, the client (`src/bginfo/webapp/static/`, plain HTML/CSS/JS, no build) only draws the screens. The session is shared with the bot: what was started in the chat continues in the form and back. The result is shown in the form and also sent as a copy to the user's private chat with the bot (`[webapp] send_result_to_chat`).

- Services: `webapp` (FastAPI, `python -m bginfo.webapp`, listens on `127.0.0.1:8092`) and `caddy` (HTTPS with a Let's Encrypt certificate, config in `deploy/Caddyfile`: `form.bginfo.eu` proxied to `127.0.0.1:8092`). Both use `network_mode: host`.
- DNS: create an A record `form.bginfo.eu` -> `37.27.13.102`.
- Ports 80 and 443 on the server must be free for Caddy. Check: `sudo ss -ltnp | grep -E ':(80|443)\b'`. If an existing nginx or another web server holds them, do not start `caddy`; add a virtual host for `form.bginfo.eu` in that server that proxies to `127.0.0.1:8092` (and run `docker compose up -d --build bot admin webapp`).
- `config.ini`, section `[webapp]`: `host`, `port`, `public_url`, `init_data_ttl_hours`, `send_result_to_chat`. No new `.env` variables: the Telegram signature check uses the existing `TELEGRAM_BOT_TOKEN`.
- The bot sets the "Анкета" menu button itself when `public_url` is set, and adds the "Fill in the form" button to the welcome message. Configuring BotFather (`/setmenubutton`) is not needed.
- Telegram opens Mini Apps only over HTTPS, so the UI can be tried locally only through a tunnel (for example `cloudflared`); locally `python -m bginfo.webapp` is useful for the API and tests.

## Folder structure

```
src/bginfo/
  config.py          secrets (.env) and constants (config.ini)
  bot/               Telegram adapter (aiogram), questionnaire storage
  engine/            dialog, condition evaluation, question planner, result building
  ai/                OpenRouter client, JEV text parsing, result explanation, limits and cost accounting
  kb/                knowledge base model, validation, releases (draft/publish/rollback)
  admin/             FastAPI admin (routes, templates, static files)
  webapp/            Telegram Mini App: FastAPI API (initData check, questionnaire steps) and static client
  seed/              initial content (YAML), loader, demo database builder
  db/                SQLite connection and migrations
  ops/               backup/restore, .env check
deploy/              Caddyfile for the Mini App (form.bginfo.eu)
scripts/             live OpenRouter checks (smoke_live.py, jev_eval.py)
tests/               tests (pytest), JEV evaluation cases (tests/jev_eval/cases.yaml)
docs/                documentation (see below)
config.ini           non-critical constants (models, limits, retention period)
.env.example         secrets template
Dockerfile, docker-compose.yml
PROMPTS.md           all requests to JEV and the generative model
```

## Installation and virtual environment activation (Windows)

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

If PowerShell blocks running scripts: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Python 3.12 is required (`requires-python = ">=3.12,<3.13"`, same as the `python:3.12-slim` image).

## External services and keys

All secrets live in `.env` (it is not committed to git). The template is `.env.example`: copy it and fill it in.

```powershell
Copy-Item .env.example .env
```

`.env` and `.env.example` must stay structurally identical (the same variables in the same order); only the values differ. Non-critical constants live in `config.ini`, not in `.env`.

### Telegram: `TELEGRAM_BOT_TOKEN`

1. In Telegram open [@BotFather](https://t.me/BotFather), send `/newbot`, choose a name and a username (it must end with `bot`).
2. BotFather sends a token like `123456789:AA...`. Put it into `.env`: `TELEGRAM_BOT_TOKEN=...`.
3. **For development and checks create a separate test bot.** Do not use the production token of `@bginfosu_bot` in development: two applications with one token conflict, and the live bot must not be touched until the switchover is allowed ([docs/SWITCHOVER.md](docs/SWITCHOVER.md)).
4. The bot works only in private chats and uses long polling. If a webhook is configured for the bot, it reports that at startup and exits (it does not delete the webhook itself).

Documentation: <https://core.telegram.org/bots#how-do-i-create-a-bot>.

### OpenRouter: `OPENROUTER_API_KEY`

OpenRouter serves both JEV (free-text parsing) and the generative model (amounts and result explanation).

1. Sign up at <https://openrouter.ai> and create a key at <https://openrouter.ai/keys>.
2. Top up the balance (Credits): calls are paid. The bot's daily spending limit is set by `[limits] daily_budget_usd` in `config.ini` (2.0 USD by default, a day is counted in UTC).
   `[limits] unknown_cost_usd = 0.01` is the conservative cost estimate for an AI call when OpenRouter does not return `usage.cost`; it counts toward the daily budget and is reserved before the call.
3. Put the key into `.env`: `OPENROUTER_API_KEY=...`.
4. Without a key the bot works, but with buttons only: free text is not parsed and explanations use a template.
5. To check what the process actually sees in `.env` (secret values are never printed): `python -m bginfo.ops.check_env` (in Docker: `docker compose run --rm admin python -m bginfo.ops.check_env`).

Documentation: JEV - <https://openrouter.ai/docs/guides/community/jev>, System One request - <https://openrouter.ai/docs/api/api-reference/systemone/submit-a-system-one-request>.
Models are set in `config.ini` (`[ai] jev_model`, `llm_model`), see [PROMPTS.md](PROMPTS.md).

### Admin: `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_SESSION_SECRET`, `ADMIN_COOKIE_SECURE`

The admin will not start without a password and a session secret (it reports what is missing).

- `ADMIN_USERNAME` - login (`admin` by default).
- `ADMIN_PASSWORD` - the admin login password (any, but not empty). Write it plainly: `ADMIN_PASSWORD=your_password`. The admin hashes it (argon2) in memory at startup; there is no separate hash command.
- `ADMIN_SESSION_SECRET` - a random string of at least 32 characters used to sign sessions:

  ```powershell
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```

- `ADMIN_COOKIE_SECURE` - `true` when served over HTTPS (production, behind Caddy), `false` for access over plain `http://` (directly by server IP, locally, through an SSH tunnel). An empty value is read as `true`.

### Other: `CONSULT_CHAT_ID`, `DB_PATH`, `APP_MODE`

- `CONSULT_CHAT_ID` - numeric id of the chat or user that receives consultation requests from the bot. Optional: without it the "Order a consultation" button says that online requests are not configured (a contact from `config.ini` `[bot] consult_contact` can be shown instead).
  How to find the id: write to [@userinfobot](https://t.me/userinfobot) from the needed account (for a personal id), or add your bot to a group chat, send a message there and open `https://api.telegram.org/bot<token>/getUpdates` - `message.chat.id` holds the id (it is negative for groups). The recipient must send `/start` to the bot at least once, otherwise Telegram does not let the bot write first. Do not publish a URL containing the token.
- `DB_PATH` - path to the SQLite file. `data/app.db` locally; in Docker it is overridden to `/data/app.db` by `docker-compose.yml` itself.
- `APP_MODE` - `production` | `demo` | `test`. In `production` mode the bot does not start if the current release is marked as demo; the admin shows a warning in that case. Use `demo` for the demo database.

## Running locally

Activate the venv (see above) and fill in `.env`.

```powershell
# Bot (needs a TEST token in .env)
python -m bginfo.bot

# Admin at http://127.0.0.1:8091 (address and port: config.ini [admin])
python -m bginfo.admin

# Mini App API at http://127.0.0.1:8092 (config.ini [webapp]); Telegram opens it only over HTTPS
python -m bginfo.webapp

# Draft of the working content into the working database (DB_PATH); publishing stays with the owner
python -m bginfo.seed seed-draft            # --force replaces an existing draft

# Separate demo database with a published test release (data/demo.db by default)
python -m bginfo.seed --db data/demo.db build-demo      # --force recreates the file
```

The `build-demo` command refuses to write to the database equal to `DB_PATH`. To work with the demo database set `DB_PATH=data/demo.db` and `APP_MODE=demo`, for example `$env:DB_PATH = "data/demo.db"; $env:APP_MODE = "demo"` in the current PowerShell session. Step by step (Russian): [docs/QUICK_START.md](docs/QUICK_START.md).

## Statistics

The admin menu item **Statistics** (`/stats`) shows, for the last 7 / 30 / 90 days or all time: new users, started and finished questionnaires, consultation requests, bot errors, AI spend, daily activity, where people drop out of the questionnaire, outcomes, how often each ground was checked first, breakdowns by user traits, and the most typical situations (click one to see its individual results). The data is anonymised: only categories (such as "other country, pension") are stored, with no names, amounts, free text or Telegram IDs, so it is kept after a user deletes their data.

The **Launches** page (`/stats/launches`, button at the top of Statistics) lists who started the bot and when, with name and Telegram link, questionnaire state and the ground picked (paged, 50 per page).

## Tests and checks

```powershell
pytest                 # regular tests; live ones (marker live) are disabled by default
ruff check .
```

### Live checks (paid)

They call the real OpenRouter and **cost money** (fractions of a cent per run). The key must be in the process environment; the scripts read `OPENROUTER_API_KEY` from the environment, not from `.env`:

```powershell
$env:OPENROUTER_API_KEY = "key-from-openrouter"
python scripts/smoke_live.py            # full questionnaire path with live JEV and LLM on an in-memory demo bundle, -v for details
python scripts/jev_eval.py              # evaluation of text parsing on tests/jev_eval/cases.yaml, writes docs/JEV_EVAL.md
```

These scripts do not use Telegram. The latest evaluation result is in [docs/JEV_EVAL.md](docs/JEV_EVAL.md).

## Deployment

Repository: https://github.com/Lightwell-bg/bginfosu_bot. Clone: `git clone https://github.com/Lightwell-bg/bginfosu_bot.git`

Ubuntu VPS + Docker Compose: [docs/DEPLOY.md](docs/DEPLOY.md). `config.ini` is mounted into the containers as a volume, so editing it only needs a container restart. The Docker image and compose file have not been verified on the development machine (see [docs/PROGRESS.md](docs/PROGRESS.md)).

## Documentation

Documents in `docs/` are written in Russian.

| Document | What it covers |
|---|---|
| [docs/QUICK_START.md](docs/QUICK_START.md) | Quick start on the demo database, step by step (Russian) |
| [docs/DEPLOY.md](docs/DEPLOY.md) | VPS deployment, updates, backups, rollback |
| [docs/SWITCHOVER.md](docs/SWITCHOVER.md) | Switching @bginfosu_bot over from the old application |
| [docs/KB_MANUAL_UPDATE.md](docs/KB_MANUAL_UPDATE.md) | Manual knowledge base update through the admin |
| [docs/KB_GAPS.md](docs/KB_GAPS.md) | What to verify in the cards before publishing |
| [docs/PRIVACY.md](docs/PRIVACY.md) | What data is stored and where it is sent |
| [docs/TEST_SCENARIOS.md](docs/TEST_SCENARIOS.md) | Manual check scenarios on the demo database |
| [docs/PROGRESS.md](docs/PROGRESS.md) | Project state, what was verified and what was not |
| [docs/JEV_EVAL.md](docs/JEV_EVAL.md) | JEV text parsing evaluation results |
| [docs/research/KB_RESEARCH.md](docs/research/KB_RESEARCH.md) | Source research for the initial content |
| [PROMPTS.md](PROMPTS.md) | Requests to JEV and the generative model |
| [README_RU.md](README_RU.md) | Russian version of this README |
