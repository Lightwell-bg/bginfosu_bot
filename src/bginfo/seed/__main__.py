"""CLI начального наполнения: ``python -m bginfo.seed [--db PATH] seed-draft | build-demo``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from bginfo.config import get_settings
from bginfo.db.conn import open_db
from bginfo.kb import releases as R
from bginfo.kb.validate import errors, validate
from bginfo.seed.loader import load_seed_bundle

log = logging.getLogger("bginfo.seed")

DEFAULT_DEMO_DB = "data/demo.db"


def _draft_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM kb_draft_items").fetchone()[0]


def _check(bundle) -> None:
    errs = errors(validate(bundle))
    if errs:
        for e in errs:
            log.error("%s: %s", e.where, e.message)
        raise SystemExit(f"Набор не прошёл проверку: ошибок {len(errs)}")


def cmd_seed_draft(db: str | None, force: bool) -> int:
    path = db or get_settings().DB_PATH
    bundle = load_seed_bundle()
    _check(bundle)
    conn = open_db(path)
    try:
        if _draft_count(conn) and not force:
            print(f"В черновике {path} уже есть элементы. Для замены запустите с --force.", file=sys.stderr)
            return 1
        R.replace_draft(conn, bundle, user="seed", reason="initial seed 2026-10-09")
    finally:
        conn.close()
    print(f"Черновик заполнен: {path}. Выпуск не опубликован — одобрение и публикация за владельцем.")
    return 0


def cmd_build_demo(db: str | None, force: bool) -> int:
    path = Path(db or DEFAULT_DEMO_DB)
    working = Path(get_settings().DB_PATH)
    if path.resolve() == working.resolve():
        print(f"Отказ: {path} совпадает с рабочей базой (DB_PATH).", file=sys.stderr)
        return 1
    if path.exists():
        if not force:
            print(f"Отказ: файл {path} уже существует. Для пересоздания запустите с --force.", file=sys.stderr)
            return 1
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    bundle = load_seed_bundle(demo=True)
    _check(bundle)
    conn = open_db(path)
    try:
        R.replace_draft(conn, bundle, user="demo-builder", reason="demo seed")
        rid = R.publish_draft(conn, user="demo-builder", comment="ДЕМО")
    finally:
        conn.close()
    print(f"Демо-база готова: {path} (выпуск №{rid}). Тестовые данные, не для рабочей базы.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bginfo.seed")
    parser.add_argument("--db", default=None, help="путь к SQLite (по умолчанию DB_PATH из настроек)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("seed-draft", "загрузить рабочее наполнение в черновик"),
                           ("build-demo", "собрать отдельную демо-базу с опубликованным выпуском")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--db", default=argparse.SUPPRESS)
        sp.add_argument("--force", action="store_true", help="заменить существующие данные")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd == "seed-draft":
        return cmd_seed_draft(args.db, args.force)
    return cmd_build_demo(args.db, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
