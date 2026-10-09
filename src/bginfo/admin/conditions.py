"""Конструктор условий: разбор формы в условие (dict) и подготовка данных для шаблона.

Простое условие — один «лист» (равно / входит в список / сравнение числа /
период / подтверждённый факт) либо and/or из таких листов. Всё остальное
(вложенные and/or, not, sponsor_basis) показывается читаемым текстом и
правится в «расширенном режиме» — JSON одного этого условия.

Имена полей формы для условия с префиксом P:
  P-mode (builder|json), P-combine (single|and|or), P-lidx (индексы листов, повтор),
  P-l<i>-type/-fact/-cmp/-value/-param/-mult, P-json (текст), P-use_json (флажок).
"""

from __future__ import annotations

import json
import re
from typing import Any

import jinja2
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import FormData

from bginfo.kb import model as m

LEAF_TYPES = {
    "eq": "равно",
    "in": "входит в список",
    "cmp": "сравнение с числом",
    "period": "период (дата не раньше / не позже)",
    "known_true": "подтверждённый факт",
}
CMP_OPS = [">=", ">", "<=", "<", "=="]
PERIOD_OPS = [">=", "<="]
_IDX = re.compile(r"[A-Za-z0-9]{1,12}$")
_COND = TypeAdapter(m.Condition)


def indices(form: FormData, name: str) -> list[str]:
    """Индексы повторяемых блоков из формы (только безопасные токены, без повторов)."""
    out: list[str] = []
    for v in form.getlist(name):
        if isinstance(v, str) and _IDX.match(v) and v not in out:
            out.append(v)
    return out


def _g(c: dict, k: str, default: Any = "") -> Any:
    v = c.get(k, default)
    return default if v is None else v


def _is_leaf(c: Any) -> bool:
    return isinstance(c, dict) and c.get("op") in LEAF_TYPES


def classify(c: Any) -> str:
    """none | builder | json — каким способом условие можно править."""
    if c is None:
        return "none"
    if _is_leaf(c):
        return "builder"
    if isinstance(c, dict) and c.get("op") in ("and", "or"):
        kids = c.get("all" if c["op"] == "and" else "any")
        if isinstance(kids, list) and kids and all(_is_leaf(k) for k in kids):
            return "builder"
    return "json"


