"""Тексты для Telegram (parse_mode=HTML).

Любой текст из базы, от пользователя или от модели проходит через ``h()``.
Разметку добавляет только этот модуль, поэтому теги всегда сбалансированы.
"""

from __future__ import annotations

import html
from dataclasses import fields
from datetime import date

from bginfo.engine.evaluate import Status
from bginfo.engine.facts import CASE, FactState, Profile
from bginfo.engine.result import GroundView, Link, MemberView, ResultView, display_value
from bginfo.kb import model as m

DISCLAIMER = ("ℹ️ Это предварительный подбор по сообщённым вами данным, а не гарантия решения ведомства. "
              "Требования могут меняться — перед подачей сверяйтесь с актуальными источниками.")
DEMO_BANNER = "🧪 <b>ДЕМО-база.</b> Тестовые карточки, не для реальных решений.\n\n"
STATUS_ICON = {Status.FIT: "✅", Status.POSSIBLE_AFTER: "🟡", Status.CLARIFY: "❔",
               Status.INDIVIDUAL: "🧑‍⚖️", Status.NOT_ELIGIBLE: "⛔"}

WELCOME = (
    "🇧🇬 Найдём возможные основания для вашего проживания в Болгарии.\n"
    "Я задам вопросы о вашей ситуации и покажу:\n"
    "✅ какие основания могут подойти;\n"
    "📋 какие условия нужно выполнить;\n"
    "🔎 что необходимо уточнить;\n"
    "➡️ с чего начать оформление.\n"
    "Отвечайте кнопками или своими словами. Точность результата зависит от ваших ответов.\n"
    "Если потребуется индивидуальный разбор, можно заказать консультацию BGINFO.\n"
    "👇 Начнём с анкеты.\n"
    "💬 Канал о жизни в Болгарии:\n"
    "https://t.me/bginfosu\n"
    "📰 Ежедневные новости Болгарии:\n"
    "https://t.me/bginfo_bgnews\n"
    'Виртуальный помощник компании "Информация о Болгарии" BGINFO.EU\n'
    "https://t.me/bginfosuai_bot\n"
    "📬 Подписаться на обновления BGINFO:\n"
    "https://bginfo.eu/subscribe/"
)
AI_NOTICE = ("<i>Если пишете своими словами, текст кратко анализируется AI-сервисом (через OpenRouter), чтобы "
             "понять вашу ситуацию. Не присылайте номера паспортов, сканы и контакты. Удалить анкету: /delete.</i>")
UNAVAILABLE = ("Сервис подбора сейчас готовится: база проверенных правил ещё не опубликована. "
               "Пожалуйста, загляните немного позже.")


def h(s: object) -> str:
    return html.escape(str(s), quote=False)


def link(lk: Link) -> str:
    return f'<a href="{html.escape(lk.url, quote=True)}">{h(lk.title)}</a>'


def who_genitive(label: str, relation: str) -> str:
    return {"spouse": "супруга (супруги)", "parent": "родителя", "other": "родственника"}.get(
        relation, label.lower())


def question_text(q: m.Question, profile: Profile, pid: str, render=lambda t: t) -> str:
    """Текст вопроса; ``render`` подставляет {param:…} из общих показателей."""
    p = profile.participants.get(pid)
    if p is None or p.relation == "main" or pid == CASE:
        return h(render(q.text))
    if q.text_member:
        return f"<b>{h(p.label)}.</b> " + h(render(q.text_member.replace("{who}", who_genitive(p.label, p.relation))))
    return f"<b>{h(p.label)}.</b> " + h(render(q.text))


def profile_lines(profile: Profile, kb: m.Bundle) -> list[tuple[str, str, str, str]]:
    """(участник, ключ, подпись, значение) по всем подтверждённым и «не знаю» фактам."""
    out = []
    order = {f.key: i for i, f in enumerate(kb.facts)}
    for f in sorted(profile.facts.values(), key=lambda f: (f.participant != CASE, f.participant, order.get(f.key, 999))):
        fd = kb.fact(f.key)
        if fd is None or f.state not in (FactState.CONFIRMED, FactState.UNKNOWN):
            continue
        val = "не знаю" if f.state is FactState.UNKNOWN else display_value(kb, f.key, f.value)
        out.append((f.participant, f.key, fd.label, val))
    return out


