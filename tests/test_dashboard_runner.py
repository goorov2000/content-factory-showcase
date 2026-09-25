import io
import json
import logging
import os
import sys
import tempfile
import threading
import time

import pytest

from cf.dashboard import runner as runner_mod
from cf.dashboard.runner import StageRunner
from tests.fakes import FakeSheets

# C3.2: raw/stats идут subprocess'ом, вебхук нужен только publish (до этапа 6)
CONFIG = {"dashboard": {"workflows": {
    "publish": "https://n8n.local/webhook/publish",
}}}

# Ожидаемые argv cli-звеньев: раннер зовёт свой же интерпретатор
COLLECT_TIKTOK = [sys.executable, "-u", "-m", "cf", "collect", "tiktok"]
COLLECT_INSTAGRAM = [sys.executable, "-u", "-m", "cf", "collect", "instagram"]
COLLECT_PERFORMANCE = [sys.executable, "-u", "-m", "cf", "collect", "performance"]


def make_runner(config=CONFIG, post_fails=False, cmd_code=0,
                cmd_output='{"result": "готово", "session_id": "sess-1"}',
                sheets=None):
    sheets = sheets if sheets is not None else FakeSheets({"run_log": []})
    posted, commands = [], []
    clock = {"t": 0.0}

    def http_post(url):
        posted.append(url)
        if post_fails:
            raise ConnectionError("n8n down")

    def run_command(argv):
        commands.append(argv)
        return (cmd_code, cmd_output)

    runner = StageRunner(sheets, config, http_post=http_post, run_command=run_command,
                         sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
                         now_fn=lambda: clock["t"],
                         locks_dir=tempfile.mkdtemp())  # изоляция локов звеньев от боевых
    runner._git_commit = lambda msg: None  # фан-аут коммитит — в тестах git не трогаем
    # контракт «агент оставил след в Run Log» проверяется своими тестами; фейковые
    # claude здесь не логируются — без заглушки каждое звено уходило бы в warn
    runner._agent_logged = lambda agent, since: True
    return runner, sheets, posted, commands


# ── C3.2: cli-звенья raw/stats — subprocess `python -m cf collect ...` ────────


def test_cli_raw_runs_both_collect_commands_and_logs():
    runner, sheets, posted, _ = make_runner()
    calls = []

    def run_command(argv):
        calls.append(argv)
        return (0, f"батчи ок\ncollect {argv[-1]}: success")

    runner.run_command = run_command
    assert runner.run_sync("raw") is True
    assert posted == []                                    # вебхук не дёргается
    assert calls == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]    # последовательно
    # detail звена — последняя строка stdout каждой команды
    assert runner.state["raw"] == {
        "status": "ok",
        "detail": "collect tiktok: success · collect instagram: success"}
    # своя строка dashboard-raw поверх строк collect-tiktok/-instagram, которые
    # подкоманда пишет сама (двойной лог ожидаем — разные агенты)
    (tab, row), = sheets.appended
    assert row["agent"] == "dashboard-raw" and row["status"] == "success"


def test_cli_raw_reports_one_entry_per_command():
    runner, _, _, _ = make_runner(cmd_output="строки: +5")
    runner.run_sync("raw")
    reports = runner.reports["raw"]
    assert len(reports) == 2
    assert reports[0]["title"] == "collect tiktok"
    assert reports[1]["title"] == "collect instagram"
    assert all(r["text"] == "строки: +5" for r in reports)
    assert all(r["session_id"] == "" for r in reports)


def test_cli_report_keeps_only_stdout_tail():
    out = "\n".join(f"строка {i}" for i in range(1, 31))   # 30 строк прогресса
    runner, _, _, _ = make_runner(cmd_output=out)
    runner.run_sync("stats")
    text = runner.reports["stats"][0]["text"]
    assert text.splitlines()[0] == "строка 11"             # хвост — последние 20
    assert text.splitlines()[-1] == "строка 30"


def test_cli_report_falls_back_to_stderr_when_stdout_is_only_progress():
    # `cf collect` печатает маркеры прогресса в stdout, а причину падения — в
    # stderr (cli.py: print(f"error: {exc}", file=sys.stderr)). После отсева
    # маркеров stdout пуст, и отчёт этапа оставался пустым: «raw упало» без причины.
    runner, _, _, _ = make_runner()
    runner.run_command = lambda argv: (
        1,
        "CF_PROGRESS phase=prepare total=8\nCF_PROGRESS phase=fetch done=1 total=8\n",
        "error: Sheets 503",
    )
    assert runner.run_sync("stats") is False
    assert runner.reports["stats"][0]["text"] == "error: Sheets 503"


def test_cli_raw_first_failure_does_not_hide_second():
    # Сбой TikTok не отменяет Instagram: обе команды гоняются, обе сводки в
    # отчётах. Разбор 2026-07-27: частичный отказ — warn, а не error. В ту ночь
    # Apify упёрся в потолок трат, Instagram не собрался, а 84 ролика TikTok уже
    # лежали в Sheets — звено-«error» обесценило их и оборвало цикл.
    calls = []

    def run_command(argv):
        calls.append(argv)
        if argv == COLLECT_TIKTOK:
            return (1, "apify: батч упал")
        return (0, "collect instagram: success")

    runner, sheets, _, _ = make_runner()
    runner.run_command = run_command
    assert runner.run_sync("raw") is True                # звено не авария
    assert calls == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]
    titles = [r["title"] for r in runner.reports["raw"]]
    assert titles == ["collect tiktok", "collect instagram"]
    assert runner.state["raw"]["status"] == "warn"
    # в detail и добыча, и причина деградации — одно не подменяет другое
    assert "collect instagram: success" in runner.state["raw"]["detail"]
    assert "collect tiktok: код выхода 1" in runner.state["raw"]["detail"]
    row = sheets.appended[-1][1]
    assert row["status"] == "insufficient_data"          # правило №2, не failed
    assert "collect tiktok" in row["errors"]


def test_cli_raw_all_commands_failed_is_still_error():
    # Граница правки: упали ВСЕ команды звена — это авария, а не «частично».
    runner, sheets, _, _ = make_runner(cmd_code=1, cmd_output="apify: лимит трат")
    assert runner.run_sync("raw") is False
    assert runner.state["raw"]["status"] == "error"
    assert sheets.appended[-1][1]["status"] == "failed"
    assert runner.run_progress["collect"]["status"] == "error"


def test_cli_raw_exception_in_first_still_runs_second():
    # Исключение run_command (нет python? таймаут) — тоже не прячет вторую команду
    # и тоже не делает звено аварией: половина работы сделана.
    calls = []

    def run_command(argv):
        calls.append(argv)
        if argv == COLLECT_TIKTOK:
            raise RuntimeError("таймаут subprocess")
        return (0, "collect instagram: success")

    runner, _, _, _ = make_runner()
    runner.run_command = run_command
    assert runner.run_sync("raw") is True
    assert calls == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]
    reports = runner.reports["raw"]
    assert "падение запуска: таймаут subprocess" in reports[0]["text"]
    assert reports[1]["text"] == "collect instagram: success"
    assert runner.state["raw"]["status"] == "warn"
    assert runner.run_progress["collect"]["status"] == "warn"


def test_cli_stats_runs_collect_performance():
    runner, sheets, posted, commands = make_runner(
        cmd_output="collect performance: success")
    assert runner.run_sync("stats") is True
    assert posted == []
    assert commands == [COLLECT_PERFORMANCE]
    assert runner.state["stats"] == {"status": "ok",
                                     "detail": "collect performance: success"}
    assert sheets.appended[-1][1]["agent"] == "dashboard-stats"


# ── publish: единственное оставшееся n8n-звено (вебхук, до этапа 6) ──────────


def test_publish_posts_all_webhooks_for_list():
    config = {"dashboard": {"workflows": {"publish": [
        "https://n8n.local/webhook/pub-1",
        "https://n8n.local/webhook/pub-2",
    ]}}}
    runner, sheets, posted, _ = make_runner(config=config)
    assert runner.run_sync("publish") is True
    assert posted == ["https://n8n.local/webhook/pub-1",
                      "https://n8n.local/webhook/pub-2"]
    assert sheets.appended[-1][1]["status"] == "success"
    reports = runner.reports["publish"]
    assert len(reports) == 2 and reports[0]["title"] == "webhook"
    assert reports[0]["text"] == "POST https://n8n.local/webhook/pub-1 → OK"


