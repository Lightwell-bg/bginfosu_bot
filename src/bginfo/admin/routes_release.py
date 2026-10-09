"""Проверка и публикация, история выпусков, пометка «требует проверки»."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from bginfo.admin.auth import PostCtx, current_user, post_ctx
from bginfo.admin.common import flash, get_conn, get_state, redirect, render
from bginfo.db.conn import iso, utcnow
from bginfo.kb import model as m
from bginfo.kb.releases import (
    ConcurrentEdit,
    PublishError,
    active_snapshot,
    cancel_scheduled,
    diff_bundles,
    draft_revision,
    flag_needs_review,
    list_releases,
    load_draft,
    load_release,
    publish_draft,
    replace_draft,
    rollback_to,
)
from bginfo.kb.validate import Issue, validate

router = APIRouter()


def parse_local_dt(text: str, request: Request) -> datetime | None:
    """Дата и время из формы вводятся по местному времени (Europe/Sofia) и переводятся в UTC."""
    text = text.strip()
    if not text:
        return None
    dt = datetime.fromisoformat(text)  # ValueError — обрабатывает вызывающий
    return dt.replace(tzinfo=get_state(request).tz).astimezone(UTC)


def _publish_page(request: Request, conn: sqlite3.Connection, status: int = 200, errors: list[str] | None = None,
                  publish_issues: list[Issue] | None = None, form_values: dict | None = None):
    # ревизию читаем ДО черновика: если он успеет измениться, публикация откажет (безопасная сторона)
    rev = draft_revision(conn)
    draft = load_draft(conn)
    snap = active_snapshot(conn)
    issues = validate(draft)
    diff = diff_bundles(snap.bundle if snap else None, draft)
    return render(
        request, "publish.html", status_code=status, issues=issues, has_errors=any(i.level == "error" for i in issues),
        diff=diff, draft_rev=rev, active=snap, errors=errors or [], publish_issues=publish_issues or [],
        values=form_values or {},
    )


@router.get("/publish")
def publish_page(request: Request, user: str = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    return _publish_page(request, conn)


@router.post("/publish")
def publish_submit(request: Request, p: PostCtx = Depends(post_ctx), conn: sqlite3.Connection = Depends(get_conn)):
    form = p.form
    values = {"comment": str(form.get("comment", "")), "effective_from": str(form.get("effective_from", ""))}
    try:
        eff = parse_local_dt(values["effective_from"], request)
    except ValueError:
        return _publish_page(request, conn, 422, ["Неверная дата вступления в силу."], form_values=values)
    raw_rev = str(form.get("draft_revision", "")).strip()
    if not raw_rev.isdigit():
        return _publish_page(request, conn, 422, ["Не передана версия черновика: откройте страницу заново."],
                             form_values=values)
    try:
        rid = publish_draft(conn, p.user, eff, values["comment"].strip(), expected_draft_revision=int(raw_rev))
    except ConcurrentEdit as e:
        return _publish_page(request, conn, 409, [f"{e}. Проверьте изменения ещё раз и опубликуйте."],
                             form_values=values)
    except PublishError as e:
        return _publish_page(request, conn, 422, [str(e)], e.issues, values)
    flash(request, f"Выпуск №{rid} опубликован.")
    return redirect("/history")


# --------------------------------------------------------------------------- history


@router.get("/history")
def history(request: Request, user: str = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    snap = active_snapshot(conn)
    now = iso(utcnow())
    rows = []
    for r in list_releases(conn):
        if r["status"] == "cancelled":
            state = "отменён"
        elif snap and r["id"] == snap.release_id:
            state = "действует сейчас"
        elif r["effective_from"] > now:
            state = "запланирован"
        else:
            state = "заменён новым"
        rows.append({**dict(r), "state": state})
    return render(request, "history.html", rows=rows)


@router.get("/releases/{rid}")
def release_view(request: Request, rid: int, user: str = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_conn)):
    row = conn.execute("SELECT id, created_at, created_by, effective_from, status, kind, comment "
                       "FROM kb_releases WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"Выпуск №{rid} не найден")
    prev = conn.execute("SELECT id FROM kb_releases WHERE id<? AND status='published' ORDER BY id DESC LIMIT 1",
                        (rid,)).fetchone()
    bundle = load_release(conn, rid)
    old = load_release(conn, prev["id"]) if prev else None
    return render(request, "release.html", rel=dict(row), prev_id=prev["id"] if prev else None,
                  diff=diff_bundles(old, bundle), bundle=bundle)


@router.post("/history/{rid}/rollback")
def do_rollback(request: Request, rid: int, p: PostCtx = Depends(post_ctx), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        new_id = rollback_to(conn, rid, p.user, str(p.form.get("comment", "")).strip())
        flash(request, f"Откат выполнен: создан выпуск №{new_id} с содержимым выпуска №{rid}.")
    except PublishError as e:
        flash(request, "Откат не выполнен: " + str(e) + "".join(f" [{i.where}: {i.message}]" for i in e.issues[:5]), "error")
    return redirect("/history")


@router.post("/history/{rid}/cancel")
def do_cancel(request: Request, rid: int, p: PostCtx = Depends(post_ctx), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        cancel_scheduled(conn, rid, p.user)
        flash(request, f"Выпуск №{rid} отменён.")
    except PublishError as e:
        flash(request, str(e), "error")
    return redirect("/history")


@router.post("/history/{rid}/to-draft")
def to_draft(request: Request, rid: int, p: PostCtx = Depends(post_ctx), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        bundle = load_release(conn, rid)
    except KeyError:
        raise HTTPException(404, f"Выпуск №{rid} не найден") from None
    replace_draft(conn, bundle, p.user, f"загружен выпуск №{rid}")
    flash(request, f"Черновик заменён содержимым выпуска №{rid}.")
    return redirect("/publish")


# --------------------------------------------------------------------------- needs review


@router.get("/needs-review")
def needs_review_list(request: Request, user: str = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    snap = active_snapshot(conn)
    return render(request, "review_list.html", active=snap, grounds=snap.bundle.grounds if snap else [])


def _ground_or_404(conn: sqlite3.Connection, gid: str) -> m.Ground:
    snap = active_snapshot(conn)
    g = snap.bundle.ground(gid) if snap else None
    if g is None:
        raise HTTPException(404, f"Основания «{gid}» нет в действующем выпуске")
    return g


@router.get("/needs-review/{gid}")
def needs_review_form(request: Request, gid: str, user: str = Depends(current_user),
                      conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, "review_form.html", g=_ground_or_404(conn, gid), reason="", chosen=[], errors=[])


@router.post("/needs-review/{gid}")
def needs_review_submit(request: Request, gid: str, p: PostCtx = Depends(post_ctx),
                        conn: sqlite3.Connection = Depends(get_conn)):
    g = _ground_or_404(conn, gid)
    reason = str(p.form.get("reason", "")).strip()
    chosen = [v for v in p.form.getlist("requirement_ids") if isinstance(v, str)]
    try:
        rid = flag_needs_review(conn, gid, reason, p.user, chosen or None)
    except PublishError as e:
        return render(request, "review_form.html", status_code=422, g=g, reason=reason, chosen=chosen, errors=[str(e)])
    flash(request, f"Основание «{gid}» помечено «требует проверки»: создан выпуск №{rid}.")
    return redirect("/history")
