"""Отчёт «кто и когда запускал бота»: одна строка на сессию, с пагинацией (только чтение)."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta

from bginfo.db.conn import iso, utcnow
from bginfo.stats.queries import ABANDON_AFTER_HOURS


def launches(conn: sqlite3.Connection, page: int, per_page: int = 50, since: datetime | None = None) -> dict:
    """Страница запусков, новые сверху.

    Возвращает {"rows": [...], "total": int, "page": int (после ограничения 1..pages), "pages": int (>=1),
    "per_page": int}. Строка: session_id, started_at (UTC iso), tg_user_id, username, first_name, last_name,
    state ("result" | "in_progress" | "abandoned"), results_count, last_result_at, result_kind,
    primary_ground, primary_status, release_id (последний результат сессии; при его отсутствии None),
    details_source ("result" | "session_view" | "missing").

    Детали результата (result_kind/primary_*) берутся из последней записи results. У старых результатов
    (до миграции 003) они NULL; тогда пробуем sessions.ui -> last_view (последний показанный результат анкеты,
    вид view_to_dict: kind, primary.ground_id, primary.status) и ставим details_source="session_view".
    Если и его нет — "missing". Запрос в два шага: страница сессий, затем результаты только для её id
    (по индексу ix_results_session), без обхода всей таблицы results.
    """
    per_page = max(1, int(per_page))
    s = iso(since) if since else ""
    total = conn.execute("SELECT COUNT(*) FROM sessions WHERE created_at >= ?", (s,)).fetchone()[0]
    pages = max(1, math.ceil(total / per_page))
    page = min(max(1, int(page)), pages)
    fresh_after = iso(utcnow() - timedelta(hours=ABANDON_AFTER_HOURS))
    sess = conn.execute(
        """
        SELECT s.id AS session_id, s.created_at AS started_at, s.tg_user_id,
               u.username, u.first_name, u.last_name, s.status AS _status, s.updated_at AS _updated, s.ui AS _ui
          FROM sessions s
          LEFT JOIN users u ON u.tg_user_id = s.tg_user_id
         WHERE s.created_at >= ?
         ORDER BY s.created_at DESC, s.id DESC
         LIMIT ? OFFSET ?
        """,
        (s, per_page, (page - 1) * per_page),
    ).fetchall()
    ids = [r["session_id"] for r in sess]
    counts: dict[int, tuple[int, int]] = {}
    last: dict[int, sqlite3.Row] = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for c in conn.execute(
            f"SELECT session_id, COUNT(*) n, MAX(id) last_id FROM results WHERE session_id IN ({ph}) GROUP BY session_id",
            ids,
        ):
            counts[c["session_id"]] = (c["n"], c["last_id"])
        last_ids = [v[1] for v in counts.values()]
        if last_ids:
            ph2 = ",".join("?" * len(last_ids))
            for r in conn.execute(
                "SELECT id, created_at, result_kind, primary_ground, primary_status, release_id "
                f"FROM results WHERE id IN ({ph2})",
                last_ids,
            ):
                last[r["id"]] = r
    rows = []
    for r in sess:
        n, last_id = counts.get(r["session_id"], (0, None))
        res = last.get(last_id)
        if n > 0:
            state = "result"
        elif r["_status"] == "active" and r["_updated"] >= fresh_after:
            state = "in_progress"
        else:
            state = "abandoned"
        kind = ground = status = None
        source = "missing"
        if res is not None and res["result_kind"] is not None:
            kind, ground, status, source = res["result_kind"], res["primary_ground"], res["primary_status"], "result"
        elif n > 0:
            view = _last_view(r["_ui"])
            if view is not None:
                kind, ground, status = view
                source = "session_view"
        rows.append({
            "session_id": r["session_id"], "started_at": r["started_at"], "tg_user_id": r["tg_user_id"],
            "username": r["username"], "first_name": r["first_name"], "last_name": r["last_name"],
            "state": state, "results_count": n,
            "last_result_at": res["created_at"] if res is not None else None,
            "result_kind": kind, "primary_ground": ground, "primary_status": status,
            "release_id": res["release_id"] if res is not None else None,
            "details_source": source,
        })
    return {"rows": rows, "total": total, "page": page, "pages": pages, "per_page": per_page}


def _last_view(raw: str | None) -> tuple[str, str | None, str | None] | None:
    """(kind, primary ground_id, primary status) из sessions.ui -> last_view; None, если его нет/он битый."""
    try:
        view = (json.loads(raw or "{}") or {}).get("last_view")
    except (ValueError, AttributeError):
        return None
    if not isinstance(view, dict) or not view.get("kind"):
        return None
    primary = view.get("primary")
    if not isinstance(primary, dict):
        primary = {}
    status = primary.get("status")
    status = getattr(status, "value", status)
    return str(view["kind"]), primary.get("ground_id"), status