def test_fanout_factory_success_status_is_ok():
    # factory теперь фан-аут; пустая очередь ниш (только run_log) → чистый успех
    runner, _, _, _ = make_runner()
    assert runner.run_sync("factory") is True
    assert runner.state["factory"] == {"status": "ok", "detail": "очередь ниш пуста"}


def test_claude_non_json_output_is_tolerated():
    # классификация в фан-ауте отдаёт не-JSON — парсер терпит, отчёт с сырым текстом
    runner, _, _, _ = make_runner(cmd_output="plain text")
    assert runner.run_sync("factory") is True
    reports = runner.reports["factory"]
    assert reports[0]["text"] == "plain text"
    assert reports[0]["session_id"] == ""
    assert runner.state["factory"]["status"] == "ok"


def test_parse_claude_output_empty_result_is_placeholder():
    assert runner_mod._parse_claude_output('{"result": "", "session_id": "s"}') == \
        ("(пустой ответ агента)", "s")


def test_stage_failure_sets_error_and_logs_failed():
    runner, sheets, _, _ = make_runner(post_fails=True)
    runner.run_sync("publish")
    assert runner.state["publish"]["status"] == "error"
    assert "n8n down" in runner.state["publish"]["detail"]
    assert sheets.appended[-1][1]["status"] == "failed"


def test_publish_missing_workflow_is_honest_error():
    # у cli-звеньев вебхук не нужен, но publish без URL обязан падать внятно
    runner, _, _, _ = make_runner(config={})
    runner.run_sync("publish")
    assert runner.state["publish"]["status"] == "error"
    assert "не настроен" in runner.state["publish"]["detail"]


def test_start_refuses_double_run():
    runner, _, _, _ = make_runner()
    runner.state["raw"]["status"] = "running"
    assert runner.start("raw") is False


def test_unknown_stage_raises():
    runner, _, _, _ = make_runner()
    with pytest.raises(KeyError):
        runner.run_sync("nope")


def test_full_cycle_runs_auto_stages_then_waits():
    runner, sheets, posted, commands = make_runner()
    runner.run_cycle_sync()
    assert posted == []                         # ни одного вебхука в авто-цикле
    # raw — cli-звено: обе collect-команды идут до factory
    assert commands[0] == COLLECT_TIKTOK
    assert commands[1] == COLLECT_INSTAGRAM
    # factory-фан-аут стартует с классификации ниш — по одному вызову на raw-вкладку
    assert commands[2] == [
        "claude", "-p", "/cf-classify-niche raw_tiktok", "--output-format", "json"]
    assert commands[3] == [
        "claude", "-p", "/cf-classify-niche raw_instagram", "--output-format", "json"]
    # прежняя заготовка «дошёл до ручного одобрения — ждёт продюсера» была
    # неправдой в обе стороны: цикл ничего не ждал, а решения, которых от
    # оператора действительно ждут (рецепты, промпты тем), не назывались
    assert runner.cycle_note.startswith("цикл прошёл")


def test_full_cycle_continues_after_failed_stage():
    """Сбой звена больше не обрывает цикл (разбор 2026-07-27, цена — сутки).

    Ночью 27.07 Apify упёрся в потолок трат, «Сбор» ушёл в error — и цикл вернулся
    прямо на этом месте, не добравшись до «Контент-завода». Между тем фан-аут
    читает Sheets и диск, а не результат сегодняшнего сбора: работа для него была.
    Прямое требование CLAUDE.md — «верх конвейера (шаги 1-4) не гейтится никогда».
    """
    runner, _, _, commands = make_runner(cmd_code=1)   # collect падает кодом 1
    runner.run_cycle_sync()
    assert commands[:2] == [COLLECT_TIKTOK, COLLECT_INSTAGRAM]
    # factory запустился: фан-аут начинается с классификации raw-вкладок
    assert commands[2] == [
        "claude", "-p", "/cf-classify-niche raw_tiktok", "--output-format", "json"]
    assert runner.state["raw"]["status"] == "error"
    assert runner.state["factory"]["status"] in ("ok", "warn")
    # заметка называет И деградацию, И итог — одно не подменяет другое
    assert "Собранные ролики" in runner.cycle_note
    assert "цикл прошёл" in runner.cycle_note


def test_cycle_stops_when_stage_already_busy():
    # M6 (аудит 2026-07-24): занятое factory теперь блокирует и raw-звено цикла —
    # мьютекс raw↔factory действует на пути _claim, а не только в start().
    # Раньше цикл успевал прогнать raw параллельно работающему factory.
    runner, _, _, commands = make_runner()
    runner.state["factory"]["status"] = "running"   # звено занято (например, reply)
    runner.run_cycle_sync()
    assert commands == []                            # ни сбора, ни claude
    assert "Контент-завод" in runner.cycle_note      # причина — мьютекс, не «занято»


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_start_runs_in_background_thread():
    sheets = FakeSheets({"run_log": []})
    gate = threading.Event()

    def run_command(argv):
        gate.wait(timeout=2.0)
        return (0, '{"result": "готово", "session_id": "sess-1"}')

    runner = StageRunner(sheets, CONFIG, http_post=lambda url: None,
                         run_command=run_command, locks_dir=tempfile.mkdtemp())
    runner._git_commit = lambda msg: None
    runner._agent_logged = lambda agent, since: True
    assert runner.start("factory") is True
    assert runner.state["factory"]["status"] == "running"
    assert runner.any_running() is True
    gate.set()
    assert _wait_until(lambda: runner.state["factory"]["status"] == "ok")
    assert runner.state["factory"]["detail"] == "очередь ниш пуста"


def test_start_cycle_refuses_double_run():
    sheets = FakeSheets({"run_log": []})
    gate = threading.Event()

    def run_command(argv):
        # первый collect raw ждёт гейт — цикл «висит» на первом звене
        gate.wait(timeout=2.0)
        return (0, "{}")

    runner = StageRunner(sheets, CONFIG, http_post=lambda url: None,
                         run_command=run_command, locks_dir=tempfile.mkdtemp())
    runner._git_commit = lambda msg: None
    assert runner.start_cycle() is True
    assert runner.any_running() is True         # первое звено помечено сразу, до потока
    assert runner.start_cycle() is False
    gate.set()
    assert _wait_until(lambda: runner.cycle_note != "" and not runner.any_running())
    # прежняя заготовка «дошёл до ручного одобрения — ждёт продюсера» была
    # неправдой в обе стороны: цикл ничего не ждал, а решения, которых от
    # оператора действительно ждут (рецепты, промпты тем), не назывались
    assert runner.cycle_note.startswith("цикл прошёл")


def test_log_failure_does_not_mask_success():
    class BrokenLogSheets(FakeSheets):
        def append_row(self, tab_key, row):
            raise ConnectionError("sheets down")

    # stats — cli-звено; пустой stdout → нет detail, остаётся дефолтная формулировка
    runner = StageRunner(BrokenLogSheets({"run_log": []}), CONFIG,
                         http_post=lambda url: None, run_command=lambda argv: (0, ""))
    assert runner.run_sync("stats") is True
    assert runner.state["stats"] == {"status": "warn",
                                     "detail": "успешно, но запись в Run Log не удалась"}


def test_default_run_errors_when_claude_missing(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="не найден"):
        runner_mod._default_run(["claude", "-p", "/cf-analyze"])


@pytest.mark.skipif(os.name == "nt",
                    reason="start_new_session/killpg — POSIX; на Windows раннер их не передаёт")
def test_default_run_captures_stdout_and_returncode(monkeypatch):
    monkeypatch.setattr(runner_mod.shutil, "which", lambda name: "/usr/bin/claude")

    class FakePopen:
        returncode = 0

        def __init__(self, argv, **kwargs):
            # M21: своя сессия процессов — kill по таймауту накрывает и детей
            assert kwargs.get("start_new_session") is True
            assert kwargs.get("text") is True

        def communicate(self, timeout=None):
            return ('{"result": "ok", "session_id": "s1"}', "")

    monkeypatch.setattr(runner_mod.subprocess, "Popen", FakePopen)
    code, out, err = runner_mod._default_run(["claude", "-p", "/cf-analyze"])
    assert code == 0
    assert out == '{"result": "ok", "session_id": "s1"}'
    assert err == ""   # stderr отдаётся отдельно — там ловится trust-warning claude


class _FakeResponse:
    def raise_for_status(self):
        pass


def _capture_httpx_post(monkeypatch):
    """Подменяет httpx.post и сбрасывает кэш токена. Возвращает список (url, headers)."""
    posts = []

    def fake_post(url, timeout, headers=None):
        posts.append((url, headers))
        return _FakeResponse()

    monkeypatch.setattr(runner_mod.httpx, "post", fake_post)
    monkeypatch.setattr(runner_mod, "_token_cache", {})
    monkeypatch.setattr(runner_mod, "_warned_paths", set())
    return posts


def test_default_post_sends_token_header(tmp_path, monkeypatch):
    posts = _capture_httpx_post(monkeypatch)
    token_file = tmp_path / "webhook-token.txt"
    token_file.write_text("tok-1234\n", encoding="utf-8")
    config = {"n8n": {"webhook_token_file": str(token_file)}}
    runner_mod._default_post("https://n8n.local/webhook/raw", config)
    assert posts == [("https://n8n.local/webhook/raw", {"X-CF-Token": "tok-1234"})]


def test_default_post_caches_token_after_first_read(tmp_path, monkeypatch):
    posts = _capture_httpx_post(monkeypatch)
    token_file = tmp_path / "webhook-token.txt"
    token_file.write_text("tok-старый", encoding="utf-8")
    config = {"n8n": {"webhook_token_file": str(token_file)}}
    runner_mod._default_post("https://n8n.local/webhook/raw", config)
    token_file.write_text("tok-новый", encoding="utf-8")
    runner_mod._default_post("https://n8n.local/webhook/raw", config)
    assert [h for _, h in posts] == [{"X-CF-Token": "tok-старый"}] * 2


@pytest.mark.parametrize("token_state", ["missing", "empty"])
def test_default_post_without_token_posts_bare_and_warns_once(
        tmp_path, monkeypatch, caplog, token_state):
    posts = _capture_httpx_post(monkeypatch)
    token_file = tmp_path / "webhook-token.txt"
    if token_state == "empty":
        token_file.write_text("   \n", encoding="utf-8")
    config = {"n8n": {"webhook_token_file": str(token_file)}}
    with caplog.at_level(logging.WARNING, logger="cf.dashboard.runner"):
        runner_mod._default_post("https://n8n.local/webhook/raw", config)
        runner_mod._default_post("https://n8n.local/webhook/raw", config)
    assert len(posts) == 2                       # мягкая деградация: POST без заголовка
    assert all("X-CF-Token" not in (h or {}) for _, h in posts)
    warnings = [r for r in caplog.records if "X-CF-Token" in r.getMessage()]
    assert len(warnings) == 1                    # предупреждение ровно один раз


def test_default_post_without_config_key_posts_bare(monkeypatch):
    posts = _capture_httpx_post(monkeypatch)
    runner_mod._default_post("https://n8n.local/webhook/raw", {})
    (url, headers), = posts
    assert url == "https://n8n.local/webhook/raw"
    assert "X-CF-Token" not in (headers or {})


def test_default_post_picks_up_token_created_after_start(tmp_path, monkeypatch):
    # самовосстановление: файл появился после старта — заголовок со следующего POST
    posts = _capture_httpx_post(monkeypatch)
    token_file = tmp_path / "webhook-token.txt"
    config = {"n8n": {"webhook_token_file": str(token_file)}}
    runner_mod._default_post("https://n8n.local/webhook/raw", config)  # файла ещё нет
    token_file.write_text("tok-поздний\n", encoding="utf-8")
    runner_mod._default_post("https://n8n.local/webhook/raw", config)  # без рестарта
    assert "X-CF-Token" not in (posts[0][1] or {})
    assert posts[1][1] == {"X-CF-Token": "tok-поздний"}


def test_runner_default_post_receives_runner_config(monkeypatch):
    seen = []
    monkeypatch.setattr(runner_mod, "_default_post",
                        lambda url, config=None: seen.append((url, config)))
    config = {"dashboard": {"workflows": {"publish": "https://n8n.local/webhook/publish"}},
              "n8n": {"webhook_token_file": "secrets/webhook-token.txt"}}
    runner = StageRunner(FakeSheets({"run_log": []}), config,
                         run_command=lambda argv: (0, "{}"))
    assert runner.run_sync("publish") is True
    assert seen == [("https://n8n.local/webhook/publish", config)]


def test_reply_sync_appends_question_and_answer():
    runner, _, _, commands = make_runner()
    result = runner._reply_sync("factory", "sess-1", "вариант 2")
    assert result is True
    reports = runner.reports["factory"]
    assert len(reports) == 2
    assert reports[0]["title"] == "вопрос продюсера"
    assert reports[0]["text"] == "вариант 2"
    assert reports[0]["session_id"] == "sess-1"
    assert reports[1]["title"] == "ответ агента"
    assert reports[1]["text"] == "готово"
    assert reports[1]["session_id"] == "sess-1"
    assert runner.state["factory"]["status"] == "ok"
    assert commands == [["claude", "-p", "--output-format", "json",
                        "--resume", "sess-1", "--", "вариант 2"]]


def test_reply_refuses_while_running():
    runner, _, _, _ = make_runner()
    runner.state["factory"]["status"] = "running"
    assert runner.reply("factory", "sess-1", "текст") is False


def test_reply_refuses_stage_claimed_by_cycle():
    runner, _, _, _ = make_runner()
    assert runner._claim("factory") is True     # цикл захватил звено
    assert runner.reply("factory", "s1", "x") is False


def test_reply_unknown_stage_raises():
    runner, _, _, _ = make_runner()
    with pytest.raises(KeyError):
        runner.reply("nope", "sess-1", "текст")


def test_reports_capped_at_ten():
    runner, _, _, _ = make_runner()
    for i in range(15):
        runner._add_report("factory", f"т{i}", f"текст {i}", "sess")
    history = runner.reports["factory"]
    assert len(history) == 10
    assert history[0]["text"] == "текст 5"
    assert history[-1]["text"] == "текст 14"


def test_runner_accepts_injected_clock():
    clock = {"t": 0.0}
    runner = StageRunner(FakeSheets({"run_log": []}), CONFIG,
                         http_post=lambda url: None,
                         run_command=lambda argv: (0, "{}"),
                         sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
                         now_fn=lambda: clock["t"])
    assert runner.now() == 0.0
    runner.sleep(30)
    assert runner.now() == 30.0


def test_publish_stage_stays_fire_and_forget():
    runner, sheets, posted, _ = make_runner()
    assert runner.run_sync("publish") is True
    assert posted == ["https://n8n.local/webhook/publish"]
    assert runner.state["publish"] == {"status": "ok", "detail": "успешно"}
    assert sheets.appended[-1][1]["status"] == "success"


def test_cli_detail_kept_when_log_fails():
    # сводка сбора не теряется, если запись строки dashboard-raw в Run Log сорвалась
    class BrokenLogSheets(FakeSheets):
        def append_row(self, tab_key, row):
            raise ConnectionError("sheets down")

    runner, _, _, _ = make_runner(sheets=BrokenLogSheets({"run_log": []}),
                                  cmd_output="collect: +5 строк")
    assert runner.run_sync("raw") is True
    assert runner.state["raw"] == {
        "status": "warn",
        "detail": "collect: +5 строк · collect: +5 строк · запись в Run Log не удалась",
    }


