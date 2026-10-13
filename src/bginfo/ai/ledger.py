"""Лимиты и учёт стоимости AI-вызовов по таблице ai_calls.

Схема резервирования (защита от параллельного перерасхода):

1. ``reserve(user, kind, model)`` в одной транзакции ``BEGIN IMMEDIATE`` проверяет суточный бюджет и
   суточный лимит пользователя и вставляет строку ``ai_calls`` со статусом ``reserved`` (в ``cost_usd``
   консервативная оценка ``unknown_cost_usd``); параллельные вызовы видят чужие резервы.
2. ``settle(call_id, usage, ok, error)`` закрывает строку фактическими токенами и стоимостью.
   Если провайдер не вернул стоимость (NULL), строка и дальше учитывается по оценке.

``Interpreter`` и ``Explainer`` вызывают ``check`` и ``record``. Поэтому ``check`` = ``reserve`` +
запоминание ``call_id`` в ``ContextVar`` (ключ: ledger, пользователь, тип), а ``record`` закрывает его
через ``settle``. Вызовы идут последовательно внутри одной asyncio-задачи, поэтому контекст задачи надёжно
связывает пару. ``record`` без предшествующего ``check`` (старый стиль) вставляет закрытую строку сам.

Дневные лимиты пользователя считаются по ``rate_counters`` (ключ ``sha256(соль + tg_user_id)``), а не по
``ai_calls``: ``/delete`` обнуляет id в ``ai_calls``, но не должен сбрасывать лимит.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from bginfo.ai.openrouter import AiError, AiUsage
from bginfo.db.conn import iso, utcnow, write_tx

log = logging.getLogger(__name__)

# Единственные значения, которые попадают в ai_calls.error (никакого текста из ответов провайдера/AI).
ERROR_CODES = frozenset({"timeout", "http", "invalid", "budget", "limit", "disabled", "rejected"})

# (id(ledger), tg_user_id, kind) -> call_id зарезервированной строки текущей asyncio-задачи.
_PENDING: ContextVar[dict | None] = ContextVar("bginfo_ledger_pending", default=None)


def _day_start(now: datetime) -> str:
    return iso(now.astimezone(UTC).replace(hour=0, minute=0, second=0))


def _day(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%d")


def error_code(error: str) -> str:
    """Свести произвольную строку ошибки к фиксированному коду."""
    if not error:
        return ""
    return error if error in ERROR_CODES else "rejected"


class AiLedger:
    def __init__(self, conn: sqlite3.Connection, daily_budget_usd: float, per_user_daily: dict[str, int],
                 unknown_cost_usd: float = 0.01):
        self._conn = conn
        self._budget = daily_budget_usd
        self._limits = per_user_daily
        self._unknown = unknown_cost_usd
        self._salt: str | None = None
        # Собственный однопоточный executor: sqlite-вызовы ledger не блокируют event loop (async-методы
        # ``acheck``/``arecord``), а соединение ledger остаётся единственным у этого потока.
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-ledger")

    def close(self) -> None:
        """Остановить executor и закрыть соединение ledger."""
        self._exec.shutdown(wait=True)
        self._conn.close()

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._exec, fn, *args)

    # ------------------------------------------------------------------ helpers
    def _salt_value(self) -> str:
        """Случайная соль в app_meta (создаётся при первом использовании, живёт вместе с базой)."""
        if self._salt is None:
            row = self._conn.execute("SELECT value FROM app_meta WHERE key='rate_salt'").fetchone()
            if row is None:
                with write_tx(self._conn):
                    self._conn.execute("INSERT OR IGNORE INTO app_meta(key, value) VALUES ('rate_salt', ?)",
                                       (secrets.token_hex(16),))
                row = self._conn.execute("SELECT value FROM app_meta WHERE key='rate_salt'").fetchone()
            self._salt = row[0]
        return self._salt

    def user_hash(self, tg_user_id: int) -> str:
        return hashlib.sha256(f"{self._salt_value()}:{tg_user_id}".encode()).hexdigest()

    def _spent(self, since: str) -> float:
        return self._conn.execute(
            "SELECT COALESCE(SUM(COALESCE(cost_usd, ?)), 0) FROM ai_calls WHERE at >= ?", (self._unknown, since)
        ).fetchone()[0]

    def _count_in_tx(self, tg_user_id: int | None, kind: str, now: datetime, enforce: bool) -> None:
        """Проверить (enforce) и увеличить суточный счётчик пользователя. Вызывать внутри write_tx."""
        if tg_user_id is None:
            return
        uh = self.user_hash(tg_user_id)
        day = _day(now)
        if enforce:
            limit = self._limits.get(kind)
            if limit is not None:
                row = self._conn.execute(
                    "SELECT n FROM rate_counters WHERE user_hash=? AND day=? AND kind=?", (uh, day, kind)
                ).fetchone()
                if row is not None and row[0] >= limit:
                    raise AiError("limit", f"user daily limit for {kind}")
        self._conn.execute(
            "INSERT INTO rate_counters(user_hash, day, kind, n) VALUES (?,?,?,1)"
            " ON CONFLICT(user_hash, day, kind) DO UPDATE SET n = n + 1",
            (uh, day, kind),
        )

    # ------------------------------------------------------------------ reserve / settle
    def reserve(self, tg_user_id: int | None, kind: str, model: str) -> int:
        """Атомарно проверить бюджет и лимит и зарезервировать вызов. AiError("budget"/"limit")."""
        now = utcnow()
        self._salt_value()  # соль создаётся вне транзакции резерва
        with write_tx(self._conn):
            if self._spent(_day_start(now)) >= self._budget:
                raise AiError("budget", "daily budget exhausted")
            self._count_in_tx(tg_user_id, kind, now, enforce=True)
            cur = self._conn.execute(
                "INSERT INTO ai_calls(at, tg_user_id, kind, model, ok, error, status, cost_usd)"
                " VALUES (?,?,?,?,0,'','reserved',?)",
                (iso(now), tg_user_id, kind, model, self._unknown),
            )
            return cur.lastrowid

    def settle(self, call_id: int, usage: AiUsage | None, ok: bool, error: str = "",
               model: str | None = None) -> None:
        """Закрыть резерв фактическими значениями. Стоимость NULL остаётся оценкой при подсчёте бюджета."""
        with write_tx(self._conn):
            self._conn.execute(
                "UPDATE ai_calls SET status='done', ok=?, error=?, model=COALESCE(?, model), input_tokens=?,"
                " output_tokens=?, cost_usd=?, latency_ms=? WHERE id=? AND status='reserved'",
                (
                    1 if ok else 0,
                    error_code(error),
                    (usage.model if usage and usage.model else model) or None,
                    usage.input_tokens if usage else 0,
                    usage.output_tokens if usage else 0,
                    usage.cost_usd if usage else None,
                    usage.latency_ms if usage else 0,
                    call_id,
                ),
            )

    # ------------------------------------------------------------------ совместимый API: check / record
    def check(self, tg_user_id: int | None, kind: str) -> None:
        """Проверка + резерв (см. модульный docstring). AiError("budget") / AiError("limit")."""
        self._remember(tg_user_id, kind, self.reserve(tg_user_id, kind, ""))

    def _remember(self, tg_user_id: int | None, kind: str, call_id: int) -> None:
        pending = _PENDING.get()
        if pending is None:
            pending = {}
            _PENDING.set(pending)
        pending[(id(self), tg_user_id, kind)] = call_id

    async def acheck(self, tg_user_id: int | None, kind: str) -> None:
        """``check`` без блокировки event loop: БД-вызов идёт в executor ledger, ``call_id`` запоминается
        в контексте вызывающей задачи (executor контекст не наследует)."""
        self._remember(tg_user_id, kind, await self._run(self.reserve, tg_user_id, kind, ""))

    async def arecord(self, tg_user_id: int | None, kind: str, model: str, usage: AiUsage | None, ok: bool,
                      error: str = "") -> None:
        """``record`` без блокировки event loop (см. ``acheck``)."""
        call_id = self._take(tg_user_id, kind)
        await self._run(self._finish, call_id, tg_user_id, kind, model, usage, ok, error)

    def _take(self, tg_user_id: int | None, kind: str) -> int | None:
        pending = _PENDING.get()
        return pending.pop((id(self), tg_user_id, kind), None) if pending else None

    def record(
        self,
        tg_user_id: int | None,
        kind: str,
        model: str,
        usage: AiUsage | None,
        ok: bool,
        error: str = "",
    ) -> None:
        self._finish(self._take(tg_user_id, kind), tg_user_id, kind, model, usage, ok, error)

    def _finish(self, call_id: int | None, tg_user_id: int | None, kind: str, model: str,
                usage: AiUsage | None, ok: bool, error: str = "") -> None:
        if call_id is not None:
            self.settle(call_id, usage, ok, error, model)
            return
        now = utcnow()
        self._salt_value()
        with write_tx(self._conn):
            self._count_in_tx(tg_user_id, kind, now, enforce=False)
            self._conn.execute(
                "INSERT INTO ai_calls(at, tg_user_id, kind, model, ok, error, status, input_tokens, output_tokens,"
                " cost_usd, latency_ms) VALUES (?,?,?,?,?,?,'done',?,?,?,?)",
                (
                    iso(now),
                    tg_user_id,
                    kind,
                    (usage.model if usage and usage.model else model),
                    1 if ok else 0,
                    error_code(error),
                    usage.input_tokens if usage else 0,
                    usage.output_tokens if usage else 0,
                    usage.cost_usd if usage else None,
                    usage.latency_ms if usage else 0,
                ),
            )

    def stats(self, days: int = 30) -> list[dict]:
        """Статистика по UTC-дням (новые первыми): вызовы, ошибки, стоимость, токены."""
        since = _day_start(utcnow() - timedelta(days=max(days, 1) - 1))
        rows = self._conn.execute(
            "SELECT substr(at, 1, 10) AS day, COUNT(*) AS calls, SUM(CASE WHEN status='done' THEN 1 - ok ELSE 0 END) AS errors,"
            " COALESCE(SUM(CASE WHEN status='done' THEN cost_usd END), 0) AS cost_usd,"
            " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens"
            " FROM ai_calls WHERE at >= ? GROUP BY day ORDER BY day DESC",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
