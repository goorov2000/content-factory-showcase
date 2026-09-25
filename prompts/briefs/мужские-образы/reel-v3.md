# Промпт генерации брифов — мужские-образы / reel (v3)

Ты генерируешь бриф на короткий вертикальный ролик по ОДНОМУ из утверждённых
рецептов темы. Сначала выбирается рецепт — всё остальное выводится из его снапшота.

## Рецепты темы и выбор между ними
Проверяй условия сверху вниз, первое совпадение забирает бриф:

1. **`grwm-interactive-frame` v2** — материал показывает сборы с обращением к
   зрителю (вопрос, выбор, реакция).
   Снапшот: `formulas/_approved/мужские-образы/grwm-interactive-frame-v2.json`.
2. **`reference-recreation-fit` v1** — есть конкретный референс-образ, который
   пересобирается из доступных вещей.
   Снапшот: `formulas/_approved/мужские-образы/reference-recreation-fit-v1.json`.
3. **`style-manifesto-statement` v2** — заявление о принципе стиля, а не разбор
   конкретной вещи.
   Снапшот: `formulas/_approved/мужские-образы/style-manifesto-statement-v2.json`.
4. **`short-styling-idea-reel` v4** — базовый случай: короткая идея по стилю.
   Снапшот: `formulas/_approved/мужские-образы/short-styling-idea-reel-v4.json`.

`formulas/мужские-образы/` не читаешь — рабочий черновик может быть новее
утверждённого, и бриф по нему не прослеживается до решения оператора.

## Вход
Снапшот выбранного рецепта из `formulas/_approved/мужские-образы/`
(hook_structure, solution_structure, visual_requirements, cta_type, prohibitions,
evidence).

## Задача
Сгенерируй бриф-JSON по schemas/brief.schema.json:
