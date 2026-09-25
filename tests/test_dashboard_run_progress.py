"""Per-run прогресс ленты конвейера в раннере (Фаза 2б).

Слой additive: инструментирование run_fanout_sync/_run_cli_sync ведёт счётчики
шагов, не трогая state/локи звена. Проверяем: реальный done/total, обнуление за
прогон + last_count, персист/восстановление, eta из медиан, collect у raw.
"""

import json
import threading
import time
from pathlib import Path

from cf.dashboard.progress import build_timeline
from cf.dashboard.runner import StageRunner
from tests.fakes import FakeSheets, make_ready_theme, ready_versions
from tests.test_runner_fanout import _claude_out, _niche_out, _raw_rows


def _runner(sheets, run_command, tmp_path, progress_path=None, now_fn=None,
            locks_dir=None):
    r = StageRunner(sheets, {}, http_post=lambda u: None, run_command=run_command,
                    analysis_dir=Path(tmp_path), progress_path=progress_path,
                    now_fn=now_fn, locks_dir=locks_dir)
    r._git_commit = lambda msg: None          # никаких реальных коммитов в тесте
    r._agent_logged = lambda agent, since: True
    return r


class _Clock:
    """Монотонные часы теста: каждый вызов двигает время на `step` секунд."""

    def __init__(self, step=1.0, start=1000.0):
        self.step, self.value = step, start

    def __call__(self):
        self.value += self.step
        return self.value


def _timeline_row(runner, key):
    """Строка ленты для шага — подписи проверяем через тот же путь, что и UI."""
    rows = build_timeline(runner.run_progress, {}, now=None)
    return next(r for r in rows if r["key"] == key)


def _dispatch(niche_map):
    def run_command(argv):
        prompt = argv[2] if len(argv) > 2 else ""
        if isinstance(prompt, str) and prompt.startswith("/cf-niche-run"):
            return niche_map[prompt.split()[-1]]
        return (0, _claude_out("готово"))
    return run_command


def _factory_sheets():
    return FakeSheets({"run_log": [], "raw_instagram": [],
                       "prompt_versions": ready_versions(),
                       "raw_tiktok": _raw_rows("D", 20, 2000)})


def test_fanout_run_fills_per_run_progress(tmp_path):
    r = _runner(_factory_sheets(), _dispatch({"D": (0, _niche_out("D", formulas=2))}),
                tmp_path)
    assert r.run_sync("factory") is True
    p = r.run_progress
    assert p["classify"]["status"] == "done" and p["classify"]["total"] == 2
    assert p["analyze"]["status"] == "done"
    assert p["analyze"]["done"] == 1 and p["analyze"]["total"] == 1   # одна ниша в очереди
    assert p["analyze"]["count"] == 2                                 # формул за прогон
    assert p["briefs"]["status"] == "done"
    assert p["review"]["status"] == "done"
    assert p["collect"]["status"] == "idle"          # raw не запускали — шаг не трогали


def test_classify_marks_count_incomplete_when_a_raw_tab_is_unreadable(tmp_path):
    # Sheets отдаёт одну вкладку и падает на второй: счётчик обязан признаться,
    # а звено — уйти в warn с внятной причиной, а не отрапортовать успех.
    class HalfBroken(FakeSheets):
        def read_rows(self, tab):
            if tab == "raw_instagram":
                raise ConnectionError("sheets 503")
            return super().read_rows(tab)

    sheets = HalfBroken({"run_log": [], "raw_instagram": [],
                         "raw_tiktok": _raw_rows("D", 20, 2000)})
    r = _runner(sheets, _dispatch({"D": (0, _niche_out("D", formulas=1))}), tmp_path)
    assert r.run_sync("factory") is True
    st = r.run_progress["classify"]
    assert st["left"] is None and st["left_unknown"] is True
    assert _timeline_row(r, "classify")["count_label"] == "не удалось посчитать"
    assert "счёт разметки неполный" in r.state["factory"]["detail"]


