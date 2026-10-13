"""API мини-аппа на FastAPI TestClient с фейковыми interpreter/explainer/bot (это НЕ живой Telegram и НЕ живой AI)."""

import pytest
from aiogram.methods import SendMessage
from fastapi.testclient import TestClient

from bginfo.ai.explain import apply_template
from bginfo.ai.interpret import Interpretation
from bginfo.bot import repo
from bginfo.bot.app import TXT_CONSULT_OK
from bginfo.config import AppConfig, Settings
from bginfo.db.conn import open_db
from bginfo.engine.facts import MAIN
from bginfo.kb import releases as R
from bginfo.stats.launches import launches
from bginfo.webapp.app import create_app
from bginfo.webapp.service import build_service
from tests.fake_tg import make_bot
from tests.kbfixture import mini_bundle
from tests.test_webapp_auth import TOKEN, make_init_data

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
    path = tmp_path / "web.db"
    conn = open_db(path)
    R._cache.clear()
    R.replace_draft(conn, mini_bundle(), "admin", "test")
    R.publish_draft(conn, "admin")
    conn.close()
    return path


class Env:
    def __init__(self, db_path, **cfg):
        self.settings = Settings(DB_PATH=str(db_path), CONSULT_CHAT_ID=CONSULT_CHAT, APP_MODE="test",
                                 TELEGRAM_BOT_TOKEN=TOKEN, _env_file=None)
        self.config = AppConfig()
        for k, v in cfg.items():
            setattr(self.config.webapp, k, v)
        self.bot, self.tg = make_bot()
        self.service = build_service(self.settings, self.config, bot=self.bot, interpreter=FakeInterp(),
                                     explainer=FakeExplainer())
        self.app = create_app(self.settings, self.config, service=self.service)
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "tma " + make_init_data()}

    def open(self):
        return self.client.post("/api/open", json={}, headers=self.headers)

    def step(self, cur, kind="button", action="", arg="", text="", **over):
        body = {"sid": cur["sid"], "rev": cur["rev"], "kind": kind, "action": action, "arg": arg, "text": text}
        body.update(over)
        return self.client.post("/api/step", json=body, headers=self.headers)

    def to_review(self, cur):
        r = self.step(cur, action="go")
        for _ in range(15):
            cur = r.json()
            q = (cur["view"] or {}).get("question")
            if q is None:
                break
            if q["qid"] == "q_cit":
                r = self.step(cur, action="ans", arg="third")
            elif q["qid"] == "q_inc":
                r = self.step(cur, action="multi", arg="pension")
            elif q["qid"] == "q_pens":
                r = self.step(cur, kind="text", text="900")
            elif q["qid"] == "q_house":
                r = self.step(cur, action="ans", arg="yes")
            else:
                r = self.step(cur, action="unk")
        return r

    def sends(self, chat):
        return [c for c in self.tg.of(SendMessage) if c.chat_id == chat]

    def db(self):
        return open_db(self.settings.DB_PATH)


@pytest.fixture
def env(db_path):
    e = Env(db_path)
    with e.client:  # lifespan: закрытие сервиса в конце
        yield e


def test_healthz_and_index(env):
    assert env.client.get("/healthz").json() == {"ok": True}
    assert env.client.get("/").status_code == 200


def test_static_client_served(env):
    r = env.client.get("/")
    assert r.status_code == 200 and "telegram-web-app.js" in r.text and "/static/app.js" in r.text
    assert "script-src 'self' https://telegram.org" in r.headers["content-security-policy"]
    for name in ("app.js", "app.css"):
        assert env.client.get(f"/static/{name}").status_code == 200
    assert "<script>" not in r.text  # CSP запрещает inline-скрипты


def test_unauthorized(env):
    assert env.client.post("/api/open", json={}).status_code == 401
    bad = {"Authorization": "tma " + make_init_data(bad_hash=True)}
    assert env.client.post("/api/open", json={}, headers=bad).status_code == 401
    assert env.client.post("/api/step", json={"kind": "text", "text": "x"}, headers={"Authorization": "x"}
                           ).status_code == 401
    old = {"Authorization": "tma " + make_init_data(auth_date=1)}
    assert env.client.post("/api/open", json={}, headers=old).status_code == 401


