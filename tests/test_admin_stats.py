"""Страницы статистики админки: /stats и /stats/situation."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import quote

import pytest

from bginfo.db.conn import iso, utcnow
from tests.test_admin import do_login, make_ctx

KEY = "self|third|pension|-"
SIT = {"who": "self", "citizenship": "third", "income": ["pension"], "ties": [], "goal": "live", "family": False}


def _add(conn, key=KEY, sit=None, kind="ok", pg="pensioner", ps="fit", grounds=None):
    conn.execute(
        "INSERT INTO stats_results(day, release_id, result_kind, primary_ground, primary_status, situation_key,"
        " situation, grounds, members, explain_source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (iso(utcnow())[:10], 1, kind, pg, ps, key, json.dumps(sit or SIT),
         json.dumps(grounds or {"pensioner": "fit", "nomad": "not_eligible"}), 0, "llm"))


@pytest.fixture
def ctx(tmp_path):
    c = make_ctx(tmp_path)
    assert do_login(c).status_code == 303
    yield c
    c.conn.close()


def test_stats_requires_login(tmp_path):
    c = make_ctx(tmp_path)
    for path in ("/stats", "/stats/situation?key=x"):
        r = c.client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    c.conn.close()


@pytest.mark.parametrize("period", ["7", "30", "90", "all", "bogus", ""])
def test_stats_every_period_ok(ctx, period):
    r = ctx.client.get(f"/stats?period={period}")
    assert r.status_code == 200 and "Статистика" in r.text


def test_bad_period_behaves_like_default(ctx):
    html = ctx.client.get("/stats?period=bogus").text
    assert 'href="/stats?period=30"' in html and ctx.client.get("/stats").text.count("secondary") == html.count("secondary")


def test_empty_db_says_no_data(ctx):
    assert "За этот период данных нет" in ctx.client.get("/stats").text
    assert "За этот период данных нет" in ctx.client.get("/stats/situation?key=zzz").text


def test_stats_shows_labels(ctx):
    _add(ctx.conn)
    html = ctx.client.get("/stats?period=all").text
    assert "Пенсионер" in html  # название основания
    assert "Другая страна" in html and "Пенсия" in html  # подписи признаков из каталога фактов
    assert "Есть вариант" in html and "Типовые ситуации" in html
    assert "связи: —" in html
    assert f"/stats/situation?key={quote(KEY, safe='')}&amp;period=all" in html


def test_situation_page(ctx):
    _add(ctx.conn)
    r = ctx.client.get(f"/stats/situation?key={quote(KEY, safe='')}&period=all")
    assert r.status_code == 200
    assert "← вся статистика" in r.text and "Пенсионер" in r.text and "Digital nomad" in r.text and "модель" in r.text


def test_situation_key_is_escaped_and_length_limited(ctx):
    xss = "<script>alert(1)</script>"
    r = ctx.client.get("/stats/situation", params={"key": xss})
    assert r.status_code == 200 and xss not in r.text and "<script>alert" not in r.text
    assert "Ситуация" in r.text
    assert ctx.client.get("/stats/situation", params={"key": "a" * 501}).status_code == 200


def test_stats_page_new_labels(ctx):
    _add(ctx.conn)
    html = ctx.client.get("/stats?period=all").text
    assert "Начато анкет" in html and "Дошли до результата" in html and "Пересчётов результата" in html
    assert "Сейчас заполняют" in html and "<th>Результатов</th>" in html and "<th>Человек</th>" not in html


def test_stats_history_note_when_results_older_than_stats(ctx):
    ctx.conn.execute("PRAGMA foreign_keys=OFF")
    ctx.conn.execute("INSERT INTO sessions(id, tg_user_id, status, created_at, updated_at) "
                     "VALUES (1, 1, 'finished', '2026-09-01T10:00:00Z', '2026-09-01T10:00:00Z')")
    ctx.conn.execute("INSERT INTO results(session_id, release_id, as_of, evaluation, message_html, explain_source,"
                     " created_at) VALUES (1, 1, 'x', '[]', 'h', 'template', '2026-09-01T10:00:00Z')")
    html = ctx.client.get("/stats?period=all").text
    assert "Разрезы по ситуациям собираются с 01.09.2026" in html


def test_situation_page_shows_date_only(ctx):
    _add(ctx.conn)
    html = ctx.client.get(f"/stats/situation?key={quote(KEY, safe='')}&period=all").text
    assert "<th>Дата</th>" in html and utcnow().strftime("%d.%m.%Y") in html


# --------------------------------------------------------------------------- /stats/launches


def _launch(conn, uid, created, *, username=None, first="Иван", last=None, kind=None, ground=None, status=None):
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT OR IGNORE INTO users(tg_user_id, created_at, last_seen_at, username, first_name, last_name)"
                 " VALUES (?,?,?,?,?,?)", (uid, created, created, username, first, last))
    sid = conn.execute("INSERT INTO sessions(tg_user_id, status, created_at, updated_at) VALUES (?,?,?,?)",
                       (uid, "active", created, created)).lastrowid
    if kind:
        conn.execute(
            "INSERT INTO results(session_id, release_id, as_of, evaluation, message_html, explain_source, created_at,"
            " result_kind, primary_ground, primary_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, 1, "2026-10-01", "[]", "x", "template", created, kind, ground, status))


def test_launches_requires_login(tmp_path):
    c = make_ctx(tmp_path)
    r = c.client.get("/stats/launches")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    c.conn.close()


def test_launches_page_ok_and_link_from_stats(ctx):
    r = ctx.client.get("/stats/launches")
    assert r.status_code == 200 and "Запуски бота" in r.text and "За этот период запусков нет" in r.text
    assert 'href="/stats/launches' in ctx.client.get("/stats").text
    assert ctx.client.get("/stats/launches?period=bogus&page=abc").status_code == 200


def test_launches_pagination(ctx):
    for i in range(120):
        _launch(ctx.conn, 1000 + i, f"2026-10-01T10:{i % 60:02d}:00Z")
    html = ctx.client.get("/stats/launches?period=all").text
    assert "Страница 1 из 3" in html and "Всего запусков: <b>120</b>" in html
    assert 'href="/stats/launches?page=2&amp;period=all">Вперёд' in html and 'href="/stats/launches?page=0' not in html
    assert ">← Назад</a>" not in html
    last = ctx.client.get("/stats/launches?page=99&period=all").text
    assert "Страница 3 из 3" in last and ">Вперёд →</a>" not in last
    assert 'href="/stats/launches?page=2&amp;period=all">← Назад' in last
    assert 'href="/stats/launches?page=1&amp;period=all"' in last


def test_launches_escapes_names(ctx):
    _launch(ctx.conn, 1, "2026-10-01T10:00:00Z", first="<script>alert(1)</script>", last="<b>x</b>")
    html = ctx.client.get("/stats/launches?period=all").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<b>x</b>" not in html


def test_launches_telegram_link_only_for_valid_username(ctx):
    _launch(ctx.conn, 1, "2026-10-01T10:00:00Z", username="ivan_1")
    _launch(ctx.conn, 2, "2026-10-01T10:01:00Z", username='a"b>')
    _launch(ctx.conn, 3, "2026-10-01T10:02:00Z")
    html = ctx.client.get("/stats/launches?period=all").text
    assert 'href="https://t.me/ivan_1" target="_blank" rel="noopener">@ivan_1</a>' in html
    assert html.count("https://t.me/") == 1
    assert "без username" in html


def test_launches_ground_title_kind_label_and_state(ctx):
    _launch(ctx.conn, 1, "2026-10-01T10:00:00Z", kind="ok", ground="pensioner", status="fit")
    _launch(ctx.conn, 2, "2026-10-01T10:01:00Z", kind="none")
    _launch(ctx.conn, 3, "2026-10-01T10:02:00Z", kind="individual")
    html = ctx.client.get("/stats/launches?period=all").text
    assert "Пенсионер" in html and "подходит" in html and "есть результат" in html
    assert "ничего не подошло" in html and "индивидуальный разбор" in html


def test_launches_time_in_sofia(ctx):
    _launch(ctx.conn, 1, "2026-10-01T10:00:00Z", kind="none")  # октябрь: EEST, UTC+3
    html = ctx.client.get("/stats/launches?period=all").text
    assert "01.10.2026 13:00" in html


def test_launches_old_result_without_details_message(ctx):
    _launch(ctx.conn, 1, "2026-10-01T10:00:00Z", kind="ok")  # kind есть, чтобы появился results
    ctx.conn.execute("UPDATE results SET result_kind=NULL")
    html = ctx.client.get("/stats/launches?period=all").text
    assert "детали старого результата недоступны" in html


def test_launches_ground_title_uses_result_release(ctx):
    from bginfo.admin.routes_stats import _Names, launch_picked
    from bginfo.kb import releases as R
    from tests.kbfixture import mini_bundle

    R._cache.clear()
    conn = ctx.conn
    R.replace_draft(conn, mini_bundle(), "admin", "t")
    rid1 = R.publish_draft(conn, "admin", effective_from=utcnow() + timedelta(days=1))
    b = mini_bundle()
    b.grounds[[g.id for g in b.grounds].index("pensioner")].title = "Переименованное"
    R.replace_draft(conn, b, "admin", "t")
    R.publish_draft(conn, "admin", effective_from=utcnow() + timedelta(days=2))
    names = _Names(conn)
    names.grounds["pensioner"] = "Переименованное"  # текущее название после переименования
    old_title = R.load_release(conn, rid1).ground("pensioner").title
    assert old_title != "Переименованное" and names.ground("pensioner") != old_title
    base = {"state": "result", "result_kind": "ok", "primary_ground": "pensioner", "primary_status": "fit",
            "details_source": "result"}
    cache: dict = {}
    assert launch_picked(conn, {**base, "release_id": rid1}, names, cache)["title"] == old_title
    # выпуска нет -> текущее название
    assert launch_picked(conn, {**base, "release_id": 9999}, names, cache)["title"] == names.ground("pensioner")
    # основания нет в выпуске -> id
    assert launch_picked(conn, {**base, "release_id": rid1, "primary_ground": "zzz"}, names, cache)["title"] == "zzz"
