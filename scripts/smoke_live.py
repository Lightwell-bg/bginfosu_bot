"""Живой smoke-тест: полный путь анкеты через контроллер с РЕАЛЬНЫМИ вызовами OpenRouter (JEV + LLM).

Telegram не используется. Нужен OPENROUTER_API_KEY в окружении. База — тестовый демо-бандл в памяти.
Запуск:  python scripts/smoke_live.py
Печатает шаги, итоговый текст, источник объяснения (llm/template) и фактическую стоимость из usage.cost.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

from bginfo.ai.explain import Explainer
from bginfo.ai.interpret import Interpreter
from bginfo.ai.ledger import AiLedger
from bginfo.ai.openrouter import OpenRouterClient
from bginfo.config import get_config
from bginfo.db.conn import open_db
from bginfo.engine.dialog import Dialog, Event, SessionData, UserCtx
from bginfo.engine.facts import Profile
from bginfo.kb.releases import Snapshot
from bginfo.seed.loader import load_seed_bundle

TEXT = "Я пенсионер из Казахстана, пенсия около 700 евро, жена тоже на пенсии, хотим переехать вместе, квартиру в Варне уже купили"


def plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


async def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY не задан — живой вызов невозможен.")
        return 2
    cfg = get_config().ai
    db_path = Path(tempfile.mkdtemp()) / "smoke.db"
    conn = open_db(db_path)
    ledger = AiLedger(conn, daily_budget_usd=1.0, per_user_daily={"jev": 50, "extract": 50, "explain": 10})
    client = OpenRouterClient(key, base_url=cfg.base_url, timeout_s=cfg.timeout_s, retries=cfg.retries,
                              app_title=cfg.app_title, http_referer=cfg.http_referer)
    interp = Interpreter(client, ledger, jev_model=cfg.jev_model, llm_model=cfg.llm_model,
                         recognize_at=cfg.jev_recognize_threshold, negate_at=cfg.jev_negation_threshold)
    expl = Explainer(client, ledger, model=cfg.llm_model, max_tokens=cfg.explain_max_tokens)
    dialog = Dialog(interp, expl, today=lambda: date.today(), consult_enabled=False)  # noqa: DTZ011 - ручной смоук, локальная дата
    snap = Snapshot(1, "2026-01-01T00:00:00Z", load_seed_bundle(demo=True))
    s = SessionData(1, 0, "start", Profile(), {}, None)
    u = UserCtx(1, None, True)

    out = await dialog.handle(s, Event("button", action="free"), snap, u)
    print(">>> ТЕКСТ:", TEXT)
    out = await dialog.handle(s, Event("text", text=TEXT), snap, u)
    for step in range(40):
        last = out.replies[-1] if out.replies else None
        if last is None:
            break
        print(f"\n[{step}] {plain(last.html)[:300]}")
        if s.state == "result":
            break
        btns = [b for row in last.buttons for b in row]
        acts = {b.action: b for b in btns}
        if "review_ok" in acts:
            ev = Event("button", action="review_ok")
        elif "pend_ok" in acts:
            ev = Event("button", action="pend_ok")
        elif "conf" in acts:
            ev = Event("button", action="conf", arg="new")
        elif "done" in acts:
            ev = Event("button", action="done")
            if not s.ui.get("sel"):
                # «Другая страна» для гражданства, «Ничего из этого» для остальных списков
                pick = next((b for b in btns if b.action == "tog" and b.arg in ("third", "none")), None)
                pick = pick or next(b for b in btns if b.action == "tog")
                await dialog.handle(s, Event("button", action="tog", arg=pick.arg), snap, u)
        elif "ans" in acts:
            yes = next((b for b in btns if b.action == "ans" and b.arg == "yes"), None)
            ev = Event("button", action="ans", arg=(yes or acts["ans"]).arg)
        elif "unk" in acts:
            ev = Event("button", action="unk")
        else:
            ev = Event("text", text="1")
        print("    -> ", ev.action, ev.arg, ev.text)
        out = await dialog.handle(s, ev, snap, u)

    print("\n=========== ИТОГ ===========")
    print(plain(out.replies[-1].html) if out.replies else "(нет ответа)")
    print("\nИсточник объяснения:", (out.result_record or {}).get("explain_source"))
    if "-v" in sys.argv:
        for r in (out.result_record or {}).get("evaluation", []):
            print(f"  {r['subject']:4} {r['ground']:24} {r['status']:14} {'; '.join(r['reasons'])[:150]}")
        for f in s.profile.facts.values():
            print(f"  fact {f.participant}:{f.key} = {f.value} [{f.state.value}/{f.source}]")
    rows = conn.execute("SELECT kind, model, ok, error, input_tokens, output_tokens, cost_usd FROM ai_calls").fetchall()
    total = 0.0
    for r in rows:
        print(f"  {r['kind']:8} {r['model']:32} ok={r['ok']} tokens={r['input_tokens']}/{r['output_tokens']} "
              f"cost={r['cost_usd']} {r['error'][:80]}")
        total += r["cost_usd"] or 0
    print(f"Фактическая стоимость (usage.cost): ${total:.6f}")
    await client.aclose()
    return 0 if s.state == "result" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
