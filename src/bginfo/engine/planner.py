"""Выбор следующего шага анкеты.

Порядок:
1. Противоречия (CONFLICT) — сначала разрешить.
2. Распознанные из текста сведения — подтвердить (multi-факты подтверждаются
   через сам вопрос с предвыбранными пунктами: это и есть контрольный вопрос,
   который страхует от пропуска направления при разборе текста).
3. Базовые вопросы (core) по приоритету.
4. Вопросы, закрывающие недостающие факты нерешённых оснований: сначала тот,
   который нужен большему числу оснований.
5. Вопросов не осталось — показать анкету для проверки.

Вопрос, на который ответили «Не знаю», повторно не задаётся.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from bginfo.engine.evaluate import Evaluation, Tri, _Ctx, eval_cond
from bginfo.engine.facts import CASE, MAIN, Fact, FactState, Profile
from bginfo.kb import model as m

StepKind = Literal["conflict", "confirm", "question", "review"]


@dataclass
class Step:
    kind: StepKind
    question: m.Question | None = None
    participant: str | None = None
    pending: list[Fact] = field(default_factory=list)


def _targets(q: m.Question, kb: m.Bundle, profile: Profile) -> list[str]:
    fd = kb.fact(q.fact)
    if fd is None:
        return []
    if fd.scope == "case":
        return [CASE]
    out = []
    if "main" in q.ask_for:
        out.append(MAIN)
    if "member" in q.ask_for:
        out += [p.id for p in profile.members() if p.relation in q.member_relations]
    return out


def _open(profile: Profile, pid: str, key: str) -> bool:
    """Факт ещё не выяснен: нет записи (RECOGNIZED обрабатывается отдельным шагом)."""
    return profile.get(pid, key) is None


def _ask_if(q: m.Question, kb: m.Bundle, profile: Profile, pid: str, as_of: date) -> Tri:
    if q.ask_if is None:
        return Tri.T
    subject = MAIN if pid == CASE else pid
    t, _ = eval_cond(q.ask_if, _Ctx(kb, profile, subject, as_of))
    return t


def question_for(kb: m.Bundle, key: str) -> m.Question | None:
    qs = [q for q in kb.questions if q.fact == key]
    return min(qs, key=lambda q: q.priority) if qs else None


def next_step(kb: m.Bundle, profile: Profile, ev: Evaluation, as_of: date) -> Step:
    pend = profile.pending()
    conflicts = [f for f in pend if f.state is FactState.CONFLICT]
    if conflicts:
        return Step("conflict", pending=conflicts[:1])

    # Распознанные multi-факты подтверждаются своим вопросом с предвыбором.
    simple, multi = [], []
    for f in pend:
        fd = kb.fact(f.key)
        (multi if fd is not None and fd.type == "multi" else simple).append(f)
    if simple:
        return Step("confirm", pending=simple)
    for f in multi:
        q = question_for(kb, f.key)
        if q is not None:
            return Step("question", question=q, participant=f.participant)

    # Базовые вопросы.
    for q in sorted((q for q in kb.questions if q.core), key=lambda q: q.priority):
        for pid in _targets(q, kb, profile):
            if _open(profile, pid, q.fact) and _ask_if(q, kb, profile, pid, as_of) is Tri.T:
                return Step("question", question=q, participant=pid)

    # Вопросы по недостающим фактам нерешённых оснований. Если вопрос пока
    # скрыт условием показа с неизвестным исходом, вес переходит на факты
    # этого условия (сначала выясняем их).
    weight: Counter = Counter()
    for fk, w in Counter(ev.missing()).items():
        weight[fk] += w
    for _ in range(3):  # ограниченная глубина распространения веса
        extra: Counter = Counter()
        for (pid, key), w in weight.items():
            q = question_for(kb, key)
            if q is None or q.ask_if is None or not _open(profile, pid, key):
                continue
            subject = MAIN if pid == CASE else pid
            t, ms = eval_cond(q.ask_if, _Ctx(kb, profile, subject, as_of))
            if t is Tri.U:
                for fk in ms:
                    if fk not in weight:
                        extra[fk] += w
        if not extra:
            break
        weight.update(extra)

    best: tuple[int, int, str, m.Question] | None = None
    for (pid, key), w in weight.items():
        if not _open(profile, pid, key):
            continue
        q = question_for(kb, key)
        if q is None or pid not in _targets(q, kb, profile):
            continue
        if _ask_if(q, kb, profile, pid, as_of) is not Tri.T:
            continue
        cand = (-w, q.priority, pid, q)
        if best is None or cand[:3] < best[:3]:
            best = cand
    if best is not None:
        return Step("question", question=best[3], participant=best[2])

    return Step("review")