def render_profile(profile: Profile, kb: m.Bundle) -> str:
    lines = ["<b>Проверьте, пожалуйста, анкету:</b>"]
    cur = None
    for pid, _, label, val in profile_lines(profile, kb):
        if pid != cur:
            cur = pid
            who = "Общее" if pid == CASE else profile.participants[pid].label if pid in profile.participants else pid
            lines.append(f"\n<b>{h(who)}</b>")
        lines.append(f"• {h(label)}: {h(val)}")
    return "\n".join(lines)


def _ground_block(v: GroundView, full: bool) -> list[str]:
    out = [f"{STATUS_ICON[v.status]} <b>{h(v.title)}</b> — {h(v.status_label.lower())}"]
    if v.personal:
        out.append(h(v.personal))
    if full:
        if v.summary:
            out.append(h(v.summary))
        if v.met:
            out.append("<b>Что уже совпадает:</b>\n" + "\n".join(f"• {h(x)}" for x in v.met))
        if v.unknown:
            out.append("<b>Что нужно уточнить или подтвердить документами:</b>\n" + "\n".join(
                f"• {h(a)}" + (f" — {h(b)}" if b else "") for a, b in v.unknown))
        if v.unmet:
            out.append("<b>Что нужно выполнить:</b>\n" + "\n".join(
                f"• {h(a)}" + (f" — {h(b)}" if b else "") for a, b in v.unmet))
        if v.individual:
            out.append("<b>Требует индивидуальной проверки:</b>\n" + "\n".join(f"• {h(x)}" for x in v.individual))
            if v.review_reason:
                out.append(f"<i>{h(v.review_reason)}</i>")
        if v.restrictions:
            out.append("<b>Важные ограничения:</b>\n" + "\n".join(f"• {h(x)}" for x in v.restrictions))
        if v.steps:
            out.append("<b>Следующие шаги:</b>\n" + "\n".join(f"{i}. {h(x)}" for i, x in enumerate(v.steps, 1)))
    if v.links:
        out.append("Подробнее: " + ", ".join(link(x) for x in v.links))
    return out


def render_result(rv: ResultView) -> str:
    parts: list[str] = [DEMO_BANNER.strip()] if rv.demo else []
    if rv.kind in ("unsupported", "none"):
        parts.append(h(rv.message))
        for v in rv.not_suitable[:3]:
            parts.append(f"⛔ <b>{h(v.title)}</b>: " + h("; ".join(v.reasons) or v.status_label))
        parts.append("Можно заказать индивидуальную консультацию — кнопка ниже.")
        parts.append(DISCLAIMER)
        return "\n\n".join(parts)
    if rv.intro:
        parts.append(h(rv.intro))
    if rv.primary:
        parts.append("<b>Что стоит проверить первым:</b>")
        parts.append("\n".join(_ground_block(rv.primary, full=True)))
        if rv.primary.criteria:
            parts.append("<i>Почему первым: " + h("; ".join(rv.primary.criteria)) + ".</i>")
    elif rv.kind == "clarify":
        parts.append(h(rv.message))
    elif rv.kind == "individual":
        parts.append("Ваша ситуация требует индивидуального разбора: по части условий нельзя сделать уверенный "
                     "вывод на основе проверенных правил.")
    if rv.alternatives:
        title = "Что можно проверить:" if rv.primary is None else "Другие варианты:"
        parts.append(f"<b>{title}</b>\n" + "\n\n".join(
            "\n".join(_ground_block(v, full=False)) for v in rv.alternatives[:3]))
    if rv.family:
        fam = ["<b>Семья:</b>"]
        if rv.family_intro:
            fam.append(h(rv.family_intro))
        for mv in rv.family:
            if mv.note:
                fam.append(f"• <b>{h(mv.label)}</b>: {h(mv.note)}")
            elif mv.grounds:
                fam.append(f"• <b>{h(mv.label)}</b>: " + "; ".join(
                    f"{STATUS_ICON[g.status]} {h(g.title)} — {h(g.status_label.lower())}" for g in mv.grounds))
            else:
                fam.append(f"• <b>{h(mv.label)}</b>: данных недостаточно — уточним при консультации.")
        fam.append("<i>Основание основного заявителя на семью автоматически не распространяется.</i>")
        parts.append("\n".join(fam))
    parts.append(DISCLAIMER)
    return "\n\n".join(parts)


