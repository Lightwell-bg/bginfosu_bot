"""Тесты админки: вход, CSRF, редактирование, публикация, откат, предпросмотр, XSS."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from bginfo.admin.app import create_app
from bginfo.admin.common import AdminConfigError
from bginfo.config import AppConfig, Settings
from bginfo.db.conn import iso, open_db, utcnow
from bginfo.kb import releases as R
from tests.kbfixture import mini_bundle

PASSWORD = "correct horse battery staple"
SOFIA = ZoneInfo("Europe/Sofia")


# --------------------------------------------------------------------------- helpers


class _Harvest(HTMLParser):
    """Собирает поля формы как браузер: без <template>, только отмеченные чекбоксы и выбранные option."""

    def __init__(self, action: str):
        super().__init__()
        self.action, self.in_form, self.done, self.tpl = action, False, False, 0
        self.fields: list[tuple[str, str]] = []
        self._sel: dict | None = None
        self._ta: str | None = None
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "template":
            self.tpl += 1
            return
        if self.tpl:
            return
        if tag == "form":
            if not self.done and a.get("action") == self.action:
                self.in_form = True
            return
        if not self.in_form:
            return
        if tag == "input":
            name, t = a.get("name"), a.get("type", "text")
            if not name or "disabled" in a or t == "submit":
                return
            if t in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((name, a.get("value") or "on"))
            else:
                self.fields.append((name, a.get("value") or ""))
        elif tag == "select":
            self._sel = {"name": a.get("name"), "multiple": "multiple" in a, "picked": [], "first": None}
        elif tag == "option" and self._sel is not None:
            v = a.get("value", "")
            if self._sel["first"] is None:
                self._sel["first"] = v
            if "selected" in a:
                self._sel["picked"].append(v)
        elif tag == "textarea":
            self._ta, self._buf = a.get("name"), ""

    def handle_data(self, data):
        if self._ta is not None and not self.tpl:
            self._buf += data

    def handle_endtag(self, tag):
        if tag == "template":
            self.tpl -= 1
        elif tag == "form" and self.in_form:
            self.in_form, self.done = False, True
        elif tag == "select" and self._sel is not None:
            s = self._sel
            picked = s["picked"] or ([] if s["multiple"] or s["first"] is None else [s["first"]])
            self.fields += [(s["name"], v) for v in picked]
            self._sel = None
        elif tag == "textarea" and self._ta is not None:
            if self.in_form:
                self.fields.append((self._ta, self._buf.removeprefix("\n")))
            self._ta = None


def harvest(html: str, action: str) -> dict[str, list[str]]:
    p = _Harvest(action)
    p.feed(html)
    assert p.done, f"форма {action} не найдена"
    out: dict[str, list[str]] = {}
    for k, v in p.fields:
        out.setdefault(k, []).append(v)
    return out


def setv(data: dict, name: str, *values: str) -> None:
    data[name] = list(values)


def make_ctx(tmp_path, *, trust_proxy=False, mode="test", max_attempts=3, bundle=None):
    R._cache.clear()
    db = tmp_path / "admin.db"
    settings = Settings(_env_file=None, ADMIN_USERNAME="boss", ADMIN_PASSWORD=PASSWORD,
                        ADMIN_SESSION_SECRET="s" * 48, ADMIN_COOKIE_SECURE=False, APP_MODE=mode, DB_PATH=str(db))
    config = AppConfig()
    config.admin.login_max_attempts = max_attempts
    config.admin.trust_proxy = trust_proxy
    app = create_app(settings, config)
    conn = open_db(db)
    R.replace_draft(conn, bundle or mini_bundle(), "seed", "test")
    client = TestClient(app, follow_redirects=False)
    return SimpleNamespace(client=client, conn=conn, app=app, settings=settings)


def do_login(ctx, headers=None, password=PASSWORD):
    page = ctx.client.get("/login")
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    return ctx.client.post("/login", data={"csrf_token": tok, "username": "boss", "password": password},
                           headers=headers or {})


def pub(conn, ago: int = 300):
    """Выпуск, действующий уже `ago` секунд (в прошлом, чтобы не конфликтовать с публикацией «сейчас»)."""
    return R.publish_draft(conn, "seed", now=utcnow() - timedelta(seconds=ago))


def edit_draft(conn, kind, item_id, **changes):
    data, rev = R.draft_item(conn, kind, item_id)
    data.update(changes)
    R.save_draft_item(conn, kind, data, "other", rev, item_id)


def edit_url(page: str) -> str:
    """Форма правки основания живёт на /grounds/<id>/edit (на /grounds/<id> — карточка)."""
    if page.startswith("/grounds/") and not page.endswith(("/_new", "/edit")):
        return page + "/edit"
    return page


def post_form(ctx, page: str, action: str, mutate=None):
    page = edit_url(page)
    data = harvest(ctx.client.get(page).text, action)
    if mutate:
        mutate(data)
    return ctx.client.post(action, data=data)


@pytest.fixture
def ctx(tmp_path):
    c = make_ctx(tmp_path)
    assert do_login(c).status_code == 303
    yield c
    c.conn.close()


@pytest.fixture
def anon(tmp_path):
    c = make_ctx(tmp_path)
    yield c
    c.conn.close()


# --------------------------------------------------------------------------- вход и безопасность


def test_redirects_to_login_without_session(anon):
    for path in ("/", "/grounds", "/grounds/pensioner", "/publish", "/history", "/preview", "/stats"):
        r = anon.client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert anon.client.post("/grounds/pensioner", data={"title": "x"}).status_code == 403


def test_wrong_password_then_login(anon):
    r = do_login(anon, password="wrong password")
    assert r.status_code == 401 and "Неверный логин или пароль" in r.text
    r = do_login(anon)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    assert anon.client.get("/").status_code == 200


def test_lockout_after_n_failures(anon):
    for _ in range(3):
        assert do_login(anon, password="nope nope nope").status_code == 401
    r = do_login(anon)  # верный пароль, но IP уже заблокирован
    assert r.status_code == 429 and "Слишком много" in r.text
    assert anon.client.get("/").status_code == 303


def test_forwarded_for_ignored_by_default(anon):
    for i in range(3):
        do_login(anon, headers={"X-Forwarded-For": f"9.9.9.{i}"}, password="nope nope nope")
    assert do_login(anon, headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 429


def test_forwarded_for_used_with_trust_proxy(tmp_path):
    c = make_ctx(tmp_path, trust_proxy=True)
    try:
        for _ in range(3):
            do_login(c, headers={"X-Forwarded-For": "1.1.1.1"}, password="nope nope nope")
        assert do_login(c, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
        assert do_login(c, headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 303
    finally:
        c.conn.close()


def test_post_without_csrf_is_403(ctx):
    before = R.draft_item(ctx.conn, "ground", "pensioner")
    assert ctx.client.post("/grounds/pensioner", data={"title": "взлом"}).status_code == 403
    assert ctx.client.post("/grounds/pensioner", data={"title": "взлом", "csrf_token": "wrong"}).status_code == 403
    assert ctx.client.post("/logout", data={}).status_code == 403
    assert ctx.client.post("/publish", data={"draft_revision": "1"}).status_code == 403
    assert R.draft_item(ctx.conn, "ground", "pensioner") == before


def test_healthz_and_security_headers(anon):
    r = anon.client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    h = anon.client.get("/login").headers
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
    assert h["content-security-policy"].startswith("default-src 'self'")
    assert anon.client.get("/docs").status_code in (303, 404)


def test_no_inline_scripts(ctx):
    html = ctx.client.get("/grounds/pensioner").text
    assert '<script src="/static/admin.js"' in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and " onclick=" not in html
    assert ctx.client.get("/static/admin.js").status_code == 200


def test_startup_requires_password_and_secret():
    with pytest.raises(AdminConfigError) as e:
        create_app(Settings(_env_file=None, ADMIN_PASSWORD="", ADMIN_SESSION_SECRET="short"), AppConfig())
    assert "ADMIN_PASSWORD" in str(e.value) and "ADMIN_SESSION_SECRET" in str(e.value)


def test_startup_fails_without_password(tmp_path):
    settings = Settings(_env_file=None, ADMIN_SESSION_SECRET="s" * 48, DB_PATH=str(tmp_path / "a.db"))
    with pytest.raises(AdminConfigError, match="Задайте ADMIN_PASSWORD"):
        create_app(settings, AppConfig())


def test_password_hashed_in_memory_not_stored_plain(anon):
    st = anon.app.state.admin
    assert st.password_hash.startswith("$argon2")
    assert st.settings.ADMIN_PASSWORD is None
    assert PASSWORD not in repr(st.password_hash)


# --------------------------------------------------------------------------- редактирование


CASES = [
    ("/grounds/pensioner", "ground", "pensioner"), ("/grounds/nomad", "ground", "nomad"),
    ("/grounds/family", "ground", "family"),  # sponsor_basis -> расширенный режим JSON
    ("/params/min_wage", "parameter", "min_wage"), ("/params/dn_income", "parameter", "dn_income"),
    ("/facts/citizenship_group", "fact", "citizenship_group"), ("/facts/pension_amount_eur", "fact", "pension_amount_eur"),
    ("/questions/q_pens", "question", "q_pens"), ("/sources/src_law", "source", "src_law"),
]


@pytest.mark.parametrize("path,kind,item_id", CASES)
def test_form_roundtrip_changes_nothing(ctx, path, kind, item_id):
    before, rev = R.draft_item(ctx.conn, kind, item_id)
    r = post_form(ctx, path, path)
    assert r.status_code == 303, r.text[:2000]
    after, rev2 = R.draft_item(ctx.conn, kind, item_id)
    assert after == before and rev2 == rev + 1


def test_edit_ground_changes_draft_not_active(ctx):
    pub(ctx.conn)
    r = post_form(ctx, "/grounds/pensioner", "/grounds/pensioner", lambda d: setv(d, "title", "Пенсионер ПЛЮС"))
    assert r.status_code == 303
    assert R.draft_item(ctx.conn, "ground", "pensioner")[0]["title"] == "Пенсионер ПЛЮС"
    assert R.active_snapshot(ctx.conn).bundle.ground("pensioner").title == "Пенсионер"
    actor = ctx.conn.execute("SELECT actor FROM audit_log WHERE action='draft_save_ground' ORDER BY id DESC").fetchone()
    assert actor["actor"] == "boss"
    assert "Сохранено" in ctx.client.get("/grounds/pensioner").text


def test_validation_errors_are_shown_in_russian_with_entered_data(ctx):
    def bad(d):
        setv(d, "title", "Введённое название")
        setv(d, "valid_from", "не-дата")
        setv(d, "req-0-id", "BAD ID")

    r = post_form(ctx, "/grounds/pensioner", "/grounds/pensioner", bad)
    assert r.status_code == 422
    assert "дата" in r.text and "ID" in r.text and "Введённое название" in r.text
    assert R.draft_item(ctx.conn, "ground", "pensioner")[0]["title"] == "Пенсионер"


def test_concurrent_edit_keeps_other_change(ctx):
    data = harvest(ctx.client.get("/grounds/pensioner/edit").text, "/grounds/pensioner")
    edit_draft(ctx.conn, "ground", "pensioner", title="Чужая правка")
    setv(data, "title", "Моя правка")
    r = ctx.client.post("/grounds/pensioner", data=data)
    assert r.status_code == 409 and "Изменено кем-то ещё" in r.text
    assert "Моя правка" in r.text  # введённые данные остались в форме
    assert R.draft_item(ctx.conn, "ground", "pensioner")[0]["title"] == "Чужая правка"


def test_condition_builder_and_advanced_json(ctx):
    def mutate(d):
        setv(d, "req-1-cond-l0-mult", "2")  # конструктор: множитель показателя
        setv(d, "req-1-cond-combine", "and")
        d["req-1-cond-lidx"] = ["0", "x1"]  # второй лист
        for k, v in {"type": "known_true", "fact": "case.has_housing"}.items():
            setv(d, f"req-1-cond-lx1-{k}", v)
        setv(d, "req-0-cond-use_json", "on")  # расширенный режим: or из двух условий
        setv(d, "req-0-cond-json", json.dumps({"op": "or", "any": [
            {"op": "in", "fact": "self.income_sources", "values": ["pension"]},
            {"op": "not", "cond": {"op": "known_true", "fact": "case.has_housing"}}]}))

    assert post_form(ctx, "/grounds/pensioner", "/grounds/pensioner", mutate).status_code == 303
    reqs = {r["id"]: r for r in R.draft_item(ctx.conn, "ground", "pensioner")[0]["requirements"]}
    c = reqs["pension_amount"]["cond"]
    assert c["op"] == "and" and c["all"][0]["multiplier"] == 2.0 and c["all"][1]["op"] == "known_true"
    assert reqs["has_pension"]["cond"]["op"] == "or"
    html = ctx.client.get("/grounds/pensioner/edit").text
    assert "ХОТЯ БЫ ОДНО из" in html and "расширенном режиме" in html  # сложное условие показано текстом + JSON


def test_invalid_json_condition_shows_error_and_keeps_text(ctx):
    def mutate(d):
        setv(d, "req-2-cond-use_json", "on")
        setv(d, "req-2-cond-json", '{"op": "eq", "fact": "bad fact"}')

    r = post_form(ctx, "/grounds/pensioner", "/grounds/pensioner", mutate)
    assert r.status_code == 422 and "&#34;bad fact&#34;" in r.text or "bad fact" in r.text
    assert "факт" in r.text
    r = post_form(ctx, "/grounds/pensioner", "/grounds/pensioner",
                  lambda d: (setv(d, "req-2-cond-use_json", "on"), setv(d, "req-2-cond-json", "{не json")))
    assert r.status_code == 422 and "некорректный JSON" in r.text


def test_research_fields_and_unverified_column(ctx):
    def mutate(d):
        setv(d, "research_note", "Заметка по основанию")
        setv(d, "req-0-research_status", "bginfo_only")
        setv(d, "req-0-research_note", "Только BGINFO")
        d.pop("req-1-verified", None)  # снять «подтверждено»

    assert post_form(ctx, "/grounds/pensioner", "/grounds/pensioner", mutate).status_code == 303
    g = R.draft_item(ctx.conn, "ground", "pensioner")[0]
    assert g["research_note"] == "Заметка по основанию"
    assert g["requirements"][0]["research_status"] == "bginfo_only" and g["requirements"][1]["verified"] is False
    page = ctx.client.get("/grounds/pensioner/edit").text
    assert "только BGINFO" in page and "не одобрение" in page
    assert "1 условие не подтверждено" in ctx.client.get("/grounds").text


def test_create_and_delete_items(ctx):
    r = ctx.client.post("/sources/_new", data={**harvest(ctx.client.get("/sources/_new").text, "/sources/_new"),
                                               "id": ["new_src"], "kind": ["law"], "title": ["Новый"],
                                               "url": ["https://example.org/x"]})
    assert r.status_code == 303 and R.draft_item(ctx.conn, "source", "new_src") is not None
    dup = ctx.client.post("/sources/_new", data={**harvest(ctx.client.get("/sources/_new").text, "/sources/_new"),
                                                 "id": ["new_src"], "kind": ["law"], "title": ["Дубль"],
                                                 "url": ["https://example.org/x"]})
    assert dup.status_code == 422 and "уже существует" in dup.text
    bad = ctx.client.post("/sources/_new", data={**harvest(ctx.client.get("/sources/_new").text, "/sources/_new"),
                                                 "id": ["s3"], "kind": ["law"], "title": ["x"], "url": ["http://no-https"]})
    assert bad.status_code == 422 and "https://" in bad.text
    assert post_form(ctx, "/sources/new_src", "/sources/new_src/delete").status_code == 303
    assert R.draft_item(ctx.conn, "source", "new_src") is None


def test_create_fact_with_options_and_param(ctx):
    def fact(d):
        d.update({"key": ["pets"], "label": ["Питомцы"], "scope": ["case"], "type": ["enum"],
                  "opt_idx": ["a", "b"], "opt-a-value": ["dog"], "opt-a-label": ["Собака"], "opt-a-detect": ["Указана ли собака?"],
                  "opt-b-value": ["cat"], "opt-b-label": ["Кошка"], "opt-b-detect": [""], "unit_label": [""]})

    assert post_form(ctx, "/facts/_new", "/facts/_new", fact).status_code == 303
    f = R.draft_item(ctx.conn, "fact", "pets")[0]
    assert [o["value"] for o in f["options"]] == ["dog", "cat"] and f["options"][0]["detect"] == "Указана ли собака?"

    def param(d):
        d.update({"id": ["fee"], "label": ["Сбор"], "unit": ["EUR"], "kind": ["values"], "val_idx": ["a"],
                  "val-a-value": ["12,5"], "val-a-valid_from": ["2026-01-01"], "val-a-valid_to": [""],
                  "val-a-source_ids": ["src_law"], "val-a-verified": ["on"], "val-a-research_status": ["consolidated"]})

    assert post_form(ctx, "/params/_new", "/params/_new", param).status_code == 303
    p = R.draft_item(ctx.conn, "parameter", "fee")[0]
    assert p["values"][0]["value"] == 12.5 and p["values"][0]["research_status"] == "consolidated"


def test_param_page_shows_affected_grounds(ctx):
    html = ctx.client.get("/params/min_wage").text
    assert "Затронет основания" in html
    assert "/grounds/pensioner" in html and "/grounds/nomad" in html  # nomad — через производный dn_income
    assert "/grounds/family" not in html
    assert "/grounds/nomad" in ctx.client.get("/params/dn_income").text


# --------------------------------------------------------------------------- публикация и история


def test_dashboard_shows_release_and_dirty_draft(ctx):
    assert "Бот пока не отвечает пользователям: база не опубликована" in ctx.client.get("/").text
    rid = pub(ctx.conn)
    html = ctx.client.get("/").text
    assert f"опубликована версия №{rid}" in html and "Бот работает" in html and "Есть изменения, которые бот ещё не видит" not in html
    edit_draft(ctx.conn, "ground", "pensioner", title="Другое")
    assert "Есть изменения, которые бот ещё не видит: 1" in ctx.client.get("/").text


def test_publish_from_form_is_seen_by_bot_immediately(ctx):
    old = pub(ctx.conn)
    edit_draft(ctx.conn, "ground", "pensioner", title="Пенсионер v2")
    page = ctx.client.get("/publish").text
    assert "Пенсионер v2" in page and "Пенсионер" in page  # поэлементное «было / стало»
    r = post_form(ctx, "/publish", "/publish", lambda d: setv(d, "comment", "второй выпуск"))
    assert r.status_code == 303 and r.headers["location"] == "/history"
    snap = R.active_snapshot(ctx.conn)
    assert snap.release_id > old and snap.bundle.ground("pensioner").title == "Пенсионер v2"
    row = ctx.conn.execute("SELECT created_by, comment FROM kb_releases WHERE id=?", (snap.release_id,)).fetchone()
    assert row["created_by"] == "boss" and row["comment"] == "второй выпуск"


def test_publish_rejected_if_draft_changed_after_review(ctx):
    pub(ctx.conn)
    data = harvest(ctx.client.get("/publish").text, "/publish")
    edit_draft(ctx.conn, "ground", "pensioner", title="Неожиданная правка")
    r = ctx.client.post("/publish", data=data)
    assert r.status_code == 409 and "черновик изменился" in r.text
    assert len(R.list_releases(ctx.conn)) == 1


def test_future_publish_is_scheduled_and_can_be_cancelled(ctx):
    base = pub(ctx.conn)
    edit_draft(ctx.conn, "ground", "pensioner", title="Будущее название")
    local = (datetime.now(SOFIA) + timedelta(days=2)).replace(second=0, microsecond=0, tzinfo=None)
    r = post_form(ctx, "/publish", "/publish",
                  lambda d: setv(d, "effective_from", local.strftime("%Y-%m-%dT%H:%M")))
    assert r.status_code == 303
    rel = R.list_releases(ctx.conn)[0]
    expected = iso(local.replace(tzinfo=SOFIA).astimezone(UTC))  # Europe/Sofia -> UTC
    assert rel["effective_from"] == expected and rel["status"] == "published"
    assert R.active_snapshot(ctx.conn).release_id == base  # пока действует старый
    assert "запланирован" in ctx.client.get("/history").text
    assert "запланирован" in ctx.client.get("/").text.lower() or str(rel["id"]) in ctx.client.get("/").text
    r = post_form(ctx, "/history", f"/history/{rel['id']}/cancel")
    assert r.status_code == 303
    assert R.list_releases(ctx.conn)[0]["status"] == "cancelled"
    assert R.active_snapshot(ctx.conn).release_id == base


def test_rollback_creates_new_release(ctx):
    first = pub(ctx.conn, 300)
    edit_draft(ctx.conn, "ground", "pensioner", title="Пенсионер v2")
    second = pub(ctx.conn, 200)
    assert R.active_snapshot(ctx.conn).release_id == second
    r = post_form(ctx, "/history", f"/history/{first}/rollback")
    assert r.status_code == 303
    rels = R.list_releases(ctx.conn)
    assert len(rels) == 3 and rels[0]["kind"] == "rollback" and rels[0]["based_on"] == first
    snap = R.active_snapshot(ctx.conn)
    assert snap.release_id == rels[0]["id"] and snap.bundle.ground("pensioner").title == "Пенсионер"
    assert f"№{first}" in ctx.client.get(f"/releases/{second}").text


def test_invalid_draft_cannot_be_published_and_errors_are_shown(ctx):
    edit_draft(ctx.conn, "ground", "pensioner", bginfo_source_ids=["nope"])
    page = ctx.client.get("/publish")
    assert "нет источника" in page.text and re.search(r"<button[^>]*disabled", page.text)
    r = post_form(ctx, "/publish", "/publish")  # обходим disabled
    assert r.status_code == 422 and "нет источника" in r.text and "Публикация отклонена" in r.text
    assert R.list_releases(ctx.conn) == []


def test_load_release_into_draft(ctx):
    rid = pub(ctx.conn)
    edit_draft(ctx.conn, "ground", "pensioner", title="Черновая правка")
    assert post_form(ctx, "/history", f"/history/{rid}/to-draft").status_code == 303
    assert R.draft_item(ctx.conn, "ground", "pensioner")[0]["title"] == "Пенсионер"
    assert 'data-confirm="Текущий черновик будет ЗАМЕНЁН' in ctx.client.get("/history").text


def test_flag_needs_review(ctx):
    pub(ctx.conn)
    assert "pensioner" in ctx.client.get("/needs-review").text
    r = post_form(ctx, "/needs-review/pensioner", "/needs-review/pensioner", lambda d: (
        setv(d, "reason", "Закон изменился"), setv(d, "requirement_ids", "has_pension")))
    assert r.status_code == 303
    snap = R.active_snapshot(ctx.conn)
    g = snap.bundle.ground("pensioner")
    assert g.status == "needs_review" and g.review_reason == "Закон изменился"
    assert R.list_releases(ctx.conn)[0]["kind"] == "review_flag"
    empty = post_form(ctx, "/needs-review/nomad", "/needs-review/nomad", lambda d: setv(d, "reason", "  "))
    assert empty.status_code == 422 and "укажите причину" in empty.text


def test_demo_release_in_production_shows_red_warning(tmp_path):
    c = make_ctx(tmp_path, mode="production", bundle=mini_bundle().model_copy(update={"demo": True}))
    try:
        assert do_login(c).status_code == 303
        pub(c.conn)
        assert "демонстрационная" in c.client.get("/").text
    finally:
        c.conn.close()


# --------------------------------------------------------------------------- тестовая анкета, статистика, XSS


PREVIEW = {"source": "draft", "as_of": "2026-10-09", "f-p0-citizenship_group": "third",
           "f-p0-income_sources": "pension", "f-p0-pension_amount_eur": "1000", "f-case-has_housing": "yes"}


def preview(ctx, **over):
    data = harvest(ctx.client.get("/preview").text, "/preview")
    data.update({k: [v] for k, v in {**PREVIEW, **over}.items() if v is not None})
    return ctx.client.post("/preview", data=data)


def test_preview_gives_statuses_and_bot_message(ctx):
    r = preview(ctx)
    assert r.status_code == 200
    assert "Предварительно подходит" in r.text and "Не подходит по указанным данным" in r.text
    assert 'class="tg-preview"' in r.text and "Что стоит проверить первым" in r.text


def test_preview_unknown_answers_and_missing_facts(ctx):
    r = preview(ctx, **{"f-p0-income_sources": None, "f-p0-income_sources-unk": "on", "f-case-has_housing": None})
    assert "Нужно уточнить" in r.text and "Не хватает данных" in r.text and "Жильё" in r.text


def test_preview_with_spouse_and_future_date(ctx):
    r = preview(ctx, with_spouse="on", **{"f-p1-citizenship_group": "third", "as_of": "2027-06-01"})
    assert r.status_code == 200 and "Супруг(а)" in r.text and "01.06.2027" in r.text


def test_preview_validates_input_and_active_source(ctx):
    assert preview(ctx, **{"f-p0-pension_amount_eur": "много"}).status_code == 422
    assert preview(ctx, as_of="вчера").status_code == 422
    r = preview(ctx, source="active")
    assert r.status_code == 422 and "Действующей версии ещё нет" in r.text
    pub(ctx.conn)
    assert "Предварительно подходит" in preview(ctx, source="active").text


def test_stats_page(ctx):
    now = iso(utcnow())
    ctx.conn.execute("INSERT INTO events(at, kind) VALUES (?, 'result_shown')", (now,))
    ctx.conn.execute("INSERT INTO events(at, kind) VALUES (?, 'session_started')", (now,))
    ctx.conn.execute("INSERT INTO ai_calls(at, kind, model, ok, cost_usd) VALUES (?, 'explain', 'm', 1, 0.5)", (now,))
    html = ctx.client.get("/stats").text
    assert "$0.50" in html and "Расходы на AI" in html and "draft_replace" in html


XSS = "<script>alert(1)</script>"


def test_xss_title_is_escaped_everywhere(ctx):
    edit_draft(ctx.conn, "ground", "pensioner", title=XSS)
    for path in ("/grounds", "/grounds/pensioner", "/publish", "/"):
        html = ctx.client.get(path).text
        assert XSS not in html and "alert(1)" not in html.replace("&lt;script&gt;alert(1)&lt;/script&gt;", ""), path
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in ctx.client.get("/grounds").text
    r = preview(ctx)  # предпросмотр бота: названия экранирует render_result, таблицу — Jinja
    assert XSS not in r.text and "&lt;script&gt;" in r.text
    # создание основания с HTML в названии через форму
    data = harvest(ctx.client.get("/grounds/_new").text, "/grounds/_new")
    data.update({"id": ["xss_one"], "title": [XSS], "status": ["draft"]})
    assert ctx.client.post("/grounds/_new", data=data).status_code == 303
    assert XSS not in ctx.client.get("/grounds/xss_one").text


# --------------------------------------------------------------------------- одобрение базы и упрощённый интерфейс


def approve_form(ctx, **over):
    data = harvest(ctx.client.get("/").text, "/approve-all")
    for k, v in over.items():
        if v is None:
            data.pop(k, None)
        else:
            setv(data, k, v)
    return ctx.client.post("/approve-all", data=data)


@pytest.fixture
def seed_ctx(tmp_path):
    from bginfo.seed.loader import load_seed_bundle

    c = make_ctx(tmp_path, bundle=load_seed_bundle())
    assert do_login(c).status_code == 303
    yield c
    c.conn.close()


def test_dashboard_new_labels_and_tiles(seed_ctx):
    html = seed_ctx.client.get("/").text
    for text in ("Что сделать сейчас", "Бот пока не отвечает пользователям: база не опубликована",
                 "Одобрить всё и опубликовать", "Основания (13)", "Цифры и суммы (", "История версий",
                 "Опубликовать изменения", "Ещё ▾"):
        assert text in html, text
    assert "Показатели" not in html


def test_approve_all_requires_name_and_confirmation(seed_ctx):
    r = approve_form(seed_ctx, by="", confirm="on")
    assert r.status_code == 422 and "Укажите ваше имя" in r.text
    r = approve_form(seed_ctx, by="Иван", confirm=None)
    assert r.status_code == 422 and "Поставьте галочку" in r.text
    assert R.active_snapshot(seed_ctx.conn) is None


def test_approve_all_publishes_and_bot_sees_it(seed_ctx):
    r = approve_form(seed_ctx, by="Иван Петров", confirm="on")
    assert r.status_code == 200
    for text in ("База одобрена и опубликована", "Опубликовано:", "Требует проверки (бот предложит индивидуальный разбор)",
                 "Открыть бота"):
        assert text in r.text, text
    assert "{param:" not in r.text
    snap = R.active_snapshot(seed_ctx.conn)
    assert snap is not None and len(snap.bundle.grounds) == 13
    html = seed_ctx.client.get("/").text
    assert f"опубликована версия №{snap.release_id}" in html and "Одобрить всё и опубликовать" not in html


def test_approve_all_stale_draft_is_rejected(seed_ctx):
    data = harvest(seed_ctx.client.get("/").text, "/approve-all")
    gid = seed_ctx.conn.execute("SELECT item_id FROM kb_draft_items WHERE kind='ground' LIMIT 1").fetchone()[0]
    edit_draft(seed_ctx.conn, "ground", gid, summary="правка")
    setv(data, "by", "Иван")
    setv(data, "confirm", "on")
    r = seed_ctx.client.post("/approve-all", data=data)
    assert r.status_code == 409 and R.active_snapshot(seed_ctx.conn) is None


def test_new_post_routes_require_csrf(ctx):
    for path in ("/approve-all", "/grounds/pensioner/approve-publish", "/publish-now", "/grounds/pensioner/mark-review", "/params/min_wage/quick"):
        assert ctx.client.post(path, data={"by": "x"}).status_code == 403, path
        assert ctx.client.post(path, data={"by": "x", "csrf_token": "bad"}).status_code == 403, path


def test_new_pages_require_login(anon):
    for path in ("/grounds/pensioner", "/grounds/pensioner/edit", "/params"):
        assert anon.client.get(path).status_code == 303, path
    assert anon.client.post("/approve-all", data={}).status_code == 403


def test_ground_list_card_and_edit_pages(ctx):
    lst = ctx.client.get("/grounds").text
    for text in ("Статус", "Что требует внимания", "Опубликовано"):
        assert text in lst, text
    card = ctx.client.get("/grounds/pensioner")
    assert card.status_code == 200
    for text in ("Одобрить и опубликовать", "Пометить: требует проверки", "Редактировать", "подтверждено"):
        assert text in card.text, text
    edit = ctx.client.get("/grounds/pensioner/edit").text
    assert "Для эксперта" in edit and 'name="req-0-label"' in edit


def test_approve_publish_button_publishes_card(ctx):
    edit_draft(ctx.conn, "ground", "pensioner", status="draft")
    html = ctx.client.get("/grounds/pensioner").text
    assert "{param:" not in html
    data = harvest(html, "/grounds/pensioner/approve-publish")
    setv(data, "by", "")
    r = ctx.client.post("/grounds/pensioner/approve-publish", data=data)  # без имени
    assert r.status_code == 422 and "Укажите ваше имя" in r.text
    assert R.active_snapshot(ctx.conn) is None
    setv(data, "by", "Анна")
    r = ctx.client.post("/grounds/pensioner/approve-publish", data=data)
    assert r.status_code == 303
    g = R.draft_item(ctx.conn, "ground", "pensioner")[0]
    assert g["checked_by"] == "Анна" and g["status"] == "published"
    snap = R.active_snapshot(ctx.conn)
    assert snap is not None and snap.bundle.ground("pensioner").status == "published"
    page = ctx.client.get("/grounds/pensioner").text
    assert f"Опубликовано. Бот уже использует версию №{snap.release_id}." in page
    assert 'value="Анна"' in page  # имя запомнено в сессии


def test_approve_publish_without_csrf_is_403(ctx):
    assert ctx.client.post("/grounds/pensioner/approve-publish", data={"by": "x", "draft_revision": "1"}).status_code == 403
    assert R.active_snapshot(ctx.conn) is None


def test_approve_publish_stale_revision(ctx):
    data = harvest(ctx.client.get("/grounds/pensioner").text, "/grounds/pensioner/approve-publish")
    setv(data, "by", "Анна")
    edit_draft(ctx.conn, "ground", "nomad", title="Другое")
    r = ctx.client.post("/grounds/pensioner/approve-publish", data=data)
    assert r.status_code == 409 and "Пока вы смотрели, базу изменили" in r.text
    assert R.active_snapshot(ctx.conn) is None


def test_card_lists_what_owner_confirms(ctx):
    item, rev = R.draft_item(ctx.conn, "ground", "nomad")
    item["requirements"][0]["disputed"] = True
    item["requirements"][0]["verified"] = False
    R.save_draft_item(ctx.conn, "ground", item, "other", rev, "nomad")
    html = ctx.client.get("/grounds/nomad").text
    assert "Вы подтверждаете, что эти условия и цифры верны:" in html
    assert "{param:" not in html


def test_pending_strip_appears_after_edit_and_disappears_after_publish_now(ctx):
    pub(ctx.conn)
    assert "Опубликовать сейчас" not in ctx.client.get("/grounds").text
    edit_draft(ctx.conn, "ground", "pensioner", title="Другое")
    html = ctx.client.get("/grounds").text
    assert "Есть изменения, которые бот ещё не видит: 1" in html and "Опубликовать сейчас" in html
    data = harvest(html, "/publish-now")
    assert ctx.client.post("/publish-now", data={"by": "Анна", "draft_revision": data["draft_revision"][0]}).status_code == 403
    setv(data, "by", "")
    assert ctx.client.post("/publish-now", data=data).status_code == 422  # без имени
    setv(data, "by", "Анна")
    assert ctx.client.post("/publish-now", data=data).status_code == 303
    assert R.active_snapshot(ctx.conn).bundle.ground("pensioner").title == "Другое"
    assert "Опубликовать сейчас" not in ctx.client.get("/grounds").text


def test_mark_review_in_draft(ctx):
    data = harvest(ctx.client.get("/grounds/pensioner").text, "/grounds/pensioner/mark-review")
    setv(data, "reason", "Закон изменился")
    assert ctx.client.post("/grounds/pensioner/mark-review", data=data).status_code == 303
    g = R.draft_item(ctx.conn, "ground", "pensioner")[0]
    assert g["status"] == "needs_review" and g["review_reason"] == "Закон изменился"


def test_params_page_and_quick_edit(ctx):
    html = ctx.client.get("/params").text
    assert "Цифры и суммы" in html and "Эти цифры используются в" in html and "МРЗ" in html
    data = harvest(html, "/params/min_wage/quick")
    setv(data, "value", "700,5")
    setv(data, "valid_from", "2027-01-01")
    assert ctx.client.post("/params/min_wage/quick", data=data).status_code == 303
    vals = R.draft_item(ctx.conn, "parameter", "min_wage")[0]["values"]
    assert vals[-1]["value"] == 700.5 and vals[-1]["valid_to"] is None
    assert vals[-2]["valid_to"] == "2026-12-31"


# --------------------------------------------------------------------------- доступ к админке


def test_access_page_requires_login(anon):
    assert anon.client.get("/access").status_code == 303
    assert anon.client.post("/access", data={"host": "0.0.0.0", "port": "8091"}).status_code == 403


def test_access_save_requires_csrf_and_writes_ini(ctx, tmp_path, monkeypatch):
    ini = tmp_path / "config.ini"
    ini.write_text("# c\n[admin]\n# h\nhost = 127.0.0.1\nport = 8080\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_INI", str(ini))
    assert ctx.client.post("/access", data={"host": "0.0.0.0", "port": "8091"}).status_code == 403
    r = ctx.client.get("/access")
    assert r.status_code == 200 and "Доступ к админке" in r.text
    data = harvest(r.text, "/access")
    setv(data, "host", "0.0.0.0")
    setv(data, "port", "8091")
    assert ctx.client.post("/access", data=data).status_code == 303
    assert ini.read_text(encoding="utf-8") == "# c\n[admin]\n# h\nhost = 0.0.0.0\nport = 8091\ntrust_proxy = false\n"
    page = ctx.client.get("/access").text
    assert "Применится после перезапуска" in page
    assert "Прямой доступ: заголовки прокси не учитываются" in page
    row = ctx.conn.execute("SELECT action, detail FROM audit_log WHERE action='admin_access_saved'").fetchone()
    assert row and "port=8091" in row[1]
    assert "Прямой доступ: заголовки прокси не учитываются" in ctx.client.get("/access").text


@pytest.mark.parametrize("host,port", [("1.2.3.4", "8091"), ("0.0.0.0", "80"), ("0.0.0.0", "abc")])
def test_access_save_rejects_invalid(ctx, tmp_path, monkeypatch, host, port):
    ini = tmp_path / "config.ini"
    ini.write_text("[admin]\nhost = 127.0.0.1\nport = 8080\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_INI", str(ini))
    data = harvest(ctx.client.get("/access").text, "/access")
    setv(data, "host", host)
    setv(data, "port", port)
    assert ctx.client.post("/access", data=data).status_code == 422
    assert "port = 8080" in ini.read_text(encoding="utf-8")


def test_access_restart_exits_process_mocked(ctx, tmp_path, monkeypatch):
    import threading

    from bginfo.admin import routes_access

    ini = tmp_path / "config.ini"
    ini.write_text("[admin]\nhost = 0.0.0.0\nport = 8091\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_INI", str(ini))
    assert ctx.client.post("/access/restart", data={}).status_code == 403
    called = threading.Event()
    codes: list[int] = []
    monkeypatch.setattr(routes_access.os, "_exit", lambda c: (codes.append(c), called.set()))
    data = harvest(ctx.client.get("/access").text, "/access/restart")
    r = ctx.client.post("/access/restart", data=data)
    assert r.status_code == 200 and ":8091" in r.text and "10 секунд" in r.text
    assert called.wait(5) and codes == [0]
    assert ctx.conn.execute("SELECT 1 FROM audit_log WHERE action='admin_restart'").fetchone()
