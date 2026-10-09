from datetime import date

import pytest

from bginfo.engine.evaluate import Status, evaluate_all, param_value
from bginfo.engine.facts import CASE, MAIN, FactState, Profile
from bginfo.engine.planner import next_step
from bginfo.kb.validate import errors, validate
from tests.kbfixture import mini_bundle

D = date(2026, 10, 9)


@pytest.fixture
def kb():
    return mini_bundle()


def prof(kb, **facts) -> Profile:
    p = Profile()
    for key, val in facts.items():
        pid = CASE if key == "has_housing" else MAIN
        p.confirm_value(pid, key, val, "button", kb)
    return p


def st(ev, gid, pid=MAIN):
    return ev.get(gid, pid).status


def test_fixture_is_valid(kb):
    assert errors(validate(kb)) == []


def test_param_periods_and_derived(kb):
    assert param_value(kb, "min_wage", date(2025, 6, 1)) == 600
    assert param_value(kb, "min_wage", D) == 620.20
    assert param_value(kb, "dn_income", D) == 31010.0
    assert param_value(kb, "min_wage", date(2024, 1, 1)) is None


def test_unknown_is_not_fail_or_pass(kb):
    ev = evaluate_all(kb, Profile(), D)
    assert st(ev, "pensioner") is Status.CLARIFY
    assert st(ev, "nomad") is Status.CLARIFY


def test_pensioner_fit_and_boundary(kb):
    p = prof(kb, citizenship_group="third", income_sources=["pension"], pension_amount_eur=620.20, has_housing=True)
    assert st(evaluate_all(kb, p, D), "pensioner") is Status.FIT
    p = prof(kb, citizenship_group="third", income_sources=["pension"], pension_amount_eur=620.19, has_housing=True)
    assert st(evaluate_all(kb, p, D), "pensioner") is Status.INDIVIDUAL  # on_fail=individual_review


def test_possible_after_housing(kb):
    p = prof(kb, citizenship_group="third", income_sources=["pension"], pension_amount_eur=900, has_housing=False)
    r = evaluate_all(kb, p, D).get("pensioner")
    assert r.status is Status.POSSIBLE_AFTER
    assert [x.id for x in r.unmet] == ["housing"]


def test_explicit_negation_excludes_only_that_ground(kb):
    p = prof(kb, citizenship_group="third", income_sources=["remote_foreign"], annual_income_eur=40000)
    ev = evaluate_all(kb, p, D)
    assert st(ev, "pensioner") is Status.NOT_ELIGIBLE
    assert st(ev, "nomad") is Status.FIT


def test_param_date_switch_changes_result(kb):
    p = prof(kb, citizenship_group="third", income_sources=["pension"], pension_amount_eur=610, has_housing=True)
    assert st(evaluate_all(kb, p, date(2025, 12, 31)), "pensioner") is Status.FIT
    assert st(evaluate_all(kb, p, date(2026, 1, 1)), "pensioner") is Status.INDIVIDUAL


def test_eu_citizen_not_third_country_grounds(kb):
    p = prof(kb, citizenship_group="eu")
    ev = evaluate_all(kb, p, D)
    assert st(ev, "pensioner") is Status.NOT_ELIGIBLE
    assert st(ev, "nomad") is Status.NOT_ELIGIBLE


def test_recognized_fact_not_used_until_confirmed(kb):
    p = prof(kb, citizenship_group="third")
    p.recognize(MAIN, "income_sources", ["pension"], "jev")
    ev = evaluate_all(kb, p, D)
    assert st(ev, "pensioner") is Status.CLARIFY
    step = next_step(kb, p, ev, D)
    assert step.kind == "question" and step.question.id == "q_inc"  # multi подтверждается вопросом с предвыбором


def test_conflict_keeps_old_value_until_resolved(kb):
    p = prof(kb, citizenship_group="third")
    assert p.recognize(MAIN, "citizenship_group", "eu", "jev") is FactState.CONFLICT
    step = next_step(kb, p, evaluate_all(kb, p, D), D)
    assert step.kind == "conflict"
    p.reject_pending(MAIN, "citizenship_group")
    assert p.value(MAIN, "citizenship_group") == "third"


