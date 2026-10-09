"""Одобрение черновика: целиком (approve_all_and_publish) и по одной карточке (approve_ground_draft)."""

from __future__ import annotations

from datetime import date

import pytest

from bginfo.db.conn import open_db
from bginfo.kb import releases as R
from bginfo.kb.approve import ApproveSummary, approve_all_and_publish, approve_ground_draft, render_summary
from bginfo.seed.__main__ import main as seed_main
from bginfo.seed.loader import load_seed_bundle


@pytest.fixture
def conn(tmp_path):
    R._cache.clear()
    c = open_db(tmp_path / "a.db")
    R.replace_draft(c, load_seed_bundle(), "seed", "test")
    yield c
    c.close()


def _has_unconfirmed(g) -> bool:
    return any(r.research_status == "unconfirmed" for r in g.requirements)


def test_approve_all_publishes_seed(conn):
    summary = approve_all_and_publish(conn, "Иван Петров")
    snap = R.active_snapshot(conn)
    assert snap is not None and snap.release_id == summary.release_id
    active = [g for g in snap.bundle.grounds if g.status in ("published", "needs_review")]
    assert len(active) == 13
    assert len(summary.published) + len(summary.needs_review) == 13
    for g in snap.bundle.grounds:
        for r in g.requirements:
            if r.research_status == "unconfirmed":
                assert r.disputed and not r.verified
            else:
                assert r.verified and r.verified_by == "Иван Петров"
        assert g.checked_by == "Иван Петров"


def test_approve_all_requires_name(conn):
    with pytest.raises(R.PublishError):
        approve_all_and_publish(conn, "  ")
    assert R.active_snapshot(conn) is None


def test_render_summary_substitutes_params(conn):
    approve_all_and_publish(conn, "Иван")
    snap = R.active_snapshot(conn)
    pid = snap.bundle.parameters[0].id
    shown = render_summary(ApproveSummary(1, ["{param:" + pid + "} тест"], [], []), snap.bundle, date(2026, 10, 9))
    assert "{param:" not in shown.published[0]


def test_approve_single_ground_changes_only_draft(conn):
    grounds = load_seed_bundle().grounds
    gid = next(g.id for g in grounds if not _has_unconfirmed(g))
    res = approve_ground_draft(conn, gid, "Анна")
    assert res.published and not res.disputed
    data, _ = R.draft_item(conn, "ground", gid)
    assert data["status"] == "published" and data["checked_by"] == "Анна"
    assert all(r["verified"] for r in data["requirements"])
    assert R.active_snapshot(conn) is None  # публикации не было
    other = next(g.id for g in grounds if g.id != gid)
    assert R.draft_item(conn, "ground", other)[0]["status"] == "draft"


def test_approve_single_ground_with_unconfirmed_goes_to_review(conn):
    gid = next(g.id for g in load_seed_bundle().grounds if _has_unconfirmed(g))
    res = approve_ground_draft(conn, gid, "Анна")
    assert res.disputed
    assert R.draft_item(conn, "ground", gid)[0]["status"] == "needs_review"


def test_approve_ground_stale_revision_and_missing(conn):
    gid = load_seed_bundle().grounds[0].id
    _, rev = R.draft_item(conn, "ground", gid)
    with pytest.raises(R.ConcurrentEdit):
        approve_ground_draft(conn, gid, "Анна", expected_revision=rev - 1)
    with pytest.raises(R.PublishError):
        approve_ground_draft(conn, "no_such", "Анна")
    with pytest.raises(R.PublishError):
        approve_ground_draft(conn, gid, " ")


def test_cli_approve_and_publish(tmp_path, capsys):
    R._cache.clear()
    db = tmp_path / "cli.db"
    c = open_db(db)
    R.replace_draft(c, load_seed_bundle(), "seed", "test")
    c.close()
    assert seed_main(["approve-and-publish", "--db", str(db), "--by", "Иван"]) == 0
    out = capsys.readouterr().out
    assert "версия №" in out and "Опубликовано:" in out
    assert "Требует проверки (бот предложит индивидуальный разбор)" in out
    assert "{param:" not in out


def _draft_state(conn):
    rows = conn.execute("SELECT kind, item_id, data, revision FROM kb_draft_items ORDER BY kind, item_id").fetchall()
    return [tuple(r) for r in rows], R.draft_revision(conn)


def test_approve_all_rolls_back_approvals_when_publish_fails(conn):
    from datetime import UTC, datetime, timedelta
    t0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    approve_all_and_publish(conn, "Первый", now=t0)
    R.publish_draft(conn, "Первый", effective_from=t0 + timedelta(days=1), now=t0)  # запланированный выпуск
    before = _draft_state(conn)
    releases = len(R.list_releases(conn))
    with pytest.raises(R.PublishError):
        approve_all_and_publish(conn, "Второй", now=t0)
    assert _draft_state(conn) == before
    assert len(R.list_releases(conn)) == releases


def test_approve_all_stale_draft_revision_changes_nothing(conn):
    seen = R.draft_revision(conn)
    gid = load_seed_bundle().grounds[0].id
    item, rev = R.draft_item(conn, "ground", gid)
    R.save_draft_item(conn, "ground", item, "other", rev)
    before = _draft_state(conn)
    with pytest.raises(R.ConcurrentEdit):
        approve_all_and_publish(conn, "Иван", expected_draft_revision=seen)
    assert _draft_state(conn) == before
    assert R.list_releases(conn) == []


def test_manually_disputed_requirement_is_not_verified(conn):
    gid = next(g.id for g in load_seed_bundle().grounds if not _has_unconfirmed(g))
    item, rev = R.draft_item(conn, "ground", gid)
    item["requirements"][0]["disputed"] = True
    item["requirements"][0]["dispute_reason"] = "ручная пометка"
    item["requirements"][0]["research_status"] = "consolidated"
    R.save_draft_item(conn, "ground", item, "admin", rev)
    res = approve_ground_draft(conn, gid, "Анна")
    assert res.disputed
    data, _ = R.draft_item(conn, "ground", gid)
    assert data["status"] == "needs_review" and data["review_reason"]
    assert data["requirements"][0]["verified"] is False
    summary = approve_all_and_publish(conn, "Анна")
    assert any(data["title"] in s for s in summary.needs_review)
    assert data["title"] not in summary.published
