"""Главная страница («Что сделать сейчас») и одобрение всей базы одним действием."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, Request

from bginfo.admin.auth import PostCtx, current_user, post_ctx
from bginfo.admin.common import GROUND_STATUS_RU, get_conn, get_state, render
from bginfo.db.conn import iso, utcnow
from bginfo.kb.approve import approve_all_and_publish, render_summary
from bginfo.kb.releases import (
    ConcurrentEdit,
    PublishError,
    active_snapshot,
    diff_bundles,
    draft_revision,
    list_releases,
    load_draft,
)

router = APIRouter()

MAX_NAME = 100


def _dashboard(request: Request, conn: sqlite3.Connection, status: int = 200, errors: list[str] | None = None,
               values: dict | None = None):
    st = get_state(request)
    snap = active_snapshot(conn)
    rev = draft_revision(conn)  # ревизию читаем ДО черновика
    draft = load_draft(conn)
    now = iso(utcnow())
    scheduled = [r for r in list_releases(conn) if r["status"] == "published" and r["effective_from"] > now]
    diff = diff_bundles(snap.bundle if snap else None, draft)
    draft_counts = Counter(g.status for g in draft.grounds)
    # кнопка «Одобрить всё»: база ни разу не опубликована или есть основания-черновики
    can_approve_all = bool(draft.grounds) and (snap is None or draft_counts.get("draft", 0) > 0)
    return render(
        request, "dashboard.html", status_code=status, active=snap, scheduled=scheduled, dirty=bool(diff),
        changes=len(diff), demo_in_prod=bool(snap and snap.bundle.demo and st.settings.APP_MODE == "production"),
        draft_counts={k: draft_counts.get(k, 0) for k in GROUND_STATUS_RU},
        grounds_total=len(draft.grounds), params_total=len(draft.parameters), can_approve_all=can_approve_all,
        draft_rev=rev, errors=errors or [], values=values or {},
    )


@router.get("/")
def dashboard(request: Request, user: str = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return _dashboard(request, conn)


@router.post("/approve-all")
def approve_all(request: Request, p: PostCtx = Depends(post_ctx), conn: sqlite3.Connection = Depends(get_conn)):
    """Одобрить весь черновик от имени человека и сразу опубликовать (логика — kb.approve)."""
    form = p.form
    name = str(form.get("by", "")).strip()
    confirmed = form.get("confirm") in ("on", "1", "true", "yes")
    values = {"by": name[:MAX_NAME], "confirm": confirmed}
    errors: list[str] = []
    if not name:
        errors.append("Укажите ваше имя: оно запишется в карточки как «кем подтверждено».")
    elif len(name) > MAX_NAME:
        errors.append(f"Имя слишком длинное: не больше {MAX_NAME} символов.")
    if not confirmed:
        errors.append("Поставьте галочку: вы проверили карточки и берёте ответственность за публикацию.")
    raw_rev = str(form.get("draft_revision", "")).strip()
    if not raw_rev.isdigit():
        errors.append("Не передана версия черновика: обновите страницу.")
    if errors:
        return _dashboard(request, conn, 422, errors, values)
    if int(raw_rev) != draft_revision(conn):
        return _dashboard(request, conn, 409, [("Пока вы смотрели страницу, черновик изменился. "
                                               "Проверьте его и нажмите кнопку ещё раз.")], values)
    try:
        summary = approve_all_and_publish(conn, name, expected_draft_revision=int(raw_rev))
    except ConcurrentEdit as e:
        return _dashboard(request, conn, 409, [str(e)], values)
    except PublishError as e:
        msg = [f"Не удалось опубликовать: {e}"] + [f"{i.where}: {i.message}" for i in e.issues[:20]]
        return _dashboard(request, conn, 422, msg, values)
    snap = active_snapshot(conn)
    st = get_state(request)
    shown = render_summary(summary, snap.bundle, datetime.now(st.tz).date()) if snap else summary
    return render(request, "approved.html", s=shown, by=name, bot_link=st.config.bot.bot_link)