def test_manual_factory_blocked_while_raw_running():
    runner, _, _, _ = make_runner()
    runner.state["raw"]["status"] = "running"
    assert runner.start("factory") is False
    assert runner.cycle_note == "«Контент-завод» не запущен: raw ещё выгружается"
    assert runner.state["factory"]["status"] == "idle"


def test_manual_factory_allowed_when_raw_idle():
    runner, _, _, _ = make_runner()
    assert runner.start("factory") is True
    assert _wait_until(lambda: runner.state["factory"]["status"] == "ok")


def test_manual_factory_start_clears_stale_note():
    runner, _, _, _ = make_runner()
    runner.state["raw"]["status"] = "running"
    assert runner.start("factory") is False
    assert runner.cycle_note == "«Контент-завод» не запущен: raw ещё выгружается"
    runner.state["raw"]["status"] = "ok"
    assert runner.start("factory") is True
    assert runner.cycle_note == ""
    assert _wait_until(lambda: runner.state["factory"]["status"] == "ok")


# ── P2.7: живучесть фонового цикла ───────────────────────────────────────────

def test_fanout_params_exception_degrades_to_error_not_zombie():
    # Сбой _fanout_params (внутри try фонового прогона) не убивает поток и не
    # оставляет звено навсегда в running — оно деградирует в error.
    runner, _, _, _ = make_runner()

    def boom():
        raise RuntimeError("сломанный fanout-конфиг")

    runner._fanout_params = boom
    assert runner.run_sync("factory") is False
    assert runner.state["factory"]["status"] == "error"
    assert "сломанный fanout-конфиг" in runner.state["factory"]["detail"]


def test_cycle_active_flag_true_across_interstage_gap():
    # В зазоре между звеньями (raw финишировал, factory ещё не помечен running)
    # any_running() обязан оставаться True за счёт флага цикла, иначе HTMX-поллинг
    # оборвётся посреди цикла.
    runner, _, _, _ = make_runner()
    seen = {}
    orig_claim = runner._claim

    def spy_claim(stage):
        if stage == "factory":
            seen["cycle_active"] = runner._cycle_active
            seen["state_running"] = any(
                s["status"] == "running" for s in runner.state.values())
            seen["any_running"] = runner.any_running()
        return orig_claim(stage)

    runner._claim = spy_claim
    runner.run_cycle_sync()
    assert seen["state_running"] is False     # ни одно звено не 'running' в зазоре
    assert seen["cycle_active"] is True        # но флаг цикла активен
    assert seen["any_running"] is True         # → поллинг продолжается
    # по завершении цикла флаг снят и поллинг штатно останавливается
    assert runner._cycle_active is False
    assert runner.any_running() is False


def test_start_raw_refused_while_factory_running():
    # Обратное направление взаимного исключения (P2.7): raw нельзя стартовать,
    # пока идёт «Контент-завод». Проверка — внутри self._lock.
    runner, _, _, _ = make_runner()
    runner.state["factory"]["status"] = "running"
    assert runner.start("raw") is False
    assert runner.state["raw"]["status"] == "idle"


# ── P5.3: best-effort уведомление о завершении цикла (notify_url) ─────────────

NOTIFY_CONFIG = {"dashboard": {"notify_url": "https://n8n.local/webhook/cf-notify"}}


def _notify_runner(config, notify_fails=False, sheets=None):
    """Раннер с внедрённым спаем http_notify; возвращает (runner, notified)."""
    sheets = sheets if sheets is not None else FakeSheets({"run_log": []})
    notified = []

    def http_notify(url, payload):
        notified.append((url, payload))
        if notify_fails:
            raise ConnectionError("notify endpoint down")

    runner = StageRunner(sheets, config, http_post=lambda url: None,
                         run_command=lambda argv: (0, "{}"),
                         http_notify=http_notify, locks_dir=tempfile.mkdtemp())
    runner._git_commit = lambda msg: None
    runner._agent_logged = lambda agent, since: True
    return runner, notified


def test_fanout_notify_posts_summary_when_configured():
    # Завершение фан-аута при заданном notify_url → ровно один POST со сводкой.
    runner, notified = _notify_runner(NOTIFY_CONFIG)
    assert runner.run_sync("factory") is True
    assert len(notified) == 1
    url, payload = notified[0]
    assert url == "https://n8n.local/webhook/cf-notify"
    assert payload["stage"] == "factory"
    assert "formulas" in payload
    assert "briefs_pending" in payload
    assert "detail" in payload


def test_fanout_no_notify_when_url_absent():
    # notify_url не задан → POST'а нет и ошибки нет (тихий no-op).
    runner, notified = _notify_runner({"dashboard": {}})
    assert runner.run_sync("factory") is True
    assert notified == []


def test_fanout_notify_failure_does_not_fail_cycle():
    # Сбой уведомления не роняет звено: run_fanout_sync завершается штатно (ok).
    runner, notified = _notify_runner(NOTIFY_CONFIG, notify_fails=True)
    assert runner.run_sync("factory") is True
    assert runner.state["factory"]["status"] == "ok"
    assert len(notified) == 1                    # попытка была, но исход проглочен


def test_fanout_notify_counts_pending_briefs():
    # briefs_pending в сводке — дешёвый подсчёт pending-брифов (грязный регистр норм.).
    sheets = FakeSheets({"run_log": [], "briefs": [
        {"brief_id": "B1", "review_status": "pending"},
        {"brief_id": "B2", "review_status": "approved"},
        {"brief_id": "B3", "review_status": "PENDING "},
    ]})
    runner, notified = _notify_runner(NOTIFY_CONFIG, sheets=sheets)
    assert runner.run_sync("factory") is True
    assert notified[0][1]["briefs_pending"] == 2


def test_start_factory_refused_while_raw_running_inside_lock():
    # Прямое направление сохранено после переноса проверки под лок.
    runner, _, _, _ = make_runner()
    runner.state["raw"]["status"] = "running"
    assert runner.start("factory") is False
    assert runner.state["factory"]["status"] == "idle"


# ── P5.7: ритуалы недели (cf-eval / cf-tune-sources) ─────────────────────────


def test_run_ritual_sync_dispatches_eval_command():
    # приёмка: кнопка запускает runner с нужной командой (фейк run_command)
    runner, _, _, commands = make_runner()
    assert runner._run_ritual_sync("eval") is True
    assert commands == [["claude", "-p", "/cf-eval", "--output-format", "json"]]
    # отчёт ушёл в отдельный канал ритуала (не в звенья)
    reports = runner.reports["ritual-eval"]
    assert len(reports) == 1 and reports[0]["text"] == "готово"


def test_run_ritual_sync_dispatches_tune_sources_command():
    runner, _, _, commands = make_runner()
    assert runner._run_ritual_sync("tune-sources") is True
    assert commands == [
        ["claude", "-p", "/cf-tune-sources", "--output-format", "json"]]
    assert len(runner.reports["ritual-tune-sources"]) == 1


def test_run_ritual_unknown_raises():
    runner, _, _, _ = make_runner()
    with pytest.raises(KeyError):
        runner.run_ritual("nope")


def test_run_ritual_refuses_double_run():
    runner, _, _, _ = make_runner()
    runner.ritual_state["eval"]["status"] = "running"
    assert runner.run_ritual("eval") is False


def test_running_ritual_keeps_any_running_true():
    # поллинг «Отчётов звеньев» не должен обрываться, пока ритуал выполняется
    runner, _, _, _ = make_runner()
    assert runner.any_running() is False
    runner.ritual_state["eval"]["status"] = "running"
    assert runner.any_running() is True


def test_run_ritual_nonzero_exit_reports_and_returns_false():
    # claude отработал, но код!=0: отчёт с выводом + видимый problems-отчёт, исход False
    # (у ритуала нет detail звена — проблема без отчёта оседала бы только в логе)
    runner, _, _, _ = make_runner(cmd_code=1)
    assert runner._run_ritual_sync("eval") is False
    assert runner.ritual_state["eval"]["status"] == "idle"
    reports = runner.reports["ritual-eval"]
    assert [r["text"] for r in reports] == ["готово", "Недельный eval не удалась"]


