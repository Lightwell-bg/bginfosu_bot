"""Версионирование базы знаний: черновик, выпуски, действующий снимок.

Модель:
- Черновик (kb_draft_items) — рабочая копия, которую правит админка.
- Выпуск (kb_releases) — неизменяемый снимок всей базы со временем вступления
  в силу. Публикация атомарна: валидация и вставка в одной транзакции
  BEGIN IMMEDIATE; бот видит либо старый, либо новый выпуск целиком.
- Действующий выпуск выбирается по текущему времени при КАЖДОМ расчёте. Выпуск
  с будущей датой начинает действовать сам, без правок и перезапуска.
- Откат — новый выпуск с содержимым старого. История не переписывается.
- Кэш разобранных выпусков ключуется (путь файла БД, id выпуска): содержимое
  выпуска неизменно, а id в разных файлах БД совпадают, поэтому путь обязателен.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta

from bginfo.db.conn import iso, utcnow, write_tx
from bginfo.kb import model as m
from bginfo.kb.validate import Issue, errors, validate

KINDS = {
    "ground": ("grounds", m.Ground, "id"),
    "parameter": ("parameters", m.Parameter, "id"),
    "fact": ("facts", m.FactDef, "key"),
    "question": ("questions", m.Question, "id"),
    "source": ("sources", m.Source, "id"),
}
PAST_TOLERANCE = timedelta(minutes=2)


class PublishError(Exception):
    def __init__(self, message: str, issues: list[Issue] | None = None):
        super().__init__(message)
        self.issues = issues or []


class ConcurrentEdit(Exception):
    """Черновик изменён другим пользователем после открытия формы."""


@dataclass(frozen=True)
class Snapshot:
    release_id: int
    effective_from: str
    bundle: m.Bundle


# --------------------------------------------------------------------------- serialization


def dump_bundle(b: m.Bundle) -> str:
    return json.dumps(b.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# --------------------------------------------------------------------------- cache


class _Cache:
    def __init__(self, size: int = 8):
        self._d: OrderedDict[tuple[str, int], m.Bundle] = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def get(self, rid: tuple[str, int], loader) -> m.Bundle:
        with self._lock:
            if rid in self._d:
                self._d.move_to_end(rid)
                return self._d[rid]
        b = loader()
        with self._lock:
            self._d[rid] = b
            while len(self._d) > self._size:
                self._d.popitem(last=False)
        return b

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


_cache = _Cache()


def _db_key(conn: sqlite3.Connection) -> str:
    """Путь файла БД соединения (PRAGMA database_list, seq 0); для соединений из ``connect`` — один раз."""
    cached = getattr(conn, "db_file", None)
    if cached is not None:
        return cached
    row = conn.execute("PRAGMA database_list").fetchone()
    key = (row[2] if row is not None else "") or f":memory:{id(conn)}"
    with contextlib.suppress(AttributeError):  # обычный sqlite3.Connection не принимает атрибуты
        conn.db_file = key
    return key


def load_release(conn: sqlite3.Connection, rid: int) -> m.Bundle:
    def loader() -> m.Bundle:
        row = conn.execute("SELECT content FROM kb_releases WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise KeyError(rid)
        return m.Bundle.model_validate_json(row["content"])

    return _cache.get((_db_key(conn), rid), loader)


def active_snapshot(conn: sqlite3.Connection, now: datetime | None = None) -> Snapshot | None:
    """Выпуск, действующий в момент ``now``. Один расчёт = один снимок."""
    t = iso(now or utcnow())
    row = conn.execute(
        "SELECT id, effective_from FROM kb_releases WHERE status='published' AND effective_from<=? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1",
        (t,),
    ).fetchone()
    if row is None:
        return None
    return Snapshot(row["id"], row["effective_from"], load_release(conn, row["id"]))


def list_releases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, created_at, created_by, effective_from, status, kind, based_on, comment, content_hash "
        "FROM kb_releases ORDER BY id DESC"
    ).fetchall()


# --------------------------------------------------------------------------- draft


def load_draft(conn: sqlite3.Connection) -> m.Bundle:
    data: dict[str, list] = {v[0]: [] for v in KINDS.values()}
    for row in conn.execute("SELECT kind, data FROM kb_draft_items ORDER BY kind, item_id"):
        data[KINDS[row["kind"]][0]].append(json.loads(row["data"]))
    demo = bool(conn.execute("SELECT demo FROM kb_draft_meta WHERE id=1").fetchone()["demo"])
    return m.Bundle.model_validate({**data, "demo": demo})


def draft_revision(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT revision FROM kb_draft_meta WHERE id=1").fetchone()["revision"]


def draft_item(conn: sqlite3.Connection, kind: str, item_id: str) -> tuple[dict, int] | None:
    row = conn.execute("SELECT data, revision FROM kb_draft_items WHERE kind=? AND item_id=?", (kind, item_id)).fetchone()
    return (json.loads(row["data"]), row["revision"]) if row else None


def save_draft_item(
    conn: sqlite3.Connection, kind: str, item: dict, user: str, expected_revision: int | None,
    old_id: str | None = None,
) -> int:
    """Сохранить элемент черновика (с проверкой модели и оптимистичной блокировкой).

    ``expected_revision=None`` — создание нового элемента. Возвращает новую ревизию.
    """
    _, model_cls, key = KINDS[kind]
    obj = model_cls.model_validate(item)
    data = json.dumps(obj.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    new_id = getattr(obj, key)
    now = iso(utcnow())
    with write_tx(conn):
        # ревизия элемента — глобально монотонная (ревизия черновика + 1) и не переиспользуется
        rev = draft_revision(conn) + 1
        if expected_revision is None:
            exists = conn.execute("SELECT 1 FROM kb_draft_items WHERE kind=? AND item_id=?", (kind, new_id)).fetchone()
            if exists:
                raise ConcurrentEdit(f"элемент «{new_id}» уже существует")
            conn.execute(
                "INSERT INTO kb_draft_items(kind, item_id, data, revision, updated_at, updated_by) VALUES (?,?,?,?,?,?)",
                (kind, new_id, data, rev, now, user),
            )
        else:
            cur_id = old_id or new_id
            if cur_id != new_id:
                raise PublishError("ID элемента менять нельзя: создайте новый элемент")
            n = conn.execute(
                "UPDATE kb_draft_items SET data=?, revision=?, updated_at=?, updated_by=? "
                "WHERE kind=? AND item_id=? AND revision=?",
                (data, rev, now, user, kind, cur_id, expected_revision),
            ).rowcount
            if n != 1:
                raise ConcurrentEdit("элемент изменён кем-то ещё — обновите страницу")
        conn.execute("UPDATE kb_draft_meta SET revision=? WHERE id=1", (rev,))
        _audit(conn, user, f"draft_save_{kind}", new_id)
    return rev


def delete_draft_item(conn: sqlite3.Connection, kind: str, item_id: str, user: str, expected_revision: int) -> None:
    with write_tx(conn):
        n = conn.execute(
            "DELETE FROM kb_draft_items WHERE kind=? AND item_id=? AND revision=?", (kind, item_id, expected_revision)
        ).rowcount
        if n != 1:
            raise ConcurrentEdit("элемент изменён или уже удалён")
        conn.execute("UPDATE kb_draft_meta SET revision=revision+1 WHERE id=1")
        _audit(conn, user, f"draft_delete_{kind}", item_id)


def replace_draft(conn: sqlite3.Connection, bundle: m.Bundle, user: str, reason: str) -> None:
    """Заменить весь черновик набором (загрузка начального наполнения или выпуска в черновик)."""
    now = iso(utcnow())
    with write_tx(conn):
        rev = draft_revision(conn) + 1
        conn.execute("DELETE FROM kb_draft_items")
        for kind, (attr, _, key) in KINDS.items():
            for obj in getattr(bundle, attr):
                conn.execute(
                    "INSERT INTO kb_draft_items(kind, item_id, data, revision, updated_at, updated_by) VALUES (?,?,?,?,?,?)",
                    (kind, getattr(obj, key), json.dumps(obj.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                     rev, now, user),
                )
        conn.execute("UPDATE kb_draft_meta SET revision=?, demo=? WHERE id=1", (rev, int(bundle.demo)))
        _audit(conn, user, "draft_replace", reason)


# --------------------------------------------------------------------------- publish


def _check_schedule(conn: sqlite3.Connection, eff: str, now: str) -> None:
    if eff < iso(datetime.fromisoformat(now) - PAST_TOLERANCE):
        raise PublishError("дата вступления в силу в прошлом — задним числом публиковать нельзя")
    same = conn.execute(
        "SELECT id FROM kb_releases WHERE status='published' AND effective_from=?", (eff,)
    ).fetchone()
    if same:
        raise PublishError(f"на это же время уже запланирован выпуск №{same['id']}")
    later = conn.execute(
        "SELECT id, effective_from FROM kb_releases WHERE status='published' AND effective_from>? ORDER BY effective_from",
        (eff,),
    ).fetchall()
    if later:
        ids = ", ".join(f"№{r['id']} ({r['effective_from']})" for r in later)
        raise PublishError(
            "после этой даты уже запланированы выпуски " + ids + ". Они были собраны из более старого черновика и "
            "отменили бы это изменение. Сначала отмените их или опубликуйте с более поздней датой."
        )


def _insert_release(conn, bundle: m.Bundle, user: str, eff: str, kind: str, comment: str, based_on) -> int:
    content = dump_bundle(bundle)
    cur = conn.execute(
        "INSERT INTO kb_releases(created_at, created_by, effective_from, kind, based_on, comment, content, content_hash) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (iso(utcnow()), user, eff, kind, based_on, comment, content, _hash(content)),
    )
    _audit(conn, user, f"release_{kind}", str(cur.lastrowid), f"effective_from={eff}; {comment}")
    return cur.lastrowid


def publish_draft(
    conn: sqlite3.Connection, user: str, effective_from: datetime | None = None, comment: str = "",
    expected_draft_revision: int | None = None, now: datetime | None = None,
) -> int:
    """Опубликовать черновик целиком как новый выпуск.

    ``expected_draft_revision`` — ревизия черновика, которую видел и проверял
    пользователь (на экране сравнения). Если черновик успел измениться,
    публикация отклоняется, чтобы не выпустить непросмотренное.
    """
    now_s = iso(now or utcnow())
    eff = iso(effective_from) if effective_from else now_s
    with write_tx(conn):
        if expected_draft_revision is not None and draft_revision(conn) != expected_draft_revision:
            raise ConcurrentEdit("черновик изменился после проверки — откройте сравнение заново")
        bundle = load_draft(conn)
        errs = errors(validate(bundle))
        if errs:
            raise PublishError("черновик не прошёл проверку", errs)
        _check_schedule(conn, eff, now_s)
        base = conn.execute(
            "SELECT id FROM kb_releases WHERE status='published' AND effective_from<=? ORDER BY effective_from DESC, id DESC LIMIT 1",
            (now_s,),
        ).fetchone()
        return _insert_release(conn, bundle, user, eff, "demo" if bundle.demo else "publish", comment,
                               base["id"] if base else None)


def cancel_scheduled(conn: sqlite3.Connection, rid: int, user: str, now: datetime | None = None) -> None:
    """Отменить ещё не вступивший в силу выпуск. Действующие и прошлые не отменяются."""
    now_s = iso(now or utcnow())
    with write_tx(conn):
        n = conn.execute(
            "UPDATE kb_releases SET status='cancelled', cancelled_at=?, cancelled_by=? "
            "WHERE id=? AND status='published' AND effective_from>?",
            (now_s, user, rid, now_s),
        ).rowcount
        if n != 1:
            raise PublishError("отменить можно только запланированный, ещё не вступивший в силу выпуск")
        _audit(conn, user, "release_cancel", str(rid))


def rollback_to(conn: sqlite3.Connection, rid: int, user: str, comment: str = "", now: datetime | None = None) -> int:
    """Откат: новый выпуск с содержимым выпуска ``rid``, действующий с текущего момента."""
    now_s = iso(now or utcnow())
    with write_tx(conn):
        row = conn.execute("SELECT content FROM kb_releases WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise PublishError(f"выпуск №{rid} не найден")
        bundle = m.Bundle.model_validate_json(row["content"])
        errs = errors(validate(bundle))
        if errs:
            raise PublishError("старый выпуск не проходит текущие проверки", errs)
        _check_schedule(conn, now_s, now_s)
        return _insert_release(conn, bundle, user, now_s, "rollback", comment or f"откат к №{rid}", rid)


def flag_needs_review(
    conn: sqlite3.Connection, ground_id: str, reason: str, user: str,
    requirement_ids: list[str] | None = None, now: datetime | None = None,
) -> int:
    """Срочно пометить основание «требует проверки» в действующей базе.

    Создаёт новый выпуск из действующего (не из черновика, чтобы не выпустить
    непроверенные правки) и переносит пометку в черновик, чтобы следующая
    публикация её не сняла молча.
    """
    if not reason.strip():
        raise PublishError("укажите причину")
    now_dt = now or utcnow()
    now_s = iso(now_dt)
    with write_tx(conn):
        snap = active_snapshot(conn, now_dt)
        if snap is None:
            raise PublishError("нет действующего выпуска")
        data = json.loads(dump_bundle(snap.bundle))
        hit = False
        for g in data["grounds"]:
            if g["id"] == ground_id:
                hit = True
                _apply_flag(g, reason, requirement_ids)
        if not hit:
            raise PublishError(f"основания «{ground_id}» нет в действующем выпуске")
        bundle = m.Bundle.model_validate(data)
        _check_schedule(conn, now_s, now_s)
        rid = _insert_release(conn, bundle, user, now_s, "review_flag", f"{ground_id}: {reason}", snap.release_id)
        row = conn.execute("SELECT data FROM kb_draft_items WHERE kind='ground' AND item_id=?", (ground_id,)).fetchone()
        if row:
            g = json.loads(row["data"])
            _apply_flag(g, reason, requirement_ids)
            rev = draft_revision(conn) + 1
            conn.execute(
                "UPDATE kb_draft_items SET data=?, revision=?, updated_at=?, updated_by=? WHERE kind='ground' AND item_id=?",
                (json.dumps(g, ensure_ascii=False, sort_keys=True), rev, now_s, user, ground_id),
            )
            conn.execute("UPDATE kb_draft_meta SET revision=? WHERE id=1", (rev,))
        return rid


def _apply_flag(g: dict, reason: str, requirement_ids: list[str] | None) -> None:
    if g["status"] != "disabled":
        g["status"] = "needs_review"
    g["review_reason"] = reason
    for r in g.get("requirements", []):
        # без списка — спорными становятся ВСЕ требования основания
        if not requirement_ids or r["id"] in requirement_ids:
            r["disputed"] = True
            r["dispute_reason"] = reason


# --------------------------------------------------------------------------- diff


def diff_bundles(old: m.Bundle | None, new: m.Bundle) -> list[dict]:
    """Поэлементное сравнение: [{kind, id, change: added|removed|changed, fields: [...]}]."""
    out = []
    for kind, (attr, _, key) in KINDS.items():
        a = {getattr(x, key): x.model_dump(mode="json") for x in (getattr(old, attr) if old else [])}
        b = {getattr(x, key): x.model_dump(mode="json") for x in getattr(new, attr)}
        for i in sorted(a.keys() | b.keys()):
            if i not in a:
                out.append({"kind": kind, "id": i, "change": "added", "fields": sorted(b[i].keys())})
            elif i not in b:
                out.append({"kind": kind, "id": i, "change": "removed", "fields": []})
            elif a[i] != b[i]:
                fields = sorted(k for k in a[i].keys() | b[i].keys() if a[i].get(k) != b[i].get(k))
                out.append({"kind": kind, "id": i, "change": "changed", "fields": fields,
                            "old": {k: a[i].get(k) for k in fields}, "new": {k: b[i].get(k) for k in fields}})
    return out


def _audit(conn: sqlite3.Connection, actor: str, action: str, target: str = "", detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, target, detail) VALUES (?,?,?,?,?)",
        (iso(utcnow()), actor, action, target, detail[:2000]),
    )
