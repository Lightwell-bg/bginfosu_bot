"""Первичный разбор свободного текста через JEV (OpenRouter System One).

JEV получает только атомарные typed-вопросы: «Указано ли получение пенсии?»,
«Указана ли удалённая работа?» и т.п. Вопросы строятся из ``detect`` у фактов и
вариантов базы знаний. Ответ JEV — распознанное сведение, а не юридический
вывод: всё распознанное записывается как RECOGNIZED и подтверждается
пользователем. JEV ничего не исключает: «явно нет» тоже требует подтверждения,
а варианты, о которых текст молчит, остаются открытыми и проверяются
контрольным вопросом.

Суммы из текста (при наличии цифр) извлекает генеративная модель в строгую
схему. Это тоже только RECOGNIZED.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from bginfo.engine.facts import CASE, MAIN, Profile
from bginfo.kb import model as m
from bginfo.logsafe import safe_exc

log = logging.getLogger(__name__)

MAX_INT = 10  # верхняя граница для целочисленных ответов (число детей)
BGN_PER_EUR = 1.95583  # фиксированный курс конвертации лева в евро

INTENTS = {
    "answer_current": "Сообщение отвечает на текущий вопрос бота.",
    "new_info": "Человек рассказывает о своей ситуации: кто переезжает, гражданство, доходы, работа, семья, жильё.",
    "correction": "Человек исправляет то, что сообщал раньше («нет, не так», «я ошибся», «на самом деле»).",
    "question_to_bot": "Человек задаёт вопрос боту или просит что-то пояснить.",
    "wants_buttons": "Человек передумал писать сам и просит, чтобы бот задавал вопросы.",
    "other": "Сообщение не относится к переезду и проживанию в Болгарии.",
}

PERSON_LABELS = {
    "self": "Да, это сказано об авторе сообщения (или о «нас» вместе с автором).",
    "spouse": "Да, это сказано о супруге автора (муж, жена).",
    "other_member": "Да, это сказано о другом члене семьи (ребёнок, родитель).",
    "explicit_no": "Прямо сказано, что этого нет.",
    "not_mentioned": "Об этом в сообщении не сказано.",
    "contradiction": "Об этом сказано противоречиво.",
}
CASE_LABELS = {
    "yes": "Да, это указано.",
    "explicit_no": "Прямо сказано, что этого нет.",
    "not_mentioned": "Об этом в сообщении не сказано.",
    "contradiction": "Об этом сказано противоречиво.",
}

AMOUNT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["amounts"],
    "properties": {
        "amounts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["fact", "who", "value", "currency"],
                "properties": {
                    "fact": {"type": "string"},
                    "who": {"type": "string", "enum": ["self", "spouse"]},
                    "value": {"type": "number"},
                    "currency": {"type": "string", "enum": ["EUR", "BGN", "OTHER"]},
                },
            },
        }
    },
}


@dataclass
class Recognized:
    role: str  # self | spouse | other_member | case
    key: str
    value: Any
    prob: float
    via: str = "jev"


@dataclass
class Interpretation:
    ok: bool
    intent: str = "new_info"
    intent_conf: float = 0.0
    items: list[Recognized] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)  # ключи фактов: «противоречиво»/спорно — спросить явно
    current_answer: Any = None
    error: str = ""


@dataclass
class _Q:
    qid: str
    kind: str  # bool | option | current
    fact: m.FactDef | None
    option: m.Option | None
    case: bool
    author: bool = False  # да/нет без ролей, относится к основному заявителю


def build_questions(kb: m.Bundle, current: m.Question | None) -> tuple[dict, dict[str, _Q]]:
    questions: dict[str, dict] = {}
    index: dict[str, _Q] = {}
    intents = dict(INTENTS)
    if current is None:
        intents.pop("answer_current")
    else:
        intents["answer_current"] = f"Сообщение отвечает на текущий вопрос бота: «{current.text}»."
    questions["intent"] = {"type": "choice", "instructions": "Что делает автор этим сообщением?", "criteria": intents}

    n = 0
    for fd in kb.facts:
        flat = fd.scope == "case" or fd.detect_author
        labels = CASE_LABELS if flat else PERSON_LABELS
        if fd.type == "bool" and fd.detect:
            n += 1
            qid = f"f{n}"
            questions[qid] = {"type": "choice", "instructions": fd.detect, "criteria": labels}
            index[qid] = _Q(qid, "bool", fd, None, flat, author=fd.detect_author and fd.scope != "case")
        elif fd.type in ("enum", "multi"):
            for opt in fd.options:
                if not opt.detect:
                    continue
                n += 1
                qid = f"f{n}"
                questions[qid] = {"type": "choice", "instructions": opt.detect, "criteria": labels}
                index[qid] = _Q(qid, "option", fd, opt, flat, author=fd.detect_author and fd.scope != "case")

    if current is not None:
        fd = kb.fact(current.fact)
        if fd is not None and fd.type in ("enum", "bool"):
            crit = {o.value: o.label for o in fd.options} if fd.type == "enum" else {
                "yes": "Да", "no": "Нет"}
            crit["unclear"] = "Из сообщения ответ на этот вопрос не ясен."
            questions["current"] = {
                "type": "choice",
                "instructions": f"Какой ответ на вопрос «{current.text}» содержится в сообщении?",
                "criteria": crit,
            }
            index["current"] = _Q("current", "current", fd, None, fd.scope == "case")
    return questions, index


def _best(ans: dict) -> tuple[str, float, dict[str, float]]:
    probs = {k: float(v) for k, v in (ans.get("probabilities") or {}).items()}
    choice = ans.get("choice")
    return choice, float(probs.get(choice, ans.get("confidence") or 0.0)), probs


def map_answers(
    answers: dict, index: dict[str, _Q], recognize_at: float, negate_at: float,
) -> Interpretation:
    """Перевести ответы JEV в распознанные сведения. Чистая функция — тестируется без сети."""
    it = Interpretation(ok=True)
    if isinstance(answers.get("intent"), dict):
        try:
            intent, conf, _ = _best(answers["intent"])
            if isinstance(intent, str):
                it.intent, it.intent_conf = intent, conf
        except (TypeError, ValueError, AttributeError):
            pass  # битый ответ про намерение: остаётся значение по умолчанию

    enum_votes: dict[tuple[str, str], list[tuple[float, str]]] = {}
    multi_add: dict[tuple[str, str], list[str]] = {}
    for qid, q in index.items():
        ans = answers.get(qid)
        if not isinstance(ans, dict):
            continue
        try:
            choice, conf, probs = _best(ans)
        except (TypeError, ValueError, AttributeError):
            continue  # нечисловые или битые вероятности по этому вопросу — пропускаем
        if q.kind == "current":
            if choice != "unclear" and conf >= 0.6:
                it.current_answer = {"yes": True, "no": False}.get(choice, choice) if q.fact.type == "bool" else choice
            continue
        key = q.fact.key
        if probs.get("contradiction", 0.0) >= 0.4:
            it.unclear.append(key)
            continue
        roles = ["case"] if q.case else ["self", "spouse", "other_member"]
        yes_labels = ["yes"] if q.case else roles
        for role in yes_labels:
            p = probs.get(role, 0.0)
            if p < recognize_at:
                continue
            r = ("self" if q.author else "case") if q.case else role
            if q.kind == "bool":
                it.items.append(Recognized(r, key, True, p))
            elif q.fact.type == "multi":
                multi_add.setdefault((r, key), []).append(q.option.value)
            else:
                enum_votes.setdefault((r, key), []).append((p, q.option.value))
        if probs.get("explicit_no", 0.0) >= negate_at and q.kind == "bool":
            it.items.append(Recognized(("self" if q.author else "case") if q.case else "self", key, False,
                                       probs["explicit_no"]))

    for (r, key), vals in multi_add.items():
        it.items.append(Recognized(r, key, sorted(set(vals)), 1.0))
    for (r, key), votes in enum_votes.items():
        votes.sort(reverse=True)
        if len(votes) > 1 and votes[1][0] >= recognize_at and votes[0][0] - votes[1][0] < 0.3:
            it.unclear.append(key)  # два правдоподобных варианта — спросить
        else:
            it.items.append(Recognized(r, key, votes[0][1], votes[0][0]))
    return it


def apply_to_profile(it: Interpretation, profile: Profile, kb: m.Bundle) -> list[Recognized]:
    """Записать распознанное в профиль как RECOGNIZED/CONFLICT.

    Сведения о супруге или другом члене семьи записываются тому участнику, если он
    уже есть в анкете. Иначе возвращаются как «отложенные» и применяются, когда
    участник появится. Для multi-фактов новое значение объединяется с уже известным.
    """
    deferred: list[Recognized] = []
    for r in it.items:
        fd = kb.fact(r.key)
        if fd is None:
            continue
        if r.role == "case":
            pid = CASE
        elif r.role == "self":
            pid = MAIN
        else:
            rel = "spouse" if r.role == "spouse" else None
            cand = [p for p in profile.members() if (rel is None or p.relation == rel)]
            if len(cand) != 1:
                deferred.append(r)
                continue
            pid = cand[0].id
        value = r.value
        if fd.type == "multi":
            cur = profile.get(pid, r.key)
            base = list(cur.value) if cur is not None and isinstance(cur.value, list) else []
            merged = sorted(set(base) | set(value))
            if cur is not None and merged == sorted(base):
                continue  # ничего нового
            # «none» не сочетается с другими вариантами
            if "none" in merged and len(merged) > 1:
                merged = [v for v in merged if v != "none"]
            profile.recognize(pid, r.key, merged, r.via)
            # дополнение подтверждённого списка — не противоречие, а уточнение
            f = profile.get(pid, r.key)
            if f is not None and f.state.value == "conflict" and set(base) <= set(merged):
                from bginfo.engine.facts import Fact, FactState
                profile.facts[profile._k(pid, r.key)] = Fact(pid, r.key, merged, FactState.RECOGNIZED, r.via)
        else:
            profile.recognize(pid, r.key, value, r.via)
    return deferred


# --------------------------------------------------------------------------- deterministic parsing

_YES = {"да", "ага", "есть", "конечно", "yes", "угу", "имеется"}
_NO = {"нет", "неа", "no", "отсутствует", "не имеется"}
_UNKNOWN = {"не знаю", "незнаю", "хз", "не уверен", "не уверена", "затрудняюсь"}
_NUM = re.compile(r"(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(тыс\.?|к)?\s*(€|eur|евро|лв\.?|лев|лева|левов|bgn)?", re.IGNORECASE)


def parse_simple(text: str, fd: m.FactDef) -> tuple[str, Any]:
    """Разобрать ответ на текущий вопрос без AI.

    Возвращает ("ok", value) | ("unknown", None) | ("bad", сообщение) | ("none", None) — не распознано.
    """
    t = text.strip().lower().rstrip(".!")
    if t in _UNKNOWN:
        return "unknown", None
    if fd.type == "bool":
        if t in _YES:
            return "ok", True
        if t in _NO:
            return "ok", False
        return "none", None
    if fd.type == "enum":
        for o in fd.options:
            if t == o.label.lower() or t == o.value:
                return "ok", o.value
        return "none", None
    if fd.type in ("money", "int"):
        mt = _NUM.search(t)
        if not mt or not re.search(r"\d", mt.group(1)):
            return "none", None
        raw = mt.group(1).replace(" ", "").replace(",", ".")
        try:
            val = float(raw)
        except ValueError:
            return "bad", "Не получилось разобрать число. Напишите, например: 850"
        if mt.group(2):
            val *= 1000
        i = mt.start(1)
        if i > 0 and t[i - 1] in "-−" and (i == 1 or not t[i - 2].isalnum()):
            val = -val  # знак минус сразу перед числом
        if val < 0:
            return "bad", "Число не может быть отрицательным."
        if fd.type == "int":
            if val != int(val):
                return "bad", "Напишите целое число."
            if val > MAX_INT:
                if fd.key == "children_count":
                    return "bad", ("Для больших семей нужен индивидуальный разбор — "
                                   "напишите число до 10 или закажите консультацию")
                return "bad", f"Напишите целое число от 0 до {MAX_INT}."
            return "ok", int(val)
        cur = (mt.group(3) or "").lower()
        if cur.startswith(("лв", "лев", "bgn")):
            val = round(val / BGN_PER_EUR, 2)
        elif re.search(r"(руб|₽|\$|usd|долл|грн|₴|тенге|₸|zł|злот)", t):
            return "bad", "Укажите, пожалуйста, сумму в евро (можно приблизительно)."
        if val > 10_000_000:
            return "bad", "Сумма выглядит слишком большой — проверьте, пожалуйста."
        return "ok", round(val, 2)
    if fd.type == "date":
        mt = re.fullmatch(r"(?:(\d{1,2})[./-])?(\d{1,2})[./-](\d{4})", t)
        if not mt:
            return "bad", "Напишите дату в виде ММ.ГГГГ или ДД.ММ.ГГГГ."
        from datetime import date
        try:
            d = date(int(mt.group(3)), int(mt.group(2)), int(mt.group(1) or 1))
        except ValueError:
            return "bad", "Такой даты не существует — проверьте, пожалуйста."
        if not 1900 <= d.year <= 2100:
            return "bad", "Проверьте год."
        return "ok", d.isoformat()
    return "none", None


# --------------------------------------------------------------------------- service


class Interpreter:
    """Вызов JEV и (при цифрах в тексте) извлечение сумм генеративной моделью.

    ``client`` — OpenRouterClient или None (ключа нет — работаем кнопками).
    ``ledger`` — учёт лимитов и стоимости (AiLedger).
    """

    def __init__(self, client, ledger, *, jev_model: str, llm_model: str,
                 recognize_at: float = 0.3, negate_at: float = 0.75):
        self.client = client
        self.ledger = ledger
        self.jev_model = jev_model
        self.llm_model = llm_model
        self.recognize_at = recognize_at
        self.negate_at = negate_at

    async def interpret(self, text: str, kb: m.Bundle, current: m.Question | None, tg_user_id: int | None) -> Interpretation:
        from bginfo.ai.openrouter import AiError

        if self.client is None:
            return Interpretation(ok=False, error="disabled")
        questions, index = build_questions(kb, current)
        try:
            await self.ledger.acheck(tg_user_id, "jev")
        except AiError as e:
            return Interpretation(ok=False, error=e.kind)
        except sqlite3.DatabaseError as e:  # БД учёта занята/повреждена: AI считаем недоступным
            log.error("ai ledger unavailable: %s", safe_exc(e))
            return Interpretation(ok=False, error="unavailable")
        try:
            answers, usage = await self.client.systemone(self.jev_model, {"user_message": text[:4000]}, questions)
        except AiError as e:
            await self._record(tg_user_id, "jev", self.jev_model, e.usage, False, e.kind)
            log.warning("jev failed: %s", e.kind)
            return Interpretation(ok=False, error=e.kind)
        await self._record(tg_user_id, "jev", self.jev_model, usage, True)
        try:
            it = map_answers(answers, index, self.recognize_at, self.negate_at)
            if re.search(r"\d", text) and it.intent in ("new_info", "correction", "answer_current"):
                it.items += await self._amounts(text, kb, tg_user_id)
        except Exception:  # noqa: BLE001 - битый ответ провайдера не должен ронять диалог
            log.warning("interpret: invalid provider answer")
            return Interpretation(ok=False, error="invalid")
        return it

    async def _record(self, tg_user_id: int | None, kind: str, model: str, usage, ok: bool, error: str = "") -> None:
        """Запись в ledger; сбой БД учёта только логируется (вызов к провайдеру уже состоялся)."""
        try:
            await self.ledger.arecord(tg_user_id, kind, model, usage, ok, error)
        except sqlite3.DatabaseError as e:
            log.error("ai ledger record failed: %s", safe_exc(e))

    async def _amounts(self, text: str, kb: m.Bundle, tg_user_id: int | None) -> list[Recognized]:
        from bginfo.ai.openrouter import AiError

        money = {f.key: f.label for f in kb.facts if f.type == "money" and f.scope == "person"}
        if not money:
            return []
        try:
            await self.ledger.acheck(tg_user_id, "extract")
        except (AiError, sqlite3.DatabaseError) as e:
            if not isinstance(e, AiError):
                log.error("ai ledger unavailable: %s", safe_exc(e))
            return []
        sys = (
            "Извлеки из сообщения пользователя только явно названные суммы денег. Не додумывай. "
            "Для каждой суммы выбери поле fact строго из списка: "
            + "; ".join(f"{k} — {v}" for k, v in money.items())
            + ". Если сумма не подходит ни к одному полю, не включай её. who: self — автор, spouse — супруг(а)."
        )
        try:
            data, usage = await self.client.chat_json(
                self.llm_model,
                [{"role": "system", "content": sys}, {"role": "user", "content": text[:2000]}],
                AMOUNT_SCHEMA, "amounts", max_tokens=300, temperature=0,
            )
        except AiError as e:
            await self._record(tg_user_id, "extract", self.llm_model, e.usage, False, e.kind)
            return []
        await self._record(tg_user_id, "extract", self.llm_model, usage, True)
        out = []
        for a in data.get("amounts", []) if isinstance(data, dict) else []:
            try:
                key, who, val, cur = a["fact"], a["who"], float(a["value"]), a["currency"]
            except (KeyError, TypeError, ValueError):
                continue
            if key not in money or who not in ("self", "spouse") or not (0 < val < 10_000_000):
                continue
            if cur == "OTHER":
                continue
            if cur == "BGN":
                val = round(val / BGN_PER_EUR, 2)
            # Сумма должна реально присутствовать в тексте (защита от выдумки моделью).
            digits = re.sub(r"\D", "", text)
            if str(int(a["value"])) not in digits:
                continue
            out.append(Recognized(who, key, round(val, 2), 0.9, via="llm"))
        return out
