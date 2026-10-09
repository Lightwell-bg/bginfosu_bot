"""Общий инструментарий сквозных сценариев: контроллер Dialog на демо-бандле, AI — заглушки (НЕ живой AI)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from bginfo.ai.explain import apply_template
from bginfo.ai.interpret import Interpretation
from bginfo.engine.dialog import Dialog, Event, Out, SessionData, UserCtx
from bginfo.engine.facts import CASE, MAIN, Profile
from bginfo.kb.releases import Snapshot
from bginfo.seed.loader import load_seed_bundle

TODAY = date(2026, 10, 9)
UNKNOWN = "__unknown__"  # в словаре фактов: нажать «Не знаю»


class FakeInterp:
    """Возвращает заранее заданную интерпретацию (или «сервис выключен»)."""

    def __init__(self, result: Interpretation | None = None):
        self.result = result or Interpretation(ok=False, error="disabled")
        self.calls = 0

    async def interpret(self, text, kb, current, uid):
        self.calls += 1
        return self.result


class FakeExplainer:
    async def explain(self, rv, lines, titles, uid):
        return apply_template(rv)


def demo_snapshot(release_id: int = 1, mutate=None) -> Snapshot:
    """Демо-бандл как снимок; ``mutate(bundle_dict)`` правит копию (для сценариев смены правил)."""
    bundle = load_seed_bundle(demo=True, today=TODAY)
    if mutate is not None:
        from bginfo.kb import model as m

        data = bundle.model_dump(mode="json")
        mutate(data)
        bundle = m.Bundle.model_validate(data)
    return Snapshot(release_id, "2026-01-01T00:00:00Z", bundle)


@dataclass
class Run:
    """Один диалог: сессия + контроллер. Помощники нажимают кнопки и отвечают по словарю фактов."""

    interp: FakeInterp = field(default_factory=FakeInterp)
    snap: Snapshot = field(default_factory=demo_snapshot)
    explainer: Any = field(default_factory=FakeExplainer)
    asked: list[tuple[str, str]] = field(default_factory=list)  # (участник, факт) в порядке показа вопросов
    outs: list[Out] = field(default_factory=list)

    def __post_init__(self):
        self.d = Dialog(self.interp, self.explainer, today=lambda: TODAY, consult_enabled=True)
        self.s = SessionData(1, 0, "start", Profile(), {}, None)
        self.u = UserCtx(42, "tester", True)
        self.out: Out | None = None

    # ------------------------------------------------------------------ events
    async def _do(self, ev: Event) -> Out:
        self.out = await self.d.handle(self.s, ev, self.snap, self.u)
        self.outs.append(self.out)
        q = self.d._current_q(self.s, self.snap.bundle)
        if q is not None and self.out.replies and self.s.state == "asking":
            pair = (self.s.ui.get("pid", MAIN), q.fact)
            if not self.asked or self.asked[-1] != pair or self.out.replies[-1].edit is False:
                self.asked.append(pair)
        return self.out

    async def start(self) -> Out:
        return await self._do(Event("start"))

    async def press(self, action: str, arg: str = "") -> Out:
        return await self._do(Event("button", action=action, arg=arg))

    async def text(self, text: str) -> Out:
        return await self._do(Event("text", text=text))

    async def command(self, action: str) -> Out:
        return await self._do(Event("command", action=action))

    def set_snapshot(self, snap: Snapshot) -> None:
        self.snap = snap

    # ------------------------------------------------------------------ helpers
    @property
    def kb(self):
        return self.snap.bundle

    def html(self) -> str:
        return "\n".join(r.html for r in self.out.replies)

    def value(self, key: str, pid: str | None = None) -> Any:
        fd = self.kb.fact(key)
        pid = pid or (CASE if fd.scope == "case" else MAIN)
        return self.s.profile.value(pid, key)

    async def _answer(self, answers: dict, strict: bool) -> None:
        q = self.d._current_q(self.s, self.kb)
        pid = self.s.ui.get("pid", MAIN)
        fd = self.kb.fact(q.fact)
        if (pid, q.fact) in answers:
            val = answers[(pid, q.fact)]
        elif q.fact in answers:
            val = answers[q.fact]
        elif strict:
            raise AssertionError(f"неожиданный вопрос: {pid}/{q.fact} ({q.text!r})")
        else:
            val = UNKNOWN
        if val == UNKNOWN:
            await self.press("unk")
        elif fd.type == "multi":
            want = set(val)
            for v in sorted(set(self.s.ui.get("sel", [])) - want):
                await self.press("tog", v)
            for v in sorted(want - set(self.s.ui.get("sel", []))):
                await self.press("tog", v)
            await self.press("done")
        elif fd.type == "bool":
            await self.press("ans", "yes" if val else "no")
        elif fd.type == "enum":
            await self.press("ans", val)
        else:  # money / int — числом текстом
            await self.text(str(val))

    async def fill(self, answers: dict, *, strict: bool = True, conflict: str = "new", max_steps: int = 80,
                   until: tuple[str, str] | None = None) -> Out:
        """Пройти анкету до экрана проверки. Ключ словаря: ``факт`` или ``(участник, факт)``.

        Значения: bool, строка (enum), список (multi), число (money/int) или ``UNKNOWN``.
        ``strict`` — вопрос без ответа в словаре считается ошибкой теста (иначе «Не знаю»).
        ``until`` — остановиться, когда показан вопрос (участник, факт), не отвечая на него.
        """
        if self.s.state == "start":
            await self.start()
            await self.press("go")
        for _ in range(max_steps):
            if self.s.state == "review":
                return self.out
            q = self.d._current_q(self.s, self.kb)
            html = self.html()
            if self.s.ui.get("qid") is None and "Правильно ли я понял" in html:
                await self.press("pend_ok")
            elif self.s.ui.get("qid") is None and "Что верно" in html:
                await self.press("conf", conflict)
            elif q is not None:
                if until is not None and (self.s.ui.get("pid", MAIN), q.fact) == until:
                    return self.out
                await self._answer(answers, strict)
            else:
                raise AssertionError(f"анкета застряла в состоянии {self.s.state!r}: {html[:200]}")
        raise AssertionError("анкета не завершилась за отведённое число шагов")

    async def result(self, answers: dict, **kw) -> Out:
        await self.fill(answers, **kw)
        return await self.press("review_ok")

    def view(self):
        from bginfo.engine.present import view_from_dict

        return view_from_dict(self.s.ui["last_view"])

    def statuses(self) -> dict[tuple[str, str], str]:
        """{(участник, основание): статус} по записанному результату."""
        rec = self.out.result_record
        return {(e["subject"], e["ground"]): e["status"] for e in rec["evaluation"]}
