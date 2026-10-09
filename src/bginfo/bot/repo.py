"""Хранилище бота: синхронные функции над sqlite3.Connection.

Все записи идут через ``write_tx`` (BEGIN IMMEDIATE). Адаптер вызывает их в
отдельном однопоточном executor, чтобы не блокировать event loop.
Текст сообщений пользователя здесь не хранится и не логируется.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from bginfo.db.conn import iso, utcnow, write_tx
from bginfo.engine.dialog import SessionData
from bginfo.engine.facts import Profile


def _now() -> str:
    return iso(utcnow())


# --------------------------------------------------------------------------- updates


def is_update_processed(conn: sqlite3.Connection, update_id: int) -> bool:
    """Только проверка (SELECT): update уже помечен обработанным? Ничего не записывает."""
    return conn.execute("SELECT 1 FROM processed_updates WHERE update_id=?", (update_id,)).fetchone() is not None


def mark_update_in_tx(conn: sqlite3.Connection, update_id: int) -> bool:
    """Записать update_id в уже открытой транзакции; False — дубль."""
    cur = conn.execute("INSERT OR IGNORE INTO processed_updates(update_id, at) VALUES (?, ?)",
                       (update_id, _now()))
    return cur.rowcount == 1


def mark_update(conn: sqlite3.Connection, update_id: int) -> bool:
    """Записать update_id отдельной транзакцией; False — такой update уже обрабатывался (дубль)."""
    with write_tx(conn):
        return mark_update_in_tx(conn, update_id)


# --------------------------------------------------------------------------- users


def touch_user(conn: sqlite3.Connection, tg_user_id: int) -> dict:
    now = _now()
    with write_tx(conn):
        conn.execute("INSERT OR IGNORE INTO users(tg_user_id, created_at, last_seen_at) VALUES (?,?,?)",
                     (tg_user_id, now, now))
        conn.execute("UPDATE users SET last_seen_at=? WHERE tg_user_id=?", (now, tg_user_id))
        row = conn.execute("SELECT ai_notice_at FROM users WHERE tg_user_id=?", (tg_user_id,)).fetchone()
    return {"ai_notice_shown": row["ai_notice_at"] is not None}


def set_ai_notice_in_tx(conn: sqlite3.Connection, tg_user_id: int) -> None:
    conn.execute("UPDATE users SET ai_notice_at=COALESCE(ai_notice_at, ?) WHERE tg_user_id=?",
                 (_now(), tg_user_id))


def set_ai_notice(conn: sqlite3.Connection, tg_user_id: int) -> None:
    with write_tx(conn):
        set_ai_notice_in_tx(conn, tg_user_id)


def delete_user_in_tx(conn: sqlite3.Connection, tg_user_id: int) -> None:
    """Удалить пользователя в открытой транзакции; сессии, результаты и заявки уходят каскадом.

    AI-учёт обезличивается. Таблица ``rate_counters`` (хэш пользователя, суточные счётчики лимитов)
    НЕ трогается: иначе /delete сбрасывал бы дневной лимит.
    """
    conn.execute("UPDATE ai_calls SET tg_user_id=NULL WHERE tg_user_id=?", (tg_user_id,))
    conn.execute("DELETE FROM consult_requests WHERE tg_user_id=?", (tg_user_id,))
    conn.execute("DELETE FROM users WHERE tg_user_id=?", (tg_user_id,))


def delete_user(conn: sqlite3.Connection, tg_user_id: int) -> None:
    with write_tx(conn):
        delete_user_in_tx(conn, tg_user_id)


# --------------------------------------------------------------------------- sessions


def _row_to_session(row: sqlite3.Row) -> SessionData:
    return SessionData(
        id=row["id"], revision=row["revision"], state=row["state"],
        profile=Profile.from_dict(json.loads(row["profile"])),
        ui=json.loads(row["ui"]), release_id=row["release_id"],
    )


def active_session(conn: sqlite3.Connection, tg_user_id: int) -> SessionData | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE tg_user_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (tg_user_id,),
    ).fetchone()
    return _row_to_session(row) if row else None


def new_session(conn: sqlite3.Connection, tg_user_id: int) -> SessionData:
    """Новая активная сессия; прежние активные помечаются abandoned."""
    now = _now()
    profile = Profile()
    with write_tx(conn):
        conn.execute("UPDATE sessions SET status='abandoned', updated_at=? WHERE tg_user_id=? AND status='active'",
                     (now, tg_user_id))
        cur = conn.execute(
            "INSERT INTO sessions(tg_user_id, status, state, revision, profile, ui, created_at, updated_at) "
            "VALUES (?, 'active', 'start', 0, ?, '{}', ?, ?)",
            (tg_user_id, json.dumps(profile.to_dict(), ensure_ascii=False), now, now),
        )
        sid = cur.lastrowid
    return SessionData(sid, 0, "start", profile, {}, None)


def insert_session_in_tx(conn: sqlite3.Connection, tg_user_id: int, s: SessionData, revision: int) -> None:
    """Сделать ``s`` новой активной сессией (прежние активные -> abandoned); проставляет ``s.id``."""
    now = _now()
    conn.execute("UPDATE sessions SET status='abandoned', updated_at=? WHERE tg_user_id=? AND status='active'",
                 (now, tg_user_id))
    cur = conn.execute(
        "INSERT INTO sessions(tg_user_id, status, state, revision, profile, ui, release_id, created_at, updated_at)"
        " VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?)",
        (tg_user_id, s.state, revision, json.dumps(s.profile.to_dict(), ensure_ascii=False),
         json.dumps(s.ui, ensure_ascii=False), s.release_id, now, now),
    )
    s.id = cur.lastrowid


def blank_session() -> SessionData:
    """«Временная» сессия (id=None, revision=0): в БД попадёт только вместе с ``commit_step(new_session=True)``."""
    return SessionData(None, 0, "start", Profile(), {}, None)


def save_session_in_tx(conn: sqlite3.Connection, s: SessionData, expected_revision: int,
                       bump: bool = True) -> bool:
    """Записать сессию (в открытой транзакции), если её ревизия в БД всё ещё expected_revision.

    ``bump=False`` — состояние не изменилось: ревизия остаётся прежней (кнопки в уже
    отправленных сообщениях сохраняют силу), но конфликт всё равно проверяется.
    """
    cur = conn.execute(
        "UPDATE sessions SET state=?, profile=?, ui=?, release_id=?, revision=revision+?, updated_at=? "
        "WHERE id=? AND revision=? AND status='active'",
        (s.state, json.dumps(s.profile.to_dict(), ensure_ascii=False),
         json.dumps(s.ui, ensure_ascii=False), s.release_id, 1 if bump else 0, _now(),
         s.id, expected_revision),
    )
    return cur.rowcount == 1


def save_session(conn: sqlite3.Connection, s: SessionData, expected_revision: int, bump: bool = True) -> bool:
    """То же, что ``save_session_in_tx``, отдельной транзакцией."""
    with write_tx(conn):
        return save_session_in_tx(conn, s, expected_revision, bump)


# --------------------------------------------------------------------------- results, consult, events


def add_result_in_tx(conn: sqlite3.Connection, session_id: int, record: dict) -> None:
    conn.execute(
        "INSERT INTO results(session_id, release_id, as_of, evaluation, message_html, explain_source, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (session_id, record["release_id"], record["as_of"],
         json.dumps(record["evaluation"], ensure_ascii=False), record["message_html"],
         record["explain_source"], _now()),
    )


def add_result(conn: sqlite3.Connection, session_id: int, record: dict) -> None:
    with write_tx(conn):
        add_result_in_tx(conn, session_id, record)


def add_consult_in_tx(conn: sqlite3.Connection, session_id: int, tg_user_id: int, payload: str) -> int | None:
    """Записать заявку (pending) в открытой транзакции шага; None — по сессии уже есть
    pending/sent/unknown (уникальный индекс), повторно не отправлять."""
    try:
        cur = conn.execute(
            "INSERT INTO consult_requests(session_id, tg_user_id, payload, status, created_at)"
            " VALUES (?,?,?,'pending',?)",
            (session_id, tg_user_id, payload, _now()),
        )
    except sqlite3.IntegrityError:
        return None
    return cur.lastrowid


CONSULT_STATUSES = ("sent", "failed", "unknown")


def mark_consult(conn: sqlite3.Connection, consult_id: int, status: str) -> None:
    """Исход отправки: sent | failed (точно не доставлено) | unknown (возможно доставлено)."""
    if status not in CONSULT_STATUSES:
        raise ValueError(status)
    with write_tx(conn):
        conn.execute("UPDATE consult_requests SET status=?, sent_at=? WHERE id=? AND status='pending'",
                     (status, _now() if status == "sent" else None, consult_id))


def add_event_in_tx(conn: sqlite3.Connection, kind: str, session_id: int | None, detail: str = "") -> None:
    conn.execute("INSERT INTO events(at, kind, session_id, detail) VALUES (?,?,?,?)",
                 (_now(), kind, session_id, detail[:200]))


def add_event(conn: sqlite3.Connection, kind: str, session_id: int | None, detail: str = "") -> None:
    with write_tx(conn):
        add_event_in_tx(conn, kind, session_id, detail)


class _Abort(Exception):
    """Внутренний сигнал: откатить транзакцию шага и вернуть код исхода."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


