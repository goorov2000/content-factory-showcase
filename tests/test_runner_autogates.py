"""Машинные ворота внутри цикла: воркеры «Проверка рецептов» и «Включение промпта».

Проверяется главное обещание вечера 2026-07-26: закрытие двух ворот больше не
требует человека, но и не становится молчаливым — пустая очередь не жжёт платный
вызов, красный чек-лист оставляет тему человеку с причиной, а снижение строгости
(судья не установлен) обязано быть видно в ленте.
"""
import json
import pathlib
import re
from pathlib import Path

from cf.dashboard.runner import StageRunner

from tests.fakes import FakeSheets


def _runner(tmp_path, config=None, run_command=None, sheets=None):
    return StageRunner(
        sheets or FakeSheets({"run_log": [], "prompt_versions": []}),
        config or {},
        http_post=lambda u: None,
        run_command=run_command or (lambda argv: (0, "", "")),
        analysis_dir=tmp_path / "analysis", locks_dir=tmp_path / "locks",
        root=tmp_path)


def _draft_formula(root, niche="мужской-стиль", name="alpha", status="proposed"):
    path = root / "formulas" / niche / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "niche": niche, "status": status,
                                "version": 1}, ensure_ascii=False), encoding="utf-8")
    return path


def _params(**over):
    params = {"exclude_niches": [], "prompt_drafts_per_run": 2}
    params.update(over)
    return params


# ── воркер «Проверка рецептов» ───────────────────────────────────────────────

def test_empty_queue_costs_nothing_and_says_why(tmp_path):
    calls = []
    runner = _runner(tmp_path, run_command=lambda argv: calls.append(argv) or (0, "", ""))
    problems = []
    runner._run_formula_gate_worker("factory", _params(), problems)
    assert calls == [] and problems == []
    step = runner.run_progress["formula-review"]
    assert step["status"] == "done" and step["produced"] == 0
    assert "черновиков рецептов нет" in step["idle_reason"]


def _judge_installed(root):
    cmd = root / ".claude" / "commands" / "cf-review-formula.md"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("судья", encoding="utf-8")


def _verdict_file(root, name, verdict="recommend"):
    path = (root / "agent-runtime" / "reviews-formula"
            / f"2026-07-27-{name}-review.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "verdict": verdict, "reasons": []},
                               ensure_ascii=False), encoding="utf-8")
    return path


def test_judge_command_is_used_when_installed(tmp_path):
    _draft_formula(tmp_path)
    _judge_installed(tmp_path)
    calls = []

    def run(argv):
        calls.append(argv)
        return (0, json.dumps({"result": "ok"}), "")

    runner = _runner(tmp_path, run_command=run)
    runner._run_formula_gate_worker("factory", _params(), [])
    assert any("/cf-review-formula" in " ".join(map(str, argv)) for argv in calls)


def test_runner_applies_the_verdict_itself(tmp_path):
    """Агент судит, решает КОД.

    Прогон 27.07 01:15: судья написал 4 вердикта (2 recommend) и до хвостовых шагов
    своей команды — «позови auto-approve-formula», «залогируй» — не дошёл. Ноль
    одобрений, ни строки в Run Log. Решение не должно зависеть от того, дочитал ли
    агент собственную инструкцию до конца."""
    path = _draft_formula(tmp_path)
    _judge_installed(tmp_path)
    review = _verdict_file(tmp_path, "alpha")
    calls = []

    def run(argv):
        calls.append([str(a) for a in argv])
        return (0, "", "")

    runner = _runner(tmp_path, run_command=run)
    runner._run_formula_gate_worker("factory", _params(), [])

    decision = [a for a in calls if "auto-approve-formula" in a]
    assert decision, "раннер не вызвал решение по рецепту"
    assert str(path) in decision[0] and "--review" in decision[0]
    assert str(review) in decision[0]
    assert "--no-judge" not in decision[0]


