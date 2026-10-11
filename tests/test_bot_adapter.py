"""Адаптер Telegram на моках (FakeSession). Это НЕ живой Telegram и НЕ живой AI."""

import asyncio
import logging
import re

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from bginfo.ai.explain import apply_template
from bginfo.ai.interpret import Interpretation
from bginfo.bot import repo
from bginfo.bot.app import (
    TXT_CONFLICT,
    TXT_CONSULT_DUP,
    TXT_CONSULT_FAIL,
    TXT_CONSULT_OK,
    TXT_CONSULT_UNKNOWN,
    TXT_MEDIA,
    TXT_NO_SESSION,
    TXT_STALE,
    build_app,
)
from bginfo.config import AppConfig, Settings
from bginfo.db.conn import open_db
from bginfo.engine.dialog import Button, Out, Reply
from bginfo.engine.present import AI_NOTICE
from bginfo.kb import releases as R
from tests.fake_tg import cb_update, make_bot, msg_update, photo_update
from tests.kbfixture import mini_bundle

UID = 42
CONSULT_CHAT = -100


class FakeInterp:
    async def interpret(self, text, kb, current, uid):
        return Interpretation(ok=False, error="disabled")


class FakeExplainer:
    async def explain(self, rv, lines, titles, uid):
        return apply_template(rv)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "bot.db"
    conn = open_db(path)
    R._cache.clear()
    R.replace_draft(conn, mini_bundle(), "admin", "test")
    R.publish_draft(conn, "admin")
    conn.close()
    return path


def make_app(db_path):
    settings = Settings(DB_PATH=str(db_path), CONSULT_CHAT_ID=CONSULT_CHAT, APP_MODE="test", _env_file=None)
    bot, session = make_bot()
    app = build_app(settings, AppConfig(), bot=bot, interpreter=FakeInterp(), explainer=FakeExplainer())
    return app, session


@pytest.fixture
async def env(db_path):
    app, session = make_app(db_path)
    yield app, session, Driver(app, session)
    await app.close()


class Driver:
    def __init__(self, app, session):
        self.app, self.session, self.n = app, session, 0

    async def send(self, text):
        self.n += 1
        await self.app.dp.feed_update(self.app.bot, msg_update(self.n, text))

    async def cb(self, data, update_id=None):
        self.n += 1
        await self.app.dp.feed_update(self.app.bot, cb_update(update_id or self.n, data))

    def buttons(self):
        """callback_data кнопок последнего сообщения с клавиатурой."""
        for c in reversed(self.session.calls):
            mk = getattr(c, "reply_markup", None)
            if isinstance(c, (SendMessage, EditMessageText)) and mk:
                return [b.callback_data for row in mk.inline_keyboard for b in row]
        return []

    def find(self, action, arg=""):
        for d in self.buttons():
            if re.match(rf"^\d+:\d+:{action}:{re.escape(arg)}$", d):
                return d
        raise AssertionError(f"no button {action}:{arg} in {self.buttons()}")

    async def press(self, action, arg=""):
        await self.cb(self.find(action, arg))

    def sent_texts(self, chat=UID):
        return [c.text for c in self.session.of(SendMessage) if c.chat_id == chat]

    def session_row(self):
        return repo.active_session(self.app.conn, UID)

    async def to_result(self):
        await self.send("/start")
        await self.press("go")
        for _ in range(12):
            ui = self.session_row().ui
            qid = ui.get("qid")
            if self.session_row().state == "review":
                break
            if qid == "q_cit":
                await self.press("ans", "third")
            elif qid == "q_inc":
                await self.press("tog", "pension")
                await self.press("done")
            elif qid == "q_pens":
                await self.send("900")
            elif qid == "q_house":
                await self.press("ans", "yes")
            else:
                await self.press("unk")
        await self.press("review_ok")


async def test_start_shows_welcome_with_two_buttons(env):
    app, session, d = env
    await d.send("/start")
    sends = session.of(SendMessage)
    assert len(sends) == 1 and "Найдём возможные основания" in sends[0].text
    assert "https://t.me/bginfosu" in sends[0].text and AI_NOTICE not in sends[0].text
    assert sends[0].link_preview_options is not None and sends[0].link_preview_options.is_disabled
    assert len(d.buttons()) == 2
    assert re.match(r"^\d+:0:go:$", d.buttons()[0])
    assert d.session_row().state == "start"
    # приветствие уведомление об AI не показывает
    assert repo.touch_user(app.conn, UID)["ai_notice_shown"] is False


