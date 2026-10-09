"""Проверка согласованности набора базы знаний перед публикацией.

Ошибки (level=error) блокируют публикацию, предупреждения показываются в админке.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise

from bginfo.kb import model as m

PLACEHOLDER = re.compile(r"\{param:([a-z][a-z0-9_]*)\}")
_NUMERIC = {"money", "int"}


@dataclass(frozen=True)
class Issue:
    level: str  # error | warning
    where: str
    message: str


def walk(c: m.Condition | None) -> Iterator[m.Condition]:
    if c is None:
        return
    yield c
    if isinstance(c, m.CondAnd):
        for x in c.all:
            yield from walk(x)
    elif isinstance(c, m.CondOr):
        for x in c.any:
            yield from walk(x)
    elif isinstance(c, m.CondNot):
        yield from walk(c.cond)


def ground_texts(g: m.Ground) -> Iterator[tuple[str, str]]:
    yield "summary", g.summary
    yield "family_note", g.family_note
    for i, s in enumerate(g.steps):
        yield f"steps[{i}]", s
    for i, s in enumerate(g.documents):
        yield f"documents[{i}]", s
    for i, n in enumerate(g.restrictions):
        yield f"restrictions[{i}]", n.text
    for i, n in enumerate(g.practice_notes):
        yield f"practice_notes[{i}]", n.text
    for u in g.unsupported:
        yield "unsupported", u.reason
    for r in g.requirements:
        yield f"requirements.{r.id}.label", r.label
        yield f"requirements.{r.id}.fail_text", r.fail_text
        yield f"requirements.{r.id}.unknown_text", r.unknown_text


def ground_conditions(g: m.Ground) -> Iterator[tuple[str, m.Condition]]:
    if g.applies_if is not None:
        yield "applies_if", g.applies_if
    for u in g.unsupported:
        yield "unsupported", u.cond
    for r in g.requirements:
        yield f"requirements.{r.id}", r.cond


def params_in_condition(c: m.Condition) -> set[str]:
    out = set()
    for n in walk(c):
        p = getattr(n, "param", None)
        if p:
            out.add(p)
    return out


def param_closure(kb: m.Bundle, pids: set[str]) -> set[str]:
    """Показатели плюс все, от которых они производны."""
    out, stack = set(), list(pids)
    while stack:
        p = stack.pop()
        if p in out:
            continue
        out.add(p)
        pp = kb.param(p)
        if pp is not None and pp.derived is not None:
            stack.append(pp.derived.of)
    return out


def grounds_using_param(kb: m.Bundle, pid: str) -> list[str]:
    """Какие основания затронет изменение показателя (с учётом производных)."""
    affected = []
    for g in kb.grounds:
        used: set[str] = set()
        for _, c in ground_conditions(g):
            used |= params_in_condition(c)
        for _, t in ground_texts(g):
            used |= set(PLACEHOLDER.findall(t or ""))
        if pid in param_closure(kb, used):
            affected.append(g.id)
    return affected


def _dupes(ids: list[str]) -> set[str]:
    seen, d = set(), set()
    for i in ids:
        (d if i in seen else seen).add(i)
    return d


def validate(kb: m.Bundle) -> list[Issue]:
    issues: list[Issue] = []
    err = lambda w, msg: issues.append(Issue("error", w, msg))
    warn = lambda w, msg: issues.append(Issue("warning", w, msg))

    for name, ids in (
        ("основания", [g.id for g in kb.grounds]),
        ("показатели", [p.id for p in kb.parameters]),
        ("факты", [f.key for f in kb.facts]),
        ("вопросы", [q.id for q in kb.questions]),
        ("источники", [s.id for s in kb.sources]),
    ):
        for d in _dupes(ids):
            err(name, f"повторяющийся ID «{d}»")

    source_ids = {s.id for s in kb.sources}
    param_ids = {p.id for p in kb.parameters}
    facts = {f.key: f for f in kb.facts}
    questioned = {q.fact for q in kb.questions}

    # ---- показатели: периоды, циклы, источники
    for p in kb.parameters:
        w = f"показатель {p.id}"
        if p.derived is None and not p.values:
            err(w, "нет ни значений, ни формулы")
        if p.derived is not None and p.values:
            err(w, "одновременно задано значение и формула")
        if p.derived is not None and p.derived.of not in param_ids:
            err(w, f"формула ссылается на несуществующий показатель «{p.derived.of}»")
        vals = sorted(p.values, key=lambda v: v.valid_from)
        for a, b in pairwise(vals):
            if a.valid_to is None or a.valid_to >= b.valid_from:
                err(w, f"пересекаются периоды действия: с {a.valid_from} и с {b.valid_from}")
        for v in vals:
            if v.valid_to is not None and v.valid_to < v.valid_from:
                err(w, f"конец периода раньше начала ({v.valid_from})")
            for s in v.source_ids:
                if s not in source_ids:
                    err(w, f"нет источника «{s}»")
    for p in kb.parameters:
        seen, cur = [p.id], p
        while cur is not None and cur.derived is not None:
            nxt = cur.derived.of
            if nxt in seen:
                err(f"показатель {p.id}", f"цикл зависимостей: {' -> '.join(seen + [nxt])}")
                break
            seen.append(nxt)
            cur = kb.param(nxt)

    # ---- факты: зависимости, опции, циклы
    for f in kb.facts:
        w = f"факт {f.key}"
        if f.type in ("enum", "multi") and not f.options:
            err(w, "для выбора из списка нужны варианты")
        for d in f.depends_on:
            if d not in facts:
                err(w, f"зависит от несуществующего факта «{d}»")
    state: dict[str, int] = {}

    def dfs(k: str, path: list[str]) -> None:
        state[k] = 1
        for d in facts[k].depends_on if k in facts else []:
            if state.get(d) == 1:
                err(f"факт {k}", f"цикл зависимостей: {' -> '.join(path + [d])}")
            elif state.get(d) is None and d in facts:
                dfs(d, path + [d])
        state[k] = 2

    for k in facts:
        if k not in state:
            dfs(k, [k])

    # ---- проверка условий
    def check_cond(c: m.Condition, w: str, roles: set[str]) -> set[str]:
        used = set()
        for n in walk(c):
            ref = getattr(n, "fact", None)
            if ref:
                role, key = ref.split(".", 1)
                used.add(key)
                if role not in roles:
                    err(w, f"роль «{role}» недопустима здесь ({ref})")
                fd = facts.get(key)
                if fd is None:
                    err(w, f"ссылка на несуществующий факт «{key}»")
                    continue
                if role in ("self", "sponsor") and fd.scope != "person":
                    err(w, f"«{key}» — факт анкеты, используйте case.{key}")
                if role == "case" and fd.scope != "case":
                    err(w, f"«{key}» — факт участника, используйте self.{key}")
                opts = {o.value for o in fd.options}
                if isinstance(n, m.CondCmp) and fd.type not in _NUMERIC:
                    err(w, f"числовое сравнение с нечисловым фактом «{key}»")
                if isinstance(n, m.CondPeriod) and fd.type != "date":
                    err(w, f"проверка периода для факта «{key}», который не дата")
                if isinstance(n, (m.CondEq, m.CondIn)):
                    vals = [n.value] if isinstance(n, m.CondEq) else n.values
                    if fd.type == "bool" and any(not isinstance(v, bool) for v in vals):
                        err(w, f"для да/нет-факта «{key}» значения должны быть true/false")
                    if fd.type in ("enum", "multi"):
                        for v in vals:
                            if v not in opts:
                                err(w, f"значение «{v}» отсутствует в вариантах факта «{key}»")
            if isinstance(n, (m.CondCmp, m.CondPeriod)):
                if (n.param is None) == (getattr(n, "value", None) is None and getattr(n, "months", None) is None):
                    err(w, "нужно указать либо число, либо показатель")
                if n.param is not None:
                    if n.param not in param_ids:
                        err(w, f"ссылка на несуществующий показатель «{n.param}»")
                    elif isinstance(n, m.CondPeriod) and kb.param(n.param).unit not in ("months",):
                        err(w, f"показатель «{n.param}» должен быть в месяцах")
        return used

    for q in kb.questions:
        w = f"вопрос {q.id}"
        if q.fact not in facts:
            err(w, f"вопрос к несуществующему факту «{q.fact}»")
        if q.ask_if is not None:
            check_cond(q.ask_if, w, {"self", "case"})

    for g in kb.grounds:
        w = f"основание {g.id}"
        roles = {"self", "case"} | ({"sponsor"} if g.subject == "family_member" else set())
        used: set[str] = set()
        for where, c in ground_conditions(g):
            used |= check_cond(c, f"{w}: {where}", roles)
        for d in _dupes([r.id for r in g.requirements]):
            err(w, f"повторяющийся ID требования «{d}»")
        for where, t in ground_texts(g):
            for p in PLACEHOLDER.findall(t or ""):
                if p not in param_ids:
                    err(f"{w}: {where}", f"в тексте показатель «{p}», которого нет")
        for s in g.bginfo_source_ids:
            src = kb.source(s)
            if src is None:
                err(w, f"нет источника «{s}»")
            elif src.kind != "bginfo":
                err(w, f"«{s}» не страница BGINFO")
        for r in g.requirements:
            for s in r.source_ids:
                if s not in source_ids:
                    err(f"{w}: {r.id}", f"нет источника «{s}»")
        if g.valid_from and g.valid_to and g.valid_to < g.valid_from:
            err(w, "конец периода действия раньше начала")

        if g.status in ("published", "needs_review"):
            if not g.requirements:
                err(w, "у действующего основания нет требований")
            for key in used:
                fd = facts.get(key)
                if key not in questioned and not (fd is not None and fd.derived):
                    err(w, f"для факта «{key}» нет вопроса — условие нельзя будет выяснить")
            if not g.bginfo_source_ids:
                warn(w, "нет ссылки на страницу BGINFO")
        if g.status == "published":
            used_params: set[str] = set()
            for _, c in ground_conditions(g):
                used_params |= params_in_condition(c)
            for _, t in ground_texts(g):
                used_params |= set(PLACEHOLDER.findall(t or ""))
            for pid in param_closure(kb, used_params):
                p = kb.param(pid)
                if p is not None and any(not v.verified for v in p.values):
                    err(w, f"использует непроверенное значение показателя «{pid}»")
            if not g.checked_at or not g.checked_by:
                err(w, "нет даты содержательной проверки или автора")
            for r in g.requirements:
                if not r.verified:
                    err(f"{w}: {r.id}", "требование не подтверждено — опубликовать нельзя (пометьте «требует проверки»)")
                elif not r.source_ids:
                    err(f"{w}: {r.id}", "у подтверждённого требования нет источника")
                elif not r.verified_at or not r.verified_by:
                    err(f"{w}: {r.id}", "нет даты или автора проверки требования")
        if g.status == "needs_review" and not g.review_reason.strip():
            err(w, "укажите причину пометки «требует проверки»")
        if g.demo and not kb.demo:
            err(w, "тестовая карточка в рабочей базе")
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "error"]
