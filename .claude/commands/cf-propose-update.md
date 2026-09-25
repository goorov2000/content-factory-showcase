---
description: Prompt Optimizer - evidence-backed proposal изменения промпта
argument-hint: "<prompt_id>"
---
Следуй промпту prompts/agents/prompt-optimizer.md. Аргументы: $ARGUMENTS

Порядок:
1. Собери evidence:
   - свежий agent-runtime/evals/*-weekly-eval.json (нет → предложи сначала /cf-eval);
   - `.venv/bin/python -m cf rejection-history`;
   - активная версия: `.venv/bin/python -m cf read prompt_versions` (active=TRUE → github_path);
   - текущий текст промпта по github_path.
2. Evidence мало → `.venv/bin/python -m cf log-run --agent prompt-optimizer --status insufficient_data`
   и остановись, объяснив, каких данных ждать.
3. Напиши proposals/YYYY-MM-DD-<prompt_id>.md по шаблону из промпта агента.
4. Проверь: `.venv/bin/python -m cf validate proposal proposals/<файл>`; исправь ошибки.
5. `.venv/bin/python -m cf log-run --agent prompt-optimizer --status success --outputs <файл>`.
6. Покажи proposal оператору и ЖДИ явного решения. Менять промпт до решения запрещено.

После решения оператора:
- **approve** — это ЗАПУСК A/B, а не мгновенная замена (P5.13: одна активная версия
  давала бы before/after со смешанными факторами):
  1. Текст с Proposed changes положи в ОТДЕЛЬНЫЙ файл рядом с активным (например
     `reel-v{N+1}.md`). Активный файл НЕ трогай — обе версии идут параллельно.
     Применять правку к тому же файлу нельзя: A/B двух идентичных файлов = вердикт по шуму.
  2. В frontmatter proposal — status: approved и applied: YYYY-MM-DD.
  3. Зарегистрируй кандидата (НЕ активируя):
     `.venv/bin/python -m cf log-prompt-version --prompt-id <id> --version <vN+1>
     --path <путь-нового-файла> --changelog "<суть>" --candidate`
     (гард откажет, если путь совпадёт с активным — файл кандидата обязан быть отдельным).
     brief-generator начнёт чередовать active/candidate, eval соберёт параллельные когорты.
  4. Запиши решение в .claude/memory/decisions/YYYY-MM-DD-prompt-<id>.md (+ MEMORY.md);
     `git add <новый файл> <proposal>` и commit "prompt(<id>): candidate <суть> [vN+1]".
  - **После вердикта weekly-eval:** победил кандидат (verdict_gate=ok, лучше active) →
    активируй его `.venv/bin/python -m cf log-prompt-version --prompt-id <id> --version <vN+1>
    --path <путь-кандидата> --changelog "победа A/B <дата-eval>"` (БЕЗ --candidate — гасит
    прежний active). Победил active или insufficient_data → кандидата оставь добирать
    данные или сними, зарегистрировав следующего. Файл проигравшего остаётся историей —
    не удаляй.
- **reject**: в frontmatter — status: rejected и reason: "<причина оператора>";
  запиши решение в память; файл промпта НЕ трогать.
Перед началом работы запомни время старта (`date -u +%Y-%m-%dT%H:%M:%S+00:00`) и передай его в log-run флагом `--started-at <запомненное время>` — иначе длительность прогона в CF Run Log будет нулевой.
