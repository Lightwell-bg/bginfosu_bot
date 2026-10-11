-- 002: обезличенная статистика результатов.
-- Без tg_user_id и session_id; не чистится при /delete и по сроку хранения (данные обезличены).
CREATE TABLE stats_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,                -- только дата YYYY-MM-DD в часовом поясе [bot] timezone, без времени
  release_id INTEGER,
  result_kind TEXT NOT NULL,        -- ok | none | unsupported | individual | clarify
  primary_ground TEXT,              -- id основания «проверить первым» или NULL
  primary_status TEXT,
  situation_key TEXT NOT NULL,
  situation TEXT NOT NULL,          -- JSON категорий, без сумм и текста
  grounds TEXT NOT NULL,            -- JSON {ground_id: status} для основного заявителя
  members INTEGER NOT NULL DEFAULT 0,
  explain_source TEXT NOT NULL
);
CREATE INDEX ix_stats_results_day ON stats_results(day);
CREATE INDEX ix_stats_results_key ON stats_results(situation_key, day);