def test_fanout_reads_each_raw_tab_twice_not_three_times(tmp_path):
    # На прогон было 6 полных чтений самых больших вкладок системы: по два на
    # счётчик разметки (до/после) и ещё по одному на очередь ниш. Снимок «после»
    # и есть вход очереди: между ними raw никто не пишет — мьютекс raw↔factory
    # не пускает сбор во время фан-аута.
    reads = []

    class Counting(FakeSheets):
        def read_rows(self, tab):
            reads.append(tab)
            return super().read_rows(tab)

    sheets = Counting({"run_log": [], "raw_instagram": [],
                       "raw_tiktok": _raw_rows("D", 20, 2000)})
    r = _runner(sheets, _dispatch({"D": (0, _niche_out("D", formulas=1))}), tmp_path)
    assert r.run_sync("factory") is True
    assert reads.count("raw_tiktok") == 2
    assert reads.count("raw_instagram") == 2


def test_fanout_resets_producing_counters_and_keeps_last_run(tmp_path):
    r = _runner(_factory_sheets(), _dispatch({"D": (0, _niche_out("D", formulas=2))}),
                tmp_path)
    r.run_sync("factory")
    first = r.run_progress["analyze"]["count"]
    assert first == 2
    r.run_sync("factory")                            # второй прогон обнуляет за прогон
    assert r.run_progress["analyze"]["last_count"] == first   # прошлый прогон сохранён
    assert r.run_progress["analyze"]["count"] == first        # тот же dispatch → снова 2


def test_run_progress_persists_and_restores_last_run(tmp_path):
    ppath = tmp_path / "progress.json"
    r = _runner(_factory_sheets(), _dispatch({"D": (0, _niche_out("D", formulas=2))}),
                tmp_path, progress_path=ppath)
    r.run_sync("factory")
    assert ppath.exists()
    r2 = _runner(_factory_sheets(), lambda a: (0, ""), tmp_path, progress_path=ppath)
    assert r2.run_progress["analyze"]["count"] == 2       # idle-число прошлого прогона
    assert r2.run_progress["analyze"]["status"] == "done"


def test_run_progress_restore_marks_interrupted_not_idle(tmp_path):
    # Прогон прошлого процесса мёртв, но «мёртв» ≠ «не начинался»: после рестарта
    # шаг помечается interrupted, иначе недозаполненная полоска молча висит и
    # читается как зависшая работа (инцидент 2026-07-25).
    ppath = tmp_path / "progress.json"
    ppath.write_text(json.dumps({"analyze": {"status": "running", "count": 5,
                     "last_count": 3, "done": 2, "total": 4}}), encoding="utf-8")
    r = _runner(FakeSheets({"run_log": []}), lambda a: (0, ""), tmp_path,
                progress_path=ppath)
    st = r.run_progress["analyze"]
    assert st["status"] == "interrupted"
    assert st["done"] == 2 and st["total"] == 4           # факт прогона не теряем
    assert st["count"] == 5 and st["last_count"] == 3     # числа прошлого прогона живы
    assert st["started_at"] is None and st["eta_median"] is None
    # бар не анимируется: шаг не «running»
    assert r.any_running() is False


def test_full_cycle_zeroes_every_step_before_the_first_stage(tmp_path):
    # ▶ «Полный цикл» обещает прогон целиком, а полоски сбрасывались по мере входа
    # в звено: пока шёл «Сбор», шаги factory держали зелёные полоски, счётчики и
    # «шёл 19:18» ПРОШЛОГО прогона — это читается как «уже сделано сейчас»
    # (замечание оператора 2026-07-25).
    r = _runner(_factory_sheets(), _dispatch({"D": (0, _niche_out("D", formulas=2))}),
                tmp_path, locks_dir=tmp_path / "locks")
    assert r.run_sync("factory") is True                 # прошлый прогон — с итогами
    assert r.run_progress["briefs"]["status"] == "done"

    inner, snapshot = r.run_command, []

    def spy(argv):
        # первая команда цикла — сбор: к этому моменту лента уже обязана быть в нуле
        if not snapshot:
            snapshot.append({k: dict(v) for k, v in r.run_progress.items()})
        return inner(argv)

    r.run_command = spy
    r.run_cycle_sync()
    # цикл дошёл до конца — иначе снимок мог быть взят на аварийном пути
    # заметка цикла теперь говорит ФАКТ: где встал и чего ждут от вас
    assert r.cycle_note.startswith("цикл прошёл")
    at_start = snapshot[0]
    for key in ("classify", "analyze", "briefs", "review"):
        st = at_start[key]
        assert st["status"] == "idle", key
        assert (st["count"], st["done"], st["total"]) == (0, None, None), key
        assert st["produced"] is None and st["duration"] is None, key
    # полоска и подпись — через тот же путь, что рисует UI
    rows = {row["key"]: row for row in build_timeline(at_start, {}, now=None)}
    assert [rows[k]["fraction"] for k in ("classify", "analyze", "briefs", "review")] \
        == [0.0, 0.0, 0.0, 0.0]
    assert rows["briefs"]["count_label"] == "ждёт"
    # числа прошлого прогона не потеряны — они и есть «last_count» ленты
    assert at_start["analyze"]["last_count"] == 2


