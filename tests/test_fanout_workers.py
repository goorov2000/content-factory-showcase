"""Воркеры фан-аута с предикатами очередей и автопродолжение после ворот.

До 2026-07-26 фан-аут был жёсткой последовательностью: генератор сценариев
запускался КАЖДЫЙ прогон и честно уходил в skipped, потому что ни у одной темы
не было включённого промпта. Деньги тратились на ответ «работы нет», а причина
простоя жила только в прозе отчёта агента.

Здесь доказывается: воркер с пустой очередью платного вызова не делает и говорит
почему; черновик промпта темы пишется ВНУТРИ цикла; автопродолжение после
закрытия ворот гоняет только сценарии с ревью.
"""
import json
from pathlib import Path

import pytest

from cf.dashboard.runner import StageRunner
from tests.fakes import FakeSheets, make_ready_theme, ready_versions


def _claude_out(text, session="s1"):
    return json.dumps({"result": text, "session_id": session})


def _runner(tmp_path, sheets=None, config=None, run_command=None):
    calls = []

    def _cmd(argv):
        calls.append(argv)
        return (0, _claude_out("готово"))

    sheets = sheets if sheets is not None else FakeSheets({"run_log": []})
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    r = StageRunner(sheets, config or {}, http_post=lambda u: None,
                    run_command=run_command or _cmd,
                    analysis_dir=tmp_path / "analysis",
                    locks_dir=tmp_path / "locks", root=root)
    r._git_commit = lambda msg: None
    r._agent_logged = lambda agent, since: True
    return r, calls


def _install_prompt_command(root):
    """Слэш-команда brief-prompt-writer установлена (иначе воркер честно молчит)."""
    p = Path(root) / ".claude" / "commands" / "cf-write-brief-prompt.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# команда", encoding="utf-8")


def _approved(root, *niches):
    idx = Path(root) / "formulas" / "_approved" / "index.json"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"approved": [
        {"name": f"r{i}", "niche": n, "version": 1,
         "path": f"formulas/_approved/{n}/r{i}-v1.json"}
        for i, n in enumerate(niches)]}, ensure_ascii=False), encoding="utf-8")


def _prompts(argv_list):
    return [a[2] for a in argv_list if len(a) > 2]


# ── воркер «Черновик промпта темы» ───────────────────────────────────────────

def test_prompt_draft_worker_writes_drafts_inside_the_cycle(tmp_path):
    # Главная правка порядка: промпт темы больше не ручной ритуал в лаборатории.
    r, calls = _runner(tmp_path)
    _install_prompt_command(r.root)
    _approved(r.root, "альфа", "бета")
    assert r.run_sync("factory") is True
    prompts = _prompts(calls)
    assert "/cf-write-brief-prompt альфа" in prompts
    assert "/cf-write-brief-prompt бета" in prompts
    assert r.run_progress["prompt-draft"]["produced"] == 2


def test_prompt_draft_count_does_not_include_crashed_agent_calls(tmp_path):
    # Ревью 14.09.2026: счётчик рос безусловно, и при упавшем claude лента писала
    # «2 черновика» со статусом done рядом с предупреждением звена.
    r, calls = _runner(tmp_path)
    _install_prompt_command(r.root)
    _approved(r.root, "альфа", "бета")
    real = r.run_command

    def run_command(argv):
        if argv and argv[0] == "claude" and "/cf-write-brief-prompt" in " ".join(map(str, argv)):
            calls.append(argv)
            return (1, "")
        return real(argv)

    r.run_command = run_command
    r.run_sync("factory")
    assert r.run_progress["prompt-draft"]["produced"] == 0


def test_prompt_draft_worker_respects_per_run_limit_and_says_what_it_dropped(tmp_path):
    # Скрытых усечений не бывает: «за прогон пишем N, ещё M в очереди» — иначе
    # оператор читает частичное покрытие как полное.
    r, calls = _runner(tmp_path, config={"dashboard": {"fanout":
                                                       {"prompt_drafts_per_run": 1}}})
    _install_prompt_command(r.root)
    _approved(r.root, "альфа", "бета", "гамма")
    assert r.run_sync("factory") is True
    written = [p for p in _prompts(calls) if p.startswith("/cf-write-brief-prompt")]
    assert len(written) == 1
    assert "ещё 2" in r.run_progress["prompt-draft"]["idle_reason"]


def test_prompt_draft_worker_skips_theme_with_pending_draft(tmp_path):
    # Иначе агент писал бы новый черновик по той же теме каждый прогон.
    r, calls = _runner(tmp_path)
    _install_prompt_command(r.root)
    _approved(r.root, "альфа")
    draft = r.root / "proposals" / "2026-07-26-brief-альфа-reel.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("---\nstatus: proposed\n---\n", encoding="utf-8")
    assert r.run_sync("factory") is True
    assert not [p for p in _prompts(calls) if p.startswith("/cf-write-brief-prompt")]
    assert "тем без промпта нет" in r.run_progress["prompt-draft"]["idle_reason"]


def test_prompt_draft_worker_needs_the_slash_command_installed(tmp_path):
    r, calls = _runner(tmp_path)
    _approved(r.root, "альфа")                       # команду НЕ ставим
    assert r.run_sync("factory") is True
    assert not [p for p in _prompts(calls) if p.startswith("/cf-write-brief-prompt")]
    assert r.run_progress["prompt-draft"]["status"] == "warn"


