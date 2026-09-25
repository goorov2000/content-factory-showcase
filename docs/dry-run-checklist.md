# Сквозной прогон полного цикла (ручной dry run)

Предусловия: cf.config.json заполнен реальным spreadsheet_id;
~/.cf/secrets/service-account.json на месте; таблица расшарена на email сервисного аккаунта;
в CF Raw TikTok >= 20 строк.

1. `/cf-status` — таблица доступна, ошибок нет.
2. `/cf-profile raw_tiktok` — отчёт с ready_for_analysis: true (иначе добрать данные).
3. `/cf-analyze raw_tiktok --niche <ниша>` — patterns-файл создан и валиден,
   наблюдения в памяти, запись в Run Log.
4. `/cf-formula` — формула в formulas/<ниша>/ валидна; после «утверждаю» попала в
   formulas/_approved/index.json, решение в памяти, commit прошёл guard.
5. `/cf-generate-briefs` сгенерировал бриф из approved-формулы → строка в CF Creative
   Briefs со status=pending (n8n брифы не пишет с 2026-07-10). (Агент недоступен →
   добавить строку руками по prompts/briefs/_shared/schema.md.)
6. `/cf-review-brief --pending` — вердикт вынесен, review_status обновлён,
   review-файл в agent-runtime/reviews/.
7. Заполнить CF Published Reels и CF Performance для одобренного брифа (или дождаться n8n).
8. `/cf-eval` — weekly-eval.json с success_criteria и атрибуцией; инсайты в памяти.
9. `/cf-propose-update <prompt_id>` — proposal с evidence; проверить оба исхода:
   approve (промпт изменён, новая строка в CF Prompt Versions, commit) и
   reject (промпт не тронут, решение в памяти).
10. `.venv/Scripts/python -m cf trace <brief_id>` — цепочка без MISSING.
11. `git log --oneline` — нет коммитов с agent-runtime/ или секретами.

Все 11 шагов прошли без правок кода → success criteria спеки закрыты, MVP готов.
