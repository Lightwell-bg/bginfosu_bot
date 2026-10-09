"""Фабрика приложения админки (FastAPI + Jinja2, без сборки фронтенда)."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from bginfo.admin import (
    auth,
    routes_grounds,
    routes_kb,
    routes_main,
    routes_preview,
    routes_release,
    routes_stats,
)
from bginfo.admin.common import PKG_DIR, AdminConfigError, AdminState, make_env, redirect, render
from bginfo.config import AppConfig, Settings, get_config, get_settings
from bginfo.db.conn import open_db

log = logging.getLogger(__name__)

CSP = "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"


def create_app(settings: Settings | None = None, config: AppConfig | None = None) -> FastAPI:
    """Создать приложение. Без хеша пароля и секрета сессии — AdminConfigError."""
    settings = settings or get_settings()
    config = config or get_config()
    auth.check_settings(settings)
    try:
        tz = ZoneInfo(config.bot.timezone)
    except Exception as e:
        raise AdminConfigError(f"Неизвестный часовой пояс в config.ini [bot] timezone: {config.bot.timezone}") from e

    open_db(settings.DB_PATH).close()  # создать файл и применить миграции
    app = FastAPI(title="BGINFO admin", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.admin = AdminState(settings, config, settings.DB_PATH, make_env(tz), tz)

    app.add_middleware(
        SessionMiddleware, secret_key=auth.secret_key(settings), session_cookie="bginfo_admin",
        max_age=config.admin.session_ttl_hours * 3600, same_site="strict", https_only=settings.ADMIN_COOKIE_SECURE,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.exception_handler(auth.NeedLogin)
    async def need_login(request: Request, exc: auth.NeedLogin):
        return redirect("/login")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        try:
            return render(request, "error.html", status_code=exc.status_code, code=exc.status_code,
                          message=str(exc.detail))
        except Exception:  # noqa: BLE001 — страница ошибки не должна падать сама
            return HTMLResponse("Ошибка", status_code=exc.status_code)

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"ok": True})

    app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")
    for r in (auth.router, routes_main.router, routes_grounds.router, routes_kb.router, routes_release.router,
              routes_preview.router, routes_stats.router):
        app.include_router(r)
    return app
