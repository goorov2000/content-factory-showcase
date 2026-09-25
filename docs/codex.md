# Codex в Content Factory

Слой совместимости добавлен 2026-07-30. Репозиторий говорит на открытом
стандарте agents.md/Agent Skills, поэтому Codex (CLI и IDE-расширение) работает
с заводом теми же правилами и командами, что Claude Code. Факты о поведении
Codex ниже сверены с исходниками openai/codex и официальными доками
(`developers.openai.com/codex/*` → `learn.chatgpt.com/docs/*`) на 2026-07-30.

## Как устроено

| Слой | Claude Code | Codex |
| --- | --- | --- |
| Правила системы | `CLAUDE.md` (симлинк) | `AGENTS.md` (канон, читает нативно) |
| Команды завода | `/cf-<имя>` из `.claude/commands/` (канон) | `$cf-<имя>` из `.agents/skills/cf-*/` (генерат) |
| Скиллпак mattpocock | `.claude/skills/*` (симлинки) | `.agents/skills/*` напрямую |
| Защита prompts/** | deny-правила `.claude/settings.json` | PreToolUse-guard `.codex/hooks.json` |
| Санбокс/сеть | политика харнеса | `.codex/config.toml` (network_access для Sheets) |

- **AGENTS.md — единственный канон правил.** `CLAUDE.md` — симлинк на него
  (официальный паттерн Anthropic: `ln -s AGENTS.md CLAUDE.md`). Codex собирает
  цепочку project-doc с бюджетом 32 KiB и молча обрезает лишнее — тест
  `test_codex_compat.py` держит размер под лимитом.
- **Команды не дублируются.** Канонический текст каждой команды остаётся в
  `.claude/commands/cf-*.md`; Codex-обёртка (`SKILL.md` + `agents/openai.yaml`)
  лишь отсылает к нему. Обёртки — генерат `cf codex-sync`; `install-agent` и
  `apply-agent-edit` перегенерируют их сами. Руками обёртки не правятся —
  правка уедет при первой перегенерации.
- **Неявный вызов выключен** (`allow_implicit_invocation: false`) у всех
  cf-скиллов: конвейерные команды — платные прогоны, они запускаются только
  явным `$cf-<имя>` (тот же принцип, что «воркер с пустой очередью платного
  вызова не делает»).
- **Guard путей.** Санбокс Codex не умеет запрещать запись в отдельные пути
  внутри воркспейса (writable_roots только расширяет), поэтому зеркало
  deny-правил — PreToolUse-хук `.codex/hooks/guard_protected_paths.py`:
  prompts/agents/**, prompts/briefs/**, .claude/commands/**,
  .claude/settings*.json и (сверх зеркала, по правилу иммутабельных снапшотов)
  formulas/_approved/**. Bash-обходы он не ловит — ровно как и deny-правила
  Claude; это инструкционная граница, а не песочница.
- **git push — только с подтверждением** (`.codex/rules/cf.rules`): правило №3,
  auto-push запрещён.

## Приёмка на новой машине (один раз)

1. Доверить проект: Codex спросит сам, либо в `~/.codex/config.toml`:
   `[projects."/путь/к/CF"] trust_level = "trusted"`. Без trusted весь слой
   `.codex/` (config, hooks, rules) молча не грузится.
2. В сессии Codex выполнить `/hooks` и доверить command-хук guard'а — Codex
   требует разового подтверждения на каждый хук по его хэшу (изменится скрипт —
   подтвердить заново).
3. Проверить, что Codex видит правила и скиллы: `/skills` покажет `cf-*`;
   «Summarize the current instructions» перескажет AGENTS.md.
4. После правки скиллов/обёрток — перезапустить сессию Codex: изменения скиллов
   подхватываются на старте.

## Ограничения, о которых стоит знать

- Custom prompts (`~/.codex/prompts`, `/prompts:<имя>`) в текущих CLI удалены —
  потому команды и портированы скиллами, это официальная замена.
- `AGENTS.override.md` (в .gitignore) Codex читает ВМЕСТО `AGENTS.md` — это
  личный локальный оверрайд, не место для постоянных правил.
- Санбокс Codex держит `.git/`, `.agents/`, `.codex/` read-only: обновление
  скиллпака и обёрток из-под Codex невозможно — это делается из Claude Code или
  руками оператора (`cf codex-sync`). По той же причине `cf install-agent` и
  `cf apply-agent-edit` из-под Codex-сессии не запишут обёртку — их запускает
  оператор в обычном терминале.
- `cf codex-sync --check` — детектор дрейфа: код возврата 0 — всё сошлось,
  код 1 — список имён дрейфа/коллизий; тот же инвариант держит
  `tests/test_codex_compat.py`.

## Что где лежит

- `.codex/config.toml` — проектные настройки Codex (сеть в песочнице, фича hooks).
- `.codex/hooks.json` + `.codex/hooks/guard_protected_paths.py` — guard путей.
- `.codex/rules/cf.rules` — правила исполнения команд (git push → prompt).
- `src/cf/codexwrap.py` — генератор обёрток; CLI: `cf codex-sync [--check]`.
- `tests/test_codex_compat.py` — инварианты совместимости.
