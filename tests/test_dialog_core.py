"""Сквозные проверки контроллера диалога на тестовой базе (AI — заглушки, это НЕ живая интеграция)."""

from datetime import date

import pytest

from bginfo.ai.explain import apply_template
from bginfo.ai.interpret import Interpretation, Recognized
from bginfo.engine.dialog import Dialog, Event, SessionData, UserCtx
from bginfo.engine.facts import MAIN, Profile
from bginfo.kb.releases import Snapshot
from tests.kbfixture import mini_bundle


class FakeInterp:
    def __init__(self, result=None):
        self.result = result or Interpretation(ok=False, error="disabled")
        self.calls = 0

    async def interpret(self, text, kb, current, uid):
        self.calls += 1
        return self.result


class FakeExplainer:
    async def explain(self, rv, lines, titles, uid):
        return apply_template(rv)


def make(interp=None, snap=None):
    d = Dialog(interp or FakeInterp(), FakeExplainer(), today=lambda: date(2026, 10, 9), consult_enabled=True)
    s = SessionData(1, 0, "start", Profile(), {}, None)
    snap = snap or Snapshot(7, "2026-10-01T00:00:00Z", mini_bundle())
    return d, s, snap, UserCtx(42, "tester", True)


async def press(d, s, snap, u, action, arg=""):
    return await d.handle(s, Event("button", action=action, arg=arg), snap, u)


async def test_buttons_only_to_result():
    d, s, snap, u = make()
    out = await d.handle(s, Event("start"), snap, u)
    assert "Найдём возможные основания" in out.replies[0].html
    out = await press(d, s, snap, u, "go")
    assert "Гражданство" in out.replies[-1].html
    await press(d, s, snap, u, "ans", "third")
    await press(d, s, snap, u, "tog", "pension")
    out = await press(d, s, snap, u, "done")
    assert s.profile.value(MAIN, "income_sources") == ["pension"]
    # дальше — размер пенсии текстом (код разбирает число без AI)
    for _ in range(6):
        q = d._current_q(s, snap.bundle)
        if s.state == "review":
            break
        if q.fact == "pension_amount_eur":
            out = await d.handle(s, Event("text", text="900 евро"), snap, u)
        elif q.fact == "has_housing":
            out = await press(d, s, snap, u, "ans", "yes")
        else:
            out = await press(d, s, snap, u, "unk")
    assert s.state == "review"
    out = await press(d, s, snap, u, "review_ok")
    assert s.state == "result"
    html = out.replies[-1].html
    assert "Пенсионер" in html and "предварительно подходит" in html
    assert "https://bginfo.eu/test-pens/" in html
    assert out.result_record["release_id"] == 7
    why = await press(d, s, snap, u, "why")
    assert "Пенсия не ниже МРЗ" in why.replies[0].html


async def test_free_text_recognized_then_confirmed():
    interp = FakeInterp(Interpretation(ok=True, intent="new_info", intent_conf=0.9,
                                       items=[Recognized("self", "income_sources", ["pension"], 0.9),
                                              Recognized("case", "has_housing", True, 0.8)]))
    d, s, snap, u = make(interp)
    await press(d, s, snap, u, "free")
    out = await d.handle(s, Event("text", text="Я пенсионер, квартира есть"), snap, u)
    # сначала подтверждение простых фактов
    assert "Правильно ли я понял" in out.replies[-1].html
    out = await press(d, s, snap, u, "pend_ok")
    assert s.profile.value("case", "has_housing") is True


async def test_jev_down_falls_back_to_buttons_without_losing_data():
    d, s, snap, u = make(FakeInterp(Interpretation(ok=False, error="timeout")))
    await press(d, s, snap, u, "go")
    await press(d, s, snap, u, "ans", "third")
    out = await d.handle(s, Event("text", text="у меня всё сложно"), snap, u)
    assert "продолжим кнопками" in out.replies[0].html
    assert s.profile.value(MAIN, "citizenship_group") == "third"


async def test_unavailable_when_no_published_grounds():
    d, s, _, u = make()
    snap = Snapshot(1, "x", mini_bundle(status="draft"))
    out = await d.handle(s, Event("start"), snap, u)
    assert "готовится" in out.replies[0].html
    out = await d.handle(s, Event("start"), None, u)
    assert "готовится" in out.replies[0].html


async def test_stale_question_button_rejected():
    d, s, snap, u = make()
    out = await press(d, s, snap, u, "ans", "third")
    assert out.alert


async def test_rules_change_before_result_rechecks():
    d, s, snap, u = make()
    await press(d, s, snap, u, "go")
    s.release_id = 6  # анкета начата на старой версии
    out = await press(d, s, snap, u, "ans", "eu")
    assert any("обновились" in r.html for r in out.replies) or s.state == "review"


@pytest.mark.parametrize("text,ok", [("850", 850.0), ("1 200,50 €", 1200.5), ("2к", 2000.0)])
def test_money_parse(text, ok):
    from bginfo.ai.interpret import parse_simple
    fd = mini_bundle().fact("pension_amount_eur")
    assert parse_simple(text, fd) == ("ok", ok)