async def test_first_free_text_after_welcome_shows_notice_before_interpret(env):
    app, _session, d = env
    calls = []

    async def spy(text, kb, current, uid):
        calls.append(text)
        return Interpretation(ok=False, error="disabled")

    app.dialog.interpreter.interpret = spy
    await d.send("/start")
    await d.send("я пенсионер из России")
    assert calls == []
    assert AI_NOTICE in d.sent_texts()[-1]
    assert repo.touch_user(app.conn, UID)["ai_notice_shown"] is True
    await d.press("parse_pending")
    assert calls == ["я пенсионер из России"]


async def test_duplicate_update_not_processed(env):
    app, session, _d = env
    update = msg_update(7, "/start")
    await app.dp.feed_update(app.bot, update)
    await app.dp.feed_update(app.bot, update)
    assert len(session.of(SendMessage)) == 1


async def test_stale_callback_alerts_and_resends_current_step(env):
    _app, session, d = env
    await d.send("/start")
    old_go = d.find("go")
    await d.cb(old_go)  # ревизия 0 -> 1
    sid = d.session_row().id
    session.clear()
    await d.cb(old_go)
    answers = session.of(AnswerCallbackQuery)
    assert [a.text for a in answers] == [TXT_STALE] and answers[0].show_alert
    # актуальный шаг прислан заново, с кнопками текущей ревизии (старое сообщение не правится)
    assert len(session.of(SendMessage)) == 1 and not session.of(EditMessageText)
    assert "Гражданство" in d.sent_texts()[-1]
    row = d.session_row()
    assert row.id == sid and all(b.startswith(f"{row.id}:{row.revision}:") for b in d.buttons())


async def test_stale_callback_without_session_offers_start(env):
    _app, session, d = env
    await d.cb("999:3:go:")
    assert [a.text for a in session.of(AnswerCallbackQuery)] == [TXT_STALE]
    assert d.sent_texts() == [TXT_NO_SESSION]
    assert _app.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_double_tap_processed_once(env):
    _app, session, d = env
    await d.send("/start")
    go = d.find("go")
    session.clear()
    await asyncio.gather(d.cb(go, update_id=501), d.cb(go, update_id=502))
    # второй тап устарел: алерт + повторный показ актуального шага, анкета продвинулась один раз
    assert sorted(a.text or "" for a in session.of(AnswerCallbackQuery)) == ["", TXT_STALE]
    assert d.session_row().ui.get("history", []) == []


async def test_long_html_is_split_with_keyboard_on_last_chunk(env):
    app, session, _d = env
    html = "<b>" + " ".join(["длинный"] * 1500) + "</b>"
    out = Out(replies=[Reply(html, [[Button("Ок", "go")]])])
    await app._send(out, UID, None, 1, 0)
    sends = session.of(SendMessage)
    assert len(sends) >= 3
    for s in sends:
        assert len(s.text) <= 4000
        assert s.text.count("<b>") == s.text.count("</b>") == 1
    assert [bool(s.reply_markup) for s in sends] == [False] * (len(sends) - 1) + [True]


def _fail_consult_send(app, exc_factory, fail_on=1):
    """Подменить send_message: n-я и последующие отправки в консультационный чат падают."""
    real = app.bot.send_message
    state = {"n": 0}

    async def fake(chat_id, *a, **k):
        if chat_id == CONSULT_CHAT:
            state["n"] += 1
            if state["n"] >= fail_on:
                raise exc_factory()
        return await real(chat_id, *a, **k)

    app.bot.send_message = fake
    return real


def _consult_status(app):
    return [r["status"] for r in app.conn.execute("SELECT status FROM consult_requests ORDER BY id")]


async def test_consult_sent_once_on_double_tap(env):
    app, session, d = env
    await d.to_result()
    await d.press("consult")
    send = d.find("consult_send")
    session.clear()
    await asyncio.gather(d.cb(send, update_id=901), d.cb(send, update_id=902))
    to_consult = [c for c in session.of(SendMessage) if c.chat_id == CONSULT_CHAT]
    assert len(to_consult) == 1
    assert TXT_CONSULT_OK in d.sent_texts()
    assert _consult_status(app) == ["sent"]
    # повторная заявка по той же сессии блокируется индексом ещё в транзакции шага
    row = d.session_row()
    await d.cb(f"{row.id}:{row.revision}:consult:")
    again = d.find("consult_send")
    session.clear()
    await d.cb(again)
    assert not [c for c in session.of(SendMessage) if c.chat_id == CONSULT_CHAT]
    assert TXT_CONSULT_DUP in d.sent_texts()
    assert _consult_status(app) == ["sent"]


