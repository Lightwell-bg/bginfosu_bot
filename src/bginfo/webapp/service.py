"""Один шаг анкеты для мини-аппа: тот же порядок, что ``BotApp._run_locked``.

Загрузка (SELECT) -> ``Dialog.handle`` (в памяти) -> ОДНА транзакция ``repo.commit_step`` -> и только затем
побочные эффекты (копия результата в чат, доставка заявки на консультацию). Сессия общая с ботом
(``repo.active_session``); гонки закрывает проверка ревизии в ``commit_step``.
Все вызовы БД идут в однопоточном executor; один пользователь обрабатывается последовательно.
В логах нет ни текстов пользователя, ни initData, ни токенов.
"""

from __future__ import annotations

import asyncio
import contextlib
import html as htmllib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bginfo.bot import repo
from bginfo.bot.app import (
    TXT_CONSULT_DUP,
    TXT_CONSULT_FAIL,
    TXT_CONSULT_OFF,
    TXT_CONSULT_OK,
    TXT_CONSULT_UNKNOWN,
    TXT_CONSULT_UNKNOWN_CONTACT,
    _fingerprint,
    build_ai,
    demo_release_in_production,
)
from bginfo.bot.delivery import DUPLICATE, FAILED, MSG_LIMIT, SENT, deliver_consult
from bginfo.bot.render import split_html
from bginfo.config import AppConfig, Settings, get_config, get_settings
from bginfo.db.conn import open_db
from bginfo.engine.dialog import Dialog, Event, Out, UserCtx, today_in
from bginfo.kb.releases import active_snapshot
from bginfo.logsafe import safe_exc
from bginfo.webapp.auth import TgUser

log = logging.getLogger(__name__)

COPY_TIMEOUT_S = 15
COPY_HEADER = "📱 Результат из формы"
CHANNEL = "webapp"

# Исходы шага
OK = "ok"
STALE = "stale"  # sid/rev не совпали с активной сессией: ничего не применено, в ответе актуальный экран
CONFLICT = "conflict"  # гонка при коммите: то же, что STALE
UNAVAILABLE = "unavailable"  # демо-выпуск в production


@dataclass
class StepResult:
    status: str
    body: dict


def out_to_body(out: Out, sid: int | None, rev: int | None, alert: str | None) -> dict:
    """Ответ API: ``{sid, rev, view, replies:[{html, buttons:[[{text, action, arg}]]}], alert}``."""
    return {
        "sid": sid, "rev": rev, "view": out.view,
        "replies": [{"html": r.html,
                     "buttons": [[{"text": b.text, "action": b.action, "arg": b.arg} for b in row]
                                 for row in r.buttons if row]}
                    for r in out.replies],
        "alert": alert,
    }


