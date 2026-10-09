import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from bginfo.db.conn import open_db
from bginfo.kb import releases as R
from bginfo.kb.validate import errors, validate
from tests.kbfixture import mini_bundle

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "t.db")
    R._cache.clear()
    yield c
    c.close()


def seed(conn, status="published"):
    R.replace_draft(conn, mini_bundle(status), "admin", "test")


def test_empty_db_has_no_snapshot(conn):
    assert R.active_snapshot(conn, T0) is None


def test_publish_and_activate(conn):
    seed(conn)
    rid = R.publish_draft(conn, "admin", now=T0)
    snap = R.active_snapshot(conn, T0 + timedelta(seconds=1))
    assert snap.release_id == rid
    assert snap.bundle.ground("pensioner").status == "published"


def test_published_content_is_immutable(conn):
    seed(conn)
    rid = R.publish_draft(conn, "admin", now=T0)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE kb_releases SET content='{}' WHERE id=?", (rid,))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM kb_releases WHERE id=?", (rid,))


def test_draft_edit_does_not_affect_bot_until_publish(conn):
    seed(conn)
    rid = R.publish_draft(conn, "admin", now=T0)
    item, rev = R.draft_item(conn, "ground", "pensioner")
    item["title"] = "Новое название"
    R.save_draft_item(conn, "ground", item, "admin", rev)
    assert R.active_snapshot(conn, T0).bundle.ground("pensioner").title == "Пенсионер"
    rid2 = R.publish_draft(conn, "admin", now=T0 + timedelta(minutes=1))
    snap = R.active_snapshot(conn, T0 + timedelta(minutes=1))
    assert snap.release_id == rid2 != rid
    assert snap.bundle.ground("pensioner").title == "Новое название"


def test_future_release_activates_by_time_without_restart(conn):
    seed(conn)
    r1 = R.publish_draft(conn, "admin", now=T0)
    item, rev = R.draft_item(conn, "parameter", "min_wage")
    item["values"][-1]["value"] = 700
    R.save_draft_item(conn, "parameter", item, "admin", rev)
    eff = T0 + timedelta(days=2)
    r2 = R.publish_draft(conn, "admin", effective_from=eff, now=T0)
    assert R.active_snapshot(conn, eff - timedelta(seconds=1)).release_id == r1
    assert R.active_snapshot(conn, eff).release_id == r2
    assert R.active_snapshot(conn, eff).bundle.param("min_wage").values[-1].value == 700


def test_publish_before_scheduled_is_blocked(conn):
    seed(conn)
    R.publish_draft(conn, "admin", effective_from=T0 + timedelta(days=5), now=T0)
    with pytest.raises(R.PublishError):
        R.publish_draft(conn, "admin", now=T0)


def test_cancel_scheduled_only_future(conn):
    seed(conn)
    r1 = R.publish_draft(conn, "admin", now=T0)
    r2 = R.publish_draft(conn, "admin", effective_from=T0 + timedelta(days=1), now=T0)
    with pytest.raises(R.PublishError):
        R.cancel_scheduled(conn, r1, "admin", now=T0)
    R.cancel_scheduled(conn, r2, "admin", now=T0)
    assert R.active_snapshot(conn, T0 + timedelta(days=2)).release_id == r1


def test_backdated_publish_rejected(conn):
    seed(conn)
    with pytest.raises(R.PublishError):
        R.publish_draft(conn, "admin", effective_from=T0 - timedelta(days=1), now=T0)


def test_invalid_draft_is_not_published(conn):
    seed(conn)
    item, rev = R.draft_item(conn, "ground", "pensioner")
    item["requirements"][0]["verified"] = False
    R.save_draft_item(conn, "ground", item, "admin", rev)
    with pytest.raises(R.PublishError) as e:
        R.publish_draft(conn, "admin", now=T0)
    assert e.value.issues
    assert R.active_snapshot(conn, T0) is None


def test_concurrent_edit_detected(conn):
    seed(conn)
    item, rev = R.draft_item(conn, "ground", "pensioner")
    R.save_draft_item(conn, "ground", item, "a", rev)
    with pytest.raises(R.ConcurrentEdit):
        R.save_draft_item(conn, "ground", item, "b", rev)