def test_run_ritual_surfaces_failure_when_run_command_raises():
    # ключевой фикс ревью: исключение run_command (нет claude в PATH / таймаут)
    # уходит только в problems и НЕ оставляет отчёта — оператор, нажавший ▶, иначе
    # не видит НИЧЕГО. Теперь проблема поднимается видимым отчётом в канал ритуала.
    runner, _, _, _ = make_runner()

    def boom(argv):
        raise RuntimeError("claude не найден в PATH")

    runner.run_command = boom
    assert runner._run_ritual_sync("eval") is False
    assert runner.ritual_state["eval"]["status"] == "idle"
    reports = runner.reports["ritual-eval"]
    assert len(reports) == 1
    assert "упал" in reports[0]["text"]              # видимый след проблемы


def test_run_ritual_full_history_keeps_limit_and_problem_report():
    # при заполненной истории канала (REPORT_HISTORY_LIMIT) append+trim держит лимит;
    # problems-отчёт теперь добавляется ВСЕГДА при проблемах (видимость для оператора)
    # и идёт последним — сразу после отчёта _fanout_claude с выводом claude.
    from cf.dashboard.runner import REPORT_HISTORY_LIMIT
    runner, _, _, _ = make_runner(cmd_code=1)   # claude отработал, но код!=0 -> problem
    channel = "ritual-eval"
    for i in range(REPORT_HISTORY_LIMIT):       # забить историю под лимит
        runner._add_report(channel, f"старый {i}", f"текст {i}")
    assert len(runner.reports[channel]) == REPORT_HISTORY_LIMIT

    assert runner._run_ritual_sync("eval") is False
    assert len(runner.reports[channel]) == REPORT_HISTORY_LIMIT
    assert runner.reports[channel][-2]["text"] == "готово"
    assert runner.reports[channel][-1]["text"] == "Недельный eval не удалась"


def test_start_cycle_not_blocked_by_running_ritual():
    # 10-30-минутный eval НЕ должен блокировать «Запустить цикл» (ритуал ≠ звено)
    runner, _, posted, _ = make_runner()
    runner.ritual_state["eval"]["status"] = "running"
    assert runner.start_cycle() is True
    assert _wait_until(lambda: runner.cycle_note != "" and not runner._cycle_active)
    # прежняя заготовка «дошёл до ручного одобрения — ждёт продюсера» была
    # неправдой в обе стороны: цикл ничего не ждал, а решения, которых от
    # оператора действительно ждут (рецепты, промпты тем), не назывались
    assert runner.cycle_note.startswith("цикл прошёл")


def test_start_cycle_still_refused_when_stage_running():
    # звено занято → цикл по-прежнему не стартует (регресс-гвоздь для _pipeline_busy)
    runner, _, _, _ = make_runner()
    runner.state["factory"]["status"] = "running"
    assert runner.start_cycle() is False


def test_pipeline_busy_ignores_rituals_but_not_stages():
    runner, _, _, _ = make_runner()
    runner.ritual_state["eval"]["status"] = "running"
    assert runner._pipeline_busy() is False          # ритуал конвейер не занимает
    assert runner.any_running() is True              # но поллинг живёт
    runner.state["raw"]["status"] = "running"
    assert runner._pipeline_busy() is True           # звено — занимает


# ── Аудит 2026-07-24 (M6): мьютекс raw↔factory на путях reply() и цикла ──────

def test_reply_blocked_while_mutex_partner_running():
    runner, _sheets, _posted, commands = make_runner()
    runner.state["raw"] = {"status": "running", "detail": "идёт сбор"}
    assert runner.reply("factory", "sess-1", "ответ продюсера") is False
    assert commands == []                          # claude не запускался
    assert "raw" in runner.cycle_note or "выгружается" in runner.cycle_note


def test_cycle_stops_when_partner_stage_running_in_process():
    runner, _sheets, _posted, commands = make_runner()
    runner.state["factory"] = {"status": "running", "detail": "фан-аут"}
    runner.run_cycle_sync()
    assert commands == []                          # raw не стартовал посреди factory
    assert "Контент-завод" in runner.cycle_note


# ── Персист «Отчётов звеньев»: рестарт дашборда больше не стирает историю ─────
# (рестарт 2026-07-24 17:10 унёс единственные следы блокировки claude-звеньев)


def test_reports_persist_and_restore_across_restart(tmp_path):
    path = tmp_path / "stage-reports.json"

    def build():
        return StageRunner(FakeSheets({"run_log": []}), CONFIG,
                           http_post=lambda url: None,
                           run_command=lambda argv: (0, "{}"),
                           locks_dir=tempfile.mkdtemp(), reports_path=path)

    first = build()
    first._add_report("factory", "/cf-classify-niche raw_tiktok", "заблокирован", "sess-7")
    assert path.exists()
    restored = build()
    entry = restored.reports["factory"][0]
    assert entry["title"] == "/cf-classify-niche raw_tiktok"
    assert entry["text"] == "заблокирован"
    assert entry["session_id"] == "sess-7"   # «ответить агенту» переживает рестарт
    # сегодняшний отчёт даты в «at» не получает
    assert entry["at"] == first.reports["factory"][0]["at"]


def test_reports_restore_prefixes_older_days_and_drops_junk(tmp_path):
    import json
    path = tmp_path / "stage-reports.json"
    path.write_text(json.dumps({
        "factory": [{"title": "t", "text": "x", "session_id": "",
                     "at": "17:23", "day": "2020-01-01"}],
        "чужой-канал": [{"title": "a", "text": "b"}],
        "raw": "не список",
    }, ensure_ascii=False), encoding="utf-8")
    runner = StageRunner(FakeSheets({"run_log": []}), CONFIG,
                         http_post=lambda url: None,
                         run_command=lambda argv: (0, "{}"),
                         locks_dir=tempfile.mkdtemp(), reports_path=path)
    # вчерашний «17:23» после рестарта не должен читаться как сегодняшний
    assert runner.reports["factory"][0]["at"] == "2020-01-01 17:23"
    assert "чужой-канал" not in runner.reports
    assert runner.reports["raw"] == []


def test_reports_broken_state_file_starts_fresh(tmp_path):
    path = tmp_path / "stage-reports.json"
    path.write_text("{битый json", encoding="utf-8")
    runner = StageRunner(FakeSheets({"run_log": []}), CONFIG,
                         http_post=lambda url: None,
                         run_command=lambda argv: (0, "{}"),
                         locks_dir=tempfile.mkdtemp(), reports_path=path)
    assert all(v == [] for v in runner.reports.values())


def test_reports_not_persisted_without_path():
    # дефолт (тесты, кастомные раннеры): reports_path=None — только память
    runner, *_ = make_runner()
    runner._add_report("factory", "t", "x")
    assert runner.reports_path is None


# ── Ритуал: холостой прогон агента (нет следа в Run Log) виден отчётом ────────


def test_run_ritual_flags_idle_run_without_runlog_trace():
    sheets = FakeSheets({"run_log": []})
    runner = StageRunner(
        sheets, CONFIG, http_post=lambda url: None,
        run_command=lambda argv: (0, '{"result": "вежливый отчёт", "session_id": "s"}'),
        locks_dir=tempfile.mkdtemp())
    assert runner._run_ritual_sync("eval") is False
    reports = runner.reports["ritual-eval"]
    assert reports[0]["text"] == "вежливый отчёт"
    assert "холостой" in reports[-1]["text"] and "eval-agent" in reports[-1]["text"]


def test_cli_empty_stdout_report_falls_back_to_stderr():
    # раньше фолбэк жил в _default_run («stdout or stderr»); теперь stderr отдаётся
    # отдельно — cli-звено обязано показать его при пустом stdout
    runner, _, _, _ = make_runner()
    runner.run_command = lambda argv: (1, "", "collect: apify упал")
    assert runner.run_sync("stats") is False
    assert runner.reports["stats"][0]["text"] == "collect: apify упал"


