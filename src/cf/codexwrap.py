"""Codex-обёртки слэш-команд: .claude/commands/<имя>.md → .agents/skills/<имя>/.

Codex не читает .claude/commands/** (его custom prompts удалены в CLI ≥0.117),
официальный репо-носитель команд — скиллы стандарта Agent Skills в .agents/skills/,
которые Codex сканирует нативно. Канон остаётся один — текст команды: обёртка
не дублирует его, а отсылает к файлу команды, поэтому дрейфовать нечему.
Дублируется только description (frontmatter скилла обязателен) — за его свежесть
отвечает перегенерация: `cf codex-sync` (и install-agent/apply-agent-edit сами).

allow_implicit_invocation: false во всех обёртках: конвейерные команды зовутся
только явно ($cf-…) — платный вызов по неявному совпадению описания это чужое
решение вместо оператора (тот же принцип, что «воркер с пустой очередью платного
вызова не делает»).

Обёртки помечены маркером MARKER: sync управляет ТОЛЬКО каталогами с маркером и
не тронет скиллпак mattpocock/skills или рукописные скиллы рядом.
"""
import json
import re
from pathlib import Path

COMMANDS_DIR = ".claude/commands"
SKILLS_DIR = ".agents/skills"
MARKER = "сгенерировано `cf codex-sync`"
# Agent Skills spec: name — lowercase alnum + дефисы, ≤64, равен имени каталога.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_DESCRIPTION = 1024


class CodexWrapError(Exception):
    """Отказ генерации с человеческой причиной."""


def _frontmatter(text):
    """dict полей из YAML-frontmatter команды (без зависимости от pyyaml:
    поля команд — плоские строки, этого достаточно)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip().strip('"')
    return {}          # frontmatter не закрыт — считаем, что его нет


def _description(name, text):
    """Description обёртки: frontmatter команды, иначе первая непустая строка тела."""
    desc = _frontmatter(text).get("description", "")
    if not desc:
        body = re.sub(r"(?s)^---.*?---", "", text)
        desc = next((l.strip() for l in body.splitlines() if l.strip()), "")
    if not desc:
        raise CodexWrapError(f"{name}: не из чего собрать description обёртки")
    return desc[:_MAX_DESCRIPTION]


def validate_name(name):
    """Имя команды пригодно как имя скилла (иначе Codex её просто не увидит)."""
    if not _SKILL_NAME_RE.fullmatch(name):
        raise CodexWrapError(
            f"«{name}» не годится как имя Codex-скилла: только [a-z0-9-], "
            f"первый символ буква/цифра, до 64 знаков")


def wrapper_files(name, command_text):
    """[(путь относительно корня, содержимое)] обёртки команды <name>."""
    validate_name(name)
    desc = _description(name, command_text)
    hint = _frontmatter(command_text).get("argument-hint", "")
    hint_line = f" (подсказка формата: {hint})" if hint else ""
    skill = f"""---
name: {name}
description: {json.dumps(desc, ensure_ascii=False)}
---

<!-- {MARKER} из {COMMANDS_DIR}/{name}.md — не править руками -->

Codex-обёртка команды завода `{name}`. Канонический текст команды один для всех
агентов — `{COMMANDS_DIR}/{name}.md`; здесь он не дублируется, чтобы не плодить
источник дрейфа.

Порядок:
1. Прочитай `{COMMANDS_DIR}/{name}.md` и выполни его инструкции от корня репозитория.
2. `$ARGUMENTS` в тексте команды — аргументы, переданные этому скиллу{hint_line}.
3. CLI завода — `.venv/bin/python -m cf ...`; правила — AGENTS.md.
"""
    display = desc.split(" - ")[0].strip()[:64] or name
    openai = f"""# {MARKER} — не править руками
interface:
  display_name: {json.dumps(display, ensure_ascii=False)}
  short_description: {json.dumps(desc, ensure_ascii=False)}
policy:
  allow_implicit_invocation: false
"""
    base = f"{SKILLS_DIR}/{name}"
    return [(f"{base}/SKILL.md", skill), (f"{base}/agents/openai.yaml", openai)]


def check_collision(root, name):
    """Имя не занято рукописным (без маркера) скиллом — иначе запись обёртки
    молча уничтожила бы чужой SKILL.md, а последующая чистка сирот снесла бы
    каталог целиком вместе с references/."""
    skill_md = Path(root) / SKILLS_DIR / name / "SKILL.md"
    if skill_md.is_file() and MARKER not in skill_md.read_text(encoding="utf-8"):
        raise CodexWrapError(
            f"«{name}»: каталог {SKILLS_DIR}/{name} занят рукописным скиллом "
            f"(SKILL.md без маркера генерата) — переименуйте команду")


def write_wrapper(root, name, command_text):
    """Записать обёртку команды. Возврат: список записанных путей."""
    check_collision(root, name)
    written = []
    for rel, content in wrapper_files(name, command_text):
        target = Path(root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(rel)
    return written


def _generated_dirs(root):
    """Каталоги .agents/skills/, чей SKILL.md несёт маркер генерата."""
    out = []
    skills = Path(root) / SKILLS_DIR
    if not skills.is_dir():
        return out
    for entry in sorted(skills.iterdir()):
        skill_md = entry / "SKILL.md"
        if not (entry.is_dir() and skill_md.is_file()):
            continue
        try:
            if MARKER in skill_md.read_text(encoding="utf-8"):
                out.append(entry.name)
        except OSError:
            continue           # нечитаемый SKILL.md — не наш генерат
    return out


def sync(root, write=True):
    """Сверить/перегенерировать все обёртки. Возврат:
    {"updated": [...], "unchanged": [...], "orphaned": [...], "invalid": [...]}
    — orphaned это генерат без живой команды (write=True его удаляет), invalid —
    команды, обёртку которых собрать нельзя (кривое имя, коллизия с рукописным
    скиллом): одна такая команда не роняет сверку остальных."""
    root = Path(root)
    commands = sorted((root / COMMANDS_DIR).glob("*.md"))
    updated, unchanged, invalid = [], [], []
    for cmd in commands:
        name = cmd.stem
        try:
            check_collision(root, name)
            files = wrapper_files(name, cmd.read_text(encoding="utf-8"))
        except CodexWrapError as exc:
            invalid.append(str(exc))
            continue
        dirty = [(rel, body) for rel, body in files
                 if not (root / rel).is_file()
                 or (root / rel).read_text(encoding="utf-8") != body]
        if not dirty:
            unchanged.append(name)
            continue
        updated.append(name)
        if write:
            for rel, body in dirty:
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8", newline="\n")

    names = {c.stem for c in commands}
    orphaned = [d for d in _generated_dirs(root) if d not in names]
    if write:
        import shutil
        for name in orphaned:
            shutil.rmtree(root / SKILLS_DIR / name)
    return {"updated": updated, "unchanged": unchanged,
            "orphaned": orphaned, "invalid": invalid}