def commit_step(conn: sqlite3.Connection, *, update_id: int | None, tg_user_id: int, session: SessionData,
                expected_revision: int, bump: bool, result_record: dict | None = None,
                events: list[tuple[str, str]] | None = None, ai_notice: bool = False,
                delete_user: bool = False, new_session: bool = False,
                replaces: tuple[int, int] | None = None, consult_html: str | None = None,
                info: dict | None = None) -> str:
    """Все записи одного шага диалога в ОДНОЙ транзакции.

    Порядок: пометка update_id (дубль) -> сессия (обновление с проверкой ревизии либо вставка новой) ->
    результат -> заявка на консультацию (pending) -> события -> уведомление об AI -> удаление
    пользователя. Возвращает ``"ok"``, ``"duplicate"`` или ``"conflict"``; при последних двух
    транзакция откатывается целиком, ничего не остаётся.

    ``new_session=True`` — ``session`` временная (``blank_session``): прежняя активная сессия
    помечается abandoned, новая вставляется с ревизией ``expected_revision + bump``, ``session.id``
    проставляется. ``replaces=(id, revision)`` — дополнительно проверить, что заменяемая активная сессия
    всё ещё именно такая (кнопка «Начать заново» из старого сообщения).
    ``consult_html`` — текст заявки; запись pending в той же транзакции. ``info`` (если передан)
    получает ``consult_id`` (None — заявка по сессии уже есть).
    ``update_id=None`` — шаг не привязан к update Telegram (пометка пропускается).
    """
    original_id = session.id
    try:
        with write_tx(conn):
            if update_id is not None and not mark_update_in_tx(conn, update_id):
                raise _Abort("duplicate")
            if new_session:
                if replaces is not None:
                    cur = active_session(conn, tg_user_id)
                    if cur is None or (cur.id, cur.revision) != replaces:
                        raise _Abort("conflict")
                insert_session_in_tx(conn, tg_user_id, session, expected_revision + (1 if bump else 0))
            elif not save_session_in_tx(conn, session, expected_revision, bump):
                raise _Abort("conflict")
            if result_record:
                add_result_in_tx(conn, session.id, result_record)
            if consult_html is not None:
                cid = add_consult_in_tx(conn, session.id, tg_user_id, consult_html)
                if info is not None:
                    info["consult_id"] = cid
            for kind, detail in events or ():
                add_event_in_tx(conn, kind, session.id, detail)
            if ai_notice:
                set_ai_notice_in_tx(conn, tg_user_id)
            if delete_user:
                delete_user_in_tx(conn, tg_user_id)
    except _Abort as a:
        session.id = original_id
        return a.status
    except BaseException:
        session.id = original_id
        raise
    return "ok"


