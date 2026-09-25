#!/usr/bin/env python3
"""PreToolUse-guard Codex: зеркало deny-правил Claude-харнеса (правило №3).

В Claude Code прямую правку prompts/**, .claude/commands/** и настроек харнеса
останавливают deny-правила .claude/settings.json. У песочницы Codex запрета
записи по произвольному пути нет (writable_roots только добавляет), поэтому ту
же границу держит этот хук: он читает JSON-событие из stdin, достаёт целевые
пути правки (поля путей + заголовки apply_patch «*** … File:») и отвечает
exit 2, если правка метит в защищённый путь. Каталог formulas/_approved/
добавлен сверх зеркала: снапшоты иммутабельны для всех агентов (AGENTS.md).

Только stdlib и системный python3: хук должен работать до создания .venv.
Bash-обходы (echo > файл) хук не ловит — ровно как и deny-правила Claude;
граница здесь инструкционная + ревью коммитов, а не песочница.
"""
import json
import posixpath
import re
import sys

PROTECTED = (
    "prompts/agents/",
    "prompts/briefs/",
    ".claude/commands/",
    ".claude/settings.json",
    ".claude/settings.local.json",
    "formulas/_approved/",
)
# Заголовки файлов внутри тела apply_patch.
_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$",
    re.MULTILINE)
_PATHY_KEYS = {"path", "file_path", "filepath", "target", "notebook_path"}


def _strings(value, key=None):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, key)
    elif isinstance(value, str):
        yield key, value


def _normalize(raw, cwd):
    # normpath (чисто лексический) схлопывает «./», «//» и «..» — иначе
    # prompts/./agents/ или docs/../prompts/agents/ проходили бы мимо префиксов
    p = posixpath.normpath(raw.strip().replace("\\", "/"))
    if cwd:
        c = posixpath.normpath(cwd.replace("\\", "/"))
        if p.startswith(c + "/"):
            p = p[len(c) + 1:]
    return p


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0            # незнакомый формат события — не блокируем всё подряд
    cwd = str(data.get("cwd") or "")
    candidates = set()
    for key, text in _strings(data.get("tool_input")):
        if key and key.lower() in _PATHY_KEYS:
            candidates.add(text)
        for m in _PATCH_FILE_RE.finditer(text):
            candidates.add(m.group(1) or m.group(2))
    # startswith — относительные пути; поиск «/<префикс>» внутри — абсолютные,
    # когда событие пришло без cwd (fail-closed в сторону защищённых имён).
    hits = sorted({p for p in (_normalize(c, cwd) for c in candidates)
                   if p.startswith(PROTECTED)
                   or any(f"/{prot}" in p for prot in PROTECTED)})
    if hits:
        print("запрещено: прямая правка " + ", ".join(hits)
              + " — только через аппликаторы cf (apply-brief-prompt, "
                "install-agent, apply-agent-edit, apply-prompt-candidate) "
                "или proposal (AGENTS.md, правило №3)",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
