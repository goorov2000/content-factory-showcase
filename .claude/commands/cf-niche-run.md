---
description: Niche Pipeline - профиль + анализ + черновики формул одной ниши (для фан-аута)
argument-hint: "<tab> <niche>"
---
Один прогон = одна ниша. Аргументы: $ARGUMENTS (вкладка, ниша).

Порядок (механика уже посчитана командами cf — НЕ пересчитывай квартили руками):
1. `.venv/bin/python -m cf profile <tab> --niche <niche>` — ready_for_analysis: false
   → залогируй insufficient_data (шагом 5, агент niche-pipeline; авто-лог profile
   его НЕ заменяет), выведи NICHE_RESULT со status: "insufficient_data",
   patterns: 0, formulas: 0 — и только после этого остановись (это не ошибка).
2. `.venv/bin/python -m cf analyze-batch <tab> --niche <niche>` — status
   insufficient_data → залогируй insufficient_data (шагом 5; авто-лог analyze-batch
   его НЕ заменяет), выведи NICHE_RESULT со status: "insufficient_data",
   patterns: 0, formulas: 0 — и только после этого остановись.
3. Прочитай analysis-JSON. Твоя работа — только смысл: следуй
   prompts/agents/pattern-analyzer.md целиком (Метод, Confidence, Правила):
   сформулируй паттерны из winners/losers/same_account, верифицируй каждый URL
   по транскрипту/капшену, отбрось непрошедшие. Запиши
   agent-runtime/patterns/YYYY-MM-DD-<niche>-<tab>-patterns.json,
   проверь `.venv/bin/python -m cf validate patterns <файл>`.
4. Из паттернов confidence high|medium собери черновики формул по
   prompts/agents/formula-writer.md со `"status": "proposed"` в JSON
   (утверждение — НЕ твоя работа, кнопка на дашборде). Файлы:
   formulas/<niche>/<name>.json + .md. Каталог formulas/_approved/ трогать
   ЗАПРЕЩЕНО (снапшоты утверждённых версий создаёт только approve оператора). `.venv/bin/python -m cf validate formula <файл>`.
   Git-команды ЗАПРЕЩЕНЫ — коммитит раннер.
5. Залогируй прогон: `.venv/bin/python -m cf log-run --agent niche-pipeline
   --status <success|insufficient_data> --input "<tab>/<niche>"
   --outputs <файлы>`.
6. В любом исходе, включая ранний стоп на шагах 1-2, последней строкой ответа
   выведи ровно одну строку-сводку в КАНОНИЧЕСКОМ формате (раннер парсит её):
   NICHE_RESULT: {"niche": "<niche>", "patterns": N, "formulas": M, "status": "<status>"}
   где status — ровно одно из: "ok" (формулы выведены успешно) |
   "insufficient_data" (ранний стоп на шагах 1-2) | "error" (прогон не удался).
   Успех обозначай "ok" (не "success"). Строка обязательна и valid-JSON во всех
   исходах: при коде выхода 0 её отсутствие или битый JSON раннер трактует как
   error ниши, а не как чистый прогон с formulas: 0.
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
