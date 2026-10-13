"""Контроллер диалога — конечный автомат анкеты без привязки к Telegram.

Кнопки и свободный текст — не отдельные режимы: любое сообщение можно
прислать в любой момент. Текст разбирается JEV и превращается в распознанные
сведения. Те, в свою очередь, подтверждаются, и анкета продолжается с
недостающих вопросов.

Адаптер (bot/) загружает сессию, вызывает ``Dialog.handle`` и сохраняет
результат атомарно с проверкой ревизии. Контроллер ничего не пишет в БД
и не отправляет сам: всё нужное он возвращает в ``Out``.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from bginfo.engine import present as P
from bginfo.engine.evaluate import evaluate_all
from bginfo.engine.facts import CASE, MAIN, MISSING, FactState, Profile
from bginfo.engine.planner import Step, next_step, question_for
from bginfo.engine.result import build_result, display_value, render_text, view_to_dict
from bginfo.kb import model as m
from bginfo.kb.releases import Snapshot
from bginfo.stats.situation import situation_key, situation_of

log = logging.getLogger(__name__)

MAX_TEXT = 1500


# --------------------------------------------------------------------------- data


@dataclass
class Button:
    text: str
    action: str
    arg: str = ""


@dataclass
class Reply:
    html: str
    buttons: list[list[Button]] = field(default_factory=list)
    edit: bool = False  # заменить сообщение с нажатой кнопкой (для переключателей)
    no_preview: bool = False  # отключить превью ссылок


@dataclass
class SessionData:
    id: int
    revision: int
    state: str
    profile: Profile
    ui: dict
    release_id: int | None


@dataclass
class UserCtx:
    tg_user_id: int
    username: str | None
    ai_notice_shown: bool


@dataclass
class Event:
    kind: str  # start | text | button | command
    text: str = ""
    action: str = ""
    arg: str = ""


@dataclass
class Out:
    replies: list[Reply] = field(default_factory=list)
    alert: str | None = None
    result_record: dict | None = None
    consult: dict | None = None
    events: list[tuple[str, str]] = field(default_factory=list)
    ai_notice_shown: bool = False
    delete_user: bool = False
    restart: bool = False
    # Машиночитаемое описание экрана для мини-аппа (question | review | result); бот его игнорирует.
    view: dict | None = None

    def say(self, html: str, buttons: list[list[Button]] | None = None, edit: bool = False) -> None:
        """Добавить ответ. Экран мини-аппа (``view``) описывает последний ответ, поэтому новый ответ его
        сбрасывает: тот, кто задаёт ``view``, делает это ПОСЛЕ своего ``say``."""
        self.replies.append(Reply(html, buttons or [], edit))
        self.view = None


NAV_HELP = Button("❓ Поясните вопрос", "help")
NAV_BACK = Button("« Назад", "back")
NAV_FIX = Button("✏️ Исправить", "fix")
RESULT_BUTTONS = [
    [Button("Почему этот вариант?", "why")],
    [Button("Какие ещё варианты?", "alts")],
    [Button("Изменить ответы", "fix")],
    [Button("Заказать консультацию", "consult")],
    [Button("Начать заново", "restart")],
]


# --------------------------------------------------------------------------- controller


class Dialog:
    def __init__(self, interpreter, explainer, *, today, consult_enabled: bool, consult_contact: str = "",
                 webapp_url: str | None = None):
        """``today`` — функция, возвращающая текущую дату в часовом поясе Болгарии.

        ``webapp_url`` — адрес мини-аппа: если задан, в приветствии бота появляется кнопка «Заполнить в форме».
        """
        self.interpreter = interpreter
        self.explainer = explainer
        self.today = today
        self.consult_enabled = consult_enabled
        self.consult_contact = consult_contact
        self.webapp_url = webapp_url

    # ------------------------------------------------------------------ entry
    async def handle(self, s: SessionData, ev: Event, snap: Snapshot | None, user: UserCtx) -> Out:
        out = Out()
        if ev.kind == "command" and ev.action == "delete":
            token = secrets.token_hex(4)
            s.ui["delete_token"] = token
            out.say("Удалить вашу анкету и результаты? Это действие необратимо.",
                    [[Button("Удалить", "del_yes", token), Button("Отмена", "del_no")]])
            return out
        if ev.kind == "button" and ev.action == "del_yes":
            token = s.ui.get("delete_token")
            if not token or ev.arg != token:
                out.alert = "Кнопка устарела."
                return out
            s.ui.pop("delete_token", None)
            out.delete_user = True
            out.say("Ваши анкеты и результаты удалены. Чтобы начать заново, отправьте /start.")
            return out
        if ev.kind == "button" and ev.action == "del_no":
            s.ui.pop("delete_token", None)
            out.say("Хорошо, ничего не удаляю.")
            return out

        if snap is None or not snap.bundle.active_grounds():
            text = P.UNAVAILABLE + (f"\n\nКонтакт: {P.h(self.consult_contact)}" if self.consult_contact else "")
            out.say(text)
            return out
        kb = snap.bundle

        if ev.kind == "start":
            return self._welcome(s, kb, user, out)
        if ev.kind == "command" and ev.action == "result":
            return await self._show_result(s, snap, user, out, refresh=True)
        if ev.kind == "button":
            return await self._button(s, ev, snap, user, out)
        if ev.kind == "text":
            return await self._text(s, ev.text, snap, user, out)
        return out

    # ------------------------------------------------------------------ welcome
    def _welcome(self, s: SessionData, kb: m.Bundle, user: UserCtx, out: Out) -> Out:
        has_progress = bool(s.profile.facts)
        banner = P.DEMO_BANNER if kb.demo else ""
        if has_progress and s.state not in ("start",):
            btns = [[Button("Продолжить", "resume")], [Button("Начать заново", "restart")]]
            if s.ui.get("last_view"):
                btns.insert(0, [Button("Показать актуальный результат", "refresh")])
            out.say(banner + "У вас есть сохранённая анкета. Продолжим с того же места?", btns)
            return out
        rows = [[Button("Подобрать вариант", "go")], [Button("Описать ситуацию самому", "free")]]
        if self.webapp_url:
            rows.append([Button("📝 Заполнить в форме", "webapp")])
        out.say(banner + P.h(P.WELCOME), rows)
        out.replies[-1].no_preview = True
        out.events.append(("session_started", ""))
        return out

    # ------------------------------------------------------------------ buttons
    async def _button(self, s: SessionData, ev: Event, snap: Snapshot, user: UserCtx, out: Out) -> Out:
        kb = snap.bundle
        a, arg = ev.action, ev.arg
        if a == "restart":
            out.restart = True
            return out
        if a in ("go", "asks", "resume"):
            if a == "asks":
                s.ui.pop("pending_text", None)
            s.state = "asking"
            return await self._advance(s, snap, user, out)
        if a == "resume_view":
            # устаревшая кнопка: повторно показываем актуальный шаг без изменений в анкете
            return await self._advance(s, snap, user, out)
        if a == "show":
            return await self._show(s, snap, user, out)
        if a == "parse_pending":
            pending = s.ui.pop("pending_text", None)
            if not pending:
                out.alert = "Кнопка устарела."
                return out
            out.ai_notice_shown = True
            return await self._text(s, pending, snap, user, out, notice_ok=True)
        if a == "free":
            s.state = "free_wait"
            out.say("Опишите вашу ситуацию в свободной форме: кто планирует переезд, гражданство, чем занимаетесь, "
                    "есть ли семья и связи с Болгарией.\n\n" + P.AI_NOTICE,
                    [[Button("Лучше задавайте вопросы", "asks")]])
            out.ai_notice_shown = True
            return out
        if a in ("ans", "tog", "done", "unk", "multi"):
            return await self._answer_button(s, a, arg, snap, user, out)
        if a == "help":
            q = self._current_q(s, kb)
            txt = render_text(q.help, kb, self.today()) if q and q.help else "Отвечайте как есть. Если не уверены — нажмите «Не знаю», это не помешает подбору."
            out.say("💡 " + P.h(txt))
            return out
        if a == "back":
            hist = s.ui.get("history", [])
            if not hist:
                out.alert = "Это первый вопрос."
                return out
            pid, key = hist.pop()
            s.profile.forget(pid, key, "back")
            q = question_for(kb, key)
            s.state = "asking"
            return self._ask(s, kb, Step("question", question=q, participant=pid), out)
        if a == "fix":
            return self._edit_menu(s, kb, out)
        if a == "edit":
            items = s.ui.get("edit_list", [])
            try:
                pid, key = items[int(arg)]
            except (ValueError, IndexError):
                out.alert = "Кнопка устарела."
                return out
            q = question_for(kb, key)
            if q is None or (pid != CASE and pid not in s.profile.participants):
                out.alert = "Этот ответ сейчас нельзя изменить."
                return out
            s.ui["editing"] = [pid, key]
            s.state = "asking"
            return self._ask(s, kb, Step("question", question=q, participant=pid), out)
        if a == "pend_ok":
            # Подтверждаются только показанные простые факты. Списки (multi) подтверждаются
            # своим вопросом с предвыбором — пользователь должен их увидеть.
            for f in list(s.profile.pending()):
                fd = kb.fact(f.key)
                if f.state is FactState.RECOGNIZED and not (fd is not None and fd.type == "multi"):
                    s.profile.accept_pending(f.participant, f.key, kb)
                    s.ui.setdefault("history", []).append([f.participant, f.key])
            self._sync_members(s, kb)
            return await self._advance(s, snap, user, out)
        if a == "pend_fix":
            for f in list(s.profile.pending()):
                fd = kb.fact(f.key)
                if f.state is FactState.RECOGNIZED and not (fd is not None and fd.type == "multi"):
                    s.profile.reject_pending(f.participant, f.key)
            return await self._advance(s, snap, user, out)
        if a == "conf":
            f = next((x for x in s.profile.pending() if x.state is FactState.CONFLICT), None)
            if f is not None:
                if arg == "new":
                    s.profile.accept_pending(f.participant, f.key, kb)
                else:
                    s.profile.reject_pending(f.participant, f.key)
                self._sync_members(s, kb)
            return await self._advance(s, snap, user, out)
        if a == "review_ok":
            return await self._show_result(s, snap, user, out, refresh=True)
        if a == "refresh":
            return await self._show_result(s, snap, user, out, refresh=True)
        if a in ("why", "alts"):
            d = s.ui.get("last_view")
            if not d:
                out.alert = "Сначала получите результат."
                return out
            if self._view_stale(d, snap):
                out = await self._show_result(s, snap, user, out, refresh=True)
                d = s.ui.get("last_view")
                if s.state != "result" or not d:
                    return out  # появились вопросы — сначала они
            rv = P.view_from_dict(d)
            out.say(P.render_why(rv) if a == "why" else P.render_alternatives(rv), RESULT_BUTTONS)
            return out
        if a == "consult":
            d = s.ui.get("last_view")
            if d and self._view_stale(d, snap):
                out = await self._show_result(s, snap, user, out, refresh=True)
                if s.state != "result":
                    return out  # появились вопросы — предпросмотр только по свежему результату
            return self._consult_preview(s, kb, user, out)
        if a == "consult_send":
            if not s.ui.get("consult_preview"):
                out.alert = "Сначала откройте предпросмотр."
                return out
            out.consult = {"session_id": s.id, "html": s.ui.pop("consult_preview")}
            s.state = "result"
            return out
        if a == "consult_cancel":
            s.ui.pop("consult_preview", None)
            s.state = "result"
            out.say("Хорошо, ничего не отправляю.", RESULT_BUTTONS)
            return out
        out.alert = "Неизвестная кнопка."
        return out

    def _view_stale(self, d: dict, snap: Snapshot) -> bool:
        """Сохранённый результат посчитан по другому выпуску правил или на другую дату."""
        return d.get("release_id") != snap.release_id or d.get("as_of") != self.today().isoformat()

    async def _answer_button(self, s: SessionData, a: str, arg: str, snap: Snapshot, user: UserCtx, out: Out) -> Out:
        kb = snap.bundle
        q = self._current_q(s, kb)
        if q is None:
            out.alert = "Этот вопрос уже не актуален."
            return out
        pid = s.ui.get("pid", MAIN)
        fd = kb.fact(q.fact)
        if a == "unk":
            if not q.allow_unknown:
                out.alert = "На этот вопрос нужен ответ."
                return out
            s.profile.mark_unknown(pid, q.fact, kb)
            self._after_answer(s, pid, q.fact)
            return await self._advance(s, snap, user, out)
        if a == "tog":
            sel = set(s.ui.get("sel", []))
            if arg not in {o.value for o in fd.options}:
                out.alert = "Кнопка устарела."
                return out
            if arg in sel:
                sel.discard(arg)
            else:
                sel.add(arg)
                if arg == "none":
                    sel = {"none"}
                else:
                    sel.discard("none")
            s.ui["sel"] = sorted(sel)
            r = self._ask(s, kb, Step("question", question=q, participant=pid), Out())
            out.say(r.replies[0].html, r.replies[0].buttons, edit=True)
            return out
        if a == "done":
            sel = s.ui.get("sel", [])
            if not sel:
                out.alert = "Отметьте хотя бы один пункт или «Ничего из этого»."
                return out
            return await self._commit(s, pid, q.fact, sorted(sel), snap, user, out)
        if a == "multi":
            # выбор копится на клиенте (форма): значения приходят одним действием через запятую
            if fd.type != "multi":
                out.alert = "Кнопка устарела."
                return out
            vals = sorted({v.strip() for v in arg.split(",") if v.strip()})
            if not vals:
                out.alert = "Отметьте хотя бы один пункт или «Ничего из этого»."
                return out
            if not set(vals) <= {o.value for o in fd.options}:
                out.alert = "Кнопка устарела."
                return out
            if "none" in vals and len(vals) > 1:
                out.alert = "«Ничего из этого» нельзя сочетать с другими пунктами."
                return out
            return await self._commit(s, pid, q.fact, vals, snap, user, out)
        # ans
        if fd.type == "bool":
            if arg not in ("yes", "no"):
                out.alert = "Кнопка устарела."
                return out
            value: Any = arg == "yes"
        else:
            if arg not in {o.value for o in fd.options}:
                out.alert = "Кнопка устарела."
                return out
            value = arg
        return await self._commit(s, pid, q.fact, value, snap, user, out)

    async def _commit(self, s: SessionData, pid: str, key: str, value: Any, snap: Snapshot, user: UserCtx,
                      out: Out) -> Out:
        kb = snap.bundle
        removed = s.profile.confirm_value(pid, key, value, "button", kb)
        if removed:
            out.say("Учёл изменение. Связанные с ним ответы уточню заново.")
        self._after_answer(s, pid, key)
        return await self._advance(s, snap, user, out)

    def _after_answer(self, s: SessionData, pid: str, key: str) -> None:
        s.ui.pop("sel", None)
        hist = s.ui.setdefault("history", [])
        if [pid, key] not in hist:
            hist.append([pid, key])
        if s.ui.get("editing") == [pid, key]:
            s.ui.pop("editing", None)

    # ------------------------------------------------------------------ text
    async def _text(self, s: SessionData, text: str, snap: Snapshot, user: UserCtx, out: Out,
                    notice_ok: bool = False) -> Out:
        kb = snap.bundle
        text = (text or "").strip()
        if not text:
            return out
        if len(text) > MAX_TEXT:
            out.say(f"Сообщение длинное — пожалуйста, сократите до {MAX_TEXT} символов или отвечайте кнопками.")
            return out
        if s.state == "consult_preview":
            out.say("Чтобы отправить заявку, нажмите «Отправить» или «Отмена» под предпросмотром.")
            return out

        q = self._current_q(s, kb) if s.state == "asking" else None
        pid = s.ui.get("pid", MAIN)
        if q is not None:
            fd = kb.fact(q.fact)
            from bginfo.ai.interpret import parse_simple

            kind, val = parse_simple(text, fd)
            if kind == "ok":
                return await self._commit(s, pid, q.fact, val, snap, user, out)
            if kind == "unknown" and q.allow_unknown:
                s.profile.mark_unknown(pid, q.fact, kb)
                self._after_answer(s, pid, q.fact)
                return await self._advance(s, snap, user, out)
            if kind == "bad":
                out.say(P.h(val))
                return self._ask(s, kb, Step("question", question=q, participant=pid), out)
            if fd.type in ("money", "int", "date") and len(text) < 25:
                out.say("Не получилось разобрать ответ. " + {
                    "money": "Напишите сумму числом, например: 850",
                    "int": "Напишите число, например: 2",
                    "date": "Напишите дату в виде ММ.ГГГГ"}[fd.type])
                return self._ask(s, kb, Step("question", question=q, participant=pid), out)

        if not user.ai_notice_shown and not notice_ok:
            # Текст уйдёт во внешний AI-сервис только после того, как человек увидел уведомление.
            s.ui["pending_text"] = text[:MAX_TEXT]
            out.say(P.AI_NOTICE,
                    [[Button("Да, разобрать мой текст", "parse_pending")],
                     [Button("Лучше задавайте вопросы", "asks")]])
            out.ai_notice_shown = True
            return out

        it = await self.interpreter.interpret(text, kb, q, user.tg_user_id)
        if not it.ok:
            reason = "лимит на сегодня исчерпан" if it.error in ("limit", "budget") else "сервис разбора недоступен"
            out.say(f"Сейчас не получается разобрать текст ({reason}). Ничего не потеряно — продолжим кнопками.")
            out.events.append(("jev_unavailable", it.error))
            if s.state in ("start", "free_wait"):
                s.state = "asking"
            return await self._advance(s, snap, user, out)

        if it.intent == "wants_buttons" and it.intent_conf >= 0.5:
            s.state = "asking"
            return await self._advance(s, snap, user, out)
        if it.intent == "other" and it.intent_conf >= 0.6 and not it.items:
            out.say("Я помогаю только с подбором оснований для проживания в Болгарии. Продолжим?")
            if s.state in ("asking", "review"):
                return await self._advance(s, snap, user, out)
            return out
        if it.intent == "question_to_bot" and it.intent_conf >= 0.6 and not it.items:
            if q is not None and q.help:
                out.say("💡 " + P.h(q.help))
                return self._ask(s, kb, Step("question", question=q, participant=pid), out)
            if s.state == "result":
                out.say("Подробности — кнопками под результатом: «Почему этот вариант?» и «Какие ещё варианты?». "
                        "Для разбора вашей ситуации можно заказать консультацию.", RESULT_BUTTONS)
                return out

        from bginfo.ai.interpret import apply_to_profile

        if q is not None and it.current_answer is not None and s.profile.get(pid, q.fact) is None:
            s.profile.recognize(pid, q.fact, it.current_answer, "jev")
        deferred = apply_to_profile(it, s.profile, kb)
        merged = {(d["role"], d["key"]): d for d in s.ui.get("deferred", [])}
        for d in deferred:
            merged[(d.role, d.key)] = d.__dict__
        s.ui["deferred"] = list(merged.values())[-20:]
        for key in it.unclear:
            s.ui.setdefault("clarify", []).append(key)
        recognized_any = bool(it.items) or it.current_answer is not None
        if not recognized_any and not it.unclear:
            if s.state == "free_wait":
                out.say("Пока не удалось понять детали. Можно дописать ещё или перейти к вопросам.",
                        [[Button("Лучше задавайте вопросы", "asks")]])
                return out
            out.say("Не совсем понял. Ответьте, пожалуйста, на вопрос — кнопками или своими словами.")
        out.events.append(("text_parsed", f"items={len(it.items)}"))
        if s.state in ("start", "free_wait", "result", "review"):
            s.state = "asking"
        return await self._advance(s, snap, user, out)

    # ------------------------------------------------------------------ flow
    def _sync_members(self, s: SessionData, kb: m.Bundle) -> None:
        """Список членов семьи из ответов о составе переезжающих + производные факты."""
        who = s.profile.value(CASE, "who_moves")
        if who not in (None, "already_in_bg") and who is not MISSING and kb.fact("status_in_bg") is not None:
            # Ещё не живёт в Болгарии — статуса там нет (производный факт, без вопроса).
            f = s.profile.get(MAIN, "status_in_bg")
            if (f is None or f.source == "derived") and s.profile.value(MAIN, "status_in_bg") != "none":
                s.profile.confirm_value(MAIN, "status_in_bg", "none", "derived", None)
        elif who == "already_in_bg":
            f = s.profile.get(MAIN, "status_in_bg")
            if f is not None and f.source == "derived":
                s.profile.forget(MAIN, "status_in_bg", "who_moves_changed")
        if who in ("self", "other_person"):
            s.profile.set_members([])  # семьи нет: участники и их факты уходят в историю
            return
        self._spouse_ties(s, kb)
        fam = s.profile.value(CASE, "family_moving")
        if not isinstance(fam, list):
            return
        wanted: list[tuple[str, str, str]] = []
        if "spouse" in fam:
            wanted.append(("s1", "spouse", "Супруг(а)"))
        if "children" in fam:
            n = s.profile.value(CASE, "children_count")
            n = n if isinstance(n, int) and 0 < n <= 10 else 0
            wanted += [(f"c{i}", "child", f"Ребёнок {i}") for i in range(1, n + 1)]
        if "parents" in fam:
            wanted.append(("par1", "parent", "Родитель"))
        if "other" in fam:
            wanted.append(("o1", "other", "Другой родственник"))
        before = set(s.profile.participants)
        s.profile.set_members(wanted)
        if kb.fact("relation") is not None:  # родство — производный факт, без вопроса
            for p in s.profile.members():
                if s.profile.value(p.id, "relation") != p.relation:
                    s.profile.confirm_value(p.id, "relation", p.relation, "derived", None)
        if set(s.profile.participants) - before and s.ui.get("deferred"):
            from bginfo.ai.interpret import Interpretation, Recognized, apply_to_profile

            items = [Recognized(**d) for d in s.ui.get("deferred", [])]
            left = apply_to_profile(Interpretation(ok=True, items=items), s.profile, kb)
            s.ui["deferred"] = [d.__dict__ for d in left]

    def _spouse_ties(self, s: SessionData, kb: m.Bundle) -> None:
        """Гражданство супруга открывает основания самому заявителю (член семьи болгарина/гражданина ЕС).

        Если в ответах заявителя эта связь не отмечена, предлагаем её подтвердить
        (RECOGNIZED), а не добавляем молча и не теряем направление.
        """
        fd = kb.fact("ties")
        if fd is None:
            return
        opts = {o.value for o in fd.options}
        want: set[str] = set()
        for p in s.profile.members():
            if p.relation != "spouse":
                continue
            cit = s.profile.value(p.id, "citizenship_group")
            cit = set(cit) if isinstance(cit, list) else set()
            if "bg" in cit and "spouse_bg" in opts:
                want.add("spouse_bg")
            elif "eu" in cit and "eu_relative" in opts:
                want.add("eu_relative")
        if not want:
            return
        cur = s.profile.get(MAIN, "ties")
        if cur is None or cur.state is not FactState.CONFIRMED or not isinstance(cur.value, list):
            return  # ещё не спрашивали — вопрос про связи будет задан и так
        if want <= set(cur.value) or s.ui.get("spouse_ties_offered"):
            return
        merged = sorted((set(cur.value) - {"none"}) | want)
        s.profile.facts[s.profile._k(MAIN, "ties")] = type(cur)(MAIN, "ties", merged, FactState.RECOGNIZED, "derived")
        s.ui["spouse_ties_offered"] = True

    async def _advance(self, s: SessionData, snap: Snapshot, user: UserCtx, out: Out) -> Out:
        kb = snap.bundle
        self._sync_members(s, kb)
        if s.state not in ("asking", "review"):
            s.state = "asking"
        changed_rules = s.release_id is not None and s.release_id != snap.release_id
        s.release_id = snap.release_id
        as_of = self.today()

        # явные просьбы уточнить (противоречия из текста)
        clar = s.ui.get("clarify", [])
        while clar:
            key = clar.pop(0)
            q = question_for(kb, key)
            fd = kb.fact(key)
            if q is None or fd is None:
                continue
            pid = CASE if fd.scope == "case" else MAIN
            cur = s.profile.get(pid, key)
            if cur is None or cur.state is not FactState.CONFIRMED:
                s.profile.forget(pid, key, "clarify")
            else:
                # Противоречивое упоминание уже подтверждённого ответа: переспрашиваем,
                # прежний ответ остаётся в силе, пока пользователь не ответит заново.
                s.ui["editing"] = [pid, key]
                out.say("В сообщении это прозвучало противоречиво — уточните, пожалуйста.")
            return self._ask(s, kb, Step("question", question=q, participant=pid), out)

        ev = evaluate_all(kb, s.profile, as_of, snap.release_id)
        step = next_step(kb, s.profile, ev, as_of)
        if changed_rules and step.kind != "review":
            out.say("ℹ️ Правила в базе обновились — уточню пару моментов.")
        if step.kind == "review":
            s.state = "review"
            return self._say_review(s, kb, out)
        return self._ask(s, kb, step, out)

    def _say_review(self, s: SessionData, kb: m.Bundle, out: Out) -> Out:
        out.say(P.render_profile(s.profile, kb),
                [[Button("✅ Всё верно — показать результат", "review_ok")], [NAV_FIX]])
        out.view = self._review_view(s, kb)
        return out

    async def _show(self, s: SessionData, snap: Snapshot, user: UserCtx, out: Out) -> Out:
        """Перерисовать текущий экран, не меняя анкету (восстановление мини-аппа после устаревшего экрана).

        Результат не пересчитывается и не записывается, объяснитель не вызывается. Если текущий экран
        воссоздать нельзя (нет сохранённого вида, правила обновились и т. п.), работает как ``resume_view``.
        """
        kb = snap.bundle
        d = s.ui.get("last_view")
        if s.state == "result" and d and not self._view_stale(d, snap):
            html = P.render_result(P.view_from_dict(d))
            out.say(html, RESULT_BUTTONS)
            out.view = {"result": {"view": d, "html": html, "disclaimer": P.DISCLAIMER}}
            return out
        if s.state == "review":
            return self._say_review(s, kb, out)
        if s.state == "asking":
            q = self._current_q(s, kb)
            if q is not None:
                return self._ask(s, kb, Step("question", question=q, participant=s.ui.get("pid", MAIN)), out)
        if s.state == "consult_preview" and s.ui.get("consult_preview"):
            return self._say_preview(s.ui["consult_preview"], out)
        return await self._advance(s, snap, user, out)

    def _current_q(self, s: SessionData, kb: m.Bundle) -> m.Question | None:
        qid = s.ui.get("qid")
        return next((q for q in kb.questions if q.id == qid), None) if qid else None

    def _ask(self, s: SessionData, kb: m.Bundle, step: Step, out: Out) -> Out:
        if step.kind == "conflict":
            f = step.pending[0]
            fd = kb.fact(f.key)
            who = "" if f.participant in (MAIN, CASE) else f" ({P.h(s.profile.participants[f.participant].label)})"
            s.ui["qid"] = None
            out.say(
                f"Уточните, пожалуйста{who}: раньше вы указали «{P.h(fd.label)}: "
                f"{P.h(display_value(kb, f.key, f.prev_value))}», а сейчас — "
                f"«{P.h(display_value(kb, f.key, f.value))}». Что верно?",
                [[Button(display_value(kb, f.key, f.value)[:60], "conf", "new")],
                 [Button(display_value(kb, f.key, f.prev_value)[:60], "conf", "old")]],
            )
            return out
        if step.kind == "confirm":
            s.ui["qid"] = None
            lines = ["Правильно ли я понял?"]
            for f in step.pending:
                fd = kb.fact(f.key)
                who = "" if f.participant in (MAIN, CASE) else f"{s.profile.participants[f.participant].label}: "
                lines.append(f"• {P.h(who)}{P.h(fd.label)} — {P.h(display_value(kb, f.key, f.value))}")
            out.say("\n".join(lines), [[Button("✅ Да, верно", "pend_ok")], [Button("✏️ Нет, исправить", "pend_fix")]])
            return out

        q = step.question
        pid = step.participant or MAIN
        fd = kb.fact(q.fact)
        cur = s.profile.get(pid, q.fact)
        cur_list = list(cur.value) if fd.type == "multi" and cur is not None and isinstance(cur.value, list) else []
        # Предвыбор для multi: при смене вопроса, а также когда появилось новое распознанное
        # значение (в т.ч. предложенное кодом) — но не при каждом переключателе.
        sel_for = [pid, q.fact, sorted(cur_list)] if cur is not None and cur.state is FactState.RECOGNIZED else None
        if s.ui.get("qid") != q.id or s.ui.get("pid") != pid or (sel_for and s.ui.get("sel_for") != sel_for):
            s.ui["sel"] = cur_list
            s.ui["sel_for"] = sel_for
        s.ui["qid"], s.ui["pid"] = q.id, pid
        today = self.today()
        text = P.question_text(q, s.profile, pid, render=lambda t: render_text(t, kb, today))
        base_text = text
        help_text = render_text(q.help, kb, today)
        rows: list[list[Button]] = []
        if fd.type == "bool":
            rows.append([Button("Да", "ans", "yes"), Button("Нет", "ans", "no")])
        elif fd.type == "enum":
            rows += [[Button(o.label, "ans", o.value)] for o in fd.options]
        elif fd.type == "multi":
            sel = set(s.ui.get("sel", []))
            rows += [[Button(("✅ " if o.value in sel else "▫️ ") + o.label, "tog", o.value)] for o in fd.options]
            rows.append([Button("Готово ➡️", "done")])
            text += "\n<i>Отметьте все подходящие пункты и нажмите «Готово».</i>"
            cur = s.profile.get(pid, q.fact)
            if cur is not None and cur.state is FactState.RECOGNIZED:
                text += "\n<i>Я отметил то, что понял из вашего сообщения, — проверьте и дополните.</i>"
        else:
            text += {"money": "\n<i>Напишите сумму в евро, например: 850</i>",
                     "int": "\n<i>Напишите число.</i>",
                     "date": "\n<i>Напишите дату в виде ММ.ГГГГ</i>"}.get(fd.type, "")
        extra = []
        if q.allow_unknown:
            extra.append(Button("Не знаю", "unk"))
        if help_text:
            extra.append(NAV_HELP)
        if extra:
            rows.append(extra)
        nav = []
        if s.ui.get("history"):
            nav.append(NAV_BACK)
        if s.profile.facts:
            nav.append(NAV_FIX)
        if nav:
            rows.append(nav)
        out.say(text, rows)
        if fd.type == "bool":
            view_opts = [{"value": "yes", "label": "Да"}, {"value": "no", "label": "Нет"}]
        else:
            view_opts = [{"value": o.value, "label": o.label} for o in fd.options]
        cur = s.profile.get(pid, q.fact)
        part = s.profile.participants.get(pid)
        out.view = {"question": {
            "qid": q.id, "pid": pid, "who": part.label if part is not None and pid not in (MAIN, CASE) else None,
            "text_html": base_text, "help_html": P.h(help_text) if help_text else "", "type": fd.type,
            "options": view_opts, "selected": sorted(s.ui.get("sel", [])) if fd.type == "multi" else [],
            "allow_unknown": q.allow_unknown, "can_back": bool(s.ui.get("history")),
            "can_fix": bool(s.profile.facts),
            "recognized": cur is not None and cur.state is FactState.RECOGNIZED,
            "answered": len(P.profile_lines(s.profile, kb)),
        }}
        return out

    def _review_view(self, s: SessionData, kb: m.Bundle) -> dict:
        """Строки обзора ответов; ``index`` совпадает с ``s.ui["edit_list"]`` (действие ``edit``)."""
        items, lines = [], []
        for pid, key, label, val in P.profile_lines(s.profile, kb):
            who = "Общее" if pid == CASE else s.profile.participants[pid].label if pid in s.profile.participants else None
            index = None
            if question_for(kb, key) is not None:
                index = len(items)
                items.append([pid, key])
            lines.append({"index": index, "who": who, "label": label, "value": val})
        s.ui["edit_list"] = items
        return {"review": {"lines": lines}}

    def _edit_menu(self, s: SessionData, kb: m.Bundle, out: Out) -> Out:
        items, rows = [], []
        for pid, key, label, val in P.profile_lines(s.profile, kb):
            if question_for(kb, key) is None:
                continue
            who = "" if pid in (MAIN, CASE) else f" ({s.profile.participants[pid].label})" if pid in s.profile.participants else ""
            rows.append([Button(f"{label}{who}: {val}"[:60], "edit", str(len(items)))])
            items.append([pid, key])
        if not items:
            out.alert = "Пока нечего исправлять."
            return out
        s.ui["edit_list"] = items
        out.say("Какой ответ исправить?", rows[:30])
        return out

    # ------------------------------------------------------------------ result
    async def _show_result(self, s: SessionData, snap: Snapshot, user: UserCtx, out: Out, refresh: bool) -> Out:
        kb = snap.bundle
        if not s.profile.facts:
            out.say("Анкета пока пустая. Нажмите /start, чтобы начать подбор.")
            return out
        changed = s.release_id is not None and s.release_id != snap.release_id
        s.release_id = snap.release_id
        as_of = self.today()
        ev = evaluate_all(kb, s.profile, as_of, snap.release_id)
        step = next_step(kb, s.profile, ev, as_of)
        if step.kind != "review":
            # Перед итогом анкета сверяется с актуальной версией правил: не хватает сведений — спрашиваем.
            s.state = "asking"
            out.say("ℹ️ Правила в базе обновились — нужно уточнить ещё немного." if changed
                    else "Нужно уточнить ещё немного.")
            return self._ask(s, kb, step, out)
        rv = build_result(ev, kb, s.profile)
        lines = [(label, val) for _, _, label, val in P.profile_lines(s.profile, kb)]
        titles = [g.title for g in kb.active_grounds()]
        rv = await self.explainer.explain(rv, lines, titles, user.tg_user_id)
        html = P.render_result(rv)
        if changed:
            html = "ℹ️ С момента прошлого подбора правила обновились — результат пересчитан.\n\n" + html
        view = view_to_dict(rv)
        s.ui["last_view"] = view
        s.state = "result"
        sit = situation_of(s.profile)
        out.result_record = {
            "release_id": snap.release_id, "as_of": as_of.isoformat(), "message_html": html,
            "day": self.today().isoformat(),  # для статистики: только дата в часовом поясе бота
            "explain_source": rv.explain_source,
            "result_kind": rv.kind,
            "primary_ground": rv.primary.ground_id if rv.primary else None,
            "primary_status": rv.primary.status.value if rv.primary else None,
            "situation": sit,
            "situation_key": situation_key(sit),
            "grounds": {r.ground_id: r.status.value for r in ev.for_subject(MAIN)},
            "members": len(s.profile.members()),
            "evaluation": [
                {"ground": r.ground_id, "subject": r.subject, "status": r.status.value, "reasons": r.reasons,
                 "requirements": [{"id": q.id, "result": q.result.value, "disputed": q.disputed} for q in r.requirements]}
                for r in ev.results
            ],
        }
        out.events.append(("result_shown", rv.explain_source))
        out.say(html, RESULT_BUTTONS)
        out.view = {"result": {"view": view, "html": html, "disclaimer": P.DISCLAIMER}}
        return out

    # ------------------------------------------------------------------ consult
    def _consult_preview(self, s: SessionData, kb: m.Bundle, user: UserCtx, out: Out) -> Out:
        if not self.consult_enabled:
            msg = "Онлайн-заявка на консультацию пока не настроена."
            if self.consult_contact:
                msg += f" Связаться можно так: {P.h(self.consult_contact)}"
            out.say(msg, RESULT_BUTTONS)
            return out
        d = s.ui.get("last_view")
        lines = ["<b>Заявка на консультацию</b>",
                 "Контакт в Telegram: " + (f"@{P.h(user.username)}" if user.username else f"id {user.tg_user_id}")]
        lines.append(P.render_profile(s.profile, kb).replace("<b>Проверьте, пожалуйста, анкету:</b>", "<b>Анкета:</b>"))
        if d:
            rv = P.view_from_dict(d)
            res = [f"{P.STATUS_ICON[v.status]} {P.h(v.title)} — {P.h(v.status_label.lower())}"
                   for v in ([rv.primary] if rv.primary else []) + rv.alternatives]
            if res:
                lines.append("<b>Предварительный подбор:</b>\n" + "\n".join(res))
        preview = "\n".join(lines)
        s.ui["consult_preview"] = preview
        s.state = "consult_preview"
        return self._say_preview(preview, out)

    def _say_preview(self, preview: str, out: Out) -> Out:
        out.say("Будет передано консультанту:\n\n" + preview + "\n\nОтправить?",
                [[Button("Отправить", "consult_send"), Button("Отмена", "consult_cancel")]])
        return out


def today_in(tz: str):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    z = ZoneInfo(tz)
    return lambda: datetime.now(z).date()
