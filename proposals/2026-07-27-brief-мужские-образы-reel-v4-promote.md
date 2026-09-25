---
status: proposed
prompt_id: brief-мужские-образы-reel
created: 2026-07-27
---

# Proposal: тема мужские-образы — перевести кандидата A/B v4 в активную версию

> **ВНИМАНИЕ: evidence синтетический. Применение блокировано.**
> Все цифры ниже получены на демо-данных прогона `demo-eval-loop` 2026-07-27
> (35 фиктивных роликов `DEMO-*`, 37 фиктивных замеров), которые стёрты в той же
> сессии. Proposal написан, чтобы замкнуть петлю обратной связи до последнего
> звена и проверить, что A/B-вердикт вообще доходит до предложения правки
> промпта. Решение принимать НЕЛЬЗЯ: реальных публикаций в CF Published Reels на
> 2026-07-27 — ноль. Дождаться первого настоящего окна замеров и пересчитать.

## Current version

Активна **v2** (`prompts/briefs/мужские-образы/reel.md`, строка CF Prompt Versions
`brief-мужские-образы-reel` / `active: TRUE`). Кандидат A/B — **v4**
(`prompts/briefs/мужские-образы/reel-v4.md`, `active: CANDIDATE`, поставлен
27.07 через `cf apply-prompt-candidate`); `cf.abtest.interleave_plan` чередует их
между брифами темы. v3 снята (`active: FALSE`) — оборванный текст.

Вердикт по паре версий не выносился ни разу: до 27.07 в CF Performance не было
ни одной строки.

## Proposed changes

Правка не в тексте промпта, а в том, какая версия работает: кандидат забирает
роль активной, прежняя активная уходит в архив.

```diff
 CF Prompt Versions, prompt_id = brief-мужские-образы-reel
-  v2  active: TRUE        prompts/briefs/мужские-образы/reel.md
-  v4  active: CANDIDATE   prompts/briefs/мужские-образы/reel-v4.md
+  v2  active: FALSE       prompts/briefs/мужские-образы/reel.md
+  v4  active: TRUE        prompts/briefs/мужские-образы/reel-v4.md
```

Механика — штатная, отдельного кода не требует:

```
.venv/bin/python -m cf log-prompt-version --prompt-id brief-мужские-образы-reel \
  --version v4 --active --github-path prompts/briefs/мужские-образы/reel-v4.md \
  --changelog "v4 выиграл A/B у v2 по eval <дата>: медиана <…> против <…>"
```

## Evidence

Все ссылки — на артефакты демо-прогона, не на боевые данные.

- A/B в параллельной когорте (`agent-runtime/evals/2026-07-27-eval-dataset.json`,
  `cohorts_by_formula["short-styling-idea-reel"].parallel_groups[0]`):
  `verdict_gate: ok`, по 5 замеренных роликов на версию, окна публикации
  пересекаются — v2 [2026-07-08, 2026-07-16], v4 [2026-07-09, 2026-07-17].
- Разница (`by_prompt_version`): `short-styling-idea-reel:v4` median_views 14600.0,
  avg_er 0.09366 против `short-styling-idea-reel:v2` median_views 5100.0,
  avg_er 0.06858 — 2.86× по медиане просмотров, +36.6% по ER.
- Разделение полное, а не хвостовое (`agent-runtime/evals/2026-07-27-weekly-eval.json`,
  `success_criteria.prompt_performance_link`): худший ролик v4 (DEMO-ssir-02, 12400)
  выше лучшего ролика v2 (DEMO-ssir-09, 6300).
- Связка performance → prompt_version без дыр: `join_health.unknown_version_share`
  0.0, `briefs_not_found` 0, статус датасета `ok` (35 роликов после дедупа P5.8).
- Рецепт, на котором стоит когорта, в производстве и не под гвардией:
  `formulas/_approved/index.json`, `short-styling-idea-reel` v4,
  `own_performance.median_views` 9350.0 на 10 роликах.

## Confidence

**low.** Статистика внутри демо-датасета безупречна (полное разделение выборок,
обе когорты выше порога, окна пересекаются), но сами цифры выдуманы: они
проверяют механику eval, а не поведение аудитории. Уверенность станет `medium`
только на первом окне настоящих замеров той же формы — 5 роликов на версию с
чередованием дат.

## Risks

- **Главный: применить это решение по демо-цифрам.** Активная версия промпта
  сменилась бы по данным, которых не было; ловится тем, что proposal остаётся
  `status: proposed` и что демо-строки стёрты (`cf demo-wipe`).
- Кандидат v4 покрывает пять рецептов темы, активная v2 написана под один
  (`short-styling-idea-reel`). Часть выигрыша в когорте может объясняться не
  качеством текста, а тем, что v4 попадает в рецепт точнее. На реальных данных
  проверяется сравнением по каждому рецепту темы отдельно, а не общей медианой.
- После смены активной версии A/B по этой паре закрывается: сравнивать станет не
  с чем, пока не появится следующий кандидат. Заметим в следующем eval по тому,
  что `comparable_pairs` темы опустеет.
