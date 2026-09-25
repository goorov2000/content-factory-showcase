import json
import logging
import subprocess
import sys
from pathlib import Path

from cf.dashboard import runner as runner_mod
from cf.dashboard.runner import StageRunner
from cf.lock import ProcessLock
from tests.fakes import FakeSheets, make_ready_theme, ready_versions


def _claude_out(result_text, session="s1"):
    return json.dumps({"result": result_text, "session_id": session})


def _niche_out(niche, formulas, patterns=1, status="ok", session="s1"):
    line = "NICHE_RESULT: " + json.dumps(
        {"niche": niche, "patterns": patterns, "formulas": formulas, "status": status})
    return _claude_out(f"прогон ниши {niche}\nитог\n{line}", session)


def _raw_rows(niche, n, views, collected="", start=0):
    # M19: очередь считает только «чистые» строки — все критические поля
    # профайлера (source_url, account, views, posted_at, niche) непусты.
    # collected — штамп first-seen для аддитивного гейта очереди (min_new_rows);
    # start сдвигает нумерацию source_url, чтобы склеивать «старую» и «новую»
    # пачки одной ниши без коллизий по ключу дедупа.
    return [{"niche": niche, "views": views, "source_url": f"u{niche}{i}",
             "account": f"acc{i}", "posted_at": "2026-07-20",
             "collected_at": collected}
            for i in range(start, start + n)]


def _make(sheets, run_command, analysis_dir):
    """Раннер с замоканным git (никаких реальных коммитов в тесте) и записью его вызовов."""
    runner = StageRunner(sheets, {}, http_post=lambda u: None,
                         run_command=run_command, analysis_dir=Path(analysis_dir))
    git_calls = []
    runner._git_commit = lambda msg: git_calls.append(msg)
    # фейковые claude не пишут в Run Log — контракт правила №6 гоняют свои тесты
    runner._agent_logged = lambda agent, since: True
    return runner, git_calls


def _dispatch(niche_map):
    """Фейковый run_command: classify/briefs/review/guard → ok; ниши — из niche_map."""
    commands = []

    def run_command(argv):
        commands.append(argv)
        prompt = argv[2] if len(argv) > 2 else ""
        if isinstance(prompt, str) and prompt.startswith("/cf-niche-run"):
            niche = prompt.split()[-1]
            code, out = niche_map[niche]
            return (code, out)
        return (0, _claude_out("готово"))

    return run_command, commands


# ── 1. Очередь: пороги + приток ──────────────────────────────────────────────

def test_fanout_queue_respects_thresholds_and_new_rows(tmp_path):
    rows = (_raw_rows("A", 20, 2000)   # хватает, но есть свежий анализ без притока → выпадет
            + _raw_rows("B", 5, 2000)   # мало строк
            + _raw_rows("C", 15, 50)    # ниже порога просмотров
            + _raw_rows("D", 20, 2000))  # проходит
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    # свежий анализ ниши A: total_rows 14 → новых строк 20-14=6 < min_new_rows=40
    (tmp_path / "2026-07-15-raw_tiktok-A-analysis.json").write_text(
        json.dumps({"total_rows": 14}), encoding="utf-8")
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == [("raw_tiktok", "D")]


def test_new_rows_gate_counts_deduped_rows(tmp_path):
    # 100 сырых строк, но только 80 уникальных source_url: дубли не приток.
    rows = _raw_rows("A", 80, 2000)
    rows += [dict(r) for r in rows[:20]]                 # 20 дублей по source_url
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    # прошлый анализ: total_rows 40 (дедуплицированный) → приток 80-40=40... но
    # берём 45, чтобы приток вышел 35 < 40. Если бы дубли считались, вышло бы
    # 100-45=55 и ниша ошибочно встала бы в очередь.
    (tmp_path / "2026-07-15-raw_tiktok-A-analysis.json").write_text(
        json.dumps({"total_rows": 45}), encoding="utf-8")
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == []


# ── 1б. Аддитивный гейт очереди (min_new_rows, 2026-07-26) ───────────────────

def _analysis(tmp_path, niche, tab="raw_tiktok", name="2026-07-15", **body):
    (tmp_path / f"{name}-{tab}-{niche}-analysis.json").write_text(
        json.dumps(body), encoding="utf-8")


def test_new_rows_counted_by_collected_at_when_delta_is_zero(tmp_path):
    # Архивная ротация/ре-классификация ужали срез: len(group) == total_rows,
    # дельта нулевая. Приток обязан посчитаться по collected_at, иначе ниша
    # замолкает навсегда (ровно то, что делал мультипликативный гейт).
    rows = _raw_rows("A", 20, 2000, collected="2026-07-01T00:00:00Z")
    rows += _raw_rows("A", 40, 2000, collected="2026-07-20T00:00:00Z", start=20)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    _analysis(tmp_path, "A", total_rows=60,
              meta={"generated_at": "2026-07-15T00:00:00+00:00", "since": None})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == [("raw_tiktok", "A")]   # 40 свежих >= 40


