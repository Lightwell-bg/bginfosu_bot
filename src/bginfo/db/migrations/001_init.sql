-- 001: исходная схема.
-- Проект ещё не развёрнут, поэтому схему правим прямо в этом файле, отдельная миграция не нужна.
-- Если база уже создана по старой версии 001, её нужно пересоздать (данных в продакшене нет).

-- База знаний -----------------------------------------------------------------
-- Выпуск = неизменяемый согласованный набор всей базы (JSON Bundle).
-- Действующий выпуск на момент T: status='published' и effective_from <= T,
-- наибольший effective_from (при равенстве — наибольший id).
CREATE TABLE kb_releases (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    effective_from  TEXT NOT NULL,             -- UTC, 'YYYY-MM-DDTHH:MM:SSZ'
    status          TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('published', 'cancelled')),
    kind            TEXT NOT NULL CHECK (kind IN ('publish', 'rollback', 'review_flag', 'demo')),
    based_on        INTEGER REFERENCES kb_releases(id),
    comment         TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    cancelled_at    TEXT,
    cancelled_by    TEXT
);
CREATE INDEX ix_kb_releases_effective ON kb_releases(status, effective_from, id);
-- Защита от изменения опубликованного содержимого задним числом.
CREATE TRIGGER kb_releases_immutable BEFORE UPDATE OF content, content_hash, effective_from, created_at ON kb_releases
BEGIN
    SELECT RAISE(ABORT, 'kb_releases: published content is immutable');
END;
CREATE TRIGGER kb_releases_no_delete BEFORE DELETE ON kb_releases
BEGIN
    SELECT RAISE(ABORT, 'kb_releases: releases cannot be deleted');
END;

-- Черновик: рабочая копия базы, по одной строке на элемент.
CREATE TABLE kb_draft_items (
    kind        TEXT NOT NULL CHECK (kind IN ('ground', 'parameter', 'fact', 'question', 'source')),
    item_id     TEXT NOT NULL,
    data        TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    PRIMARY KEY (kind, item_id)
);
CREATE TABLE kb_draft_meta (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    revision         INTEGER NOT NULL DEFAULT 0,  -- растёт при любом изменении черновика
    demo             INTEGER NOT NULL DEFAULT 0
);
INSERT INTO kb_draft_meta (id, revision, demo) VALUES (1, 0, 0);

-- Журнал действий в админке.
CREATE TABLE audit_log (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT ''
);

-- Пользователи бота и анкеты --------------------------------------------------
CREATE TABLE users (
    tg_user_id      INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    ai_notice_at    TEXT,               -- когда показано уведомление об AI-обработке
    last_seen_at    TEXT NOT NULL
);

-- AUTOINCREMENT: id никогда не переиспользуется, кнопка из старого сообщения не оживёт на новой сессии.
CREATE TABLE sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id      INTEGER NOT NULL REFERENCES users(tg_user_id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'finished', 'abandoned')),
    state           TEXT NOT NULL DEFAULT 'start',
    revision        INTEGER NOT NULL DEFAULT 0,   -- растёт на каждом шаге; кнопки несут его
    profile         TEXT NOT NULL DEFAULT '{}',   -- JSON engine.facts.Profile
    ui              TEXT NOT NULL DEFAULT '{}',   -- JSON: текущий вопрос, участник, выбор в multi и т.п.
    release_id      INTEGER REFERENCES kb_releases(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX ix_sessions_user ON sessions(tg_user_id, status);
-- Не больше одной активной сессии на пользователя.
CREATE UNIQUE INDEX ux_sessions_one_active ON sessions(tg_user_id) WHERE status = 'active';

-- Итоговые результаты. Хранятся как исторические: при запросе актуального
-- результата создаётся новая строка.
CREATE TABLE results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    release_id      INTEGER NOT NULL REFERENCES kb_releases(id),
    as_of           TEXT NOT NULL,
    evaluation      TEXT NOT NULL,       -- JSON: статусы и трасса
    message_html    TEXT NOT NULL,       -- показанный текст
    explain_source  TEXT NOT NULL CHECK (explain_source IN ('llm', 'template')),
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_results_session ON results(session_id, id);

-- Заявки на консультацию (отправляются только по явному действию пользователя).
-- Статусы: pending (записана вместе с шагом, отправка ещё не завершена), sent, failed (точно не доставлена,
-- можно повторить), unknown (таймаут/сеть: возможно доставлена, автоповтор запрещён).
CREATE TABLE consult_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tg_user_id      INTEGER NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed', 'unknown')),
    created_at      TEXT NOT NULL,
    sent_at         TEXT
);
CREATE UNIQUE INDEX ux_consult_once ON consult_requests(session_id) WHERE status IN ('pending', 'sent', 'unknown');

-- Надёжность ------------------------------------------------------------------
CREATE TABLE processed_updates (
    update_id   INTEGER PRIMARY KEY,
    at          TEXT NOT NULL
);

-- Учёт AI: фактические usage и стоимость из ответа OpenRouter.
-- status='reserved': строка зарезервирована до вызова (cost_usd = консервативная оценка), 'done': закрыта фактом.
CREATE TABLE ai_calls (
    id              INTEGER PRIMARY KEY,
    at              TEXT NOT NULL,
    tg_user_id      INTEGER,
    kind            TEXT NOT NULL,       -- jev | explain | extract
    model           TEXT NOT NULL,
    ok              INTEGER NOT NULL,
    error           TEXT NOT NULL DEFAULT '',   -- только фиксированные коды, без текста исключений
    status          TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('reserved', 'done')),
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL,                -- NULL, если провайдер не вернул стоимость
    latency_ms      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_ai_calls_at ON ai_calls(at);
CREATE INDEX ix_ai_calls_user ON ai_calls(tg_user_id, at);

-- Суточные счётчики лимитов по пользователю. user_hash = sha256(соль + tg_user_id): id в открытом виде
-- не хранится. Таблица НЕ очищается при /delete (иначе удаление сбрасывало бы дневной лимит);
-- cleanup удаляет строки старше 2 дней.
CREATE TABLE rate_counters (
    user_hash   TEXT NOT NULL,
    day         TEXT NOT NULL,           -- UTC 'YYYY-MM-DD'
    kind        TEXT NOT NULL,
    n           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_hash, day, kind)
);

-- Служебные значения (соль для user_hash).
CREATE TABLE app_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- События для минимальной статистики (без текста пользователя).
CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- session_started | result_shown | error | consult_sent | ...
    session_id  INTEGER,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_events_kind_at ON events(kind, at);

-- Админка ---------------------------------------------------------------------
CREATE TABLE admin_login_attempts (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    ip      TEXT NOT NULL,
    ok      INTEGER NOT NULL
);
CREATE INDEX ix_login_ip_at ON admin_login_attempts(ip, at);
