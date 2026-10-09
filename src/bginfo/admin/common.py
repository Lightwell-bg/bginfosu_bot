"""Общие части админки: состояние приложения, подключение к БД, шаблоны, подписи."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from bginfo.config import AppConfig, Settings
from bginfo.db.conn import connect

PKG_DIR = Path(__file__).parent

GROUND_STATUS_RU = {
    "draft": "черновик (бот его не видит)", "published": "опубликована", "needs_review": "требует проверки",
    "disabled": "отключена",
}
# Цветные плашки статуса основания: (подпись, css-класс)
GROUND_BADGE = {
    "published": ("Опубликовано", "ok"), "needs_review": ("Требует проверки", "warn"),
    "draft": ("Черновик", "gray"), "disabled": ("Отключено", "bad"),
}
REGIME_RU = {
    "third_country": "гражданин третьей страны", "eu": "гражданин ЕС", "family_third_country": "семья (третья страна)",
    "family_eu": "семья (ЕС)", "bg_origin": "болгарское происхождение",
}
KIND_RU = {"ground": "основание", "parameter": "цифра или сумма", "fact": "факт", "question": "вопрос", "source": "источник"}
RELEASE_KIND_RU = {"publish": "публикация", "rollback": "откат", "review_flag": "пометка «требует проверки»", "demo": "демо"}
RESEARCH_STATUS_RU = {
    "": "— не указано —",
    "official_publication": "сверено с ДВ",
    "consolidated": "консолидированный текст, ДВ не открыт",
    "bginfo_only": "только BGINFO",
    "unconfirmed": "не подтверждено",
}
RESEARCH_HINT = ("Итог первоначального исследования, не одобрение. Одобрение — галочка «подтверждено» "
                 "плюс автор и дата.")
FIELD_RU = {
    "id": "ID", "key": "ключ", "title": "название", "regime": "режим", "subject": "субъект", "status": "статус",
    "review_reason": "причина проверки", "valid_from": "действует с", "valid_to": "действует по",
    "summary": "кратко", "family_note": "заметка о семье", "goals": "цели", "checked_at": "дата проверки",
    "checked_by": "кто проверил", "steps": "шаги", "documents": "документы", "restrictions": "ограничения",
    "practice_notes": "практические замечания", "bginfo_source_ids": "страницы BGINFO",
    "requirements": "требование", "unsupported": "неподдерживаемое условие", "applies_if": "условие применимости",
    "cond": "условие", "label": "подпись", "kind": "тип", "stage": "этап", "on_fail": "если не выполнено",
    "fail_text": "текст при невыполнении", "unknown_text": "текст при неизвестном", "source_ids": "источники",
    "verified": "подтверждено", "verified_at": "дата подтверждения", "verified_by": "кем подтверждено",
    "disputed": "спорно", "dispute_reason": "причина спора", "text": "текст", "scope": "область",
    "reason": "причина", "research_status": "итог исследования", "research_note": "заметка исследования",
    "unit": "единица", "values": "значения", "value": "значение", "derived": "формула", "of": "от показателя",
    "multiplier": "множитель", "note": "заметка", "options": "варианты", "depends_on": "зависит от",
    "essential": "обязательный", "detect": "вопрос для JEV", "unit_label": "подпись единицы", "fact": "факт",
    "text_member": "текст для члена семьи", "help": "подсказка", "ask_if": "задавать, если", "ask_for": "кому",
    "priority": "приоритет", "core": "основной", "allow_unknown": "можно «не знаю»", "url": "ссылка",
    "type": "тип", "param": "цифра", "months": "месяцев", "cmp": "оператор", "values_": "значения",
    "statuses": "статусы", "all": "все из", "any": "любое из", "version": "версия", "demo": "демо",
    "parameters": "цифры и суммы", "facts": "факты", "questions": "вопросы", "sources": "источники",
    "grounds": "основания",
}


CHOICES = {
    "req_kind": {"legal": "закон", "procedure": "процедура", "practice": "практика", "individual": "индивидуальный случай"},
    "stage": {"any": "любой", "visa_d": "виза D", "permit": "разрешение", "renewal": "продление"},
    "on_fail": {"not_eligible": "не подходит", "possible_after": "возможно после выполнения",
                "individual_review": "нужен индивидуальный разбор"},
    "subject": {"applicant": "заявитель", "family_member": "член семьи"},
    "unit": {"EUR": "евро", "EUR/month": "евро в месяц", "EUR/year": "евро в год", "months": "месяцы",
             "years": "годы", "count": "штуки"},
    "scope": {"person": "участник (у каждого свой)", "case": "анкета (один на всех)"},
    "fact_type": {"bool": "да / нет", "enum": "выбор одного", "multi": "выбор нескольких", "money": "сумма",
                  "int": "целое число", "date": "дата"},
    "source_kind": {"bginfo": "страница BGINFO", "law": "закон", "official_publication": "официальная публикация",
                    "official_procedure": "официальная процедура", "consolidated_text": "консолидированный текст",
                    "practice": "практика"},
    "ask_for": {"main": "основной заявитель", "member": "член семьи"},
}


class AdminConfigError(RuntimeError):
    """Админку нельзя запускать с текущими настройками."""


@dataclass
class AdminState:
    settings: Settings
    config: AppConfig
    db_path: str
    env: jinja2.Environment
    tz: ZoneInfo
    password_hash: str = ""  # argon2-хеш ADMIN_PASSWORD, считается при старте


def get_state(request: Request) -> AdminState:
    return request.app.state.admin


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Своё подключение на запрос: SQLite-соединение нельзя делить между потоками запросов."""
    conn = connect(get_state(request).db_path)
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- templates