def test_body_limit_and_bad_requests(env):
    big = {"kind": "text", "text": "x" * 9000}
    assert env.client.post("/api/step", json=big, headers=env.headers).status_code == 413
    assert env.client.post("/api/step", json={"kind": "nope"}, headers=env.headers).status_code == 400
    assert env.client.post("/api/step", content=b"{oops", headers=env.headers).status_code == 400
    assert env.client.post("/api/step", json={"kind": "text", "text": "a", "sid": 1}, headers=env.headers
                           ).status_code == 400


def test_full_flow_to_result_and_copy_once(env):
    r = env.open()
    assert r.status_code == 200
    cur = r.json()
    assert cur["replies"][0]["buttons"] and cur["view"] is None and cur["sid"] and cur["rev"] is not None
    assert {"text", "action", "arg"} <= set(cur["replies"][0]["buttons"][0][0])
    r = env.to_review(cur)
    assert r.json()["view"]["review"]["lines"]
    assert not env.sends(UID)  # до результата в чат ничего не уходит
    r = env.step(r.json(), action="review_ok")
    body = r.json()
    assert r.status_code == 200 and body["view"]["result"]["view"]["primary"]["ground_id"] == "pensioner"
    sent = env.sends(UID)
    assert len(sent) == 1 and sent[0].text.startswith("📱 Результат из формы") and "Пенсионер" in sent[0].text
    conn = env.db()
    assert conn.execute("SELECT channel FROM sessions").fetchone()[0] == "webapp"
    assert conn.execute("SELECT channel FROM results").fetchone()[0] == "webapp"
    assert conn.execute("SELECT detail FROM events WHERE kind='session_started'").fetchone()[0] == "webapp"
    assert launches(conn, 1)["rows"][0]["channel"] == "webapp"
    conn.close()


def test_copy_sent_only_after_commit(env):
    seen = {}
    real = env.bot.send_message

    async def spy(chat_id, *a, **k):
        conn = env.db()
        seen["results"] = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        conn.close()
        return await real(chat_id, *a, **k)

    env.bot.send_message = spy
    cur = env.to_review(env.open().json()).json()
    assert env.step(cur, action="review_ok").status_code == 200
    assert seen["results"] == 1


def test_copy_failure_does_not_break_response(env):
    env.tg.fail_chats.add(UID)
    cur = env.to_review(env.open().json()).json()
    r = env.step(cur, action="review_ok")
    assert r.status_code == 200 and r.json()["view"]["result"]
    assert env.db().execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
    assert len(env.sends(UID)) == 1  # попытка была одна, без автоповтора


def test_copy_disabled_by_config(db_path):
    e = Env(db_path, send_result_to_chat=False)
    with e.client:
        cur = e.to_review(e.open().json()).json()
        assert e.step(cur, action="review_ok").status_code == 200
    assert not e.sends(UID)


def test_stale_revision_returns_409_with_current_screen(env):
    cur = env.open().json()
    first = env.step(cur, action="go").json()
    conn = env.db()
    before = repo.active_session(conn, UID)
    # повторное применение старой ревизии (двойное нажатие / ответ в другом окне)
    r = env.step(cur, action="go")
    assert r.status_code == 409
    body = r.json()
    assert body["view"]["question"]["qid"] == first["view"]["question"]["qid"] and body["sid"] == first["sid"]
    after = repo.active_session(conn, UID)
    assert (after.state, after.profile.to_dict()) == (before.state, before.profile.to_dict())
    # присланный ответ на старый экран не применяется
    r = env.step(first, action="ans", arg="third").json()
    stale = env.step(first, action="ans", arg="bg")
    assert stale.status_code == 409
    assert repo.active_session(conn, UID).profile.value(MAIN, "citizenship_group") == "third"
    assert r["rev"] != first["rev"]
    conn.close()


def test_step_without_ref_when_session_exists_is_409(env):
    env.open()
    r = env.client.post("/api/step", json={"kind": "button", "action": "go"}, headers=env.headers)
    assert r.status_code == 409 and r.json()["replies"]


def test_step_without_session_starts_new(env):
    r = env.client.post("/api/step", json={"kind": "button", "action": "go"}, headers=env.headers)
    assert r.status_code == 200 and r.json()["view"]["question"]["qid"] == "q_cit"


def test_shared_session_with_bot(env):
    """Сессия, начатая «в чате» (repo), продолжается в форме."""
    conn = env.db()
    repo.touch_user(conn, UID)
    s = repo.blank_session()
    s.state = "asking"
    assert repo.commit_step(conn, update_id=None, tg_user_id=UID, session=s, expected_revision=0, bump=True,
                            new_session=True) == "ok"
    conn.close()
    r = env.open()
    assert r.status_code == 200 and r.json()["sid"] == s.id


