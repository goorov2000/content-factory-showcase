"""Сторож: замечает, что завод не отработал, и говорит об этом владельцу.

Дыра, ради которой он делался: единственный отправитель уведомлений жил внутри
дашборда, поэтому его собственную смерть заметить было некому — тишина в чате
означала одновременно «всё хорошо» и «завод умер».
"""
import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import pytest

import cf.notify as notify_mod
from cf.cli import cmd_alert_unit, cmd_heartbeat
from tests.fakes import FakeSheets


@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(notify_mod, "notify_telegram",
                        lambda text, cfg=None, client=None: box.append(text) or True)
    return box


def progress(tmp_path, hours_ago=None):
    p = tmp_path / "pipeline-progress.json"
    data = {}
    if hours_ago is not None:
        at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        data["__cycle__"] = {"at": at.isoformat(), "note": "прогон"}
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def args(path, **kw):
    return Namespace(progress=str(path), max_hours=kw.pop("max_hours", None),
                     **kw)


def sheets():
    return FakeSheets(tables={"run_log": []},
                      config={"dashboard": {"public_origin": "https://x.test"}})


def test_healthy_factory_stays_silent(tmp_path, sent):
    assert cmd_heartbeat(sheets(), args(progress(tmp_path, hours_ago=15))) == 0
    assert sent == []


def test_silent_factory_is_reported(tmp_path, sent):
    assert cmd_heartbeat(sheets(), args(progress(tmp_path, hours_ago=31))) == 0
    assert len(sent) == 1
    assert "Завод не отработал" in sent[0]


def test_missing_progress_file_is_reported_not_swallowed(tmp_path, sent):
    assert cmd_heartbeat(sheets(), args(tmp_path / "нет.json")) == 0
    assert sent and "не читается" in sent[0]


def test_watchdog_does_not_nag_twice_in_a_day(tmp_path, sent):
    p = progress(tmp_path, hours_ago=31)
    cmd_heartbeat(sheets(), args(p))
    cmd_heartbeat(sheets(), args(p))
    assert len(sent) == 1


def test_watchdog_says_when_the_factory_is_back(tmp_path, sent):
    cmd_heartbeat(sheets(), args(progress(tmp_path, hours_ago=31)))
    sent.clear()
    cmd_heartbeat(sheets(), args(progress(tmp_path, hours_ago=2)))
    assert sent and "снова отрабатывает" in sent[0]


def test_watchdog_always_exits_zero(tmp_path, sent):
    # ненулевой код поднял бы OnFailure= самого сторожа и удвоил сообщение
    assert cmd_heartbeat(sheets(), args(progress(tmp_path, hours_ago=99))) == 0


def test_failed_unit_reaches_the_owner(sent):
    assert cmd_alert_unit(sheets(), Namespace(unit="cf-backup.service")) == 0
    assert sent and "резервная копия таблиц" in sent[0]


def test_same_unit_failing_twice_today_speaks_once(sent):
    cmd_alert_unit(sheets(), Namespace(unit="cf-backup.service"))
    cmd_alert_unit(sheets(), Namespace(unit="cf-backup.service"))
    assert len(sent) == 1


def test_different_units_are_reported_separately(sent):
    cmd_alert_unit(sheets(), Namespace(unit="cf-backup.service"))
    cmd_alert_unit(sheets(), Namespace(unit="cf-archive.service"))
    assert len(sent) == 2
