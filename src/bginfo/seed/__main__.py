"""CLI начального наполнения: ``python -m bginfo.seed [--db PATH] seed-draft | build-demo | approve-and-publish``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from bginfo.config import get_settings
from bginfo.db.conn import open_db
from bginfo.kb import releases as R
from bginfo.kb.approve import approve_all_and_publish, render_summary
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


def cmd_approve_and_publish(db: str | None, by: str) -> int:
    path = db or get_settings().DB_PATH
    conn = open_db(path)
    try:
        try:
            summary = approve_all_and_publish(conn, by)
        except R.PublishError as e:
            print(f"Не выполнено: {e}", file=sys.stderr)
            for i in e.issues[:20]:
                print(f"  - {i.where}: {i.message}", file=sys.stderr)
            return 1
        snap = R.active_snapshot(conn)
        shown = render_summary(summary, snap.bundle, date.today()) if snap else summary  # noqa: DTZ011
    finally:
        conn.close()
    print(f"База одобрена и опубликована: версия №{shown.release_id}.")
    print(f"Опубликовано: {len(shown.published)}" + (" — " + "; ".join(shown.published) if shown.published else ""))
    print(f"Требует проверки (бот предложит индивидуальный разбор): {len(shown.needs_review)}"
          + (" — " + "; ".join(shown.needs_review) if shown.needs_review else ""))
    if shown.params_unconfirmed:
        print("Цифры без подтверждения (остались неодобренными): " + "; ".join(shown.params_unconfirmed))
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
    ap = sub.add_parser("approve-and-publish", help="одобрить весь черновик от своего имени и опубликовать версию базы")
    ap.add_argument("--db", default=argparse.SUPPRESS)
    ap.add_argument("--by", required=True, help="ваше имя: попадёт в карточки как «кем подтверждено»")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd == "approve-and-publish":
        return cmd_approve_and_publish(args.db, args.by)
    if args.cmd == "seed-draft":
        return cmd_seed_draft(args.db, args.force)
    return cmd_build_demo(args.db, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
