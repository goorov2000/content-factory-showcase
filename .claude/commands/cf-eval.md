---
description: Eval Agent - еженедельная оценка performance по prompt_version
argument-hint: "[--since YYYY-MM-DD]"
---
Следуй промпту prompts/agents/eval-agent.md. Аргументы: $ARGUMENTS

1. `.venv/bin/python -m cf eval-prep $ARGUMENTS` — собери датасет.
2. Гейт «мало данных» — по порогам из eval-agent.md (per-version и unknown-доля), числа
   не дублируй здесь. Если датасет помечен insufficient_data (unknown-доля) → `.venv/bin/python -m cf log-run
   --agent eval-agent --status insufficient_data --input "<since>"`, скажи
   оператору, чего не хватает, и остановись.
3. Построй weekly-eval.json по промпту агента; запиши инсайты в память.
4. `.venv/bin/python -m cf log-run --agent eval-agent --status success --outputs <отчёт>`.
5. Покажи оператору: success_criteria (met/не met), топ-3 инсайта, атрибуцию провалов.
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
