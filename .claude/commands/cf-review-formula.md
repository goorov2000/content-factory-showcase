---
description: Formula Reviewer - ревью черновика рецепта перед машинным одобрением
argument-hint: "<путь к formulas/тема/имя.json> | --proposed"
---
Следуй промпту prompts/agents/formula-reviewer.md. Аргументы: $ARGUMENTS

ПЕРВЫМ ДЕЙСТВИЕМ запомни время старта: `date -u +%Y-%m-%dT%H:%M:%S+00:00`.

Порядок:
1. Собери очередь. С аргументом-путём — один рецепт. С `--proposed` (или без
   аргументов) — все `formulas/*/*.json` со `status: proposed`, КРОМЕ каталога
   `formulas/_approved/` и тем из `dashboard.fanout.exclude_niches` в
   `cf.config.json`. Очередь пуста → залогируй холостой прогон и остановись:
   `.venv/bin/python -m cf log-run --agent formula-reviewer --status skipped
   --input "нет черновиков рецептов" --started-at <время старта>` (правило №6).
2. Прогони по каждому черновику машинный чек-лист, чтобы не тратить суждение на
   то, что уже посчитано: `.venv/bin/python -m cf auto-approve-formula <путь>
   --check --no-judge`. Красные пункты процитируй в reasons — они часть картины,
   но твой вердикт от них не зависит.
3. Вынеси вердикт по пяти проверкам промпта агента.
4. Запиши `agent-runtime/reviews-formula/YYYY-MM-DD-<имя>-review.json`:
   {"name": ..., "niche": ..., "verdict": "recommend|revise|reject",
    "reasons": [...], "checked_at": ...}. Имя файла обязано содержать имя рецепта
   целиком — по нему раннер находит твой вердикт.
5. СРАЗУ после записи вердикта по КАЖДОМУ рецепту (не в конце всего цикла)
   залогируй прогон:
   `.venv/bin/python -m cf log-run --agent formula-reviewer --status success
   --input "proposed:N" --outputs <файлы ревью> --started-at <время старта>`.
   Прогон 27.07 закончился без единой строки в Run Log именно потому, что лог
   стоял последним шагом после длинного цикла.
6. Покажи оператору таблицу: рецепт, вердикт, одна строка причины.

Статус рецепта ты НЕ меняешь и `cf auto-approve-formula` без `--check` НЕ
вызываешь: решение по твоему вердикту принимает раннер конвейера
(`_run_formula_gate_worker`). Твой продукт — вердикт-файл, и только он.