def test_missing_verdict_is_not_silent_consent(tmp_path):
    # Судья установлен, но по рецепту вердикта нет: «судья не сработал» не должно
    # означать «судья согласен».
    _draft_formula(tmp_path)
    _judge_installed(tmp_path)
    calls = []
    runner = _runner(tmp_path,
                     run_command=lambda argv: calls.append([str(a) for a in argv]) or (0, "", ""))

    runner._run_formula_gate_worker("factory", _params(), [])

    decision = [a for a in calls if "auto-approve-formula" in a]
    assert decision and "--no-judge" not in decision[0] and "--review" not in decision[0]
    assert "без вердикта судьи" in runner.run_progress["formula-review"]["idle_reason"]


def test_verdict_of_another_formula_is_not_reused(tmp_path):
    _draft_formula(tmp_path, name="alpha")
    _judge_installed(tmp_path)
    _verdict_file(tmp_path, "beta")           # вердикт ЧУЖОГО рецепта
    calls = []
    runner = _runner(tmp_path,
                     run_command=lambda argv: calls.append([str(a) for a in argv]) or (0, "", ""))

    runner._run_formula_gate_worker("factory", _params(), [])

    decision = [a for a in calls if "auto-approve-formula" in a]
    assert decision and "--review" not in decision[0]


def test_without_judge_falls_back_and_says_so(tmp_path):
    # Молчаливое снижение строгости — ровно то, из-за чего завод однажды встал
    # незаметно. Фолбэк допустим, немой фолбэк — нет.
    path = _draft_formula(tmp_path)
    calls = []

    def run(argv):
        calls.append([str(a) for a in argv])
        return (0, "", "")

    runner = _runner(tmp_path, run_command=run)
    runner._run_formula_gate_worker("factory", _params(), [])
    assert any("auto-approve-formula" in argv and "--no-judge" in argv
               for argv in calls)
    assert any(str(path) in argv for argv in calls)
    assert "судья не установлен" in runner.run_progress["formula-review"]["idle_reason"]


def test_excluded_niche_drafts_are_not_reviewed(tmp_path):
    _draft_formula(tmp_path, niche="женская-мода")
    calls = []
    runner = _runner(tmp_path, run_command=lambda argv: calls.append(argv) or (0, "", ""))
    runner._run_formula_gate_worker("factory",
                                    _params(exclude_niches=["женская-мода"]), [])
    assert calls == []


# ── воркер «Включение промпта темы» ──────────────────────────────────────────

def _prompt_draft(root, niche, body, status="proposed"):
    path = root / "proposals" / f"2026-07-26-brief-{niche}-reel.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstatus: {status}\n---\n\n## Proposed changes\n\n```markdown\n"
        f"{body}\n```\n", encoding="utf-8")
    return path


def _approved(root, niche, names):
    idx = root / "formulas" / "_approved" / "index.json"
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"approved": [
        {"name": n, "niche": niche, "version": 1,
         "path": f"formulas/_approved/{niche}/{n}-v1.json"} for n in names]},
        ensure_ascii=False), encoding="utf-8")
    (root / "prompts" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "agents" / "niche-taxonomy.json").write_text(
        json.dumps({"niches": [niche]}, ensure_ascii=False), encoding="utf-8")


def test_green_draft_is_applied_without_human(tmp_path):
    _approved(tmp_path, "мужской-стиль", ["alpha", "beta"])
    _prompt_draft(tmp_path, "мужской-стиль", "Рецепты темы: alpha, beta.")
    sheets = FakeSheets({"run_log": [], "prompt_versions": []})
    runner = _runner(tmp_path, sheets=sheets)

    runner._run_prompt_apply_worker("factory", _params(), [])

    assert (tmp_path / "prompts" / "briefs" / "мужской-стиль" / "reel.md").is_file()
    assert runner.run_progress["prompt-apply"]["produced"] == 1
    # версия промпта записана — иначе eval не отличит один промпт от другого
    assert any(str(r.get("prompt_id")) == "brief-мужской-стиль-reel"
               for r in sheets.tables["prompt_versions"])