def test_prompt_draft_worker_skips_excluded_theme(tmp_path):
    r, calls = _runner(tmp_path, config={"dashboard": {"fanout":
                                                       {"exclude_niches": ["стритвир"]}}})
    _install_prompt_command(r.root)
    _approved(r.root, "стритвир")
    assert r.run_sync("factory") is True
    assert not [p for p in _prompts(calls) if p.startswith("/cf-write-brief-prompt")]


# ── воркер «Сценарии»: платный вызов только при непустой очереди ─────────────

def test_briefs_worker_does_not_burn_a_paid_call_on_an_empty_queue(tmp_path):
    r, calls = _runner(tmp_path)
    _approved(r.root, "альфа")                       # рецепт есть, промпта нет
    assert r.run_sync("factory") is True
    assert "/cf-generate-briefs" not in _prompts(calls)
    assert "/cf-review-brief --pending" not in _prompts(calls)
    # Полоска без объяснения читается как «зависло» — говорим причину.
    assert "ждёт" in r.run_progress["briefs"]["idle_reason"]
    assert "включения промпта" in r.run_progress["briefs"]["idle_reason"]


def test_briefs_worker_runs_when_a_theme_is_ready(tmp_path):
    sheets = FakeSheets({"run_log": [], "prompt_versions": ready_versions()})
    r, calls = _runner(tmp_path, sheets=sheets)
    make_ready_theme(r.root)
    assert r.run_sync("factory") is True
    assert "/cf-generate-briefs" in _prompts(calls)
    assert "/cf-review-brief --pending" in _prompts(calls)


def test_briefs_worker_still_runs_when_versions_are_unreadable(tmp_path):
    # Правило №2: лист не прочитан -> судить нельзя. Молча гасить производство по
    # непрочитанным данным нельзя — идём как раньше и не экономим на догадке.
    class Broken(FakeSheets):
        def read_rows(self, tab):
            if tab == "prompt_versions":
                raise RuntimeError("Sheets недоступен")
            return super().read_rows(tab)

    r, calls = _runner(tmp_path, sheets=Broken({"run_log": []}))
    _approved(r.root, "альфа")
    assert r.run_sync("factory") is True
    assert "/cf-generate-briefs" in _prompts(calls)


# ── заметка цикла говорит факт ───────────────────────────────────────────────

def test_cycle_note_names_the_gate_that_holds_the_conveyor(tmp_path):
    r, _ = _runner(tmp_path)
    _approved(r.root, "альфа", "бета")
    r.run_cycle_sync()
    assert "Включение правил сценариев темы" in r.cycle_note
    assert "2 темы ждут вас" in r.cycle_note        # склонение, а не бинарная развилка


def test_cycle_note_says_it_passed_when_no_gate_holds(tmp_path):
    sheets = FakeSheets({"run_log": [], "prompt_versions": ready_versions()})
    r, _ = _runner(tmp_path, sheets=sheets)
    make_ready_theme(r.root)
    r.run_cycle_sync()
    assert r.cycle_note.startswith("цикл прошёл целиком")


# ── автопродолжение после закрытия ворот ─────────────────────────────────────

def test_continue_after_gate_runs_only_scripts_and_review(tmp_path):
    # Решение оператора закрыло ворота -> догоняем производство, но БЕЗ повторного
    # платного сбора и анализа: к решению они отношения не имеют.
    sheets = FakeSheets({"run_log": [], "prompt_versions": ready_versions()})
    r, calls = _runner(tmp_path, sheets=sheets)
    make_ready_theme(r.root)
    assert r.continue_after_gate("включён промпт темы альфа") is True
    _wait_idle(r)
    prompts = _prompts(calls)
    assert "/cf-generate-briefs" in prompts
    assert "/cf-review-brief --pending" in prompts
    assert not [p for p in prompts if p.startswith("/cf-classify-niche")]
    assert not [p for p in prompts if p.startswith("/cf-niche-run")]


def test_continue_after_gate_is_switchable_off(tmp_path):
    # Оператор выбрал «ехать сразу», но откат должен стоить строку в конфиге,
    # а не правку кода: немедленные платные вызовы после каждого решения могут
    # начать раздражать.
    sheets = FakeSheets({"run_log": [], "prompt_versions": ready_versions()})
    r, calls = _runner(tmp_path, sheets=sheets,
                       config={"dashboard": {"fanout": {"autocontinue": False}}})
    make_ready_theme(r.root)
    assert r.continue_after_gate("x") is False
    assert calls == []


def test_continue_after_gate_does_nothing_without_a_producing_theme(tmp_path):
    r, calls = _runner(tmp_path)
    _approved(r.root, "альфа")                       # промпт так и не включён
    assert r.continue_after_gate("x") is False
    assert calls == []


def test_continue_after_gate_refuses_while_pipeline_is_busy(tmp_path):
    sheets = FakeSheets({"run_log": [], "prompt_versions": ready_versions()})
    r, calls = _runner(tmp_path, sheets=sheets)
    make_ready_theme(r.root)
    r.state["raw"] = {"status": "running", "detail": "выполняется…"}
    assert r.continue_after_gate("x") is False
    assert "конвейер занят" in r.cycle_note
    assert calls == []


def _wait_idle(runner, timeout=5.0):
    """Дождаться завершения фонового потока продолжения."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runner.state["factory"]["status"] != "running":
            return
        time.sleep(0.02)
    pytest.fail("продолжение после ворот не завершилось")
