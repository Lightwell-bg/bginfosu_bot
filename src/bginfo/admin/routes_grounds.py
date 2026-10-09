"""Основания: список и редактор."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from bginfo.admin.auth import PostCtx, post_ctx
from bginfo.admin.common import flash, get_conn, redirect
from bginfo.admin.crud import Spec, register_crud
from bginfo.admin.forms import parse_ground, ru_errors
from bginfo.kb import model as m
from bginfo.kb.approve import approve_ground_draft
from bginfo.kb.releases import (
    ConcurrentEdit,
    PublishError,
    _apply_flag,
    active_snapshot,
    draft_item,
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
    reqs = [{"label": r.get("label") or r.get("id"), "badge": _req_state(r)} for r in item.get("requirements", [])]
    today = date.today()  # noqa: DTZ011 - календарная дата для подстановки значений
    return {"summary": render_text(item.get("summary", ""), draft, today), "reqs": reqs, "in_active": in_active,
            "steps": [render_text(x, draft, today) for x in item.get("steps", [])],
            "documents": [render_text(x, draft, today) for x in item.get("documents", [])]}


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


@router.post("/grounds/{gid}/approve")
def approve_one(request: Request, gid: str, p: PostCtx = Depends(post_ctx),
                conn: sqlite3.Connection = Depends(get_conn)):
    """Одобрить одну карточку в черновике (без публикации)."""
    by = str(p.form.get("by", "")).strip()
    rev = _rev(p.form)
    if not by:
        flash(request, "Укажите ваше имя, чтобы одобрить карточку.", "error")
        return redirect(f"/grounds/{gid}")
    if rev is None:
        flash(request, "Не передана версия карточки: обновите страницу.", "error")
        return redirect(f"/grounds/{gid}")
    try:
        res = approve_ground_draft(conn, gid, by[:100], expected_revision=rev)
    except (PublishError, ConcurrentEdit) as e:
        flash(request, f"Не удалось одобрить: {e}", "error")
        return redirect(f"/grounds/{gid}")
    if res.disputed:
        flash(request, f"Карточка «{res.title}» одобрена, но требует проверки: не подтверждено "
                       f"{'; '.join(res.disputed)}. Бот предложит индивидуальный разбор. Изменения в черновике.")
    else:
        flash(request, f"Карточка «{res.title}» одобрена. Бот увидит её после публикации.")
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