def test_uncovered_draft_is_left_to_the_human(tmp_path):
    # Дословный дефект «мужских-образов»: промпт называл 1 рецепт из 4.
    _approved(tmp_path, "мужской-стиль", ["alpha", "beta", "gamma", "delta"])
    _prompt_draft(tmp_path, "мужской-стиль", "Только alpha.")
    runner = _runner(tmp_path)

    runner._run_prompt_apply_worker("factory", _params(), [])

    assert not (tmp_path / "prompts" / "briefs" / "мужской-стиль" / "reel.md").exists()
    step = runner.run_progress["prompt-apply"]
    assert step["produced"] == 0 and "на ваше решение" in step["idle_reason"]


def _candidate_draft(root, niche, version, body, status="proposed"):
    path = root / "proposals" / f"2026-07-26-brief-{niche}-reel-v{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstatus: {status}\n---\n\n## Proposed changes\n\n```markdown\n"
        f"{body}\n```\n", encoding="utf-8")
    return path


def test_prompt_edit_becomes_ab_candidate_not_a_replacement(tmp_path):
    """Правка действующего промпта не подменяет активную версию, а встаёт рядом.

    Подмена текста активной версии порвала бы связку «версия → текст → результат»:
    сценарии, уже произведённые по v2, стали бы указывать на текст v3."""
    _approved(tmp_path, "мужской-стиль", ["alpha", "beta"])
    live = tmp_path / "prompts" / "briefs" / "мужской-стиль" / "reel.md"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("действующий промпт v2", encoding="utf-8")
    _candidate_draft(tmp_path, "мужской-стиль", 3, "Рецепты темы: alpha, beta.")
    sheets = FakeSheets({"run_log": [], "prompt_versions": [
        {"prompt_id": "brief-мужской-стиль-reel", "version": "v2", "active": "TRUE",
         "github_path": "prompts/briefs/мужской-стиль/reel.md", "changelog": "",
         "activated_at": "2026-07-26T00:00:00+00:00"}]})
    runner = _runner(tmp_path, sheets=sheets)

    runner._run_prompt_apply_worker("factory", _params(), [])

    # активный файл не тронут, кандидат лежит отдельным файлом
    assert live.read_text(encoding="utf-8") == "действующий промпт v2"
    assert (tmp_path / "prompts" / "briefs" / "мужской-стиль" / "reel-v3.md").is_file()
    rows = sheets.tables["prompt_versions"]
    assert any(str(r.get("version")) == "v3"
               and str(r.get("active")).upper() == "CANDIDATE" for r in rows)
    # активная версия осталась активной — иначе это не A/B, а та же замена
    assert any(str(r.get("version")) == "v2"
               and str(r.get("active")).upper() == "TRUE" for r in rows)


def test_prompt_edit_without_active_version_is_refused(tmp_path):
    # Правка того, чего нет: первое включение темы идёт другим путём.
    _approved(tmp_path, "мужской-стиль", ["alpha"])
    _candidate_draft(tmp_path, "мужской-стиль", 3, "Рецепт alpha.")
    runner = _runner(tmp_path)

    runner._run_prompt_apply_worker("factory", _params(), [])

    assert not (tmp_path / "prompts" / "briefs" / "мужской-стиль" / "reel-v3.md").exists()
    assert "на ваше решение" in runner.run_progress["prompt-apply"]["idle_reason"]


def test_manual_policy_applies_nothing(tmp_path):
    _approved(tmp_path, "мужской-стиль", ["alpha"])
    _prompt_draft(tmp_path, "мужской-стиль", "Рецепт alpha.")
    runner = _runner(tmp_path, config={"gates": {"policy": {"prompt": "manual"}}})

    runner._run_prompt_apply_worker("factory", _params(), [])

    assert not (tmp_path / "prompts" / "briefs" / "мужской-стиль" / "reel.md").exists()
    assert "ручном режиме" in runner.run_progress["prompt-apply"]["idle_reason"]


def test_no_drafts_costs_nothing(tmp_path):
    runner = _runner(tmp_path)
    runner._run_prompt_apply_worker("factory", _params(), [])
    step = runner.run_progress["prompt-apply"]
    assert step["status"] == "done" and step["produced"] == 0
    assert "черновиков промптов тем нет" in step["idle_reason"]


