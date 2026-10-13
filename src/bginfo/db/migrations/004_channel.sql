-- 004: канал, через который идёт анкета: bot (чат) или webapp (форма-мини-апп).
ALTER TABLE sessions ADD COLUMN channel TEXT NOT NULL DEFAULT 'bot';
ALTER TABLE results ADD COLUMN channel TEXT NOT NULL DEFAULT 'bot';