def test_new_rows_counted_by_delta_when_collected_at_is_stale(tmp_path):
    # Обратный случай: строки собраны ДО анализа, нишу им проставил классификатор
    # позже. По collected_at приток нулевой, спасает дельта к total_rows.
    rows = _raw_rows("A", 60, 2000, collected="2026-07-01T00:00:00Z")
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    _analysis(tmp_path, "A", total_rows=15,
              meta={"generated_at": "2026-07-15T00:00:00+00:00", "since": None})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == [("raw_tiktok", "A")]   # дельта 45 >= 40


def test_new_rows_gate_boundary_39_and_40(tmp_path):
    def queue(n):
        rows = _raw_rows("A", 20, 2000, collected="2026-07-01T00:00:00Z")
        rows += _raw_rows("A", n, 2000, collected="2026-07-20T00:00:00Z", start=20)
        sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
        _analysis(tmp_path, "A", total_rows=20 + n,
                  meta={"generated_at": "2026-07-15T00:00:00+00:00", "since": None})
        runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
        return runner._eligible_niches()
    assert queue(39) == []                        # ровно на единицу не хватило
    assert queue(40) == [("raw_tiktok", "A")]     # порог включительный


def test_shrunk_slice_does_not_produce_negative_delta(tmp_path):
    # После `cf archive` срез меньше прошлого замера: дельта отрицательна.
    # Она обязана клампиться в 0, а не подсчитываться как приток.
    rows = _raw_rows("A", 30, 2000, collected="2026-07-01T00:00:00Z")
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    _analysis(tmp_path, "A", total_rows=200,
              meta={"generated_at": "2026-07-15T00:00:00+00:00", "since": None})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == []
    assert runner._new_rows_since(rows, runner._previous_analysis("raw_tiktok", "A")) == 0


def test_partial_analysis_with_since_ignores_total_rows(tmp_path):
    # analyze-batch --since считал ЧАСТЬ среза: total_rows несравним, дельта по
    # нему завысила бы приток. Остаётся только сигнал по collected_at.
    rows = _raw_rows("A", 60, 2000, collected="2026-07-01T00:00:00Z")
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    _analysis(tmp_path, "A", total_rows=5,
              meta={"generated_at": "2026-07-15T00:00:00+00:00", "since": "2026-07-10"})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == []      # дельта 55 отброшена, свежих 0


def test_null_since_does_not_disable_delta_signal(tmp_path):
    # Ключ since ПРИСУТСТВУЕТ во всех боевых analysis-файлах со значением null.
    # Проверка обязана быть на truthy: `if "since" in meta` обнулила бы дельту
    # разом у всех ниш и оставила гейт на одном сигнале.
    rows = _raw_rows("A", 60, 2000, collected="2026-07-01T00:00:00Z")
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    _analysis(tmp_path, "A", total_rows=15,
              meta={"generated_at": "2026-07-15T00:00:00+00:00", "since": None})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == [("raw_tiktok", "A")]


def test_meta_generated_at_wins_over_filename_date(tmp_path):
    # Имя файла — легаси-фолбэк (полночь UTC). Если есть meta.generated_at,
    # считать надо по нему: строки того же дня до анализа притоком не являются.
    rows = _raw_rows("A", 20, 2000, collected="2026-07-15T09:00:00Z")
    rows += _raw_rows("A", 40, 2000, collected="2026-07-15T21:00:00Z", start=20)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    _analysis(tmp_path, "A", total_rows=60,
              meta={"generated_at": "2026-07-15T12:00:00+00:00", "since": None})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    # по имени файла (полночь) свежими были бы все 60; по generated_at — ровно 40
    assert runner._new_rows_since(
        rows, runner._previous_analysis("raw_tiktok", "A")) == 40


def test_legacy_analysis_without_meta_falls_back_to_filename_date(tmp_path):
    # Старый файл без meta: дата берётся из имени, гейт продолжает работать.
    rows = _raw_rows("A", 60, 2000, collected="2026-07-20T00:00:00Z")
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    _analysis(tmp_path, "A", total_rows=60)          # без meta вовсе
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == [("raw_tiktok", "A")]


def test_min_new_rows_is_configurable(tmp_path):
    rows = _raw_rows("A", 20, 2000, collected="2026-07-01T00:00:00Z")
    rows += _raw_rows("A", 10, 2000, collected="2026-07-20T00:00:00Z", start=20)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    _analysis(tmp_path, "A", total_rows=30,
              meta={"generated_at": "2026-07-15T00:00:00+00:00", "since": None})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == []                     # 10 < дефолтных 40
    runner.config = {"dashboard": {"fanout": {"min_new_rows": 10}}}
    assert runner._eligible_niches() == [("raw_tiktok", "A")]   # порог опущен


def test_legacy_growth_key_warns_and_does_not_gate(caplog):
    # Снятый ключ не должен молча выглядеть работающей настройкой.
    runner = StageRunner(FakeSheets({"run_log": []}),
                         {"dashboard": {"fanout": {"growth": 1.5}}},
                         http_post=lambda u: None, run_command=lambda a: (0, ""))
    with caplog.at_level(logging.WARNING, logger="cf.dashboard.runner"):
        params = runner._fanout_params()
    assert "growth" not in params                  # в параметрах ключа нет
    assert params["min_new_rows"] == 40
    assert any("growth больше не используется" in r.message for r in caplog.records)


