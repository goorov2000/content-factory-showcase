---
description: Source Tuner - скоринг источников сбора, proposal на обновление списков
---
Следуй промпту prompts/agents/source-tuner.md. Аргументы: $ARGUMENTS
(по умолчанию обе вкладки). В конце залогируй запуск:
.venv/bin/python -m cf log-run --agent source-tuner --status <success|insufficient_data> ...
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
