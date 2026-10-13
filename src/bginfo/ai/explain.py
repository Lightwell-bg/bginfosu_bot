"""Персональное пояснение результата генеративной моделью (через OpenRouter).

Модель получает только компактный профиль (факты, использованные в расчёте),
статусы, рассчитанные кодом, и названия выполненных/неизвестных условий.
Она пишет короткое вступление и по одному предложению «почему» для каждого
показанного основания, со ссылкой на ответы человека.

Модель НЕ может:
- менять статусы и добавлять основания (ID проверяются);
- добавлять суммы, сроки и ссылки (числа и URL проверяются);
- обещать результат (стоп-фразы).
Структурно валидный JSON ещё не значит правильный, поэтому при любом нарушении
вся персонализация отбрасывается и используется проверенный шаблон.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

from bginfo.engine.result import GroundView, ResultView
from bginfo.logsafe import safe_exc

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intro", "grounds", "family"],
    "properties": {
        "intro": {"type": "string"},
        "grounds": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "why"],
                "properties": {"id": {"type": "string"}, "why": {"type": "string"}},
            },
        },
        "family": {"type": "string"},
    },
}

SYSTEM_PROMPT = """Ты помогаешь объяснить человеку результат предварительного подбора оснований для проживания в Болгарии.
Решение уже принято программой по проверенным правилам. Твоя задача — только персонально пересказать его простыми словами.

