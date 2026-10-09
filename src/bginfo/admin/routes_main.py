"""Главная страница."""

from __future__ import annotations

import sqlite3
from collections import Counter

from fastapi import APIRouter, Depends, Request

from bginfo.admin.auth import current_user
from bginfo.admin.common import GROUND_STATUS_RU, get_conn, get_state, render
from bginfo.db.conn import iso, utcnow
from bginfo.kb.releases import active_snapshot, diff_bundles, list_releases, load_draft

router = APIRouter()


@router.get("/")
def dashboard(request: Request, user: str = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    st = get_state(request)
    snap = active_snapshot(conn)
    draft = load_draft(conn)
    now = iso(utcnow())
    scheduled = [r for r in list_releases(conn) if r["status"] == "published" and r["effective_from"] > now]
    diff = diff_bundles(snap.bundle if snap else None, draft)
    counts = Counter(c["change"] for c in diff)
    return render(
        request, "dashboard.html", active=snap, scheduled=scheduled, diff_counts=dict(counts), dirty=bool(diff),
        demo_in_prod=bool(snap and snap.bundle.demo and st.settings.APP_MODE == "production"),
        draft_counts={k: Counter(g.status for g in draft.grounds).get(k, 0) for k in GROUND_STATUS_RU},
        active_counts={k: Counter(g.status for g in snap.bundle.grounds).get(k, 0) for k in GROUND_STATUS_RU}
        if snap else None,
    )