def test_reply_sync_untrusted_workspace_is_error():
    # согласованность untrusted-семантики: reply при недоверенном воркспейсе — не
    # тихий success (агент не мог выполнить ни одной cf-команды)
    warn = "Ignoring 44 permissions.allow entries: this workspace has not been trusted."
    runner, _, _, _ = make_runner()
    runner.run_command = lambda argv: (0, '{"result": "не смог", "session_id": "s2"}', warn)
    runner.state["factory"] = {"status": "running", "detail": ""}
    assert runner._reply_sync("factory", "s1", "вопрос") is False
    assert runner.state["factory"]["status"] == "error"
    assert runner.reports["factory"][-1]["text"].startswith("⚠")


def test_reports_day_prefix_not_duplicated_after_second_restart(tmp_path):
    # находка ревью: датный префикс в «at» не должен накапливаться с каждым рестартом
    import json
    path = tmp_path / "stage-reports.json"
    path.write_text(json.dumps({"factory": [{
        "title": "t", "text": "x", "session_id": "", "at": "17:23",
        "day": "2020-01-01"}]}), encoding="utf-8")

    def build():
        return StageRunner(FakeSheets({"run_log": []}), CONFIG,
                           http_post=lambda url: None,
                           run_command=lambda argv: (0, "{}"),
                           locks_dir=tempfile.mkdtemp(), reports_path=path)

    second = build()                              # первый рестарт: префикс дописан
    second._add_report("raw", "новый", "запись")  # мутированная история ушла на диск
    third = build()                               # второй рестарт: без дубля префикса
    assert third.reports["factory"][0]["at"] == "2020-01-01 17:23"


# ── Потоковое чтение stdout подпроцесса (прогресс v2, спека §7) ────────────────


def test_default_run_streams_stdout_lines_as_they_appear():
    import sys as _sys
    from cf.dashboard.runner import _default_run

    script = ("import sys, time\n"
              "for i in range(3):\n"
              "    print('CF_PROGRESS phase=fetch done=%d total=3' % i, flush=True)\n"
              "print('предупреждение', file=sys.stderr)\n")
    seen = []
    code, out, err = _default_run([_sys.executable, "-c", script], on_line=seen.append)
    assert code == 0
    assert len(seen) == 3 and seen[0].startswith("CF_PROGRESS")
    assert "CF_PROGRESS phase=fetch done=2 total=3" in out   # полный stdout сохранён
    assert "предупреждение" in err                            # stderr не потерян


def test_default_run_without_callback_keeps_old_behaviour():
    import sys as _sys
    from cf.dashboard.runner import _default_run

    code, out, err = _default_run([_sys.executable, "-c", "print('готово')"])
    assert code == 0 and "готово" in out and err == ""


def test_stream_process_hits_deadline_on_silent_hang():
    # Подпроцесс молчит и не выходит: без отдельного потока чтения дедлайн бы
    # никогда не сработал (блокирующее чтение до EOF).
    import subprocess as _sp
    import sys as _sys
    from cf.dashboard.runner import _StreamTimeout, _stream_process

    p = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(30)"],
                  stdout=_sp.PIPE, stderr=_sp.PIPE, text=True)
    try:
        with pytest.raises(_StreamTimeout):
            _stream_process(p, lambda line: None, timeout=0.5, kill=p.kill)
    finally:
        p.kill()
        p.wait()


class _FakeProc:
    """Подпроцесс, который просто отдаёт готовый stdout (для _stream_process)."""

    def __init__(self, text):
        self.stdout = io.StringIO(text)
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


def test_progress_callback_failure_is_logged_once_not_per_line(caplog):
    # Сбор печатает маркер на каждый батч плюс свои строки. Если колбэк падает
    # стабильно (неписучий agent-runtime/reports, зависший лок), трейсбек на
    # КАЖДУЮ строку топил настоящую причину в журнале юнита.
    proc = _FakeProc("".join(f"строка {i}\n" for i in range(50)))

    def boom(_line):
        raise RuntimeError("лок прогресса занят")

    with caplog.at_level(logging.WARNING):
        out, err = runner_mod._stream_process(proc, boom, timeout=5,
                                              kill=lambda: None)
    assert out.count("строка") == 50                     # строки не потеряны
    assert len([r for r in caplog.records if r.exc_info]) == 1   # трейсбек один
    assert any("ещё 49 раз" in r.getMessage() for r in caplog.records)


def test_default_run_kills_and_joins_reader_on_timeout(monkeypatch):
    # На таймауте main-поток звал p.communicate(), пока поток-читатель ещё
    # итерировал тот же stdout: хвост вывода делился между ними произвольно, а
    # читатель получал ValueError на закрытом файле (его глотал blanket except).
    monkeypatch.setattr(runner_mod, "SUBPROCESS_TIMEOUT", 0.5)
    with pytest.raises(RuntimeError, match="превысил таймаут"):
        runner_mod._default_run([sys.executable, "-c", "import time; time.sleep(30)"],
                                on_line=lambda line: None)
    assert not [t for t in threading.enumerate() if t.name == "cf-stdout-reader"]


# ── Разбор 2026-07-27: цикл переживает сбой звена и перестаёт молчать ─────────
#
# Ночь 27.07: аккаунт Apify упёрся в жёсткий потолок трат (70.098 при лимите 70),
# новые раны не стартовали — у TikTok не поднялись 2 батча из 6, у Instagram все
# 5. Внешний отказ обнажил три бага раннера: цикл обрывался после сбойного звена,
# частичный сбор объявлялся аварией, и об обрыве не узнавал никто — ни Telegram,
# ни CF Run Log, ни лента после рестарта cf-dashboard (Restart=always).


def _cycle_runner(tmp_path, cmd_code=0, sheets=None, progress_path=None,
                  fail_collect=False):
    """Раннер для проверок цикла: свой корень и локи, git и Run Log — фейковые.

    fail_collect — падает ТОЛЬКО сбор (ровно ночь 27.07: Apify не стартовал раны,
    а claude-звенья работали как обычно)."""
    sheets = sheets if sheets is not None else FakeSheets({"run_log": []})
    commands = []

    def run_command(argv):
        commands.append(argv)
        collect = "collect" in [str(a) for a in argv]
        return (1 if (fail_collect and collect) else cmd_code,
                '{"result": "готово", "session_id": "s1"}')

    runner = StageRunner(sheets, CONFIG, http_post=lambda url: None,
                         run_command=run_command, locks_dir=str(tmp_path / "locks"),
                         analysis_dir=tmp_path / "analysis",
                         progress_path=progress_path, root=tmp_path / "repo")
    runner._git_commit = lambda msg: None
    runner._agent_logged = lambda agent, since: True
    return runner, sheets, commands


def test_cycle_notifies_operator_about_degraded_stage(tmp_path, monkeypatch):
    # Про обрыв цикла не узнавал никто: ветка обрыва не звала _notify, а
    # единственный call-site уведомления сидел в хвосте фан-аута, который в ту
    # ночь не запускался.
    import cf.notify as notify_mod

    sent = []
    monkeypatch.setattr(notify_mod, "notify_telegram",
                        lambda text, cfg=None, client=None: sent.append(text) or True)
    runner, _, _ = _cycle_runner(tmp_path, fail_collect=True)
    runner.run_cycle_sync()
    assert len(sent) == 1
    # Формулировка человеческая (тексты — cf.messages): называем сломавшийся
    # этап и сразу говорим, что остальное доехало.
    assert "Не отработал этап: Собранные ролики" in sent[0]
    assert "Остальное прошло нормально" in sent[0]


def test_cycle_notify_failure_does_not_break_the_cycle(tmp_path, monkeypatch):
    # Телеметрия не имеет права ронять прогон — даже если модуль уведомлений
    # нарушит свой best-effort-контракт и бросит.
    import cf.notify as notify_mod

    def boom(text, cfg=None, client=None):
        raise RuntimeError("telegram недоступен")

    monkeypatch.setattr(notify_mod, "notify_telegram", boom)
    runner, _, _ = _cycle_runner(tmp_path, fail_collect=True)
    runner.run_cycle_sync()
    assert "цикл прошёл" in runner.cycle_note      # цикл дошёл до конца