def test_one_broken_draft_does_not_stop_the_others(tmp_path):
    # Тема с битым черновиком не должна уносить с собой готовую.
    _approved(tmp_path, "мужской-стиль", ["alpha"])
    _prompt_draft(tmp_path, "мужской-стиль", "Рецепт alpha.")
    broken = tmp_path / "proposals" / "2026-07-26-brief-битая-reel.md"
    broken.write_text("---\nstatus: proposed\n---\n\nбез секции\n", encoding="utf-8")
    runner = _runner(tmp_path)

    problems = []
    runner._run_prompt_apply_worker("factory", _params(), problems)

    assert (tmp_path / "prompts" / "briefs" / "мужской-стиль" / "reel.md").is_file()
    assert runner.run_progress["prompt-apply"]["produced"] == 1


# ── воркер «Доработка сценариев» ─────────────────────────────────────────────

def _revised(brief_id, notes=""):
    return {"brief_id": brief_id, "formula_id": "alpha", "review_status": "revised",
            "reviewer_notes": notes, "generated_at": "2026-07-26T00:00:00+00:00"}


def _fix_command(root):
    path = root / ".claude" / "commands" / "cf-fix-brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("доработчик", encoding="utf-8")


def test_revised_briefs_go_to_the_fixer(tmp_path):
    _fix_command(tmp_path)
    sheets = FakeSheets({"run_log": [], "briefs": [_revised("b-1")]})
    calls = []
    runner = _runner(tmp_path, sheets=sheets,
                     run_command=lambda argv: calls.append(argv) or (0, "", ""))

    runner._run_fix_worker("factory", [], 0)

    assert any("/cf-fix-brief" in " ".join(map(str, argv)) for argv in calls)


def test_already_fixed_brief_is_not_fixed_twice(tmp_path):
    # Иначе завод и ревьюер пингуют друг друга бесконечно, а бриф не доходит ни до
    # съёмки, ни до человека.
    _fix_command(tmp_path)
    sheets = FakeSheets({"run_log": [],
                         "briefs": [_revised("b-1", "доработано заводом: CTA")]})
    calls = []
    runner = _runner(tmp_path, sheets=sheets,
                     run_command=lambda argv: calls.append(argv) or (0, "", ""))

    runner._run_fix_worker("factory", [], 0)

    assert calls == []
    assert "сценариев в доработке нет" in runner.run_progress["fix"]["idle_reason"]


def test_missing_fixer_agent_is_loud(tmp_path):
    # Молчаливый пропуск вернул бы «доработку» в состояние тупика незаметно.
    sheets = FakeSheets({"run_log": [], "briefs": [_revised("b-1")]})
    runner = _runner(tmp_path, sheets=sheets)

    runner._run_fix_worker("factory", [], 0)

    step = runner.run_progress["fix"]
    assert step["status"] == "warn" and "агент не установлен" in step["idle_reason"]


def test_fix_worker_returns_updated_pending_count(tmp_path):
    # Переписанные брифы возвращаются в очередь ревью: без пересчёта шаг «Ревью»
    # отчитался бы отрицательной разницей и показал бы ноль проверенных.
    _fix_command(tmp_path)
    sheets = FakeSheets({"run_log": [], "briefs": [_revised("b-1")]})
    runner = _runner(tmp_path, sheets=sheets)
    runner._brief_counts = lambda: (5, 3)

    assert runner._run_fix_worker("factory", [], 1) == 3


