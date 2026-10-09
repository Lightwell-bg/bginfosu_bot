import sqlite3

import pytest

from bginfo.db.conn import open_db
from bginfo.ops import backup as bk


def test_backup_verify_restore(tmp_path):
    db = tmp_path / "app.db"
    conn = open_db(db)
    conn.execute("INSERT INTO events(at, kind) VALUES ('2026-01-01T00:00:00Z', 'marker')")
    conn.close()

    out = bk.backup(db, tmp_path / "bk", retention_days=14)
    assert out.is_file()
    assert bk.verify(out) == []

    db.write_bytes(b"garbage" * 100)
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"app.db{suffix}").unlink(missing_ok=True)
    assert bk.verify(db)

    safety = bk.restore(out, db)
    assert safety is not None and safety.is_file()
    c = sqlite3.connect(db)
    assert c.execute("SELECT kind FROM events").fetchone()[0] == "marker"
    c.close()


def test_restore_requires_yes(tmp_path):
    db = tmp_path / "app.db"
    open_db(db).close()
    out = bk.backup(db, tmp_path / "bk", 14)
    assert bk.main(["restore", str(out), "--db", str(db)]) == 2


def test_verify_rejects_foreign_db(tmp_path):
    f = tmp_path / "x.db"
    c = sqlite3.connect(f)
    c.execute("CREATE TABLE t(a)")
    c.commit()
    c.close()
    assert any("schema_migrations" in p for p in bk.verify(f))
    with pytest.raises(SystemExit):
        bk.restore(f, tmp_path / "t.db")


def _make_db(tmp_path, marker):
    db = tmp_path / "app.db"
    conn = open_db(db)
    conn.execute("INSERT INTO events(at, kind) VALUES ('2026-01-01T00:00:00Z', ?)", (marker,))
    conn.close()
    return db


def _marker(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT kind FROM events").fetchone()[0]
    finally:
        c.close()


def test_restore_rejects_same_source_and_target(tmp_path):
    db = _make_db(tmp_path, "live")
    before = db.read_bytes()
    with pytest.raises(SystemExit):
        bk.restore(db, db)
    with pytest.raises(SystemExit):
        bk.restore(tmp_path / "sub" / ".." / "app.db", db)
    assert db.read_bytes() == before


def test_restore_failed_temp_verify_keeps_live_db(tmp_path, monkeypatch):
    db = _make_db(tmp_path, "live")
    other = tmp_path / "other"
    other.mkdir()
    src_db = _make_db(other, "old")
    out = bk.backup(src_db, tmp_path / "bk", 14)
    real = bk.verify
    calls = []

    def flaky(path):
        calls.append(path)
        return real(path) if len(calls) == 1 else ["broken temp"]

    monkeypatch.setattr(bk, "verify", flaky)
    with pytest.raises(SystemExit):
        bk.restore(out, db)
    assert _marker(db) == "live"
    assert not list(tmp_path.glob("app.db.restore-tmp*"))
    assert not list((tmp_path / "bk").glob("app-prerestore-*"))


def test_restore_prerestore_copy_in_backup_dir_and_rotation(tmp_path):
    import os
    import time

    live = _make_db(tmp_path, "live")
    other = tmp_path / "other"
    other.mkdir()
    out = bk.backup(_make_db(other, "old"), tmp_path / "bk", 14)
    safety = bk.restore(out, live)
    assert safety.parent == out.parent and safety.name.startswith("app-prerestore-")
    assert _marker(live) == "old"
    assert _marker(safety) == "live"
    # страховочная копия ротируется по backup_retention_days вместе с обычными
    old = time.time() - 20 * 86400
    os.utime(safety, (old, old))
    bk.backup(live, tmp_path / "bk", 14)
    assert not safety.exists()
