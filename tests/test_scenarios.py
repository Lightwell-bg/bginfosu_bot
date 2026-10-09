"""Сквозные сценарии анкеты через контроллер Dialog на ДЕМО-бандле (seed, demo=True).

Это МОКИ, а не живая интеграция: Telegram отсутствует, JEV и генеративная модель заменены
заглушками (FakeInterp, FakeExplainer, FakeClient), БД нужна только сценариям публикации выпусков.
Проверяется логика карточек src/bginfo/seed/grounds/*.yaml и поведение контроллера, а не AI.

Тесты с пометкой ``xfail(strict=True)`` фиксируют НАЙДЕННЫЕ расхождения с задуманным поведением:
ожидание записано по замыслу кода (docstring планировщика, текст вопроса), а не подогнано под
фактический результат. Когда расхождение исправят, strict-xfail упадёт и напомнит снять пометку.
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import timedelta
from html.parser import HTMLParser
from typing import ClassVar

import pytest

from bginfo.ai.explain import Explainer
from bginfo.ai.interpret import Interpretation, Recognized
from bginfo.ai.ledger import AiLedger
from bginfo.ai.openrouter import AiError
from bginfo.db.conn import open_db, utcnow
from bginfo.engine import present as P
from bginfo.engine.evaluate import evaluate_all
from bginfo.engine.facts import CASE, MAIN, MISSING, FactState, Profile
from bginfo.engine.result import build_result
from bginfo.kb import releases as R
from bginfo.seed.loader import load_seed_bundle
from tests.scenario_kit import TODAY, UNKNOWN, FakeInterp, Run, demo_snapshot

PENSIONER_TITLE = "[ТЕСТ] ВНЖ для пенсионеров"
DN_TITLE = "[ТЕСТ] ВНЖ для цифровых кочевников (Digital nomad)"

# Пенсионер из третьей страны: пенсия 900, документ есть, жильё есть.
PENSIONER = {
    "who_moves": "self", "citizenship_group": ["third"], "residence": "home", "income_sources": ["pension"],
    "pension_amount_eur": 900, "pension_doc": True, "has_housing": True, "ties": ["none"], "goal": UNKNOWN,
}
# Удалённый работник по найму у работодателя вне ЕС.
REMOTE = {
    "who_moves": "self", "citizenship_group": ["third"], "residence": "home", "income_sources": ["remote_employee"],
    "employer_outside_eu": True, "income_prev_year_eur": 40000, "rep_company_ready": False, "has_housing": True,
    "ties": ["none"], "goal": UNKNOWN,
}


def hrefs(text: str) -> list[str]:
    return [htmllib.unescape(h) for h in re.findall(r'href="([^"]*)"', text)]


class _TagChecker(HTMLParser):
    """Проверка, что в тексте только разрешённые теги и они сбалансированы."""

    ALLOWED: ClassVar[set[str]] = {"b", "i", "a"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.bad: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED:
            self.bad.append(tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.bad.append("/" + tag)


def assert_safe_html(text: str) -> None:
    chk = _TagChecker()
    chk.feed(text)
    chk.close()
    assert not chk.bad and not chk.stack, (chk.bad, chk.stack)
    assert not re.search(r"&(?!(?:amp|lt|gt|quot|#\d+);)", text), "неэкранированный &"


# --------------------------------------------------------------------------- a


async def test_a_buttons_only_pensioner_gets_pensioner_ground_first():
    r = Run()
    await r.result(PENSIONER)
    assert r.s.state == "result"
    rv = r.view()
    assert rv.kind == "ok" and rv.primary.title == PENSIONER_TITLE
    assert rv.primary.status.value == "fit" and "предварительно подходит" in r.html()
    st = r.statuses()
    assert st[(MAIN, "pensioner")] == "fit"
    assert [k for k, v in st.items() if v == "fit"] == [(MAIN, "pensioner")]
    assert r.out.result_record["release_id"] == 1 and r.s.release_id == 1
    # анкета не задавала лишнего: ровно базовые вопросы и уточнения по пенсии
    assert ("p0", "pension_amount_eur") in r.asked and ("p0", "employer_outside_eu") not in r.asked


# --------------------------------------------------------------------------- b


def _text_interp(*items: Recognized, **kw) -> FakeInterp:
    return FakeInterp(Interpretation(ok=True, intent=kw.get("intent", "new_info"), intent_conf=0.9,
                                     items=list(items), unclear=kw.get("unclear", [])))


async def test_b_free_text_first_then_confirmation_questions_and_result():
    interp = _text_interp(Recognized("self", "income_sources", ["pension"], 0.9),
                          Recognized("case", "has_housing", True, 0.8),
                          Recognized("case", "family_moving", ["spouse"], 0.9))
    r = Run(interp=interp)
    await r.start()
    await r.press("free")
    assert r.s.state == "free_wait"
    await r.text("Я пенсионер, переезжаем с женой, квартира в Болгарии есть")
    assert interp.calls == 1 and "Правильно ли я понял" in r.html() and "Жильё в Болгарии — да" in r.html()
    assert r.s.profile.value(CASE, "has_housing") is MISSING  # до подтверждения значение не принято
    await r.press("pend_ok")
    assert r.value("has_housing") is True
    answers = {**PENSIONER, "who_moves": "self_family", "family_moving": ["spouse"],
               ("s1", "citizenship_group"): ["third"], ("s1", "income_sources"): ["none"],
               ("s1", "relation_docs"): True}
    await r.fill(answers)
    await r.press("review_ok")
    assert r.view().primary.title == PENSIONER_TITLE
    assert ("case", "has_housing") not in r.asked  # подтверждённое из текста второй раз не спрашивается
    assert ("p0", "citizenship_group") in r.asked and ("s1", "relation_docs") in r.asked
    assert r.statuses()[("s1", "family_member")] == "possible_after"


async def test_b_recognized_multi_facts_are_confirmed_by_their_own_question():
    interp = _text_interp(Recognized("self", "income_sources", ["pension"], 0.9),
                          Recognized("case", "has_housing", True, 0.8))
    r = Run(interp=interp)
    await r.start()
    await r.press("free")
    await r.text("Я пенсионер, квартира есть")
    await r.press("pend_ok")
    await r.fill(PENSIONER, strict=False)
    assert ("p0", "income_sources") in r.asked


# --------------------------------------------------------------------------- c


async def test_c_switch_from_buttons_to_text_keeps_answers_and_does_not_repeat_questions():
    interp = _text_interp(Recognized("self", "income_sources", ["pension"], 0.9),
                          Recognized("case", "has_housing", True, 0.8))
    r = Run(interp=interp)
    await r.start()
    await r.press("go")
    await r.press("ans", "self")
    await r.press("tog", "third")
    await r.press("done")
    assert r.s.ui["qid"] == "q_residence"
    await r.text("Вообще я пенсионер, жильё в Болгарии уже есть")  # на середине анкеты
    assert "Правильно ли я понял" in r.html() and "Жильё в Болгарии — да" in r.html()
    await r.press("pend_ok")
    assert r.value("has_housing") is True and r.value("citizenship_group") == ["third"]
    # в словаре НЕТ who_moves/citizenship/has_housing: повторный вопрос по ним (strict) упал бы с ошибкой
    rest = {k: v for k, v in PENSIONER.items() if k not in ("who_moves", "citizenship_group", "has_housing")}
    await r.fill(rest)
    await r.press("review_ok")
    assert r.asked.count(("p0", "citizenship_group")) == 1 and r.asked.count(("case", "who_moves")) == 1
    assert ("case", "has_housing") not in r.asked
    assert r.view().primary.title == PENSIONER_TITLE


# --------------------------------------------------------------------------- d


async def test_d_remote_employee_plus_foreign_company_gets_digital_nomad_and_alternatives():
    r = Run()
    answers = dict(REMOTE, income_sources=["remote_employee", "own_company_abroad"], rep_company_ready=UNKNOWN)
    await r.result(answers)
    assert r.statuses()[(MAIN, "digital_nomad")] == "fit"
    rv = r.view()
    assert rv.primary.title == DN_TITLE and rv.primary.status.value == "fit"
    assert any("торговое представительство" in a.title for a in rv.alternatives)
    await r.press("alts")
    alts = r.html()
    assert "Все рассмотренные варианты" in alts and "торговое представительство" in alts
    assert alts.count("⛔") >= 2  # прочие основания перечислены как «не подходит»


# --------------------------------------------------------------------------- e


FAMILY_ANSWERS = {
    **PENSIONER, "who_moves": "self_family", "family_moving": ["spouse"],
    ("s1", "citizenship_group"): ["third"], ("s1", "income_sources"): ["remote_employee"],
    ("s1", "employer_outside_eu"): True, ("s1", "income_prev_year_eur"): 40000,
    ("s1", "rep_company_ready"): False, ("s1", "relation_docs"): True}
# Заявитель из третьей страны, ничем из перечисленного не занят; супруг(а) — гражданин(ка) ЕС.
EU_SPOUSE = {
    **PENSIONER, "income_sources": ["none"], "who_moves": "self_family", "family_moving": ["spouse"],
    ("s1", "citizenship_group"): ["eu"], ("s1", "income_sources"): ["none"], ("s1", "relation_docs"): True}


async def test_e_spouse_with_own_remote_work_gets_family_block_with_reunification():
    r = Run()
    await r.result(FAMILY_ANSWERS)
    st = r.statuses()
    assert st[(MAIN, "pensioner")] == "fit"
    # воссоединение: основной заявитель подходит, но ВНЖ ещё нет -> «возможно после выполнения условия»
    assert st[("s1", "family_member")] == "possible_after"
    html = r.html()
    assert "<b>Семья:</b>" in html and "<b>Супруг(а)</b>" in html
    family = next(mv for mv in r.view().family if mv.pid == "s1")
    assert [g.status.value for g in family.grounds if "воссоединение" in g.title] == ["possible_after"]
    assert "возможно после выполнения условия" in html
    assert "автоматически не распространяется" in html


async def test_e_spouse_own_ground_can_reach_fit():
    r = Run()
    await r.result(FAMILY_ANSWERS)
    assert r.statuses()[("s1", "digital_nomad")] == "fit"


# --------------------------------------------------------------------------- f


async def test_f_everything_unknown_never_claims_fit():
    r = Run()
    await r.result({"who_moves": "self"}, strict=False)  # на всё остальное — «Не знаю»
    assert r.s.state == "result"
    st = r.statuses()
    assert set(st.values()) == {"clarify"}  # ни «подходит», ни «не подходит» без данных
    html = r.html()
    assert "предварительно подходит" not in html and "✅" not in html
    assert "нужно уточнить" in html
    rv = r.view()
    assert rv.kind == "clarify" and rv.primary is None
    assert "Что стоит проверить первым" not in html
    assert_safe_html(html)
    # «Почему» и «Какие ещё варианты» тоже строятся без исключений
    await r.press("why")
    assert_safe_html(r.html())
    await r.press("alts")
    assert_safe_html(r.html())


# --------------------------------------------------------------------------- g


async def _to_review(interp=None, answers=None) -> Run:
    r = Run(interp=interp or FakeInterp())
    await r.fill(answers or PENSIONER)
    assert r.s.state == "review"
    return r


async def test_g_contradiction_with_confirmed_value_asks_and_old_choice_keeps_it():
    r = await _to_review()
    r.interp.result = Interpretation(ok=True, intent="correction", intent_conf=0.9,
                                     items=[Recognized("case", "has_housing", False, 0.9)])
    await r.text("Нет, жилья в Болгарии у меня нет")
    f = r.s.profile.get(CASE, "has_housing")
    assert f.state is FactState.CONFLICT and f.prev_value is True
    assert "Что верно?" in r.html() and r.s.ui["qid"] is None
    assert [b.arg for row in r.out.replies[-1].buttons for b in row] == ["new", "old"]
    await r.press("conf", "old")
    assert r.value("has_housing") is True and not r.s.profile.pending()
    assert r.s.state == "review"


async def test_g_contradiction_keeps_old_value_until_answered_and_asks_again():
    r = await _to_review()
    r.interp.result = Interpretation(ok=True, intent="correction", intent_conf=0.9,
                                     items=[Recognized("case", "has_housing", False, 0.9)])
    await r.text("Нет, жилья в Болгарии у меня нет")
    # пока человек не ответил, подтверждённое значение не заменено, а вопрос «Что верно?» висит
    f = r.s.profile.get(CASE, "has_housing")
    assert f.state is FactState.CONFLICT and f.prev_value is True
    assert r.value("has_housing") is not False  # новое значение в расчёт не попадает
    assert "Что верно?" in r.html()
    # ещё один текст не снимает конфликт молча
    r.interp.result = Interpretation(ok=True, intent="other", intent_conf=0.9)
    await r.text("и что теперь")
    f = r.s.profile.get(CASE, "has_housing")
    assert f.state is FactState.CONFLICT and f.prev_value is True


async def test_g_contradiction_new_choice_replaces_value():
    r = await _to_review()
    r.interp.result = Interpretation(ok=True, intent="correction", intent_conf=0.9,
                                     items=[Recognized("case", "has_housing", False, 0.9)])
    await r.text("Нет, жилья нет")
    await r.press("conf", "new")
    assert r.value("has_housing") is False and not r.s.profile.pending()


async def test_g_unclear_statement_is_asked_again_explicitly():
    r = Run(interp=FakeInterp(Interpretation(ok=True, intent="new_info", intent_conf=0.9, unclear=["has_housing"])))
    await r.start()
    await r.press("go")
    await r.press("ans", "self")
    await r.text("то ли есть жильё, то ли нет")
    # вопрос про жильё задан сразу (в обычном порядке он шёл бы гораздо позже), ничего не принято молча
    assert r.s.ui["qid"] == "q_housing" and "жильё" in r.html().lower()
    assert r.s.profile.get(CASE, "has_housing") is None


async def test_g_off_topic_text_changes_nothing():
    r = await _to_review()
    before = r.s.profile.to_dict()["facts"]
    r.interp.result = Interpretation(ok=True, intent="other", intent_conf=0.9)
    await r.text("какая сегодня погода")
    assert "Я помогаю только с подбором" in r.html()
    assert r.s.profile.to_dict()["facts"] == before and r.s.state == "review"


# --------------------------------------------------------------------------- h


async def test_h_editing_income_sources_invalidates_dependents_and_asks_new_questions():
    r = Run()
    await r.result(PENSIONER)
    assert r.value("pension_amount_eur") == 900 and r.value("pension_doc") is True
    await r.press("fix")
    idx = r.s.ui["edit_list"].index([MAIN, "income_sources"])
    await r.press("edit", str(idx))
    assert r.s.ui["qid"] == "q_income" and r.s.ui["sel"] == ["pension"]
    await r.press("tog", "pension")
    await r.press("tog", "remote_employee")
    await r.press("done")
    assert r.value("income_sources") == ["remote_employee"]
    assert r.s.profile.get(MAIN, "pension_amount_eur") is None and r.s.profile.get(MAIN, "pension_doc") is None
    assert "Учёл изменение" in "\n".join(x.html for x in r.out.replies)
    invalidated = {h["key"] for h in r.s.profile.history if h["event"] == "invalidated_by:income_sources"}
    assert invalidated == {"pension_amount_eur", "pension_doc"}
    assert r.s.ui["qid"] == "q_employer_outside_eu"  # новые вопросы вместо старых
    # независимые ответы сохранились
    assert r.value("has_housing") is True and r.value("citizenship_group") == ["third"]
    await r.fill(REMOTE)
    await r.press("review_ok")
    assert r.view().primary.title == DN_TITLE
    assert r.statuses()[(MAIN, "pensioner")] == "not_eligible"


# --------------------------------------------------------------------------- i


async def test_i_no_suitable_option_offers_consultation():
    r = Run()
    answers = dict(PENSIONER, income_sources=["none"])
    await r.result(answers)
    rv = r.view()
    assert rv.kind == "none" and rv.primary is None and not rv.alternatives
    assert set(r.statuses().values()) == {"not_eligible"}
    html = r.html()
    assert "ни одно из проверенных оснований сейчас не подходит" in html
    assert "индивидуальную консультацию" in html and "✅" not in html
    assert ("consult", "") in [(b.action, b.arg) for row in r.out.replies[-1].buttons for b in row]
    await r.press("consult")
    assert r.s.state == "consult_preview" and "Отправить" in str(r.out.replies[-1].buttons)


# --------------------------------------------------------------------------- j


async def test_j_bulgarian_citizen_needs_no_permit():
    r = Run()
    await r.result(dict(PENSIONER, citizenship_group=["bg"], income_sources=["none"]), strict=False)
    rv = r.view()
    assert rv.kind == "unsupported" and rv.primary is None
    assert "разрешение на проживание не нужно" in r.html()
    assert "✅" not in r.html()


async def test_j_spouse_eu_citizenship_offers_eu_relative_tie():
    r = Run()
    await r.fill(EU_SPOUSE, until=("p0", "ties"))
    await r.press("tog", "none")
    await r.press("done")  # заявитель ответил «ничего из этого» — а супруг(а) гражданин ЕС
    ties = r.s.profile.get(MAIN, "ties")
    assert r.s.ui.get("spouse_ties_offered") is True
    assert ties.state is FactState.RECOGNIZED and ties.value == ["eu_relative"]  # предложено, не добавлено молча
    assert r.s.profile.value(MAIN, "ties") is MISSING
    assert r.s.ui["qid"] == "q_ties"  # и подтверждается тем же вопросом
    assert "Я отметил то, что понял" in r.html()


async def test_j_offered_eu_relative_is_preselected():
    r = Run()
    await r.fill(EU_SPOUSE, until=("p0", "ties"))
    await r.press("tog", "none")
    await r.press("done")
    buttons = [b.text for row in r.out.replies[-1].buttons for b in row]
    assert any(t.startswith("✅") and "гражданин ЕС" in t for t in buttons)


# --------------------------------------------------------------------------- k


@pytest.mark.parametrize("amount,status,kind", [
    ("31010", "fit", "ok"),               # ровно 50 × 620,20 = 31 010 -> порог включительно
    ("31 010,00 €", "fit", "ok"),
    ("31009.99", "individual", "individual"),  # на копейку ниже: по карточке on_fail=individual_review
])
async def test_k_digital_nomad_income_boundary(amount, status, kind):
    r = Run()
    await r.result(dict(REMOTE, income_prev_year_eur=amount))
    dn = r.statuses()[(MAIN, "digital_nomad")]
    assert dn == status and dn != "not_eligible"
    assert r.view().kind == kind
    assert ("предварительно подходит" in r.html()) == (status == "fit")


# --------------------------------------------------------------------------- l


def _raise_min_pension(data: dict) -> None:
    for p in data["parameters"]:
        if p["id"] == "min_pension_bg":
            p["values"][0]["value"] = 1000.0


def _add_insurance_requirement(data: dict) -> None:
    data["facts"].append({"key": "insurance", "scope": "person", "type": "bool", "label": "Есть медицинская страховка"})
    data["questions"].append({"id": "q_insurance", "fact": "insurance", "priority": 106,
                              "text": "Есть ли у вас медицинская страховка?"})
    g = next(g for g in data["grounds"] if g["id"] == "pensioner")
    g["requirements"].append({
        "id": "insurance", "label": "Есть медицинская страховка", "cond": {"op": "known_true", "fact": "self.insurance"},
        "kind": "procedure", "stage": "permit", "on_fail": "possible_after", "fail_text": "Нужна страховка.",
        "unknown_text": "Уточните страховку.", "source_ids": ["zchrb"], "verified": True,
        "verified_by": "тест", "verified_at": "2026-10-09", "research_status": "consolidated"})


async def test_l_changed_parameter_before_result_is_announced_and_recalculated():
    r = Run(snap=demo_snapshot(1))
    await r.fill(PENSIONER)
    assert r.s.release_id == 1
    r.set_snapshot(demo_snapshot(2, _raise_min_pension))  # минимальная пенсия выросла до 1000
    await r.press("review_ok")
    assert "правила обновились" in r.html() and "пересчитан" in r.html()
    assert r.out.result_record["release_id"] == 2 and r.s.release_id == 2
    assert r.statuses()[(MAIN, "pensioner")] == "individual"  # 900 < 1000: по карточке нужен индивидуальный разбор
    assert r.view().release_id == 2


async def test_l_changed_card_with_new_question_asks_before_result():
    r = Run(snap=demo_snapshot(1))
    await r.fill(PENSIONER)
    r.set_snapshot(demo_snapshot(2, _add_insurance_requirement))
    await r.press("review_ok")
    assert "Правила в базе обновились" in r.html()
    assert r.s.state == "asking" and r.s.ui["qid"] == "q_insurance"
    assert r.out.result_record is None  # результата по старым правилам не было
    await r.press("ans", "yes")
    assert r.s.state == "review"
    await r.press("review_ok")
    assert r.out.result_record["release_id"] == 2
    assert r.statuses()[(MAIN, "pensioner")] == "fit"
    assert any(q["id"] == "insurance" for e in r.out.result_record["evaluation"] for q in e["requirements"])


# --------------------------------------------------------------------------- m


async def test_m_scheduled_publication_switches_release_by_time(tmp_path):
    conn = open_db(tmp_path / "kb.db")
    now = utcnow().replace(second=0)
    R.replace_draft(conn, load_seed_bundle(demo=True, today=TODAY), "admin", "demo v1")
    first = R.publish_draft(conn, "admin", now=now)
    second_bundle = demo_snapshot(0, _raise_min_pension).bundle
    R.replace_draft(conn, second_bundle, "admin", "demo v2")
    second = R.publish_draft(conn, "admin", effective_from=now + timedelta(days=1), now=now)
    assert second > first
    snap1 = R.active_snapshot(conn, now)
    snap2 = R.active_snapshot(conn, now + timedelta(days=1))
    assert snap1.release_id == first and snap2.release_id == second

    ids = []
    for snap in (snap1, snap2):
        r = Run(snap=snap)
        await r.result(PENSIONER)
        ids.append((r.out.result_record["release_id"], r.statuses()[(MAIN, "pensioner")]))
    assert ids == [(first, "fit"), (second, "individual")]  # разные выпуски дают разные расчёты
    conn.close()


# --------------------------------------------------------------------------- n


async def test_n_old_result_is_recalculated_on_new_snapshot():
    r = Run(snap=demo_snapshot(1))
    await r.result(PENSIONER)
    old = r.out.result_record
    assert old["release_id"] == 1 and r.s.ui["last_view"]["release_id"] == 1
    r.set_snapshot(demo_snapshot(2, _raise_min_pension))
    await r.command("result")
    new = r.out.result_record
    assert new is not None and new is not old and new["release_id"] == 2
    assert "пересчитан" in new["message_html"] and "пересчитан" in r.html()
    assert r.s.ui["last_view"]["release_id"] == 2  # прежний вид в ui заменён
    assert r.s.release_id == 2 and r.s.state == "result"
    # кнопка «Почему» теперь объясняет новый расчёт
    await r.press("why")
    assert "№2" in r.html()


# --------------------------------------------------------------------------- o


class FakeClient:
    """Клиент OpenRouter-заглушка: chat_json возвращает заданное или бросает ошибку."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    async def chat_json(self, model, messages, schema, name, max_tokens, temperature=0.2):
        self.calls += 1
        b = self.behaviour
        if isinstance(b, Exception):
            raise b
        return (b(messages) if callable(b) else b), None