def _num(v: Any) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def _scalar_text(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return _num(v) if isinstance(v, (int, float)) else str(v)


def _leaf_view(i: str, c: dict) -> dict:
    t = c.get("op", "eq")
    if t == "eq":
        value = _scalar_text(_g(c, "value"))
    elif t == "in":
        value = ", ".join(_scalar_text(x) for x in _g(c, "values", []))
    elif t == "cmp":
        value = _num(c.get("value"))
    elif t == "period":
        value = _num(c.get("months"))
    else:
        value = ""
    return {
        "i": i, "type": t, "fact": _g(c, "fact"), "cmp": _g(c, "cmp", ">="), "value": value,
        "param": _g(c, "param"), "mult": _num(c.get("multiplier", 1.0)) or "1",
    }


def describe(c: Any, facts: dict[str, m.FactDef]) -> str:
    """Читаемая запись условия по-русски."""
    try:
        return _describe(c, facts)
    except Exception:  # noqa: BLE001 — кривой dict из формы не должен ронять страницу
        return json.dumps(c, ensure_ascii=False)


def _fact_title(ref: str, facts: dict[str, m.FactDef]) -> str:
    key = ref.split(".", 1)[-1]
    fd = facts.get(key)
    return f"«{fd.label}» [{ref}]" if fd else f"[{ref}]"


def _describe(c: Any, facts: dict[str, m.FactDef]) -> str:
    op = c.get("op")
    if op == "eq":
        return f"{_fact_title(c['fact'], facts)} = {_scalar_text(c['value'])}"
    if op == "in":
        return f"{_fact_title(c['fact'], facts)} входит в: " + ", ".join(_scalar_text(x) for x in c["values"])
    if op == "cmp":
        rhs = (f"показатель {c['param']}" + (f" × {_num(c.get('multiplier', 1))}" if c.get("multiplier", 1) != 1 else "")
               if c.get("param") else f"{_num(c.get('value'))} × {_num(c.get('multiplier', 1))}")
        return f"{_fact_title(c['fact'], facts)} {c['cmp']} {rhs}"
    if op == "period":
        rhs = f"показатель {c['param']} мес." if c.get("param") else f"{c.get('months')} мес."
        return f"{_fact_title(c['fact'], facts)} {c['cmp']} (дата расчёта + {rhs})"
    if op == "known_true":
        return f"{_fact_title(c['fact'], facts)} — подтверждено «да»"
    if op == "sponsor_basis":
        return "у основного заявителя есть основание (статусы: " + (", ".join(c.get("statuses", [])) or "любые") + ")"
    if op == "and":
        return "ВСЕ из: (" + "; ".join(_describe(x, facts) for x in c["all"]) + ")"
    if op == "or":
        return "ХОТЯ БЫ ОДНО из: (" + "; ".join(_describe(x, facts) for x in c["any"]) + ")"
    if op == "not":
        return "НЕ (" + _describe(c["cond"], facts) + ")"
    return json.dumps(c, ensure_ascii=False)


@jinja2.pass_context
def cond_ctx(ctx: jinja2.runtime.Context, prefix: str, cond: Any, required: bool = False) -> dict:
    """Данные для макроса cond_builder. Контекст шаблона даёт каталог фактов и сырой JSON после ошибки."""
    facts = ctx.get("facts_by_key") or {}
    raw = (ctx.get("json_raw") or {}).get(prefix)
    kind = classify(cond)
    leaves: list[dict] = []
    combine = "single"
    if raw is None and kind == "builder":
        if cond["op"] in ("and", "or"):
            combine = cond["op"]
            kids = cond["all" if cond["op"] == "and" else "any"]
        else:
            kids = [cond]
        leaves = [_leaf_view(str(i), k) for i, k in enumerate(kids)]
    if not leaves and required and raw is None and kind != "json":
        leaves = [_leaf_view("0", {"op": "eq"})]
    json_mode = raw is not None or kind == "json"
    return {
        "prefix": prefix, "mode": "json" if json_mode else "builder", "combine": combine, "leaves": leaves,
        "json_text": raw if raw is not None else (json.dumps(cond, ensure_ascii=False, indent=2) if cond else ""),
        "readable": describe(cond, facts) if cond else "",
        "required": required,
    }


# --------------------------------------------------------------------------- parsing


def coerce_scalar(text: str, fd: m.FactDef | None) -> Any:
    t = text.strip()
    if fd is not None:
        if fd.type == "bool":
            if t.lower() in ("true", "1", "да", "yes"):
                return True
            if t.lower() in ("false", "0", "нет", "no"):
                return False
            raise ValueError("для да/нет-факта укажите true или false")
        if fd.type == "int":
            return int(t)
        if fd.type == "money":
            return float(t.replace(",", "."))
    return t


def _split_list(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n;]", text) if x.strip()]


def _parse_leaf(form: FormData, p: str, facts: dict[str, m.FactDef]) -> tuple[dict | None, str | None]:
    f = lambda k: str(form.get(f"{p}-{k}", "") or "").strip()
    typ, ref, value, param = f("type") or "eq", f("fact"), f("value"), f("param")
    if not ref and not value and not param:
        return None, None  # пустая строка конструктора пропускается
    if not re.match(m.FACT_REF_PATTERN, ref):
        return None, "укажите факт"
    fd = facts.get(ref.split(".", 1)[1])
    try:
        if typ == "eq":
            if not value:
                return None, "укажите значение"
            return {"op": "eq", "fact": ref, "value": coerce_scalar(value, fd)}, None
        if typ == "in":
            vals = [coerce_scalar(x, fd) for x in _split_list(value)]
            if not vals:
                return None, "укажите хотя бы одно значение через запятую"
            return {"op": "in", "fact": ref, "values": vals}, None
        if typ == "known_true":
            return {"op": "known_true", "fact": ref}, None
        if typ in ("cmp", "period"):
            cmpop = f("cmp") or ">="
            if typ == "cmp":
                mult = float((f("mult") or "1").replace(",", "."))
                leaf: dict = {"op": "cmp", "fact": ref, "cmp": cmpop, "multiplier": mult}
                if param:
                    leaf["param"] = param
                elif value:
                    leaf["value"] = float(value.replace(",", "."))
                else:
                    return None, "укажите число или показатель"
                return leaf, None
            leaf = {"op": "period", "fact": ref, "cmp": cmpop}
            if param:
                leaf["param"] = param
            elif value:
                leaf["months"] = int(value)
            else:
                return None, "укажите число месяцев или показатель"
            return leaf, None
    except ValueError:
        return None, "некорректное значение: ожидается число или допустимое значение"
    return None, "неизвестный тип условия"


def parse_cond(
    form: FormData, prefix: str, facts: dict[str, m.FactDef], required: bool, label: str,
) -> tuple[dict | None, str | None, list[str]]:
    """Условие из формы -> (dict | None, сырой JSON при ошибке, ошибки)."""
    from bginfo.admin.forms import ru_errors  # поздний импорт: forms импортирует этот модуль

    if form.get(f"{prefix}-mode") == "json" or form.get(f"{prefix}-use_json"):
        text = str(form.get(f"{prefix}-json", "") or "").strip()
        if not text:
            return None, text, ([f"{label}: условие не задано"] if required else [])
        try:
            data = json.loads(text)
        except ValueError as e:
            return None, text, [f"{label}: некорректный JSON ({e})"]
        try:
            obj = _COND.validate_python(data)
        except ValidationError as e:
            return None, text, [f"{label}: {x}" for x in ru_errors(e)]
        return _COND.dump_python(obj, mode="json"), None, []

    errs: list[str] = []
    leaves: list[dict] = []
    loose: list[dict] = []  # то же, но с «сырыми» значениями — чтобы показать форму после ошибки
    for n, i in enumerate(indices(form, f"{prefix}-lidx"), 1):
        leaf, err = _parse_leaf(form, f"{prefix}-l{i}", facts)
        if err:
            errs.append(f"{label}, условие №{n}: {err}")
            loose.append(_loose_leaf(form, f"{prefix}-l{i}"))
        elif leaf:
            leaves.append(leaf)
            loose.append(leaf)
    if errs:
        return _partial(loose), None, errs
    if not leaves:
        return None, None, ([f"{label}: условие не задано"] if required else [])
    combine = str(form.get(f"{prefix}-combine", "single"))
    if len(leaves) == 1:
        return leaves[0], None, []
    if combine == "or":
        return {"op": "or", "any": leaves}, None, []
    return {"op": "and", "all": leaves}, None, []


def _partial(leaves: list[dict]) -> dict | None:
    """Что удалось разобрать — для повторного показа формы после ошибки."""
    if not leaves:
        return None
    return leaves[0] if len(leaves) == 1 else {"op": "and", "all": leaves}


def _loose_leaf(form: FormData, p: str) -> dict:
    f = lambda k: str(form.get(f"{p}-{k}", "") or "").strip()
    typ = f("type") or "eq"
    leaf: dict = {"op": typ, "fact": f("fact"), "cmp": f("cmp") or ">=", "param": f("param"), "multiplier": f("mult") or "1"}
    if typ == "in":
        leaf["values"] = _split_list(f("value"))
    elif typ == "period":
        leaf["months"] = f("value")
    else:
        leaf["value"] = f("value")
    return leaf
