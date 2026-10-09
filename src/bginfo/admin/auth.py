"""Вход, сессия, CSRF и ограничение попыток входа.

Сессия — подписанный cookie (Starlette SessionMiddleware поверх itsdangerous
TimestampSigner): HttpOnly, SameSite=Strict, Secure по ADMIN_COOKIE_SECURE.
Кроме скользящего срока cookie, в сессии хранится момент входа ``iat``:
сессия живёт не дольше session_ttl_hours с входа.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import FormData

from bginfo.admin.common import AdminConfigError, flash, get_conn, get_state, redirect, render
from bginfo.config import Settings
from bginfo.db.conn import iso, utcnow, write_tx

log = logging.getLogger(__name__)
router = APIRouter()
_hasher = PasswordHasher()
MIN_SECRET_LEN = 32


class NeedLogin(Exception):
    """Нет действующей сессии: отправить на страницу входа."""


@dataclass
class PostCtx:
    form: FormData
    user: str


def check_settings(settings: Settings) -> None:
    """Админка не стартует без хеша пароля и секрета сессии."""
    problems = []
    if not settings.ADMIN_PASSWORD_HASH:
        problems.append("не задан ADMIN_PASSWORD_HASH (получить: python -m bginfo.ops.hash_password, "
                        "результат записать в .env)")
    secret = settings.ADMIN_SESSION_SECRET.get_secret_value() if settings.ADMIN_SESSION_SECRET else ""
    if len(secret) < MIN_SECRET_LEN:
        problems.append(f"ADMIN_SESSION_SECRET не задан или короче {MIN_SECRET_LEN} символов "
                        "(например: python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
    if not settings.ADMIN_USERNAME:
        problems.append("не задан ADMIN_USERNAME")
    if problems:
        raise AdminConfigError("Админка не запущена: " + "; ".join(problems) + ".")


def secret_key(settings: Settings) -> str:
    return settings.ADMIN_SESSION_SECRET.get_secret_value()


def client_ip(request: Request, trust_proxy: bool) -> str:
    """IP клиента. X-Forwarded-For — только за доверенным прокси; берём правый элемент
    (его добавил наш прокси), левые клиент может подделать."""
    if trust_proxy:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def verify_credentials(settings: Settings, username: str, password: str) -> bool:
    """Логин и пароль проверяются всегда оба, чтобы время ответа не выдавало верный логин."""
    user_ok = hmac.compare_digest(username.encode(), settings.ADMIN_USERNAME.encode())
    try:
        pw_ok = _hasher.verify(settings.ADMIN_PASSWORD_HASH, password)
    except (VerificationError, InvalidHashError):
        pw_ok = False
    except Exception:
        log.exception("password verification failed unexpectedly")
        pw_ok = False
    return user_ok and pw_ok


def failed_recent(conn: sqlite3.Connection, ip: str, window_min: int) -> int:
    since = iso(utcnow() - timedelta(minutes=window_min))
    return conn.execute(
        "SELECT COUNT(*) FROM admin_login_attempts WHERE ip=? AND ok=0 AND at>=?", (ip, since)
    ).fetchone()[0]


def record_attempt(conn: sqlite3.Connection, ip: str, ok: bool) -> None:
    with write_tx(conn):
        conn.execute("INSERT INTO admin_login_attempts(at, ip, ok) VALUES (?,?,?)", (iso(utcnow()), ip, int(ok)))
        conn.execute("DELETE FROM admin_login_attempts WHERE at<?", (iso(utcnow() - timedelta(days=7)),))


# --------------------------------------------------------------------------- dependencies


def ensure_csrf(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


def current_user(request: Request) -> str:
    st = get_state(request)
    s = request.session
    user = s.get("u")
    ttl = st.config.admin.session_ttl_hours * 3600
    if (
        isinstance(user, str)
        and hmac.compare_digest(user.encode(), st.settings.ADMIN_USERNAME.encode())
        and 0 <= time.time() - float(s.get("iat", 0)) <= ttl
    ):
        return user
    raise NeedLogin()


async def csrf_form(request: Request) -> FormData:
    """Разобрать форму и проверить CSRF-токен (constant-time). Без токена — 403."""
    form = await request.form(max_fields=5000, max_files=0)
    token = form.get("csrf_token")
    expected = request.session.get("csrf")
    if not isinstance(token, str) or not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(403, "Недействительный CSRF-токен: обновите страницу и повторите действие.")
    return form


def post_ctx(request: Request, form: FormData = Depends(csrf_form)) -> PostCtx:
    """CSRF уже проверен; затем требуем вход."""
    return PostCtx(form, current_user(request))


# --------------------------------------------------------------------------- routes


@router.get("/login")
def login_page(request: Request):
    ensure_csrf(request)
    return render(request, "login.html", error=None)


@router.post("/login")
def login_submit(request: Request, form: FormData = Depends(csrf_form), conn: sqlite3.Connection = Depends(get_conn)):
    st = get_state(request)
    cfg = st.config.admin
    ip = client_ip(request, cfg.trust_proxy)
    if failed_recent(conn, ip, cfg.login_window_min) >= cfg.login_max_attempts:
        return render(request, "login.html", status_code=429,
                      error=f"Слишком много неудачных попыток входа. Повторите через {cfg.login_window_min} мин.")
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    ok = verify_credentials(st.settings, username, password)
    record_attempt(conn, ip, ok)
    if not ok:
        log.warning("admin login failed from %s", ip)
        return render(request, "login.html", status_code=401, error="Неверный логин или пароль.")
    request.session.clear()  # новая сессия после входа
    request.session.update({"u": st.settings.ADMIN_USERNAME, "csrf": secrets.token_urlsafe(32), "iat": time.time()})
    return redirect("/")


@router.post("/logout")
def logout(request: Request, ctx: PostCtx = Depends(post_ctx)):
    request.session.clear()
    return redirect("/login")


__all__ = ["NeedLogin", "PostCtx", "check_settings", "csrf_form", "current_user", "flash", "post_ctx", "router"]