def test_progress_begin_sets_eta_from_run_log_median(tmp_path):
    run_log = [{"agent": "brief-generator", "status": "success",
                "started_at": "2026-07-14T12:00:00+00:00",
                "completed_at": "2026-07-14T12:02:00+00:00"}]     # 120s
    sheets = FakeSheets({"run_log": run_log, "raw_tiktok": [], "raw_instagram": []})
    r = _runner(sheets, lambda a: (0, _claude_out("ok")), tmp_path)
    r._progress_begin("factory")
    assert r.run_progress["briefs"]["eta_median"] == 120.0    # из Run Log
    assert r.run_progress["analyze"]["eta_median"] > 0        # без истории — дефолт


def test_raw_stage_tracks_collect_progress_and_leaves_downstream(tmp_path):
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    r = _runner(sheets, lambda argv: (0, "собрано"), tmp_path)
    assert r.run_sync("raw") is True
    collect = r.run_progress["collect"]
    assert collect["status"] == "done"
    assert collect["done"] == 2 and collect["total"] == 2    # tiktok + instagram
    # stats — downstream: производящих шагов нет, прогресс не заводит
    assert r.run_sync("stats") is True
    assert all(r.run_progress[k]["status"] in ("idle", "done")
               for k in r.run_progress)                       # ни одного нового running


# ── Прогресс v2: живые фазы сбора из потокового stdout (спека §7) ─────────────


def _collect_stdout(argv, on_line=None):
    """Фейковый `cf collect <платформа>`: печатает маркеры фаз, как настоящий сбор."""
    platform = argv[-1]
    lines = ["CF_PROGRESS phase=prepare total=3",
             "CF_PROGRESS phase=fetch done=0 total=3",
             "CF_PROGRESS phase=fetch done=1 total=3",
             "CF_PROGRESS phase=fetch done=3 total=3",
             "CF_PROGRESS phase=gate",
             "CF_PROGRESS phase=write",
             f"collect {platform}: success"]
    if on_line is not None:
        for line in lines:
            on_line(line)
    return (0, "\n".join(lines), "")


def test_collect_progress_follows_phases_and_batches(tmp_path):
    # Раньше шаг «Сбор» знал только «платформа началась/кончилась»: бар стоял на 0
    # и прыгал на 50%. Теперь фазы и готовые батчи приходят по ходу.
    seen = []
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})

    def run_command(argv, on_line=None):
        def spy(line):
            on_line(line)
            state = dict(seen_runner[0].run_progress["collect"])
            seen.append((state.get("unit_label"), state.get("phase"),
                         state.get("phase_done"), state.get("phase_total")))
        return _collect_stdout(argv, spy if on_line else None)

    seen_runner = [None]
    r = _runner(sheets, run_command, tmp_path)
    seen_runner[0] = r
    assert r.run_sync("raw") is True

    # первая платформа прошла все фазы, счётчик батчей двигался внутри fetch
    tiktok = [s for s in seen if s[0] == "TikTok"]
    assert [s[1] for s in tiktok][:2] == ["prepare", "fetch"]
    assert (3, 3) in [(s[2], s[3]) for s in tiktok]
    assert "gate" in [s[1] for s in tiktok] and "write" in [s[1] for s in tiktok]
    # и вторая платформа со своим планом фаз (у Instagram он другой)
    assert any(s[0] == "Instagram" for s in seen)
    collect = r.run_progress["collect"]
    assert collect["status"] == "done" and collect["done"] == 2


