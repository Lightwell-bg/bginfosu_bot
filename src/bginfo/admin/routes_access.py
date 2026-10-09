"""Страница «Доступ к админке»: host/port в config.ini и перезапуск админки."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

from fastapi import APIRouter, Depends, Request

from bginfo.admin.auth import PostCtx, current_user, post_ctx
from bginfo.admin.common import flash, get_conn, get_state, redirect, render
from bginfo.config import ADMIN_HOSTS, config_path, load_config, update_admin_access
from bginfo.db.conn import iso, utcnow, write_tx

log = logging.getLogger(__name__)
router = APIRouter()

RESTART_DELAY_S = 1.0


def _audit(conn: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
    with write_tx(conn):
        conn.execute("INSERT INTO audit_log(at, actor, action, target, detail) VALUES (?,?,?,?,?)",
                     (iso(utcnow()), actor, action, "admin_access", detail[:2000]))


def _exit_now() -> None:
    os._exit(0)


def schedule_exit(delay: float = RESTART_DELAY_S) -> None:
    """Завершить процесс через delay секунд в отдельном потоке (Docker поднимет контейнер заново)."""
    t = threading.Timer(delay, _exit_now)
    t.daemon = True
    t.start()


def _page(request: Request, status: int = 200, errors: list[str] | None = None, values: dict | None = None):
    saved = load_config(config_path()).admin
    running = get_state(request).config.admin
    return render(
        request, "access.html", status_code=status, errors=errors or [], saved=saved, running=running,
        pending=(saved.host, saved.port) != (running.host, running.port),
        values=values or {"host": saved.host, "port": str(saved.port)},
    )


@router.get("/access")
def access_page(request: Request, user: str = Depends(current_user)):
    return _page(request)


@router.post("/access")
def access_save(request: Request, p: PostCtx = Depends(post_ctx), conn: sqlite3.Connection = Depends(get_conn)):
    host = str(p.form.get("host", "")).strip()
    raw_port = str(p.form.get("port", "")).strip()
    values = {"host": host, "port": raw_port}
    errors: list[str] = []
    if host not in ADMIN_HOSTS:
        errors.append("Выберите способ доступа.")
    port = 0
    if raw_port.isascii() and raw_port.isdigit() and 1024 <= int(raw_port) <= 65535:
        port = int(raw_port)
    else:
        errors.append("Порт — целое число от 1024 до 65535.")
    if errors:
        return _page(request, 422, errors, values)
    try:
        # прямой доступ: заголовкам прокси верить нельзя
        update_admin_access(config_path(), host, port, trust_proxy=False if host == "0.0.0.0" else None)
    except (OSError, ValueError) as e:
        log.exception("update_admin_access failed")
        return _page(request, 500, [f"Не удалось сохранить config.ini: {e}"], values)
    _audit(conn, p.user, "admin_access_saved", f"host={host} port={port}")
    flash(request, "Сохранено. Применится после перезапуска админки.")
    return redirect("/access")


@router.post("/access/restart")
def access_restart(request: Request, p: PostCtx = Depends(post_ctx), conn: sqlite3.Connection = Depends(get_conn)):
    saved = load_config(config_path()).admin
    _audit(conn, p.user, "admin_restart", f"host={saved.host} port={saved.port}")
    schedule_exit()
    return render(request, "access_restart.html", ip=request.url.hostname or "IP_СЕРВЕРА", port=saved.port)
