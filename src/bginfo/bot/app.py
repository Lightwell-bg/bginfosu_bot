"""Тонкий Telegram-адаптер вокруг контроллера диалога (aiogram 3).

Принципы:
- один sqlite-соединение для бота; все вызовы БД идут в однопоточном executor;
- один пользователь обрабатывается последовательно (asyncio.Lock);
- middleware лишь ПРОВЕРЯЕТ (SELECT), что update_id ещё не обработан; пометка делается
  в той же транзакции, что и сохранение шага (``repo.commit_step``);
- кнопки несут ``session_id:revision:action:arg``; на устаревшую кнопку бот отвечает алертом и присылает
  актуальный шаг (``resume_view``), чтобы у пользователя всегда были живые кнопки;
- новая сессия (/start, /restart) создаётся внутри транзакции шага вместе с пометкой update_id;
- ответы отправляются только после успешного коммита шага;
- в логи не попадают ни тексты пользователей, ни токены.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import html as htmllib
import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message, Update

from bginfo.ai.explain import Explainer
from bginfo.ai.interpret import Interpreter
from bginfo.ai.ledger import AiLedger
from bginfo.ai.openrouter import OpenRouterClient
from bginfo.bot import repo
from bginfo.bot.render import keyboard, split_html
from bginfo.config import AppConfig, Settings, get_config, get_settings
from bginfo.db.conn import open_db
from bginfo.engine.dialog import RESULT_BUTTONS, Dialog, Event, Out, SessionData, UserCtx, today_in
from bginfo.kb.releases import Snapshot, active_snapshot
from bginfo.logsafe import safe_exc

log = logging.getLogger(__name__)

CLEANUP_PERIOD_S = 6 * 3600
CONSULT_TIMEOUT_S = 15
MSG_LIMIT = 4000

TXT_STALE = "Эта кнопка устарела"
TXT_NO_SESSION = "Анкета не найдена. Отправьте /start, чтобы начать."
TXT_CONFLICT = "Анкета изменилась в другом окне, повторите"
TXT_ERROR = "Произошла ошибка, попробуйте ещё раз"
TXT_MEDIA = "Отвечайте кнопками или текстом. Не присылайте документы и фото."
TXT_HELP = (
    "Я помогаю подобрать основание для проживания в Болгарии.\n\n"
    "/start — начать или продолжить анкету\n"
    "/restart — начать заново\n"
    "/result — показать актуальный результат\n"
    "/delete — удалить мои данные\n"
    "/help — эта справка\n\n"
    "Отвечайте кнопками или своими словами."
)
TXT_CONSULT_DUP = "Заявка уже отправлена"
TXT_CONSULT_OK = "Заявка отправлена"
TXT_CONSULT_FAIL = "Не удалось отправить, попробуйте позже"
TXT_CONSULT_UNKNOWN = "Не удалось подтвердить отправку. Возможно, заявка дошла — консультант свяжется."
TXT_CONSULT_UNKNOWN_CONTACT = " Если ответа нет, напишите: "
TXT_CONSULT_OFF = "Онлайн-заявка на консультацию пока не настроена."

# Ошибки Telegram, после которых сообщение ТОЧНО не доставлено (можно повторить вручную). Всё остальное
# (таймаут, сеть, 5xx, flood-wait, неизвестное) — «возможно доставлено»: статус unknown, автоповтора нет.
_NOT_DELIVERED = (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError, TelegramNotFound,
                  TelegramEntityTooLarge, TelegramMigrateToChat)

# Состояние обработки текущего update (ставит middleware, читают run/_run_locked):
# {"id": update_id, "done": шаг закоммичен вместе с пометкой update_id, "failed": обработчик упал}.
_STEP: contextvars.ContextVar[dict | None] = contextvars.ContextVar("bginfo_step", default=None)


class BotApp:
    """Сборка бота: Bot + Dispatcher + БД + контроллер. Создаётся ``build_app``."""

    def __init__(self, *, bot: Bot, conn, dialog: Dialog, settings: Settings, config: AppConfig,
                 extra_closers: list[Callable[[], Any]] | None = None):
        self.bot = bot
        self.conn = conn
        self.dialog = dialog
        self.settings = settings
        self.config = config
        self._db_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bot-db")
        self._locks: dict[int, list] = {}  # uid -> [Lock, число ожидающих/держащих]
        self._closers = extra_closers or []
        self.dp = Dispatcher()
        self.router = Router()
        self._register()
        self.dp.include_router(self.router)

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

    # ------------------------------------------------------------------ registration
    def _register(self) -> None:
        r = self.router
        r.message.filter(F.chat.type == "private")

        @self.dp.update.outer_middleware()
        async def dedupe(handler, event: Update, data: dict):
            if await self.db(repo.is_update_processed, self.conn, event.update_id):
                log.info("duplicate update skipped")
                return None
            step = {"id": event.update_id, "done": False, "failed": False}
            token = _STEP.set(step)
            try:
                result = await handler(event, data)
            finally:
                _STEP.reset(token)
            if not step["done"] and not step["failed"]:
                # Шаг не писал в БД (/help, вложения, устаревшая кнопка, конфликт): пометить отдельно.
                await self.db(repo.mark_update, self.conn, event.update_id)
            return result

        @r.message(Command("start"))
        async def _start(m: Message):
            await self.run(m.from_user, Event("start"), message=m)

        @r.message(Command("restart"))
        async def _restart(m: Message):
            await self.run(m.from_user, Event("start"), message=m, fresh=True)

        @r.message(Command("result"))
        async def _result(m: Message):
            await self.run(m.from_user, Event("command", action="result"), message=m)

        @r.message(Command("delete"))
        async def _delete(m: Message):
            await self.run(m.from_user, Event("command", action="delete"), message=m)

        @r.message(Command("help"))
        async def _help(m: Message):
            await self.bot.send_message(m.chat.id, TXT_HELP)

        @r.message(F.text)
        async def _text(m: Message):
            await self.run(m.from_user, Event("text", text=m.text or ""), message=m)

        @r.message()
        async def _other(m: Message):
            # Вложения не скачиваем и не сохраняем.
            await self.bot.send_message(m.chat.id, TXT_MEDIA)

        @r.callback_query()
        async def _cb(c: CallbackQuery):
            parsed = _parse_callback(c.data or "")
            if parsed is None:
                # непонятные данные кнопки: тот же путь, что у устаревшей (алерт + актуальный шаг)
                await self.run(c.from_user, Event("button", action="resume_view"), callback=c, ref=(-1, -1))
                return
            sid, rev, action, arg = parsed
            await self.run(c.from_user, Event("button", action=action, arg=arg), callback=c, ref=(sid, rev))

    # ------------------------------------------------------------------ DB steps (executor)
    def _load(self, uid: int, ref: tuple[int, int] | None, fresh: bool, create: bool):
        """(session, user_ctx_flag, snapshot, status, is_new). status: ok | stale.

        Новая сессия здесь только «временная» (``repo.blank_session``): в БД её вставит ``commit_step``.
        """
        u = repo.touch_user(self.conn, uid)
        s = repo.active_session(self.conn, uid)
        is_new = False
        if ref is not None:
            if s is None or s.id != ref[0] or s.revision != ref[1]:
                return None, u, None, "stale", False
        elif fresh or (s is None and create):
            s = repo.blank_session()
            is_new = True
        snap = active_snapshot(self.conn)
        return s, u, snap, "ok", is_new

    def _commit(self, uid: int, s: SessionData, expected: int, bump: bool, out: Out, update_id: int | None,
                new_session: bool, replaces: tuple[int, int] | None, consult_html: str | None,
                info: dict) -> str:
        """Одна транзакция на весь шаг: ``"ok"`` | ``"duplicate"`` | ``"conflict"`` (см. ``repo.commit_step``)."""
        return repo.commit_step(
            self.conn, update_id=update_id, tg_user_id=uid, session=s, expected_revision=expected, bump=bump,
            result_record=out.result_record, events=list(out.events), ai_notice=out.ai_notice_shown,
            delete_user=out.delete_user, new_session=new_session, replaces=replaces,
            consult_html=consult_html, info=info)

    # ------------------------------------------------------------------ main flow
    async def run(self, tg_user, ev: Event, *, message: Message | None = None,
                  callback: CallbackQuery | None = None, ref: tuple[int, int] | None = None,
                  fresh: bool = False) -> None:
        uid = tg_user.id
        chat_id = uid  # бот работает только в личных чатах
        async with self.user_lock(uid):
            try:
                await self._run_locked(uid, tg_user.username, chat_id, ev, message, callback, ref, fresh)
            except Exception as e:  # noqa: BLE001
                # только тип и стек: текст исключения может содержать данные пользователя или провайдера
                log.error("handler error: %s", safe_exc(e))
                step = _STEP.get()
                if step is not None:
                    step["failed"] = True
                with contextlib.suppress(Exception):
                    await self.db(repo.add_event, self.conn, "error", None, type(e).__name__)
                with contextlib.suppress(Exception):
                    await self.bot.send_message(chat_id, TXT_ERROR)
                with contextlib.suppress(Exception):
                    await self._answer(callback, None)

    async def _run_locked(self, uid, username, chat_id, ev, message, callback, ref, fresh) -> None:
        """Один шаг диалога.

        Порядок: загрузка (SELECT; заодно touch_user/new_session) -> ``dialog.handle`` (в памяти, без
        записи) -> ОДНА транзакция ``commit_step`` (update_id, сессия с проверкой ревизии, результат,
        события, AI-уведомление, удаление пользователя) -> и только затем отправка сообщений.

        Если процесс упал или ``commit_step`` бросил исключение до коммита, транзакция откатывается:
        update_id НЕ помечен, ничего не сохранено и ничего не отправлено. Telegram повторит update, и
        он обработается заново; повторное применение безопасно, потому что состояние защищено
        ревизией сессии (устаревшая кнопка отклонится как stale, а не применится дважды). Дубль уже
        закоммиченного update отбрасывается: сначала SELECT в middleware, а при гонке — отказ вставки
        update_id в самой транзакции (откат, тихий выход).
        """
        step = _STEP.get()
        update_id = step["id"] if step is not None else None
        create = callback is None
        s, u, snap, status, is_new = await self.db(self._load, uid, ref, fresh, create)
        if status == "stale":
            # Алерт + актуальный шаг обычным шагом (resume_view), чтобы у пользователя остались живые кнопки,
            # даже если ответ на прежнее сообщение не дошёл. Старое сообщение не правим (callback=None).
            await self._answer(callback, TXT_STALE, alert=True)
            callback = None
            ev = Event("button", action="resume_view")
            s, u, snap, status, is_new = await self.db(self._load, uid, None, False, False)
            if s is None:
                await self.bot.send_message(chat_id, TXT_NO_SESSION)
                return
        user = UserCtx(uid, username, u["ai_notice_shown"])
        before = _fingerprint(s)
        expected = s.revision
        replaces: tuple[int, int] | None = None
        out = await self.dialog.handle(s, ev, snap, user)
        if out.restart:
            replaces = (s.id, s.revision) if s.id is not None else None
            s, is_new, expected, before = repo.blank_session(), True, 0, None
            out = await self.dialog.handle(s, Event("start"), snap, user)
        bump = (before is None or _fingerprint(s) != before or out.result_record is not None
                or out.consult is not None or out.delete_user)
        consult_html = None
        if out.consult is not None and self.settings.CONSULT_CHAT_ID is not None:
            consult_html = out.consult.get("html", "")
        info: dict = {}
        status = await self.db(self._commit, uid, s, expected, bump, out, update_id, is_new, replaces,
                               consult_html, info)
        if status == "duplicate":
            log.info("duplicate update skipped at commit")
            if step is not None:
                step["done"] = True
            await self._answer(callback, None)
            return
        if status != "ok":
            await self._answer(callback, None)
            await self.bot.send_message(chat_id, TXT_CONFLICT)
            return
        if step is not None:
            step["done"] = True
        rev = s.revision + (1 if bump else 0)
        await self._send(out, chat_id, callback, s.id, rev)
        if out.consult is not None:
            if consult_html is None:
                await self._consult_say(TXT_CONSULT_OFF, chat_id, s.id, rev)
            else:
                await self._consult(consult_html, uid, chat_id, s.id, rev, info.get("consult_id"))

    # ------------------------------------------------------------------ sending
    async def _answer(self, callback: CallbackQuery | None, text: str | None, alert: bool = False) -> None:
        if callback is None:
            return
        with contextlib.suppress(Exception):
            await self.bot.answer_callback_query(callback.id, text=text, show_alert=alert)

    async def _send(self, out: Out, chat_id: int, callback: CallbackQuery | None, sid: int, rev: int) -> None:
        for reply in out.replies:
            markup = keyboard(reply.buttons, sid, rev) if reply.buttons else None
            msg = callback.message if callback is not None else None
            lpo = LinkPreviewOptions(is_disabled=True) if reply.no_preview else None
            if reply.edit and msg is not None and len(reply.html) <= MSG_LIMIT:
                try:
                    await self.bot.edit_message_text(reply.html, chat_id=chat_id, message_id=msg.message_id,
                                                     reply_markup=markup, link_preview_options=lpo)
                except TelegramBadRequest as e:
                    if "message is not modified" not in str(e):
                        raise
                continue
            chunks = split_html(reply.html, MSG_LIMIT)
            for i, chunk in enumerate(chunks):
                last = i == len(chunks) - 1
                await self.bot.send_message(chat_id, chunk, reply_markup=markup if last else None,
                                            link_preview_options=lpo)
        if out.alert:
            if callback is not None:
                await self._answer(callback, out.alert)
                return
            await self.bot.send_message(chat_id, out.alert)
        await self._answer(callback, None)

    async def _consult_say(self, text: str, chat_id: int, sid: int, rev: int) -> None:
        await self.bot.send_message(chat_id, text, reply_markup=keyboard(RESULT_BUTTONS, sid, rev))

    def _consult_unknown_text(self) -> str:
        contact = self.config.bot.consult_contact.strip()
        if not contact:
            return TXT_CONSULT_UNKNOWN
        return TXT_CONSULT_UNKNOWN + TXT_CONSULT_UNKNOWN_CONTACT + htmllib.escape(contact)

    async def _consult(self, html: str, uid: int, chat_id: int, sid: int, rev: int, cid: int | None) -> None:
        """Отправить заявку, уже записанную (pending) в транзакции шага; ``cid=None`` — заявка уже есть.

        Исходы: sent; failed (Telegram точно отказал, ничего не доставлено: можно повторить вручную);
        unknown (таймаут, сеть, flood-wait, неизвестная ошибка или хотя бы одна часть уже ушла: возможно
        доставлено, повторная отправка заблокирована индексом).
        """
        chat = self.settings.CONSULT_CHAT_ID
        if cid is None:
            await self._consult_say(TXT_CONSULT_DUP, chat_id, sid, rev)
            return
        parts_sent = 0
        try:
            async with asyncio.timeout(CONSULT_TIMEOUT_S):
                for chunk in split_html(html, MSG_LIMIT):
                    await self.bot.send_message(chat, chunk)
                    parts_sent += 1
        except Exception as e:  # noqa: BLE001 - исход классифицируем ниже
            log.error("consult send failed: %s", safe_exc(e))
            definite = parts_sent == 0 and isinstance(e, _NOT_DELIVERED)
            status = "failed" if definite else "unknown"
            await self.db(repo.mark_consult, self.conn, cid, status)
            await self.db(repo.add_event, self.conn, f"consult_{status}", sid, type(e).__name__)
            await self._consult_say(TXT_CONSULT_FAIL if definite else self._consult_unknown_text(),
                                    chat_id, sid, rev)
            return
        await self.db(repo.mark_consult, self.conn, cid, "sent")
        await self.db(repo.add_event, self.conn, "consult_sent", sid, "")
        await self._consult_say(TXT_CONSULT_OK, chat_id, sid, rev)

    # ------------------------------------------------------------------ background
    async def cleanup_loop(self) -> None:
        days = self.config.privacy.retention_days
        while True:
            try:
                await self.db(repo.cleanup, self.conn, days)
            except Exception as e:  # noqa: BLE001
                log.error("cleanup failed: %s", type(e).__name__)
            await asyncio.sleep(CLEANUP_PERIOD_S)


def _fingerprint(s: SessionData) -> str:
    return json.dumps([s.state, s.profile.to_dict(), s.ui, s.release_id], sort_keys=True, ensure_ascii=False)


def _parse_callback(data: str) -> tuple[int, int, str, str] | None:
    parts = data.split(":", 3)
    if len(parts) != 4:
        return None
    try:
        return int(parts[0]), int(parts[1]), parts[2], parts[3]
    except ValueError:
        return None


# --------------------------------------------------------------------------- assembly


def build_app(settings: Settings | None = None, config: AppConfig | None = None, *, bot: Bot | None = None,
              interpreter=None, explainer=None) -> BotApp:
    """Собрать приложение. ``bot``, ``interpreter`` и ``explainer`` можно подменить (тесты)."""
    settings = settings or get_settings()
    config = config or get_config()
    conn = open_db(settings.DB_PATH)
    closers: list[Callable[[], Any]] = []
    if interpreter is None or explainer is None:
        client = None
        if settings.OPENROUTER_API_KEY is not None:
            ai = config.ai
            client = OpenRouterClient(settings.OPENROUTER_API_KEY.get_secret_value(), base_url=ai.base_url,
                                      timeout_s=ai.timeout_s, retries=ai.retries, app_title=ai.app_title,
                                      http_referer=ai.http_referer)
            closers.append(client.aclose)
        # Отдельное соединение: Interpreter вызывает ledger синхронно из event loop, а
        # основное соединение занято executor-потоком (транзакции не должны перемешиваться).
        ledger_conn = open_db(settings.DB_PATH)
        closers.append(ledger_conn.close)
        lim = config.limits
        ledger = AiLedger(ledger_conn, lim.daily_budget_usd,
                          {"jev": lim.user_text_per_day, "extract": lim.user_text_per_day,
                           "explain": lim.user_explain_per_day},
                          unknown_cost_usd=lim.unknown_cost_usd)
        interpreter = interpreter or Interpreter(
            client, ledger, jev_model=config.ai.jev_model, llm_model=config.ai.llm_model,
            recognize_at=config.ai.jev_recognize_threshold, negate_at=config.ai.jev_negation_threshold)
        explainer = explainer or Explainer(client, ledger, model=config.ai.llm_model,
                                           max_tokens=config.ai.explain_max_tokens)
    dialog = Dialog(interpreter, explainer, today=today_in(config.bot.timezone),
                    consult_enabled=settings.CONSULT_CHAT_ID is not None,
                    consult_contact=config.bot.consult_contact)
    if bot is None:
        token = settings.TELEGRAM_BOT_TOKEN.get_secret_value() if settings.TELEGRAM_BOT_TOKEN else ""
        bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return BotApp(bot=bot, conn=conn, dialog=dialog, settings=settings, config=config, extra_closers=closers)


def demo_release_in_production(settings: Settings, snap: Snapshot | None) -> bool:
    return settings.APP_MODE == "production" and snap is not None and snap.bundle.demo


async def main() -> None:
    settings = get_settings()
    if settings.TELEGRAM_BOT_TOKEN is None:
        log.error("TELEGRAM_BOT_TOKEN is not set; see .env.example")
        return
    app = build_app(settings)
    cleanup_task: asyncio.Task | None = None
    try:
        snap = await app.db(active_snapshot, app.conn)
        if demo_release_in_production(settings, snap):
            log.error("Active knowledge release is marked demo while APP_MODE=production; polling not started")
            return
        info = await app.bot.get_webhook_info()
        if info.url:
            log.error("A webhook is configured for this bot, polling would conflict with it. "
                      "See docs/SWITCHOVER.md")
            return
        cleanup_task = asyncio.create_task(app.cleanup_loop())
        await app.dp.start_polling(app.bot, allowed_updates=["message", "callback_query"])
    finally:
        if cleanup_task:
            cleanup_task.cancel()
        with contextlib.suppress(Exception):
            await app.bot.session.close()
        await app.close()

