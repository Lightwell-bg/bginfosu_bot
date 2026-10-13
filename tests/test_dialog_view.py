"""Out.view (экран для мини-аппа) и действие ``multi``."""

from datetime import date

from bginfo.ai.explain import apply_template
from bginfo.ai.interpret import Interpretation
from bginfo.engine.dialog import Dialog, Event, Out, SessionData, UserCtx
from bginfo.engine.facts import CASE, MAIN, Profile
from bginfo.engine.planner import Step
from bginfo.kb import model as m
from bginfo.kb.releases import Snapshot
from tests.kbfixture import mini_bundle


class FakeInterp:
    async def interpret(self, text, kb, current, uid):
        return Interpretation(ok=False, error="disabled")


class FakeExplainer:
    async def explain(self, rv, lines, titles, uid):
        return apply_template(rv)


def bundle_with_extra() -> m.Bundle:
    d = mini_bundle().model_dump(mode="json")
    d["facts"] += [
        {"key": "arrival", "scope": "person", "type": "date", "label": "Дата приезда"},
        {"key": "kids", "scope": "case", "type": "int", "label": "Детей"},
    ]
    d["questions"] += [
        {"id": "q_arr", "fact": "arrival", "text": "Когда приехали?", "help": "Месяц и год", "priority": 40},
        {"id": "q_kids", "fact": "kids", "text": "Сколько детей?", "priority": 41, "allow_unknown": False},
    ]
    return m.Bundle.model_validate(d)


def make(webapp_url=None):
    d = Dialog(FakeInterp(), FakeExplainer(), today=lambda: date(2026, 10, 9), consult_enabled=True,
               webapp_url=webapp_url)
    s = SessionData(1, 0, "start", Profile(), {}, None)
    return d, s, Snapshot(7, "2026-10-01T00:00:00Z", bundle_with_extra()), UserCtx(42, "tester", True)


def ask(d, s, snap, qid, pid=MAIN) -> dict:
    q = next(x for x in snap.bundle.questions if x.id == qid)
    out = d._ask(s, snap.bundle, Step("question", question=q, participant=pid), Out())
    return out.view["question"]


async def press(d, s, snap, u, action, arg=""):
    return await d.handle(s, Event("button", action=action, arg=arg), snap, u)


def test_view_enum_and_flags():
    d, s, snap, _ = make()
    v = ask(d, s, snap, "q_cit")
    assert v["qid"] == "q_cit" and v["type"] == "enum" and v["who"] is None
    assert [o["value"] for o in v["options"]] == ["bg", "eu", "third"]
    assert v["allow_unknown"] is True and v["can_back"] is False and v["can_fix"] is False
    assert v["recognized"] is False and v["answered"] == 0 and v["selected"] == []
    assert v["text_html"] == "Гражданство?"


def test_view_bool_money_int_date():
    d, s, snap, _ = make()
    b = ask(d, s, snap, "q_house")
    assert b["type"] == "bool" and b["options"] == [{"value": "yes", "label": "Да"}, {"value": "no", "label": "Нет"}]
    money = ask(d, s, snap, "q_pens")
    assert money["type"] == "money" and money["options"] == []
    n = ask(d, s, snap, "q_kids")
    assert n["type"] == "int" and n["allow_unknown"] is False
    dt = ask(d, s, snap, "q_arr")
    assert dt["type"] == "date" and dt["help_html"] == "Месяц и год"


async def test_view_multi_selected_and_answered():
    d, s, snap, u = make()
    await press(d, s, snap, u, "go")
    out = await press(d, s, snap, u, "ans", "third")
    assert out.view["question"]["type"] == "multi"
    v = ask(d, s, snap, "q_inc")
    assert v["type"] == "multi" and v["selected"] == [] and v["answered"] == 1
    assert v["can_fix"] is True and v["can_back"] is True
    s.ui["sel"] = ["pension"]
    assert ask(d, s, snap, "q_inc")["selected"] == ["pension"]