def test_collect_progress_sets_phase_plan_and_unit_label(tmp_path):
    plans = []
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})

    def run_command(argv, on_line=None):
        plans.append((r.run_progress["collect"]["unit_label"],
                      r.run_progress["collect"]["phase_plan"]))
        return _collect_stdout(argv, on_line)

    r = _runner(sheets, run_command, tmp_path)
    assert r.run_sync("raw") is True
    assert plans == [("TikTok", "tiktok"), ("Instagram", "instagram")]


def test_progress_markers_do_not_leak_into_stage_reports(tmp_path):
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    r = _runner(sheets, _collect_stdout, tmp_path)
    assert r.run_sync("raw") is True
    reports = "\n".join(e["text"] for e in r.reports.get("raw", []))
    assert "CF_PROGRESS" not in reports              # телеметрия не для оператора
    assert "success" in reports                      # а сводка сбора — на месте


def test_old_style_run_command_without_on_line_still_works(tmp_path):
    # инжектированный раннер без поддержки on_line (старые тесты/кастомные раннеры):
    # прогресс остаётся дискретным, но звено работает
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    r = _runner(sheets, lambda argv: (0, "собрано"), tmp_path)
    assert r.run_sync("raw") is True
    assert r.run_progress["collect"]["done"] == 2


def test_typeerror_inside_command_does_not_rerun_collect(tmp_path):
    # Проба «вызвать с on_line и поймать TypeError» неотличима от TypeError,
    # прилетевшего ИЗ ТЕЛА уже выполненной команды: сбор запускался второй раз —
    # второй платный прогон Apify, второй upsert и второй collect-* в Run Log.
    calls = []

    def run_command(argv, on_line=None):
        calls.append(argv)
        raise TypeError("упало внутри команды, а не на арности")

    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    r = _runner(sheets, run_command, tmp_path)
    assert r.run_sync("raw") is False
    assert len(calls) == 2          # по одному разу на платформу, без повторов


def test_interrupted_run_gets_closing_runlog_row_once(tmp_path):
    # Правило №6: у прогона обязан быть след. Процесс умер посреди фан-аута —
    # закрывающей строки dashboard-factory не появилось. Пишем её на старте
    # следующего процесса и нормализуем файл, чтобы рестарты не плодили дубли.
    ppath = tmp_path / "progress.json"
    ppath.write_text(json.dumps({"analyze": {"status": "running", "done": 1,
                                             "total": 2, "count": 0,
                                             "last_count": 0}}), encoding="utf-8")
    sheets = FakeSheets({"run_log": []})
    r = _runner(sheets, lambda a: (0, ""), tmp_path, progress_path=ppath)
    r._startup_log_thread.join(timeout=5)      # запись в Run Log идёт в фоне
    rows = sheets.tables["run_log"]
    assert len(rows) == 1
    assert rows[0]["agent"] == "dashboard-factory"
    assert rows[0]["status"] == "failed"
    assert "прогон прерван" in rows[0]["input_summary"]
    assert "Анализ приёмов" in rows[0]["input_summary"]        # какой шаг не закрылся
    assert r.run_progress["analyze"]["status"] == "interrupted"

    # второй запуск процесса по тому же файлу — новых строк не пишет
    r2 = _runner(FakeSheets({"run_log": []}), lambda a: (0, ""), tmp_path,
                 progress_path=ppath)
    assert r2.run_progress["analyze"]["status"] == "interrupted"
    assert json.loads(ppath.read_text())["analyze"]["status"] == "interrupted"


def test_interrupted_runlog_failure_does_not_block_startup(tmp_path):
    # Sheets недоступен на старте — дашборд всё равно поднимается
    class Broken(FakeSheets):
        def append_row(self, tab, row):
            raise ConnectionError("sheets down")

    ppath = tmp_path / "progress.json"
    ppath.write_text(json.dumps({"collect": {"status": "running", "done": 1,
                                             "total": 2}}), encoding="utf-8")
    r = _runner(Broken({"run_log": []}), lambda a: (0, ""), tmp_path,
                progress_path=ppath)
    r._startup_log_thread.join(timeout=5)      # запись в Run Log идёт в фоне
    assert r.run_progress["collect"]["status"] == "interrupted"