def test_correction_invalidates_dependents(kb):
    p = prof(kb, citizenship_group="third", income_sources=["pension"], pension_amount_eur=900)
    removed = p.confirm_value(MAIN, "income_sources", ["remote_foreign"], "button", kb)
    assert any("pension_amount_eur" in r for r in removed)
    assert p.get(MAIN, "pension_amount_eur") is None
    assert p.value(MAIN, "citizenship_group") == "third"  # независимый факт сохраняется


def test_family_member_evaluated_separately(kb):
    p = prof(kb, citizenship_group="third", income_sources=["pension"], pension_amount_eur=900, has_housing=True)
    p.set_members([("s1", "spouse", "Супруг(а)")])
    p.confirm_value("s1", "citizenship_group", "third", "button", kb)
    p.confirm_value("s1", "relation_doc", True, "button", kb)
    ev = evaluate_all(kb, p, D)
    assert st(ev, "pensioner") is Status.FIT
    assert st(ev, "family", "s1") is Status.FIT
    # основание заявителя не переносится на супруга автоматически
    assert st(ev, "pensioner", "s1") is Status.CLARIFY


def test_family_without_sponsor_basis(kb):
    p = prof(kb, citizenship_group="eu", status_in_bg="none")
    p.set_members([("s1", "spouse", "Супруг(а)")])
    p.confirm_value("s1", "relation_doc", True, "button", kb)
    assert st(evaluate_all(kb, p, D), "family", "s1") is Status.NOT_ELIGIBLE


def test_removing_member_drops_their_facts(kb):
    p = prof(kb)
    p.set_members([("s1", "spouse", "Супруг(а)")])
    p.confirm_value("s1", "citizenship_group", "third", "button", kb)
    p.set_members([])
    assert p.get("s1", "citizenship_group") is None


def test_needs_review_gives_individual(kb):
    data = kb.model_dump(mode="json")
    data["grounds"][0]["status"] = "needs_review"
    data["grounds"][0]["review_reason"] = "Изменился закон"
    data["grounds"][0]["requirements"][1]["disputed"] = True
    from bginfo.kb.model import Bundle
    kb2 = Bundle.model_validate(data)
    p = prof(kb2, citizenship_group="third", income_sources=["pension"], pension_amount_eur=5000, has_housing=True)
    assert st(evaluate_all(kb2, p, D), "pensioner") is Status.INDIVIDUAL


def test_planner_asks_core_then_relevant(kb):
    p = Profile()
    s = next_step(kb, p, evaluate_all(kb, p, D), D)
    assert s.question.id == "q_cit"
    p = prof(kb, citizenship_group="third")
    s = next_step(kb, p, evaluate_all(kb, p, D), D)
    assert s.question.id == "q_inc"
    p = prof(kb, citizenship_group="third", income_sources=["pension"])
    s = next_step(kb, p, evaluate_all(kb, p, D), D)
    assert s.question.id in ("q_pens", "q_house")
    assert s.question.id != "q_annual"  # не спрашиваем про доход DN без удалёнки


def test_planner_skips_unknown_answers(kb):
    p = prof(kb, citizenship_group="third", income_sources=["pension"])
    p.mark_unknown(MAIN, "pension_amount_eur")
    p.mark_unknown(CASE, "has_housing")
    s = next_step(kb, p, evaluate_all(kb, p, D), D)
    assert s.kind == "review"


def test_profile_roundtrip(kb):
    p = prof(kb, citizenship_group="third", income_sources=["pension"])
    p.recognize(CASE, "has_housing", True, "jev")
    q = Profile.from_dict(p.to_dict())
    assert q.value(MAIN, "income_sources") == ["pension"]
    assert q.get(CASE, "has_housing").state is FactState.RECOGNIZED