async def _to_review(d, s, snap, u):
    await press(d, s, snap, u, "go")
    for _ in range(15):
        if s.state == "review":
            return
        q = d._current_q(s, snap.bundle)
        if q.fact == "citizenship_group":
            await press(d, s, snap, u, "ans", "third")
        elif q.fact == "income_sources":
            await press(d, s, snap, u, "multi", "pension")
        elif q.fact == "pension_amount_eur":
            await d.handle(s, Event("text", text="900"), snap, u)
        elif q.fact == "has_housing":
            await press(d, s, snap, u, "ans", "yes")
        elif q.fact == "kids":
            await d.handle(s, Event("text", text="1"), snap, u)
        elif q.fact == "arrival":
            await d.handle(s, Event("text", text="01.2024"), snap, u)
        else:
            await press(d, s, snap, u, "unk")


async def test_view_review_and_edit_index():
    d, s, snap, u = make()
    await _to_review(d, s, snap, u)
    assert s.state == "review"
    out = await press(d, s, snap, u, "resume_view")
    review = out.view["review"]
    assert review["lines"] and all({"index", "who", "label", "value"} <= set(x) for x in review["lines"])
    idx = [x["index"] for x in review["lines"] if x["index"] is not None]
    assert idx == list(range(len(idx))) and len(s.ui["edit_list"]) == len(idx)
    first = next(x for x in review["lines"] if x["index"] is not None)
    pid, key = s.ui["edit_list"][first["index"]]
    out = await press(d, s, snap, u, "edit", str(first["index"]))
    assert out.view["question"]["qid"] == next(q.id for q in snap.bundle.questions if q.fact == key)
    assert pid in (MAIN, CASE)


async def test_view_result():
    d, s, snap, u = make()
    await _to_review(d, s, snap, u)
    out = await press(d, s, snap, u, "review_ok")
    res = out.view["result"]
    assert res["html"] == out.replies[-1].html and "Пенсионер" in res["html"]
    assert res["view"]["kind"] == "ok" and res["view"]["primary"]["ground_id"] == "pensioner"


async def test_view_none_for_other_steps():
    d, s, snap, u = make()
    assert set((await d.handle(s, Event("start"), snap, u)).view) == {"welcome"}
    assert (await press(d, s, snap, u, "free")).view is None
    assert (await d.handle(s, Event("command", action="delete"), snap, u)).view is None


# ------------------------------------------------------------------ multi


async def _at_income(d, s, snap, u):
    await press(d, s, snap, u, "go")
    await press(d, s, snap, u, "ans", "third")
    assert d._current_q(s, snap.bundle).fact == "income_sources"


async def test_multi_valid_commits_sorted():
    d, s, snap, u = make()
    await _at_income(d, s, snap, u)
    out = await press(d, s, snap, u, "multi", "remote_foreign,pension")
    assert out.alert is None
    assert s.profile.value(MAIN, "income_sources") == ["pension", "remote_foreign"]


async def test_multi_none_alone_ok_and_mixed_rejected():
    d, s, snap, u = make()
    await _at_income(d, s, snap, u)
    out = await press(d, s, snap, u, "multi", "none,pension")
    assert out.alert and s.profile.get(MAIN, "income_sources") is None
    out = await press(d, s, snap, u, "multi", "none")
    assert out.alert is None and s.profile.value(MAIN, "income_sources") == ["none"]


async def test_multi_foreign_empty_and_wrong_type():
    d, s, snap, u = make()
    await _at_income(d, s, snap, u)
    for arg in ("", " , ", "pension,bogus", "bogus"):
        out = await press(d, s, snap, u, "multi", arg)
        assert out.alert and s.profile.get(MAIN, "income_sources") is None
    d2, s2, snap2, u2 = make()
    await press(d2, s2, snap2, u2, "go")  # текущий вопрос — enum, не multi
    out = await press(d2, s2, snap2, u2, "multi", "bg")
    assert out.alert and s2.profile.get(MAIN, "citizenship_group") is None


async def test_welcome_webapp_button():
    d, s, snap, u = make(webapp_url="https://form.example")
    out = await d.handle(s, Event("start"), snap, u)
    assert "webapp" in [b.action for row in out.replies[0].buttons for b in row]
    d2, s2, snap2, u2 = make()
    out = await d2.handle(s2, Event("start"), snap2, u2)
    assert "webapp" not in [b.action for row in out.replies[0].buttons for b in row]


# ------------------------------------------------------------------ say() сбрасывает view; сопровождающие ответы


def test_say_resets_view():
    out = Out()
    out.say("a")
    out.view = {"question": {}}
    out.say("b")
    assert out.view is None


async def test_bad_number_returns_note_and_question_view():
    d, s, snap, u = make()
    await press(d, s, snap, u, "go")
    await press(d, s, snap, u, "ans", "third")
    await press(d, s, snap, u, "multi", "pension")
    assert d._current_q(s, snap.bundle).fact == "pension_amount_eur"
    out = await d.handle(s, Event("text", text="-5"), snap, u)
    assert len(out.replies) == 2 and "отрицательным" in out.replies[0].html
    assert out.view["question"]["qid"] == "q_pens"
    assert out.view["question"]["text_html"] in out.replies[1].html


async def test_consult_on_stale_result_ends_with_generic_screen():
    d, s, snap, u = make()
    await _to_review(d, s, snap, u)
    await press(d, s, snap, u, "review_ok")
    d.today = lambda: date(2026, 10, 10)  # результат устарел по дате
    out = await press(d, s, snap, u, "consult")
    assert len(out.replies) == 2 and "Заявка на консультацию" in out.replies[-1].html
    assert out.view is None and s.state == "consult_preview"


# ------------------------------------------------------------------ show: перерисовка экрана без изменений


class CountingExplainer(FakeExplainer):
    calls = 0

    async def explain(self, rv, lines, titles, uid):
        type(self).calls += 1
        return await super().explain(rv, lines, titles, uid)


async def test_show_result_is_read_only():
    d, s, snap, u = make()
    d.explainer = CountingExplainer()
    await _to_review(d, s, snap, u)
    await press(d, s, snap, u, "review_ok")
    CountingExplainer.calls = 0
    from bginfo.bot.app import _fingerprint

    fp = _fingerprint(s)
    out = await press(d, s, snap, u, "show")
    assert out.view["result"]["view"]["primary"]["ground_id"] == "pensioner"
    assert out.view["result"]["disclaimer"] and out.view["result"]["disclaimer"] in out.replies[-1].html
    assert out.result_record is None and not out.events and CountingExplainer.calls == 0
    assert s.state == "result" and _fingerprint(s) == fp
    assert "why" in [b.action for row in out.replies[-1].buttons for b in row]


async def test_show_review_asking_and_preview():
    from bginfo.bot.app import _fingerprint

    d, s, snap, u = make()
    await press(d, s, snap, u, "go")
    fp = _fingerprint(s)
    out = await press(d, s, snap, u, "show")
    assert out.view["question"]["qid"] == "q_cit" and _fingerprint(s) == fp
    await _to_review(d, s, snap, u)
    fp = _fingerprint(s)
    out = await press(d, s, snap, u, "show")
    assert out.view["review"]["lines"] and s.state == "review" and _fingerprint(s) == fp
    await press(d, s, snap, u, "review_ok")
    await press(d, s, snap, u, "consult")
    fp = _fingerprint(s)
    out = await press(d, s, snap, u, "show")
    assert s.state == "consult_preview" and _fingerprint(s) == fp
    assert [b.action for b in out.replies[-1].buttons[0]] == ["consult_send", "consult_cancel"]


async def test_show_result_with_stale_view_falls_back_to_resume():
    d, s, snap, u = make()
    await _to_review(d, s, snap, u)
    await press(d, s, snap, u, "review_ok")
    d.today = lambda: date(2026, 10, 10)
    out = await press(d, s, snap, u, "show")
    assert out.result_record is None and s.state in ("asking", "review")