async def test_consult_pending_written_in_step_transaction(env, monkeypatch):
    app, session, d = env
    await d.to_result()
    await d.press("consult")

    real = repo.add_consult_in_tx

    def boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("crash in step")

    monkeypatch.setattr(repo, "add_consult_in_tx", boom)
    await d.press("consult_send")
    assert _consult_status(app) == []  # откат шага: заявки нет, и ничего не отправлено
    assert not [c for c in session.of(SendMessage) if c.chat_id == CONSULT_CHAT]


async def test_consult_definite_failure_marks_failed_and_can_retry(env):
    app, session, d = env
    await d.to_result()
    await d.press("consult")
    real = _fail_consult_send(app, lambda: TelegramBadRequest(method=None, message="chat not found"))
    await d.press("consult_send")
    assert TXT_CONSULT_FAIL in d.sent_texts()
    assert _consult_status(app) == ["failed"]
    assert not [c for c in session.of(SendMessage) if c.chat_id == CONSULT_CHAT]
    # failed не блокирует ручной повтор
    app.bot.send_message = real
    await d.press("consult")
    await d.press("consult_send")
    assert _consult_status(app) == ["failed", "sent"]


async def test_consult_network_error_is_unknown_and_blocks_resend(env):
    app, session, d = env
    app.config.bot.consult_contact = "@consultant"
    await d.to_result()
    await d.press("consult")
    session.fail_chats.add(CONSULT_CHAT)  # RuntimeError: исход неизвестен
    await d.press("consult_send")
    assert _consult_status(app) == ["unknown"]
    text = d.sent_texts()[-1]
    assert text.startswith(TXT_CONSULT_UNKNOWN) and "@consultant" in text
    assert len([c for c in session.of(SendMessage) if c.chat_id == CONSULT_CHAT]) == 1  # без автоповтора
    session.fail_chats.clear()
    await d.press("consult")
    again = d.find("consult_send")
    session.clear()
    await d.cb(again)
    assert TXT_CONSULT_DUP in d.sent_texts()
    assert not [c for c in session.of(SendMessage) if c.chat_id == CONSULT_CHAT]
    assert _consult_status(app) == ["unknown"]


async def test_consult_retry_after_is_unknown(env):
    app, _session, d = env
    await d.to_result()
    await d.press("consult")
    _fail_consult_send(app, lambda: TelegramRetryAfter(method=None, message="flood", retry_after=5))
    await d.press("consult_send")
    assert _consult_status(app) == ["unknown"]


async def test_consult_partial_multipart_failure_is_unknown(env):
    app, _session, d = env
    await d.to_result()
    await d.press("consult")
    s = d.session_row()
    s.ui["consult_preview"] = " ".join(["слово"] * 3000)  # несколько частей по 4000 символов
    assert repo.save_session(app.conn, s, s.revision, bump=False)
    # вторая часть отклонена Telegram, но первая уже ушла -> unknown, не failed
    _fail_consult_send(app, lambda: TelegramBadRequest(method=None, message="bad"), fail_on=2)
    await d.cb(d.find("consult_send"))
    assert _consult_status(app) == ["unknown"]


async def test_delete_removes_user_data(env):
    app, _session, d = env
    await d.to_result()
    assert app.conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] >= 1
    await d.send("/delete")
    assert "необратимо" in d.sent_texts()[-1]
    await d.cb(next(b for b in d.buttons() if ":del_yes:" in b))  # кнопка несёт одноразовый токен
    for table in ("users", "sessions", "results"):
        assert app.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert "удалены" in d.sent_texts()[-1]


async def test_restart_new_app_continues_same_question(db_path):
    app, session = make_app(db_path)
    d = Driver(app, session)
    await d.send("/start")
    await d.press("go")
    await d.press("ans", "third")
    question_before = d.sent_texts()[-1]
    sid = d.session_row().id
    await app.close()

    app2, session2 = make_app(db_path)
    d2 = Driver(app2, session2)
    d2.n = 1000
    try:
        await d2.send("/start")
        assert "сохранённая анкета" in d2.sent_texts()[-1]
        await d2.press("resume")
        assert d2.sent_texts()[-1] == question_before
        assert d2.session_row().id == sid
    finally:
        await app2.close()


async def test_restart_command_abandons_old_session(env):
    app, _session, d = env
    await d.send("/start")
    await d.press("go")
    old = d.session_row().id
    await d.send("/restart")
    new = d.session_row()
    assert new.id != old and new.state == "start"
    assert app.conn.execute("SELECT status FROM sessions WHERE id=?", (old,)).fetchone()[0] == "abandoned"


