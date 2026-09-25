---
description: Pattern Analyzer - анализ паттернов профилированного батча
argument-hint: "[tab=raw_tiktok] [--niche X] [--since YYYY-MM-DD]"
---
Следуй промпту prompts/agents/pattern-analyzer.md. Аргументы: $ARGUMENTS

Порядок:
1. Найди свежайший agent-runtime/profiles/*-profile.json для этой вкладки (и ниши, если
   задана). Отчёта нет или ready_for_analysis: false → остановись и попроси оператора
   запустить /cf-profile. Анализ без профиля запрещён.
2. Выполни `.venv/bin/python -m cf analyze-batch <tab> --niche <niche> [--since ...]` —
   он сам берёт строки из Sheets, дедуплицирует, считает квартили/winners/losers/
   same-account. status: insufficient_data → залогируй и остановись (это не ошибка).
3. По отчёту (agent-runtime/analysis/*-analysis.json) выполни анализ по промпту
   агента (смысловая часть — формулировка паттернов и верификация evidence, механику
   руками НЕ пересчитывай); запиши patterns-файл; проверь
   `.venv/bin/python -m cf validate patterns <файл>`; исправь ошибки валидации и перепроверь.
4. Запиши наблюдения в память и залогируй запуск (log-run).
5. Сводка оператору: сколько паттернов, какие confidence, есть ли конфликтующие
   (по ним попроси решение оператора).
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
