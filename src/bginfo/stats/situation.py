"""Обезличенное описание ситуации для статистики: только категории, без сумм и текста."""

from __future__ import annotations

from bginfo.engine.facts import CASE, MAIN, MISSING, Profile


def _as_list(raw) -> list | None:
    """Подтверждённое значение как список строк без «none»; None — неизвестно."""
    if raw is MISSING or raw is None:
        return None
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    return sorted({str(x) for x in items if str(x) != "none"})


def situation_of(profile: Profile) -> dict:
    """Категории ситуации по подтверждённым значениям основного заявителя и анкеты."""
    who = profile.value(CASE, "who_moves")
    goal = profile.value(CASE, "goal")
    raw = profile.value(MAIN, "citizenship_group")
    if raw is MISSING or raw is None:
        cit = "unknown"
    else:
        cits = set(raw) if isinstance(raw, (list, tuple, set)) else {raw}
        cit = "bg" if "bg" in cits else "eu" if "eu" in cits else "third" if cits else "unknown"
    return {
        "who": str(who) if who is not MISSING and who else "unknown",
        "citizenship": cit,
        "income": _as_list(profile.value(MAIN, "income_sources")),
        "ties": _as_list(profile.value(MAIN, "ties")),
        "goal": str(goal) if goal is not MISSING and goal else "unknown",
        "family": bool(profile.members()),
    }


def _part(v: list | None) -> str:
    if v is None:
        return "?"
    return "+".join(sorted(v)) if v else "-"


def situation_key(s: dict) -> str:
    """Стабильный ключ ``who|citizenship|income|ties`` (цель и семья не входят)."""
    return "|".join([str(s.get("who") or "unknown"), str(s.get("citizenship") or "unknown"),
                     _part(s.get("income")), _part(s.get("ties"))])