def test_clean_cycle_does_not_ping_the_operator(tmp_path, monkeypatch):
    # Уведомление — про деградацию, а не про каждый прогон: иначе оператор
    # перестанет читать сообщения ровно к тому дню, когда что-то сломается.
    import cf.notify as notify_mod

    sent = []
    monkeypatch.setattr(notify_mod, "notify_telegram",
                        lambda text, cfg=None, client=None: sent.append(text) or True)
    runner, _, _ = _cycle_runner(tmp_path)
    runner.run_cycle_sync()
    assert sent == []


def test_cycle_writes_its_own_runlog_row(tmp_path, monkeypatch):
    # Правило №6: у цикла КАК ЦЕЛОГО не было строки в Run Log вовсе — звенья
    # писали каждое за себя, а прогон целиком не логировался никем.
    # now_iso раннера подменён: started_at обязан быть моментом СТАРТА цикла, а не
    # моментом записи строки — иначе длительность в Run Log нулевая (см. CLAUDE.md
    # про `cf log-run --started-at`).
    monkeypatch.setattr(runner_mod, "now_iso", lambda: "2026-07-27T01:15:00+00:00")
    runner, sheets, _ = _cycle_runner(tmp_path, fail_collect=True)
    runner.run_cycle_sync()
    rows = [r for _tab, r in sheets.appended if r["agent"] == "dashboard-cycle"]
    assert len(rows) == 1
    assert rows[0]["status"] == "insufficient_data"      # деградация, не failed
    assert "Собранные ролики" in rows[0]["errors"]
    assert rows[0]["input_summary"] == runner.cycle_note  # итог, а не только сбой
    assert rows[0]["started_at"] == "2026-07-27T01:15:00+00:00"
    assert rows[0]["started_at"] < rows[0]["completed_at"]


def test_clean_cycle_logs_success_row(tmp_path):
    runner, sheets, _ = _cycle_runner(tmp_path)
    runner.run_cycle_sync()
    row = [r for _tab, r in sheets.appended if r["agent"] == "dashboard-cycle"][0]
    assert row["status"] == "success" and row["errors"] == "[]"


def test_busy_stage_still_stops_the_cycle_and_leaves_a_trace(tmp_path):
    # Единственный оставшийся жёсткий выход: звено занято другим прогоном —
    # ехать дальше значило бы писать в те же данные вдвоём.
    runner, sheets, commands = _cycle_runner(tmp_path)
    runner.state["factory"]["status"] = "running"
    runner.run_cycle_sync()
    assert commands == []                                 # мьютекс не пустил и raw
    row = [r for _tab, r in sheets.appended if r["agent"] == "dashboard-cycle"][0]
    assert row["status"] == "skipped"                     # осознанно не состоялся
    assert "Контент-завод" in runner.cycle_note


def test_cycle_note_survives_restart(tmp_path):
    # cf-dashboard ходит с Restart=always: заметка, жившая только в памяти
    # процесса, исчезала вместе с ним — утром оператор не видел ни слова о ночи.
    ppath = tmp_path / "pipeline-progress.json"
    runner, _, _ = _cycle_runner(tmp_path, cmd_code=1, progress_path=ppath)
    runner.run_cycle_sync()
    note = runner.cycle_note
    assert "Собранные ролики" in note

    revived, _, _ = _cycle_runner(tmp_path, progress_path=ppath)
    assert revived.cycle_note == note


def test_progress_snapshot_keeps_step_keys_intact(tmp_path):
    # Заметка лежит РЯДОМ с шагами: чужой ключ не должен ни попасть в
    # run_progress (его читает build_timeline), ни сломать восстановление шагов.
    ppath = tmp_path / "pipeline-progress.json"
    runner, _, _ = _cycle_runner(tmp_path, progress_path=ppath)
    runner.run_cycle_sync()
    saved = json.loads(ppath.read_text(encoding="utf-8"))
    assert saved["collect"]["status"] in ("done", "idle", "warn", "error")
    assert runner_mod.CYCLE_NOTE_KEY not in runner.run_progress


# ── Разбор 2026-07-27: вердикт судьи не переживает правку черновика ───────────


def _formula_gate_env(tmp_path, verdict_extra=None, judge=True):
    """Черновик рецепта + установленный судья + его вердикт (mtime по порядку)."""
    root = tmp_path / "repo"
    draft = root / "formulas" / "мужской-стиль" / "alpha.json"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(json.dumps({"name": "alpha", "niche": "мужской-стиль",
                                 "status": "proposed", "version": 2},
                                ensure_ascii=False), encoding="utf-8")
    if judge:
        cmd = root / ".claude" / "commands" / "cf-review-formula.md"
        cmd.parent.mkdir(parents=True, exist_ok=True)
        cmd.write_text("судья", encoding="utf-8")
    review = (root / "agent-runtime" / "reviews-formula"
              / "2026-07-27-alpha-review.json")
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(json.dumps({"name": "alpha", "verdict": "recommend",
                                  **(verdict_extra or {})}, ensure_ascii=False),
                      encoding="utf-8")
    return draft, review


def _gate_argv(tmp_path):
    """Прогнать воркер ворот рецептов и вернуть argv решения по рецепту."""
    runner, _, commands = _cycle_runner(tmp_path)
    runner._run_formula_gate_worker(
        "factory", {"exclude_niches": [], "prompt_drafts_per_run": 2}, [])
    argv = [[str(a) for a in c] for c in commands
            if "auto-approve-formula" in [str(a) for a in c]]
    return runner, argv


def test_stale_verdict_does_not_approve_a_rewritten_draft(tmp_path):
    """Ворота рецептов стоят на "auto", а черновик перезаписывается v+1 каждым
    прогоном темы: вердикт, написанный ДО правки, одобрял бы никем не читанный
    рецепт. Сбой судьи на хвосте цикла уже случался (27.07 01:15)."""
    draft, review = _formula_gate_env(tmp_path)
    os.utime(review, (1_700_000_000, 1_700_000_000))    # вердикт вчерашний
    os.utime(draft, (1_700_003_600, 1_700_003_600))     # черновик переписан позже

    runner, argv = _gate_argv(tmp_path)
    assert argv and "--review" not in argv[0]           # вердикт не подставлен
    assert "--no-judge" not in argv[0]                  # и строгость не снижена
    reason = runner.run_progress["formula-review"]["idle_reason"]
    assert "без вердикта судьи" in reason
    assert "не видел эту версию" in reason              # причина, а не «нет файла»
    assert any("вердикт судьи не принят" in r["text"]
               for r in runner.reports["factory"])


def test_fresh_verdict_is_still_used(tmp_path):
    # Обратная сторона: вердикт, написанный ПОСЛЕ черновика, работает как раньше.
    draft, review = _formula_gate_env(tmp_path)
    os.utime(draft, (1_700_000_000, 1_700_000_000))
    os.utime(review, (1_700_003_600, 1_700_003_600))

    _runner, argv = _gate_argv(tmp_path)
    assert argv and "--review" in argv[0] and str(review) in argv[0]


def test_verdict_about_another_version_is_refused(tmp_path):
    # Forward-compatible сверка: поле version судья пока не пишет (его промпт под
    # deny-правилами), но как только появится — расхождение обязано остановить.
    _draft, _review = _formula_gate_env(tmp_path, verdict_extra={"version": 1})

    runner, argv = _gate_argv(tmp_path)
    assert argv and "--review" not in argv[0]
    assert "версии version=1" in runner.run_progress["formula-review"]["idle_reason"]


def test_verdict_without_version_field_is_accepted(tmp_path):
    # Сегодняшний формат вердикта версии не называет — отказывать по её
    # отсутствию значило бы остановить ворота рецептов целиком.
    _draft, review = _formula_gate_env(tmp_path)
    _runner, argv = _gate_argv(tmp_path)
    assert argv and str(review) in argv[0]


def test_verdict_with_mismatched_sha_is_refused(tmp_path):
    _draft, _review = _formula_gate_env(
        tmp_path, verdict_extra={"formula_sha": "0" * 64})
    runner, argv = _gate_argv(tmp_path)
    assert argv and "--review" not in argv[0]
    assert "formula_sha" in runner.run_progress["formula-review"]["idle_reason"]