def test_publish_requires_reviewed_draft_revision(conn):
    seed(conn)
    seen = R.draft_revision(conn)
    item, rev = R.draft_item(conn, "ground", "nomad")
    R.save_draft_item(conn, "ground", item, "other", rev)
    with pytest.raises(R.ConcurrentEdit):
        R.publish_draft(conn, "admin", expected_draft_revision=seen, now=T0)


def test_rollback_creates_new_release(conn):
    seed(conn)
    r1 = R.publish_draft(conn, "admin", now=T0)
    item, rev = R.draft_item(conn, "ground", "pensioner")
    item["title"] = "X"
    R.save_draft_item(conn, "ground", item, "admin", rev)
    R.publish_draft(conn, "admin", now=T0 + timedelta(minutes=1))
    r3 = R.rollback_to(conn, r1, "admin", now=T0 + timedelta(minutes=2))
    snap = R.active_snapshot(conn, T0 + timedelta(minutes=2))
    assert snap.release_id == r3 and snap.bundle.ground("pensioner").title == "Пенсионер"
    assert len(R.list_releases(conn)) == 3


def test_flag_needs_review(conn):
    seed(conn)
    R.publish_draft(conn, "admin", now=T0)
    R.flag_needs_review(conn, "pensioner", "Изменение закона", "admin", ["pension_amount"], now=T0 + timedelta(minutes=1))
    g = R.active_snapshot(conn, T0 + timedelta(minutes=1)).bundle.ground("pensioner")
    assert g.status == "needs_review" and g.requirements[1].disputed
    draft_g, _ = R.draft_item(conn, "ground", "pensioner")
    assert draft_g["status"] == "needs_review"


def test_drafts_only_db_means_no_snapshot(conn):
    seed(conn, status="draft")
    assert errors(validate(R.load_draft(conn))) == []
    R.publish_draft(conn, "admin", now=T0)
    assert R.active_snapshot(conn, T0).bundle.active_grounds() == []


def test_diff(conn):
    seed(conn)
    a = R.load_draft(conn)
    item, rev = R.draft_item(conn, "ground", "nomad")
    item["title"] = "DN"
    R.save_draft_item(conn, "ground", item, "admin", rev)
    d = R.diff_bundles(a, R.load_draft(conn))
    assert d == [{"kind": "ground", "id": "nomad", "change": "changed", "fields": ["title"],
                  "old": {"title": "Digital nomad"}, "new": {"title": "DN"}}]


def test_validation_detects_cycles_and_overlaps():
    data = mini_bundle().model_dump(mode="json")
    data["parameters"].append({"id": "pa", "label": "a", "unit": "EUR", "derived": {"of": "pb", "multiplier": 1}})
    data["parameters"].append({"id": "pb", "label": "b", "unit": "EUR", "derived": {"of": "pa", "multiplier": 1}})
    data["parameters"][0]["values"][0]["valid_to"] = "2026-03-01"
    data["facts"][2]["depends_on"] = ["annual_income_eur"]
    data["facts"][3]["depends_on"] = ["pension_amount_eur"]
    from bginfo.kb.model import Bundle
    msgs = " | ".join(i.message for i in errors(validate(Bundle.model_validate(data))))
    assert "цикл" in msgs and "пересекаются" in msgs


def test_cache_is_keyed_by_db_file_and_release_id(tmp_path):
    """Два файла БД с одинаковым id выпуска, но разным содержимым, не должны подменять друг друга в кэше."""
    R._cache.clear()
    a, b = open_db(tmp_path / "a.db"), open_db(tmp_path / "b.db")
    try:
        R.replace_draft(a, mini_bundle(), "admin", "a")
        R.replace_draft(b, mini_bundle(), "admin", "b")
        b_bundle = mini_bundle().model_dump(mode="json")
        b_bundle["grounds"][0]["title"] = "Другое название"
        R.replace_draft(b, type(mini_bundle()).model_validate(b_bundle), "admin", "b2")
        ra, rb = R.publish_draft(a, "admin", now=T0), R.publish_draft(b, "admin", now=T0)
        assert ra == rb == 1
        va = {g.title for g in R.load_release(a, ra).grounds}
        vb = {g.title for g in R.load_release(b, rb).grounds}
        assert "Другое название" in vb and "Другое название" not in va
    finally:
        a.close()
        b.close()