async def test_restart_button_starts_new_session(env):
    _app, _session, d = env
    await d.to_result()
    old = d.session_row().id
    await d.press("restart")
    assert d.session_row().id != old
    assert "Найдём возможные основания" in d.sent_texts()[-1]


async def test_media_gets_polite_reply(env):
    app, _session, d = env
    await app.dp.feed_update(app.bot, photo_update(1))
    assert d.sent_texts() == [TXT_MEDIA]
    assert app.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_unchanged_state_does_not_bump_revision(env):
    """Кнопка без изменения анкеты (алерт) не убивает остальные кнопки на экране."""
    _app, session, d = env
    await d.send("/start")
    await d.press("go")
    row = d.session_row()
    keys = d.buttons()
    session.clear()
    await d.cb(f"{row.id}:{row.revision}:back:")
    assert [a.text for a in session.of(AnswerCallbackQuery)] == ["Это первый вопрос."]
    assert d.session_row().revision == row.revision
    await d.cb(keys[0])  # прежняя кнопка вопроса всё ещё работает
    assert d.session_row().revision == row.revision + 1


async def test_save_conflict_sends_no_replies(env):
    app, session, d = env
    await d.send("/start")
    go = d.find("go")
    s = d.session_row()
    # параллельное «другое окно» меняет ревизию между загрузкой и сохранением
    original = app.dialog.handle

    async def racing(sess, ev, snap, user):
        out = await original(sess, ev, snap, user)
        repo.save_session(app.conn, d.session_row(), s.revision)
        return out

    app.dialog.handle = racing
    session.clear()
    await d.cb(go)
    assert d.sent_texts() == [TXT_CONFLICT]


async def test_handler_error_is_reported_without_user_text(env):
    app, _session, d = env

    async def boom(*a, **k):
        raise ValueError("secret user text")

    app.dialog.handle = boom
    await d.send("мой секретный текст")
    assert "ошибка" in d.sent_texts()[-1]
    row = app.conn.execute("SELECT kind, detail FROM events WHERE kind='error'").fetchone()
    assert row["detail"] == "ValueError"


def _update_marked(app, update_id):
    return repo.is_update_processed(app.conn, update_id)


async def test_crash_between_handle_and_commit_leaves_no_partial_save(env, monkeypatch):
    """Сбой внутри транзакции шага (после записи сессии и update_id) откатывает всё."""
    app, session, _d = env
    update = msg_update(77, "/start")

    def boom(*a, **k):
        raise RuntimeError("crash before commit")

    monkeypatch.setattr(repo, "add_event_in_tx", boom)
    await app.dp.feed_update(app.bot, update)
    # сессия создаётся внутри транзакции шага, поэтому откат убирает и её
    assert app.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert not _update_marked(app, 77)
    assert app.conn.execute("SELECT ai_notice_at FROM users").fetchone()[0] is None
    assert not [e for e in app.conn.execute("SELECT kind FROM events") if e["kind"] != "error"]
    # единственное сообщение пользователю — сообщение об ошибке, не welcome
    assert len(session.of(SendMessage)) == 1 and "ошибка" in session.of(SendMessage)[0].text


