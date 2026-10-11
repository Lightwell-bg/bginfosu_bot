"""Статистика: ситуация, запись в stats_results, подсчёты."""

import json
import re
from datetime import UTC, datetime

from bginfo.bot import repo
from bginfo.db.conn import _apply_missing, _split_sql, connect, migrate, open_db
from bginfo.engine.facts import CASE, MAIN, Fact, FactState, Participant, Profile
from bginfo.stats import queries as Q
from bginfo.stats.situation import situation_key, situation_of

T = datetime(2026, 10, 1, tzinfo=UTC)


def _profile(**vals) -> Profile:
    p = Profile()
    for key, v in vals.items():
        pid = CASE if key in ("who_moves", "goal") else MAIN
        p.facts[p._k(pid, key)] = Fact(pid, key, v, FactState.CONFIRMED, "button")
    return p


# ------------------------------------------------------------------ situation


def test_situation_empty_and_unknown():
    s = situation_of(Profile())
    assert s == {"who": "unknown", "citizenship": "unknown", "income": None, "ties": None,
                 "goal": "unknown", "family": False}
    assert situation_key(s) == "unknown|unknown|?|?"


def test_situation_values_sorted_and_none_dropped():
    p = _profile(who_moves="self", citizenship_group=["third"], income_sources=["pension", "none", "work"],
                 ties=["none"], goal="live")
    p.participants["p1"] = Participant("p1", "spouse", "Супруг")
    s = situation_of(p)
    assert s["income"] == ["pension", "work"] and s["ties"] == [] and s["family"] is True
    assert situation_key(s) == "self|third|pension+work|-"


def test_situation_key_order_independent_and_ignores_goal_family():
    a = {"who": "w", "citizenship": "eu", "income": ["b", "a"], "ties": ["z"], "goal": "x", "family": True}
    b = {"who": "w", "citizenship": "eu", "income": ["a", "b"], "ties": ["z"], "goal": "y", "family": False}
    assert situation_key(a) == situation_key(b) == "w|eu|a+b|z"


def test_citizenship_priority():
    assert situation_of(_profile(citizenship_group=["third", "eu"]))["citizenship"] == "eu"
    assert situation_of(_profile(citizenship_group=["eu", "bg"]))["citizenship"] == "bg"
    assert situation_of(_profile(citizenship_group="third"))["citizenship"] == "third"


# ------------------------------------------------------------------ repo


def _record(**extra):
    r = {"release_id": None, "as_of": "2026-10-01", "evaluation": [], "message_html": "x", "explain_source": "llm"}
    r.update(extra)
    return r


def test_migration_applies_on_existing_001(tmp_path):
    conn = connect(tmp_path / "m.db")
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    # применяем только 001, затем остальное
    from importlib import resources

    from bginfo.db.conn import _split_sql
    sql = resources.files("bginfo.db.migrations").joinpath("001_init.sql").read_text(encoding="utf-8")
    for st in _split_sql(sql):
        conn.execute(st)
    conn.execute("INSERT INTO schema_migrations VALUES ('001_init.sql', 'x')")
    assert migrate(conn) == ["002_stats.sql"]
    assert conn.execute("SELECT COUNT(*) FROM stats_results").fetchone()[0] == 0


def _step_with_result(conn, uid, record):
    repo.touch_user(conn, uid)
    s = repo.new_session(conn, uid)
    assert repo.commit_step(conn, update_id=None, tg_user_id=uid, session=s, expected_revision=0, bump=True,
                            result_record=record) == "ok"
    return s


