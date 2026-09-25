# Работа в CF из Codex — с чего начать

Одна страница для сессии в Codex (`gpt-6-astra`). Как устроен слой совместимости и почему так —
[codex.md](codex.md); здесь только вход в работу. Сверено с машиной 14.09.2026.

## Запуск

- Терминал: `codex` из корня `~/projects/CF` (или панель расширения в VS Code). Модель, усилие и тариф
  заданы глобально в `~/.codex/config.toml`: `gpt-6-astra`, `ultra`, `priority`.
- Проект уже trusted, guard защищённых путей уже доверен по хэшу. Если скрипт guard'а
  (`.codex/hooks/guard_protected_paths.py`) или `.codex/hooks.json` изменится — Codex попросит доверить
  его заново командой `/hooks`.
- `codex doctor` на 14.09: 19 ok, 0 предупреждений. `codex` и `rg` в `~/.local/bin` — скрипты,
  которые берут свежайшую версию расширения в момент вызова: до 14.09 там был симлинк на
  удалённую версию, и `codex` из терминала не запускался, а `rg` не было вовсе.
- На этой машине 4 ядра: больше двух подагентов одновременно ставить бессмысленно.
- **Песочница шелла.** Ubuntu 24.04 запрещает непривилегированные user namespaces
  (`kernel.apparmor_restrict_unprivileged_userns = 1`), и встроенный в Codex `bwrap` падал с
  `setting up uid map: Permission denied` — в режиме workspace-write не выполнялась ни одна команда шелла.
  Починено 14.09: `apt install bubblewrap` плюс профиль AppArmor `/etc/apparmor.d/bwrap`
  (`userns` только для `/usr/bin/bwrap`). Если после переустановки системы шелл снова падает — это оно.
- **Проверено 14.09 живым прогоном `codex exec`:** шелл работает (`cf --help`, `rg`); правка файла в
  `prompts/agents/` через `apply_patch` отклоняется хуком; `git push` требует подтверждения (правило
  `.codex/rules/cf.rules` загружается); **запись в `prompts/agents/` через шелл проходит** — guard смотрит
  только инструменты правки файлов, дальше граница держится на правиле №3.

## Что Codex видит и чего не видит

| Видит | Не видит |
| --- | --- |
| `AGENTS.md` (канон правил, до 32 КБ, сейчас 21 КБ) | `.claude/skills/**` — только через обёртки в `.agents/skills/` |
| `.agents/skills/*` — команды завода `$cf-*`, скиллпак mattpocock | авто-память Claude Code (`~/.claude/projects/.../memory/`) — ориентир в [README.md](README.md) |
| `.codex/` — сеть в песочнице, guard путей, правило на `git push` | инструмент Artifact, Workflow и MCP playwright Claude Code |

Подагентов (`spawn_agent`) можно звать для батчей и параллельного разбора — это разрешено в `AGENTS.md`.

## Команды и скиллы

- CLI завода: `.venv/bin/python -m cf <команда>`; перед запуском прочитать абзац команды в
  [cf-cli.md](cf-cli.md). Сеть в песочнице включена, секреты — `~/.cf/secrets/` (права 600), ключи
  прочих интеграций — `secrets/` в репо (в `.gitignore`).
- Скиллы вызываются **только явно**: `$cf-status`, `$cf-niche-run …`. Конвейерные
  `$cf-*` — платные прогоны.
- Тесты: `.venv/bin/python -m pytest -q` (2220 штук, ~2,5 мин).

## Что нельзя

- Писать напрямую в `prompts/agents/**`, `prompts/briefs/**`, `.claude/commands/**`,
  `.claude/settings*.json`, `formulas/_approved/**`. Guard ловит правку файлов (`apply_patch`), но не
  запись через шелл (`echo > …`, `python -c`): граница держится на инструкции, а не на песочнице.
  Правка промптов и команд идёт через аппликаторы `cf apply-brief-prompt`, `cf apply-agent-edit` (правило №3).
- `git push` без явного подтверждения владельца (правило №3). Коммитить можно: Codex уже писал в `.git`
  (`.git/refs/codex/turn-diffs`), хотя [codex.md](codex.md) называет `.git` в песочнице read-only —
  если коммит откажет, его делает владелец.
- Это боевой VPS: не перезапускать сервисы, не гонять `cf collect` (платный Apify), не трогать
  `~/.cf/secrets`. `sudo` под паролем — root-действия делает только владелец.

## Звенья, которые остаются за Claude Code

Codex их запускает, но внутри они зовут бинарь `claude` (он установлен, `~/.local/bin/claude`):
кнопка ▶ и фан-аут ниш на дашборде (`src/cf/dashboard/runner.py`), разбор медиа `cf vision`
(`vision.engine = headless-subscription`) и судья найма (`src/cf/hiring/judge.py`).
Платит подписка Claude, а не Codex.

## Сеть

VPS стоит в европейском датацентре, и часть российских сайтов режет его по IP (капчи, 429).
Для скрейпинга при необходимости поднимался SOCKS-туннель через машину в России; прокси
ставится только на скрейпинг (`HTTPS_PROXY=socks5h://127.0.0.1:1080` для requests), не на весь VPS.

## Где продолжать

Карта документов — [README.md](README.md). Каждый прогон агента — строка в Run Log:
`.venv/bin/python -m cf log-run --agent <имя> --status success --started-at <ISO8601> …`.
