"""Регрессионные тесты по замечаниям ревью: каждый воспроизводит сценарий сбоя."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from bginfo.ai.interpret import (
    Interpretation,
    Interpreter,
    Recognized,
    build_questions,
    map_answers,
    parse_simple,
)
from bginfo.db.conn import open_db
from bginfo.engine.evaluate import Status, evaluate_all
from bginfo.engine.facts import CASE, MAIN, FactState, Profile
from bginfo.kb import releases as R
from tests.kbfixture import mini_bundle
from tests.scenario_kit import TODAY, FakeInterp, Run, demo_snapshot

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
KB = demo_snapshot().bundle


# --------------------------------------------------------------------------- 2: битые ответы JEV


def _qid(index, key, option=None):
    return next(i for i, q in index.items() if q.fact.key == key and (option is None or q.option.value == option))


def test_map_answers_skips_non_numeric_probabilities_per_question():
    _, index = build_questions(KB, None)
    bad = _qid(index, "income_sources", "pension")
    good = _qid(index, "ties", "bg_origin")
    answers = {
        "intent": {"choice": "new_info", "probabilities": {"new_info": "oops"}},
        bad: {"choice": "self", "probabilities": {"self": "много"}},
        good: {"choice": "yes", "probabilities": {"yes": 0.9}},
    }
    it = map_answers(answers, index, 0.3, 0.75)
    assert [r.key for r in it.items] == ["ties"]


class _Ledger:
    async def acheck(self, *a):
        pass

    async def arecord(self, *a, **k):
        pass


class _Client:
    def __init__(self, answers):
        self.answers = answers

    async def systemone(self, model, state, questions):
        return self.answers, None


@pytest.mark.parametrize("answers", ["garbage", [1, 2]])
async def test_interpret_wraps_invalid_provider_answer(answers):
    it = Interpreter(_Client(answers), _Ledger(), jev_model="m", llm_model="m")
    res = await it.interpret("текст", KB, None, 1)
    assert res.ok is False and res.error == "invalid"


# --------------------------------------------------------------------------- 3: числа


def test_parse_simple_negative_and_limits():
    money = KB.fact("pension_amount_eur")
    assert parse_simple("-700", money) == ("bad", "Число не может быть отрицательным.")
    assert parse_simple("−700 евро", money)[0] == "bad"
    assert parse_simple("700", money) == ("ok", 700.0)
    kids = KB.fact("children_count")
    assert parse_simple("-2", kids) == ("bad", "Число не может быть отрицательным.")
    assert parse_simple("0", kids) == ("ok", 0)
    assert parse_simple("10", kids) == ("ok", 10)
    kind, msg = parse_simple("11", kids)
    assert kind == "bad" and "индивидуальный разбор" in msg and "до 10" in msg


def _family_session(n):
    r = Run()
    p = r.s.profile
    p.confirm_value(CASE, "who_moves", "self_family", "t", None)
    p.confirm_value(CASE, "family_moving", ["children"], "t", None)
    p.confirm_value(CASE, "children_count", n, "t", None)
    r.d._sync_members(r.s, KB)
    return [x for x in p.members() if x.relation == "child"]


def test_children_created_up_to_ten_consistent_with_parser():
    assert len(_family_session(10)) == 10
    assert len(_family_session(11)) == 0


# --------------------------------------------------------------------------- 4: unsupported с неизвестным исходом


def test_unknown_unsupported_condition_blocks_fit():
    p = Profile()
    for pid, k, v in [(CASE, "who_moves", "already_in_bg"), (CASE, "has_housing", True),
                      (MAIN, "citizenship_group", ["third"]), (MAIN, "income_sources", ["pension"]),
                      (MAIN, "pension_amount_eur", 900), (MAIN, "pension_doc", True)]:
        p.confirm_value(pid, k, v, "t", None)
    p.mark_unknown(MAIN, "status_in_bg")
    r = evaluate_all(KB, p, TODAY).get("pensioner")
    assert r.status is Status.CLARIFY
    assert any(x.startswith("Неизвестно, относится ли к вам:") for x in r.reasons)
    assert (MAIN, "status_in_bg") in r.missing


# --------------------------------------------------------------------------- 5: факты


def test_mark_unknown_invalidates_dependents_of_confirmed_value():
    p = Profile()
    p.confirm_value(MAIN, "income_sources", ["pension"], "t", None)
    p.confirm_value(MAIN, "pension_amount_eur", 900, "t", None)
    removed = p.mark_unknown(MAIN, "income_sources", KB)
    assert p.get(MAIN, "pension_amount_eur") is None and removed


def test_confirm_after_unknown_invalidates_dependents():
    p = Profile()
    p.mark_unknown(MAIN, "income_sources")
    p.confirm_value(MAIN, "pension_amount_eur", 900, "t", None)
    p.confirm_value(MAIN, "income_sources", ["remote_employee"], "t", KB)
    assert p.get(MAIN, "pension_amount_eur") is None


def test_recognize_on_conflict_keeps_confirmed_base():
    p = Profile()
    p.confirm_value(CASE, "who_moves", "self", "t", None)
    assert p.recognize(CASE, "who_moves", "self_family", "jev") is FactState.CONFLICT
    assert p.recognize(CASE, "who_moves", "other_person", "jev") is FactState.CONFLICT
    f = p.get(CASE, "who_moves")
    assert f.prev_value == "self" and f.value == "other_person"
    # возврат к подтверждённому значению снимает конфликт
    assert p.recognize(CASE, "who_moves", "self", "jev") is FactState.CONFIRMED
    f = p.get(CASE, "who_moves")
    assert f.state is FactState.CONFIRMED and f.value == "self"


# --------------------------------------------------------------------------- 6: диалог

PENS = {
    "who_moves": "self", "citizenship_group": ["third"], "residence": "home", "income_sources": ["pension"],
    "pension_amount_eur": 900, "pension_doc": True, "has_housing": True, "ties": ["none"],
}


async def test_why_alts_consult_refresh_on_new_release():
    for action in ("why", "alts", "consult"):
        r = Run(snap=demo_snapshot(1))
        await r.result(PENS, strict=False)
        r.set_snapshot(demo_snapshot(2))
        await r.press(action)
        assert r.s.ui["last_view"]["release_id"] == 2, action
        assert r.out.result_record is not None, action


async def test_consult_preview_refreshes_on_new_day():
    r = Run(snap=demo_snapshot(1))
    await r.result(PENS, strict=False)
    r.d.today = lambda: TODAY + timedelta(days=1)
    await r.press("consult")
    assert r.s.ui["last_view"]["as_of"] == (TODAY + timedelta(days=1)).isoformat()
    assert r.s.state == "consult_preview"


def test_sync_members_clears_family_when_who_moves_is_self():
    r = Run()
    p = r.s.profile
    p.confirm_value(CASE, "who_moves", "self_family", "t", None)
    p.confirm_value(CASE, "family_moving", ["spouse"], "t", None)
    r.d._sync_members(r.s, KB)
    assert [x.id for x in p.members()] == ["s1"]
    p.confirm_value("s1", "citizenship_group", ["third"], "t", None)
    p.confirm_value(CASE, "who_moves", "self", "t", KB)
    r.d._sync_members(r.s, KB)
    assert p.members() == [] and p.get("s1", "citizenship_group") is None
    assert any(h["event"] == "participant_removed" for h in p.history)


def test_family_question_asked_for_already_in_bg_too():
    q = next(q for q in KB.questions if q.id == "q_family")
    assert q.ask_if.op == "in" and set(q.ask_if.values) == {"self_family", "already_in_bg"}


async def test_deferred_items_are_merged_not_replaced():
    r = Run(interp=FakeInterp(Interpretation(ok=True, items=[Recognized("spouse", "income_sources", ["pension"], 0.9)])))
    await r.start()
    await r.text("моя жена на пенсии")
    r.interp.result = Interpretation(ok=True, items=[Recognized("spouse", "citizenship_group", ["third"], 0.9)])
    await r.text("и она не из ЕС")
    keys = {(d["role"], d["key"]) for d in r.s.ui["deferred"]}
    assert keys == {("spouse", "income_sources"), ("spouse", "citizenship_group")}


async def test_delete_requires_current_token():
    r = Run()
    await r.start()
    await r.command("delete")
    token = r.out.replies[-1].buttons[0][0].arg
    assert token and r.s.ui["delete_token"] == token
    await r.press("del_yes", "stale")
    assert r.out.alert == "Кнопка устарела." and not r.out.delete_user
    await r.press("del_no")
    assert "delete_token" not in r.s.ui
    await r.press("del_yes", token)
    assert r.out.alert == "Кнопка устарела." and not r.out.delete_user
    await r.command("delete")
    token2 = r.out.replies[-1].buttons[0][0].arg
    assert token2 != token
    await r.press("del_yes", token2)
    assert r.out.delete_user


async def test_text_waits_for_ai_notice_before_external_analysis():
    r = Run(interp=FakeInterp(Interpretation(ok=True, items=[Recognized("self", "income_sources", ["pension"], 0.9)])))
    r.u.ai_notice_shown = False
    await r.start()
    await r.press("free")
    await r.text("я пенсионер из России")
    assert r.interp.calls == 0
    assert r.s.ui["pending_text"] == "я пенсионер из России" and r.out.ai_notice_shown
    actions = [b.action for row in r.out.replies[-1].buttons for b in row]
    assert actions == ["parse_pending", "asks"]
    await r.press("parse_pending")
    assert r.interp.calls == 1 and "pending_text" not in r.s.ui


async def test_asks_drops_pending_text_and_stale_parse_pending_alerts():
    r = Run()
    r.u.ai_notice_shown = False
    await r.start()
    await r.press("free")
    await r.text("что-то про меня")
    await r.press("asks")
    assert "pending_text" not in r.s.ui and r.interp.calls == 0
    await r.press("parse_pending")
    assert r.out.alert == "Кнопка устарела." and r.interp.calls == 0


async def test_resume_view_reshows_current_step():
    r = Run()
    await r.start()
    await r.press("go")
    before = dict(r.s.profile.facts)
    await r.press("resume_view")
    assert r.out.replies and r.s.profile.facts == before


# --------------------------------------------------------------------------- 7: ревизии черновика


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "t.db")
    R._cache.clear()
    yield c
    c.close()


def test_stale_form_revision_rejected_after_replace_draft(conn):
    R.replace_draft(conn, mini_bundle(), "admin", "one")
    item, rev = R.draft_item(conn, "ground", "pensioner")
    R.replace_draft(conn, mini_bundle(), "admin", "two")
    _, rev2 = R.draft_item(conn, "ground", "pensioner")
    assert rev2 > rev  # ревизия не переиспользуется
    with pytest.raises(R.ConcurrentEdit):
        R.save_draft_item(conn, "ground", item, "admin", rev)
    assert R.save_draft_item(conn, "ground", item, "admin", rev2) > rev2


def test_new_item_revision_is_monotonic(conn):
    R.replace_draft(conn, mini_bundle(), "admin", "one")
    item, rev = R.draft_item(conn, "ground", "nomad")
    R.delete_draft_item(conn, "ground", "nomad", "admin", rev)
    R.save_draft_item(conn, "ground", item, "admin", None)
    _, rev_new = R.draft_item(conn, "ground", "nomad")
    assert rev_new > rev


def test_general_flag_after_partial_disputes_all_requirements(conn):
    R.replace_draft(conn, mini_bundle(), "admin", "x")
    R.publish_draft(conn, "admin", now=T0)
    R.flag_needs_review(conn, "pensioner", "частично", "admin", ["housing"], now=T0 + timedelta(minutes=1))
    R.flag_needs_review(conn, "pensioner", "в целом", "admin", None, now=T0 + timedelta(minutes=2))
    snap = R.active_snapshot(conn, T0 + timedelta(minutes=3))
    g = snap.bundle.ground("pensioner")
    assert g.requirements and all(r.disputed for r in g.requirements)
    p = Profile()
    p.confirm_value(MAIN, "citizenship_group", "third", "t", None)
    p.confirm_value(MAIN, "income_sources", ["pension"], "t", None)
    p.confirm_value(MAIN, "pension_amount_eur", 900, "t", None)
    p.confirm_value(CASE, "has_housing", True, "t", None)
    res = evaluate_all(snap.bundle, p, TODAY).get("pensioner")
    assert res.status is Status.INDIVIDUAL


# --------------------------------------------------------------------------- AI-ledger: сбой БД учёта


class _BrokenLedger:
    async def acheck(self, *a):
        raise sqlite3.OperationalError("database is locked")

    async def arecord(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


async def test_interpret_ledger_db_error_means_ai_unavailable():
    client = _Client({})
    it = Interpreter(client, _BrokenLedger(), jev_model="m", llm_model="m")
    res = await it.interpret("текст", KB, None, 1)
    assert res.ok is False and res.error == "unavailable"
