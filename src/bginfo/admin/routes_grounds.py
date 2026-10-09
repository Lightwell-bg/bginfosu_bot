"""Основания: список и редактор."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from bginfo.admin.auth import PostCtx, post_ctx
from bginfo.admin.common import flash, get_conn, issue_lines, redirect, render
from bginfo.admin.crud import Spec, register_crud
from bginfo.admin.forms import parse_ground, ru_errors
from bginfo.kb import model as m
from bginfo.kb.approve import approve_ground_and_publish, pending_confirmations
from bginfo.kb.releases import (
    ConcurrentEdit,
    PublishError,
    _apply_flag,
    active_snapshot,
    draft_item,
    draft_revision,
    load_draft,
    save_draft_item,
)

router = APIRouter()


def _new() -> dict:
    return {"id": "", "title": "", "regime": "third_country", "subject": "applicant", "status": "draft",
            "review_reason": "", "summary": "", "family_note": "", "research_note": "", "goals": [], "steps": [],
            "documents": [], "restrictions": [], "practice_notes": [], "bginfo_source_ids": [], "requirements": [],
            "unsupported": [], "applies_if": None, "valid_from": None, "valid_to": None, "checked_at": None,
            "checked_by": ""}


def attention_text(g: m.Ground) -> str:
    """«Что требует внимания» для списка: сколько условий не подтверждено / спорно."""
    unverified = sum(1 for r in g.requirements if not r.verified)
    disputed = sum(1 for r in g.requirements if r.disputed)
    parts = []
    if unverified:
        word = "условие" if unverified % 10 == 1 and unverified % 100 != 11 else (
            "условия" if unverified % 10 in (2, 3, 4) and unverified % 100 not in (12, 13, 14) else "условий")
        verb = "не подтверждено" if word != "условия" else "не подтверждены"
        parts.append(f"{unverified} {word} {verb}")
    if disputed:
        parts.append(f"спорных: {disputed}")
    return "; ".join(parts) or "—"


def _list_ctx(conn: sqlite3.Connection, draft: m.Bundle) -> dict:
    rows = [{"id": g.id, "title": g.title, "status": g.status, "attention": attention_text(g),
             "warn": attention_text(g) != "—"} for g in draft.grounds]
    return {"title": "Основания", "rows": rows}


def _req_state(r: dict) -> tuple[str, str]:
    if r.get("disputed"):
        return "спорно", "warn"
    if r.get("verified"):
        return "подтверждено", "ok"
    return "не подтверждено", "gray"


def _card_ctx(conn: sqlite3.Connection, draft: m.Bundle, item: dict, revision: int) -> dict:
    from datetime import date

    from bginfo.engine.result import render_text

    snap = active_snapshot(conn)
    in_active = bool(snap and snap.bundle.ground(item["id"]))
    today = date.today()  # noqa: DTZ011 - календарная дата для подстановки значений
    reqs = [{"label": render_text(r.get("label") or r.get("id"), draft, today), "badge": _req_state(r)}
            for r in item.get("requirements", [])]
    confirm_reqs, confirm_params = pending_confirmations(draft, item["id"], today)
    return {"summary": render_text(item.get("summary", ""), draft, today), "reqs": reqs, "in_active": in_active,
            "steps": [render_text(x, draft, today) for x in item.get("steps", [])],
            "documents": [render_text(x, draft, today) for x in item.get("documents", [])],
            "restrictions": [render_text(x.get("text", ""), draft, today) for x in item.get("restrictions", [])],
            "confirm_reqs": confirm_reqs, "confirm_params": confirm_params, "draft_rev": draft_revision(conn)}


def _form_ctx(draft: m.Bundle, item: dict, is_new: bool) -> dict:
    goal_fact = draft.fact("goal")
    options = [(o.value, o.label) for o in goal_fact.options] if goal_fact else []
    known = {v for v, _ in options}
    extra = [g for g in item.get("goals", []) if g not in known]
    return {"goal_options": options, "goals_extra": ", ".join(extra)}


SPEC = Spec(kind="ground", base="/grounds", one="основание", many="Основания", template="ground_form.html",
            parse=parse_ground, new_item=_new, list_ctx=_list_ctx, form_ctx=_form_ctx,
            list_template="ground_list.html", card_template="ground_card.html", card_ctx=_card_ctx,
            hint="Здесь все карточки. Откройте карточку, чтобы проверить её, одобрить или отредактировать.")
register_crud(router, SPEC)


# --------------------------------------------------------------------------- действия на карточке


def _rev(form) -> int | None:
    raw = str(form.get("revision", "")).strip()
    return int(raw) if raw.isdigit() else None


def _card_response(request: Request, conn: sqlite3.Connection, gid: str, errors: list[str], status: int):
    """Карточка с красным блоком ошибок (после неудачной публикации)."""
    cur = draft_item(conn, "ground", gid)
    if cur is None:
        raise HTTPException(404, f"Не найдено: {gid}")
    return render(request, "ground_card.html", status_code=status, spec=SPEC, item=cur[0], revision=cur[1],
                  errors=errors, **_card_ctx(conn, load_draft(conn), cur[0], cur[1]))


@router.post("/grounds/{gid}/approve-publish")
def approve_publish(request: Request, gid: str, p: PostCtx = Depends(post_ctx),
                    conn: sqlite3.Connection = Depends(get_conn)):
    """Одобрить карточку и сразу опубликовать: бот начинает использовать её немедленно."""
    by = str(p.form.get("by", "")).strip()
    raw_rev = str(p.form.get("draft_revision", "")).strip()
    if not by:
        return _card_response(request, conn, gid, ["Укажите ваше имя: оно запишется как «кем подтверждено»."], 422)
    if len(by) > 100:
        return _card_response(request, conn, gid, ["Имя слишком длинное: не больше 100 символов."], 422)
    if not raw_rev.isdigit():
        return _card_response(request, conn, gid, ["Не передана версия черновика: обновите страницу."], 422)
    try:
        rid, _, _ = approve_ground_and_publish(conn, gid, by, expected_draft_revision=int(raw_rev))
    except ConcurrentEdit:
        return _card_response(request, conn, gid, ["Пока вы смотрели, базу изменили. Обновите страницу."], 409)
    except PublishError as e:
        lines = issue_lines(e.issues, load_draft(conn)) or [str(e)]
        return _card_response(request, conn, gid, [f"Не удалось опубликовать: {e}", *lines], 422)
    request.session["approver_name"] = by
    flash(request, f"Опубликовано. Бот уже использует версию №{rid}.")
    return redirect(f"/grounds/{gid}")


@router.post("/grounds/{gid}/mark-review")
def mark_review(request: Request, gid: str, p: PostCtx = Depends(post_ctx),
                conn: sqlite3.Connection = Depends(get_conn)):
    """Пометить карточку в черновике «требует проверки» (в боте появится после публикации)."""
    reason = str(p.form.get("reason", "")).strip()
    rev = _rev(p.form)
    cur = draft_item(conn, "ground", gid)
    if cur is None:
        raise HTTPException(404, f"Не найдено: {gid}")
    if not reason:
        flash(request, "Укажите причину: её увидят пользователи.", "error")
        return redirect(f"/grounds/{gid}")
    if rev is None:
        flash(request, "Не передана версия карточки: обновите страницу.", "error")
        return redirect(f"/grounds/{gid}")
    data = cur[0]
    _apply_flag(data, reason, None)
    try:
        save_draft_item(conn, "ground", data, p.user, rev, gid)
    except ValidationError as e:
        flash(request, "Не удалось пометить: " + "; ".join(ru_errors(e, data.get("title", ""))), "error")
    except (ConcurrentEdit, PublishError) as e:
        flash(request, f"Не удалось пометить: {e}", "error")
    else:
        flash(request, "Карточка помечена «требует проверки» в черновике. В боте это появится после публикации.")
    return redirect(f"/grounds/{gid}")