# --------------------------------------------------------------------------- retention


def cleanup(conn: sqlite3.Connection, retention_days: int) -> None:
    now = utcnow()
    cutoff = iso(now - timedelta(days=retention_days))
    with write_tx(conn):
        conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM users WHERE last_seen_at < ? AND NOT EXISTS "
            "(SELECT 1 FROM sessions s WHERE s.tg_user_id = users.tg_user_id)",
            (cutoff,),
        )
        conn.execute("UPDATE ai_calls SET tg_user_id = NULL WHERE at < ?", (cutoff,))
        conn.execute("DELETE FROM processed_updates WHERE at < ?", (iso(now - timedelta(days=7)),))
        conn.execute("DELETE FROM admin_login_attempts WHERE at < ?", (iso(now - timedelta(days=30)),))
        # счётчики лимитов переживают /delete; хватает двух суток (лимит суточный, по UTC-дню)
        conn.execute("DELETE FROM rate_counters WHERE day < ?", ((now - timedelta(days=2)).strftime("%Y-%m-%d"),))
        # заявка, которую не успели отправить (падение процесса): исход неизвестен, автоповтор запрещён
        conn.execute("UPDATE consult_requests SET status='unknown' WHERE status='pending' AND created_at < ?",
                     (iso(now - timedelta(minutes=10)),))
