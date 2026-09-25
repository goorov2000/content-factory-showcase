---
description: Brief Fixer - переписать забракованный сценарий по замечаниям ревьюера
argument-hint: "<brief_id> | --revised"
---
Следуй промпту prompts/agents/brief-fixer.md. Аргументы: $ARGUMENTS

Порядок:
1. Сними снапшот: `.venv/bin/python -m cf read briefs --out
   agent-runtime/briefs/briefs-snapshot.json`. Возьми брифы со
   `review_status = revised`; с `--revised` (или без аргументов) — все такие,
   КРОМЕ тех, у кого в `reviewer_notes` уже стоит «доработано заводом» (одна
   попытка на бриф). Очередь пуста → залогируй холостой прогон и остановись:
   `.venv/bin/python -m cf log-run --agent brief-fixer --status skipped
   --input "нет сценариев в доработке"` (правило №6).
2. Для каждого: собери бриф из `payload_json`, прочитай `reviewer_notes`, найди
   формулу по `formula_id` в `formulas/_approved/index.json` (ревьюй по СНАПШОТУ,
   не по черновику) и активный промпт темы.
3. Исправь ровно названный дефект по промпту агента. Запиши
   `agent-runtime/briefs/<brief_id>-fixed.json`, проверь
   `.venv/bin/python -m cf validate brief <файл>`.
4. Верни на ревью: `.venv/bin/python -m cf revise-brief <brief_id>
   --file agent-runtime/briefs/<brief_id>-fixed.json --notes "<что исправлено>"`.
   Команда сама откажет, если бриф уже правился или вышел из статуса «доработка», —
   её решение НЕ оспаривай и не подменяй своим set-review.
5. Залогируй: `.venv/bin/python -m cf log-run --agent brief-fixer --status success
   --input "revised:N" --outputs <файлы>`.
6. Покажи оператору таблицу: бриф, замечание одной строкой, что исправлено.
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и
передай его в log-run флагом `--started-at <запомненное время>` — иначе
длительность прогона в CF Run Log будет нулевой.
