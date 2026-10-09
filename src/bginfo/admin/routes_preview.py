"""Тестовая анкета: прогон движка по введённым фактам и предпросмотр сообщения бота."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.datastructures import FormData

from bginfo.admin.auth import PostCtx, current_user, post_ctx
from bginfo.admin.common import get_conn, get_state, render
from bginfo.engine.evaluate import STATUS_LABELS, evaluate_all
from bginfo.engine.facts import CASE, MAIN, Participant, Profile
from bginfo.engine.present import render_result
from bginfo.engine.result import build_result
from bginfo.kb import model as m
from bginfo.kb.releases import active_snapshot, load_draft

router = APIRouter()
SPOUSE = "p1"


def _bundle(conn: sqlite3.Connection, source: str) -> tuple[m.Bundle | None, int | None, str]:
    if source == "active":
        snap = active_snapshot(conn)
        return (snap.bundle, snap.release_id, "действующий выпуск") if snap else (None, None, "действующий выпуск")
    return load_draft(conn), None, "черновик"


def _value(form: FormData, name: str, fd: m.FactDef) -> Any:
    """Значение поля анкеты или None (пусто = факт не выяснен). Ошибка формата — ValueError."""
    if fd.type == "multi":
        vals = [v for v in form.getlist(name) if isinstance(v, str) and v]
        allowed = {o.value for o in fd.options}
        if any(v not in allowed for v in vals):
            raise ValueError("недопустимый вариант")
        if vals:
            return vals
        return [] if form.get(f"{name}-none") else None
    raw = str(form.get(name, "") or "").strip()
    if not raw:
        return None
    if fd.type == "bool":
        if raw not in ("yes", "no"):
            raise ValueError("ожидается да/нет")
        return raw == "yes"
    if fd.type == "enum":
        if raw not in {o.value for o in fd.options}:
            raise ValueError("недопустимый вариант")
        return raw
    if fd.type == "money":
        return float(raw.replace(",", "."))
    if fd.type == "int":
        return int(raw)
    if fd.type == "date":
        return date.fromisoformat(raw).isoformat()
    return raw


def build_profile(form: FormData, kb: m.Bundle, with_spouse: bool) -> tuple[Profile, list[str]]:
    profile = Profile()
    if with_spouse:
        profile.participants[SPOUSE] = Participant(SPOUSE, "spouse", "Супруг(а)")
    errors: list[str] = []
    for fd in kb.facts:
        pids = [CASE] if fd.scope == "case" else [MAIN] + ([SPOUSE] if with_spouse else [])
        for pid in pids:
            name = f"f-{pid}-{fd.key}"
            if form.get(f"{name}-unk"):
                profile.mark_unknown(pid, fd.key)
                continue
            try:
                v = _value(form, name, fd)
            except ValueError as e:
                errors.append(f"{fd.label} ({'супруг(а)' if pid == SPOUSE else 'вы' if pid == MAIN else 'анкета'}): {e}")
                continue
            if v is not None:
                profile.confirm_value(pid, fd.key, v, "button", None)
    return profile, errors


def _page(request: Request, conn: sqlite3.Connection, source: str, form: FormData, result: dict | None = None,
          errors: list[str] | None = None, status: int = 200):
    kb, _, source_label = _bundle(conn, source)
    st = get_state(request)
    return render(
        request, "preview.html", status_code=status, kb=kb, source=source, source_label=source_label, form=form,
        result=result, errors=errors or [], with_spouse=bool(form.get("with_spouse")),
        as_of=str(form.get("as_of") or datetime.now(st.tz).date().isoformat()),
        person_facts=[f for f in kb.facts if f.scope == "person"] if kb else [],
        case_facts=[f for f in kb.facts if f.scope == "case"] if kb else [],
    )


@router.get("/preview")
def preview_page(request: Request, source: str = "draft", user: str = Depends(current_user),
                 conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn, "active" if source == "active" else "draft", FormData())


@router.post("/preview")
def preview_run(request: Request, p: PostCtx = Depends(post_ctx), conn: sqlite3.Connection = Depends(get_conn)):
    form = p.form
    source = "active" if form.get("source") == "active" else "draft"
    kb, release_id, _ = _bundle(conn, source)
    if kb is None:
        return _page(request, conn, source, form, errors=["Действующего выпуска ещё нет."], status=422)
    try:
        as_of = date.fromisoformat(str(form.get("as_of", "")).strip())
    except ValueError:
        return _page(request, conn, source, form, errors=["Неверная дата расчёта."], status=422)
    profile, errors = build_profile(form, kb, bool(form.get("with_spouse")))
    if errors:
        return _page(request, conn, source, form, errors=errors, status=422)
    ev = evaluate_all(kb, profile, as_of, release_id)
    rv = build_result(ev, kb, profile)
    rows = []
    for r in ev.results:
        g = kb.ground(r.ground_id)
        who = profile.participants[r.subject].label if r.subject in profile.participants else r.subject
        rows.append({
            "who": who, "ground": g.title if g else r.ground_id, "ground_id": r.ground_id,
            "status": r.status.value, "status_label": STATUS_LABELS[r.status], "reasons": r.reasons,
            "missing": [(kb.fact(k).label if kb.fact(k) else k) + ("" if pid in (MAIN, CASE) else " (супруг(а))")
                        for pid, k in r.missing],
            "reqs": [(q.label, q.result.value, q.disputed) for q in r.requirements],
        })
    result = {"rows": rows, "html": render_result(rv), "kind": rv.kind, "as_of": as_of}
    return _page(request, conn, source, form, result)
