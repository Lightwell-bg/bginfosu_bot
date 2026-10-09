"""Unit-тесты map_answers и apply_to_profile (чистые функции, без сети; JEV-ответы собраны вручную)."""

from __future__ import annotations

from bginfo.ai.interpret import (
    Interpretation,
    Recognized,
    apply_to_profile,
    build_questions,
    map_answers,
)
from bginfo.engine.dialog import Dialog, SessionData
from bginfo.engine.facts import CASE, MAIN, MISSING, FactState, Profile
from tests.scenario_kit import TODAY, FakeExplainer, FakeInterp, demo_snapshot

KB = demo_snapshot().bundle
RECOGNIZE_AT, NEGATE_AT = 0.3, 0.75
_, INDEX = build_questions(KB, None)


def qid(key: str, option: str | None = None) -> str:
    """Идентификатор атомарного вопроса JEV для факта (и варианта)."""
    for k, q in INDEX.items():
        if q.fact and q.fact.key == key and (option is None or (q.option and q.option.value == option)):
            return k
    raise KeyError((key, option))


def ans(**probs: float) -> dict:
    """Ответ JEV: choice — самая вероятная метка."""
    return {"choice": max(probs, key=probs.get), "probabilities": probs}


def run(answers: dict) -> Interpretation:
    return map_answers(answers, INDEX, RECOGNIZE_AT, NEGATE_AT)


# --------------------------------------------------------------------------- map_answers


def test_role_self_and_spouse_are_separate_items():
    it = run({qid("citizenship_group", "third"): ans(self=0.8, spouse=0.7, not_mentioned=0.1)})
    got = {(r.role, r.key): r.value for r in it.items}
    assert got == {("self", "citizenship_group"): ["third"], ("spouse", "citizenship_group"): ["third"]}


def test_role_only_spouse_when_author_not_mentioned():
    it = run({qid("citizenship_group", "eu"): ans(spouse=0.9, self=0.05)})
    assert [(r.role, r.value) for r in it.items] == [("spouse", ["eu"])]


def test_below_recognition_threshold_is_ignored():
    it = run({qid("citizenship_group", "eu"): ans(self=0.29, not_mentioned=0.7)})
    assert it.items == [] and it.unclear == []


def test_author_fact_ties_detected_as_self_not_case():
    assert KB.fact("ties").detect_author is True
    it = run({qid("ties", "spouse_bg"): ans(yes=0.9, not_mentioned=0.05)})
    assert [(r.role, r.key, r.value) for r in it.items] == [("self", "ties", ["spouse_bg"])]


def test_case_fact_goes_to_case_role():
    it = run({qid("has_housing"): ans(yes=0.9)})
    assert [(r.role, r.key, r.value) for r in it.items] == [("case", "has_housing", True)]


def test_two_close_enum_options_become_unclear():
    a, b = qid("who_moves", "self_family"), qid("who_moves", "other_person")
    it = run({a: ans(yes=0.8), b: ans(yes=0.7)})  # разница 0.1 < 0.3
    assert it.unclear == ["who_moves"] and it.items == []


def test_distinct_enum_options_pick_the_leader():
    a, b = qid("who_moves", "self_family"), qid("who_moves", "other_person")
    it = run({a: ans(yes=0.9), b: ans(yes=0.35)})  # разница 0.55
    assert [(r.key, r.value) for r in it.items] == [("who_moves", "self_family")] and it.unclear == []


def test_contradiction_at_threshold_is_unclear_and_has_no_item():
    q = qid("has_housing")
    it = run({q: ans(yes=0.6, contradiction=0.4)})
    assert it.unclear == ["has_housing"] and it.items == []


def test_contradiction_below_threshold_is_recognized():
    it = run({qid("has_housing"): ans(yes=0.8, contradiction=0.39)})
    assert it.unclear == [] and [(r.key, r.value) for r in it.items] == [("has_housing", True)]


def test_explicit_no_for_bool_recognized_as_false():
    it = run({qid("has_housing"): ans(explicit_no=0.9)})
    assert [(r.key, r.value) for r in it.items] == [("has_housing", False)]


def test_explicit_no_below_negation_threshold_is_ignored():
    it = run({qid("has_housing"): ans(explicit_no=0.7, not_mentioned=0.3)})
    assert it.items == []


