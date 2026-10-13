"""Доставка заявки на консультацию: общая для бота и мини-аппа.

Заявка (pending) уже записана в транзакции шага; здесь только отправка и фиксация исхода.
Автоповтора нет: таймаут означает «возможно доставлено».
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramUnauthorizedError,
)

from bginfo.bot import repo
from bginfo.bot.render import split_html
from bginfo.logsafe import safe_exc

log = logging.getLogger(__name__)

CONSULT_TIMEOUT_S = 15
MSG_LIMIT = 4000

# Исходы доставки (``deliver_consult``)
DUPLICATE = "duplicate"  # по сессии заявка уже есть, ничего не отправлялось
SENT = "sent"
FAILED = "failed"  # Telegram точно отказал: можно повторить вручную
UNKNOWN = "unknown"  # возможно доставлено: повторная отправка заблокирована индексом

# Ошибки Telegram, после которых сообщение ТОЧНО не доставлено (можно повторить вручную). Всё остальное
# (таймаут, сеть, 5xx, flood-wait, неизвестное) — «возможно доставлено»: статус unknown, автоповтора нет.
NOT_DELIVERED = (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError, TelegramNotFound,
                 TelegramEntityTooLarge, TelegramMigrateToChat)


async def deliver_consult(bot: Bot, conn: Any, db: Callable[..., Awaitable[Any]], chat: int | None, html: str,
                          sid: int, cid: int | None) -> str:
    """Отправить заявку, уже записанную (pending) в транзакции шага; ``cid=None`` — заявка уже есть.

    ``db`` — корутина-обёртка над executor (``await db(fn, *args)``). Возвращает ``DUPLICATE``, ``SENT``,
    ``FAILED`` или ``UNKNOWN``; статус записывается в БД и событие ``consult_*`` добавляется здесь.
    """
    if cid is None:
        return DUPLICATE
    parts_sent = 0
    try:
        async with asyncio.timeout(CONSULT_TIMEOUT_S):
            for chunk in split_html(html, MSG_LIMIT):
                await bot.send_message(chat, chunk)
                parts_sent += 1
    except Exception as e:  # noqa: BLE001 - исход классифицируем ниже
        log.error("consult send failed: %s", safe_exc(e))
        definite = parts_sent == 0 and isinstance(e, NOT_DELIVERED)
        status = FAILED if definite else UNKNOWN
        await db(repo.mark_consult, conn, cid, status)
        await db(repo.add_event, conn, f"consult_{status}", sid, type(e).__name__)
        return status
    await db(repo.mark_consult, conn, cid, SENT)
    await db(repo.add_event, conn, "consult_sent", sid, "")
    return SENT
