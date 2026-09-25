import json
from datetime import datetime, timedelta, timezone

import pytest

from cf import runlog

from tests.fakes import FakeSheets


def test_log_run_appends_row_to_run_log():
    sheets = FakeSheets()
    row = runlog.log_run(sheets, agent="raw-batch-profiler", status="success",
                         input_summary="raw_tiktok", output_paths=["agent-runtime/x.json"])
    tab, appended = sheets.appended[0]
    assert tab == "run_log"
    assert appended["agent"] == "raw-batch-profiler"
    assert appended["status"] == "success"
    assert json.loads(appended["output_paths"]) == ["agent-runtime/x.json"]
    assert row["run_id"] and row["started_at"] and row["completed_at"]


def test_log_run_rejects_unknown_status():
    with pytest.raises(ValueError):
        runlog.log_run(FakeSheets(), agent="x", status="meh")


# --- cf log-run --started-at: реальная длительность прогона (аудит 2026-07-26) ---

def test_started_at_arg_normalizes_to_now_iso_format():
    from cf.cli import _started_at_arg
    from cf.runlog import now_iso
    # формат обязан совпасть с completed_at, иначе median_duration читает
    # разнородные строки, а _agent_logged сравнивает несравнимое
    assert _started_at_arg("2026-07-26T09:15:00+00:00") == "2026-07-26T09:15:00+00:00"
    assert _started_at_arg("2026-07-26T09:15:00Z") == "2026-07-26T09:15:00+00:00"
    assert _started_at_arg(" 2026-07-26T09:15:00.123456Z ") == "2026-07-26T09:15:00+00:00"
    # naive: часы агента и хоста одни, считаем UTC
    assert _started_at_arg("2026-07-26T09:15:00") == "2026-07-26T09:15:00+00:00"
    # другая зона приводится к UTC
    assert _started_at_arg("2026-07-26T12:15:00+03:00") == "2026-07-26T09:15:00+00:00"
    assert len(now_iso()) == len("2026-07-26T09:15:00+00:00")


def test_garbage_started_at_still_logs_the_row(capsys):
    # Правило №6 старше точности метрики: argparse-exit на мусоре означал бы, что
    # строки в Run Log нет вообще, а StageRunner._agent_logged объявил бы прогон
    # ниши холостым и перевёл её в error — хуже, чем нулевая длительность.
    from cf.cli import _started_at_arg
    assert _started_at_arg("вчера вечером") is None
    err = capsys.readouterr().err
    assert "warning" in err and "ISO8601" in err


def test_cmd_log_run_passes_started_at_and_echoes_it(capsys):
    import argparse
    from cf.cli import cmd_log_run
    sheets = FakeSheets()
    # старт заведомо в прошлом: тест не должен зависеть от часов машины
    started = (datetime.now(timezone.utc) - timedelta(minutes=17)).isoformat(
        timespec="seconds")
    rc = cmd_log_run(sheets, argparse.Namespace(
        agent="niche-pipeline", status="success", input="raw_tiktok/стритвир",
        outputs=[], errors=[], trigger="manual", started_at=started))
    assert rc == 0
    _, appended = sheets.appended[0]
    assert appended["started_at"] == started
    assert appended["completed_at"] > appended["started_at"]   # длительность НЕ нулевая
    out = capsys.readouterr()
    assert f"started_at={started}" in out.out
    assert "в будущем" not in out.err


def test_cmd_log_run_without_flag_keeps_old_behaviour(capsys):
    import argparse
    from cf.cli import cmd_log_run
    sheets = FakeSheets()
    cmd_log_run(sheets, argparse.Namespace(
        agent="cleanup", status="success", input="", outputs=[], errors=[],
        trigger="manual", started_at=None))
    _, appended = sheets.appended[0]
    assert appended["started_at"] == appended["completed_at"]   # как было до правки
    assert "started_at=" not in capsys.readouterr().out


def test_future_started_at_warns_but_logs(capsys):
    import argparse
    from cf.cli import cmd_log_run
    sheets = FakeSheets()
    cmd_log_run(sheets, argparse.Namespace(
        agent="x", status="success", input="", outputs=[], errors=[],
        trigger="manual", started_at="2099-01-01T00:00:00+00:00"))
    assert sheets.appended                      # строка всё равно записана
    assert "в будущем" in capsys.readouterr().err


def test_parser_accepts_started_at_flag():
    from cf.cli import build_parser
    args = build_parser().parse_args(
        ["log-run", "--agent", "x", "--status", "success",
         "--started-at", "2026-07-26T09:15:00Z"])
    assert args.started_at == "2026-07-26T09:15:00+00:00"


def test_iso_seconds_matches_now_iso_shape():
    from cf.collect.util import _iso_seconds
    # collected_at приходит с миллисекундами и 'Z' — приводим к виду now_iso()
    assert _iso_seconds("2026-07-25T04:27:17.269Z") == "2026-07-25T04:27:17+00:00"
    assert _iso_seconds("") is None
    assert _iso_seconds("не дата") is None