def test_explicit_no_for_multi_option_excludes_nothing():
    it = run({qid("income_sources", "pension"): ans(explicit_no=0.95)})
    assert it.items == [] and it.unclear == []
    # и на профиле ничего не меняется: подтверждённая пенсия остаётся
    p = Profile()
    p.confirm_value(MAIN, "income_sources", ["pension"], "button", KB)
    apply_to_profile(it, p, KB)
    assert p.value(MAIN, "income_sources") == ["pension"] and not p.pending()


def test_multi_options_are_collected_into_one_item():
    it = run({qid("income_sources", "pension"): ans(self=0.9), qid("income_sources", "passive"): ans(self=0.6)})
    assert [(r.role, r.key, r.value) for r in it.items] == [("self", "income_sources", ["passive", "pension"])]


def test_intent_and_current_answer_are_mapped():
    q = next(q for q in KB.questions if q.id == "q_housing")
    _, index = build_questions(KB, q)
    it = map_answers({"intent": ans(answer_current=0.9), "current": ans(yes=0.8)}, index, RECOGNIZE_AT, NEGATE_AT)
    assert it.intent == "answer_current" and it.intent_conf == 0.9 and it.current_answer is True
    it = map_answers({"current": ans(unclear=0.9)}, index, RECOGNIZE_AT, NEGATE_AT)
    assert it.current_answer is None


# --------------------------------------------------------------------------- apply_to_profile


def test_apply_writes_recognized_not_confirmed():
    p = Profile()
    apply_to_profile(Interpretation(ok=True, items=[Recognized("case", "has_housing", True, 0.9)]), p, KB)
    f = p.get(CASE, "has_housing")
    assert f.state is FactState.RECOGNIZED and p.value(CASE, "has_housing") is MISSING


def test_multi_is_merged_with_confirmed_without_false_conflict():
    p = Profile()
    p.confirm_value(MAIN, "income_sources", ["pension"], "button", KB)
    it = Interpretation(ok=True, items=[Recognized("self", "income_sources", ["remote_employee"], 1.0)])
    assert apply_to_profile(it, p, KB) == []
    f = p.get(MAIN, "income_sources")
    assert f.state is FactState.RECOGNIZED and f.value == ["pension", "remote_employee"]
    assert not [x for x in p.pending() if x.state is FactState.CONFLICT]


def test_multi_subset_of_confirmed_changes_nothing():
    p = Profile()
    p.confirm_value(MAIN, "income_sources", ["pension", "passive"], "button", KB)
    apply_to_profile(Interpretation(ok=True, items=[Recognized("self", "income_sources", ["pension"], 1.0)]), p, KB)
    f = p.get(MAIN, "income_sources")
    assert f.state is FactState.CONFIRMED and f.value == ["pension", "passive"]


def test_multi_none_does_not_combine_with_other_options():
    p = Profile()
    apply_to_profile(Interpretation(ok=True, items=[Recognized("self", "income_sources", ["none", "pension"], 1.0)]), p, KB)
    assert p.get(MAIN, "income_sources").value == ["pension"]


def test_scalar_disagreement_with_confirmed_is_conflict():
    p = Profile()
    p.confirm_value(CASE, "has_housing", True, "button", KB)
    apply_to_profile(Interpretation(ok=True, items=[Recognized("case", "has_housing", False, 0.9)]), p, KB)
    f = p.get(CASE, "has_housing")
    assert f.state is FactState.CONFLICT and f.prev_value is True


def test_spouse_info_without_member_is_deferred():
    p = Profile()
    item = Recognized("spouse", "citizenship_group", ["eu"], 0.9)
    left = apply_to_profile(Interpretation(ok=True, items=[item]), p, KB)
    assert left == [item] and not p.facts


def test_deferred_spouse_info_is_applied_when_member_appears():
    p = Profile()
    item = Recognized("spouse", "citizenship_group", ["eu"], 0.9)
    deferred = apply_to_profile(Interpretation(ok=True, items=[item]), p, KB)
    s = SessionData(1, 0, "asking", p, {"deferred": [d.__dict__ for d in deferred]}, None)
    d = Dialog(FakeInterp(), FakeExplainer(), today=lambda: TODAY, consult_enabled=False)
    d._sync_members(s, KB)  # состав семьи ещё не известен: сведение ждёт
    assert not p.members() and s.ui["deferred"] == [item.__dict__]
    p.confirm_value(CASE, "family_moving", ["spouse"], "button", KB)
    d._sync_members(s, KB)  # супруг(а) появился: сведение применяется
    spouse = p.members()[0]
    f = p.get(spouse.id, "citizenship_group")
    assert spouse.relation == "spouse" and f.state is FactState.RECOGNIZED and f.value == ["eu"]
    assert s.ui["deferred"] == []
