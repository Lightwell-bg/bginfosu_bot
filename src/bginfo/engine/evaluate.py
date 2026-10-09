"""Детерминированная проверка условий оснований.

Трёхзначная логика: True / False / Unknown. Неизвестное условие не считается
ни выполненным, ни нарушенным. Для каждого основания вычисляется один из пяти
статусов, и к нему прикладывается проверяемая трасса: какие факты использованы,
какие требования выполнены, какие нет, какие неизвестны, какой выпуск базы
применялся.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from bginfo.engine.facts import CASE, MAIN, MISSING, Profile
from bginfo.kb import model as m


class Tri(str, Enum):
    T = "true"
    F = "false"
    U = "unknown"


class Status(str, Enum):
    FIT = "fit"                        # Предварительно подходит
    CLARIFY = "clarify"                # Нужно уточнить
    POSSIBLE_AFTER = "possible_after"  # Возможно после выполнения конкретного условия
    NOT_ELIGIBLE = "not_eligible"      # Не подходит по указанным данным
    INDIVIDUAL = "individual"          # Нужен индивидуальный разбор


STATUS_LABELS = {
    Status.FIT: "Предварительно подходит",
    Status.CLARIFY: "Нужно уточнить",
    Status.POSSIBLE_AFTER: "Возможно после выполнения условия",
    Status.NOT_ELIGIBLE: "Не подходит по указанным данным",
    Status.INDIVIDUAL: "Нужен индивидуальный разбор",
}

FactKey = tuple[str, str]  # (participant_id, fact_key)


@dataclass
class Leaf:
    ref: str
    participant: str
    value: Any
    result: Tri


@dataclass
class ReqResult:
    id: str
    label: str
    result: Tri
    on_fail: str
    disputed: bool
    kind: str
    leaves: list[Leaf] = field(default_factory=list)
    missing: list[FactKey] = field(default_factory=list)


@dataclass
class GroundResult:
    ground_id: str
    subject: str  # participant id
    status: Status
    reasons: list[str] = field(default_factory=list)
    requirements: list[ReqResult] = field(default_factory=list)
    missing: list[FactKey] = field(default_factory=list)
    applicable: Tri = Tri.T

    @property
    def met(self) -> list[ReqResult]:
        return [r for r in self.requirements if r.result is Tri.T and not r.disputed]

    @property
    def unmet(self) -> list[ReqResult]:
        return [r for r in self.requirements if r.result is Tri.F]

    @property
    def unknown(self) -> list[ReqResult]:
        return [r for r in self.requirements if r.result is Tri.U]

    def facts_used(self) -> list[Leaf]:
        seen: dict[tuple[str, str], Leaf] = {}
        for r in self.requirements:
            for lf in r.leaves:
                if lf.value is not MISSING:
                    seen[(lf.participant, lf.ref)] = lf
        return list(seen.values())


@dataclass
class Evaluation:
    release_id: int | None
    as_of: date
    results: list[GroundResult]

    def for_subject(self, pid: str) -> list[GroundResult]:
        return [r for r in self.results if r.subject == pid]

    def get(self, gid: str, pid: str = MAIN) -> GroundResult | None:
        return next((r for r in self.results if r.ground_id == gid and r.subject == pid), None)

    def missing(self) -> list[FactKey]:
        """Недостающие факты всех ещё не решённых оснований (с повторами — для подсчёта веса)."""
        out: list[FactKey] = []
        for r in self.results:
            if r.status is not Status.NOT_ELIGIBLE:
                out.extend(r.missing)
        return out


# --------------------------------------------------------------------------- params


class ParamError(ValueError):
    pass


def param_value(kb: m.Bundle, pid: str, as_of: date, _stack: tuple[str, ...] = ()) -> float | None:
    """Значение общего показателя, действующее на дату ``as_of``; None — не задано."""
    if pid in _stack:
        raise ParamError(f"цикл в показателях: {' -> '.join(_stack + (pid,))}")
    p = kb.param(pid)
    if p is None:
        return None
    if p.derived is not None:
        base = param_value(kb, p.derived.of, as_of, _stack + (pid,))
        if base is None:
            return None
        return float(Decimal(str(base)) * Decimal(str(p.derived.multiplier)))
    for v in p.values:
        if v.valid_from <= as_of and (v.valid_to is None or as_of <= v.valid_to):
            return v.value
    return None


# --------------------------------------------------------------------------- conditions


@dataclass
class _Ctx:
    kb: m.Bundle
    profile: Profile
    subject: str
    as_of: date
    sponsor_results: list[GroundResult] | None = None
    leaves: list[Leaf] = field(default_factory=list)


def _resolve(ref: str, ctx: _Ctx) -> tuple[str, str]:
    role, key = ref.split(".", 1)
    pid = {"self": ctx.subject, "sponsor": MAIN, "case": CASE}[role]
    return pid, key


def _num(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None or v is MISSING:
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001 — некорректное значение = неизвестно
        return None


def _cmp(a: Decimal, op: str, b: Decimal) -> bool:
    return {">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b, "==": a == b}[op]


def _add_months(d: date, months: int) -> date:
    y, mo = divmod(d.month - 1 + months, 12)
    y += d.year
    mo += 1
    import calendar

    day = min(d.day, calendar.monthrange(y, mo)[1])
    return date(y, mo, day)


def _leaf(ref: str, ctx: _Ctx, fn) -> tuple[Tri, set[FactKey]]:
    pid, key = _resolve(ref, ctx)
    v = ctx.profile.value(pid, key)
    if v is MISSING:
        ctx.leaves.append(Leaf(ref, pid, MISSING, Tri.U))
        return Tri.U, {(pid, key)}
    r = fn(v)
    ctx.leaves.append(Leaf(ref, pid, v, r))
    return r, set()


def _b(x: bool) -> Tri:
    return Tri.T if x else Tri.F


def eval_cond(c: m.Condition, ctx: _Ctx) -> tuple[Tri, set[FactKey]]:
    """Вычислить условие. Возвращает результат и недостающие факты, от которых он зависит."""
    if isinstance(c, m.CondEq):
        return _leaf(c.fact, ctx, lambda v: _b(c.value in v) if isinstance(v, list) else _b(v == c.value))
    if isinstance(c, m.CondIn):
        vals = set(c.values)
        return _leaf(c.fact, ctx, lambda v: _b(bool(vals & set(v))) if isinstance(v, list) else _b(v in vals))
    if isinstance(c, m.CondKnownTrue):
        return _leaf(c.fact, ctx, lambda v: _b(v is True or (isinstance(v, list) and len(v) > 0)))
    if isinstance(c, m.CondCmp):
        if c.param is not None:
            pv = param_value(ctx.kb, c.param, ctx.as_of)
            if pv is None:
                return Tri.U, set()
            threshold = Decimal(str(pv)) * Decimal(str(c.multiplier))
        else:
            threshold = Decimal(str(c.value)) * Decimal(str(c.multiplier))

        def f(v: Any) -> Tri:
            n = _num(v)
            return Tri.U if n is None else _b(_cmp(n, c.cmp, threshold))

        return _leaf(c.fact, ctx, f)
    if isinstance(c, m.CondPeriod):
        months = c.months
        if c.param is not None:
            pv = param_value(ctx.kb, c.param, ctx.as_of)
            if pv is None:
                return Tri.U, set()
            months = int(pv)
        target = _add_months(ctx.as_of, int(months or 0))

        def f(v: Any) -> Tri:
            try:
                d = v if isinstance(v, date) else date.fromisoformat(str(v))
            except ValueError:
                return Tri.U
            return _b(d >= target if c.cmp == ">=" else d <= target)

        return _leaf(c.fact, ctx, f)
    if isinstance(c, m.CondSponsorBasis):
        status = ctx.profile.value(MAIN, "status_in_bg")
        if status is not MISSING and status in c.statuses:
            ctx.leaves.append(Leaf("sponsor.status_in_bg", MAIN, status, Tri.T))
            return Tri.T, set()
        res = [r for r in (ctx.sponsor_results or []) if r.status is not Status.NOT_ELIGIBLE]
        if any(r.status is Status.FIT for r in res):
            return Tri.T, set()
        if res:
            miss: set[FactKey] = set()
            for r in res:
                miss |= set(r.missing)
            return Tri.U, miss
        if status is MISSING:
            return Tri.U, {(MAIN, "status_in_bg")}
        return Tri.F, set()
    if isinstance(c, m.CondAnd):
        parts = [eval_cond(x, ctx) for x in c.all]
        if any(t is Tri.F for t, _ in parts):
            return Tri.F, set()
        if all(t is Tri.T for t, _ in parts):
            return Tri.T, set()
        return Tri.U, set().union(*(ms for t, ms in parts if t is Tri.U))
    if isinstance(c, m.CondOr):
        parts = [eval_cond(x, ctx) for x in c.any]
        if any(t is Tri.T for t, _ in parts):
            return Tri.T, set()
        if all(t is Tri.F for t, _ in parts):
            return Tri.F, set()
        return Tri.U, set().union(*(ms for t, ms in parts if t is Tri.U))
    if isinstance(c, m.CondNot):
        t, ms = eval_cond(c.cond, ctx)
        return ({Tri.T: Tri.F, Tri.F: Tri.T, Tri.U: Tri.U}[t], ms)
    raise TypeError(f"неизвестный тип условия: {type(c).__name__}")  # pragma: no cover


# --------------------------------------------------------------------------- grounds


def _in_period(g: m.Ground, as_of: date) -> bool:
    return (g.valid_from is None or g.valid_from <= as_of) and (g.valid_to is None or as_of <= g.valid_to)


def evaluate_ground(
    g: m.Ground, kb: m.Bundle, profile: Profile, subject: str, as_of: date,
    sponsor_results: list[GroundResult] | None = None,
) -> GroundResult | None:
    """Оценить одно основание для одного участника. None — основание вне периода действия."""
    if not _in_period(g, as_of):
        return None
    res = GroundResult(g.id, subject, Status.CLARIFY)
    missing: set[FactKey] = set()

    if g.applies_if is not None:
        ctx = _Ctx(kb, profile, subject, as_of, sponsor_results)
        t, ms = eval_cond(g.applies_if, ctx)
        res.applicable = t
        if t is Tri.F:
            res.status = Status.NOT_ELIGIBLE
            res.reasons.append("Основание рассчитано на другую категорию заявителей.")
            return res
        missing |= ms

    unsupported_hit = []
    unsupported_unknown: list[str] = []
    for u in g.unsupported:
        t, ums = eval_cond(u.cond, _Ctx(kb, profile, subject, as_of, sponsor_results))
        if t is Tri.T:
            unsupported_hit.append(u.reason)
        elif t is Tri.U:
            unsupported_unknown.append(u.reason)
            missing |= ums

    whole_disputed = g.status == "needs_review" and not any(r.disputed for r in g.requirements)
    for req in g.requirements:
        ctx = _Ctx(kb, profile, subject, as_of, sponsor_results)
        t, ms = eval_cond(req.cond, ctx)
        disputed = req.disputed or whole_disputed or not req.verified
        rr = ReqResult(req.id, req.label, t, req.on_fail, disputed, req.kind, ctx.leaves, sorted(ms))
        res.requirements.append(rr)
        if t is Tri.U:
            missing |= ms

    hard_fail = [r for r in res.requirements if r.result is Tri.F and r.on_fail == "not_eligible" and not r.disputed]
    unknown = [r for r in res.requirements if r.result is Tri.U and not r.disputed]
    individual = [r for r in res.requirements if r.disputed or (r.result is Tri.F and r.on_fail == "individual_review")]
    after = [r for r in res.requirements if r.result is Tri.F and r.on_fail == "possible_after" and not r.disputed]

    if hard_fail:
        res.status = Status.NOT_ELIGIBLE
        res.reasons += [f"Не выполнено: {r.label}" for r in hard_fail]
        res.missing = []
        return res
    if unsupported_hit:
        res.status = Status.INDIVIDUAL
        res.reasons += unsupported_hit
    elif res.applicable is Tri.U or unknown:
        res.status = Status.CLARIFY
        res.reasons += [f"Неизвестно: {r.label}" for r in unknown]
    elif individual:
        res.status = Status.INDIVIDUAL
        if g.status == "needs_review":
            res.reasons.append(g.review_reason or "Условия основания сейчас уточняются.")
        res.reasons += [f"Требует индивидуальной проверки: {r.label}" for r in individual]
    elif after:
        res.status = Status.POSSIBLE_AFTER
        res.reasons += [f"Нужно выполнить: {r.label}" for r in after]
    else:
        res.status = Status.FIT
    if unsupported_unknown and res.status in (Status.FIT, Status.POSSIBLE_AFTER):
        # исключающее условие не удалось проверить — «подходит» утверждать нельзя
        res.status = Status.CLARIFY
        res.reasons += [f"Неизвестно, относится ли к вам: {r}" for r in unsupported_unknown]
    res.missing = sorted(missing)
    return res


def evaluate_all(kb: m.Bundle, profile: Profile, as_of: date, release_id: int | None = None) -> Evaluation:
    """Оценить все действующие основания для основного заявителя и членов семьи.

    Основания заявителя проверяются и для взрослых членов семьи (у супруга может
    быть своё основание). Основания члена семьи (subject=family_member)
    проверяются для каждого члена семьи относительно основного заявителя.
    Основание заявителя на семью автоматически не переносится.
    """
    grounds = kb.active_grounds()
    results: list[GroundResult] = []
    main_results: list[GroundResult] = []
    for g in grounds:
        if g.subject == "applicant":
            r = evaluate_ground(g, kb, profile, MAIN, as_of)
            if r is not None:
                main_results.append(r)
    results += main_results
    for p in profile.members():
        for g in grounds:
            if g.subject == "family_member" or (g.subject == "applicant" and p.relation in ("spouse", "parent", "other")):
                r = evaluate_ground(g, kb, profile, p.id, as_of, sponsor_results=main_results)
                if r is not None:
                    results.append(r)
    return Evaluation(release_id, as_of, results)
