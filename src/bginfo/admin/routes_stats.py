"""Статистика: события бота, расходы на AI, журнал действий."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Depends, Request

from bginfo.admin.auth import current_user
from bginfo.admin.common import get_conn, get_state, render
from bginfo.ai.ledger import AiLedger
from bginfo.db.conn import iso, utcnow

router = APIRouter()
EVENT_KINDS = [("session_started", "Начато"), ("result_shown", "Результат показан"), ("error", "Ошибки"),
               ("consult_sent", "Консультация"), ("jev_unavailable", "JEV недоступен")]
DAYS = 30


@router.get("/stats")
def stats(request: Request, user: str = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
    st = get_state(request)
    since = iso(utcnow() - timedelta(days=DAYS))
    days: dict[str, dict] = {}

    def day(d: str) -> dict:
        return days.setdefault(d, {"day": d, "events": {}, "calls": 0, "ai_errors": 0, "cost": 0.0})

    for r in conn.execute("SELECT substr(at,1,10) AS d, kind, COUNT(*) AS n FROM events WHERE at>=? GROUP BY d, kind",
                          (since,)):
        day(r["d"])["events"][r["kind"]] = r["n"]
    ledger = AiLedger(conn, st.config.limits.daily_budget_usd, {})
    for r in ledger.stats(DAYS):
        x = day(r["day"])
        x["calls"], x["ai_errors"], x["cost"] = r["calls"], r["errors"] or 0, r["cost_usd"] or 0.0
    rows = [days[d] for d in sorted(days, reverse=True)]
    totals = {"events": {k: sum(r["events"].get(k, 0) for r in rows) for k, _ in EVENT_KINDS},
              "calls": sum(r["calls"] for r in rows), "cost": sum(r["cost"] for r in rows)}
    audit = conn.execute("SELECT at, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT 50").fetchall()
    return render(request, "stats.html", rows=rows, totals=totals, event_kinds=EVENT_KINDS, audit=audit, days=DAYS)
