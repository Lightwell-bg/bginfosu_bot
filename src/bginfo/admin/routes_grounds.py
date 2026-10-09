"""Основания: список и редактор."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter

from bginfo.admin.common import GROUND_STATUS_RU, REGIME_RU
from bginfo.admin.crud import Spec, register_crud
from bginfo.admin.forms import parse_ground
from bginfo.kb import model as m
from bginfo.kb.releases import active_snapshot

router = APIRouter()


def _new() -> dict:
    return {"id": "", "title": "", "regime": "third_country", "subject": "applicant", "status": "draft",
            "review_reason": "", "summary": "", "family_note": "", "research_note": "", "goals": [], "steps": [],
            "documents": [], "restrictions": [], "practice_notes": [], "bginfo_source_ids": [], "requirements": [],
            "unsupported": [], "applies_if": None, "valid_from": None, "valid_to": None, "checked_at": None,
            "checked_by": ""}


def _list_ctx(conn: sqlite3.Connection, draft: m.Bundle) -> dict:
    snap = active_snapshot(conn)
    active = {g.id: g for g in snap.bundle.grounds} if snap else {}
    rows = []
    for g in draft.grounds:
        a = active.get(g.id)
        unverified = sum(1 for r in g.requirements if not r.verified)
        rows.append({
            "id": g.id,
            "cells": [
                g.title, REGIME_RU.get(g.regime, g.regime),
                f"{GROUND_STATUS_RU[g.status]} / " + (GROUND_STATUS_RU[a.status] if a else "нет в действующем"),
                g.checked_at.strftime("%d.%m.%Y") if g.checked_at else "—",
                f"требований без подтверждения: {unverified}" if unverified else "все подтверждены",
            ],
            "warn": bool(unverified),
        })
    return {"title": "Основания", "headers": ["Название", "Режим", "Статус: черновик / действующий выпуск",
                                              "Дата проверки", "Подтверждение требований"], "rows": rows}


def _form_ctx(draft: m.Bundle, item: dict, is_new: bool) -> dict:
    goal_fact = draft.fact("goal")
    options = [(o.value, o.label) for o in goal_fact.options] if goal_fact else []
    known = {v for v, _ in options}
    extra = [g for g in item.get("goals", []) if g not in known]
    return {"goal_options": options, "goals_extra": ", ".join(extra)}


SPEC = Spec(kind="ground", base="/grounds", one="основание", many="Основания", template="ground_form.html",
            parse=parse_ground, new_item=_new, list_ctx=_list_ctx, form_ctx=_form_ctx)
register_crud(router, SPEC)
