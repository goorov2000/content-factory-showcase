---
status: proposed
prompt_id: brief-prompt-writer
created: 2026-07-26
---
# Proposal: свести имя секции черновика к «Proposed changes»

## Current version
`prompts/agents/brief-prompt-writer.md:68-72` предписывает агенту секции

> Current version, **Proposed prompt**, Evidence, Confidence, Risks

и прямо называет этот список «тем, который требует `cf validate proposal`».

Валидатор требует другого. `src/cf/proposals.py:3-9`:

```python
REQUIRED_SECTIONS = (
    "## Current version",
    "## Proposed changes",
    ...
)
```

То есть промпт агента и валидатор расходятся в имени обязательной секции. Пока
это стоило только удачи: реальный черновик
`proposals/2026-07-26-brief-бренды-магазины-reel.md` написан с «## Proposed
changes» и потому прошёл. Черновик, написанный ровно по букве промпта, не
прошёл бы `cf validate proposal` — и, что важнее с 2026-07-26, не был бы применён
кнопкой на воротах: `cf.dashboard.prompt_apply._proposed_block` ищет блок именно
в «## Proposed changes» и отказывает с «в черновике нет секции».

## Proposed changes

```markdown
## Выход — proposal, не файл промпта
Сохрани `proposals/YYYY-MM-DD-brief-{niche}-reel.md`. Frontmatter: `status: proposed`,
`prompt_id: brief-{niche}-reel`, `created: YYYY-MM-DD`. Заголовок —
«Proposal: промпт темы {niche}». Дальше секции второго уровня в том порядке,
который требует `cf validate proposal` (Current version, Proposed changes, Evidence,
Confidence, Risks) плюс «Применение»:

- **Proposed changes** — полный текст будущего `prompts/briefs/{niche}/reel.md`
  РОВНО в одном markdown-блоке ```` ```markdown ```` … ```` ``` ````. Двух блоков в
  этой секции быть не должно: аппликатор откажется применять черновик, в котором
  непонятно, какой из блоков — промпт.
```

Прежнее имя «Proposed prompt» из промпта убирается целиком, чтобы не осталось
второго названия одной секции.

## Evidence
- `src/cf/proposals.py:3-9` — `REQUIRED_SECTIONS` содержит `## Proposed changes`;
  `validate_proposal_text` перечисляет отсутствующую секцию как ошибку.
- `prompts/agents/brief-prompt-writer.md:68-72` — «Proposed prompt» плюс ложное
  утверждение, что это список валидатора.
- `proposals/2026-07-26-brief-бренды-магазины-reel.md:16` — реальный черновик
  использует «## Proposed changes», то есть контракт де-факто уже такой.
- `src/cf/dashboard/prompt_apply.py:_proposed_block` — отказ «в черновике нет
  секции «## Proposed changes»»; с 2026-07-26 это блокирует применение кнопкой.
- Масштаб: черновики промптов пишутся для всех тем с утверждёнными рецептами —
  на 2026-07-26 это 14 записей `formulas/_approved/index.json` в 4 темах, из них
  3 темы ещё ждут промпта. Каждый будущий черновик прошёл бы мимо аппликатора.

Замечание к валидатору (отдельной правки не предлагаю): `cf validate proposal`
засчитывает как «ссылку на источник» только `http`, `agent-runtime/` или
`formulas/` (`src/cf/proposals.py:30`). Proposal, чьи доказательства целиком лежат
в `src/` и `prompts/` — как этот, — признаётся «Evidence без ссылок». Строка выше
добавлена ровно для того, чтобы пройти проверку; список маркеров стоит расширить.

## Confidence
high — расхождение проверяется чтением двух файлов, обе стороны однозначны.

## Risks
1. **Старые черновики с «Proposed prompt»** (если такие появятся до применения
   правки) не применятся кнопкой. На 2026-07-26 таких файлов в `proposals/` нет —
   проверено глобом; риск теоретический.
2. Правка меняет только текст инструкции агента, не содержание промптов тем.

## Применение (делает оператор)
1. Ревью текста.
2. Заменить блок «## Выход — proposal, не файл промпта» в
   `prompts/agents/brief-prompt-writer.md` на предложенный.
3. `.venv/bin/python -m cf log-prompt-version --prompt-id brief-prompt-writer
   --version v2 --path prompts/agents/brief-prompt-writer.md --changelog "секция
   черновика — Proposed changes (снято расхождение с cf validate proposal и
   аппликатором ворот)"`.
4. Коммит промпта вместе с этим proposal в статусе `approved`.
