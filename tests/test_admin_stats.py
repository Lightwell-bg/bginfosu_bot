"""Страницы статистики админки: /stats и /stats/situation."""

from __future__ import annotations

import json
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
