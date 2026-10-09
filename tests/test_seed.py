"""Проверка начального наполнения: рабочий набор, демо-набор, загрузчик и сценарии на демо."""

import re
from datetime import date

import pytest

from bginfo.db.conn import open_db
from bginfo.engine.evaluate import Status, evaluate_all
from bginfo.engine.facts import CASE, MAIN, Profile
from bginfo.kb import releases as R
from bginfo.kb.validate import errors, ground_texts, validate
from bginfo.seed.__main__ import main
from bginfo.seed.loader import load_seed_bundle

AS_OF = date(2026, 10, 9)
THIRD_COUNTRY_IDS = [
    "pensioner", "trade_representative", "digital_nomad", "work_single_permit", "eu_blue_card", "student",
    "business_jobs", "investment_real_estate", "investment_region", "family_of_bg_citizen",
]
ALL_IDS = THIRD_COUNTRY_IDS + ["family_member", "eu_citizen", "bulgarian_origin"]


@pytest.fixture(scope="module")
def work():
    return load_seed_bundle()


@pytest.fixture(scope="module")
def demo():
    return load_seed_bundle(demo=True)


def test_work_bundle_valid(work):
    assert errors(validate(work)) == []
    assert not work.demo
    assert sorted(g.id for g in work.grounds) == sorted(ALL_IDS)


def test_work_bundle_is_all_draft_and_unverified(work):
    for g in work.grounds:
        assert g.status == "draft", g.id
        assert not g.demo and not g.checked_at and not g.checked_by, g.id
        assert g.requirements, g.id
        for r in g.requirements:
            assert r.verified is False, (g.id, r.id)
            assert r.verified_by == "" and r.verified_at is None, (g.id, r.id)
    for p in work.parameters:
        assert all(not v.verified for v in p.values), p.id


def test_bginfo_only_not_in_requirements(work):
    for g in work.grounds:
        for r in g.requirements:
            assert r.research_status != "bginfo_only", (g.id, r.id)
            assert r.research_status, (g.id, r.id)


def test_no_euro_sums_in_texts(work):
    sums = re.compile(r"\d{3,}\s*(€|евро)|(€|евро)\s*\d{3,}", re.IGNORECASE)
    for g in work.grounds:
        for where, text in ground_texts(g):
            assert not sums.search(text or ""), (g.id, where, text)


def test_demo_bundle_valid(demo):
    assert demo.demo
    assert errors(validate(demo)) == []
    for g in demo.grounds:
        assert g.status == "published" and g.demo and g.title.startswith("[ТЕСТ] "), g.id
        assert all(r.verified and r.verified_by and r.verified_at for r in g.requirements), g.id


def test_cli_seed_draft_and_demo(tmp_path):
    db = tmp_path / "work.db"
    assert main(["--db", str(db), "seed-draft"]) == 0
    assert main(["--db", str(db), "seed-draft"]) == 1  # черновик уже заполнен
    assert main(["--db", str(db), "seed-draft", "--force"]) == 0
    conn = open_db(db)
    try:
        assert R.active_snapshot(conn) is None  # выпуск не опубликован
        assert len(R.load_draft(conn).grounds) == len(ALL_IDS)
    finally:
        conn.close()

    demo_db = tmp_path / "demo.db"
    assert main(["--db", str(demo_db), "build-demo"]) == 0
    assert main(["build-demo", "--db", str(demo_db)]) == 1  # файл уже есть
    conn = open_db(demo_db)
    try:
        snap = R.active_snapshot(conn)
        assert snap is not None and snap.bundle.demo
        assert snap.bundle.active_grounds()
    finally:
        conn.close()


def test_build_demo_refuses_working_db(tmp_path, monkeypatch):
    from bginfo.config import get_settings, reset_config_cache

    target = tmp_path / "app.db"
    monkeypatch.setenv("DB_PATH", str(target))
    reset_config_cache()
    try:
        assert get_settings().DB_PATH == str(target)
        assert main(["--db", str(target), "build-demo", "--force"]) == 1
        assert not target.exists()
    finally:
        monkeypatch.undo()
        reset_config_cache()


# ----------------------------------------------------------------------------- сценарии на демо


def _profile(**person) -> Profile:
    p = Profile()
    # без ответа «для кого подбор» исключающее условие основания неизвестно и статус не может быть FIT
    p.confirm_value(CASE, "who_moves", "self", "test", None)
    for k, v in person.items():
        p.confirm_value(MAIN, k, v, "test", None)
    return p


def _status(ev, gid, pid=MAIN):
    r = ev.get(gid, pid)
    assert r is not None, gid
    return r.status


def test_scenario_pensioner_fit(demo):
    p = _profile(citizenship_group=["third"], income_sources=["pension"], pension_amount_eur=900, pension_doc=True)
    p.confirm_value(CASE, "has_housing", True, "test", None)
    assert _status(evaluate_all(demo, p, AS_OF), "pensioner") is Status.FIT