async def test_same_update_is_reprocessed_after_failure_then_deduped(env, monkeypatch):
    app, session, _d = env
    update = msg_update(78, "/start")
    with monkeypatch.context() as mp:
        mp.setattr(repo, "add_event_in_tx", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        await app.dp.feed_update(app.bot, update)
    assert not _update_marked(app, 78)
    session.clear()
    await app.dp.feed_update(app.bot, update)  # повтор после сбоя
    assert _update_marked(app, 78)
    assert len(session.of(SendMessage)) == 1 and "Найдём возможные основания" in session.of(SendMessage)[0].text
    session.clear()
    await app.dp.feed_update(app.bot, update)  # дубль после успеха
    assert not session.of(SendMessage)


async def test_duplicate_detected_inside_transaction_rolls_back(env):
    """Гонка: update_id помечен между SELECT в middleware и коммитом -> откат и тихий выход."""
    app, session, d = env
    await d.send("/start")
    go = d.find("go")
    s = d.session_row()
    original = app.dialog.handle

    async def racing(sess, ev, snap, user):
        out = await original(sess, ev, snap, user)
        repo.mark_update(app.conn, 600)  # «другой процесс» успел закоммитить этот же update
        return out

    app.dialog.handle = racing
    session.clear()
    await d.cb(go, update_id=600)
    assert not session.of(SendMessage)
    assert d.session_row().revision == s.revision


def test_commit_step_rolls_back_on_conflict(tmp_path):
    conn = open_db(tmp_path / "c.db")
    repo.touch_user(conn, 5)
    s = repo.new_session(conn, 5)
    out = repo.commit_step(conn, update_id=1, tg_user_id=5, session=s, expected_revision=99, bump=True,
                           events=[("e", "d")])
    assert out == "conflict"
    assert not repo.is_update_processed(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert repo.commit_step(conn, update_id=1, tg_user_id=5, session=s, expected_revision=0, bump=True) == "ok"
    assert repo.commit_step(conn, update_id=1, tg_user_id=5, session=s, expected_revision=1, bump=True) == "duplicate"
    assert repo.active_session(conn, 5).revision == 1


def test_cleanup_and_repo_basics(tmp_path):
    conn = open_db(tmp_path / "r.db")
    assert repo.mark_update(conn, 1) and not repo.mark_update(conn, 1)
    repo.touch_user(conn, 5)
    s = repo.new_session(conn, 5)
    s2 = repo.new_session(conn, 5)
    assert repo.active_session(conn, 5).id == s2.id
    assert repo.save_session(conn, s2, 0) and not repo.save_session(conn, s2, 0)
    with repo.write_tx(conn):
        assert repo.add_consult_in_tx(conn, s2.id, 5, "p") is not None
        assert repo.add_consult_in_tx(conn, s2.id, 5, "p") is None
    conn.execute("UPDATE sessions SET updated_at='2000-01-01T00:00:00Z'")
    conn.execute("UPDATE users SET last_seen_at='2000-01-01T00:00:00Z'")
    conn.execute("UPDATE processed_updates SET at='2000-01-01T00:00:00Z'")
    repo.cleanup(conn, 90)
    for table in ("sessions", "users", "processed_updates", "consult_requests"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert s.id != s2.id


def test_cleanup_anonymizes_old_ai_calls(tmp_path):
    conn = open_db(tmp_path / "ai.db")
    ins = ("INSERT INTO ai_calls(at, tg_user_id, kind, model, ok, input_tokens, output_tokens, cost_usd) "
           "VALUES (?, 5, 'jev', 'm', 1, 10, 20, 0.01)")
    conn.execute(ins, ("2000-01-01T00:00:00Z",))
    conn.execute(ins, (repo.iso(repo.utcnow()),))
    repo.cleanup(conn, 90)
    rows = conn.execute("SELECT tg_user_id, input_tokens, cost_usd FROM ai_calls ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0][0] is None and rows[0][1] == 10 and rows[0][2] == 0.01
    assert rows[1][0] == 5


def test_session_ids_are_never_reused(tmp_path):
    conn = open_db(tmp_path / "ids.db")
    repo.touch_user(conn, 5)
    first = repo.new_session(conn, 5)
    repo.delete_user(conn, 5)
    repo.touch_user(conn, 5)
    second = repo.new_session(conn, 5)
    assert second.id != first.id and second.id > first.id


async def test_duplicate_restart_updates_create_one_session(env):
    app, _session, d = env
    await d.send("/start")
    await d.press("go")
    update = msg_update(700, "/restart")
    await app.dp.feed_update(app.bot, update)
    await app.dp.feed_update(app.bot, update)
    await asyncio.gather(app.dp.feed_update(app.bot, msg_update(701, "/restart")),
                         app.dp.feed_update(app.bot, msg_update(701, "/restart")))
    # исходная + /restart(700) + /restart(701): дубли отброшены
    assert app.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3
    assert app.conn.execute("SELECT COUNT(*) FROM sessions WHERE status='active'").fetchone()[0] == 1


async def test_restart_race_inside_transaction_rolls_back_new_session(env):
    app, _session, d = env
    await d.send("/start")
    original = app.dialog.handle

    async def racing(sess, ev, snap, user):
        out = await original(sess, ev, snap, user)
        repo.mark_update(app.conn, 710)  # другой процесс уже обработал этот update
        return out

    app.dialog.handle = racing
    before = app.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    await app.dp.feed_update(app.bot, msg_update(710, "/restart"))
    assert app.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == before
    assert app.conn.execute("SELECT COUNT(*) FROM sessions WHERE status='active'").fetchone()[0] == 1


async def test_handler_error_log_has_no_exception_text(env, caplog):
    app, _session, d = env

    secret = "secret" + " user text"

    async def boom(*a, **k):
        raise ValueError(secret)

    app.dialog.handle = boom
    with caplog.at_level(logging.ERROR):
        await d.send("hello")
    assert "ValueError" in caplog.text and secret not in caplog.text
