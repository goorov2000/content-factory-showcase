---
name: cf-review-formula
description: "Formula Reviewer - ревью черновика рецепта перед машинным одобрением"
---

<!-- сгенерировано `cf codex-sync` из .claude/commands/cf-review-formula.md — не править руками -->

Codex-обёртка команды завода `cf-review-formula`. Канонический текст команды один для всех
агентов — `.claude/commands/cf-review-formula.md`; здесь он не дублируется, чтобы не плодить
источник дрейфа.

Порядок:
1. Прочитай `.claude/commands/cf-review-formula.md` и выполни его инструкции от корня репозитория.
2. `$ARGUMENTS` в тексте команды — аргументы, переданные этому скиллу (подсказка формата: <путь к formulas/тема/имя.json> | --proposed).
3. CLI завода — `.venv/bin/python -m cf ...`; правила — AGENTS.md.