def test_runner_applies_the_rewritten_brief_itself(tmp_path):
    """Агент ПИШЕТ исправленный сценарий, применяет его КОД.

    Прогон 27.07 02:44: агент переписал 5 из 6 и честно отчитался «возврат на ревью
    не выполнен: cf revise-brief отклонён разрешениями харнесса». Работа агента не
    должна пропадать из-за настройки, которой он не управляет.

    Второй бриф очереди агент не переписал — с 27.07 такой уходит из очереди
    фиксера к человеку пометкой --refused, а не висит в ней вечно, жгя платный
    вызов каждый цикл (разбор 2026-07-27)."""
    _fix_command(tmp_path)
    fixed = tmp_path / "agent-runtime" / "briefs" / "b-1-fixed.json"
    fixed.parent.mkdir(parents=True, exist_ok=True)
    fixed.write_text(json.dumps({"brief_id": "b-1"}), encoding="utf-8")
    sheets = FakeSheets({"run_log": [], "briefs": [_revised("b-1"), _revised("b-2")]})
    calls = []
    runner = _runner(tmp_path, sheets=sheets,
                     run_command=lambda argv: calls.append([str(a) for a in argv]) or (0, "", ""))

    runner._run_fix_worker("factory", [], 0)

    revise = [a for a in calls if "revise-brief" in a]
    applied = [a for a in revise if "--refused" not in a]
    refused = [a for a in revise if "--refused" in a]
    assert len(applied) == 1                      # только у того, кого переписали
    assert "b-1" in applied[0] and str(fixed) in applied[0]
    assert len(refused) == 1                      # второй снят с доработки к человеку
    assert "b-2" in refused[0] and "b-1" not in refused[0]
    step = runner.run_progress["fix"]
    assert step["produced"] == 1 and "1 из 2" in step["idle_reason"]
    assert "снято с доработки" in step["idle_reason"]


def test_pipeline_runs_gates_between_formulas_and_briefs(tmp_path):
    """Порядок звеньев: рецепты утверждаются ДО того, как пишется промпт темы.

    До 26.07 свежий рецепт ждал решения человека сутки, прежде чем попасть в
    промпт своей темы, — порядок и есть смысл правки."""
    order = []
    runner = _runner(tmp_path)
    for name in ("_run_formula_gate_worker", "_run_prompt_draft_worker",
                 "_run_prompt_apply_worker", "_run_briefs_worker"):
        def hook(*a, _n=name, **kw):
            order.append(_n)
        setattr(runner, name, hook)
    runner._fanout_guard = lambda problems: None
    runner._git_commit = lambda msg: None
    runner._eligible_niches = lambda problems=None, rows_by_tab=None: []
    runner._fanout_claude = lambda *a, **kw: None
    runner._raw_rows = lambda tab: []
    runner._unclassified_count = lambda tab: 0

    runner.run_fanout_sync("factory")

    assert order == ["_run_formula_gate_worker", "_run_prompt_draft_worker",
                     "_run_prompt_apply_worker", "_run_briefs_worker"]


def test_agent_install_puts_prompt_and_command_in_place(tmp_path):
    """Установка нового агента — одна команда человека вместо копипаста двух блоков."""
    from cf.agentinstall import install
    draft = tmp_path / "proposals" / "2026-07-26-x-agent.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(
        "---\nstatus: proposed\nagent: x\n---\n\n"
        "## Файл 1: prompts/agents/x.md\n\n```markdown\n# X\nроль\n```\n\n"
        "## Файл 2: .claude/commands/cf-x.md\n\n```markdown\n---\nd: x\n---\nтело\n```\n",
        encoding="utf-8")

    written = install(tmp_path, draft.name, git=lambda msg, paths: None)

    # вместе с командой пишется её Codex-обёртка (генерат codex-sync):
    # без неё новый агент существует только для Claude Code
    assert written == ["prompts/agents/x.md", ".claude/commands/cf-x.md",
                       ".agents/skills/cf-x/SKILL.md",
                       ".agents/skills/cf-x/agents/openai.yaml"]
    assert (tmp_path / "prompts" / "agents" / "x.md").read_text(
        encoding="utf-8").startswith("# X")
    assert "status: approved" in draft.read_text(encoding="utf-8")


def test_agent_install_refuses_path_outside_allowed_dirs(tmp_path):
    from cf.agentinstall import AgentInstallError, install
    draft = tmp_path / "proposals" / "2026-07-26-y-agent.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(
        "---\nstatus: proposed\n---\n\n"
        "## Файл 1: cf.config.json\n\n```json\n{}\n```\n", encoding="utf-8")
    try:
        install(tmp_path, draft.name, git=lambda msg, paths: None)
    except AgentInstallError as exc:
        assert "вне разрешённых каталогов" in str(exc)
    else:
        raise AssertionError("установка не отказала на пути вне разрешённых каталогов")
    assert not (tmp_path / "cf.config.json").exists()


