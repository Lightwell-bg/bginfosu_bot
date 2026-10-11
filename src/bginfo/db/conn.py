"""Подключение к SQLite и миграции.

Бот и админка — разные процессы над одним файлом. Поэтому WAL (читатели
не блокируют писателя), busy_timeout и короткие транзакции. Пишущие
транзакции открываются через ``write_tx`` (BEGIN IMMEDIATE): блокировка на
запись берётся сразу, без апгрейда посреди транзакции и без SQLITE_BUSY.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    """Каноничная UTC-строка: сравнивается лексикографически."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Connection(sqlite3.Connection):
    """sqlite3.Connection с местом под закэшированный путь файла БД (``db_file``)."""

    db_file: str | None = None


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False,
                           factory=Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Применить недостающие миграции из пакета bginfo.db.migrations по порядку имён."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    files = sorted(
        (f for f in resources.files("bginfo.db.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    return _apply_missing(conn, files, done)


def _apply_missing(conn: sqlite3.Connection, files: list, done: set[str]) -> list[str]:
    """Применить миграции из ``files``, которых нет в ``done``.

    Внутри write_tx наличие записи перепроверяется: другой процесс мог применить миграцию
    между нашим чтением ``done`` и взятием блокировки на запись.
    """
    applied = []
    for f in files:
        if f.name in done:
            continue
        sql = f.read_text(encoding="utf-8")
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (f.name,)).fetchone():
                continue
            # executescript делает COMMIT сам, поэтому выполняем операторы по одному
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)", (f.name, iso(utcnow())))
        log.info("migration applied: %s", f.name)
        applied.append(f.name)
    return applied


def _split_sql(sql: str) -> list[str]:
    """Разбить скрипт на операторы с учётом блоков BEGIN ... END в триггерах."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    out, buf = [], []
    for stmt in "\n".join(lines).split(";"):
        buf.append(stmt)
        joined = ";".join(buf)
        if not joined.strip():
            buf = []
            continue
        if sqlite3.complete_statement(joined + ";"):
            out.append(joined.strip())
            buf = []
    return [s for s in out if s]


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