Правила:
- Пиши по-русски, просто, на «вы», без канцелярита.
- intro: 1–2 предложения о том, что получилось, без перечисления всех вариантов.
- grounds: для КАЖДОГО переданного основания (id без изменений) одно-два предложения «почему», опираясь только на ответы человека (facts) и выполненные/невыясненные условия (met/unknown).
- family: 1–2 предложения о членах семьи, если они есть во входных данных; иначе пустая строка.
- Не меняй статусы и не спорь с ними. Не добавляй других оснований.
- Не пиши никаких сумм, процентов, сроков, дат и чисел, кроме тех, что есть во входных данных.
- Не добавляй требований, документов, сроков и ссылок, которых нет во входных данных.
- Не обещай результат: никаких «гарантированно», «точно дадут», «одобрят».
- Без HTML и markdown."""

BANNED = re.compile(
    r"(гарант|точно (?:да|получ|одобр)|обязательно (?:да|одобр)|одобрят|100\s*%|без проблем|наверняка|стопроцентн)",
    re.IGNORECASE,
)
URLISH = re.compile(r"(https?://|www\.|\.(?:eu|bg|com|org|net|ru)\b|t\.me/)", re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:[ .,]\d+)*")
MAX_LEN = 450


def _numbers(s: str) -> set[str]:
    return {re.sub(r"\D", "", n) for n in NUMBER.findall(s or "") if re.sub(r"\D", "", n)}


def build_payload(rv: ResultView, profile_lines: list[tuple[str, str]]) -> dict:
    def g(v: GroundView) -> dict:
        return {
            "id": v.ground_id, "title": v.title, "status": v.status_label,
            "met": v.met, "unknown": [x[0] for x in v.unknown], "unmet": [x[0] for x in v.unmet],
            "facts": [f"{a}: {b}" for a, b in v.facts_used],
        }

    shown = ([rv.primary] if rv.primary else []) + rv.alternatives[:3]
    return {
        "profile": [f"{a}: {b}" for a, b in profile_lines],
        "grounds": [g(v) for v in shown],
        "family": [
            {"who": mv.label, "options": [{"title": x.title, "status": x.status_label} for x in mv.grounds]}
            for mv in rv.family
        ],
    }


LEGAL_STEMS = (
    "виз", "разрешени", "внж", "пмж", "работ", "налог", "срок", "документ", "страхов", "продлен",
    "гражданств", "консульств", "миграци", "заявлени", "отказ", "одобр", "нужн", "требу", "обязат",
    "запрещ", "можно", "нельзя", "должн",
)
LEGAL_RES = [(st, re.compile(r"(?<!\w)" + st)) for st in LEGAL_STEMS]
NEGATION = re.compile(r"(не\s+(?:нужн|требу|обязат)|без\s+(?:виз|разрешени|документ)|можно\s+не)", re.IGNORECASE)
LINK_TOKEN = re.compile(r"(?<![\w-])[\w-]+\.[a-zа-я]{2,}(?!\w)")


def _structure_ok(data) -> bool:
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("intro"), str) or not isinstance(data.get("family"), str):
        return False
    gr = data.get("grounds")
    if not isinstance(gr, list):
        return False
    return all(isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("why"), str) for x in gr)


def _check_text(t: str, context: str, allowed_numbers: set[str], shown_titles: set[str],
                all_titles: list[str]) -> set[str]:
    """Коды нарушений одного текста. Сырые значения из ответа модели в коды не попадают."""
    codes: set[str] = set()
    if len(t) > MAX_LEN:
        codes.add("length")
    if URLISH.search(t) or LINK_TOKEN.search(t) or "@" in t:
        codes.add("url")
    if "<" in t or ">" in t:
        codes.add("markup")
    if BANNED.search(t):
        codes.add("promise")
    if _numbers(t) - allowed_numbers:
        codes.add("numbers")
    low = t.lower()
    if NEGATION.search(low):
        codes.add("negation")
    ctx = context.lower()
    for _stem, rx in LEGAL_RES:
        if rx.search(low) and not rx.search(ctx):
            codes.add("legal_term")
    for title in all_titles:
        if title.lower() not in shown_titles and len(title) > 5 and title.lower() in low:
            codes.add("ids")
    return codes


def check(data: dict, payload: dict, all_titles: list[str]) -> list[str]:
    """Программная проверка ответа модели. Пустой список — можно показывать.

    Возвращает только фиксированные коды (legal_term, negation, url, numbers, ids, length,
    markup, promise, structure) без значений из ответа модели.
    """
    try:
        if not _structure_ok(data):
            return ["structure"]
        codes: set[str] = set()
        allowed_ids = [g["id"] for g in payload["grounds"]]
        got_ids = [x["id"] for x in data["grounds"]]
        if sorted(got_ids) != sorted(allowed_ids):
            codes.add("ids")
        allowed_numbers = _numbers(json.dumps(payload, ensure_ascii=False))
        shown_titles = {g["title"].lower() for g in payload["grounds"]}
        for mv in payload["family"]:
            shown_titles |= {o["title"].lower() for o in mv["options"]}
        whole = json.dumps(payload, ensure_ascii=False)
        by_id = {g["id"]: json.dumps(g, ensure_ascii=False) for g in payload["grounds"]}
        texts = [(data["intro"], whole), (data["family"], whole)]
        for x in data["grounds"]:
            texts.append((x["why"], by_id.get(x["id"], "")))
            if not x["why"].strip():
                codes.add("structure")
        for t, ctx in texts:
            codes |= _check_text(t, ctx, allowed_numbers, shown_titles, all_titles)
        if not payload["family"] and data["family"]:
            codes.add("structure")
        return sorted(codes)
    except Exception:  # noqa: BLE001 - любая ошибка проверки = отклонить ответ
        return ["structure"]


def template_reason(v: GroundView) -> str:
    if not v.facts_used:
        return ""
    return "Вы указали: " + "; ".join(f"{a} — {b}" for a, b in v.facts_used[:5]) + "."


def apply_template(rv: ResultView) -> ResultView:
    rv.explain_source = "template"
    rv.intro = ""
    rv.family_intro = ""
    for v in ([rv.primary] if rv.primary else []) + rv.alternatives:
        v.personal = template_reason(v)
    return rv


class Explainer:
    def __init__(self, client, ledger, *, model: str, max_tokens: int = 900):
        self.client = client
        self.ledger = ledger
        self.model = model
        self.max_tokens = max_tokens

    async def _record(self, tg_user_id: int | None, kind: str, usage, ok: bool, error: str = "") -> None:
        """Запись в ledger; сбой БД учёта только логируется (вызов к провайдеру уже состоялся)."""
        try:
            await self.ledger.arecord(tg_user_id, kind, self.model, usage, ok, error)
        except sqlite3.DatabaseError as e:
            log.error("ai ledger record failed: %s", safe_exc(e))

    async def explain(self, rv: ResultView, profile_lines: list[tuple[str, str]], all_titles: list[str],
                      tg_user_id: int | None) -> ResultView:
        """Дописать персональные пояснения; при любой проблеме — проверенный шаблон."""
        from bginfo.ai.openrouter import AiError

        apply_template(rv)
        if self.client is None or rv.kind not in ("ok", "individual", "clarify") or not (rv.primary or rv.alternatives):
            return rv
        payload = build_payload(rv, profile_lines)
        try:
            await self.ledger.acheck(tg_user_id, "explain")
        except AiError:
            return rv
        except sqlite3.DatabaseError as e:  # БД учёта занята/повреждена: проверенный шаблон
            log.error("ai ledger unavailable: %s", safe_exc(e))
            return rv
        try:
            data, usage = await self.client.chat_json(
                self.model,
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                SCHEMA, "explanation", max_tokens=self.max_tokens, temperature=0.2,
            )
        except AiError as e:
            await self._record(tg_user_id, "explain", e.usage, False, e.kind)
            log.warning("explain failed: %s", e.kind)
            return rv
        try:
            problems = check(data, payload, all_titles)
        except Exception:  # noqa: BLE001
            problems = ["structure"]
        codes = ",".join(problems)[:100]
        await self._record(tg_user_id, "explain", usage, not problems, codes)
        if problems:
            log.warning("explain rejected: %s", codes)
            return rv
        why = {x["id"]: x["why"].strip() for x in data["grounds"]}
        for v in ([rv.primary] if rv.primary else []) + rv.alternatives:
            if v.ground_id in why:
                v.personal = why[v.ground_id]
        rv.intro = data["intro"].strip()
        rv.family_intro = data.get("family", "").strip()
        rv.explain_source = "llm"
        return rv
