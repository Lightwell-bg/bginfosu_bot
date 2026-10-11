-- 003: отчёт «кто и когда запускал бота».
-- Имя из Telegram (удаляется вместе с пользователем: /delete и по сроку хранения).
ALTER TABLE users ADD COLUMN username TEXT;
ALTER TABLE users ADD COLUMN first_name TEXT;
ALTER TABLE users ADD COLUMN last_name TEXT;
-- Итог подбора прямо в results (для отчёта; уходит вместе с сессией).
ALTER TABLE results ADD COLUMN result_kind TEXT;
ALTER TABLE results ADD COLUMN primary_ground TEXT;
ALTER TABLE results ADD COLUMN primary_status TEXT;
CREATE INDEX ix_sessions_created ON sessions(created_at);
