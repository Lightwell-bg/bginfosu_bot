"""Небольшая ТЕСТОВАЯ база знаний для unit-тестов движка и версий.

Цифры условные и юридического смысла не имеют.
"""

from __future__ import annotations

from datetime import date

from bginfo.kb import model as m

V = {"verified": True, "verified_at": date(2026, 10, 1), "verified_by": "test", "source_ids": ["src_law"]}


def mini_bundle(status: str = "published") -> m.Bundle:
    return m.Bundle.model_validate({
        "demo": False,
        "sources": [
            {"id": "src_law", "kind": "law", "title": "Тестовый закон", "url": "https://example.org/law"},
            {"id": "bg_pens", "kind": "bginfo", "title": "Пенсионеры", "url": "https://bginfo.eu/test-pens/"},
        ],
        "parameters": [
            {"id": "min_wage", "label": "МРЗ", "unit": "EUR/month", "values": [
                {"value": 600, "valid_from": "2025-01-01", "valid_to": "2025-12-31", "verified": True,
                 "source_ids": ["src_law"]},
                {"value": 620.20, "valid_from": "2026-01-01", "verified": True, "source_ids": ["src_law"]},
            ]},
            {"id": "dn_income", "label": "Доход DN", "unit": "EUR/year", "derived": {"of": "min_wage", "multiplier": 50}},
        ],
        "facts": [
            {"key": "citizenship_group", "scope": "person", "type": "enum", "label": "Гражданство",
             "options": [{"value": "bg", "label": "Болгария"}, {"value": "eu", "label": "ЕС"},
                         {"value": "third", "label": "Другая страна"}]},
            {"key": "income_sources", "scope": "person", "type": "multi", "label": "Доходы",
             "options": [{"value": "pension", "label": "Пенсия"}, {"value": "remote_foreign", "label": "Удалёнка"},
                         {"value": "none", "label": "Нет"}]},
            {"key": "pension_amount_eur", "scope": "person", "type": "money", "label": "Пенсия, €/мес",
             "depends_on": ["income_sources"]},
            {"key": "annual_income_eur", "scope": "person", "type": "money", "label": "Доход, €/год",
             "depends_on": ["income_sources"]},
            {"key": "has_housing", "scope": "case", "type": "bool", "label": "Жильё"},
            {"key": "status_in_bg", "scope": "person", "type": "enum", "label": "Статус",
             "options": [{"value": "none", "label": "Нет"}, {"value": "long_term", "label": "ВНЖ"}]},
            {"key": "relation_doc", "scope": "person", "type": "bool", "label": "Документ о браке"},
        ],
        "questions": [
            {"id": "q_cit", "fact": "citizenship_group", "text": "Гражданство?", "core": True, "priority": 1,
             "ask_for": ["main", "member"]},
            {"id": "q_inc", "fact": "income_sources", "text": "Доходы?", "core": True, "priority": 2},
            {"id": "q_pens", "fact": "pension_amount_eur", "text": "Размер пенсии?", "priority": 10,
             "ask_if": {"op": "in", "fact": "self.income_sources", "values": ["pension"]}},
            {"id": "q_annual", "fact": "annual_income_eur", "text": "Доход за год?", "priority": 11,
             "ask_if": {"op": "in", "fact": "self.income_sources", "values": ["remote_foreign"]}},
            {"id": "q_house", "fact": "has_housing", "text": "Жильё есть?", "priority": 20},
            {"id": "q_status", "fact": "status_in_bg", "text": "Статус в Болгарии?", "priority": 5},
            {"id": "q_reldoc", "fact": "relation_doc", "text": "Брак зарегистрирован?", "priority": 30,
             "ask_for": ["member"]},
        ],
        "grounds": [
            {"id": "pensioner", "title": "Пенсионер", "regime": "third_country", "status": status,
             "checked_at": "2026-10-01", "checked_by": "test", "bginfo_source_ids": ["bg_pens"],
             "summary": "Пенсия не ниже {param:min_wage} € в месяц.",
             "applies_if": {"op": "eq", "fact": "self.citizenship_group", "value": "third"},
             "requirements": [
                 {"id": "has_pension", "label": "Получает пенсию",
                  "cond": {"op": "in", "fact": "self.income_sources", "values": ["pension"]}, **V},
                 {"id": "pension_amount", "label": "Пенсия не ниже МРЗ",
                  "cond": {"op": "cmp", "fact": "self.pension_amount_eur", "cmp": ">=", "param": "min_wage"},
                  "on_fail": "individual_review", **V},
                 {"id": "housing", "label": "Есть жильё",
                  "cond": {"op": "known_true", "fact": "case.has_housing"}, "on_fail": "possible_after", **V},
             ]},
            {"id": "nomad", "title": "Digital nomad", "regime": "third_country", "status": status,
             "checked_at": "2026-10-01", "checked_by": "test",
             "applies_if": {"op": "eq", "fact": "self.citizenship_group", "value": "third"},
             "requirements": [
                 {"id": "remote", "label": "Удалённая работа на иностранных клиентов",
                  "cond": {"op": "in", "fact": "self.income_sources", "values": ["remote_foreign"]}, **V},
                 {"id": "income", "label": "Доход не ниже порога",
                  "cond": {"op": "cmp", "fact": "self.annual_income_eur", "cmp": ">=", "param": "dn_income"}, **V},
             ]},
            {"id": "family", "title": "Член семьи", "regime": "family_third_country", "subject": "family_member",
             "status": status, "checked_at": "2026-10-01", "checked_by": "test",
             "requirements": [
                 {"id": "sponsor", "label": "У заявителя есть основание",
                  "cond": {"op": "sponsor_basis", "statuses": ["long_term"]}, **V},
                 {"id": "doc", "label": "Родство подтверждено документом",
                  "cond": {"op": "known_true", "fact": "self.relation_doc"}, "on_fail": "possible_after", **V},
             ]},
        ],
    })
