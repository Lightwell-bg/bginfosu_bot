"""Общие маршруты «список / создать / изменить / удалить» для элементов черновика."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from bginfo.admin.auth import PostCtx, current_user, post_ctx
from bginfo.admin.common import flash, get_conn, redirect, render
from bginfo.admin.forms import ru_errors
from bginfo.kb import model as m
from bginfo.kb.releases import (
    ConcurrentEdit,
    PublishError,
    delete_draft_item,
    draft_item,
    load_draft,
    save_draft_item,
)

CONFLICT_MSG = ("Изменено кем-то ещё после того, как вы открыли форму. Ваши данные НЕ сохранены и показаны ниже; "
                "откройте актуальную версию и внесите правку заново.")


@dataclass
class Spec:
    kind: str                       # ground | parameter | fact | question | source
    base: str                       # /grounds
    one: str                        # «основание»
    many: str                       # «Основания»
    template: str
    parse: Callable[..., tuple[dict, dict[str, str], list[str]]]
    new_item: Callable[[], dict]
    list_ctx: Callable[[sqlite3.Connection, m.Bundle], dict]
    form_ctx: Callable[[m.Bundle, dict, bool], dict]
    id_field: str = "id"
    list_template: str = "kb_list.html"
    hint: str = ""                  # строка «что здесь делать» под заголовком списка
    # Если задан card_ctx: GET base/{id} показывает карточку (сводку), а форма правки живёт на base/{id}/edit
    card_template: str | None = None
    card_ctx: Callable[[sqlite3.Connection, m.Bundle, dict, int], dict] | None = None


def kb_ctx(draft: m.Bundle) -> dict[str, Any]:
    """Каталог фактов, показателей и источников для выпадающих списков."""
    facts = {f.key: f for f in draft.facts}
    refs, catalog = [], {}
    for f in draft.facts:
        roles = ["case"] if f.scope == "case" else ["self", "sponsor"]
        for r in roles:
            ref = f"{r}.{f.key}"
            refs.append((ref, f"{f.label} [{ref}]"))
            catalog[ref] = {"type": f.type, "label": f.label, "options": [{"value": o.value, "label": o.label} for o in f.options]}
    return {"facts_by_key": facts, "fact_refs": refs, "fact_catalog": catalog, "params": draft.parameters,
            "sources": draft.sources, "fact_list": draft.facts}


def register_crud(router: APIRouter, spec: Spec) -> None:
    base = spec.base

    def page(request: Request, conn: sqlite3.Connection, item: dict, revision: Any, errors: list[str],
             raw: dict, is_new: bool, status: int = 200, conflict: bool = False):
        draft = load_draft(conn)
        ctx = kb_ctx(draft)
        ctx.update(spec.form_ctx(draft, item, is_new))
        return render(request, spec.template, status_code=status, item=item, revision=revision, errors=errors,
                      json_raw=raw, is_new=is_new, spec=spec, conflict=conflict, **ctx)

    def save(conn, item, user, expected, old_id) -> tuple[list[str], bool]:
        try:
            save_draft_item(conn, spec.kind, item, user, expected, old_id)
            return [], False
        except ValidationError as e:
            return ru_errors(e, str(item.get("title") or item.get("label") or item.get("text") or "")), False
        except ConcurrentEdit as e:
            return ([CONFLICT_MSG], True) if expected is not None else ([f"{spec.one.capitalize()}: {e}"], False)
        except PublishError as e:
            return [str(e)], False

    def list_page(request: Request, user: str = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        return render(request, spec.list_template, spec=spec, **spec.list_ctx(conn, load_draft(conn)))

    def new_page(request: Request, user: str = Depends(current_user), conn: sqlite3.Connection = Depends(get_conn)):
        return page(request, conn, spec.new_item(), "", [], {}, True)

    def create(request: Request, p: PostCtx = Depends(post_ctx), conn: sqlite3.Connection = Depends(get_conn)):
        facts = {f.key: f for f in load_draft(conn).facts}
        item, raw, errs = spec.parse(p.form, None, facts, None)
        if errs:
            return page(request, conn, item, "", errs, raw, True, 422)
        errors, _ = save(conn, item, p.user, None, None)
        if errors:
            return page(request, conn, item, "", errors, raw, True, 422)
        flash(request, f"Создано: {item[spec.id_field]}")
        return redirect(f"{base}/{item[spec.id_field]}")

    def edit_page(request: Request, item_id: str, user: str = Depends(current_user),
                  conn: sqlite3.Connection = Depends(get_conn)):
        cur = draft_item(conn, spec.kind, item_id)
        if cur is None:
            raise HTTPException(404, f"Не найдено: {item_id}")
        return page(request, conn, cur[0], cur[1], [], {}, False)

    def card_page(request: Request, item_id: str, user: str = Depends(current_user),
                  conn: sqlite3.Connection = Depends(get_conn)):
        cur = draft_item(conn, spec.kind, item_id)
        if cur is None:
            raise HTTPException(404, f"Не найдено: {item_id}")
        assert spec.card_ctx is not None
        return render(request, spec.card_template, spec=spec, item=cur[0], revision=cur[1],
                      **spec.card_ctx(conn, load_draft(conn), cur[0], cur[1]))

    def edit_save(request: Request, item_id: str, p: PostCtx = Depends(post_ctx),
                  conn: sqlite3.Connection = Depends(get_conn)):
        cur = draft_item(conn, spec.kind, item_id)
        facts = {f.key: f for f in load_draft(conn).facts}
        item, raw, errs = spec.parse(p.form, cur[0] if cur else None, facts, item_id)
        raw_rev = str(p.form.get("revision", "")).strip()
        if not raw_rev.isdigit():
            errs = [*errs, "Не передана версия формы: обновите страницу."]
        if errs:
            return page(request, conn, item, raw_rev, errs, raw, False, 422)
        errors, conflict = save(conn, item, p.user, int(raw_rev), item_id)
        if errors:
            return page(request, conn, item, raw_rev, errors, raw, False, 409 if conflict else 422, conflict)
        flash(request, "Сохранено в черновике. Бот увидит изменения только после публикации.")
        return redirect(f"{base}/{item_id}")

    def delete(request: Request, item_id: str, p: PostCtx = Depends(post_ctx),
               conn: sqlite3.Connection = Depends(get_conn)):
        raw_rev = str(p.form.get("revision", "")).strip()
        try:
            delete_draft_item(conn, spec.kind, item_id, p.user, int(raw_rev))
        except (ConcurrentEdit, ValueError):
            flash(request, "Не удалось удалить: элемент изменён кем-то ещё или уже удалён.", "error")
            return redirect(f"{base}/{item_id}")
        flash(request, f"Удалено из черновика: {item_id}")
        return redirect(base)

    router.add_api_route(base, list_page, methods=["GET"])
    router.add_api_route(f"{base}/_new", new_page, methods=["GET"])
    router.add_api_route(f"{base}/_new", create, methods=["POST"])
    if spec.card_ctx is not None:
        router.add_api_route(f"{base}/{{item_id}}", card_page, methods=["GET"])
        router.add_api_route(f"{base}/{{item_id}}/edit", edit_page, methods=["GET"])
    else:
        router.add_api_route(f"{base}/{{item_id}}", edit_page, methods=["GET"])
    router.add_api_route(f"{base}/{{item_id}}", edit_save, methods=["POST"])
    router.add_api_route(f"{base}/{{item_id}}/delete", delete, methods=["POST"])
