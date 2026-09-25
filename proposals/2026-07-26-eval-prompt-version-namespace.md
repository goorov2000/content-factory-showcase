---
status: proposed
prompt_id: eval-agent
created: 2026-07-26
---
# Proposal: ключи by_prompt_version стали «промпт + версия» — научить eval-агента их читать

## Current version
- `prompts/agents/eval-agent.md:55-58` — «Замеренные reels = `reels` версии в
  `by_prompt_version`»: агент считает ключ этого агрегата ВЕРСИЕЙ («v2»).
- `prompts/agents/eval-agent.md:14-16` — «by_prompt_version: из датасета + вывод по
  каждой версии (лучше/хуже и почему)».
- `prompts/agents/eval-agent.md:31` — «Underperforming = views < 0.5 x медианы своей
  prompt_version в датасете».

## Proposed changes

```diff
-- by_prompt_version: из датасета + вывод по каждой версии (лучше/хуже и почему —
-  или insufficient_data по порогу per-version, см. Правила).
+- by_prompt_version: из датасета + вывод по каждой версии (лучше/хуже и почему —
+  или insufficient_data по порогу per-version, см. Правила). Ключ ведра —
+  «<промпт>:<версия>» (`brief-мужские-образы-reel:v2`, при отсутствии prompt_id в
+  строке — `<formula_id>:<версия>`): голая версия не идентифицирует промпт, одну и ту
+  же «v2» пишут брифы разных тем. Ключ без двоеточия («v2», «unknown») — ведро без
+  пространства имён: вердикт по нему не выноси, смотри warnings датасета.
-  Замеренные reels = `reels` версии в by_prompt_version (строки
-  датасета уже дедуплицированы P5.8: один reel_id — ближайший к 7-му дню).
+  Замеренные reels = `reels` НУЖНОГО КЛЮЧА в by_prompt_version (строки датасета уже
+  дедуплицированы P5.8: один reel_id — ближайший к 7-му дню); строка датасета несёт
+  своё ведро в поле `prompt_key`, а `cohorts_by_formula` по-прежнему говорит голыми
+  версиями внутри формулы.
-Underperforming = views < 0.5 x медианы своей prompt_version в датасете.
+Underperforming = views < 0.5 x медианы своего `prompt_key` в датасете (медиана
+«своей версии» без пространства имён смешивала бы разные темы).
```

## Evidence
- Дефект: `src/cf/evalprep.py:128` агрегировал по голой строке версии, а
  `schemas/brief.schema.json:12` требует от брифа только `prompt_version` вида «v1»
  без имени промпта; `src/cf/publish.py:87` копирует это значение в строку рила.
  В CF Creative Briefs на 26.07.2026 — 42 строки: 26 с «v2» (рецепты
  `short-styling-idea-reel`, `grwm-interactive-frame`, `reference-recreation-fit`,
  `style-manifesto-statement` — тема мужские-образы) и 16 с «v1» (`expert-item-breakdown`,
  `mass-brand-hot-take`, `store-native-skit`, `brand-list-quick-positioning` — тема
  бренды-магазины, см. `formulas/_approved/index.json`). После первой публикации обеих
  тем их замеры складывались бы в 2 ведра «v1»/«v2» на 8 разных рецептов из 2 тем.
- Колонки `prompt_id` в живых листах нет: заголовки CF Creative Briefs (25 колонок) и
  CF Performance (17 колонок) её не содержат, а `src/cf/sheets.py:331-343` поля вне
  заголовков выбрасывает МОЛЧА — поэтому пространство имён берётся из `formula_id`
  (рецепт принадлежит ровно 1 теме) и автоматически станет `prompt_id`, как только
  колонка появится.
- Фикс и приёмка: `src/cf/evalprep.py:_prompt_key`, тесты
  `tests/test_evalprep.py::test_same_version_of_two_themes_does_not_merge` и
  `::test_prompt_id_is_preferred_namespace_and_unites_formulas` (37 тестов файла зелёные).

## Confidence
high — расхождение промпта и датасета проверяемо: ключи в
`agent-runtime/evals/<дата>-eval-dataset.json` теперь содержат двоеточие, и агент,
читающий их как версии, напишет вердикт по неверной метке.

## Risks
Низкие: правка только текстовая, поведение кода уже изменено и покрыто тестами. Без
неё агент назовёт «версией» строку вида `short-styling-idea-reel:v2` — вердикты
останутся арифметически верными, но метки в отчёте и в
`.claude/memory/evaluations/*` будут читаться неправильно. Применение — руками
оператора (deny-правила запрещают агентам писать в `prompts/agents/**`) +
`cf log-prompt-version` при необходимости.
