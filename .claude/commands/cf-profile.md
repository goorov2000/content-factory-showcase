---
description: Raw Batch Profiler - проверить качество батча перед анализом
argument-hint: "[tab=raw_tiktok] [--niche X] [--since YYYY-MM-DD]"
---
Следуй промпту prompts/agents/raw-batch-profiler.md. Аргументы: $ARGUMENTS
(по умолчанию tab=raw_tiktok). Запуск логируется самим CLI — log-run вручную не нужен.
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
