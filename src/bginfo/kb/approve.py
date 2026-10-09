"""Одобрить черновик базы от имени владельца и опубликовать одним действием.

Вызывается только явным действием владельца (команда или кнопка в админке)
с его именем. Правила:
- требование с research_status «unconfirmed» не одобряется: оно помечается
  спорным, а основание получает статус «требует проверки». По такому условию
  бот не даёт уверенного вывода и предлагает индивидуальный разбор;
- остальные требования получают verified=true, автор и дата одобрения;
- значения показателей одобряются, кроме «unconfirmed»;
- основание без спорных требований становится «опубликована»;
- отключённые основания не трогаются.
Затем черновик публикуется обычной атомарной публикацией (с валидацией).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from bginfo.db.conn import iso, utcnow, write_tx
from bginfo.kb import releases as R

UNCONFIRMED_REASON = "Условие не подтверждено официальным источником при первоначальной проверке — нужен индивидуальный разбор."


@dataclass
class ApproveSummary:
    release_id: int = 0
    published: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)  # «основание: требование, …»
    params_unconfirmed: list[str] = field(default_factory=list)


def _apply_ground_approval(data: dict, by: str, day: str) -> list[str]:
    """Одобрить словарь основания на месте. Возвращает подписи спорных требований (пусто — основание опубликовано)."""
    disputed: list[str] = []
    for r in data.get("requirements", []):
        if r.get("research_status") == "unconfirmed":
            r["verified"] = False
            r["disputed"] = True
            r["dispute_reason"] = r.get("dispute_reason") or UNCONFIRMED_REASON
            disputed.append(r.get("label") or r["id"])
        elif r.get("disputed"):
            # спорное по любой причине (в том числе ручная пометка) не получает verified
            r["verified"] = False
            disputed.append(r.get("label") or r["id"])
        else:
            r["verified"] = True
            r["verified_by"] = by
            r["verified_at"] = day
    data["checked_by"] = by
    data["checked_at"] = day
    if disputed:
        data["status"] = "needs_review"
        data["review_reason"] = data.get("review_reason") or UNCONFIRMED_REASON
    else:
        data["status"] = "published"
    return disputed


def _store(conn: sqlite3.Connection, kind: str, item_id: str, data: dict, rev: int, now: datetime, by: str) -> None:
    conn.execute(
        "UPDATE kb_draft_items SET data=?, revision=?, updated_at=?, updated_by=? WHERE kind=? AND item_id=?",
        (json.dumps(data, ensure_ascii=False, sort_keys=True), rev, iso(now), by, kind, item_id),
    )


@dataclass
class GroundApproval:
    title: str
    disputed: list[str] = field(default_factory=list)  # подписи спорных требований

    @property
    def published(self) -> bool:
        return not self.disputed


def approve_ground_draft(conn: sqlite3.Connection, ground_id: str, by: str, now: datetime | None = None,
                         today: date | None = None, expected_revision: int | None = None) -> GroundApproval:
    """Одобрить ОДНУ карточку в черновике (без публикации) по тем же правилам, что и approve_all_and_publish.

    ``expected_revision`` — ревизия карточки, которую видел человек: если её уже изменили, ConcurrentEdit.
    """
    by = by.strip()
    if not by:
        raise R.PublishError("укажите, кто одобряет (имя)")
    now = now or utcnow()
    day = (today or now.date()).isoformat()
    with write_tx(conn):
        row = conn.execute("SELECT data, revision FROM kb_draft_items WHERE kind='ground' AND item_id=?",
                           (ground_id,)).fetchone()
        if row is None:
            raise R.PublishError(f"основания «{ground_id}» нет в черновике")
        if expected_revision is not None and row["revision"] != expected_revision:
            raise R.ConcurrentEdit("карточка изменена кем-то ещё — обновите страницу")
        data = json.loads(row["data"])
        if data.get("status") == "disabled":
            raise R.PublishError("основание отключено: сначала включите его")
        disputed = _apply_ground_approval(data, by, day)
        rev = R.draft_revision(conn) + 1
        _store(conn, "ground", ground_id, data, rev, now, by)
        conn.execute("UPDATE kb_draft_meta SET revision=? WHERE id=1", (rev,))
        R._audit(conn, by, "approve_ground", ground_id, "needs_review" if disputed else "published")
    return GroundApproval(data.get("title", ground_id), disputed)


def _ground_params(kb, g) -> set[str]:
    """Показатели, которые использует основание: в условиях и плейсхолдерах, с учётом производных."""
    from bginfo.kb import validate as V

    used: set[str] = set()
    for _, c in V.ground_conditions(g):
        used |= V.params_in_condition(c)
    for _, t in V.ground_texts(g):
        used |= set(V.PLACEHOLDER.findall(t or ""))
    return {p for p in V.param_closure(kb, used) if kb.param(p) is not None}


def pending_confirmations(kb, ground_id: str, as_of: date) -> tuple[list[str], list[str]]:
    """Что владелец подтверждает кнопкой «Одобрить и опубликовать».

    Возвращает (условия, цифры): условия — спорные или неподтверждённые (label с подставленными суммами),
    цифры — «название: значение» для показателей карточки с хотя бы одним непроверенным значением.
    """
    from bginfo.engine.result import render_text

    g = kb.ground(ground_id)
    if g is None:
        return [], []
    reqs = [render_text(r.label, kb, as_of) for r in g.requirements if r.disputed or not r.verified]
    params = []
    for pid in sorted(_ground_params(kb, g)):
        p = kb.param(pid)
        if any(not v.verified for v in p.values):
            params.append(f"{p.label}: {render_text('{param:' + pid + '}', kb, as_of)}")
    return reqs, params


def approve_ground_and_publish(conn: sqlite3.Connection, ground_id: str, by: str, now: datetime | None = None,
                               today: date | None = None, expected_draft_revision: int | None = None,
                               ) -> tuple[int, list[str], list[str]]:
    """Подтвердить ВСЕ требования карточки и её цифры, сделать основание «опубликовано» и выпустить версию.

    Всё в одной транзакции: ошибка публикации откатывает и изменения карточки.
    Возвращает (номер выпуска, подписи подтверждённых спорных требований, названия подтверждённых цифр).
    """
    by = by.strip()
    if not by:
        raise R.PublishError("укажите, кто одобряет (имя)")
    now = now or utcnow()
    now_s = iso(now)
    day = (today or now.date()).isoformat()
    confirmed_disputed: list[str] = []
    confirmed_params: list[str] = []
    with write_tx(conn):
        if expected_draft_revision is not None and R.draft_revision(conn) != expected_draft_revision:
            raise R.ConcurrentEdit("черновик изменился после показа страницы — обновите её")
        row = conn.execute("SELECT data FROM kb_draft_items WHERE kind='ground' AND item_id=?", (ground_id,)).fetchone()
        if row is None:
            raise R.PublishError(f"основания «{ground_id}» нет в черновике")
        data = json.loads(row["data"])
        if data.get("status") == "disabled":
            raise R.PublishError("основание отключено: сначала включите его")
        kb = R.load_draft(conn)
        param_ids = _ground_params(kb, kb.ground(ground_id))
        rev = R.draft_revision(conn)

        for r in data.get("requirements", []):
            if r.get("disputed"):
                confirmed_disputed.append(r.get("label") or r["id"])
            r.update(verified=True, verified_by=by, verified_at=day, disputed=False, dispute_reason="")
        data.update(status="published", review_reason="", checked_by=by, checked_at=day)
        rev += 1
        _store(conn, "ground", ground_id, data, rev, now, by)

        for prow in conn.execute("SELECT item_id, data FROM kb_draft_items WHERE kind='parameter'").fetchall():
            pdata = json.loads(prow["data"])
            if pdata.get("id") not in param_ids:
                continue
            if any(not v.get("verified") for v in pdata.get("values", [])):
                confirmed_params.append(pdata.get("label", pdata["id"]))
                for v in pdata.get("values", []):
                    v["verified"] = True
                rev += 1
                _store(conn, "parameter", prow["item_id"], pdata, rev, now, by)
        conn.execute("UPDATE kb_draft_meta SET revision=? WHERE id=1", (rev,))
        R._audit(conn, by, "approve_ground_publish", ground_id,
                 f"disputed={len(confirmed_disputed)}; params={len(confirmed_params)}")
        release_id = R._publish_in_tx(conn, by, now_s, f"Одобрено и опубликовано: {data.get('title', ground_id)} ({by})",
                                      now_s, None)
    return release_id, confirmed_disputed, confirmed_params


def approve_all_and_publish(conn: sqlite3.Connection, by: str, now: datetime | None = None,
                            today: date | None = None,
                            expected_draft_revision: int | None = None) -> ApproveSummary:
    """Одобрить и опубликовать в ОДНОЙ транзакции: ошибка публикации откатывает и одобрения."""
    by = by.strip()
    if not by:
        raise R.PublishError("укажите, кто одобряет (имя)")
    now = now or utcnow()
    now_s = iso(now)
    day = (today or now.date()).isoformat()
    summary = ApproveSummary()
    with write_tx(conn):
        if expected_draft_revision is not None and R.draft_revision(conn) != expected_draft_revision:
            raise R.ConcurrentEdit("черновик изменился после показа страницы — обновите её")
        rows = conn.execute("SELECT kind, item_id, data FROM kb_draft_items WHERE kind IN ('ground','parameter')").fetchall()
        rev = R.draft_revision(conn)
        for row in rows:
            data = json.loads(row["data"])
            if row["kind"] == "ground":
                if data.get("status") == "disabled":
                    continue
                disputed = _apply_ground_approval(data, by, day)
                title = data.get("title", row["item_id"])
                if disputed:
                    summary.needs_review.append(f"{title}: " + "; ".join(disputed))
                else:
                    summary.published.append(title)
            else:
                for v in data.get("values", []):
                    if v.get("research_status") == "unconfirmed":
                        summary.params_unconfirmed.append(data.get("label", row["item_id"]))
                    else:
                        v["verified"] = True
            rev += 1
            _store(conn, row["kind"], row["item_id"], data, rev, now, by)
        conn.execute("UPDATE kb_draft_meta SET revision=? WHERE id=1", (rev,))
        R._audit(conn, by, "approve_all", "draft", f"published={len(summary.published)}; review={len(summary.needs_review)}")
        summary.release_id = R._publish_in_tx(conn, by, now_s, f"Одобрено целиком: {by}", now_s, None)
    return summary


def render_summary(summary: ApproveSummary, bundle, as_of: date) -> ApproveSummary:
    """Копия итога, где плейсхолдеры ``{param:...}`` в названиях заменены значениями из ``bundle``."""
    from bginfo.engine.result import render_text

    def fix(items: list[str]) -> list[str]:
        return [render_text(t, bundle, as_of) for t in items]

    return ApproveSummary(summary.release_id, fix(summary.published), fix(summary.needs_review),
                          fix(summary.params_unconfirmed))
