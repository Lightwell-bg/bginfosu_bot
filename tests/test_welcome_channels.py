"""Структурированное приветствие (бот и мини-апп) и разбивка статистики по каналам."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from bginfo.db.conn import open_db
from bginfo.engine import present as P
from bginfo.stats import queries as Q
from bginfo.stats.launches import launches
from tests.test_admin import do_login, make_ctx
from tests.test_dialog_view import make

# ------------------------------------------------------------------ приветствие


def test_render_welcome_html_links_and_no_bare_urls():
    html = P.render_welcome_html()
    for icon, title, url in P.WELCOME_LINKS:
        assert f'{icon} <a href="{url}">' in html
    assert html.startswith("<b>🇧🇬 Найдём возможные основания")
    assert "✅ какие основания могут подойти;" in html and "➡️ с чего начать оформление." in html
    for n in P.WELCOME_NOTES:
        assert n in html
    assert P.WELCOME_CTA in html
    # URL встречаются только внутри href
    assert "https://" not in re.sub(r'href="[^"]*"', "", html)
    assert "&quot;Информация о Болгарии&quot;" in html or '"Информация о Болгарии"' in html


async def test_dialog_welcome_view_shape_and_buttons():
    from bginfo.engine.dialog import Event
    d, s, snap, u = make(webapp_url="https://form.example")
    out = await d.handle(s, Event("start"), snap, u)
    w = out.view["welcome"]
    assert w["title"] == P.WELCOME_TITLE and w["intro"] == P.WELCOME_INTRO and w["cta"] == P.WELCOME_CTA
    assert w["points"][0] == {"icon": "✅", "text": "какие основания могут подойти"} and len(w["points"]) == 4
    assert w["notes"] == P.WELCOME_NOTES and len(w["links"]) == 4
    assert w["links"][0] == {"icon": "💬", "title": "Канал о жизни в Болгарии", "url": "https://t.me/bginfosu"}
    assert [b.action for row in out.replies[0].buttons for b in row] == ["go", "free", "webapp"]
    assert out.replies[0].no_preview


# ------------------------------------------------------------------ статистика по каналам


def _sess(conn, sid, channel, at="2026-10-05T10:00:00Z", result_channel=None):
    conn.execute("INSERT INTO sessions(id, tg_user_id, status, created_at, updated_at, channel) VALUES (?,?,?,?,?,?)",
                 (sid, sid, "active", at, at, channel))
    if result_channel:
        conn.execute(
            "INSERT INTO results(session_id, release_id, as_of, evaluation, message_html, explain_source, created_at,"
            " channel) VALUES (?, 1, 'x', '[]', 'h', 'template', ?, ?)", (sid, at, result_channel))


def _db(tmp_path):
    conn = open_db(tmp_path / "c.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def test_overview_channels(tmp_path):
    conn = _db(tmp_path)
    _sess(conn, 1, "bot", at="2026-09-01T10:00:00Z", result_channel="bot")
    _sess(conn, 2, "bot", result_channel="webapp")
    _sess(conn, 3, "webapp", result_channel="webapp")
    _sess(conn, 4, "webapp")
    allt = Q.overview(conn, None)["channels"]
    assert allt == {"sessions": {"bot": 2, "webapp": 2}, "results": {"bot": 1, "webapp": 2}}
    per = Q.overview(conn, datetime(2026, 10, 1, tzinfo=UTC))["channels"]
    assert per == {"sessions": {"bot": 1, "webapp": 2}, "results": {"bot": 0, "webapp": 2}}


def test_launches_channel_filter_and_result_channel(tmp_path):
    conn = _db(tmp_path)
    _sess(conn, 1, "bot", result_channel="webapp")
    _sess(conn, 2, "webapp")
    assert launches(conn, 1)["total"] == 2
    bot = launches(conn, 1, channel="bot")
    assert bot["total"] == 1 and bot["rows"][0]["channel"] == "bot" and bot["rows"][0]["result_channel"] == "webapp"
    assert launches(conn, 1, channel="webapp")["total"] == 1
    assert launches(conn, 1, channel="bogus")["total"] == 2


def test_channel_label():
    from bginfo.admin.routes_stats import channel_label
    assert channel_label("bot", "bot") == "Бот" and channel_label("webapp", None) == "Форма"
    assert channel_label("bot", "webapp") == "Бот → результат в форме"
    assert channel_label("webapp", "bot") == "Форма → результат в боте"


@pytest.fixture
def ctx(tmp_path):
    c = make_ctx(tmp_path)
    assert do_login(c).status_code == 303
    yield c
    c.conn.close()


def test_admin_pages_show_channels(ctx):
    conn = ctx.conn
    conn.execute("PRAGMA foreign_keys=OFF")
    _sess(conn, 1, "bot", result_channel="webapp")
    _sess(conn, 2, "webapp")
    html = ctx.client.get("/stats?period=all").text
    assert "Откуда запуски: Бот 1 · Форма 1; результаты: Бот 0 · Форма 1" in html
    page = ctx.client.get("/stats/launches?period=all").text
    assert "Бот → результат в форме" in page and "Всего запусков: <b>2</b>" in page
    f = ctx.client.get("/stats/launches?period=all&channel=webapp").text
    assert "Всего запусков: <b>1</b>" in f and "Бот → результат" not in f
    assert "Всего запусков: <b>2</b>" in ctx.client.get("/stats/launches?period=all&channel=zzz").text
