---
description: Formula Writer - вывести формулу из валидированных паттернов
argument-hint: "[путь к patterns-файлу] [pattern_id ...]"
---
Следуй промпту prompts/agents/formula-writer.md. Аргументы: $ARGUMENTS

Порядок:
1. Прочитай patterns-файл (не указан → свежайший в agent-runtime/patterns/).
   Возьми паттерны с confidence high|medium; если заданы pattern_id — только их.
2. Подходящих паттернов нет → сообщи insufficient_data, залогируй
   (`.venv/bin/python -m cf log-run --agent formula-writer --status insufficient_data`) и остановись.
3. Паттерны с human_review: true (конфликты) → сначала покажи оба варианта оператору
   и спроси решение; в формулы конфликтующие паттерны идут только с взаимоисключающими
   conditions.
4. Собери формулу(ы) по правилам промпта агента; проверь
   `.venv/bin/python -m cf validate formula formulas/{niche}/{name}.json`; исправь ошибки.
   Верификация evidence срезала формулу до < 3 URL → формулу не писать:
   `.venv/bin/python -m cf log-run --agent formula-writer --status insufficient_data
   --errors "<какие URL и почему не прошли>"`, сообщи оператору и остановись
   (шаг 6 со status success в этом случае НЕ выполняется).
5. Напиши человекочитаемый formulas/{niche}/{name}.md.
6. Залогируй: `.venv/bin/python -m cf log-run --agent formula-writer --status success --outputs <файлы>`.
7. Покажи формулы оператору. Утверждение — отдельный шаг (см. Task 11): по явному
   «утверждаю» выполни approve-formula и запиши решение в память.

После явного «утверждаю» от оператора:
1. `.venv/bin/python -m cf formula-status formulas/{niche}/{name}.json approved`
   (или кнопка на дашборде — она сама пишет decision-файл и коммитит). Approve
   копирует формулу в иммутабельный снапшот formulas/_approved/{niche}/{name}-vN.json
   и ставит его path в индекс; писать в formulas/_approved/ вручную ЗАПРЕЩЕНО.
2. Запиши решение: .claude/memory/decisions/YYYY-MM-DD-approve-{name}.md — что утверждено,
   почему (evidence кратко), какие альтернативы отклонены. Добавь строку в MEMORY.md.
3. Git-команды агенту запрещены — коммитит раннер/дашборд.
Индекс formulas/_approved/index.json — единственный источник approved-формул для генерации брифов (n8n-сбор списан 2026-07-24).
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
