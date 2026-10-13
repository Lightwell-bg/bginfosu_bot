"""Проверка ``initData`` Telegram Mini App.

Заголовок ``Authorization: tma <initData>``. Подпись: секрет = HMAC_SHA256(key="WebAppData", msg=токен бота),
hash = HMAC_SHA256(key=секрет, msg=data_check_string). Токен и initData в логи не попадают.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

FUTURE_SKEW_S = 300  # auth_date из будущего допускается не более чем на 5 минут (расхождение часов)
MAX_INIT_DATA = 4096


class AuthError(Exception):
    """initData отсутствует, подделан или просрочен. Текст безопасен для ответа клиенту."""


@dataclass(frozen=True)
class TgUser:
    id: int
    username: str | None
    first_name: str | None
    last_name: str | None


def _opt_str(v: object) -> str | None:
    return v if isinstance(v, str) else None


def verify_init_data(init_data: str, bot_token: str, ttl_hours: float, now: float | None = None) -> TgUser:
    """Вернуть пользователя из корректного и свежего ``initData``; иначе ``AuthError``."""
    if not init_data or len(init_data) > MAX_INIT_DATA or not bot_token:
        raise AuthError("bad init data")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise AuthError("bad init data") from None
    fields: dict[str, str] = {}
    for k, v in pairs:
        if k in fields:
            raise AuthError("bad init data")
        fields[k] = v
    got_hash = fields.pop("hash", None)
    if not got_hash:
        raise AuthError("bad init data")
    # ``signature`` (если есть) остаётся в строке проверки: исключается только ``hash``
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode("ascii"), got_hash.encode("utf-8")):
        raise AuthError("bad signature")
    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError:
        raise AuthError("bad auth_date") from None
    t = time.time() if now is None else now
    if t - auth_date > ttl_hours * 3600 or auth_date - t > FUTURE_SKEW_S:
        raise AuthError("expired")
    try:
        user = json.loads(fields.get("user", ""))
        uid = user["id"]
    except (ValueError, KeyError, TypeError):
        raise AuthError("no user") from None
    if not isinstance(user, dict) or isinstance(uid, bool) or not isinstance(uid, int):
        raise AuthError("no user")
    return TgUser(uid, _opt_str(user.get("username")), _opt_str(user.get("first_name")),
                  _opt_str(user.get("last_name")))


def parse_header(value: str | None) -> str:
    """``tma <initData>`` -> initData; иначе ``AuthError``."""
    if not value or not value.startswith("tma ") or len(value) > MAX_INIT_DATA + 4:
        raise AuthError("bad header")
    return value[4:]