def test_stats_row_written_and_survives_delete_and_cleanup(tmp_path):
    conn = open_db(tmp_path / "s.db")
    rec = _record(result_kind="ok", primary_ground="g1", primary_status="fit", situation={"who": "self"},
                  situation_key="self|third|-|-", grounds={"g1": "fit", "g2": "clarify"}, members=2)
    # release_id в results — внешний ключ: создаём выпуск
    conn.execute("PRAGMA foreign_keys=OFF")
    _step_with_result(conn, 7, {**rec, "release_id": 1})
    row = conn.execute("SELECT * FROM stats_results").fetchone()
    assert row["result_kind"] == "ok" and row["primary_ground"] == "g1" and row["members"] == 2
    assert json.loads(row["grounds"]) == {"g1": "fit", "g2": "clarify"}
    cols = list(row.keys())
    assert "tg_user_id" not in cols and "session_id" not in cols and "at" not in cols
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", row["day"])  # только дата, без времени
    repo.delete_user(conn, 7)
    assert conn.execute("SELECT COUNT(*) FROM stats_results").fetchone()[0] == 1
    _step_with_result(conn, 8, {**rec, "release_id": 1})
    conn.execute("UPDATE sessions SET updated_at='2000-01-01T00:00:00Z'")
    conn.execute("UPDATE users SET last_seen_at='2000-01-01T00:00:00Z'")
    conn.execute("UPDATE stats_results SET day='2000-01-01'")
    repo.cleanup(conn, 90)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM stats_results").fetchone()[0] == 2


