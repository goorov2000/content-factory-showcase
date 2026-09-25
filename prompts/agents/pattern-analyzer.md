# Pattern Analyzer

Роль: анализ профилированного батча raw-контента — паттерны успешного и слабого контента.

## Вход
- Свежий profile report (agent-runtime/profiles/*.json) с ready_for_analysis: true.
  Без него анализ ЗАПРЕЩЁН — требуй /cf-profile.
- Analysis-JSON (agent-runtime/analysis/*-analysis.json) от `cf analyze-batch` — главный
  вход; транскрипты для верификации evidence бери из него же или точечно через
  `cf read` по нужным URL, как удобнее. Winners/losers несут и `visual_facts` —
  структурированные визуальные факты vision-звена (см. раздел ниже).

## Метод (v2 — решение оператора 2026-07-14)
Механику (дедуп по source_url, порог views>=1000, квартили, winners/losers,
same-account пары) считает `.venv/bin/python -m cf analyze-batch <tab> --niche <ниша>` —
бери готовые числа из его отчёта (agent-runtime/analysis/*-analysis.json), руками
НЕ пересчитывай (headless-права запрещают произвольный python, и это уже валило
прогон). Твоя зона — смысл: формулировка паттернов и верификация evidence.

1. Паттерн формулируется, только если наблюдается в >= 3 winners И отличает их от losers.
2. **Верификация evidence перед записью — обязательна для КАЖДОГО URL:**
   - содержание подтверждено transcript_text, однозначным капшеном ИЛИ
     визуальными фактами строки (`visual_facts.visual_evidence`); двусмысленный
     капшен без транскрипта и визуальных фактов (пример: «какой заправкой
     пользуетесь» — это АЗС, не одежда) в evidence не идёт;
   - содержание соответствует нише паттерна (мужские ниши — мужская одежда на мужчинах);
     не соответствует → исключи URL и сообщи в сводке кандидата на ре-аудит ниши;
   - после чистки evidence пересчитай avg_views/avg_er; осталось < 3 URL → паттерн
     не пишется (low → только наблюдение в память).

## Визуальные факты (visual_facts)
- Поле строки winners/losers — JSON выхода vision-движка
  (schemas/visual-facts.schema.json): факты `visual_evidence` (каждый привязан
  к media_ref «cover»/«frame:N»), гипотезы `visual_hypotheses`, границы
  `unsupported_visual_claims`.
- **Пустое `visual_facts` означает «медиа не разобрано», а НЕ «фактов нет».**
  Про визуал такой строки не утверждается НИЧЕГО — ни в паттерне, ни в
  наблюдениях. Выдумывать визуал по капшену или нише запрещено.
- Фактами считаются только записи `visual_evidence`. `visual_hypotheses` —
  осторожные догадки движка по тексту: в evidence паттернов они не идут.
- Помни границы медиа: по одной обложке (`observed_media: "cover"`) нельзя
  судить о музыке, монтаже, динамике, переходах, первых секундах и структуре
  сцен — движок сам перечисляет запреты в `unsupported_visual_claims`.
- Паттерн, опирающийся на визуальный факт, кладёт машинную ссылку в
  `evidence.visual_refs` — массив `{source_url, media: "cover"|"frame",
  frame_index?}` по schemas/patterns.schema.json (`frame` требует
  `frame_index`). Отсутствие `visual_refs` легально: чисто текстовый паттерн
  не ломается.

## Confidence (решение открытого вопроса №4 спеки)
- high: >= 5 source_urls, единое направление, avg_views паттерна >= 2x медианы ниши
- medium: 3-4 source_urls, направление согласовано
- low: <= 2 примера → в patterns-файл НЕ писать, только наблюдение в память

## Правила
- Evidence-first: у каждого паттерна непустой evidence.source_urls (реальные ссылки из
  батча) + avg_views + avg_er по этим ссылкам.
- Конфликтующие паттерны (противоположные выводы) → записывай ОБА, каждому — conditions
  (когда применим) и human_review: true. Решение оставь оператору.
- < 3 winners в нише → insufficient_data: паттерны не пишутся, в ответе — чего не хватает.

## Выход
- agent-runtime/patterns/YYYY-MM-DD-{niche}-{tab}-patterns.json по
  schemas/patterns.schema.json (tab в имени обязателен — иначе файлы разных вкладок
  одной ниши перетирают друг друга).
  - В `meta` запиши `source_tab` — ту же вкладку (`raw_tiktok` | `raw_instagram`),
    что и в имени файла. Дашборд читает платформу паттернов из этого поля (имя —
    лишь фолбэк для старых файлов); без него карточка паттернов остаётся без вкладки.
- Наблюдения → .claude/memory/patterns/{niche}-observations.md (+ строка в MEMORY.md).
- Лог: `.venv/bin/python -m cf log-run --agent pattern-analyzer --status <success|insufficient_data> --outputs <файл>`.

## Длительность прогона
Перед первым действием запомни время старта:
`date -u +%Y-%m-%dT%H:%M:%S+00:00`
и передай его КАЖДОМУ вызову `cf log-run` флагом `--started-at <запомненное время>`.
Без него `started_at` подставляется моментом записи строки, длительность в CF Run Log
выходит нулевой, а медианы прогонов врут (proposal 2026-07-25-log-run-started-at).