def test_agent_install_refuses_to_overwrite_existing(tmp_path):
    from cf.agentinstall import AgentInstallError, install
    existing = tmp_path / "prompts" / "agents" / "x.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("действующий промпт", encoding="utf-8")
    draft = tmp_path / "proposals" / "2026-07-26-x-agent.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(
        "---\nstatus: proposed\n---\n\n"
        "## Файл 1: prompts/agents/x.md\n\n```markdown\nновый\n```\n",
        encoding="utf-8")
    try:
        install(tmp_path, draft.name, git=lambda msg, paths: None)
    except AgentInstallError as exc:
        assert "уже существует" in str(exc)
    else:
        raise AssertionError("установка перезаписала действующий промпт")
    assert existing.read_text(encoding="utf-8") == "действующий промпт"


def test_installed_agents_match_their_latest_draft(tmp_path):
    """Боевой файл агента совпадает с ПОСЛЕДНИМ применённым черновиком.

    Тот же класс, что test_shipped_source_proposals_apply_cleanly: пока черновик не
    применён — он обязан применяться; после применения — proposals/ не должен
    превращаться в устаревшую копию боевого текста, иначе следующий читатель
    правит не то. «Последним» правит именно правка: 27.07 команда судьи была
    установлена одним черновиком и в ту же ночь исправлена другим."""
    import re as _re
    from cf.agentinstall import _STATUS_RE, parse_draft
    latest = {}          # относительный путь -> (имя черновика, тело)
    for draft in sorted(pathlib.Path("proposals").glob("*.md")):
        text = draft.read_text(encoding="utf-8")
        # agent-edit — словарь черновиков cf apply-agent-edit (правки 14.08):
        # без него правка агента невидима гварду, и «последним» черновиком
        # оставался бы черновик установки — ложный дрейф после первой же правки.
        if not _re.search(r"^kind:\s*(new-agent|agent-prompt-edit|agent-edit)\s*$",
                          text, _re.MULTILINE):
            continue
        # Только ПРИМЕНЁННЫЕ (ревью 14.09.2026): свежий proposed-черновик правки
        # иначе становился «последним» и краснил сверку до своего применения.
        status = _STATUS_RE.search(text)
        if not status or status.group("status") != "approved":
            continue
        for rel, body in parse_draft(text):
            latest[rel] = (draft.name, body)   # сортировка по имени = по дате

    assert latest, "в репозитории нет ни одного черновика агента"
    for rel, (name, body) in sorted(latest.items()):
        installed = pathlib.Path(rel)
        if not installed.is_file():
            continue        # черновик ещё не применён — это проверяет соседний тест
        assert installed.read_text(encoding="utf-8") == body, (
            f"{rel} разошёлся с последним черновиком {name}")


def test_unapplied_agent_drafts_are_installable(tmp_path):
    """Черновик, который нельзя применить, — это не черновик, а текст."""
    import re as _re
    from cf.agentinstall import install, parse_draft
    for draft in sorted(pathlib.Path("proposals").glob("*.md")):
        text = draft.read_text(encoding="utf-8")
        if not _re.search(r"^kind:\s*new-agent\s*$", text, _re.MULTILINE):
            continue
        if not _re.search(r"^status:\s*proposed\s*$", text, _re.MULTILINE):
            continue
        expected = [rel for rel, _b in parse_draft(text)]
        dst = tmp_path / draft.name / "proposals"
        dst.mkdir(parents=True, exist_ok=True)
        (dst / draft.name).write_text(text, encoding="utf-8")
        written = install(dst.parent, draft.name, git=lambda msg, paths: None)
        assert written == expected, draft.name


