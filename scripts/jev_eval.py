"""Оценка разбора русского текста через JEV на размеченных примерах (tests/jev_eval/cases.yaml).

ЖИВЫЕ вызовы OpenRouter (нужен OPENROUTER_API_KEY). Главная метрика — пропуски направлений
(expect, которые не распознаны). Ноль пропусков на этой выборке НЕ гарантирует точность в общем
случае: пропуск страхуется контрольными кнопочными вопросами анкеты.
Запуск:  python scripts/jev_eval.py [--out docs/JEV_EVAL.md]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

import yaml

from bginfo.ai.interpret import build_questions, map_answers
from bginfo.ai.openrouter import AiError, OpenRouterClient
from bginfo.config import get_config
from bginfo.seed.loader import load_seed_bundle

ROOT = Path(__file__).resolve().parents[1]


def labels(it) -> set[str]:
    out = set()
    for r in it.items:
        vals = r.value if isinstance(r.value, list) else [r.value]
        for v in vals:
            out.add(f"{r.role}:{r.key}:{str(v).lower() if isinstance(v, bool) else v}")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "JEV_EVAL.md"))
    args = ap.parse_args()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY не задан — живой прогон невозможен.")
        return 2
    cfg = get_config().ai
    kb = load_seed_bundle()
    cases = yaml.safe_load((ROOT / "tests" / "jev_eval" / "cases.yaml").read_text(encoding="utf-8"))
    client = OpenRouterClient(key, base_url=cfg.base_url, timeout_s=cfg.timeout_s, retries=cfg.retries)
    questions, index = build_questions(kb, None)
    misses, fps, total_expect, cost = [], [], 0, 0.0
    clarified: list[tuple[str, str]] = []
    rows = []
    for c in cases:
        try:
            answers, usage = await client.systemone(cfg.jev_model, {"user_message": c["text"]}, questions)
        except AiError as e:
            rows.append(f"| {c['id']} | ОШИБКА {e.kind} | | |")
            misses.append((c["id"], "call_failed"))
            continue
        cost += usage.cost_usd or 0
        it = map_answers(answers, index, cfg.jev_recognize_threshold, cfg.jev_negation_threshold)
        got = labels(it)
        exp = set(c.get("expect", []))
        total_expect += len(exp)
        # «неясно» по факту — бот задаст этот вопрос явно, направление не теряется
        asked = sorted(e for e in exp - got if e.split(":")[1] in it.unclear)
        clarified.extend((c["id"], a) for a in asked)
        miss = sorted(exp - got - set(asked))
        for group in c.get("any_of", []):
            total_expect += 1
            if not (set(group) & got):
                miss.append("any_of:" + "|".join(group))
        fp = sorted(set(c.get("forbid", [])) & got)
        misses += [(c["id"], m) for m in miss]
        fps += [(c["id"], f) for f in fp]
        rows.append(f"| {c['id']} | {', '.join(miss) or '—'} | {', '.join(fp) or '—'} | "
                    f"{it.intent} ({it.intent_conf:.2f}); unclear: {', '.join(it.unclear) or '—'} |")
        print(c["id"], "miss:", miss, "fp:", fp)
    await client.aclose()

    recall = 1 - len(misses) / max(1, total_expect)
    report = [
        "# Оценка разбора текста через JEV",
        "",
        f"Дата прогона: {date.today().isoformat()}. Модель: `{cfg.jev_model}`. Живые вызовы OpenRouter System One.",  # noqa: DTZ011 - локальная дата отчёта
        f"Порог распознавания: {cfg.jev_recognize_threshold}, порог «явно нет»: {cfg.jev_negation_threshold}.",
        "",
        f"- Примеров: {len(cases)}; ожидаемых направлений: {total_expect}",
        f"- **Пропущено направлений: {len(misses)}** (полнота {recall:.0%})",
        (f"- Помечено «неясно» и будет спрошено явно: {len(clarified)} "
         f"({', '.join(f'{a}/{b}' for a, b in clarified) or '—'})"),
        f"- Ложных срабатываний из списка forbid: {len(fps)}",
        f"- Фактическая стоимость прогона (usage.cost): ${cost:.6f}",
        "",
        "Ноль пропусков на этой выборке не означает гарантированной точности. Пропуск JEV не исключает",
        "основание: анкета всё равно задаёт контрольные вопросы с выбором вариантов, а распознанное",
        "только предвыбирается и подтверждается пользователем.",
        "",
        "| Пример | Пропуски | Ложные срабатывания | Намерение / неясное |",
        "|---|---|---|---|",
        *rows,
        "",
    ]
    Path(args.out).write_text("\n".join(report), encoding="utf-8")
    print(f"\nПропусков: {len(misses)} из {total_expect}; ложных: {len(fps)}; стоимость ${cost:.6f}; отчёт: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
