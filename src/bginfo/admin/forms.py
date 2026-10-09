"""Разбор HTML-форм в элементы черновика (dict в формате JSON модели) и русские тексты ошибок.

Формы не строят модели сами: они собирают dict и отдают его в
``save_draft_item``, где работает pydantic. Ошибки модели переводятся в
короткие русские фразы. Поля, которых нет в форме (version, demo), берутся
из сохранённого элемента, чтобы правка формы их не сбрасывала.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from starlette.datastructures import FormData

from bginfo.admin.common import FIELD_RU
from bginfo.admin.conditions import indices, parse_cond
from bginfo.kb import model as m

_TAGS = {"eq", "in", "cmp", "period", "known_true", "sponsor_basis", "and", "or", "not", "bool", "int", "float", "str"}


# --------------------------------------------------------------------------- errors


def _pattern_msg(pattern: str) -> str:
    if pattern == m.ID_PATTERN:
        return "ID: латинские строчные буквы, цифры и «_», начинается с буквы, 2–64 символа"
    if pattern == m.FACT_REF_PATTERN:
        return "факт должен быть вида self.ключ, sponsor.ключ или case.ключ"
    if "https" in pattern:
        return "ссылка должна начинаться с https://"
    if "a-z0-9_" in pattern:
        return "допустимы латинские строчные буквы, цифры и «_»"
    return "недопустимый формат"


def _message(e: dict) -> str:
    t, ctx = e.get("type", ""), e.get("ctx") or {}
    if t == "missing":
        return "обязательное поле"
    if t == "string_pattern_mismatch":
        return _pattern_msg(str(ctx.get("pattern", "")))
    if t.startswith("date"):
        return "неверная дата, нужен формат ГГГГ-ММ-ДД"
    if t.startswith("int"):
        return "нужно целое число"
    if t.startswith("float"):
        return "нужно число"
    if t.startswith("bool"):
        return "нужно да/нет"
    if t == "literal_error":
        return "недопустимое значение" + (f" (можно: {ctx['expected']})" if ctx.get("expected") else "")
    if t in ("too_short", "too_short_value"):
        return "список не должен быть пустым"
    if t == "extra_forbidden":
        return "лишнее поле"
    if t == "string_type":
        return "нужен текст"
    if t.startswith("union_tag") or t == "model_attributes_type":
        return "неизвестный тип условия"
    return str(e.get("msg", "ошибка"))


def ru_errors(exc: ValidationError, title: str = "") -> list[str]:
    """Короткие русские сообщения об ошибках модели, без повторов; с названием карточки, если оно известно."""
    out: list[str] = []
    for e in exc.errors():
        parts: list[str] = []
        for p in e["loc"]:
            if isinstance(p, int):
                if parts:
                    parts[-1] += f" №{p + 1}"
            elif p in _TAGS:
                continue
            else:
                parts.append(FIELD_RU.get(p, str(p)))
        msg = _message(e)
        line = (" → ".join(parts) + ": " if parts and not msg.startswith("ID:") else "") + msg
        if title:
            line = f"«{title}»: {line}"
        if line not in out:
            out.append(line)
    return out


# --------------------------------------------------------------------------- helpers


def s(form: FormData, name: str) -> str:
    v = form.get(name, "")
    return v.strip() if isinstance(v, str) else ""


def opt(form: FormData, name: str) -> str | None:
    return s(form, name) or None


def checked(form: FormData, name: str) -> bool:
    return form.get(name) in ("on", "1", "true", "yes")


def lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def strs(form: FormData, name: str) -> list[str]:
    return [v.strip() for v in form.getlist(name) if isinstance(v, str) and v.strip()]


def _notes(form: FormData, name: str) -> list[dict]:
    out = []
    for i in indices(form, f"{name}_idx"):
        p = f"{name}-{i}"
        if s(form, f"{p}-text"):
            out.append({"text": s(form, f"{p}-text"), "scope": s(form, f"{p}-scope"),
                        "kind": s(form, f"{p}-kind") or "practice"})
    return out


Parsed = tuple[dict, dict[str, str], list[str]]  # item, json_raw, errors


def _cond(form, prefix, facts, required, label, raw: dict, errs: list[str]):
    c, r, e = parse_cond(form, prefix, facts, required, label)
    if r is not None:
        raw[prefix] = r
    errs.extend(e)
    return c


# --------------------------------------------------------------------------- ground


def parse_ground(form: FormData, base: dict | None, facts: dict[str, m.FactDef], gid: str | None) -> Parsed:
    raw: dict[str, str] = {}
    errs: list[str] = []
    item: dict[str, Any] = dict(base or {})
    goals = strs(form, "goals") + [x.strip() for x in s(form, "goals_extra").split(",") if x.strip()]
    item.update(
        id=gid or s(form, "id"), title=s(form, "title"), regime=s(form, "regime"), subject=s(form, "subject"),
        status=s(form, "status"), review_reason=s(form, "review_reason"), valid_from=opt(form, "valid_from"),
        valid_to=opt(form, "valid_to"), summary=s(form, "summary"), family_note=s(form, "family_note"),
        research_note=s(form, "research_note"),
        goals=list(dict.fromkeys(goals)), checked_at=opt(form, "checked_at"), checked_by=s(form, "checked_by"),
        steps=lines(str(form.get("steps", "") or "")), documents=lines(str(form.get("documents", "") or "")),
        bginfo_source_ids=strs(form, "bginfo_source_ids"),
        restrictions=_notes(form, "restrictions"), practice_notes=_notes(form, "practice_notes"),
    )
    item["applies_if"] = _cond(form, "applies_if", facts, False, "Условие применимости", raw, errs)
    reqs = []
    for i in indices(form, "req_idx"):
        p = f"req-{i}"
        rid = s(form, f"{p}-id")
        label = f"Требование «{rid or i}»"
        reqs.append({
            "id": rid, "label": s(form, f"{p}-label"), "kind": s(form, f"{p}-kind") or "legal",
            "stage": s(form, f"{p}-stage") or "any", "on_fail": s(form, f"{p}-on_fail") or "not_eligible",
            "fail_text": s(form, f"{p}-fail_text"), "unknown_text": s(form, f"{p}-unknown_text"),
            "source_ids": strs(form, f"{p}-source_ids"), "verified": checked(form, f"{p}-verified"),
            "verified_at": opt(form, f"{p}-verified_at"), "verified_by": s(form, f"{p}-verified_by"),
            "disputed": checked(form, f"{p}-disputed"), "dispute_reason": s(form, f"{p}-dispute_reason"),
            "research_status": s(form, f"{p}-research_status"), "research_note": s(form, f"{p}-research_note"),
            "cond": _cond(form, f"{p}-cond", facts, True, label, raw, errs),
        })
    item["requirements"] = reqs
    uns = []
    for i in indices(form, "uns_idx"):
        p = f"uns-{i}"
        uns.append({"reason": s(form, f"{p}-reason"),
                    "cond": _cond(form, f"{p}-cond", facts, True, f"Неподдерживаемое условие №{len(uns) + 1}", raw, errs)})
    item["unsupported"] = uns
    return item, raw, errs


# --------------------------------------------------------------------------- parameter


def parse_param(form: FormData, base: dict | None, facts: dict[str, m.FactDef], pid: str | None) -> Parsed:
    item: dict[str, Any] = dict(base or {})
    item.update(id=pid or s(form, "id"), label=s(form, "label"), unit=s(form, "unit"), note=s(form, "note"))
    if s(form, "kind") == "derived":
        item["derived"] = {"of": s(form, "derived_of"), "multiplier": s(form, "derived_mult") or "1"}
        item["values"] = []
    else:
        item["derived"] = None
        vals = []
        for i in indices(form, "val_idx"):
            p = f"val-{i}"
            if not s(form, f"{p}-value") and not s(form, f"{p}-valid_from"):
                continue
            vals.append({
                "value": s(form, f"{p}-value").replace(",", "."), "valid_from": s(form, f"{p}-valid_from"),
                "valid_to": opt(form, f"{p}-valid_to"), "source_ids": strs(form, f"{p}-source_ids"),
                "verified": checked(form, f"{p}-verified"), "note": s(form, f"{p}-note"),
                "research_status": s(form, f"{p}-research_status"),
            })
        item["values"] = vals
    return item, {}, []


# --------------------------------------------------------------------------- fact / question / source


def parse_fact(form: FormData, base: dict | None, facts: dict[str, m.FactDef], key: str | None) -> Parsed:
    item: dict[str, Any] = dict(base or {})
    opts = []
    for i in indices(form, "opt_idx"):
        p = f"opt-{i}"
        if s(form, f"{p}-value") or s(form, f"{p}-label"):
            opts.append({"value": s(form, f"{p}-value"), "label": s(form, f"{p}-label"), "detect": s(form, f"{p}-detect")})
    item.update(
        key=key or s(form, "key"), scope=s(form, "scope"), type=s(form, "type"), label=s(form, "label"),
        essential=checked(form, "essential"), depends_on=strs(form, "depends_on"), detect=s(form, "detect"),
        unit_label=s(form, "unit_label"), options=opts,
    )
    return item, {}, []


def parse_question(form: FormData, base: dict | None, facts: dict[str, m.FactDef], qid: str | None) -> Parsed:
    raw: dict[str, str] = {}
    errs: list[str] = []
    item: dict[str, Any] = dict(base or {})
    item.update(
        id=qid or s(form, "id"), fact=s(form, "fact"), text=s(form, "text"), text_member=s(form, "text_member"),
        help=s(form, "help"), ask_for=strs(form, "ask_for"), priority=s(form, "priority"),
        core=checked(form, "core"), allow_unknown=checked(form, "allow_unknown"),
    )
    item["ask_if"] = _cond(form, "ask_if", facts, False, "Условие «задавать, если»", raw, errs)
    return item, raw, errs


def parse_source(form: FormData, base: dict | None, facts: dict[str, m.FactDef], sid: str | None) -> Parsed:
    item: dict[str, Any] = dict(base or {})
    item.update(id=sid or s(form, "id"), kind=s(form, "kind"), title=s(form, "title"), url=s(form, "url"),
                note=s(form, "note"))
    return item, {}, []
