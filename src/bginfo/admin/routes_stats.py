"""Статистика: цифры по боту (обезличенные), типовые ситуации, журнал действий админки."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request

from bginfo.admin.auth import current_user
from bginfo.admin.common import get_conn, get_state, render
from bginfo.db.conn import utcnow
from bginfo.kb.releases import active_snapshot, load_draft
from bginfo.stats import queries as Q

router = APIRouter()

PERIODS = (("7", "7 дней"), ("30", "30 дней"), ("90", "90 дней"), ("all", "Всё время"))
DEFAULT_PERIOD = "30"
MAX_KEY_LEN = 500

KIND_RU = {
    "ok": "Есть вариант", "none": "Ничего не подошло", "unsupported": "Особый режим (гражданин Болгарии/ЕС)",
    "individual": "Нужен индивидуальный разбор", "clarify": "Не хватило данных",
}
# статус основания -> (подпись, css-класс цвета)
STATUS_RU = {
    "fit": ("подходит", "s-fit"), "possible_after": ("возможно после", "s-after"), "clarify": ("уточнить", "s-clarify"),
    "individual": ("индивидуально", "s-indiv"), "not_eligible": ("не подходит", "s-no"),
}
STATUS_ORDER = ("fit", "possible_after", "clarify", "individual", "not_eligible")
DIM_TITLES = {
    "who": "Для кого подбор", "citizenship": "Гражданство", "income": "Занятость и доходы",
    "ties": "Связи с Болгарией", "goal": "Цель", "family": "Есть семья в анкете",
}
# признак ситуации -> ключ факта в каталоге (для подписей вариантов)
DIM_FACT = {"who": "who_moves", "citizenship": "citizenship_group", "income": "income_sources", "ties": "ties",
            "goal": "goal"}
BUILTIN_LABELS = {"none": "ничего из перечисленного", "unknown": "не указано"}
CIT_FALLBACK = {"bg": "Гражданин Болгарии", "eu": "Гражданин ЕС", "third": "Другая страна"}
WHO_FALLBACK = {"self": "Для себя", "self_family": "Для себя и семьи", "other": "Для другого человека"}


def _period(raw: str | None) -> str:
    return raw if raw in {p for p, _ in PERIODS} else DEFAULT_PERIOD


def _since(period: str):
    return None if period == "all" else utcnow() - timedelta(days=int(period))


def wclass(share: float) -> str:
    """Класс ширины полоски: доля 0..1 -> w-0 … w-100 с шагом 5 (CSP не даёт inline style)."""
    pct = max(0.0, min(1.0, share)) * 100
    step = round(pct / 5) * 5
    if pct > 0 and step == 0:
        step = 5
    return f"w-{int(step)}"


def pct_text(share: float) -> str:
    return f"{share * 100:.0f}%"


class _Names:
    """Подписи из действующего выпуска, иначе из черновика, иначе сам идентификатор."""

    def __init__(self, conn: sqlite3.Connection):
        bundles = []
        with contextlib.suppress(Exception):  # подписи не должны ломать страницу
            bundles.append(load_draft(conn))
        with contextlib.suppress(Exception):
            snap = active_snapshot(conn)
            if snap is not None:
                bundles.append(snap.bundle)  # действующий — последним, он перекрывает черновик
        self.grounds: dict[str, str] = {}
        self.questions: dict[str, str] = {}
        self.options: dict[str, dict[str, str]] = {}
        for b in bundles:
            self.grounds.update({g.id: g.title for g in b.grounds})
            self.questions.update({q.id: q.text for q in b.questions})
            for f in b.facts:
                self.options[f.key] = {o.value: o.label for o in f.options}

    def ground(self, gid) -> str:
        return self.grounds.get(gid, str(gid))

    def question(self, qid: str) -> str:
        return self.questions.get(qid, qid)

    def value(self, dim: str, v) -> str:
        if isinstance(v, bool):
            return "да" if v else "нет"
        if v in BUILTIN_LABELS:
            return BUILTIN_LABELS[v]
        opts = self.options.get(DIM_FACT.get(dim, ""), {})
        fallback = CIT_FALLBACK if dim == "citizenship" else WHO_FALLBACK if dim == "who" else {}
        return opts.get(v) or fallback.get(v) or str(v)

    def situation_text(self, sit: dict) -> str:
        parts = [self.value("who", sit.get("who") or "unknown"), self.value("citizenship", sit.get("citizenship") or "unknown")]
        income = sit.get("income")
        parts.append("доходы: не указано" if income is None else
                     ", ".join(self.value("income", x) for x in income) if income else "доходы: —")
        ties = sit.get("ties")
        parts.append("связи: " + ("не указано" if ties is None else
                                  ", ".join(self.value("ties", x) for x in ties) if ties else "—"))
        return " · ".join(parts)

    def top(self, top: dict | None) -> dict | None:
        if not top:
            return None
        return {"title": self.ground(top["ground_id"]), "share": pct_text(top["share"])}


def _kind_chips(kinds: dict) -> list[dict]:
    return [{"label": KIND_RU.get(k, k), "n": n} for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))]


def _bar_rows(rows: list[dict], key: str = "n") -> list[dict]:
    mx = max((r[key] for r in rows), default=0)
    for r in rows:
        r["w"] = wclass(r[key] / mx if mx else 0)
    return rows


def _ru_date(day: str) -> str:
    """YYYY-MM-DD -> DD.MM.YYYY."""
    return f"{day[8:10]}.{day[5:7]}.{day[0:4]}"


def _money(x: float) -> str:
    return "< $0.01" if 0 < x < 0.01 else f"${x:.2f}"


@router.get("/stats")
def stats(request: Request, period: str | None = None, user: str = Depends(current_user),
          conn: sqlite3.Connection = Depends(get_conn)):
    period = _period(period)
    since = _since(period)
    names = _Names(conn)
    tz = str(get_state(request).tz)
    ov = Q.overview(conn, since, tz)
    dg = Q.dimensions_and_grounds(conn, since, tz)  # один проход по stats_results
    dropoff = Q.dropoff(conn, since)
    first_day = Q.history_start(conn, since, tz)

    daily = [{"day": f"{r['day'][8:10]}.{r['day'][5:7]}", "started": r["started"], "results": r["results"],
              "consults": r["consults"], "errors": r["errors"], "ai": _money(r["ai_cost"]), "n": r["started"]}
             for r in Q.daily(conn, since, tz)]
    drop = _bar_rows([{"label": names.question(r["qid"]) if r["qid"] != Q.NO_QID_LABEL else Q.NO_QID_LABEL,
                       "n": r["n"]} for r in dropoff["items"]])
    kinds = _kind_chips(ov["by_kind"])
    grounds = []
    for r in dg["grounds"]:
        segs = [{"label": STATUS_RU.get(s, (s, "s-clarify"))[0], "cls": STATUS_RU.get(s, (s, "s-clarify"))[1],
                 "n": r["statuses"][s], "w": wclass(r["statuses"][s] / r["evaluated"])}
                for s in (*STATUS_ORDER, *sorted(set(r["statuses"]) - set(STATUS_ORDER))) if r["statuses"].get(s)]
        grounds.append({"title": names.ground(r["ground_id"]), "primary": r["primary"], "evaluated": r["evaluated"],
                        "segs": segs})
    dims = []
    for dim in Q.DIMENSIONS:
        rows = [{"label": names.value(dim, r["value"]), "n": r["n"], "pct": pct_text(r["share"]),
                 "w": wclass(r["share"]), "top": names.top(r["top_primary"])} for r in dg["dimensions"][dim]]
        dims.append({"title": DIM_TITLES[dim], "multi": dim in Q.LIST_DIMENSIONS, "rows": rows})
    sits = [{"text": names.situation_text(s["situation"]), "n": s["n"], "family": pct_text(s["members_share"]),
             "top": names.top(s["top_primary"]), "chips": _kind_chips(s["kinds"]),
             "url": f"/stats/situation?key={quote(s['key'], safe='')}&period={period}"}
            for s in dg["situations"]]
    audit = conn.execute("SELECT at, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT 50").fetchall()
    consults = ov["consults"]
    tiles = [
        ("Новые пользователи", str(ov["new_users"]), ""),
        ("Начато анкет", str(ov["started"]), ""),
        ("Дошли до результата", str(ov["completed"]), f"конверсия {pct_text(ov['conversion'])}"),
        ("Заявки на консультацию", str(sum(consults.values())),
         f"отправлено {consults['sent']} / не подтверждено {consults['unknown']} / ошибка {consults['failed']}"),
        ("Ошибки бота", str(ov["errors"]), ""),
        ("Разбор текста недоступен", str(ov["jev_unavailable"]), ""),
        ("Расходы на AI", _money(ov["ai_cost_usd"]), ""),
        ("Объяснения от модели", pct_text(ov["llm_share"]), "остальное — шаблон"),
    ]
    return render(request, "stats.html", periods=PERIODS, period=period, tiles=tiles, daily=_bar_rows(daily),
                  drop=drop, in_progress=dropoff["in_progress"], recalc=ov["recalculations"],
                  history_from=_ru_date(first_day) if first_day else None, kinds=kinds, grounds=grounds, dims=dims, sits=sits, audit=audit,
                  status_legend=[STATUS_RU[s] for s in STATUS_ORDER])


@router.get("/stats/situation")
def stats_situation(request: Request, key: str = "", period: str | None = None, user: str = Depends(current_user),
                    conn: sqlite3.Connection = Depends(get_conn)):
    period = _period(period)
    key = key if len(key) <= MAX_KEY_LEN else ""
    names = _Names(conn)
    title, rows = "Ситуация", []
    if key:
        found = conn.execute("SELECT situation FROM stats_results WHERE situation_key=? LIMIT 1", (key,)).fetchone()
        if found is not None:
            with contextlib.suppress(ValueError):
                title = names.situation_text(json.loads(found["situation"]))
        for r in Q.situation_rows(conn, key, _since(period), tz=str(get_state(request).tz)):
            chips = [{"title": names.ground(g), "label": STATUS_RU.get(s, (s, "s-clarify"))[0],
                      "cls": STATUS_RU.get(s, (s, "s-clarify"))[1]} for g, s in r["grounds"].items()]
            first_status = STATUS_RU.get(r["primary_status"] or "", ("", ""))[0]
            rows.append({
                "day": _ru_date(r["day"]), "release_id": r["release_id"], "kind": KIND_RU.get(r["result_kind"], r["result_kind"]),
                "first": names.ground(r["primary_ground"]) if r["primary_ground"] else "—", "first_status": first_status,
                "chips": chips, "family": r["members"] > 0,
                "source": "модель" if r["explain_source"] == "llm" else "шаблон",
            })
    return render(request, "stats_situation.html", title=title, rows=rows, period=period)
