"""Тестовая сессия aiogram: записывает вызовы и возвращает правдоподобные объекты (НЕ живой Telegram)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list = []
        self.fail_chats: set[int] = set()
        self._ids = itertools.count(1000)

    async def close(self) -> None:  # pragma: no cover
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = method.chat_id
            if chat_id in self.fail_chats:
                raise RuntimeError("network down")
            mid = getattr(method, "message_id", None) or next(self._ids)
            return Message(message_id=mid, date=datetime.now(UTC),
                           chat=Chat(id=chat_id, type="private"), text=method.text)
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError(f"unexpected method {type(method).__name__}")

    def of(self, cls) -> list:
        return [c for c in self.calls if isinstance(c, cls)]

    def clear(self) -> None:
        self.calls.clear()


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot("123456:TESTTOKEN", session=session), session


def tg_user(uid: int = 42) -> User:
    return User(id=uid, is_bot=False, first_name="T", username="tester")


def msg_update(update_id: int, text: str, uid: int = 42, message_id: int = 1) -> Update:
    return Update(update_id=update_id, message=Message(
        message_id=message_id, date=datetime.now(UTC), chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid), text=text))


def cb_update(update_id: int, data: str, uid: int = 42, message_id: int = 500) -> Update:
    msg = Message(message_id=message_id, date=datetime.now(UTC), chat=Chat(id=uid, type="private"),
                  from_user=User(id=1, is_bot=True, first_name="bot"), text="x")
    return Update(update_id=update_id, callback_query=CallbackQuery(
        id=f"cb{update_id}", from_user=tg_user(uid), chat_instance="ci", data=data, message=msg))


def photo_update(update_id: int, uid: int = 42) -> Update:
    from aiogram.types import PhotoSize
    return Update(update_id=update_id, message=Message(
        message_id=9, date=datetime.now(UTC), chat=Chat(id=uid, type="private"),
        from_user=tg_user(uid), photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]))