def test_unreadable_analysis_does_not_gate_the_niche(tmp_path):
    # Битый JSON анализа не должен ронять фан-аут и не должен глушить нишу.
    rows = _raw_rows("A", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    (tmp_path / "2026-07-15-raw_tiktok-A-analysis.json").write_text(
        "{не json", encoding="utf-8")
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == [("raw_tiktok", "A")]


# ── 2. Падение одной ниши не валит прогон ─────────────────────────────────────

def test_fanout_one_niche_failure_does_not_kill_run(tmp_path):
    rows = _raw_rows("X", 20, 2000) + _raw_rows("Y", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    run_command, _ = _dispatch({
        "X": (0, _niche_out("X", formulas=2)),
        "Y": (1, _niche_out("Y", formulas=0, status="ok")),
    })
    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "warn"
    detail = runner.state["factory"]["detail"]
    assert "error 1" in detail
    assert "формул 2" in detail
    titles = [r["title"] for r in runner.reports["factory"]]
    assert any("ниша Y" in t for t in titles)


# ── 3. Сумма формул в detail ─────────────────────────────────────────────────

def test_fanout_detail_counts_formulas(tmp_path):
    rows = _raw_rows("P", 20, 2000) + _raw_rows("Q", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    run_command, _ = _dispatch({
        "P": (0, _niche_out("P", formulas=2)),
        "Q": (0, _niche_out("Q", formulas=1)),
    })
    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "ok"
    detail = runner.state["factory"]["detail"]
    assert "формул 3" in detail
    assert "ok 2" in detail


# ── 4. git — ровно один раз, из раннера, не из воркеров ───────────────────────

def test_fanout_git_called_once_from_runner(tmp_path):
    rows = _raw_rows("P", 20, 2000) + _raw_rows("Q", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    run_command, commands = _dispatch({
        "P": (0, _niche_out("P", formulas=1)),
        "Q": (0, _niche_out("Q", formulas=1)),
    })
    runner, git_calls = _make(sheets, run_command, tmp_path)
    runner.run_sync("factory")
    assert len(git_calls) == 1
    # агенты никогда не коммитят: ни один вызов run_command не начинается с git
    assert all(str(argv[0]) != "git" for argv in commands)


# ── P5.11: шаг 5 гоняет formula-guard раньше formula-perf ────────────────────

def test_fanout_runs_formula_guard_before_formula_perf(tmp_path):
    # Порядок важен: гвардия может убрать формулу из индекса, затем витрина
    # own_performance считает только по выжившим approved-формулам.
    sheets = FakeSheets({"run_log": []})
    run_command, commands = _dispatch({})
    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    cf_cmds = [argv[-1] for argv in commands
               if len(argv) >= 4 and argv[1] == "-m" and argv[2] == "cf"]
    assert "formula-guard" in cf_cmds and "formula-perf" in cf_cmds
    assert cf_cmds.index("formula-guard") < cf_cmds.index("formula-perf")


# ── 5. Пустая очередь — успех, брифы всё равно идут ──────────────────────────

def test_fanout_empty_queue_is_success(tmp_path):
    # Нет НОВЫХ raw-строк -> очередь анализа пуста, но уже готовая тема продолжает
    # производить сценарии: пустая очередь анализа не должна гасить генератор.
    sheets = FakeSheets({"run_log": [], "prompt_versions": ready_versions()})
    run_command, commands = _dispatch({})
    runner, _ = _make(sheets, run_command, tmp_path)
    make_ready_theme(runner.root)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "ok"
    assert "очередь ниш пуста" in runner.state["factory"]["detail"]
    prompts = [argv[2] for argv in commands if len(argv) > 2]
    assert "/cf-generate-briefs" in prompts
    assert "/cf-review-brief --pending" in prompts


# ── 6. Обе raw-вкладки недоступны — warn, не тихий успех ─────────────────────

def test_fanout_all_raw_tabs_unreadable_is_warn(tmp_path):
    class NoRawSheets(FakeSheets):
        def read_rows(self, tab_key):
            if tab_key.startswith("raw_"):
                raise ConnectionError("sheets down")
            return super().read_rows(tab_key)

    sheets = NoRawSheets({"run_log": []})
    run_command, _ = _dispatch({})
    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "warn"
    assert "raw-вкладки недоступны" in runner.state["factory"]["detail"]


# ── P1.21: классификация обеих raw-вкладок ───────────────────────────────────

def test_fanout_classifies_both_raw_tabs(tmp_path):
    # Классификация обязана пройти по КАЖДОЙ raw-вкладке: иначе строки Instagram
    # навсегда остаются без niche и не попадают в очередь ниш.
    sheets = FakeSheets({"run_log": []})
    run_command, commands = _dispatch({})
    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    prompts = [argv[2] for argv in commands if len(argv) > 2]
    assert "/cf-classify-niche raw_tiktok" in prompts
    assert "/cf-classify-niche raw_instagram" in prompts


def test_fanout_one_raw_tab_classification_failure_degrades_only_that_tab(tmp_path):
    # Падение классификации одной вкладки деградирует только её (заметка в problems),
    # вторая вкладка всё равно классифицируется, прогон не рушится.
    sheets = FakeSheets({"run_log": []})
    commands = []

    def run_command(argv):
        commands.append(argv)
        prompt = argv[2] if len(argv) > 2 else ""
        if prompt == "/cf-classify-niche raw_instagram":
            return (1, _claude_out("сбой классификации"))
        return (0, _claude_out("готово"))

    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "warn"
    detail = runner.state["factory"]["detail"]
    assert "классификация Instagram не удалась" in detail
    prompts = [argv[2] for argv in commands if len(argv) > 2]
    assert "/cf-classify-niche raw_tiktok" in prompts   # вторая вкладка не пропущена


# ── 7. Реальный _git_commit скоупится на formulas/ ───────────────────────────

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def test_git_commit_commits_only_formulas(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@test")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "formulas").mkdir()
    (tmp_path / "formulas" / "x.json").write_text("{}", encoding="utf-8")
    (tmp_path / "stray.txt").write_text("wip", encoding="utf-8")
    _git(tmp_path, "add", "stray.txt")   # посторонний staged-файл продюсера

    runner = StageRunner(FakeSheets({"run_log": []}), {},
                         http_post=lambda u: None, run_command=lambda a: (0, "{}"))
    runner._git_commit("formulas: тест", cwd=tmp_path)

    committed = _git(tmp_path, "show", "--name-only", "--format=").stdout.split()
    assert committed == ["formulas/x.json"]  # stray.txt в коммит не попал
    staged = _git(tmp_path, "diff", "--cached", "--name-only").stdout.split()
    assert staged == ["stray.txt"]           # и остался staged


# ── 8. P1.9: сбой git поднимается в problems звена, не только в лог ────────────

def test_fanout_git_failure_surfaces_in_problems(tmp_path):
    rows = _raw_rows("P", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    run_command, _ = _dispatch({"P": (0, _niche_out("P", formulas=1))})
    runner = StageRunner(sheets, {}, http_post=lambda u: None,
                         run_command=run_command, analysis_dir=Path(tmp_path),
                         locks_dir=tmp_path)
    # _git_commit вернул строку-проблему (как при ненулевом commit) — она обязана
    # оказаться в problems → detail звена, а звено уйти в warn (не тихий ok).
    runner._git_commit = lambda msg: "git-коммит формул не удался"
    runner._agent_logged = lambda agent, since: True
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "warn"
    assert "git-коммит формул не удался" in runner.state["factory"]["detail"]


def test_git_commit_returns_problem_on_commit_failure(tmp_path, monkeypatch):
    # Реальный _git_commit: commit вернул ненулевой код → метод возвращает строку-проблему.
    def fake_run(argv, **kwargs):
        class R:
            stdout = ""
            stderr = "boom"
        r = R()
        if "diff" in argv:
            r.returncode = 1        # есть staged-изменения → идём коммитить
        elif "commit" in argv:
            r.returncode = 1        # сам commit падает
        else:
            r.returncode = 0        # add
        return r

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    runner = StageRunner(FakeSheets({"run_log": []}), {}, http_post=lambda u: None,
                         run_command=lambda a: (0, "{}"), locks_dir=tmp_path)
    problem = runner._git_commit("formulas: тест", cwd=tmp_path)
    assert problem and "git" in problem.lower()


def test_git_commit_returns_none_on_success(tmp_path):
    # Пустой индекс (нечего коммитить) — не ошибка: возврат None, problems пуст.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@test")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "formulas").mkdir()
    runner = StageRunner(FakeSheets({"run_log": []}), {}, http_post=lambda u: None,
                         run_command=lambda a: (0, "{}"), locks_dir=tmp_path)
    assert runner._git_commit("formulas: пусто", cwd=tmp_path) is None


# ── 9. P1.9: кросс-процессный захват звена ────────────────────────────────────

def test_stage_claim_refused_when_stage_lock_held_by_other_process(tmp_path):
    # A захватывает звено (держит stage-factory.lock на своём fd). Второй дескриптор
    # того же файла (эмуляция второго процесса/раннера) не может взять лок, пока A
    # держит; после _finish A освобождает — второй дескриптор проходит.
    a = StageRunner(FakeSheets({"run_log": []}), {}, http_post=lambda u: None,
                    run_command=lambda x: (0, "{}"), locks_dir=tmp_path)
    assert a._claim("factory") is True
    other = ProcessLock(tmp_path / "stage-factory.lock")
    assert other.acquire() is False           # занято раннером A
    a._finish("factory", detail="готово")     # штатный хвост освобождает лок звена
    assert other.acquire() is True            # звено свободно
    other.release()


def test_second_runner_claim_refused_same_process(tmp_path):
    # Два разных экземпляра StageRunner в одном процессе, общий locks_dir: пока A
    # держит звено, claim у B отклоняется (ОС-лок сериализует экземпляры).
    a = StageRunner(FakeSheets({"run_log": []}), {}, http_post=lambda u: None,
                    run_command=lambda x: (0, "{}"), locks_dir=tmp_path)
    b = StageRunner(FakeSheets({"run_log": []}), {}, http_post=lambda u: None,
                    run_command=lambda x: (0, "{}"), locks_dir=tmp_path)
    assert a._claim("factory") is True
    assert b._claim("factory") is False
    a._finish("factory", detail="готово")
    assert b._claim("factory") is True
    b._finish("factory", detail="готово")


# ── P2.6: контракт NICHE_RESULT ──────────────────────────────────────────────

def test_parse_niche_result_returns_none_on_broken_or_missing():
    # None-ветка _parse_niche_result: строки нет / JSON битый / пустой / None-вход
    assert runner_mod._parse_niche_result("прогон без строки-сводки") is None
    assert runner_mod._parse_niche_result("NICHE_RESULT: {битый json") is None
    assert runner_mod._parse_niche_result("") is None
    assert runner_mod._parse_niche_result(None) is None
    # валидная строка — dict (для контраста)
    assert runner_mod._parse_niche_result('NICHE_RESULT: {"status": "ok"}') == {"status": "ok"}


def test_fanout_status_success_is_treated_as_ok(tmp_path):
    # Агент печатает status "success" (как у log-run) — трактуем как синоним "ok".
    rows = _raw_rows("S", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    run_command, _ = _dispatch({"S": (0, _niche_out("S", formulas=2, status="success"))})
    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "ok"
    detail = runner.state["factory"]["detail"]
    assert "ok 1" in detail
    assert "формул 2" in detail


def test_fanout_broken_niche_result_at_exit0_is_error(tmp_path):
    # Битый/отсутствующий NICHE_RESULT при коде выхода 0 — ошибка ниши, НЕ тихий ok.
    rows = _raw_rows("Z", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    broken = _claude_out("прогон ниши Z\nNICHE_RESULT: {битый")
    run_command, _ = _dispatch({"Z": (0, broken)})
    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "warn"
    detail = runner.state["factory"]["detail"]
    assert "error 1" in detail
    assert "ok 0" in detail


def test_fanout_missing_niche_result_at_exit0_is_error(tmp_path):
    # Нет строки NICHE_RESULT вовсе при коде 0 — тоже ошибка (не formulas=0/ok).
    rows = _raw_rows("Z", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    run_command, _ = _dispatch({"Z": (0, _claude_out("прогон завершён, но сводки нет"))})
    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "warn"
    assert "error 1" in runner.state["factory"]["detail"]


# ── P2.8: единый порог очереди фан-аута и профиля ────────────────────────────

def test_fanout_queue_threshold_equals_profile_default(tmp_path):
    from cf.cli import build_parser
    from cf.dashboard.runner import PROFILE_MIN_ROWS

    # 15 дедуп-строк (views>=1000) НЕ ставятся в очередь (ниже единого порога)
    small = FakeSheets({"run_log": [], "raw_tiktok": _raw_rows("small", 15, 2000)})
    runner_s, _ = _make(small, lambda a: (0, _claude_out("x")), tmp_path)
    assert runner_s._eligible_niches() == []

    # 20 дедуп-строк — ставятся
    big = FakeSheets({"run_log": [], "raw_tiktok": _raw_rows("big", 20, 2000)})
    runner_b, _ = _make(big, lambda a: (0, _claude_out("x")), tmp_path)
    assert runner_b._eligible_niches() == [("raw_tiktok", "big")]

    # порог — одна константа, ведущая и очередь, и дефолт `cf profile --min-rows`
    assert PROFILE_MIN_ROWS == 20
    ns = build_parser().parse_args(["profile", "raw_tiktok"])
    assert ns.min_rows == PROFILE_MIN_ROWS


# ── P2.9: валидация имени ниши + падение воркера ──────────────────────────────

def test_fanout_invalid_niche_name_errors_without_launching_claude(tmp_path):
    # Имя с пробелом/точкой с запятой ломает разбор аргументов слэш-команды:
    # ниша деградирует в error БЕЗ запуска claude, соседняя проходит.
    rows = _raw_rows("обувь x; y", 20, 2000) + _raw_rows("good", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    run_command, commands = _dispatch({"good": (0, _niche_out("good", formulas=1))})
    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    # claude ни разу не звался с битым именем
    niche_prompts = [argv[2] for argv in commands
                     if len(argv) > 2 and str(argv[2]).startswith("/cf-niche-run")]
    assert all("обувь" not in p for p in niche_prompts)
    assert any("good" in p for p in niche_prompts)
    detail = runner.state["factory"]["detail"]
    assert "error 1" in detail          # битое имя ушло в error
    assert "ok 1" in detail             # good отработала


def test_fanout_worker_exception_degrades_only_that_niche(tmp_path):
    # run_command бросает TimeoutExpired на одной нише → она error, остальные завершаются.
    rows = _raw_rows("X", 20, 2000) + _raw_rows("Y", 20, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    commands = []

    def run_command(argv):
        commands.append(argv)
        prompt = argv[2] if len(argv) > 2 else ""
        if isinstance(prompt, str) and prompt.startswith("/cf-niche-run"):
            niche = prompt.split()[-1]
            if niche == "X":
                raise subprocess.TimeoutExpired(cmd=argv, timeout=1800)
            return (0, _niche_out("Y", formulas=2))
        return (0, _claude_out("готово"))

    runner, _ = _make(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True     # прогон завершился, не упал целиком
    assert runner.state["factory"]["status"] == "warn"
    detail = runner.state["factory"]["detail"]
    assert "error 1" in detail                    # X упала
    assert "формул 2" in detail                   # Y всё равно посчитана


# ── M20 (аудит 2026-07-24): одна ниша — один прогон ──────────────────────────

def test_fanout_queue_dedupes_same_niche_across_tabs(tmp_path):
    # ниша в обеих вкладках → в очереди один прогон, вкладка с бОльшим материалом
    sheets = FakeSheets({"run_log": [],
                         "raw_tiktok": _raw_rows("fitness", 20, 2000),
                         "raw_instagram": _raw_rows("fitness", 25, 2000)})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == [("raw_instagram", "fitness")]


def test_fanout_queue_same_niche_tie_prefers_tiktok(tmp_path):
    sheets = FakeSheets({"run_log": [],
                         "raw_tiktok": _raw_rows("fitness", 20, 2000),
                         "raw_instagram": _raw_rows("fitness", 20, 2000)})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == [("raw_tiktok", "fitness")]


def test_fanout_queue_different_niches_keep_their_tabs(tmp_path):
    sheets = FakeSheets({"run_log": [],
                         "raw_tiktok": _raw_rows("A", 20, 2000),
                         "raw_instagram": _raw_rows("B", 20, 2000)})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert sorted(runner._eligible_niches()) == [("raw_instagram", "B"),
                                                 ("raw_tiktok", "A")]


def test_fanout_queue_skips_rows_missing_critical_fields(tmp_path):
    # M19: строка без posted_at не считается материалом — очередь совпадает с
    # критерием профайлера, иначе ниша вечно жгла бы claude на insufficient_data.
    rows = _raw_rows("A", 25, 2000)
    for r in rows[:10]:
        r["posted_at"] = ""
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    assert runner._eligible_niches() == []                 # 15 чистых < 20


# ── 10. Холостой прогон claude-звена виден (аудит блокировки 2026-07-24) ───────
# Заблокированный permission-системой агент «вежливо» отчитывался текстом и
# выходил кодом 0 — звено ложилось в Run Log как success. Два детектора:
# stderr-маркер недоверенного воркспейса и след агента в CF Run Log (правило №6).


def _idle_runner(sheets, run_command, tmp_path):
    """Раннер БЕЗ заглушки _agent_logged — проверяется настоящий контракт."""
    runner = StageRunner(sheets, {}, http_post=lambda u: None,
                         run_command=run_command, analysis_dir=Path(tmp_path),
                         locks_dir=tmp_path)
    runner._git_commit = lambda msg: None
    return runner


def test_fanout_flags_claude_run_without_runlog_trace(tmp_path):
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    runner = _idle_runner(
        sheets, lambda argv: (0, _claude_out("вежливый отчёт о блокировке")), tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "warn"
    detail = runner.state["factory"]["detail"]
    assert "прогон холостой" in detail
    assert "niche-classifier" in detail
    # звено легло в Run Log как insufficient_data, не success
    logged = [r for r in sheets.tables["run_log"] if r.get("agent") == "dashboard-factory"]
    assert [r["status"] for r in logged] == ["insufficient_data"]


def test_fanout_contract_passes_when_agents_leave_runlog_trace(tmp_path):
    from cf.runlog import log_run
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    agent_by_prompt = {"/cf-classify-niche raw_tiktok": "niche-classifier",
                       "/cf-classify-niche raw_instagram": "niche-classifier",
                       "/cf-generate-briefs": "brief-generator",
                       "/cf-review-brief --pending": "brief-reviewer"}

    def run_command(argv):
        prompt = argv[2] if len(argv) > 2 else ""
        agent = agent_by_prompt.get(prompt)
        if agent:  # агент честно оставляет след, как велит правило №6
            log_run(sheets, agent=agent, status="success", trigger_type="cli")
        return (0, _claude_out("готово"))

    runner = _idle_runner(sheets, run_command, tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"] == {"status": "ok", "detail": "очередь ниш пуста"}


def test_fanout_untrusted_workspace_surfaces_once_and_skips_runlog_check(tmp_path):
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    warn = ("Ignoring 44 permissions.allow entries from .claude/settings.json: "
            "this workspace has not been trusted.")
    runner = _idle_runner(
        sheets, lambda argv: (0, _claude_out("вежливый отчёт"), warn), tmp_path)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "warn"
    detail = runner.state["factory"]["detail"]
    assert runner_mod.UNTRUSTED_PROBLEM in detail
    assert detail.count("воркспейс не доверен") == 1   # дедуп между 4 вызовами claude
    assert "прогон холостой" not in detail             # причина названа — Run Log не тревожим
    # каждый отчёт помечен предупреждением для оператора
    assert all(r["text"].startswith("⚠") for r in runner.reports["factory"])


def test_run_niche_untrusted_workspace_forces_error(tmp_path):
    sheets = FakeSheets({"run_log": []})
    warn = "… this workspace has not been trusted."
    runner = _idle_runner(
        sheets, lambda argv: (0, _niche_out("A", formulas=3), warn), tmp_path)
    res = runner._run_niche("factory", "raw_tiktok", "A")
    # самоотчёту NICHE_RESULT (formulas=3) не верим: cf-команды были заблокированы
    assert res["status"] == "error" and res["formulas"] == 0
    assert runner.reports["factory"][-1]["text"].startswith("⚠")


def test_agent_logged_matches_only_fresh_rows_of_that_agent(tmp_path):
    from cf.runlog import log_run
    sheets = FakeSheets({"run_log": []})
    runner = _idle_runner(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    log_run(sheets, agent="niche-classifier", status="success")
    assert len(sheets.tables["run_log"]) == 1
    assert runner._agent_logged("niche-classifier", "2020-01-01T00:00:00+00:00") is True
    assert runner._agent_logged("brief-generator", "2020-01-01T00:00:00+00:00") is False
    # строка старше окна вызова не считается следом текущего прогона
    assert runner._agent_logged("niche-classifier", "9999-01-01T00:00:00+00:00") is False


def test_agent_logged_skips_check_when_runlog_unreadable(tmp_path):
    sheets = FakeSheets({"run_log": []}, fail_read_tabs={"run_log": ConnectionError("down")})
    runner = _idle_runner(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    # best-effort: сбой Sheets не должен ронять звено ложным «холостым прогоном»
    assert runner._agent_logged("niche-classifier", "2020-01-01T00:00:00+00:00") is True


# ── 11. Правки по адверсариальному ревью фикса ────────────────────────────────


def test_run_niche_without_runlog_trace_is_error(tmp_path):
    # правило №6 и для ниш: чистый NICHE_RESULT без следа niche-pipeline в Run Log
    # не выдаётся за успех
    sheets = FakeSheets({"run_log": []})
    runner = _idle_runner(sheets, lambda argv: (0, _niche_out("A", formulas=2)), tmp_path)
    res = runner._run_niche("factory", "raw_tiktok", "A")
    assert res["status"] == "error"
    assert "не оставил след" in runner.reports["factory"][-1]["text"]


def test_run_niche_with_runlog_trace_is_ok(tmp_path):
    from cf.runlog import log_run
    sheets = FakeSheets({"run_log": []})

    def run_command(argv):
        log_run(sheets, agent="niche-pipeline", status="success", trigger_type="cli")
        return (0, _niche_out("A", formulas=2))

    runner = _idle_runner(sheets, run_command, tmp_path)
    res = runner._run_niche("factory", "raw_tiktok", "A")
    assert res["status"] == "ok" and res["formulas"] == 2


def test_claude_empty_stdout_reports_stderr(tmp_path):
    # регресс ревью: раньше «stdout or stderr» показывал текст падения claude —
    # фолбэк сохранён и на claude-путях
    sheets = FakeSheets({"run_log": []})
    runner = _idle_runner(
        sheets, lambda argv: (1, "", "claude: fatal: config broken"), tmp_path)
    problems = []
    runner._fanout_claude("factory", "/cf-generate-briefs", "генерация брифов", problems)
    assert runner.reports["factory"][-1]["text"] == "claude: fatal: config broken"
    assert problems == ["генерация брифов не удалась"]


def test_untrusted_marker_survives_giant_report(tmp_path):
    # _add_report режет текст С НАЧАЛА — префикс ⚠ не должен срезаться на длинном выводе
    from cf.dashboard.runner import REPORT_TEXT_LIMIT
    sheets = FakeSheets({"run_log": []})
    giant = "x" * (REPORT_TEXT_LIMIT + 5000)
    warn = "this workspace has not been trusted"
    runner = _idle_runner(sheets, lambda argv: (0, _claude_out(giant), warn), tmp_path)
    problems = []
    runner._fanout_claude("factory", "/cf-generate-briefs", "генерация брифов", problems)
    assert runner.reports["factory"][-1]["text"].startswith("⚠")


# ── 12. Нецелевые ниши исключаются из очереди производства ────────────────────


def test_eligible_niches_skips_excluded(tmp_path):
    # женская-мода набирает материал, но она в exclude_niches → в очередь не идёт;
    # целевая мужские-образы с тем же объёмом остаётся
    rows = _raw_rows("женская-мода", 25, 2000) + _raw_rows("мужские-образы", 25, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    runner = StageRunner(
        sheets, {"dashboard": {"fanout": {"exclude_niches": ["женская-мода"]}}},
        http_post=lambda u: None, run_command=lambda a: (0, _claude_out("x")),
        analysis_dir=Path(tmp_path), locks_dir=tmp_path)
    assert runner._eligible_niches() == [("raw_tiktok", "мужские-образы")]


def test_eligible_niches_no_exclude_by_default(tmp_path):
    # без exclude_niches (дефолт []) поведение прежнее — ниша проходит
    rows = _raw_rows("женская-мода", 25, 2000)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": rows})
    runner = StageRunner(sheets, {}, http_post=lambda u: None,
                         run_command=lambda a: (0, _claude_out("x")),
                         analysis_dir=Path(tmp_path), locks_dir=tmp_path)
    assert runner._eligible_niches() == [("raw_tiktok", "женская-мода")]


# ── Длительность звена в Run Log (аудит 2026-07-26) ──────────────────────────

def test_stage_run_logs_real_started_at(tmp_path):
    # Строка dashboard-<звено> — единственный машинный источник, знающий полную
    # длительность звена; до правки она писалась с нулевой длительностью.
    sheets = FakeSheets({"run_log": [], "raw_tiktok": []})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("готово")), tmp_path)
    runner.run_sync("factory")
    rows = [r for r in sheets.read_rows("run_log") if r["agent"] == "dashboard-factory"]
    assert rows, "звено не оставило строку в Run Log"
    assert rows[-1]["started_at"] <= rows[-1]["completed_at"]
    assert rows[-1]["started_at"]      # штамп проставлен, а не пуст


def test_stage_started_stamp_is_popped_after_finish(tmp_path):
    # Висящий ключ приписал бы СЛЕДУЮЩЕМУ прогону чужой старт.
    sheets = FakeSheets({"run_log": [], "raw_tiktok": []})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("готово")), tmp_path)
    runner.run_sync("factory")
    assert "factory" not in runner._stage_started


def test_agent_logged_checks_completed_at_not_started_at(tmp_path):
    # Защитная правка: с --started-at агент вправе указать старт РАНЬШЕ since
    # (перезапуск, долгая работа). Раньше это дало бы ложное «след не оставлен»
    # и перевело нишу в error на ровном месте.
    sheets = FakeSheets({"run_log": [{
        "agent": "niche-pipeline",
        "started_at": "2026-07-26T09:00:00+00:00",     # раньше since
        "completed_at": "2026-07-26T09:40:00+00:00",   # позже since
        "status": "success"}]})
    runner, _ = _make(sheets, lambda argv: (0, _claude_out("x")), tmp_path)
    runner._agent_logged = StageRunner._agent_logged.__get__(runner)
    assert runner._agent_logged("niche-pipeline", "2026-07-26T09:30:00+00:00")


# ── Этап 2 кадров (тикет 07): шаг vision в прогоне ниши ──────────────────────

VISION_OUT = ("очередь ниши\n"
              "queued=3 done=2 failed=1 deferred=0 engine=fake model=fake "
              "frames=off niche=A")


def _vision_runner(sheets, tmp_path, config, vision_result=(0, VISION_OUT)):
    """Раннер с диспетчером: cf vision -> vision_result, claude -> ниша ok."""
    commands = []

    def run_command(argv):
        commands.append(argv)
        if "vision" in argv and argv[0] == sys.executable:
            return vision_result
        return (0, _niche_out("A", formulas=1))

    runner = StageRunner(sheets, config, http_post=lambda u: None,
                         run_command=run_command, analysis_dir=Path(tmp_path))
    runner._git_commit = lambda msg: None
    runner._agent_logged = lambda agent, since: True
    return runner, commands


def test_in_cycle_off_or_absent_niche_run_calls_no_vision(tmp_path):
    # Строится выключенным: пауза завода не трогается, поведение прежнее.
    for config in ({}, {"vision": {"in_cycle": False}}):
        sheets = FakeSheets({"run_log": [], "raw_tiktok": []})
        runner, commands = _vision_runner(sheets, tmp_path, config)
        res = runner._run_niche("factory", "raw_tiktok", "A", [])
        assert res["status"] == "ok"
        assert all("vision" not in argv for argv in commands)


def test_in_cycle_on_vision_runs_before_analysis_with_feed(tmp_path):
    sheets = FakeSheets({"run_log": [], "raw_tiktok": []})
    runner, commands = _vision_runner(sheets, tmp_path,
                                      {"vision": {"in_cycle": True}})
    problems = []
    res = runner._run_niche("factory", "raw_tiktok", "A", problems)
    assert res["status"] == "ok"
    # vision зовётся ДО платного анализа, по нише и вкладке очереди
    assert commands[0][:5] == [sys.executable, "-u", "-m", "cf", "vision"]
    assert "--tab" in commands[0] and "raw_tiktok" in commands[0]
    assert "--niche" in commands[0] and "A" in commands[0]
    assert commands[1][0] == "claude"
    # итог vision — в ленте прогона ниши, числа совпадают с выводом vision
    vision_reports = [r for r in runner.reports["factory"]
                      if "визуал" in r["title"]]
    assert vision_reports
    assert "queued=3 done=2 failed=1 deferred=0" in vision_reports[0]["text"]
    assert problems == []


def test_vision_failure_does_not_block_analysis(tmp_path):
    # Зеркало правила «по непрочитанным данным не гасим»: отказ vision не
    # меняет статус анализа ниши, причина уходит в ленту и problems.
    sheets = FakeSheets({"run_log": [], "raw_tiktok": []})
    runner, commands = _vision_runner(
        sheets, tmp_path, {"vision": {"in_cycle": True}},
        vision_result=(1, "движок остановил прогон: weekly limit"))
    problems = []
    res = runner._run_niche("factory", "raw_tiktok", "A", problems)
    assert res["status"] == "ok"                  # анализ НЕ заблокирован
    assert res["formulas"] == 1
    assert any("vision" in p for p in problems)
    vision_reports = [r for r in runner.reports["factory"]
                      if "визуал" in r["title"]]
    assert "weekly limit" in vision_reports[0]["text"]


def test_vision_crash_isolated_from_niche_run(tmp_path):
    sheets = FakeSheets({"run_log": [], "raw_tiktok": []})
    commands = []

    def run_command(argv):
        commands.append(argv)
        if "vision" in argv and argv[0] == sys.executable:
            raise RuntimeError("подпроцесс не стартовал")
        return (0, _niche_out("A", formulas=1))

    runner = StageRunner(sheets, {"vision": {"in_cycle": True}},
                         http_post=lambda u: None, run_command=run_command,
                         analysis_dir=Path(tmp_path))
    runner._git_commit = lambda msg: None
    runner._agent_logged = lambda agent, since: True
    problems = []
    res = runner._run_niche("factory", "raw_tiktok", "A", problems)
    assert res["status"] == "ok"
    assert any("vision" in p for p in problems)
