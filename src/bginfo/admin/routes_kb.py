"""Показатели, каталог фактов, вопросы и источники."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter

from bginfo.admin.common import CHOICES
from bginfo.admin.crud import Spec, register_crud
from bginfo.admin.forms import parse_fact, parse_param, parse_question, parse_source
from bginfo.kb import model as m
from bginfo.kb.validate import grounds_using_param

router = APIRouter()


# ----------------------------------------------------------------------- parameters


def _param_list(conn: sqlite3.Connection, draft: m.Bundle) -> dict:
    rows = []
    for p in draft.parameters:
        cur = "формула: " + f"{p.derived.of} × {p.derived.multiplier:g}" if p.derived else (
            f"{p.values[-1].value:g} (с {p.values[-1].valid_from:%d.%m.%Y})" if p.values else "—")
        rows.append({"id": p.id, "cells": [p.label, CHOICES["unit"].get(p.unit, p.unit), cur,
                                           ", ".join(grounds_using_param(draft, p.id)) or "—"]})
    return {"title": "Общие показатели", "headers": ["Название", "Единица", "Значение", "Используется в основаниях"],
            "rows": rows}


def _param_form(draft: m.Bundle, item: dict, is_new: bool) -> dict:
    affected = []
    if not is_new and item.get("id"):
        for gid in grounds_using_param(draft, item["id"]):
            g = draft.ground(gid)
            affected.append((gid, g.title if g else gid))
    return {"affected": affected}


PARAM = Spec(kind="parameter", base="/params", one="показатель", many="Показатели", template="param_form.html",
             parse=parse_param,
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