def test_startup_does_not_block_on_sheets_when_closing_interrupted_run(tmp_path):
    # Оператор рестартует сервис именно чтобы разгрести застрявший прогон.
    # Запись следа в Run Log шла синхронно из __init__ — лежачий Sheets
    # задерживал подъём дашборда (uvicorn ещё не слушает, страницы не отдаются).
    gate = threading.Event()

    class Hanging(FakeSheets):
        def append_row(self, tab, row):
            gate.wait(5)                       # «Sheets висит»
            return super().append_row(tab, row)

    ppath = tmp_path / "progress.json"
    ppath.write_text(json.dumps({"collect": {"status": "running"}}), encoding="utf-8")
    started = time.monotonic()
    r = _runner(Hanging({"run_log": []}), lambda a: (0, ""), tmp_path,
                progress_path=ppath)
    assert time.monotonic() - started < 1.0    # конструктор не ждал Sheets
    assert r.run_progress["collect"]["status"] == "interrupted"
    gate.set()
    r._startup_log_thread.join(timeout=5)
    assert len(r.sheets.tables["run_log"]) == 1


# ── Счёт в единицах результата + таймер после финиша (правка оператора 25.07) ──


def _collect_stdout_with_totals(rows_per_platform, new_per_platform):
    """Фейковый сбор, который в конце сообщает добычу — как настоящий после upsert."""
    def run_command(argv, on_line=None):
        lines = ["CF_PROGRESS phase=prepare total=2",
                 "CF_PROGRESS phase=fetch done=2 total=2",
                 "CF_PROGRESS phase=write",
                 f"CF_PROGRESS phase=write rows={rows_per_platform} "
                 f"new={new_per_platform}",
                 f"collect {argv[-1]}: success"]
        if on_line is not None:
            for line in lines:
                on_line(line)
        return (0, "\n".join(lines), "")
    return run_command


def test_collect_counts_reels_across_platforms(tmp_path):
    # «2/2 платформ» — единица обхода машины; оператору нужны ролики, и они
    # складываются по платформам за прогон.
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    r = _runner(sheets, _collect_stdout_with_totals(60, 12), tmp_path)
    assert r.run_sync("raw") is True
    collect = r.run_progress["collect"]
    assert collect["produced"] == 120 and collect["produced_new"] == 24
    row = _timeline_row(r, "collect")
    assert row["count_label"] == "120 роликов · 24 новых"


def test_classify_counts_rows_it_actually_marked(tmp_path):
    # Агент отчитывается прозой, поэтому раннер считает сам: строки без темы
    # до шага минус после. «Разметили N роликов» — это и есть работа шага.
    sheets = FakeSheets({"run_log": [],
                         "raw_tiktok": [dict(r, niche="") for r in _raw_rows("D", 5, 2000)],
                         "raw_instagram": [dict(r, niche="") for r in _raw_rows("D", 2, 2000)]})

    def run_command(argv, on_line=None):
        prompt = argv[2] if len(argv) > 2 else ""
        if prompt.startswith("/cf-classify-niche raw_tiktok"):
            for row in sheets.tables["raw_tiktok"]:       # разметил все
                row["niche"] = "D"
        elif prompt.startswith("/cf-classify-niche raw_instagram"):
            sheets.tables["raw_instagram"][0]["niche"] = "D"   # одну из двух
        return (0, _claude_out("готово"))

    r = _runner(sheets, run_command, tmp_path)
    assert r.run_sync("factory") is True
    classify = r.run_progress["classify"]
    assert classify["produced"] == 6 and classify["left"] == 1
    assert _timeline_row(r, "classify")["count_label"] == "6 роликов размечено · 1 без темы"