def _remote(income, **extra) -> Profile:
    p = _profile(citizenship_group=["third"], income_sources=["remote_employee"], employer_outside_eu=True,
                 income_prev_year_eur=income, **extra)
    p.confirm_value(CASE, "has_housing", True, "test", None)
    return p


def test_scenario_digital_nomad_fit(demo):
    assert _status(evaluate_all(demo, _remote(40000), AS_OF), "digital_nomad") is Status.FIT


def test_scenario_digital_nomad_low_income_individual(demo):
    assert _status(evaluate_all(demo, _remote(29000), AS_OF), "digital_nomad") is Status.INDIVIDUAL


def test_scenario_freelancer_with_bg_clients_not_eligible(demo):
    p = _profile(citizenship_group=["third"], income_sources=["freelance_foreign"], freelance_one_year=True,
                 clients_in_bg=True, income_prev_year_eur=40000)
    p.confirm_value(CASE, "has_housing", True, "test", None)
    assert _status(evaluate_all(demo, p, AS_OF), "digital_nomad") is Status.NOT_ELIGIBLE


def test_scenario_eu_citizen(demo):
    p = _profile(citizenship_group=["eu"])
    ev = evaluate_all(demo, p, AS_OF)
    for gid in THIRD_COUNTRY_IDS:
        assert _status(ev, gid) is Status.NOT_ELIGIBLE, gid
    assert _status(ev, "eu_citizen") in (Status.CLARIFY, Status.FIT)


def test_scenario_spouse_of_pensioner_fit(demo):
    p = _profile(citizenship_group=["third"], income_sources=["pension"], pension_amount_eur=900, pension_doc=True)
    p.confirm_value(CASE, "has_housing", True, "test", None)
    p.set_members([("s1", "spouse", "Супруга")])
    p.confirm_value("s1", "relation", "spouse", "test", None)
    p.confirm_value("s1", "citizenship_group", ["third"], "test", None)
    p.confirm_value("s1", "relation_docs", True, "test", None)
    # Переезжают вместе: у заявителя пока нет статуса в Болгарии (в диалоге это производный факт).
    p.confirm_value(MAIN, "status_in_bg", "none", "derived", None)
    ev = evaluate_all(demo, p, AS_OF)
    assert _status(ev, "pensioner") is Status.FIT
    # чл. 33ф: воссоединение — после того, как заявитель получит разрешение не меньше чем на год
    r = ev.get("family_member", "s1")
    assert r.status is Status.POSSIBLE_AFTER
    assert [x.id for x in r.unmet] == ["sponsor_one_year"]
    # Заявитель уже живёт с ВНЖ на год — супруг предварительно подходит.
    p.confirm_value(MAIN, "status_in_bg", "long_term", "test", None)
    p.confirm_value(MAIN, "sponsor_permit_1y", True, "test", None)
    assert _status(evaluate_all(demo, p, AS_OF), "family_member", "s1") is Status.FIT


# ----------------------------------------------------------------------------- проходимость оснований членов семьи

# Факты, которые членам семьи осознанно не задаются: {факт: причина}.
NOT_ASKED_TO_FAMILY = {
    "status_in_bg": "статус в Болгарии спрашивается только у заявителя; у переезжающих членов семьи его нет",
    "ties": "связи (учёба, инвестиции, происхождение) — планы самого заявителя; контрольный вопрос про связи один",
    "bg_origin_doc": "зависит от связей (ties) заявителя",
    "full_time_study": "зависит от связей (ties) заявителя",
    "study_admitted": "зависит от связей (ties) заявителя",
    "invest_kind": "зависит от связей (ties) заявителя",
    "invest_amount_eur": "зависит от связей (ties) заявителя",
    "invest_loan_ok": "зависит от вида инвестиции заявителя",
    "region_share_50": "зависит от вида инвестиции заявителя",
    "region_jobs_ready": "зависит от вида инвестиции заявителя",
}


def _self_facts(c, out):
    from bginfo.kb import model as m

    if isinstance(c, m.CondAnd):
        for x in c.all:
            _self_facts(x, out)
    elif isinstance(c, m.CondOr):
        for x in c.any:
            _self_facts(x, out)
    elif isinstance(c, m.CondNot):
        _self_facts(c.cond, out)
    elif not isinstance(c, m.CondSponsorBasis) and c.fact.startswith("self."):
        out.add(c.fact[5:])


def test_applicant_grounds_are_passable_for_spouse(work):
    for g in work.grounds:
        if g.subject != "applicant":
            continue
        facts: set[str] = set()
        for r in g.requirements:
            _self_facts(r.cond, facts)
        if g.applies_if is not None:
            _self_facts(g.applies_if, facts)
        for u in g.unsupported:
            _self_facts(u.cond, facts)
        for key in sorted(facts - set(NOT_ASKED_TO_FAMILY)):
            qs = [q for q in work.questions if q.fact == key]
            assert any("member" in q.ask_for and "spouse" in q.member_relations for q in qs), (g.id, key)


def test_family_question_exceptions_are_real_facts(work):
    assert all(work.fact(k) is not None for k in NOT_ASKED_TO_FAMILY)