def render_why(rv: ResultView) -> str:
    v = rv.primary or (rv.alternatives[0] if rv.alternatives else None)
    if v is None:
        return h(rv.message or "Подходящего варианта по проверенным правилам не найдено.")
    out = [f"<b>Почему «{h(v.title)}»</b>"]
    if v.facts_used:
        out.append("<b>Ваши ответы, которые учтены:</b>\n" + "\n".join(f"• {h(a)}: {h(b)}" for a, b in v.facts_used))
    if v.met:
        out.append("<b>Выполненные условия:</b>\n" + "\n".join(f"• {h(x)}" for x in v.met))
    if v.unknown:
        out.append("<b>Неизвестно:</b>\n" + "\n".join(f"• {h(a)}" for a, _ in v.unknown))
    if v.criteria:
        out.append("<b>Критерии сравнения:</b> " + h("; ".join(v.criteria)) + ".")
    if v.documents:
        out.append("<b>Обычно нужны документы:</b>\n" + "\n".join(f"• {h(x)}" for x in v.documents))
    if v.practice_notes:
        out.append("<b>Практические замечания:</b>\n" + "\n".join(
            f"• {h(t)}" + (f" <i>({h(s)})</i>" if s else "") for t, s in v.practice_notes))
    if v.official:
        out.append("<b>Официальные источники:</b>\n" + "\n".join(f"• {link(x)}" for x in v.official))
    out.append(f"<i>Версия базы правил: №{rv.release_id}, расчёт на {rv.as_of.strftime('%d.%m.%Y')}.</i>")
    return "\n\n".join(out)


def render_alternatives(rv: ResultView) -> str:
    out = ["<b>Все рассмотренные варианты</b>"]
    for v in ([rv.primary] if rv.primary else []) + rv.alternatives:
        line = f"{STATUS_ICON[v.status]} <b>{h(v.title)}</b> — {h(v.status_label.lower())}"
        if v.unknown:
            line += "\n   Уточнить: " + h("; ".join(a for a, _ in v.unknown[:3]))
        if v.unmet:
            line += "\n   Выполнить: " + h("; ".join(a for a, _ in v.unmet[:3]))
        if v.links:
            line += "\n   " + ", ".join(link(x) for x in v.links)
        out.append(line)
    for v in rv.not_suitable:
        out.append(f"⛔ <b>{h(v.title)}</b> — не подходит по указанным данным\n   " + h("; ".join(v.reasons)))
    if len(out) == 1:
        out.append("Других проверенных вариантов по вашим ответам нет.")
    return "\n\n".join(out)


# --------------------------------------------------------------------------- (де)сериализация вида


def view_from_dict(d: dict) -> ResultView:
    def gv(x: dict) -> GroundView:
        x = dict(x)
        x["status"] = Status(x["status"])
        x["links"] = [Link(**lk) for lk in x.get("links", [])]
        x["official"] = [Link(**lk) for lk in x.get("official", [])]
        for k in ("unknown", "unmet", "facts_used", "practice_notes"):
            x[k] = [tuple(t) for t in x.get(k, [])]
        known = {f.name for f in fields(GroundView)}
        return GroundView(**{k: v for k, v in x.items() if k in known})

    rv = ResultView(kind=d["kind"], release_id=d["release_id"], as_of=date.fromisoformat(d["as_of"]),
                    demo=d.get("demo", False), message=d.get("message", ""), intro=d.get("intro", ""),
                    family_intro=d.get("family_intro", ""), explain_source=d.get("explain_source", "template"))
    rv.primary = gv(d["primary"]) if d.get("primary") else None
    rv.alternatives = [gv(x) for x in d.get("alternatives", [])]
    rv.not_suitable = [gv(x) for x in d.get("not_suitable", [])]
    rv.family = [MemberView(x["pid"], x["label"], x["relation"], [gv(g) for g in x.get("grounds", [])],
                            x.get("note", ""))
                 for x in d.get("family", [])]
    return rv