# ── Правка действующего промпта агента (27.07) ───────────────────────────────
# install-agent намеренно не пишет поверх работающего файла, а пути «поправить
# существующее» не было вовсе: deny-правила харнесса не пускают в
# prompts/agents/** и .claude/commands/** Edit-инструменты.

def _edit_draft(root, rel, body, status="proposed"):
    path = root / "proposals" / "2026-07-27-x-edit.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstatus: {status}\nagent: x\n---\n\n"
        f"## Файл 1: {rel}\n\n```markdown\n{body}\n```\n", encoding="utf-8")
    return path


def _existing(root, rel, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


_CMD = """---
description: X
---
Следуй промпту prompts/agents/x.md.

Порядок:
1. Шаг один, достаточно длинный, чтобы объём был осмысленным.
2. Шаг два, тоже длинный, чтобы правка не выглядела обрывом.
3. Шаг три, закрывающий цикл и логирующий прогон.
"""


def test_agent_edit_replaces_existing_file(tmp_path):
    from cf.agentinstall import apply_edit
    target = _existing(tmp_path, ".claude/commands/cf-x.md", _CMD)
    new = _CMD.replace("3. Шаг три", "3. Шаг три с логом после каждого предмета")
    draft = _edit_draft(tmp_path, ".claude/commands/cf-x.md", new)

    written = apply_edit(tmp_path, draft.name, git=lambda msg, paths: None)

    # правка команды перегенерирует и её Codex-обёртку — description может дрейфовать
    assert written == [".claude/commands/cf-x.md",
                       ".agents/skills/cf-x/SKILL.md",
                       ".agents/skills/cf-x/agents/openai.yaml"]
    assert "с логом после каждого предмета" in target.read_text(encoding="utf-8")
    assert "status: approved" in draft.read_text(encoding="utf-8")


def test_agent_edit_refuses_missing_file(tmp_path):
    from cf.agentinstall import AgentInstallError, apply_edit
    draft = _edit_draft(tmp_path, ".claude/commands/cf-new.md", _CMD)
    try:
        apply_edit(tmp_path, draft.name, git=lambda msg, paths: None)
    except AgentInstallError as exc:
        assert "install-agent" in str(exc)
    else:
        raise AssertionError("правка несуществующего файла прошла")


def test_agent_edit_refuses_truncated_text(tmp_path):
    # Дословно дефект 27.07: блок оборвался, текст применился, смысл потерян.
    from cf.agentinstall import AgentInstallError, apply_edit
    target = _existing(tmp_path, ".claude/commands/cf-x.md", _CMD)
    draft = _edit_draft(tmp_path, ".claude/commands/cf-x.md",
                        _CMD.split("1. Шаг один")[0] + "1. Шаг один, а дальше:")
    try:
        apply_edit(tmp_path, draft.name, git=lambda msg, paths: None)
    except AgentInstallError as exc:
        assert "короче" in str(exc) or "оборван" in str(exc)
    else:
        raise AssertionError("обрубок применился")
    assert target.read_text(encoding="utf-8") == _CMD


def test_agent_edit_refuses_command_without_frontmatter(tmp_path):
    from cf.agentinstall import AgentInstallError, apply_edit
    _existing(tmp_path, ".claude/commands/cf-x.md", _CMD)
    draft = _edit_draft(tmp_path, ".claude/commands/cf-x.md",
                        _CMD.replace("---\ndescription: X\n---\n", "", 1))
    try:
        apply_edit(tmp_path, draft.name, git=lambda msg, paths: None)
    except AgentInstallError as exc:
        assert "frontmatter" in str(exc)
    else:
        raise AssertionError("команда без frontmatter применилась")


def test_agent_edit_refuses_identical_text(tmp_path):
    from cf.agentinstall import AgentInstallError, apply_edit
    _existing(tmp_path, ".claude/commands/cf-x.md", _CMD)
    draft = _edit_draft(tmp_path, ".claude/commands/cf-x.md", _CMD)
    try:
        apply_edit(tmp_path, draft.name, git=lambda msg, paths: None)
    except AgentInstallError as exc:
        assert "не отличается" in str(exc)
    else:
        raise AssertionError("пустая правка прошла")
