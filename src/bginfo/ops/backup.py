"""Бэкап и восстановление SQLite: ``python -m bginfo.ops.backup backup|restore|verify``."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("bginfo.backup")

REQUIRED_TABLES = ("schema_migrations", "kb_releases")


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _copy(src: Path, dst: Path) -> None:
    """Онлайн-копия через sqlite3 backup API (безопасно для работающей WAL-базы)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(str(src), timeout=30)
    d = sqlite3.connect(str(dst), timeout=30)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def verify(path: Path) -> list[str]:
    """Вернуть список проблем (пусто = файл годен)."""
    if not path.is_file():
        return [f"файл не найден: {path}"]
    problems: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            res = conn.execute("PRAGMA integrity_check").fetchall()
            if [r[0] for r in res] != ["ok"]:
                problems.append("integrity_check: " + "; ".join(str(r[0]) for r in res[:5]))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for t in REQUIRED_TABLES:
                if t not in tables:
                    problems.append(f"нет таблицы {t}")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        problems.append(f"sqlite: {exc}")
    return problems


def backup(db: Path, out_dir: Path, retention_days: int) -> Path:
    if not db.is_file():
        raise SystemExit(f"база не найдена: {db}")
    target = out_dir / f"app-{_stamp()}.db"
    _copy(db, target)
    problems = verify(target)
    if problems:
        target.unlink(missing_ok=True)
        raise SystemExit("бэкап не прошёл проверку: " + "; ".join(problems))
    rotate(out_dir, retention_days, keep=target)
    return target


def rotate(out_dir: Path, retention_days: int, keep: Path | None = None) -> None:
    """Удалить копии старше срока. Под маску ``app-*.db`` попадают и страховочные ``app-prerestore-*.db``."""
    cutoff = time.time() - retention_days * 86400
    for old in out_dir.glob("app-*.db"):
        if old != keep and old.stat().st_mtime < cutoff:
            old.unlink()
            log.info("removed old backup %s", old.name)


def restore(src: Path, db: Path, backup_dir: Path | None = None) -> Path | None:
    """Восстановить ``db`` из ``src``. Возвращает путь страховочной копии прежней базы (или None).

    Порядок: проверка src -> копия во временный файл рядом с целью и его проверка (рабочая база пока не
    тронута) -> страховочная копия ``app-prerestore-<время>.db`` в каталоге бэкапов (ротируется вместе с
    ними) -> удаление -wal/-shm цели -> атомарный ``os.replace``.
    """
    if src.resolve() == db.resolve() or (src.exists() and db.exists() and os.path.samefile(src, db)):
        raise SystemExit("источник и цель восстановления совпадают; рабочая база не тронута")
    problems = verify(src)
    if problems:
        raise SystemExit("файл бэкапа непригоден: " + "; ".join(problems))
    backup_dir = backup_dir or src.parent
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_name(db.name + ".restore-tmp")

    def _drop_tmp() -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            tmp.with_name(tmp.name + suffix).unlink(missing_ok=True)

    _drop_tmp()
    try:
        _copy(src, tmp)
        problems = verify(tmp)
        if problems:
            raise SystemExit("восстановленная копия не прошла проверку, рабочая база не тронута: " + "; ".join(problems))
        safety = None
        if db.is_file():
            safety = backup_dir / f"app-prerestore-{_stamp()}.db"
            safety.parent.mkdir(parents=True, exist_ok=True)
            try:
                _copy(db, safety)
            except sqlite3.DatabaseError:
                # текущая база повреждена: сохраняем файл как есть
                safety.unlink(missing_ok=True)
                shutil.copy2(db, safety)
        # хвосты WAL старой базы (боту и админке уже велено быть остановленными) удаляем до замены
        for suffix in ("-wal", "-shm"):
            db.with_name(db.name + suffix).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            tmp.with_name(tmp.name + suffix).unlink(missing_ok=True)
        os.replace(tmp, db)
    except BaseException:
        _drop_tmp()
        raise
    return safety


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(prog="bginfo.ops.backup")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--db")
    b.add_argument("--dir", default="data/backups")
    b.add_argument("--retention-days", type=int)
    v = sub.add_parser("verify")
    v.add_argument("file")
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--db")
    r.add_argument("--dir", help="каталог для страховочной копии прежней базы (по умолчанию каталог файла бэкапа)")
    r.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "verify":
        problems = verify(Path(args.file))
        print("OK" if not problems else "FAIL: " + "; ".join(problems))
        return 0 if not problems else 1

    from bginfo.config import get_config, get_settings

    db = Path(args.db or get_settings().DB_PATH)
    if args.cmd == "backup":
        days = args.retention_days if args.retention_days is not None else get_config().privacy.backup_retention_days
        print(f"Бэкап создан: {backup(db, Path(args.dir), days)}")
        return 0

    print("ВНИМАНИЕ: перед восстановлением остановите бота и админку.")
    if not args.yes:
        print("Добавьте --yes для подтверждения.", file=sys.stderr)
        return 2
    safety = restore(Path(args.file), db, Path(args.dir) if args.dir else None)
    print(f"База {db} восстановлена из {args.file}." + (f" Копия прежней: {safety}" if safety else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
