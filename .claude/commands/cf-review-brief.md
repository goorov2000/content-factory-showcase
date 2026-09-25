---
description: Brief Reviewer - ревью брифа по формуле (recommend/revise/reject)
argument-hint: "<brief_id> | --pending"
---
Следуй промпту prompts/agents/brief-reviewer.md. Аргументы: $ARGUMENTS

Порядок:
1. Сними снапшот брифов: `.venv/bin/python -m cf read briefs --out agent-runtime/reviews/briefs-snapshot.json`.
   Выбери бриф по brief_id; для --pending — все с review_status=pending
   (таких нет → залогируй холостой прогон и остановись:
   `.venv/bin/python -m cf log-run --agent brief-reviewer --status skipped
   --input "нет pending-брифов"` — правило №6).
2. Для каждого брифа: собери объект брифа из колонки payload_json (полный JSON,
   уже прошёл валидацию по schemas/brief.schema.json при add-brief). Отдельные
   колонки (cta, references, source_pattern_ids) — запасной путь, если payload_json
   пуст: references разбей по "; ", source_pattern_ids по ", ", cta бери из колонки cta.
   Сохрани в agent-runtime/reviews/<brief_id>-input.json, проверь
   `.venv/bin/python -m cf validate brief <файл>`. Схема невалидна → вердикт reject
   с причиной "schema: <первая ошибка>".
3. Найди формулу по formula_id: возьми path (и niche) из formulas/_approved/index.json —
   совпадение по name; path указывает на СНАПШОТ утверждённой версии — ревьюй по нему
   (черновик в formulas/{niche}/ может быть новее). Если в индексе нет, поищи
   formulas/*/{formula_id}.json.
   Формула не найдена → reject "unknown formula".
4. Прогони проверки промпта агента, определи вердикт.
5. Запиши agent-runtime/reviews/YYYY-MM-DD-<brief_id>-review.json:
   {"brief_id": ..., "verdict": "recommend|revise|reject", "reasons": [...],
    "formula_id": ..., "checked_at": ...}.
6. Обнови таблицу (review-JSON из шага 5 уже записан — auto-approve его читает):
   - вердикт recommend → `.venv/bin/python -m cf auto-approve <brief_id> --review
     agent-runtime/reviews/YYYY-MM-DD-<brief_id>-review.json`. Команда сама проверит:
     формула в approved-индексе, промпт ниши активен, кап (5 за 7 дней). Она либо
     одобрит бриф (авто), либо оставит pending и напечатает причину — её решение
     НЕ оспаривай и НЕ подменяй своим set-review; в сводке оператору покажи её
     вывод дословно;
   - вердикт revise → `.venv/bin/python -m cf set-review --status revised --brief-id <brief_id> --reason-code <code> --notes "<что поправить>"`;
   - вердикт reject → `.venv/bin/python -m cf set-review --status rejected --brief-id <brief_id> --reason-code <code> --notes "<деталь>"`.
   `--reason-code <code>` — код причины из enum (reference_mismatch, female_reference,
   prohibition_violation, weak_hook, not_producible, other); он группирует повторы для
   Prompt Optimizer. Конкретику (URL, что поправить) — в `--notes` и в reasons ревью-файла.
   ВАЖНО: `--status <вердикт>` — всегда ПЕРВЫЙ флаг после `set-review`. Порядок флагов
   значим для allowlist: авто-разрешены только `--status revised`/`--status rejected`
   первым флагом (approved агент не ставит никогда, см. brief-reviewer.md). Осечка
   порядка не опасна — команда просто уйдёт в запрос подтверждения, бриф останется
   pending.
7. Залогируй: `.venv/bin/python -m cf log-run --agent brief-reviewer --status success
   --input "<brief_id или pending:N>" --outputs <review-файлы>`.
8. Покажи оператору вердикты и причины по каждому брифу.
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
