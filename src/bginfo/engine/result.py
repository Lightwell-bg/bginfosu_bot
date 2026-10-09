"""Сборка итогового результата из расчёта и проверенных блоков базы знаний.

Всё юридически значимое (статусы, условия, ограничения, шаги, документы,
ссылки, суммы) берётся отсюда, из базы и расчёта. Генеративная модель только
дописывает персональное пояснение «почему», которое проверяется отдельно
(ai/explain.py). Ссылки — только из источников базы по их ID.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from bginfo.engine.evaluate import STATUS_LABELS, Evaluation, GroundResult, Status, Tri, param_value
from bginfo.engine.facts import CASE, MAIN, MISSING, Profile
from bginfo.kb import model as m
from bginfo.kb.validate import PLACEHOLDER

ORDER = {Status.FIT: 0, Status.POSSIBLE_AFTER: 1, Status.CLARIFY: 2, Status.INDIVIDUAL: 3, Status.NOT_ELIGIBLE: 4}
UNIT_SUFFIX = {"EUR": " €", "EUR/month": " € в месяц", "EUR/year": " € в год", "months": " мес.", "years": " г.", "count": ""}


def fmt_number(v: float) -> str:
    if abs(v - round(v)) < 0.005:
        s = f"{round(v):,}".replace(",", " ")
    else:
        s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return s


def render_text(t: str, kb: m.Bundle, as_of: date) -> str:
    """Подставить значения общих показателей вместо {param:id}."""

    def sub(mt: re.Match) -> str:
        p = kb.param(mt.group(1))
        v = param_value(kb, mt.group(1), as_of) if p else None
        if p is None or v is None:
            return "[значение уточняется]"
        return fmt_number(v) + UNIT_SUFFIX.get(p.unit, "")

    return PLACEHOLDER.sub(sub, t or "")


def display_value(kb: m.Bundle, key: str, value: Any) -> str:
    fd = kb.fact(key)
    if fd is None:
        return str(value)
    if fd.type == "bool":
        return "да" if value else "нет"
    if fd.type in ("enum", "multi"):
        labels = {o.value: o.label for o in fd.options}
        vals = value if isinstance(value, list) else [value]
        return ", ".join(labels.get(v, str(v)) for v in vals) or "ничего из перечисленного"
    if fd.type == "money":
        return fmt_number(float(value)) + (f" {fd.unit_label}" if fd.unit_label else " €")
    if fd.type == "date":
        try:
            d = date.fromisoformat(str(value))
            return d.strftime("%d.%m.%Y")
        except ValueError:
            return str(value)
    return str(value)


@dataclass
class Link:
    title: str
    url: str


@dataclass
class GroundView:
    ground_id: str
    subject: str
    title: str
    status: Status
    status_label: str
    summary: str
    met: list[str] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)
    unmet: list[tuple[str, str]] = field(default_factory=list)
    individual: list[str] = field(default_factory=list)
    restrictions: list[str] = field(default_factory=list)
    practice_notes: list[tuple[str, str]] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    family_note: str = ""
    links: list[Link] = field(default_factory=list)
    official: list[Link] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    facts_used: list[tuple[str, str]] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    personal: str = ""  # проверенное персональное пояснение модели (plain text)
    review_reason: str = ""


@dataclass
class MemberView:
    pid: str
    label: str
    relation: str
    grounds: list[GroundView] = field(default_factory=list)
    note: str = ""


@dataclass
class ResultView:
    kind: str  # ok | none | unsupported
    release_id: int | None
    as_of: date
    demo: bool = False
    primary: GroundView | None = None
    alternatives: list[GroundView] = field(default_factory=list)
    not_suitable: list[GroundView] = field(default_factory=list)
    family: list[MemberView] = field(default_factory=list)
    message: str = ""
    intro: str = ""
    family_intro: str = ""
    explain_source: str = "template"


def _goal_match(g: m.Ground, profile: Profile) -> bool:
    goal = profile.value(CASE, "goal")
    return goal is not MISSING and goal in g.goals


def rank_key(r: GroundResult, g: m.Ground, profile: Profile) -> tuple:
    total = max(1, len(r.requirements))
    met = len(r.met)
    return (ORDER[r.status], -met / total, len(r.unknown), 0 if _goal_match(g, profile) else 1,
            len(g.restrictions), g.id)


def ground_view(r: GroundResult, kb: m.Bundle, profile: Profile, as_of: date) -> GroundView:
    g = kb.ground(r.ground_id)
    t = lambda s: render_text(s, kb, as_of)
    reqs = {q.id: q for q in g.requirements}
    v = GroundView(
        ground_id=g.id, subject=r.subject, title=g.title, status=r.status, status_label=STATUS_LABELS[r.status],
        summary=t(g.summary), restrictions=[t(n.text) for n in g.restrictions],
        practice_notes=[(t(n.text), n.scope) for n in g.practice_notes],
        steps=[t(s) for s in g.steps], documents=[t(s) for s in g.documents], family_note=t(g.family_note),
        review_reason=g.review_reason if g.status == "needs_review" else "",
    )
    for rr in r.requirements:
        q = reqs[rr.id]
        if rr.disputed:
            v.individual.append(t(q.label))
        elif rr.result is Tri.T:
            v.met.append(t(q.label))
        elif rr.result is Tri.F:
            v.unmet.append((t(q.label), t(q.fail_text)))
        else:
            v.unknown.append((t(q.label), t(q.unknown_text)))
    v.reasons = [t(x) for x in r.reasons]
    for lf in r.facts_used():
        key = lf.ref.split(".", 1)[1]
        fd = kb.fact(key)
        if fd is not None:
            v.facts_used.append((fd.label, display_value(kb, key, lf.value)))
    for sid in g.bginfo_source_ids:
        s = kb.source(sid)
        if s is not None:
            v.links.append(Link(s.title, s.url))
    seen = set()
    for q in g.requirements:
        for sid in q.source_ids:
            s = kb.source(sid)
            if s is not None and s.kind != "bginfo" and sid not in seen:
                seen.add(sid)
                v.official.append(Link(s.title, s.url))
    total = len(r.requirements)
    if r.status is Status.FIT:
        v.criteria.append("по вашим ответам выполнены все проверяемые условия")
    elif total:
        v.criteria.append(f"по вашим ответам выполнено условий: {len(r.met)} из {total}")
    if r.unknown:
        v.criteria.append(f"нужно уточнить: {len(r.unknown)}")
    if _goal_match(g, profile):
        v.criteria.append("совпадает с вашей целью переезда")
    if g.restrictions:
        v.criteria.append(f"есть существенные ограничения: {len(g.restrictions)}")
    return v


def build_result(ev: Evaluation, kb: m.Bundle, profile: Profile) -> ResultView:
    as_of = ev.as_of
    rv = ResultView(kind="ok", release_id=ev.release_id, as_of=as_of, demo=kb.demo)

    raw = profile.value(MAIN, "citizenship_group")
    cits = set(raw) if isinstance(raw, list) else ({raw} if raw is not MISSING else set())
    cit = "bg" if "bg" in cits else "eu" if "eu" in cits else "third" if cits else None
    regimes = {g.regime for g in kb.active_grounds()}
    if cit == "bg":
        rv.kind = "unsupported"
        rv.message = ("Вы указали болгарское гражданство: гражданину Болгарии разрешение на проживание не нужно. "
                      "Если вопрос касается членов семьи с другим гражданством, напишите об этом — разберём отдельно.")
        return rv
    if cit == "eu" and "eu" not in regimes:
        rv.kind = "unsupported"
        rv.message = ("Для граждан ЕС, ЕЭЗ и Швейцарии действует отдельный порядок регистрации. Проверенных правил "
                      "для него в боте пока нет, поэтому похожие процедуры для других стран применять нельзя. "
                      "Рекомендуем индивидуальный разбор.")
        return rv

    main = [r for r in ev.for_subject(MAIN) if kb.ground(r.ground_id) is not None]
    main.sort(key=lambda r: rank_key(r, kb.ground(r.ground_id), profile))
    candidates = [r for r in main if r.status is not Status.NOT_ELIGIBLE]
    top = candidates[0] if candidates else None
    if top is not None and top.status is Status.CLARIFY and not top.met:
        # Ничего не выполнено — объявлять «вариант для проверки первым» нечестно.
        rv.kind = "clarify"
        rv.message = ("Пока недостаточно сведений, чтобы выделить подходящий вариант. Ниже — направления, "
                      "которые можно проверить; ответьте на уточняющие вопросы или закажите консультацию.")
        rv.alternatives = [ground_view(r, kb, profile, as_of) for r in candidates]
    elif top is not None and top.status in (Status.FIT, Status.POSSIBLE_AFTER, Status.CLARIFY):
        rv.primary = ground_view(candidates[0], kb, profile, as_of)
        rv.alternatives = [ground_view(r, kb, profile, as_of) for r in candidates[1:]]
    elif candidates:  # только «нужен индивидуальный разбор»
        rv.kind = "individual"
        rv.alternatives = [ground_view(r, kb, profile, as_of) for r in candidates]
    else:
        rv.kind = "none"
        rv.message = ("По вашим ответам ни одно из проверенных оснований сейчас не подходит. Это не значит, что "
                      "вариантов нет совсем: часть ситуаций в боте не покрыта. Рекомендуем индивидуальный разбор.")
    rv.not_suitable = [ground_view(r, kb, profile, as_of) for r in main
                       if r.status is Status.NOT_ELIGIBLE and r.applicable is not Tri.F]

    for p in profile.members():
        mv = MemberView(p.id, p.label, p.relation)
        mc = profile.value(p.id, "citizenship_group")
        mc = set(mc) if isinstance(mc, list) else set()
        if "bg" in mc:
            mv.note = "гражданин(ка) Болгарии — разрешение на проживание не нужно."
            rv.family.append(mv)
            continue
        if "eu" in mc and "eu" not in regimes:
            mv.note = ("гражданин(ка) ЕС — отдельный порядок регистрации, проверенных правил для него в боте нет; "
                       "нужен индивидуальный разбор.")
            rv.family.append(mv)
            continue
        rs = [r for r in ev.for_subject(p.id) if r.status is not Status.NOT_ELIGIBLE or
              kb.ground(r.ground_id).subject == "family_member"]
        rs.sort(key=lambda r: rank_key(r, kb.ground(r.ground_id), profile))
        # собственное основание члена семьи (например, у супруга своя удалённая работа) показываем,
        # только если по нему уже «подходит» или «возможно после условия» — иначе это шум
        mv.grounds = [ground_view(r, kb, profile, as_of) for r in rs
                      if kb.ground(r.ground_id).subject == "family_member"
                      or r.status in (Status.FIT, Status.POSSIBLE_AFTER)][:3]
        rv.family.append(mv)
    return rv


def view_to_dict(rv: ResultView) -> dict:
    """Сериализация для хранения результата (исторического) в БД."""
    from dataclasses import asdict

    d = asdict(rv)
    d["as_of"] = rv.as_of.isoformat()
    return d
