"""Безопасное описание исключения для логов: тип и стек, без текста (в нём могут быть данные
провайдера или пользователя)."""

from __future__ import annotations

import traceback


def safe_exc(e: BaseException) -> str:
    """``ТипИсключения`` + кадры стека (файл/строка/функция), без сообщения и без цепочки причин."""
    return f"{type(e).__name__}\n{''.join(traceback.format_tb(e.__traceback__))}".rstrip()
