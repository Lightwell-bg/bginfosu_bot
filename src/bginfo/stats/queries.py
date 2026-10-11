"""Подсчёты статистики для админки: чистые функции над ``sqlite3.Connection`` (только чтение).

Период задаётся ``since`` (UTC; None — всё время). Данные берутся из обезличенной
``stats_results`` и из служебных таблиц (events, consult_requests, ai_calls, users, sessions).
Разбор JSON — в Python; на каждую метрику — один запрос, без N+1.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from bginfo.db.conn import iso

DIMENSIONS = ("who", "citizenship", "income", "ties", "goal", "family")
LIST_DIMENSIONS = ("income", "ties")
NO_QID_LABEL = "до первого вопроса"
DEFAULT_TZ = "Europe/Sofia"
ABANDON_AFTER_HOURS = 24  # активная сессия без результата старше этого срока считается брошенной


def _since(since: datetime | None) -> str:
    return iso(since) if since else ""


def _since_day(since: datetime | None, tz: str = DEFAULT_TZ) -> str:
    """Начало периода как дата YYYY-MM-DD в часовом поясе ``tz`` (stats_results хранит только день)."""
    return since.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d") if since else ""


def _div(a: float, b: float) -> float:
    return a / b if b else 0


def _top(counter: Counter, total: int) -> dict | None:
    """Самый частый элемент: {"ground_id", "n", "share"} (share от total) или None."""
    if not counter:
        return None
    gid, n = min(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return {"ground_id": gid, "n": n, "share": _div(n, total)}


def overview(conn: sqlite3.Connection, since: datetime | None, tz: str = DEFAULT_TZ) -> dict:
    """Общие показатели за период.

    {"new_users", "started" (различные session_id с session_started), "completed" (из них имеют строку в
     results — когорта, данные в пределах срока хранения), "conversion" (completed/started, 0 при 0),
     "results_total" (строк stats_results за период), "recalculations" (max(0, results_total - completed)),
     "consults": {"sent", "unknown", "failed"}, "errors", "jev_unavailable", "ai_calls",
     "ai_cost_usd" (float), "llm_share" (0..1), "by_kind": {kind: n}};
    синонимы "sessions_started" = started и "results" = results_total.
    """
    s = _since(since)
    d = _since_day(since, tz)
    new_users = conn.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (s,)).fetchone()[0]
    ev = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) n FROM events WHERE at >= ? AND kind IN ('error','jev_unavailable') GROUP BY kind",
        (s,))}
    cohort = conn.execute(
        "SELECT COUNT(*) started, COALESCE(SUM(EXISTS(SELECT 1 FROM results r WHERE r.session_id = x.session_id)), 0)"
        " completed FROM (SELECT DISTINCT session_id FROM events WHERE kind='session_started' AND at >= ? "
        "AND session_id IS NOT NULL) x", (s,)).fetchone()
    started, completed = cohort["started"], cohort["completed"]
    by_kind = {r["result_kind"]: r["n"] for r in conn.execute(
        "SELECT result_kind, COUNT(*) n FROM stats_results WHERE day >= ? GROUP BY result_kind", (d,))}
    llm = conn.execute("SELECT COUNT(*) FROM stats_results WHERE day >= ? AND explain_source='llm'", (d,)).fetchone()[0]
    consults = {"sent": 0, "unknown": 0, "failed": 0}
    for r in conn.execute("SELECT status, COUNT(*) n FROM consult_requests WHERE created_at >= ? GROUP BY status", (s,)):
        if r["status"] in consults:
            consults[r["status"]] = r["n"]
    ai = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(cost_usd), 0) c FROM ai_calls "
                      "WHERE at >= ? AND status='done'", (s,)).fetchone()
    results_total = sum(by_kind.values())
    return {
        "new_users": new_users, "started": started, "completed": completed, "sessions_started": started,
        "conversion": _div(completed, started), "results_total": results_total, "results": results_total,
        "recalculations": max(0, results_total - completed), "consults": consults,
        "errors": ev.get("error", 0), "jev_unavailable": ev.get("jev_unavailable", 0),
        "ai_calls": ai["n"], "ai_cost_usd": float(ai["c"] or 0),
        "llm_share": _div(llm, results_total), "by_kind": by_kind,
    }


def history_start(conn: sqlite3.Connection, since: datetime | None, tz: str = DEFAULT_TZ) -> str | None:
    """Дата (YYYY-MM-DD), с которой собираются разрезы по ситуациям, если за период есть более ранние результаты.

    Нужна для пометки «разрезы собираются с …»: возвращает None, когда stats_results покрывает весь период
    (или в периоде нет результатов вообще). Если stats_results пуст, а results есть — берётся день самого
    раннего результата в периоде.
    """
    first = conn.execute("SELECT MIN(day) FROM stats_results").fetchone()[0]
    s = _since(since)
    old = conn.execute("SELECT MIN(created_at) FROM results WHERE created_at >= ?", (s,)).fetchone()[0]
    if old is None:
        return None
    if first is None:
        return _day_of(old, tz)
    # самый ранний результат периода в локальном дне раньше первой строки stats_results
    return first if _day_of(old, tz) < first else None


def _day_of(at: str, tz: str) -> str:
    return datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def daily(conn: sqlite3.Connection, since: datetime | None, tz: str = DEFAULT_TZ) -> list[dict]:
    """По дням (в часовом поясе ``tz``), по возрастанию даты.

    [{"day": "YYYY-MM-DD", "started", "results", "consults", "errors", "ai_cost"}]; дни без событий пропущены.
    """
    zone = ZoneInfo(tz)
    days: dict[str, dict] = defaultdict(lambda: {"started": 0, "results": 0, "consults": 0, "errors": 0,
                                                 "ai_cost": 0.0})

    def day_of(at: str) -> str:
        return datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(zone).strftime("%Y-%m-%d")

    rows = conn.execute(
        "SELECT 'started' k, at, 0.0 v, NULL d FROM events WHERE kind='session_started' AND at >= :s "
        "UNION ALL SELECT 'errors', at, 0.0, NULL FROM events WHERE kind='error' AND at >= :s "
        "UNION ALL SELECT 'results', NULL, 0.0, day FROM stats_results WHERE day >= :d "
        "UNION ALL SELECT 'consults', created_at, 0.0, NULL FROM consult_requests WHERE created_at >= :s "
        "UNION ALL SELECT 'ai_cost', at, COALESCE(cost_usd, 0), NULL FROM ai_calls WHERE status='done' AND at >= :s",
        {"s": _since(since), "d": _since_day(since, tz)})
    for r in rows:
        d = days[r["d"] or day_of(r["at"])]
        if r["k"] == "ai_cost":
            d["ai_cost"] += float(r["v"])
        else:
            d[r["k"]] += 1
    return [{"day": day, **days[day]} for day in sorted(days)]


def dimensions_and_grounds(conn: sqlite3.Connection, since: datetime | None, tz: str = DEFAULT_TZ,
                           limit: int = 30) -> dict:
    """Все разрезы за ОДИН проход по stats_results периода (каждый JSON разбирается один раз).

    {"dimensions": {dim: [строки by_dimension]}, "grounds": [строки by_ground],
     "situations": [строки situations] (не более ``limit``, после подсчёта)}.
    """
    d_total = 0
    d_n: dict[str, Counter] = {dim: Counter() for dim in DIMENSIONS}
    d_prim: dict[str, dict] = {dim: defaultdict(Counter) for dim in DIMENSIONS}
    d_kinds: dict[str, dict] = {dim: defaultdict(Counter) for dim in DIMENSIONS}
    evaluated: Counter = Counter()
    statuses: dict[str, Counter] = defaultdict(Counter)
    primary: Counter = Counter()
    n: Counter = Counter()
    sit: dict = {}
    prim: dict = defaultdict(Counter)
    kinds: dict = defaultdict(Counter)
    fam: Counter = Counter()
    for r in conn.execute("SELECT situation_key, situation, primary_ground, result_kind, members, grounds "
                          "FROM stats_results WHERE day >= ?", (_since_day(since, tz),)):
        d_total += 1
        pg, kind, key = r["primary_ground"], r["result_kind"], r["situation_key"]
        situation = json.loads(r["situation"])
        for dim in DIMENSIONS:
            for v in _dim_values(situation, dim):
                d_n[dim][v] += 1
                d_kinds[dim][v][kind] += 1
                if pg:
                    d_prim[dim][v][pg] += 1
        for gid, st in json.loads(r["grounds"]).items():
            evaluated[gid] += 1
            statuses[gid][st] += 1
        if pg:
            primary[pg] += 1
            prim[key][pg] += 1
        n[key] += 1
        sit.setdefault(key, situation)
        kinds[key][kind] += 1
        if r["members"] > 0:
            fam[key] += 1
    dimensions = {
        dim: [{"value": v, "n": c, "share": _div(c, d_total), "top_primary": _top(d_prim[dim][v], c),
               "kinds": dict(d_kinds[dim][v])}
              for v, c in sorted(d_n[dim].items(), key=lambda kv: (-kv[1], str(kv[0])))]
        for dim in DIMENSIONS}
    grounds = sorted(
        ({"ground_id": g, "evaluated": evaluated[g], "statuses": dict(statuses[g]), "primary": primary[g]}
         for g in set(evaluated) | set(primary)), key=lambda x: (-x["evaluated"], x["ground_id"]))
    top = sorted(n.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    sits = [{"key": k, "n": c, "situation": sit[k], "top_primary": _top(prim[k], c), "kinds": dict(kinds[k]),
             "members_share": _div(fam[k], c)} for k, c in top]
    return {"dimensions": dimensions, "grounds": grounds, "situations": sits}


def by_ground(conn: sqlite3.Connection, since: datetime | None, tz: str = DEFAULT_TZ) -> list[dict]:
    """По основаниям: [{"ground_id", "evaluated", "statuses": {status: n}, "primary"}], по убыванию evaluated."""
    return dimensions_and_grounds(conn, since, tz)["grounds"]


def _dim_values(sit: dict, dim: str) -> list:
    v = sit.get(dim)
    if dim in LIST_DIMENSIONS:
        if v is None:
            return ["unknown"]
        return list(v) or ["none"]
    if dim == "family":
        return [bool(v)]
    return [v if v else "unknown"]


def by_dimension(conn: sqlite3.Connection, since: datetime | None, dim: str, tz: str = DEFAULT_TZ) -> list[dict]:
    """Разрез по признаку ``dim`` (who, citizenship, income, ties, goal, family).

    [{"value", "n", "share", "top_primary", "kinds"}], по убыванию n. ``share`` = n / число результатов за период
    (для income/ties строки пересекаются, сумма долей может быть > 1; "none" = пустой список, "unknown" =
    неизвестно). ``family``: value True/False. ``top_primary`` = {"ground_id", "n", "share"} (доля внутри строки)
    или None. ``kinds`` = {result_kind: n}.
    """
    if dim not in DIMENSIONS:
        raise ValueError(dim)
    return dimensions_and_grounds(conn, since, tz)["dimensions"][dim]


def situations(conn: sqlite3.Connection, since: datetime | None, limit: int = 30, tz: str = DEFAULT_TZ) -> list[dict]:
    """Группы по ``situation_key``, по убыванию n.

    [{"key", "n", "situation", "top_primary", "kinds", "members_share"}].
    """
    return dimensions_and_grounds(conn, since, tz, limit)["situations"]


def situation_rows(conn: sqlite3.Connection, key: str, since: datetime | None, limit: int = 200,
                   tz: str = DEFAULT_TZ) -> list[dict]:
    """Строки одной ситуации: свежие дни первыми, внутри дня порядок случайный (ничего не выдаёт).

    [{"day", "release_id", "result_kind", "primary_ground", "primary_status", "grounds" ({id: status}),
      "members", "explain_source"}].
    """
    rows = conn.execute(
        "SELECT day, release_id, result_kind, primary_ground, primary_status, grounds, members, explain_source "
        "FROM stats_results WHERE situation_key=? AND day >= ? ORDER BY day DESC, random() LIMIT ?",
        (key, _since_day(since, tz), limit))
    return [{**dict(zip(r.keys(), tuple(r), strict=True)), "grounds": json.loads(r["grounds"])} for r in rows]


def dropoff(conn: sqlite3.Connection, since: datetime | None, now: datetime | None = None) -> dict:
    """Где бросают анкету.

    Брошенные: сессии без результата со status='abandoned' либо status='active' и updated_at старше
    ``ABANDON_AFTER_HOURS`` часов; updated_at в периоде. Возвращает
    {"items": [{"qid", "n"}] по убыванию n (пустой qid -> "до первого вопроса"),
     "in_progress": число активных без результата, обновлённых за последние ABANDON_AFTER_HOURS часов}.
    Только в пределах срока хранения sessions.
    """
    border = iso((now or datetime.now(UTC)) - timedelta(hours=ABANDON_AFTER_HOURS))
    n: Counter = Counter()
    for r in conn.execute(
            "SELECT ui FROM sessions s WHERE updated_at >= ? AND (status='abandoned' OR (status='active' AND "
            "updated_at < ?)) AND NOT EXISTS (SELECT 1 FROM results r WHERE r.session_id = s.id)",
            (_since(since), border)):
        try:
            qid = (json.loads(r["ui"]) or {}).get("qid")
        except ValueError:
            qid = None
        n[qid or NO_QID_LABEL] += 1
    in_progress = conn.execute(
        "SELECT COUNT(*) FROM sessions s WHERE status='active' AND updated_at >= ? "
        "AND NOT EXISTS (SELECT 1 FROM results r WHERE r.session_id = s.id)", (border,)).fetchone()[0]
    return {"items": [{"qid": q, "n": c} for q, c in sorted(n.items(), key=lambda kv: (-kv[1], kv[0]))],
            "in_progress": in_progress}