class WebappService:
    """Сервис шагов мини-аппа. Создаётся ``build_service``."""

    def __init__(self, *, bot: Bot | None, conn, dialog: Dialog, settings: Settings, config: AppConfig,
                 extra_closers: list[Callable[[], Any]] | None = None):
        self.bot = bot
        self.conn = conn
        self.dialog = dialog
        self.settings = settings
        self.config = config
        self._db_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="webapp-db")
        self._locks: dict[int, list] = {}  # uid -> [Lock, число ожидающих/держащих]
        self._closers = extra_closers or []

    # ------------------------------------------------------------------ infra
    async def db(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._db_exec, fn, *args)

    @contextlib.asynccontextmanager
    async def user_lock(self, uid: int):
        entry = self._locks.setdefault(uid, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(uid, None)

    async def close(self) -> None:
        for c in self._closers:
            with contextlib.suppress(Exception):
                r = c()
                if asyncio.iscoroutine(r):
                    await r
        self._db_exec.shutdown(wait=True)
        with contextlib.suppress(Exception):
            self.conn.close()

    # ------------------------------------------------------------------ DB steps (executor)
    def _load(self, uid: int, ref: tuple[int, int] | None, require_ref: bool, names: tuple):
        """(session, user_ctx_flag, snapshot, stale, is_new). Новая сессия «временная» (``blank_session``)."""
        u = repo.touch_user(self.conn, uid, *names)
        s = repo.active_session(self.conn, uid)
        stale = False
        if ref is not None:
            stale = s is None or s.id != ref[0] or s.revision != ref[1]
        elif require_ref and s is not None:
            stale = True
        is_new = False
        if s is None:
            s, is_new = repo.blank_session(), True
        return s, u, active_snapshot(self.conn), stale, is_new

    def _commit(self, uid: int, s, expected: int, bump: bool, out: Out, new_session: bool,
                replaces: tuple[int, int] | None, consult_html: str | None, info: dict,
                expect_no_active: bool = False) -> str:
        return repo.commit_step(
            self.conn, update_id=None, tg_user_id=uid, session=s, expected_revision=expected, bump=bump,
            result_record=out.result_record, events=list(out.events), ai_notice=out.ai_notice_shown,
            delete_user=out.delete_user, new_session=new_session, replaces=replaces,
            consult_html=consult_html, info=info, channel=CHANNEL, expect_no_active=expect_no_active)

    # ------------------------------------------------------------------ main flow
    async def open(self, user: TgUser) -> StepResult:
        """Первый экран: приветствие или «Продолжить / Начать заново»."""
        return await self.step(user, Event("start"), None, require_ref=False)

    async def step(self, user: TgUser, ev: Event, ref: tuple[int, int] | None, *,
                   require_ref: bool = True) -> StepResult:
        async with self.user_lock(user.id):
            res = await self._step_locked(user, ev, ref, require_ref)
            if res.status == CONFLICT:
                # гонка с ботом/другим окном: показываем актуальный экран без изменения анкеты (show)
                res = await self._step_locked(user, Event("button", action="show"), None, False, resume=True)
            return res

    async def _step_locked(self, user: TgUser, ev: Event, ref, require_ref: bool, *,
                           resume: bool = False) -> StepResult:
        uid = user.id
        names = (user.username, user.first_name, user.last_name)
        s, u, snap, stale, is_new = await self.db(self._load, uid, ref, require_ref, names)
        if demo_release_in_production(self.settings, snap):
            return StepResult(UNAVAILABLE, {"error": "unavailable"})
        status = OK
        if stale:
            # ничего из присланного не применяем; актуальный шаг показываем обычным шагом
            status = STALE
            ev = Event("start") if is_new else Event("button", action="show")
        elif resume:
            status = CONFLICT
        ctx = UserCtx(uid, user.username, u["ai_notice_shown"])
        before = None if is_new else _fingerprint(s)
        expected = s.revision
        replaces: tuple[int, int] | None = None
        # сессия создаётся только потому, что активной не было (не явный рестарт): при гонке с ботом или другим
        # окном commit_step вернёт conflict, а не пометит чужую свежую сессию abandoned
        expect_no_active = is_new
        out = await self.dialog.handle(s, ev, snap, ctx)
        if out.restart:
            replaces = (s.id, s.revision) if s.id is not None else None
            s, is_new, expected, before = repo.blank_session(), True, 0, None
            expect_no_active = False
            out = await self.dialog.handle(s, Event("start"), snap, ctx)
        out.events = [(k, CHANNEL if k == "session_started" else d) for k, d in out.events]
        if out.result_record is not None:
            out.result_record["channel"] = CHANNEL
        bump = (before is None or _fingerprint(s) != before or out.result_record is not None
                or out.consult is not None or out.delete_user)
        consult_html = None
        if out.consult is not None and self.settings.CONSULT_CHAT_ID is not None:
            consult_html = out.consult.get("html", "")
        info: dict = {}
        # восстановление после устаревшего экрана/гонки (STALE/CONFLICT) без изменений в анкете: ничего не пишем
        read_only = ((status != OK or resume) and not bump and not is_new and out.result_record is None
                     and out.consult is None and not out.delete_user and not out.events and not out.ai_notice_shown)
        if read_only:
            res = "ok"
        else:
            res = await self.db(self._commit, uid, s, expected, bump, out, is_new, replaces, consult_html, info,
                                expect_no_active)
        if res != "ok":
            if resume:
                return StepResult(CONFLICT, {"error": "conflict"})
            return StepResult(CONFLICT, {})
        rev = s.revision + (1 if bump else 0)
        alert = out.alert
        # --- побочные эффекты: только после успешного коммита
        if out.result_record is not None:
            await self._copy_to_chat(uid, out.result_record.get("message_html", ""))
        screen = out
        if out.consult is not None:
            alert = await self._consult(consult_html, s.id, info.get("consult_id"))
            # экран после заявки: сохранённый результат с кнопками (действие show, ничего не пишет); исход
            # доставки идёт в alert. Если результат успел устареть, экран не подменяем (клиент лишь покажет alert)
            d = s.ui.get("last_view")
            if d and not self.dialog._view_stale(d, snap):
                screen = await self.dialog.handle(s, Event("button", action="show"), snap, ctx)
            else:
                screen = Out()
        if out.delete_user:
            body = out_to_body(out, None, None, alert)
        else:
            body = out_to_body(screen, s.id, rev, alert)
        if status == STALE:
            body["stale"] = True
        return StepResult(status, body)

    # ------------------------------------------------------------------ side effects
    async def _copy_to_chat(self, uid: int, html: str) -> None:
        """Копия результата в личный чат с ботом. Ошибка только логируется, автоповтора нет."""
        if not self.config.webapp.send_result_to_chat or self.bot is None or not html:
            return
        try:
            async with asyncio.timeout(COPY_TIMEOUT_S):
                for chunk in split_html(f"{COPY_HEADER}\n\n{html}", MSG_LIMIT):
                    await self.bot.send_message(uid, chunk)
        except Exception as e:  # noqa: BLE001 - результат уже на экране формы
            log.error("copy result to chat failed: %s", safe_exc(e))

    async def _consult(self, consult_html: str | None, sid: int, cid: int | None) -> str:
        """Доставка заявки (общая с ботом); итог показывается как ``alert``."""
        if consult_html is None or self.bot is None:
            return TXT_CONSULT_OFF
        status = await deliver_consult(self.bot, self.conn, self.db, self.settings.CONSULT_CHAT_ID,
                                       consult_html, sid, cid)
        if status == DUPLICATE:
            return TXT_CONSULT_DUP
        if status == SENT:
            return TXT_CONSULT_OK
        if status == FAILED:
            return TXT_CONSULT_FAIL
        contact = self.config.bot.consult_contact.strip()
        if not contact:
            return TXT_CONSULT_UNKNOWN
        return TXT_CONSULT_UNKNOWN + TXT_CONSULT_UNKNOWN_CONTACT + htmllib.escape(contact)


def build_service(settings: Settings | None = None, config: AppConfig | None = None, *, bot: Bot | None = None,
                  interpreter=None, explainer=None) -> WebappService:
    """Собрать сервис. ``bot``, ``interpreter`` и ``explainer`` можно подменить (тесты)."""
    settings = settings or get_settings()
    config = config or get_config()
    conn = open_db(settings.DB_PATH)
    interpreter, explainer, closers = build_ai(settings, config, interpreter, explainer)
    dialog = Dialog(interpreter, explainer, today=today_in(config.bot.timezone),
                    consult_enabled=settings.CONSULT_CHAT_ID is not None,
                    consult_contact=config.bot.consult_contact)  # webapp_url=None: кнопки «в форме» в форме нет
    if bot is None and settings.TELEGRAM_BOT_TOKEN is not None:
        bot = Bot(settings.TELEGRAM_BOT_TOKEN.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        closers.append(bot.session.close)
    return WebappService(bot=bot, conn=conn, dialog=dialog, settings=settings, config=config,
                         extra_closers=closers)
