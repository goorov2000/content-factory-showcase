"""Инварианты совместимости с Codex (2026-07-30, docs/codex.md).

Канон правил — AGENTS.md (Codex читает его нативно, Claude Code — через симлинк
CLAUDE.md); канон команд — .claude/commands/*.md, а их Codex-обёртки в
.agents/skills/ — генерат `cf codex-sync`. Эти тесты держат три вещи:
(1) канон и генерат не дрейфуют, (2) AGENTS.md не вылезает за 32 KiB-бюджет
project-doc Codex (лишнее он обрезает МОЛЧА), (3) PreToolUse-guard реально
блокирует защищённые пути и не трогает остальные.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cf.agentinstall import AgentInstallError, apply_edit, install
from cf.codexwrap import (CodexWrapError, MARKER, check_collision, sync,
                          validate_name, wrapper_files, write_wrapper)

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / ".codex/hooks/guard_protected_paths.py"
# Бюджет project-doc Codex: DEFAULT_PROJECT_DOC_MAX_BYTES = 32 KiB.
CODEX_DOC_BUDGET = 32 * 1024
# Витринная копия: в индексе git симлинки записаны как 120000, но checkout на Windows без
# прав администратора (или с core.symlinks=false) кладёт их обычными файлами. Проверки
# самих симлинков в таком checkout пропускаются; на Linux/macOS они выполняются.
SYMLINKS_MATERIALIZED = (REPO_ROOT / "CLAUDE.md").is_symlink()
needs_symlinks = pytest.mark.skipif(
    not SYMLINKS_MATERIALIZED,
    reason="checkout без симлинков (Windows без прав администратора / core.symlinks=false)")


# --- инварианты репозитория ---------------------------------------------------

def test_agents_md_is_canon_within_codex_budget():
    agents = REPO_ROOT / "AGENTS.md"
    assert agents.is_file() and not agents.is_symlink()
    assert agents.stat().st_size < CODEX_DOC_BUDGET, (
        "AGENTS.md больше 32 KiB — Codex обрежет его молча")


def test_claude_deny_rules_mirror_the_codex_guard():
    """Правило №3 держат два зеркала: deny-правила Claude Code (.claude/settings.json)
    и PreToolUse-guard Codex. До 14.09.2026 formulas/_approved/** был закрыт только в
    Codex, и расхождение жило незамеченным: сверку не делал ни один тест."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    settings = json.loads((REPO_ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    denied = set()
    for rule in settings["permissions"]["deny"]:
        assert rule.startswith("Edit(") and rule.endswith(")"), rule
        denied.add(rule[len("Edit("):-1].removesuffix("**"))
    assert denied == set(guard.PROTECTED)


@needs_symlinks
def test_claude_md_is_symlink_to_agents_md():
    claude = REPO_ROOT / "CLAUDE.md"
    assert claude.is_symlink(), "CLAUDE.md должен быть симлинком на AGENTS.md"
    assert claude.readlink() == Path("AGENTS.md")
    assert claude.read_text(encoding="utf-8").startswith(
        "# Content Factory — правила системы")


def test_codex_wrappers_do_not_drift():
    report = sync(REPO_ROOT, write=False)
    assert not report["updated"], (
        f"обёртки дрейфуют от канона: {report['updated']} — "
        f"прогони `cf codex-sync`")
    assert not report["orphaned"], (
        f"генерат без живой команды: {report['orphaned']} — "
        f"прогони `cf codex-sync`")
    assert not report["invalid"], f"обёртки не собираются: {report['invalid']}"
    assert report["unchanged"], "ни одной обёртки — codex-sync не прогнан?"


def test_every_command_has_wrapper_dir():
    commands = sorted(p.stem for p in (REPO_ROOT / ".claude/commands").glob("*.md"))
    assert commands, "команды завода пропали?"
    for name in commands:
        skill = REPO_ROOT / ".agents/skills" / name / "SKILL.md"
        assert skill.is_file(), f"у команды {name} нет Codex-обёртки"
        openai = REPO_ROOT / ".agents/skills" / name / "agents/openai.yaml"
        assert "allow_implicit_invocation: false" in openai.read_text(encoding="utf-8")


@needs_symlinks
def test_every_claude_skill_is_visible_to_codex():
    """Codex читает только .agents/skills/. Каждый скилл из .claude/skills/ обязан там
    быть: либо симлинком из .claude/ в .agents/ (как весь скиллпак), либо — если канон
    держит настоящий каталог в .claude/ (например, когда на путь скилла смотрят боевые
    юниты systemd) — обёрткой с тем же именем. Список имён не жёсткий: 14.09.2026 так
    нашлись скилл, которого Codex не видел вовсе, и вторая копия
    discovery-interview вместо симлинка."""
    skills = REPO_ROOT / ".claude/skills"
    for entry in sorted(skills.iterdir()):
        twin = REPO_ROOT / ".agents/skills" / entry.name / "SKILL.md"
        assert twin.is_file(), f"{entry.name}: нет в .agents/skills — Codex его не видит"
        if not entry.is_symlink():
            text = twin.read_text(encoding="utf-8")
            assert f".claude/skills/{entry.name}/SKILL.md" in text, (
                f"{entry.name}: настоящий каталог в .claude/skills — нужна обёртка, "
                f"которая отсылает к канону, а не вторая копия")


@needs_symlinks
@pytest.mark.parametrize("name", ["cf-demo-eval-loop", "cf-ui-rethink"])
def test_project_skills_canonical_in_agents_dir(name):
    """Проектные скиллы лежат в .agents/skills/ (их видит Codex), а в
    .claude/skills/ — симлинки, как у всего скиллпака."""
    canon = REPO_ROOT / ".agents/skills" / name
    link = REPO_ROOT / ".claude/skills" / name
    assert (canon / "SKILL.md").is_file()
    assert link.is_symlink() and (link / "SKILL.md").is_file()
    assert "allow_implicit_invocation: false" in (
        canon / "agents/openai.yaml").read_text(encoding="utf-8")


def test_codex_layer_files_are_consistent():
    hooks = json.loads((REPO_ROOT / ".codex/hooks.json").read_text(encoding="utf-8"))
    [entry] = hooks["hooks"]["PreToolUse"]
    [hook] = entry["hooks"]
    assert hook["type"] == "command"
    # путь скрипта — от корня репо через rev-parse: cwd-относительный путь при
    # сессии из подкаталога давал бы exit 2 «файл не найден» = deny на ВСЁ
    assert "$(git rev-parse --show-toplevel)" in hook["command"]
    assert ".codex/hooks/guard_protected_paths.py" in hook["command"]
    assert GUARD.is_file()
    assert "network_access = true" in (
        REPO_ROOT / ".codex/config.toml").read_text(encoding="utf-8")
    assert "git" in (REPO_ROOT / ".codex/rules/cf.rules").read_text(encoding="utf-8")


# --- генератор обёрток --------------------------------------------------------

CMD = """---
description: Test Agent - проверка обёртки
argument-hint: "[x] [--y]"
---
Следуй промпту prompts/agents/test-agent.md. Аргументы: $ARGUMENTS
"""


def test_wrapper_files_from_frontmatter():
    files = dict(wrapper_files("cf-test", CMD))
    skill = files[".agents/skills/cf-test/SKILL.md"]
    assert "name: cf-test" in skill
    assert '"Test Agent - проверка обёртки"' in skill
    assert ".claude/commands/cf-test.md" in skill
    assert "[x] [--y]" in skill
    assert MARKER in skill
    openai = files[".agents/skills/cf-test/agents/openai.yaml"]
    assert 'display_name: "Test Agent"' in openai
    assert "allow_implicit_invocation: false" in openai


def test_wrapper_description_falls_back_to_first_body_line():
    files = dict(wrapper_files("cf-bare", "Просто тело команды.\nВторая строка."))
    assert '"Просто тело команды."' in files[".agents/skills/cf-bare/SKILL.md"]


@pytest.mark.parametrize("bad", ["Тест", "CF-Analyze", "cf_analyze", "-x", "a" * 65])
def test_validate_name_rejects_non_skill_names(bad):
    with pytest.raises(CodexWrapError):
        validate_name(bad)


def test_sync_generates_idempotent_and_removes_orphans(tmp_path):
    (tmp_path / ".claude/commands").mkdir(parents=True)
    (tmp_path / ".claude/commands/cf-test.md").write_text(CMD, encoding="utf-8")
    assert sync(tmp_path)["updated"] == ["cf-test"]
    assert sync(tmp_path)["unchanged"] == ["cf-test"]

    # чужой (не-генерат) скилл рядом sync не трогает никогда
    alien = tmp_path / ".agents/skills/handmade"
    alien.mkdir(parents=True)
    (alien / "SKILL.md").write_text("---\nname: handmade\n---\nруки", encoding="utf-8")

    (tmp_path / ".claude/commands/cf-test.md").unlink()
    report = sync(tmp_path, write=False)
    assert report["orphaned"] == ["cf-test"]        # --check только сообщает
    assert (tmp_path / ".agents/skills/cf-test").is_dir()
    assert sync(tmp_path)["orphaned"] == ["cf-test"]
    assert not (tmp_path / ".agents/skills/cf-test").exists()
    assert (alien / "SKILL.md").is_file()


def test_sync_isolates_invalid_command_and_syncs_the_rest(tmp_path):
    """Одна кривая команда (руки человека мимо install-agent) не роняет
    сверку/генерацию остальных."""
    (tmp_path / ".claude/commands").mkdir(parents=True)
    (tmp_path / ".claude/commands/CF_Legacy.md").write_text(CMD, encoding="utf-8")
    (tmp_path / ".claude/commands/cf-good.md").write_text(CMD, encoding="utf-8")
    report = sync(tmp_path)
    assert report["updated"] == ["cf-good"]
    assert len(report["invalid"]) == 1 and "CF_Legacy" in report["invalid"][0]
    assert (tmp_path / ".agents/skills/cf-good/SKILL.md").is_file()


def test_sync_refuses_to_clobber_handmade_skill(tmp_path):
    """Коллизия имени команды с рукописным скиллом — invalid, файл цел."""
    (tmp_path / ".claude/commands").mkdir(parents=True)
    (tmp_path / ".claude/commands/cf-mine.md").write_text(CMD, encoding="utf-8")
    mine = tmp_path / ".agents/skills/cf-mine"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: cf-mine\n---\nруки",
                                   encoding="utf-8")
    report = sync(tmp_path)
    assert len(report["invalid"]) == 1 and "рукописным скиллом" in report["invalid"][0]
    assert (mine / "SKILL.md").read_text(encoding="utf-8").endswith("руки")
    with pytest.raises(CodexWrapError, match="рукописным скиллом"):
        write_wrapper(tmp_path, "cf-mine", CMD)
    with pytest.raises(CodexWrapError):
        check_collision(tmp_path, "cf-mine")


def test_manual_edit_of_wrapper_is_detected_as_drift(tmp_path):
    (tmp_path / ".claude/commands").mkdir(parents=True)
    (tmp_path / ".claude/commands/cf-test.md").write_text(CMD, encoding="utf-8")
    sync(tmp_path)
    skill = tmp_path / ".agents/skills/cf-test/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nручная правка\n",
                     encoding="utf-8")
    assert sync(tmp_path, write=False)["updated"] == ["cf-test"]


# --- интеграция с install-agent -----------------------------------------------

def _draft(name):
    return (
        "status: proposed\n"
        f"agent: {name}\n\n"
        f"## Файл 1: prompts/agents/{name}.md\n"
        "```md\nпромпт агента\n```\n\n"
        f"## Файл 2: .claude/commands/{name}.md\n"
        "```md\n---\ndescription: Новый агент - тест\n---\n"
        f"Следуй промпту prompts/agents/{name}.md.\n```\n")


def test_install_agent_writes_codex_wrapper(tmp_path):
    (tmp_path / "proposals").mkdir()
    (tmp_path / "proposals/draft.md").write_text(_draft("cf-new"), encoding="utf-8")
    written = install(tmp_path, "draft.md", git=lambda msg, paths: None)
    assert ".agents/skills/cf-new/SKILL.md" in written
    body = (tmp_path / ".agents/skills/cf-new/SKILL.md").read_text(encoding="utf-8")
    assert ".claude/commands/cf-new.md" in body and MARKER in body


def test_install_agent_refuses_unskillable_name_before_writing(tmp_path):
    (tmp_path / "proposals").mkdir()
    (tmp_path / "proposals/draft.md").write_text(_draft("Новый_Агент"),
                                                 encoding="utf-8")
    with pytest.raises(AgentInstallError, match="имя Codex-скилла"):
        install(tmp_path, "draft.md", git=lambda msg, paths: None)
    assert not (tmp_path / "prompts").exists(), "отказ должен идти ДО записи"


def test_install_agent_refuses_collision_with_handmade_skill(tmp_path):
    """Имя команды занято рукописным скиллом — отказ до первой записи, иначе
    установка молча уничтожила бы чужой SKILL.md."""
    (tmp_path / "proposals").mkdir()
    (tmp_path / "proposals/draft.md").write_text(_draft("cf-new"), encoding="utf-8")
    mine = tmp_path / ".agents/skills/cf-new"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: cf-new\n---\nруки", encoding="utf-8")
    with pytest.raises(AgentInstallError, match="рукописным скиллом"):
        install(tmp_path, "draft.md", git=lambda msg, paths: None)
    assert not (tmp_path / "prompts").exists(), "отказ должен идти ДО записи"
    assert (mine / "SKILL.md").read_text(encoding="utf-8").endswith("руки")


def test_apply_edit_checks_wrapper_before_write(tmp_path):
    """Правка команды с не-скилловым именем (руки человека мимо install-agent)
    отвергается ДО записи — иначе черновик клинил бы в полуприменённом виде."""
    cmd_path = tmp_path / ".claude/commands/CF_Legacy.md"
    cmd_path.parent.mkdir(parents=True)
    old = CMD + "Дополнительные строки, чтобы объём правки был осмысленным.\n"
    cmd_path.write_text(old, encoding="utf-8")
    new = old.replace("проверка обёртки", "проверка обёртки v2")
    (tmp_path / "proposals").mkdir()
    (tmp_path / "proposals/edit.md").write_text(
        "status: proposed\nagent: legacy\n\n"
        "## Файл 1: .claude/commands/CF_Legacy.md\n"
        "```md\n" + new + "```\n", encoding="utf-8")
    with pytest.raises(AgentInstallError, match="имя Codex-скилла"):
        apply_edit(tmp_path, "edit.md", git=lambda msg, paths: None)
    assert cmd_path.read_text(encoding="utf-8") == old, "запись не должна начаться"


# --- PreToolUse-guard ---------------------------------------------------------

def _guard(payload):
    return subprocess.run([sys.executable, str(GUARD)],
                          input=payload, capture_output=True, text=True)


def test_guard_blocks_patch_into_protected_paths():
    patch = ("*** Begin Patch\n*** Update File: prompts/agents/brief-generator.md\n"
             "@@\n-a\n+b\n*** End Patch")
    res = _guard(json.dumps({"cwd": str(REPO_ROOT),
                             "tool_input": {"input": patch}}))
    assert res.returncode == 2
    assert "prompts/agents/brief-generator.md" in res.stderr


@pytest.mark.parametrize("path", [
    ".claude/commands/cf-status.md",
    "formulas/_approved/ниша/имя-v1.json",
    "/abs/clone/.claude/settings.json",       # абсолютный путь без cwd в событии
])
def test_guard_blocks_pathy_fields(path):
    assert _guard(json.dumps({"tool_input": {"file_path": path}})).returncode == 2


@pytest.mark.parametrize("path", [
    "docs/../prompts/agents/x.md",     # обход через ..
    "prompts/./agents/x.md",           # обход через ./ внутри
    "prompts//agents/x.md",            # обход через //
])
def test_guard_normalizes_tricky_paths(path):
    assert _guard(json.dumps({"tool_input": {"file_path": path}})).returncode == 2


@pytest.mark.parametrize("path", [
    "docs/prompts-agents.md",          # похожие имена — не защищённые пути
    "myprompts/agents/x.md",
    "docs/prompts/agents-guide.md",
])
def test_guard_passes_lookalike_paths(path):
    assert _guard(json.dumps({"tool_input": {"file_path": path}})).returncode == 0


def test_guard_passes_innocent_edits_and_mentions():
    # правка обычного файла, УПОМИНАЮЩЕГО защищённый путь в теле патча
    patch = ("*** Begin Patch\n*** Update File: docs/codex.md\n"
             "@@\n-x\n+см. prompts/agents/ и .claude/commands/\n*** End Patch")
    ok = {"cwd": str(REPO_ROOT), "tool_input": {"input": patch}}
    assert _guard(json.dumps(ok)).returncode == 0
    assert _guard(json.dumps({"tool_input": {"file_path": "src/cf/cli.py"}})).returncode == 0
    assert _guard("не json вовсе").returncode == 0
