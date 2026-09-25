---
status: approved
applied: 2026-07-10
prompt_id: brief-мужские-образы-reel
created: 2026-07-10
---
# Proposal: обязательный производственный блок в visual_direction брифа

## Current version
Версия v1, файл `prompts/briefs/мужские-образы/reel.md` (активная строка CF Prompt Versions).

## Proposed changes
```diff
 - **visual_direction** — как снимать одним креатором на телефон: свет, план,
   локация, смена образа каждые 2–4с, текст-плашка с формулировкой идеи, 9:16.
+  В конце visual_direction добавь блок «Обязательно: текст-плашка с идеей на
+  каждом образе; публикация в окно 9:00–21:00». Бриф без этого блока не отдавать.
```

## Evidence
- Rejection reasons: нет — 0 записей в agent-runtime/reviews/rejection_history.json
  (ревью прошёл 1 бриф, approved).
- Performance (agent-runtime/evals/2026-07-10-weekly-eval.json, данные dryrun):
  r-dryrun-003 снят без текст-плашки, опубликован 21:30 → 1 150 views / ER 0.0339
  против 61 500 / ER 0.1447 у r-dryrun-002, точно следовавшего брифу (×53 по views).
  Attribution: production_failure, второй кандидат publishing_failure.
- Формула formulas/мужские-образы/short-styling-idea-reel.json: «текст-плашка с
  формулировкой идеи» уже в visual_requirements — правка переносит требование в каждый
  бриф как явный чек-лист, снижая шанс потери при производстве
  (паттерн agent-runtime/patterns/2026-07-10-мужские-образы-patterns.json,
  muzhskie-obrazy-styling-idea-hook-01).

## Confidence
medium — атрибуция построена на одном underperformer из синтетического dryrun-набора;
механизм (плашка = носитель идеи-хука) согласуется с паттерном styling-idea-hook-01.

## Risks
visual_direction удлиняется — если креаторы перестанут его дочитывать, вырастет доля
production_failure атрибуций в следующем eval (метрика: attribution по категориям).
Если провалы были вызваны постингом, а не плашкой, увидим это по publishing_failure
у роликов с плашкой, опубликованных поздно.