def test_briefs_and_review_count_what_they_produced(tmp_path):
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": [],
                         "prompt_versions": ready_versions(),
                         "briefs": [{"brief_id": "b0", "review_status": "pending"}]})

    def run_command(argv, on_line=None):
        prompt = argv[2] if len(argv) > 2 else ""
        if prompt.startswith("/cf-generate-briefs"):
            sheets.tables["briefs"] += [{"brief_id": "b1", "review_status": "pending"},
                                        {"brief_id": "b2", "review_status": "pending"}]
        elif prompt.startswith("/cf-review-brief"):
            for row in sheets.tables["briefs"][:2]:
                row["review_status"] = "recommend"
        return (0, _claude_out("готово"))

    r = _runner(sheets, run_command, tmp_path)
    make_ready_theme(r.root)          # без готовой темы генератор не запускается
    assert r.run_sync("factory") is True
    assert r.run_progress["briefs"]["produced"] == 2
    assert r.run_progress["review"]["produced"] == 2
    assert _timeline_row(r, "briefs")["count_label"] == "2 сценария"
    assert _timeline_row(r, "review")["count_label"] == "2 проверено"


def test_finished_step_keeps_its_timer(tmp_path):
    # «Сколько это заняло» спрашивают уже ПОСЛЕ финиша — таймер обязан остаться.
    clock = _Clock(step=30.0)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    r = _runner(sheets, lambda argv, on_line=None: (0, "собрано"), tmp_path,
                now_fn=clock)
    assert r.run_sync("raw") is True
    assert r.run_progress["collect"]["duration"] > 0
    row = _timeline_row(r, "collect")
    assert row["duration"] == r.run_progress["collect"]["duration"]
    assert row["elapsed"] is None                     # шаг не бежит — живого таймера нет


def test_measured_durations_feed_next_eta(tmp_path):
    # Run Log для ETA непригоден: cf log-run пишет started_at = completed_at, и
    # медиана там нулевая. Раннер меряет сам — иначе кривая обещает 90 секунд
    # шагу, который идёт 18 минут (жалоба оператора 25.07).
    zero_len = [{"agent": "collect-tiktok", "status": "success",
                 "started_at": "2026-07-25T03:44:29+00:00",
                 "completed_at": "2026-07-25T03:44:29+00:00"}]
    dpath = tmp_path / "progress.json"
    sheets = FakeSheets({"run_log": zero_len, "raw_tiktok": [], "raw_instagram": []})
    clock = _Clock(step=120.0)
    r = _runner(sheets, lambda argv, on_line=None: (0, "собрано"), tmp_path,
                progress_path=dpath, now_fn=clock)
    assert r.run_sync("raw") is True
    measured = r.run_progress["collect"]["duration"]
    assert measured > 0
    assert r.step_durations["collect"] == [measured]

    r.run_sync("raw")                                  # следующий прогон берёт замер
    assert r.run_progress["collect"]["eta_median"] == measured
    # и замеры переживают рестарт процесса
    r2 = _runner(sheets, lambda argv, on_line=None: (0, ""), tmp_path,
                 progress_path=dpath, now_fn=clock)
    assert r2._measured_eta("collect") == measured


def test_failed_step_duration_is_shown_but_not_used_for_eta(tmp_path):
    # Сорвавшийся шаг честно говорит, сколько прожил, но медиану ETA не портит:
    # «сколько работа занимает» и «когда всё сломалось» — разные числа.
    clock = _Clock(step=10.0)
    sheets = FakeSheets({"run_log": [], "raw_tiktok": [], "raw_instagram": []})
    r = _runner(sheets, lambda argv, on_line=None: (1, "", "сбой"), tmp_path,
                now_fn=clock)
    r.run_sync("raw")
    collect = r.run_progress["collect"]
    assert collect["status"] == "error" and collect["duration"] > 0
    assert r.step_durations.get("collect") in (None, [])


def test_classify_does_not_invent_zero_when_sheets_unreadable(tmp_path):
    # Sheets недоступен → «размечать было нечего» было бы враньём: показываем
    # состояние шага, а не выдуманный ноль.
    class Broken(FakeSheets):
        def read_rows(self, tab, **kw):
            if tab.startswith("raw_"):
                raise ConnectionError("sheets down")
            return super().read_rows(tab, **kw)

    sheets = Broken({"run_log": [], "raw_tiktok": [], "raw_instagram": [],
                     "briefs": []})
    r = _runner(sheets, lambda argv, on_line=None: (0, _claude_out("готово")), tmp_path)
    assert r.run_sync("factory") is True
    assert r.run_progress["classify"]["produced"] is None
    assert _timeline_row(r, "classify")["count_label"] == "готово"