def test_old_record_without_new_fields_gets_defaults(tmp_path):
    conn = open_db(tmp_path / "d.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    _step_with_result(conn, 1, _record(release_id=1))
    row = conn.execute("SELECT * FROM stats_results").fetchone()
    assert row["result_kind"] == "ok" and row["grounds"] == "{}" and row["members"] == 0


# ------------------------------------------------------------------ queries


def _sr(conn, day, key="a|third|-|-", sit=None, kind="ok", pg="g1", ps="fit", grounds=None, members=0, src="template"):
    sit = sit or {"who": "a", "citizenship": "third", "income": [], "ties": [], "goal": "live", "family": False}
    conn.execute(
        "INSERT INTO stats_results(day, release_id, result_kind, primary_ground, primary_status, situation_key,"
        " situation, grounds, members, explain_source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (day[:10], 1, kind, pg, ps, key, json.dumps(sit), json.dumps(grounds or {"g1": "fit"}), members, src))


def _event(conn, at, kind, sid=None):
    conn.execute("INSERT INTO events(at, kind, session_id) VALUES (?,?,?)", (at, kind, sid))


def _session(conn, sid, status="active", at="2026-10-05T10:00:00Z", ui=None, result=False):
    conn.execute("INSERT INTO sessions(id, tg_user_id, status, ui, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                 (sid, sid, status, json.dumps(ui or {}), at, at))
    if result:
        conn.execute("INSERT INTO results(session_id, release_id, as_of, evaluation, message_html, explain_source,"
                     " created_at) VALUES (?, 1, 'x', '[]', 'h', 'template', ?)", (sid, at))


def test_empty_db(tmp_path):
    conn = open_db(tmp_path / "e.db")
    o = Q.overview(conn, None)
    assert o["conversion"] == 0 and o["llm_share"] == 0 and o["results"] == 0
    assert o["consults"] == {"sent": 0, "unknown": 0, "failed": 0} and o["by_kind"] == {}
    assert Q.daily(conn, None) == [] and Q.by_ground(conn, None) == [] and Q.situations(conn, None) == []
    assert Q.dropoff(conn, None) == {"items": [], "in_progress": 0} and Q.situation_rows(conn, "k", None) == []
    for d in Q.DIMENSIONS:
        assert Q.by_dimension(conn, None, d) == []


def test_overview_and_period(tmp_path):
    conn = open_db(tmp_path / "o.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    old, new = "2026-09-01T10:00:00Z", "2026-10-05T10:00:00Z"
    conn.execute("INSERT INTO users(tg_user_id, created_at, last_seen_at) VALUES (1,?,?),(2,?,?)", (old, old, new, new))
    _session(conn, 1, at=old, result=True)
    _session(conn, 2, at=new, result=True)
    _session(conn, 3, at=new)
    for sid, at in ((1, old), (2, new), (2, new), (3, new)):  # сессия 2 стартовала дважды: считается один раз
        _event(conn, at, "session_started", sid)
    _event(conn, new, "error")
    _event(conn, new, "jev_unavailable")
    _sr(conn, old, src="llm")
    _sr(conn, new, src="llm")
    _sr(conn, new, kind="none", pg=None, ps=None)
    _sr(conn, new, kind="none", pg=None, ps=None)
    for st in ("sent", "failed", "unknown"):
        conn.execute("INSERT INTO sessions(id, tg_user_id, status, created_at, updated_at) VALUES (?,2,'finished',?,?)",
                     (10 + len(st), new, new))
        conn.execute("INSERT INTO consult_requests(session_id, tg_user_id, payload, status, created_at) "
                     "VALUES (?,2,'p',?,?)", (10 + len(st), st, new))
    conn.execute("INSERT INTO ai_calls(at, kind, model, ok, status, cost_usd) VALUES (?,?,?,?,?,?)",
                 (new, "explain", "m", 1, "done", 0.5))
    conn.execute("INSERT INTO ai_calls(at, kind, model, ok, status, cost_usd) VALUES (?,?,?,?,?,?)",
                 (new, "explain", "m", 1, "reserved", 9.0))
    allt = Q.overview(conn, None)
    assert allt["results_total"] == 4 and allt["started"] == 3 and allt["completed"] == 2 and allt["new_users"] == 2
    assert abs(allt["conversion"] - 2 / 3) < 1e-9 and allt["recalculations"] == 2
    assert allt["ai_calls"] == 1 and allt["ai_cost_usd"] == 0.5 and allt["by_kind"] == {"ok": 2, "none": 2}
    assert abs(allt["llm_share"] - 2 / 4) < 1e-9 and allt["consults"] == {"sent": 1, "unknown": 1, "failed": 1}
    per = Q.overview(conn, datetime(2026, 10, 1, tzinfo=UTC))
    assert per["results_total"] == 3 and per["started"] == 2 and per["completed"] == 1 and per["new_users"] == 1
    assert per["conversion"] == 0.5 and per["recalculations"] == 2
    assert per["errors"] == 1 and per["jev_unavailable"] == 1


def test_recalculations_never_negative(tmp_path):
    conn = open_db(tmp_path / "rc.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    _session(conn, 1, result=True)
    _event(conn, "2026-10-05T10:00:00Z", "session_started", 1)
    o = Q.overview(conn, None)  # stats_results пуст (история), results есть
    assert o["completed"] == 1 and o["results_total"] == 0 and o["recalculations"] == 0 and o["conversion"] == 1


def test_history_start(tmp_path):
    conn = open_db(tmp_path / "h.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    assert Q.history_start(conn, None) is None  # результатов нет вообще
    _session(conn, 1, at="2026-09-01T10:00:00Z", result=True)
    assert Q.history_start(conn, None) == "2026-09-01"  # stats_results пуст, results есть
    _sr(conn, "2026-10-05T10:00:00Z")
    assert Q.history_start(conn, None) == "2026-10-05"  # период шире первой строки
    assert Q.history_start(conn, datetime(2026, 10, 1, tzinfo=UTC)) is None  # результатов периода до неё нет


def test_daily_groups_by_sofia_day(tmp_path):
    conn = open_db(tmp_path / "dd.db")
    _event(conn, "2026-10-05T22:30:00Z", "session_started")  # Sofia (UTC+3): уже 6 октября
    _event(conn, "2026-10-05T10:00:00Z", "session_started")
    _sr(conn, "2026-10-06")  # результаты хранят готовый день, пересчёта часовых поясов нет
    conn.execute("INSERT INTO ai_calls(at, kind, model, ok, status, cost_usd) VALUES "
                 "('2026-10-05T10:00:00Z','x','m',1,'done',0.25)")
    d = {r["day"]: r for r in Q.daily(conn, T)}
    assert list(d) == ["2026-10-05", "2026-10-06"]
    assert d["2026-10-06"]["started"] == 1 and d["2026-10-06"]["results"] == 1
    assert d["2026-10-05"]["started"] == 1 and d["2026-10-05"]["ai_cost"] == 0.25


def test_by_ground(tmp_path):
    conn = open_db(tmp_path / "bg.db")
    _sr(conn, "2026-10-05T10:00:00Z", pg="g1", grounds={"g1": "fit", "g2": "not_eligible"})
    _sr(conn, "2026-10-05T11:00:00Z", pg="g2", grounds={"g1": "clarify", "g2": "fit"})
    _sr(conn, "2026-09-05T11:00:00Z", pg="g2", grounds={"g2": "fit"})
    r = {x["ground_id"]: x for x in Q.by_ground(conn, T)}
    assert r["g1"] == {"ground_id": "g1", "evaluated": 2, "statuses": {"fit": 1, "clarify": 1}, "primary": 1}
    assert r["g2"]["evaluated"] == 2 and r["g2"]["primary"] == 1
    assert {x["ground_id"]: x["evaluated"] for x in Q.by_ground(conn, None)}["g2"] == 3


def test_by_dimension_scalar_list_family(tmp_path):
    conn = open_db(tmp_path / "bd.db")
    at = "2026-10-05T10:00:00Z"
    base = {"who": "self", "citizenship": "third", "goal": "live"}
    _sr(conn, at, sit={**base, "income": ["pension", "work"], "ties": [], "family": True}, pg="g1")
    _sr(conn, at, sit={**base, "income": [], "ties": None, "family": False}, pg="g1", kind="none")
    _sr(conn, at, sit={**base, "income": ["work"], "ties": ["bg_origin"], "family": False}, pg="g2")
    inc = {r["value"]: r for r in Q.by_dimension(conn, None, "income")}
    assert inc["work"]["n"] == 2 and inc["pension"]["n"] == 1 and inc["none"]["n"] == 1
    assert inc["work"]["top_primary"]["ground_id"] in ("g1", "g2") and abs(inc["work"]["share"] - 2 / 3) < 1e-9
    ties = {r["value"]: r["n"] for r in Q.by_dimension(conn, None, "ties")}
    assert ties == {"none": 1, "unknown": 1, "bg_origin": 1}
    fam = {r["value"]: r for r in Q.by_dimension(conn, None, "family")}
    assert fam[True]["n"] == 1 and fam[False]["n"] == 2 and fam[False]["kinds"] == {"none": 1, "ok": 1}
    who = Q.by_dimension(conn, None, "who")
    assert who[0]["value"] == "self" and who[0]["share"] == 1 and who[0]["top_primary"]["ground_id"] == "g1"
    assert Q.by_dimension(conn, T.replace(year=2027), "who") == []


def test_situations_and_rows(tmp_path):
    conn = open_db(tmp_path / "si.db")
    for i in range(3):
        _sr(conn, f"2026-10-0{i + 1}T10:00:00Z", key="k1", members=1 if i == 0 else 0)
    _sr(conn, "2026-10-05T10:00:00Z", key="k2", pg=None, ps=None, kind="none")
    s = Q.situations(conn, None)
    assert [x["key"] for x in s] == ["k1", "k2"] and s[0]["n"] == 3
    assert abs(s[0]["members_share"] - 1 / 3) < 1e-9 and s[0]["top_primary"]["share"] == 1
    assert s[1]["top_primary"] is None and s[1]["kinds"] == {"none": 1}
    assert len(Q.situations(conn, None, limit=1)) == 1
    rows = Q.situation_rows(conn, "k1", None, limit=2)
    assert len(rows) == 2 and rows[0]["day"] > rows[1]["day"] and rows[0]["grounds"] == {"g1": "fit"}
    assert "at" not in rows[0] and re.fullmatch(r"\d{4}-\d\d-\d\d", rows[0]["day"])
    assert Q.situation_rows(conn, "k1", datetime(2026, 10, 3, tzinfo=UTC)) [0]["day"] == "2026-10-03"


def test_dropoff_abandoned_and_in_progress(tmp_path):
    conn = open_db(tmp_path / "dr.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    old, new = "2026-09-01T10:00:00Z", "2026-10-05T10:00:00Z"  # new старше 24 ч
    fresh = "2026-10-06T08:00:00Z"
    _session(conn, 1, "active", new, {"qid": "q1"})  # активна > 24 ч: брошена
    _session(conn, 2, "abandoned", new, {"qid": "q1"})
    _session(conn, 3, "abandoned", new, {})
    _session(conn, 4, "finished", new, {"qid": "q2"})
    _session(conn, 5, "abandoned", old, {"qid": "q3"})
    _session(conn, 6, "abandoned", new, {"qid": "q9"}, result=True)
    _session(conn, 7, "active", fresh, {"qid": "q5"})  # свежая: заполняют сейчас
    _session(conn, 8, "active", fresh, {"qid": "q5"}, result=True)  # с результатом: не считается
    d = Q.dropoff(conn, T, now)
    assert d["items"] == [{"qid": "q1", "n": 2}, {"qid": Q.NO_QID_LABEL, "n": 1}]
    assert d["in_progress"] == 1
    assert {r["qid"] for r in Q.dropoff(conn, None, now)["items"]} == {"q1", Q.NO_QID_LABEL, "q3"}


def test_dimensions_and_grounds_single_pass(tmp_path):
    conn = open_db(tmp_path / "one.db")
    _sr(conn, "2026-10-05", key="k1", pg="g1", grounds={"g1": "fit", "g2": "clarify"})
    _sr(conn, "2026-10-05", key="k2", pg="g2", grounds={"g2": "fit"})
    full = Q.dimensions_and_grounds(conn, None, limit=1)
    assert set(full["dimensions"]) == set(Q.DIMENSIONS) and len(full["situations"]) == 1
    assert full["grounds"] == Q.by_ground(conn, None)
    for dim in Q.DIMENSIONS:
        assert full["dimensions"][dim] == Q.by_dimension(conn, None, dim)
    assert Q.situations(conn, None) == Q.dimensions_and_grounds(conn, None)["situations"]


def test_situation_rows_order_by_day_then_any(tmp_path):
    conn = open_db(tmp_path / "ord.db")
    for day in ("2026-10-01", "2026-10-03", "2026-10-03", "2026-10-02"):
        _sr(conn, day, key="k")
    days = [r["day"] for r in Q.situation_rows(conn, "k", None)]
    assert days == ["2026-10-03", "2026-10-03", "2026-10-02", "2026-10-01"]


def test_cleanup_removes_old_events_but_keeps_stats(tmp_path):
    conn = open_db(tmp_path / "ce.db")
    _event(conn, "2000-01-01T00:00:00Z", "session_started", 1)
    _event(conn, "2999-01-01T00:00:00Z", "session_started", 2)
    _sr(conn, "2000-01-01")
    repo.cleanup(conn, 90)
    assert [r["session_id"] for r in conn.execute("SELECT session_id FROM events")] == [2]
    assert conn.execute("SELECT COUNT(*) FROM stats_results").fetchone()[0] == 1


def test_migrate_second_connection_with_stale_view_is_noop(tmp_path):
    from importlib import resources
    path = tmp_path / "race.db"
    c0 = connect(path)
    c0.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for st in _split_sql(resources.files("bginfo.db.migrations").joinpath("001_init.sql").read_text(encoding="utf-8")):
        c0.execute(st)
    c0.execute("INSERT INTO schema_migrations VALUES ('001_init.sql', 'x')")
    files = sorted((f for f in resources.files("bginfo.db.migrations").iterdir() if f.name.endswith(".sql")),
                   key=lambda f: f.name)
    c1, c2 = connect(path), connect(path)
    stale = {"001_init.sql"}  # оба соединения «увидели» только 001
    assert _apply_missing(c1, files, set(stale)) == ["002_stats.sql"]
    assert _apply_missing(c2, files, set(stale)) == []  # без ошибки «table already exists»
    assert c2.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='002_stats.sql'").fetchone()[0] == 1
