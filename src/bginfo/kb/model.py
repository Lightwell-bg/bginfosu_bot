"""Модель базы знаний (KB bundle).

Весь опубликованный контент живёт в одном неизменяемом «выпуске» (release):
основания, общие показатели, каталог фактов, вопросы и источники вместе.
Так публикация атомарна: бот всегда видит согласованный набор.

Тексты могут содержать плейсхолдеры ``{param:<id>}``. Значения подставляет код
из общих показателей, поэтому суммы не дублируются в текстах.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

ID_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
FACT_REF_PATTERN = r"^(self|sponsor|case)\.[a-z][a-z0-9_]{1,63}$"

Id = Annotated[str, Field(pattern=ID_PATTERN)]
FactRef = Annotated[str, Field(pattern=FACT_REF_PATTERN)]
Scalar = bool | int | float | str


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- conditions
# Ограниченный набор типизированных условий. Никакого eval: каждое условие
# интерпретирует engine.evaluate.


class CondEq(_Strict):
    op: Literal["eq"] = "eq"
    fact: FactRef
    value: Scalar


class CondIn(_Strict):
    """Значение факта входит в множество (для multi-фактов: есть пересечение)."""

    op: Literal["in"] = "in"
    fact: FactRef
    values: list[Scalar] = Field(min_length=1)


class CondCmp(_Strict):
    """Числовое сравнение факта с числом или общим показателем (с множителем)."""

    op: Literal["cmp"] = "cmp"
    fact: FactRef
    cmp: Literal[">=", ">", "<=", "<", "=="]
    value: float | None = None
    param: Id | None = None
    multiplier: float = 1.0


class CondPeriod(_Strict):
    """Дата из факта сравнивается с датой расчёта, сдвинутой на N месяцев.

    Например «договор действует ещё не менее 12 месяцев»:
    ``{op: period, fact: self.contract_end, cmp: ">=", months: 12}``.
    """

    op: Literal["period"] = "period"
    fact: FactRef
    cmp: Literal[">=", "<="]
    months: int | None = None
    param: Id | None = None


class CondKnownTrue(_Strict):
    """Наличие подтверждённого факта: факт подтверждён и истинен."""

    op: Literal["known_true"] = "known_true"
    fact: FactRef


class CondSponsorBasis(_Strict):
    """Для членов семьи: у основного заявителя есть или может появиться основание.

    True — у него уже есть статус из списка или хотя бы одно основание
    «предварительно подходит»; Unknown — есть основания «нужно уточнить»;
    False — ничего из этого.
    """

    op: Literal["sponsor_basis"] = "sponsor_basis"
    statuses: list[str] = Field(default_factory=list, description="значения sponsor.status_in_bg, которые засчитываются")


class CondAnd(_Strict):
    op: Literal["and"] = "and"
    all: list[Condition] = Field(min_length=1)


class CondOr(_Strict):
    op: Literal["or"] = "or"
    any: list[Condition] = Field(min_length=1)


class CondNot(_Strict):
    op: Literal["not"] = "not"
    cond: Condition


Condition = Annotated[
    CondEq | CondIn | CondCmp | CondPeriod | CondKnownTrue | CondSponsorBasis | CondAnd | CondOr | CondNot,
    Field(discriminator="op"),
]

for _m in (CondAnd, CondOr, CondNot):
    _m.model_rebuild()


# --------------------------------------------------------------------------- sources & params

SourceKind = Literal["bginfo", "law", "official_publication", "official_procedure", "consolidated_text", "practice"]


class Source(_Strict):
    id: Id
    kind: SourceKind
    title: str
    url: str = Field(pattern=r"^https://")
    note: str = ""


class ParamValue(_Strict):
    value: float
    valid_from: date
    valid_to: date | None = None
    source_ids: list[Id] = Field(default_factory=list)
    verified: bool = False
    research_status: ResearchStatus = ""
    note: str = ""


class ParamDerived(_Strict):
    """Производный показатель: произведение множителя и другого показателя."""

    of: Id
    multiplier: float


class Parameter(_Strict):
    id: Id
    label: str
    unit: Literal["EUR", "EUR/month", "EUR/year", "months", "years", "count"]
    values: list[ParamValue] = Field(default_factory=list)
    derived: ParamDerived | None = None
    note: str = ""


# --------------------------------------------------------------------------- facts & questions

FactType = Literal["bool", "enum", "multi", "money", "int", "date"]


class Option(_Strict):
    value: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    label: str
    # Атомарный вопрос для JEV: «Указано ли …?». Пусто — вариант из текста не распознаётся.
    detect: str = ""


class FactDef(_Strict):
    """Определение факта.

    ``scope=person`` — факт есть у каждого участника (гражданство, пенсия);
    ``scope=case`` — один на анкету (цель, жильё).
    ``depends_on`` — ключи фактов (без префикса роли), при изменении которых
    этот факт перестаёт быть достоверным и запрашивается заново.
    """

    key: Id
    scope: Literal["person", "case"]
    type: FactType
    label: str
    options: list[Option] = Field(default_factory=list)
    depends_on: list[Id] = Field(default_factory=list)
    essential: bool = True
    # Для bool-фактов: атомарный вопрос JEV («Указано ли, что …?»). Для enum/multi
    # используются detect у вариантов.
    detect: str = ""
    # Как показывать значение в анкете (для money: «€ в месяц» и т.п.).
    unit_label: str = ""
    # Значение выставляет код (например, родство участника), вопрос не нужен.
    derived: bool = False
    # Факт описывает ситуацию автора («супруг — гражданин Болгарии»): JEV спрашивается без ролей,
    # результат относится к основному заявителю.
    detect_author: bool = False


class Question(_Strict):
    id: Id
    fact: Id
    text: str
    # Формулировка для члена семьи; {who} заменяется на «супруга(и)», «ребёнка 1» и т.п.
    text_member: str = ""
    help: str = ""
    ask_if: Condition | None = None
    # Кому задаётся person-вопрос: main — основному заявителю, member — членам семьи.
    ask_for: list[Literal["main", "member"]] = Field(default_factory=lambda: ["main"])
    # Каким членам семьи задавать (если ask_for содержит member).
    member_relations: list[Literal["spouse", "child", "parent", "other"]] = Field(
        default_factory=lambda: ["spouse", "child", "parent", "other"])
    priority: int = 50
    core: bool = False
    allow_unknown: bool = True


# --------------------------------------------------------------------------- grounds

GroundStatus = Literal["draft", "published", "needs_review", "disabled"]
Regime = Literal["third_country", "eu", "family_third_country", "family_eu", "bg_origin"]
OnFail = Literal["not_eligible", "possible_after", "individual_review"]
ReqKind = Literal["legal", "procedure", "practice", "individual"]
# official_publication — сверено с Държавен вестник; consolidated — найдено в консолидированном
# тексте (Ciela/Lex), ДВ не открывался; bginfo_only — только BGINFO; unconfirmed — не подтверждено.
ResearchStatus = Literal["", "official_publication", "consolidated", "bginfo_only", "unconfirmed"]


class Requirement(_Strict):
    id: Id
    label: str
    cond: Condition
    kind: ReqKind = "legal"
    stage: Literal["visa_d", "permit", "renewal", "any"] = "any"
    on_fail: OnFail = "not_eligible"
    fail_text: str = ""
    unknown_text: str = ""
    source_ids: list[Id] = Field(default_factory=list)
    verified: bool = False
    verified_at: date | None = None
    verified_by: str = ""
    disputed: bool = False
    dispute_reason: str = ""
    # Итог первоначального исследования (не одобрение!). Одобрение — verified/verified_by.
    research_status: ResearchStatus = ""
    research_note: str = ""


class Unsupported(_Strict):
    cond: Condition
    reason: str


class Note(_Strict):
    text: str
    scope: str = ""
    kind: ReqKind = "practice"


class Ground(_Strict):
    id: Id
    title: str
    regime: Regime
    subject: Literal["applicant", "family_member"] = "applicant"
    status: GroundStatus = "draft"
    review_reason: str = ""
    version: int = 1
    valid_from: date | None = None
    valid_to: date | None = None
    applies_if: Condition | None = None
    unsupported: list[Unsupported] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    summary: str = ""
    restrictions: list[Note] = Field(default_factory=list)
    practice_notes: list[Note] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    family_note: str = ""
    goals: list[str] = Field(default_factory=list)
    bginfo_source_ids: list[Id] = Field(default_factory=list)
    checked_at: date | None = None
    checked_by: str = ""
    research_note: str = ""
    demo: bool = False


class Bundle(_Strict):
    schema_version: int = SCHEMA_VERSION
    grounds: list[Ground] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)
    facts: list[FactDef] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    demo: bool = False

    # Индексы строятся лениво; Bundle неизменяем по соглашению (выпуски не редактируются).
    def ground(self, gid: str) -> Ground | None:
        return next((g for g in self.grounds if g.id == gid), None)

    def param(self, pid: str) -> Parameter | None:
        return next((p for p in self.parameters if p.id == pid), None)

    def fact(self, key: str) -> FactDef | None:
        return next((f for f in self.facts if f.key == key), None)

    def source(self, sid: str) -> Source | None:
        return next((s for s in self.sources if s.id == sid), None)

    def active_grounds(self) -> list[Ground]:
        """Основания, участвующие в подборе: опубликованные и требующие проверки."""
        return [g for g in self.grounds if g.status in ("published", "needs_review")]
