"""Отчёт о запусках: миграция 003, имя пользователя, поля результата, launches()."""

from datetime import UTC, datetime, timedelta
from importlib import resources

from bginfo.bot import repo
from bginfo.db.conn import _split_sql, connect, iso, migrate, open_db, utcnow
from bginfo.stats.launches import launches


def _db(tmp_path):
    conn = open_db(tmp_path / "l.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def _session(conn, uid, created, *, status="active", updated=None):
    conn.execute("INSERT OR IGNORE INTO users(tg_user_id, created_at, last_seen_at) VALUES (?,?,?)",
                 (uid, created, created))
    cur = conn.execute(
        "INSERT INTO sessions(tg_user_id, status, created_at, updated_at) VALUES (?,?,?,?)",
        (uid, status, created, updated or created))
    return cur.lastrowid


def _result(conn, sid, at, **kw):
    conn.execute(
        "INSERT INTO results(session_id, release_id, as_of, evaluation, message_html, explain_source, created_at,"
        " result_kind, primary_ground, primary_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, kw.get("release_id", 1), "2026-10-01", "[]", "x", "template", at,
         kw.get("kind"), kw.get("ground"), kw.get("status")))


def test_migration_003_on_populated_001_002(tmp_path):
    conn = connect(tmp_path / "m.db")
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for name in ("001_init.sql", "002_stats.sql"):
        for st in _split_sql(resources.files("bginfo.db.migrations").joinpath(name).read_text(encoding="utf-8")):
            conn.execute(st)
        conn.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (name,))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO users(tg_user_id, created_at, last_seen_at) VALUES (1,'t','t')")
    conn.execute("INSERT INTO sessions(tg_user_id, created_at, updated_at) VALUES (1,'t','t')")
    conn.execute("INSERT INTO results(session_id, release_id, as_of, evaluation, message_html, explain_source,"
                 " created_at) VALUES (1,1,'a','[]','x','llm','t')")
    assert migrate(conn) == ["003_launches.sql", "004_channel.sql"]
    u = conn.execute("SELECT * FROM users").fetchone()
    assert u["username"] is None and u["first_name"] is None and u["last_name"] is None
    r = conn.execute("SELECT * FROM results").fetchone()
    assert r["result_kind"] is None and r["primary_ground"] is None and r["primary_status"] is None
    idx = {x["name"] for x in conn.execute("PRAGMA index_list(sessions)")}
    assert "ix_sessions_created" in idx


def test_touch_user_saves_and_updates_names(tmp_path):
    conn = _db(tmp_path)
    repo.touch_user(conn, 5, "@Ivan_1", "Иван", "Петров")
    u = conn.execute("SELECT * FROM users WHERE tg_user_id=5").fetchone()
    assert (u["username"], u["first_name"], u["last_name"]) == ("Ivan_1", "Иван", "Петров")
    repo.touch_user(conn, 5, "ivan_new", "Иван", None)
    u = conn.execute("SELECT * FROM users WHERE tg_user_id=5").fetchone()
    assert (u["username"], u["first_name"], u["last_name"]) == ("ivan_new", "Иван", None)


def test_touch_user_invalid_username_and_truncation(tmp_path):
    conn = _db(tmp_path)
    repo.touch_user(conn, 6, "ab", "x" * 300, "y" * 200)
    u = conn.execute("SELECT * FROM users WHERE tg_user_id=6").fetchone()
    assert u["username"] is None and len(u["first_name"]) == 128 and len(u["last_name"]) == 128
    repo.touch_user(conn, 6, "bad name!")
    assert conn.execute("SELECT username FROM users WHERE tg_user_id=6").fetchone()[0] is None
    repo.touch_user(conn, 6, "a" * 65)  # после обрезки до 64 допустим
    assert conn.execute("SELECT username FROM users WHERE tg_user_id=6").fetchone()[0] == "a" * 64


def test_result_fields_written_to_results(tmp_path):
    conn = _db(tmp_path)
    repo.touch_user(conn, 7)
    s = repo.new_session(conn, 7)
    rec = {"release_id": 1, "as_of": "2026-10-01", "evaluation": [], "message_html": "x",
           "explain_source": "template", "result_kind": "ok", "primary_ground": "g1", "primary_status": "fit"}
    assert repo.commit_step(conn, update_id=None, tg_user_id=7, session=s, expected_revision=0, bump=True,
                            result_record=rec) == "ok"
    r = conn.execute("SELECT * FROM results").fetchone()
    assert (r["result_kind"], r["primary_ground"], r["primary_status"]) == ("ok", "g1", "fit")
    # запись без полей -> NULL
    repo.add_result(conn, s.id, {k: v for k, v in rec.items() if not k.startswith(("result_kind", "primary"))})
    r2 = conn.execute("SELECT * FROM results ORDER BY id DESC LIMIT 1").fetchone()
    assert r2["result_kind"] is None and r2["primary_ground"] is None


def test_launches_empty(tmp_path):
    conn = _db(tmp_path)
    assert launches(conn, 0) == {"rows": [], "total": 0, "page": 1, "pages": 1, "per_page": 50}


