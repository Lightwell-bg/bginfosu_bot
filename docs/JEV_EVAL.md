# Оценка разбора текста через JEV

Дата прогона: 2026-10-09. Модель: `~typesafe/jev-latest`. Живые вызовы OpenRouter System One.
Порог распознавания: 0.3, порог «явно нет»: 0.75.

- Примеров: 20; ожидаемых направлений: 31
- **Пропущено направлений: 0** (полнота 100%)
- Помечено «неясно» и будет спрошено явно: 1 (c12_already_in_bg/case:who_moves:already_in_bg)
- Ложных срабатываний из списка forbid: 0
- Фактическая стоимость прогона (usage.cost): $0.005173

Ноль пропусков на этой выборке не означает гарантированной точности. Пропуск JEV не исключает
основание: анкета всё равно задаёт контрольные вопросы с выбором вариантов, а распознанное
только предвыбирается и подтверждается пользователем.

| Пример | Пропуски | Ложные срабатывания | Намерение / неясное |
|---|---|---|---|
| c01_pensioner_wife_works | — | — | new_info (1.00); unclear: — |
| c02_it_remote | — | — | new_info (1.00); unclear: — |
| c03_freelancer_typos | — | — | new_info (0.97); unclear: — |
| c04_negation_pension | — | — | new_info (0.97); unclear: — |
| c05_job_offer | — | — | new_info (1.00); unclear: — |
| c06_student | — | — | new_info (1.00); unclear: — |
| c07_bg_spouse | — | — | new_info (1.00); unclear: — |
| c08_bg_origin | — | — | question_to_bot (0.96); unclear: — |
| c09_investor | — | — | new_info (0.97); unclear: — |
| c10_business | — | — | new_info (0.68); unclear: — |
| c11_eu_citizen | — | — | new_info (1.00); unclear: — |
| c12_already_in_bg | — | — | new_info (0.98); unclear: who_moves |
| c13_mixed_family | — | — | new_info (1.00); unclear: — |
| c14_passive | — | — | new_info (0.97); unclear: — |
| c15_for_other | — | — | new_info (0.96); unclear: — |
| c16_short_vague | — | — | other (0.56); unclear: — |
| c17_rep_office | — | — | other (0.62); unclear: — |
| c18_contradiction | — | — | correction (0.82); unclear: income_sources |
| c19_parents_and_kids | — | — | new_info (1.00); unclear: — |
| c20_eu_relative | — | — | new_info (1.00); unclear: — |