def _expl(why="Вы получаете пенсию, и это подходит под условия.", intro="Подходит вариант для пенсионеров.",
          ids=("pensioner",)):
    return {"intro": intro, "grounds": [{"id": i, "why": why} for i in ids], "family": ""}


BAD_EXPLANATIONS = {
    "timeout": AiError("timeout", "no answer"),
    "extra_ground": _expl(ids=("pensioner", "digital_nomad")),
    "url": _expl(why="Подробности: https://example.com/guaranteed"),
    "foreign_number": _expl(why="Пенсия 7777 евро в месяц подходит."),
    "promise": _expl(why="Вам гарантированно одобрят ВНЖ."),
    "legal_claim": _expl(why="Виза D вам не нужна, разрешение на работу не требуется."),
    "bare_link": _expl(why="Посетите residence.info"),
    "structure_not_dict": ["не", "объект"],
    "structure_bad_ground": {"intro": "Подходит.", "grounds": [{"id": 5, "why": "x"}], "family": ""},
    "structure_intro_not_str": {"intro": 7, "grounds": [{"id": "pensioner", "why": "x"}], "family": ""},
}


@pytest.fixture
def ledger(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    yield AiLedger(conn, 100.0, {})
    conn.close()


@pytest.mark.parametrize("name", sorted(BAD_EXPLANATIONS))
async def test_o_bad_ai_explanation_falls_back_to_template(name, ledger):
    client = FakeClient(BAD_EXPLANATIONS[name])
    r = Run(explainer=Explainer(client, ledger, model="fake"))
    await r.result(PENSIONER)
    assert client.calls == 1  # модель действительно вызывалась
    assert r.out.result_record["explain_source"] == "template"
    html = r.out.replies[-1].html
    assert "Что стоит проверить первым" in html and PENSIONER_TITLE in html  # результат показан
    for bad in ("example.com", "7777", "гарантированно"):
        assert bad not in html
    assert_safe_html(html)


async def test_o_valid_ai_explanation_is_used(ledger):
    """Контроль: проверки не отбрасывают вообще всё подряд."""
    client = FakeClient(_expl(why="Пенсия 900 евро в месяц у вас есть."))
    r = Run(explainer=Explainer(client, ledger, model="fake"))
    await r.result(PENSIONER)
    assert r.out.result_record["explain_source"] == "llm"
    assert "Пенсия 900 евро в месяц у вас есть." in r.out.replies[-1].html


@pytest.mark.parametrize("name,codes", [
    ("legal_claim", {"legal_term", "negation"}), ("bare_link", {"url"}),
    ("structure_not_dict", {"structure"}), ("structure_bad_ground", {"structure"}),
])
async def test_o_rejection_logs_only_fixed_codes_and_cost_is_recorded(name, codes, ledger, caplog):
    client = FakeClient(BAD_EXPLANATIONS[name])
    r = Run(explainer=Explainer(client, ledger, model="fake"))
    await r.result(PENSIONER)
    rows = ledger._conn.execute("SELECT ok, error FROM ai_calls WHERE kind='explain'").fetchall()
    assert len(rows) == 1 and not rows[0]["ok"]  # вызов учтён
    assert "residence" not in rows[0]["error"] and "Виза" not in rows[0]["error"]
    line = next(r.getMessage() for r in caplog.records if "explain rejected" in r.getMessage())
    assert set(line.split(": ", 1)[1].split(",")) == codes  # в лог идут только коды, без текста модели


def test_o_check_exceptions_inside_check_become_structure():
    from bginfo.ai.explain import check
    payload = {"grounds": [{"id": "pensioner", "title": "Пенсионер"}], "family": []}  # нет met/facts
    assert check({"intro": "ok", "grounds": [{"id": "pensioner", "why": "ok"}], "family": ""}, payload, []) == []
    assert check(None, payload, []) == ["structure"]
    assert check({"intro": "a", "grounds": "x", "family": ""}, payload, []) == ["structure"]


def test_o_legal_word_allowed_when_present_in_input():
    from bginfo.ai.explain import check
    payload = {"grounds": [{"id": "g", "title": "Работа по найму", "status": "s", "met": ["Есть работа"],
                            "unknown": [], "unmet": [], "facts": []}], "family": []}
    ok = {"intro": "Вы работаете.", "grounds": [{"id": "g", "why": "Вы указали работу."}], "family": ""}
    assert check(ok, payload, []) == []
    bad = {"intro": "Нужен документ.", "grounds": [{"id": "g", "why": "Вы указали работу."}], "family": ""}
    assert check(bad, payload, []) == ["legal_term"]
    # слово из чужого основания не разрешает утверждение в пояснении этого основания
    payload["grounds"].append({"id": "h", "title": "Налоги", "status": "s", "met": [], "unknown": [],
                               "unmet": [], "facts": []})
    cross = {"intro": "Вы работаете.", "grounds": [{"id": "g", "why": "Платите налог."},
                                                  {"id": "h", "why": "Вы указали налоги."}], "family": ""}
    assert check(cross, payload, []) == ["legal_term"]


# --------------------------------------------------------------------------- p


async def test_p_all_links_come_from_bundle_sources():
    known = {s.url for s in demo_snapshot().bundle.sources}
    scenarios = [PENSIONER, REMOTE, dict(PENSIONER, income_sources=["none"]), FAMILY_ANSWERS]
    seen: set[str] = set()
    for answers in scenarios:
        r = Run()
        await r.result(answers, strict=False)
        pages = [r.html()]
        for action in ("why", "alts"):
            await r.press(action)
            pages.append(r.html())
        for page in pages:
            links = hrefs(page)
            assert set(links) <= known, set(links) - known
            seen.update(links)
    assert seen  # ссылки вообще были (тест не пустой)


# --------------------------------------------------------------------------- q

EVIL = "<script>alert(1)</script> & Co"


def _evil_profile() -> Profile:
    p = Profile()
    p.set_members([("s1", "spouse", EVIL)])
    for pid, key, val in [(CASE, "who_moves", "self_family"), (CASE, "family_moving", ["spouse"]),
                          (CASE, "has_housing", True), (MAIN, "citizenship_group", ["third"]),
                          (MAIN, "income_sources", ["pension", EVIL]), (MAIN, "pension_amount_eur", 900.0),
                          (MAIN, "pension_doc", True), (MAIN, "ties", ["none"]),
                          ("s1", "citizenship_group", ["third"]), ("s1", "income_sources", ["none"])]:
        p.confirm_value(pid, key, val, "button", None)
    return p


def test_q_user_supplied_strings_are_escaped_in_profile_and_result():
    kb = demo_snapshot().bundle
    p = _evil_profile()
    prof = P.render_profile(p, kb)
    assert "<script" not in prof and "&lt;script&gt;" in prof and "&amp; Co" in prof
    assert_safe_html(prof)

    from bginfo.ai.explain import apply_template

    rv = apply_template(build_result(evaluate_all(kb, p, TODAY, 1), kb, p))
    pages = [P.render_result(rv), P.render_why(rv), P.render_alternatives(rv)]
    assert any("&lt;script&gt;" in page for page in pages)
    for page in pages:
        assert "<script" not in page and "alert(1)</" not in page
        assert_safe_html(page)


async def test_q_username_is_escaped_in_consult_preview():
    r = Run()
    r.u.username = EVIL
    await r.result(PENSIONER)
    await r.press("consult")
    page = r.html()
    assert "<script" not in page and "&lt;script&gt;" in page
    assert_safe_html(page)


async def test_q_free_text_with_markup_does_not_reach_output_unescaped():
    """Текст пользователя, содержащий разметку, после разбора не попадает в сообщения как есть."""
    r = Run(interp=FakeInterp(Interpretation(ok=False, error="timeout")))
    await r.start()
    await r.press("go")
    await r.text("<b>я</b> <script>alert(1)</script> & пенсионер")
    page = r.html()
    assert "<script" not in page and "alert(1)" not in page
    assert_safe_html(page)


def test_template_reason_keeps_case_of_facts():
    from types import SimpleNamespace

    from bginfo.ai.explain import template_reason
    v = SimpleNamespace(facts_used=[("Гражданство", "Не гражданин ЕС"), ("Жильё", "Есть в Болгарии")])
    text = template_reason(v)
    assert text == "Вы указали: Гражданство — Не гражданин ЕС; Жильё — Есть в Болгарии."
    assert "ЕС" in text and "Болгарии" in text
