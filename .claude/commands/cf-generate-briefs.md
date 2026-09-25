Следуй промпту prompts/agents/brief-generator.md. Аргументы: $ARGUMENTS
(опционально: имя формулы; без аргументов — все формулы из approved-индекса).

Порядок (каркас; точные правила, числа и форматы — в brief-generator.md,
не дублируй их здесь, чтобы не плодить второй источник дрейфа):
1. Прочитай formulas/_approved/index.json; пусто → сообщи и остановись.
2. Сними снапшот брифов (`.venv/bin/python -m cf read briefs`) и посчитай по каждой формуле
   approved-брифы за последние 7 дней (review_status=approved, момент одобрения:
   reviewed_at, при пустом — generated_at) — формула с >= 5 пропускается (отметь в сводке).
3. Для каждой оставшейся формулы возьми активную версию промпта ниши
   (`.venv/bin/python -m cf read prompt_versions`, prompt_id=brief-{niche}-reel). Нет активной →
   НЕ молчаливый скип: в сводке ЯВНО «ниша X ждёт brief-промпта» и к следующей формуле.
4. Построй план версий A/B — это ОБЯЗАТЕЛЬНЫЙ вход генерации, а не опция:
   `.venv/bin/python -m cf ab-plan --prompt-id brief-{niche}-reel --count {N}` (N = сколько брифов
   даёшь формуле за прогон, до briefs_per_formula). План возвращает по строке
   `{index, prompt_version, github_path, cohort}` на бриф; есть candidate → active/candidate
   чередуются ~50/50 (честный interleaving — P5.13). Команда упала (ненулевой код) →
   версии руками НЕ выдумывай: `.venv/bin/python -m cf log-run --agent brief-generator
   --status failed --errors "ab-plan: <ошибка>"` и останови ИМЕННО эту формулу
   (правило 3а brief-generator.md).
5. Генерируй строго по плану: бриф с `index` i пиши по промпту из его `github_path`
   и проставляй `prompt_version` из плана в бриф-JSON (сам, без внешних LLM-нод),
   ротируя tested_variable ортогонально версии; canonical JSON → agent-runtime/briefs/,
   запись: `.venv/bin/python -m cf add-brief <файл> --caption "<подпись>"`.
6. Залогируй запуск (`.venv/bin/python -m cf log-run --agent brief-generator ...`).
7. Покажи оператору, что сгенерировано, и предложи /cf-review-brief --pending.
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
