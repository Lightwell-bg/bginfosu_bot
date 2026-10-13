"""FastAPI-приложение мини-аппа: JSON API шагов анкеты + статический клиент."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bginfo.config import AppConfig, Settings, get_config, get_settings
from bginfo.engine.dialog import Event
from bginfo.webapp import auth
from bginfo.webapp.service import OK, UNAVAILABLE, WebappService, build_service

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).resolve().parent
MAX_BODY = 8 * 1024
MAX_TEXT_FIELD = 4000  # дальше «Сообщение длинное» говорит Dialog (лимит 1500); тут лишь защита от мусора
CSP = ("default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; base-uri 'self'; form-action 'none'")


class _BadRequest(Exception):
    pass


class _TooLarge(Exception):
    pass


async def _read_json(request: Request) -> dict:
    """Тело запроса ≤ 8 КБ (проверка и по Content-Length, и по факту чтения потока)."""
    cl = request.headers.get("content-length")
    if cl is not None and cl.isdigit() and int(cl) > MAX_BODY:
        raise _TooLarge
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise _TooLarge
    try:
        data = json.loads(bytes(buf) or b"{}")
    except ValueError:
        raise _BadRequest from None
    if not isinstance(data, dict):
        raise _BadRequest
    return data


def _int(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise _BadRequest
    return v


def _str(data: dict, key: str, limit: int) -> str:
    v = data.get(key, "")
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > limit:
        raise _BadRequest
    return v


def _err(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def create_app(settings: Settings | None = None, config: AppConfig | None = None, *,
               service: WebappService | None = None) -> FastAPI:
    """Создать приложение. ``service`` можно подменить (тесты)."""
    settings = settings or get_settings()
    config = config or get_config()
    token = settings.TELEGRAM_BOT_TOKEN.get_secret_value() if settings.TELEGRAM_BOT_TOKEN else ""
    svc = service or build_service(settings, config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await svc.close()

    app = FastAPI(title="BGINFO webapp", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.service = svc

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def authorize(request: Request) -> auth.TgUser:
        init = auth.parse_header(request.headers.get("authorization"))
        return auth.verify_init_data(init, token, config.webapp.init_data_ttl_hours)

    def respond(res) -> JSONResponse:
        if res.status == OK:
            return JSONResponse(res.body)
        if res.status == UNAVAILABLE:
            return JSONResponse(res.body, status_code=503)
        return JSONResponse(res.body, status_code=409)  # устаревший экран / гонка: тело = актуальный экран

    @app.post("/api/open")
    async def api_open(request: Request):
        try:
            user = authorize(request)
        except auth.AuthError:
            return _err(401, "unauthorized")
        try:
            await _read_json(request)
        except _TooLarge:
            return _err(413, "too_large")
        except _BadRequest:
            return _err(400, "bad_request")
        return respond(await svc.open(user))

    @app.post("/api/step")
    async def api_step(request: Request):
        try:
            user = authorize(request)
        except auth.AuthError:
            return _err(401, "unauthorized")
        try:
            data = await _read_json(request)
            kind = data.get("kind")
            if kind not in ("button", "text"):
                raise _BadRequest
            sid, rev = data.get("sid"), data.get("rev")
            if (sid is None) != (rev is None):
                raise _BadRequest
            ref = (_int(sid), _int(rev)) if sid is not None else None
            if kind == "button":
                ev = Event("button", action=_str(data, "action", 64), arg=_str(data, "arg", 200))
                if not ev.action:
                    raise _BadRequest
            else:
                ev = Event("text", text=_str(data, "text", MAX_TEXT_FIELD))
        except _TooLarge:
            return _err(413, "too_large")
        except _BadRequest:
            return _err(400, "bad_request")
        return respond(await svc.step(user, ev, ref))

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"ok": True})

    @app.get("/")
    def index():
        return FileResponse(PKG_DIR / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")
    return app
