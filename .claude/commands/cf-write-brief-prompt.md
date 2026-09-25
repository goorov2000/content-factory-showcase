---
description: Brief Prompt Writer - написать brief-промпт ниши по её утверждённым формулам (proposal)
---
Следуй промпту prompts/agents/brief-prompt-writer.md. Аргументы: $ARGUMENTS
(имя ниши; без аргумента — первая ниша, у которой есть утверждённая формула, но нет
prompts/briefs/{niche}/reel.md). В конце залогируй запуск:
.venv/bin/python -m cf log-run --agent brief-prompt-writer --status <success|insufficient_data|failed> ...
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