def test_restart_creates_new_session(env):
    cur = env.to_review(env.open().json()).json()
    r = env.step(cur, action="restart")
    assert r.status_code == 200 and r.json()["sid"] != cur["sid"]


def test_consult_alert(env):
    cur = env.to_review(env.open().json()).json()
    cur = env.step(cur, action="review_ok").json()
    cur = env.step(cur, action="consult").json()
    r = env.step(cur, action="consult_send")
    assert r.status_code == 200 and r.json()["alert"] == TXT_CONSULT_OK
    assert len(env.sends(CONSULT_CHAT)) == 1



# --------------------------------------------------------------------------- ревью: гонка, восстановление, заявка


def test_first_session_race_does_not_abandon_other_session(env):
    real = env.service._load
    fired = []

    def racing(*a, **k):
        res = real(*a, **k)
        if not fired:  # «бот» создаёт сессию между загрузкой и коммитом формы
            fired.append(1)
            other = env.db()
            repo.new_session(other, UID)
            other.close()
        return res

    env.service._load = racing
    r = env.open()
    assert r.status_code == 409 and (r.json()["view"] or r.json()["replies"])
    conn = env.db()
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE status='abandoned'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    conn.close()


def test_stale_recovery_in_result_state_does_not_mutate(env):
    review = env.to_review(env.open().json()).json()
    result = env.step(review, action="review_ok").json()
    conn = env.db()
    before = repo.active_session(conn, UID)
    calls = []
    real = env.service.dialog.explainer.explain

    async def spy(*a, **k):
        calls.append(1)
        return await real(*a, **k)

    env.service.dialog.explainer.explain = spy
    copies = len(env.sends(UID))
    r = env.step(review, action="review_ok")  # старая ревизия: ответ из другого окна
    assert r.status_code == 409
    body = r.json()
    assert body["stale"] and body["view"]["result"]["view"]["primary"]["ground_id"] == "pensioner"
    assert body["rev"] == result["rev"] == before.revision and body["sid"] == before.id
    after = repo.active_session(conn, UID)
    assert after.state == "result" and after.revision == before.revision
    assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
    assert not calls and len(env.sends(UID)) == copies
    conn.close()


def test_consult_send_returns_result_screen_and_alert(env):
    cur = env.to_review(env.open().json()).json()
    cur = env.step(cur, action="review_ok").json()
    cur = env.step(cur, action="consult").json()
    body = env.step(cur, action="consult_send").json()
    assert body["alert"] == TXT_CONSULT_OK
    assert body["view"]["result"]["view"]["primary"]["ground_id"] == "pensioner"
    actions = [b["action"] for r in body["replies"] for row in r["buttons"] for b in row]
    assert "consult" in actions and "restart" in actions
    conn = env.db()
    assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1  # новый результат не записан
    conn.close()


def test_failed_consult_keeps_result_screen_and_can_retry(env):
    from aiogram.exceptions import TelegramBadRequest

    from bginfo.bot.app import TXT_CONSULT_FAIL

    cur = env.to_review(env.open().json()).json()
    cur = env.step(cur, action="review_ok").json()
    cur = env.step(cur, action="consult").json()
    real = env.bot.send_message

    async def failing(chat_id, *a, **k):
        if chat_id == CONSULT_CHAT:
            raise TelegramBadRequest(method=None, message="chat not found")
        return await real(chat_id, *a, **k)

    env.bot.send_message = failing
    body = env.step(cur, action="consult_send").json()
    assert body["alert"] == TXT_CONSULT_FAIL and body["view"]["result"]
    assert "consult" in [b["action"] for r in body["replies"] for row in r["buttons"] for b in row]
    env.bot.send_message = real
    again = env.step(body, action="consult").json()
    assert "Заявка на консультацию" in again["replies"][-1]["html"]
    assert env.step(again, action="consult_send").json()["alert"] == TXT_CONSULT_OK


def test_result_view_has_disclaimer(env):
    cur = env.to_review(env.open().json()).json()
    body = env.step(cur, action="review_ok").json()
    res = body["view"]["result"]
    assert res["disclaimer"] and res["disclaimer"] in res["html"]


def test_static_css_has_no_modern_only_features(env):
    css = env.client.get("/static/app.css").text
    assert "color-mix" not in css and ":has(" not in css  # старые Telegram WebView их не знают
    js = env.client.get("/static/app.js").text
    assert "syncOpts" in js and "infoNotes" in js and "freeLink" in js
