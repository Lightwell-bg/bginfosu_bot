"""Показатели, каталог фактов, вопросы и источники."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from bginfo.admin.auth import PostCtx, post_ctx
from bginfo.admin.common import CHOICES, flash, get_conn, redirect
from bginfo.admin.crud import Spec, register_crud
from bginfo.admin.forms import parse_fact, parse_param, parse_question, parse_source, ru_errors
from bginfo.kb import model as m
from bginfo.kb.releases import ConcurrentEdit, PublishError, draft_item, save_draft_item
from bginfo.kb.validate import grounds_using_param

router = APIRouter()


# ----------------------------------------------------------------------- parameters


def current_value(p: m.Parameter, today: date):
    """Значение, действующее сегодня; если такого нет — самое позднее по дате начала."""
    for v in sorted(p.values, key=lambda x: x.valid_from, reverse=True):
        if v.valid_from <= today and (v.valid_to is None or v.valid_to >= today):
            return v
    return max(p.values, key=lambda x: x.valid_from) if p.values else None


def _fmt(x: float) -> str:
    return f"{x:g}"


def _param_list(conn: sqlite3.Connection, draft: m.Bundle) -> dict:
    today = date.today()  # noqa: DTZ011 - календарная дата для «действует сегодня»
    rows, usage = [], []
    for p in draft.parameters:
        cur = current_value(p, today)
        unit = CHOICES["unit"].get(p.unit, p.unit)
        titles = [g.title for g in draft.grounds if g.id in grounds_using_param(draft, p.id)]
        got = draft_item(conn, "parameter", p.id)
        rows.append({
            "revision": got[1] if got else "", "id": p.id, "label": p.label, "unit": unit, "derived": p.derived is not None,
            "formula": f"{_fmt(p.derived.multiplier)} × «{(draft.param(p.derived.of).label if draft.param(p.derived.of) else p.derived.of)}»"
            if p.derived else "",
            "value": _fmt(cur.value) if cur else "", "valid_from": cur.valid_from.isoformat() if cur else "",
            "valid_from_ru": cur.valid_from.strftime("%d.%m.%Y") if cur else "—", "verified": bool(cur and cur.verified),
        })
        usage.append({"label": p.label, "titles": titles})
    return {"title": "Цифры и суммы", "rows": rows, "usage": usage, "today": today.isoformat()}


def _param_form(draft: m.Bundle, item: dict, is_new: bool) -> dict:
    affected = []
    if not is_new and item.get("id"):
        for gid in grounds_using_param(draft, item["id"]):
            g = draft.ground(gid)
            affected.append((gid, g.title if g else gid))
    return {"affected": affected}


PARAM = Spec(kind="parameter", base="/params", one="цифра или сумма", many="Цифры и суммы", template="param_form.html",
             parse=parse_param, list_template="param_list.html",
             hint="Числа, которые бот подставляет в тексты. Измените значение и дату «действует с» — старое значение закроется автоматически.",
             new_item=lambda: {"id": "", "label": "", "unit": "EUR", "note": "", "values": [], "derived": None},
             list_ctx=_param_list, form_ctx=_param_form)


# ----------------------------------------------------------------------- facts


def _fact_list(conn: sqlite3.Connection, draft: m.Bundle) -> dict:
    rows = [{"id": f.key, "cells": [f.label, CHOICES["scope"][f.scope], CHOICES["fact_type"][f.type],
                                    str(len(f.options)) if f.options else "—"]} for f in draft.facts]
    return {"title": "Каталог фактов", "headers": ["Название", "Область", "Тип", "Вариантов"], "rows": rows}


FACT = Spec(kind="fact", base="/facts", one="факт", many="Факты", template="fact_form.html", parse=parse_fact,
            new_item=lambda: {"key": "", "scope": "person", "type": "bool", "label": "", "options": [],
                              "depends_on": [], "essential": True, "detect": "", "unit_label": ""},
            list_ctx=_fact_list, form_ctx=lambda d, i, n: {}, id_field="key")


# ----------------------------------------------------------------------- questions


def _question_list(conn: sqlite3.Connection, draft: m.Bundle) -> dict:
    rows = [{"id": q.id, "cells": [q.text, q.fact, str(q.priority), "да" if q.core else "нет"]} for q in draft.questions]
    return {"title": "Вопросы анкеты", "headers": ["Текст", "Факт", "Приоритет", "Основной"], "rows": rows}


def _question_form(draft: m.Bundle, item: dict, is_new: bool) -> dict:
    # в условиях вопросов sponsor.* недопустим
    return {"fact_refs": [(r, t) for r, t in _refs(draft) if not r.startswith("sponsor.")]}


def _refs(draft: m.Bundle):
    from bginfo.admin.crud import kb_ctx

    return kb_ctx(draft)["fact_refs"]


QUESTION = Spec(kind="question", base="/questions", one="вопрос", many="Вопросы", template="question_form.html",
                parse=parse_question,
                new_item=lambda: {"id": "", "fact": "", "text": "", "text_member": "", "help": "", "ask_if": None,
                                  "ask_for": ["main"], "priority": 50, "core": False, "allow_unknown": True},
                list_ctx=_question_list, form_ctx=_question_form)


# ----------------------------------------------------------------------- sources


def _source_list(conn: sqlite3.Connection, draft: m.Bundle) -> dict:
    rows = [{"id": s.id, "cells": [s.title, CHOICES["source_kind"].get(s.kind, s.kind), s.url]} for s in draft.sources]
    return {"title": "Источники", "headers": ["Название", "Тип", "Ссылка"], "rows": rows}


SOURCE = Spec(kind="source", base="/sources", one="источник", many="Источники", template="source_form.html",
              parse=parse_source,
              new_item=lambda: {"id": "", "kind": "law", "title": "", "url": "https://", "note": ""},
              list_ctx=_source_list, form_ctx=lambda d, i, n: {})

for _spec in (PARAM, FACT, QUESTION, SOURCE):
    register_crud(router, _spec)


@router.post("/params/{pid}/quick")
def param_quick(request: Request, pid: str, p: PostCtx = Depends(post_ctx),
                conn: sqlite3.Connection = Depends(get_conn)):
    """Быстро задать новое значение «действует с даты»: прежнее значение закрывается днём раньше."""
    back = redirect("/params")
    cur = draft_item(conn, "parameter", pid)
    raw_rev = str(p.form.get("revision", "")).strip()
    if cur is None or not raw_rev.isdigit():
        flash(request, "Не удалось сохранить: обновите страницу и повторите.", "error")
        return back
    item = cur[0]
    label = item.get("label", pid)
    if item.get("derived"):
        flash(request, f"«{label}» считается по формуле: меняйте исходную цифру.", "error")
        return back
    try:
        value = float(str(p.form.get("value", "")).strip().replace(",", ".").replace(" ", ""))
        start = date.fromisoformat(str(p.form.get("valid_from", "")).strip())
    except ValueError:
        flash(request, f"«{label}»: укажите число и дату «действует с».", "error")
        return back
    verified = p.form.get("verified") in ("on", "1", "true", "yes")
    values = item.get("values", [])
    same = [v for v in values if v["valid_from"] == start.isoformat()]
    if same:
        same[0]["value"], same[0]["verified"] = value, verified
    else:
        if any(date.fromisoformat(v["valid_from"]) > start for v in values):
            flash(request, f"«{label}»: уже есть значение с более поздней датой. Откройте «Подробнее», чтобы изменить периоды.", "error")
            return back
        prev_sources: list[str] = []
        for v in values:
            if v.get("valid_to") is None or date.fromisoformat(v["valid_to"]) >= start:
                v["valid_to"] = (start - timedelta(days=1)).isoformat()
                prev_sources = v.get("source_ids", [])
        values.append({"value": value, "valid_from": start.isoformat(), "valid_to": None, "source_ids": prev_sources,
                       "verified": verified, "note": "", "research_status": ""})
    item["values"] = values
    try:
        save_draft_item(conn, "parameter", item, p.user, int(raw_rev), pid)
    except ValidationError as e:
        flash(request, "Не удалось сохранить: " + "; ".join(ru_errors(e, label)), "error")
    except (ConcurrentEdit, PublishError) as e:
        flash(request, f"«{label}»: {e}", "error")
    else:
        flash(request, f"«{label}»: значение сохранено в черновике. Бот увидит его после публикации.")
    return back
