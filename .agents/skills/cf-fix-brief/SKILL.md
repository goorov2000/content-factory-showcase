---
name: cf-fix-brief
description: "Brief Fixer - переписать забракованный сценарий по замечаниям ревьюера"
---

<!-- сгенерировано `cf codex-sync` из .claude/commands/cf-fix-brief.md — не править руками -->

Codex-обёртка команды завода `cf-fix-brief`. Канонический текст команды один для всех
агентов — `.claude/commands/cf-fix-brief.md`; здесь он не дублируется, чтобы не плодить
источник дрейфа.

Порядок:
1. Прочитай `.claude/commands/cf-fix-brief.md` и выполни его инструкции от корня репозитория.
2. `$ARGUMENTS` в тексте команды — аргументы, переданные этому скиллу (подсказка формата: <brief_id> | --revised).
3. CLI завода — `.venv/bin/python -m cf ...`; правила — AGENTS.md.