def test_launches_pagination_and_order(tmp_path):
    conn = _db(tmp_path)
    ids = [_session(conn, 1, f"2026-10-01T10:00:0{i}Z", status="abandoned") for i in range(5)]
    same = _session(conn, 1, "2026-10-01T10:00:04Z", status="abandoned")  # то же время, больший id
    p1 = launches(conn, 1, per_page=4)
    assert p1["total"] == 6 and p1["pages"] == 2 and p1["page"] == 1
    assert [r["session_id"] for r in p1["rows"]] == [same, ids[4], ids[3], ids[2]]
    p2 = launches(conn, 2, per_page=4)
    assert [r["session_id"] for r in p2["rows"]] == [ids[1], ids[0]]
    assert launches(conn, 0, per_page=4)["page"] == 1
    far = launches(conn, 99, per_page=4)
    assert far["page"] == 2 and len(far["rows"]) == 2
    assert launches(conn, -3, per_page=4)["page"] == 1


def test_launches_states_and_last_result(tmp_path):
    conn = _db(tmp_path)
    now = utcnow()
    old = iso(now - timedelta(hours=48))
    fresh = iso(now - timedelta(hours=1))
    repo.touch_user(conn, 1, "user_a", "Анна", "К")
    a = _session(conn, 1, old, status="abandoned")
    b = _session(conn, 2, fresh)  # active, свежая
    c = _session(conn, 3, old)  # active, давно
    d = _session(conn, 4, fresh)
    _result(conn, d, "2026-10-01T10:00:00Z", release_id=1, kind="ok", ground="g1", status="fit")
    _result(conn, d, "2026-10-02T10:00:00Z", release_id=2, kind="none", ground=None, status=None)
    _result(conn, d, "2026-10-02T11:00:00Z", release_id=3, kind="ok", ground="g9", status="clarify")
    rows = {r["session_id"]: r for r in launches(conn, 1)["rows"]}
    assert rows[a]["state"] == "abandoned" and rows[b]["state"] == "in_progress" and rows[c]["state"] == "abandoned"
    assert rows[a]["username"] == "user_a" and rows[a]["first_name"] == "Анна"
    assert rows[a]["results_count"] == 0 and rows[a]["last_result_at"] is None and rows[a]["release_id"] is None
    rd = rows[d]
    assert rd["state"] == "result" and rd["results_count"] == 3
    assert (rd["last_result_at"], rd["result_kind"], rd["primary_ground"], rd["primary_status"], rd["release_id"]) \
        == ("2026-10-02T11:00:00Z", "ok", "g9", "clarify", 3)
    assert rd["started_at"] == fresh and rd["tg_user_id"] == 4
    assert launches(conn, 1)["total"] == 4


def test_launches_since(tmp_path):
    conn = _db(tmp_path)
    _session(conn, 1, "2026-09-01T00:00:00Z", status="abandoned")
    keep = _session(conn, 1, "2026-10-05T00:00:00Z", status="abandoned")
    res = launches(conn, 1, since=datetime(2026, 10, 1, tzinfo=UTC))
    assert res["total"] == 1 and [r["session_id"] for r in res["rows"]] == [keep]


def _set_ui(conn, sid, ui):
    import json

    conn.execute("UPDATE sessions SET ui=? WHERE id=?", (json.dumps(ui), sid))


def test_launches_results_outside_page_do_not_affect_counts(tmp_path):
    conn = _db(tmp_path)
    old = _session(conn, 1, "2026-10-01T10:00:00Z", status="abandoned")
    new = _session(conn, 2, "2026-10-02T10:00:00Z", status="abandoned")
    for i in range(3):
        _result(conn, old, f"2026-10-01T11:0{i}:00Z", kind="ok", ground="g1", status="fit")
    _result(conn, new, "2026-10-02T11:00:00Z", release_id=7, kind="none")
    res = launches(conn, 1, per_page=1)
    assert [r["session_id"] for r in res["rows"]] == [new]
    row = res["rows"][0]
    assert row["results_count"] == 1 and row["release_id"] == 7 and row["details_source"] == "result"
    assert launches(conn, 2, per_page=1)["rows"][0]["results_count"] == 3


def test_launches_null_fields_fall_back_to_session_view(tmp_path):
    conn = _db(tmp_path)
    sid = _session(conn, 1, "2026-10-01T10:00:00Z")
    _result(conn, sid, "2026-10-01T10:05:00Z")  # поля NULL, как после миграции 003
    _set_ui(conn, sid, {"last_view": {"kind": "ok", "primary": {"ground_id": "g1", "status": "fit"}}})
    row = launches(conn, 1)["rows"][0]
    assert row["state"] == "result" and row["details_source"] == "session_view"
    assert (row["result_kind"], row["primary_ground"], row["primary_status"]) == ("ok", "g1", "fit")


def test_launches_null_fields_without_view_are_missing(tmp_path):
    conn = _db(tmp_path)
    sid = _session(conn, 1, "2026-10-01T10:00:00Z")
    _result(conn, sid, "2026-10-01T10:05:00Z")
    row = launches(conn, 1)["rows"][0]
    assert row["details_source"] == "missing" and row["result_kind"] is None
    _set_ui(conn, sid, {"last_view": "garbage"})
    assert launches(conn, 1)["rows"][0]["details_source"] == "missing"