def _local_dt(tz: ZoneInfo):
    def f(value: str | None) -> str:
        if not value:
            return "—"
        try:
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            return str(value)
        return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")

    return f


def _fmt_val(v) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def make_env(tz: ZoneInfo) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(PKG_DIR / "templates")),
        autoescape=True,  # весь пользовательский текст экранируется
        trim_blocks=True, lstrip_blocks=True,
    )
    from bginfo.admin import conditions

    env.filters["local_dt"] = _local_dt(tz)
    env.filters["local_date"] = lambda v: _local_dt(tz)(v).split(" ")[0]
    env.filters["fmt_val"] = _fmt_val
    env.filters["field_ru"] = lambda k: FIELD_RU.get(k, k)
    env.filters["ground_status"] = lambda s: GROUND_STATUS_RU.get(s, s)
    env.filters["badge"] = lambda st: GROUND_BADGE.get(st, (st, "gray"))
    env.filters["regime_ru"] = lambda s: REGIME_RU.get(s, s)
    env.filters["kind_ru"] = lambda s: KIND_RU.get(s, s)
    env.filters["release_kind"] = lambda s: RELEASE_KIND_RU.get(s, s)
    env.globals["CHOICES"] = CHOICES
    env.globals["LEAF_TYPES"] = conditions.LEAF_TYPES
    env.globals["RESEARCH_STATUS_RU"] = RESEARCH_STATUS_RU
    env.globals["RESEARCH_HINT"] = RESEARCH_HINT
    env.globals["GROUND_STATUS_RU"] = GROUND_STATUS_RU
    env.globals["REGIME_RU"] = REGIME_RU
    env.globals["cond_ctx"] = conditions.cond_ctx
    return env


def flash(request: Request, message: str, level: str = "ok") -> None:
    request.session["flash"] = [*request.session.get("flash", []), [level, message]]


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    st = get_state(request)
    sess = request.session
    flashes = sess.pop("flash", [])
    base = {
        "request": request, "user": sess.get("u"), "csrf": sess.get("csrf", ""), "flashes": flashes,
        "app_mode": st.settings.APP_MODE, "tz_name": str(st.tz),
    }
    html = st.env.get_template(name).render(**base, **ctx)
    resp = HTMLResponse(html, status_code=status_code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
