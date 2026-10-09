"""Профиль анкеты: участники, факты и их состояния.

Состояния факта:
- CONFIRMED  — пользователь подтвердил (кнопкой или явным ответом на вопрос);
- RECOGNIZED — распознано из свободного текста, ждёт подтверждения;
- UNKNOWN    — пользователь ответил «Не знаю»;
- CONFLICT   — новое сведение противоречит подтверждённому, нужно уточнить.
Отсутствие записи — «не выяснено». Явное отрицание — CONFIRMED со значением False.

В проверке условий участвуют только CONFIRMED-факты. Всё остальное для
движка правил неизвестно, поэтому неподтверждённое сведение не может ни
закрыть, ни открыть основание.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from bginfo.kb.model import Bundle

CASE = "case"  # псевдо-участник для фактов уровня анкеты
MAIN = "p0"


class FactState(str, Enum):
    CONFIRMED = "confirmed"
    RECOGNIZED = "recognized"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Any = _Missing()


@dataclass
class Fact:
    participant: str
    key: str
    value: Any
    state: FactState
    source: str  # button | text | jev | llm | derived
    prev_value: Any = None  # для CONFLICT: подтверждённое ранее значение
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


@dataclass
class Participant:
    id: str
    relation: str  # main | spouse | child | parent | other
    label: str


@dataclass
class Profile:
    participants: dict[str, Participant] = field(
        default_factory=lambda: {MAIN: Participant(MAIN, "main", "Вы")}
    )
    facts: dict[str, Fact] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------ access
    @staticmethod
    def _k(pid: str, key: str) -> str:
        return f"{pid}:{key}"

    def get(self, pid: str, key: str) -> Fact | None:
        return self.facts.get(self._k(pid, key))

    def value(self, pid: str, key: str) -> Any:
        """Подтверждённое значение или MISSING."""
        f = self.get(pid, key)
        return f.value if f and f.state is FactState.CONFIRMED else MISSING

    def members(self) -> list[Participant]:
        return [p for p in self.participants.values() if p.relation != "main"]

    def pending(self) -> list[Fact]:
        """Распознанные или противоречивые сведения, требующие реакции пользователя."""
        return [f for f in self.facts.values() if f.state in (FactState.RECOGNIZED, FactState.CONFLICT)]

    # ------------------------------------------------------------------ writes
    def confirm_value(self, pid: str, key: str, value: Any, source: str, kb: Bundle | None) -> list[str]:
        """Записать подтверждённое значение. Возвращает ключи инвалидированных фактов."""
        old = self.get(pid, key)
        self._put(Fact(pid, key, value, FactState.CONFIRMED, source))
        if old is None:
            return []
        # для CONFLICT «действующим» было подтверждённое prev_value, а не конфликтное сведение
        base = old.prev_value if old.state is FactState.CONFLICT else old.value
        if old.state is FactState.CONFIRMED and old.value != value:
            self._log("changed", old)
        if base != value and kb is not None:
            return self.invalidate_dependents(pid, key, kb)
        return []

    def recognize(self, pid: str, key: str, value: Any, source: str) -> FactState:
        """Записать распознанное сведение, не затирая подтверждённые данные.

        Совпадает с подтверждённым — ничего не меняем. Расходится — CONFLICT
        (старое значение сохраняется в prev_value и в силе, пока пользователь не решит).
        """
        old = self.get(pid, key)
        if old is not None and old.state is FactState.CONFIRMED:
            if old.value == value:
                return FactState.CONFIRMED
            self._put(Fact(pid, key, value, FactState.CONFLICT, source, prev_value=old.value))
            return FactState.CONFLICT
        if old is not None and old.state is FactState.CONFLICT:
            # подтверждённая основа сохраняется: повторное сведение не должно её стирать
            if old.value == value:
                return FactState.CONFLICT
            if old.prev_value == value:
                self._put(Fact(pid, key, old.prev_value, FactState.CONFIRMED, "previous"))
                return FactState.CONFIRMED
            self._put(Fact(pid, key, value, FactState.CONFLICT, source, prev_value=old.prev_value))
            return FactState.CONFLICT
        self._put(Fact(pid, key, value, FactState.RECOGNIZED, source))
        return FactState.RECOGNIZED

    def accept_pending(self, pid: str, key: str, kb: Bundle | None) -> list[str]:
        f = self.get(pid, key)
        if f is None or f.state not in (FactState.RECOGNIZED, FactState.CONFLICT):
            return []
        prev = f.prev_value
        self._put(Fact(pid, key, f.value, FactState.CONFIRMED, f.source))
        if f.state is FactState.CONFLICT and kb is not None and prev != f.value:
            self._log("changed", Fact(pid, key, prev, FactState.CONFIRMED, "previous"))
            return self.invalidate_dependents(pid, key, kb)
        return []

    def reject_pending(self, pid: str, key: str) -> None:
        f = self.get(pid, key)
        if f is None:
            return
        if f.state is FactState.CONFLICT:
            self._put(Fact(pid, key, f.prev_value, FactState.CONFIRMED, "previous"))
        elif f.state is FactState.RECOGNIZED:
            self._log("rejected", f)
            del self.facts[self._k(pid, key)]

    def mark_unknown(self, pid: str, key: str, kb: Bundle | None = None) -> list[str]:
        """«Не знаю». Если раньше значение было подтверждено — зависимые факты инвалидируются."""
        old = self.get(pid, key)
        self._put(Fact(pid, key, None, FactState.UNKNOWN, "button"))
        if old is not None and old.state is FactState.CONFIRMED:
            self._log("changed", old)
            if kb is not None:
                return self.invalidate_dependents(pid, key, kb)
        return []

    def forget(self, pid: str, key: str, reason: str) -> None:
        f = self.facts.pop(self._k(pid, key), None)
        if f is not None:
            self._log(reason, f)

    def invalidate_dependents(self, pid: str, key: str, kb: Bundle) -> list[str]:
        """Убрать факты, зависящие (транзитивно) от изменённого ключа.

        Для person-факта затрагиваются факты того же участника; изменение
        case-факта затрагивает зависимых у всех участников.
        """
        dependents = _transitive_dependents(key, kb)
        removed: list[str] = []
        for fk, f in list(self.facts.items()):
            if f.key not in dependents:
                continue
            if pid != CASE and f.participant != pid:
                continue
            self.facts.pop(fk)
            self._log(f"invalidated_by:{key}", f)
            removed.append(fk)
        return removed

    # ------------------------------------------------------------------ participants
    def set_members(self, wanted: Iterable[tuple[str, str, str]]) -> list[str]:
        """Синхронизировать список членов семьи с ответами о составе.

        ``wanted`` — (id, relation, label). Удалённые участники уходят вместе
        с их фактами (в историю). Возвращает id удалённых.
        """
        wanted = list(wanted)
        keep = {MAIN} | {w[0] for w in wanted}
        removed = [pid for pid in self.participants if pid not in keep]
        for pid in removed:
            self.participants.pop(pid)
            for fk, f in list(self.facts.items()):
                if f.participant == pid:
                    self.facts.pop(fk)
                    self._log("participant_removed", f)
        for pid, rel, label in wanted:
            cur = self.participants.get(pid)
            if cur is None or cur.relation != rel:
                if cur is not None:  # смена роли — факты участника недостоверны
                    for fk, f in list(self.facts.items()):
                        if f.participant == pid:
                            self.facts.pop(fk)
                            self._log("participant_role_changed", f)
                self.participants[pid] = Participant(pid, rel, label)
            else:
                cur.label = label
        return removed

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        return {
            "participants": [asdict(p) for p in self.participants.values()],
            "facts": [{**asdict(f), "state": f.state.value} for f in self.facts.values()],
            "history": self.history[-200:],
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> Profile:
        p = cls()
        if not d:
            return p
        p.participants = {x["id"]: Participant(**x) for x in d.get("participants", [])} or p.participants
        for x in d.get("facts", []):
            f = Fact(**{**x, "state": FactState(x["state"])})
            p.facts[cls._k(f.participant, f.key)] = f
        p.history = list(d.get("history", []))
        return p

    # ------------------------------------------------------------------ internals
    def _put(self, f: Fact) -> None:
        self.facts[self._k(f.participant, f.key)] = f

    def _log(self, event: str, f: Fact) -> None:
        self.history.append(
            {"event": event, "participant": f.participant, "key": f.key, "value": f.value,
             "state": f.state.value, "source": f.source, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        )


def _transitive_dependents(key: str, kb: Bundle) -> set[str]:
    rev: dict[str, set[str]] = {}
    for fd in kb.facts:
        for dep in fd.depends_on:
            rev.setdefault(dep, set()).add(fd.key)
    out: set[str] = set()
    stack = [key]
    while stack:
        k = stack.pop()
        for d in rev.get(k, ()):
            if d not in out:
                out.add(d)
                stack.append(d)
    return out