# ── Разбор 2026-07-27: отказ фиксера выпускает бриф из его очереди ────────────


def _fix_runner(tmp_path, briefs, code=0):
    sheets = FakeSheets({"run_log": [], "briefs": briefs})
    runner, _sheets, commands = _cycle_runner(tmp_path, cmd_code=code, sheets=sheets)
    cmd = runner.root / ".claude" / "commands" / "cf-fix-brief.md"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("доработчик", encoding="utf-8")
    return runner, sheets, commands


def _revised(brief_id, notes=""):
    return {"brief_id": brief_id, "formula_id": "alpha", "review_status": "revised",
            "reviewer_notes": notes, "generated_at": "2026-07-26T00:00:00+00:00"}


def test_refused_fix_leaves_the_queue_instead_of_burning_a_call_forever(tmp_path):
    """FIX_MARKER пишет ТОЛЬКО успешная правка (`cf revise-brief <id> --file`).

    Фиксер, отказавшийся переписывать (а он ОБЯЗАН отказываться, когда сценарий
    требует несуществующего события), не оставлял следа — и бриф висел в очереди
    доработки вечно, тратя платный вызов агента каждый цикл. На 27.07 так заперты
    b-own-event-announcement-20260726-founder-opening и …-20260727-popup-offers.
    """
    runner, _sheets, commands = _fix_runner(tmp_path, [_revised("b-1")])
    runner._run_fix_worker("factory", [], 0)

    refusals = [[str(a) for a in c] for c in commands
                if "--refused" in [str(a) for a in c]]
    assert len(refusals) == 1
    argv = refusals[0]
    assert "revise-brief" in argv
    assert argv[argv.index("--brief-id") + 1] == "b-1"
    assert argv[argv.index("--notes") + 1] == runner_mod.FIXER_REFUSED_NOTE
    assert "--file" not in argv                      # это не правка сценария
    assert "снято с доработки" in runner.run_progress["fix"]["idle_reason"]


def test_rewritten_brief_is_not_marked_refused(tmp_path):
    # Тот, кого раннер только что применил, к человеку не уходит: иначе фиксер
    # отменял бы собственную работу.
    runner, _sheets, commands = _fix_runner(tmp_path, [_revised("b-1")])
    fixed = runner.root / "agent-runtime" / "briefs" / "b-1-fixed.json"
    fixed.parent.mkdir(parents=True, exist_ok=True)
    fixed.write_text(json.dumps({"brief_id": "b-1"}), encoding="utf-8")

    runner._run_fix_worker("factory", [], 0)

    assert not [c for c in commands if "--refused" in [str(a) for a in c]]


def test_brief_fixed_by_the_agent_itself_is_not_marked_refused(tmp_path):
    # Метку мог поставить и сам агент, если разрешения харнесса ему позволили:
    # очередь перечитывается, и такой бриф второй раз не трогаем.
    runner, sheets, commands = _fix_runner(tmp_path, [_revised("b-1")])

    def agent_fixed(argv):
        commands.append(argv)
        # агент сам дошёл до `cf revise-brief` — метка в листе появилась
        sheets.tables["briefs"][0]["reviewer_notes"] = "доработано заводом: CTA"
        return (0, '{"result": "ок"}')

    runner.run_command = agent_fixed
    runner._run_fix_worker("factory", [], 0)

    assert not [c for c in commands if "--refused" in [str(a) for a in c]]


def test_unreadable_briefs_do_not_mark_anything_refused(tmp_path):
    # Правило №2: пометка «завод не смог» по НЕпрочитанным данным — это вывод
    # из отсутствия данных, а не из данных.
    class Flaky(FakeSheets):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.reads = 0

        def read_rows(self, tab_key, include_heavy=False):
            if tab_key == "briefs":
                self.reads += 1
                if self.reads > 1:
                    raise ConnectionError("sheets 503")
            return super().read_rows(tab_key, include_heavy)

    sheets = Flaky({"run_log": [], "briefs": [_revised("b-1")]})
    runner, _sheets, commands = _cycle_runner(tmp_path, sheets=sheets)
    cmd = runner.root / ".claude" / "commands" / "cf-fix-brief.md"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("доработчик", encoding="utf-8")

    runner._run_fix_worker("factory", [], 0)

    assert not [c for c in commands if "--refused" in [str(a) for a in c]]


# ── Разбор 2026-07-27, второй заход: дедлайн шага съел генератор брифов ───────
#
# Прогон 27.07 18:xx: длительности шага «Сценарии» шли 1230 -> 954 -> 1654 ->
# 1700 -> 1801.9 при SUBPROCESS_TIMEOUT=1800. На пятом claude убили SIGKILL ровно
# на границе: исключение летит ДО _add_report, поэтому отчёта не осталось вовсе,
# звено ушло в деградацию с «генерация брифов упала», а сценариев за сутки —
# ноль. Цифры роста четыре прогона лежали в step-durations.json и молчали.


def test_subprocess_deadline_comes_from_config(tmp_path):
    """Дедлайн двигается строкой в конфиге, а не правкой кода."""
    cfg = json.loads(json.dumps(CONFIG))
    cfg.setdefault("dashboard", {}).setdefault("fanout", {})["subprocess_timeout"] = 120
    runner = StageRunner(FakeSheets({"run_log": []}), cfg,
                         http_post=lambda url: None,
                         locks_dir=str(tmp_path / "locks"),
                         analysis_dir=tmp_path / "analysis",
                         root=tmp_path / "repo")
    assert runner.subprocess_timeout == 120
    # значение зашито в дефолтный run_command, а не потеряно в поле
    assert runner.run_command.keywords["timeout"] == 120
    # без ключа — модульный дефолт, поднятый до часа
    plain = StageRunner(FakeSheets({"run_log": []}), CONFIG,
                        http_post=lambda url: None,
                        locks_dir=str(tmp_path / "locks2"),
                        analysis_dir=tmp_path / "analysis",
                        root=tmp_path / "repo")
    assert plain.subprocess_timeout == runner_mod.SUBPROCESS_TIMEOUT == 3600


def test_default_run_honours_injected_timeout(tmp_path):
    """_default_run уважает переданный дедлайн, а не только модульную константу."""
    with pytest.raises(RuntimeError, match="превысил таймаут 1с"):
        runner_mod._default_run(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)


def test_step_close_to_the_deadline_is_reported_before_it_dies(tmp_path):
    """Подход к потолку — деградация звена, а не молчание до первого SIGKILL."""
    runner, _sheets, _cmds = _cycle_runner(tmp_path)
    runner.subprocess_timeout = 100
    ticks = iter([0.0, 85.0, 0.0, 10.0])          # первый шаг 85с, второй 10с
    runner.now = lambda: next(ticks)

    problems = []
    runner._fanout_claude("factory", "/cf-generate-briefs", "генерация брифов",
                          problems, expect_agent=None)
    assert any("85с из дедлайна 100с" in p for p in problems)
    assert any("subprocess_timeout" in p for p in problems)

    quiet = []
    runner._fanout_claude("factory", "/cf-review-brief", "ревью брифов", quiet,
                          expect_agent=None)
    assert quiet == []                            # 10 из 100 — тревожить незачем


def test_crashed_fixer_agent_does_not_burn_every_briefs_single_attempt(tmp_path):
    # Ревью 14.09.2026: результат вызова агента не проверялся, и таймаут claude,
    # кончившаяся квота или недоверенный воркспейс помечали ВСЕ брифы очереди как
    # «фиксер отказался» — единственная попытка сгорала без единой попытки агента.
    runner, _sheets, commands = _fix_runner(tmp_path, [_revised("b-1"), _revised("b-2")])

    def run_command(argv):
        commands.append(argv)
        if argv and argv[0] == "claude":
            return (1, "")                           # агент упал до ответа
        return (0, '{"result": "ок"}')

    runner.run_command = run_command
    problems = []
    runner._run_fix_worker("factory", problems, 0)

    assert not [c for c in commands if "--refused" in [str(a) for a in c]]
    assert any("доработка сценариев" in p for p in problems)
    assert runner.run_progress["fix"]["status"] == "warn"
