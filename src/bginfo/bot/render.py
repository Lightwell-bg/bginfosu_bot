"""Отображение для Telegram: разбиение длинного HTML и inline-клавиатуры."""

from __future__ import annotations

import logging
import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from bginfo.engine.dialog import Button

log = logging.getLogger(__name__)

# Атом: тег целиком, HTML-сущность целиком или один символ.
_ATOM = re.compile(r"<[^>]*>|&#?\w+;|[\s\S]")
_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)")


def _tag_info(atom: str) -> tuple[bool, str] | None:
    """(закрывающий?, имя) для атома-тега, иначе None."""
    if not atom.startswith("<"):
        return None
    m = _TAG.match(atom)
    if not m:
        return None
    return bool(m.group(1)), m.group(2).lower()


def _closer(open_tag: str) -> str:
    info = _tag_info(open_tag)
    return f"</{info[1]}>" if info else ""


def _push_pop(stack: list[str], atom: str) -> None:
    info = _tag_info(atom)
    if info is None or atom.endswith("/>"):
        return
    closing, name = info
    if not closing:
        stack.append(atom)
    elif stack and _tag_info(stack[-1])[1] == name:
        stack.pop()


def split_html(text: str, limit: int = 4000) -> list[str]:
    """Разделить HTML на куски ≤ limit символов, не разрывая теги и сущности.

    Режет по пустой строке, затем по переводу строки, затем по пробелу, иначе жёстко.
    Открытые на границе теги (<b>, <i>, <a href=...> и т.п.) закрываются в конце куска
    и заново открываются в начале следующего.
    """
    if len(text) <= limit:
        return [text]
    atoms = _ATOM.findall(text)
    n = len(atoms)
    chunks: list[str] = []
    reopen: list[str] = []
    i = 0
    while i < n:
        stack = list(reopen)
        used = sum(len(t) for t in stack)
        sum(len(_closer(t)) for t in stack)
        cands: dict[int, tuple[int, list[str]]] = {}
        j = i
        cut: int | None = None
        cut_stack: list[str] = stack
        while j < n:
            a = atoms[j]
            if j > i:
                if a == "\n":
                    cands[2 if j + 1 < n and atoms[j + 1] == "\n" else 1] = (j, list(stack))
                elif a == " ":
                    cands[0] = (j, list(stack))
            trial = list(stack)
            _push_pop(trial, a)
            new_close = sum(len(_closer(t)) for t in trial)
            if j > i and used + len(a) + new_close > limit:
                break
            stack, _close_len = trial, new_close
            used += len(a)
            j += 1
        if j >= n:
            cut, cut_stack = n, []
            body = "".join(reopen) + "".join(atoms[i:n])
            chunks.append(body)
            break
        for prio in (2, 1, 0):
            if prio in cands:
                cut, cut_stack = cands[prio]
                break
        if cut is None:
            cut, cut_stack = j, stack
        body = "".join(reopen) + "".join(atoms[i:cut]) + "".join(_closer(t) for t in reversed(cut_stack))
        chunks.append(body)
        reopen = cut_stack
        i = cut
        while i < n and atoms[i] in ("\n", " "):
            i += 1
        if i >= n:
            break
    return chunks


def _cut_bytes(s: str, max_bytes: int) -> str:
    return s.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def callback_data(session_id: int, revision: int, action: str, arg: str = "") -> str:
    """``session_id:revision:action:arg`` длиной ≤ 64 байт (при необходимости arg обрезается)."""
    head = f"{session_id}:{revision}:{action}:"
    room = 64 - len(head.encode("utf-8"))
    assert room >= 0, "callback_data: служебная часть длиннее 64 байт"
    if len(arg.encode("utf-8")) > room:
        log.warning("callback arg truncated for action %s", action)
        arg = _cut_bytes(arg, room)
    return head + arg


def _button(b: Button, session_id: int, revision: int, webapp_url: str | None) -> InlineKeyboardButton | None:
    if b.action == "webapp":
        # кнопка открытия мини-аппа: без callback; без адреса мини-аппа её нет
        if not webapp_url:
            return None
        return InlineKeyboardButton(text=b.text[:64], web_app=WebAppInfo(url=webapp_url))
    return InlineKeyboardButton(text=b.text[:64], callback_data=callback_data(session_id, revision, b.action, b.arg))


def keyboard(buttons: list[list[Button]], session_id: int, revision: int,
             webapp_url: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    for row in buttons:
        made = [x for x in (_button(b, session_id, revision, webapp_url) for b in row) if x is not None]
        if made:
            rows.append(made)
    return InlineKeyboardMarkup(inline_keyboard=rows)
