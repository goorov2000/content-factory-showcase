"""Структурная проверка systemd-пары cf-collect-metrika (UTM-контур, тикет 06).

Юниты — текст, их никто не импортирует: дрейф (ExecStart мимо .venv, таймер не
в 08:20) всплыл бы только на проде. Проверяем по образцу существующих юнитов
сбора: oneshot из корня репо, расписание строго между raw (08:00) и
performance (08:40) — сборщик Метрики не должен толкаться с ними за stage-локи.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "deploy" / "systemd"


def _oncalendar_time(name):
    text = (UNITS / name).read_text(encoding="utf-8")
    match = re.search(r"^OnCalendar=\*-\*-\* (\d{2}:\d{2}):00$", text, re.MULTILINE)
    assert match, f"{name}: нет ежедневного OnCalendar"
    return match.group(1)


def test_metrika_service_runs_cf_collect_metrika_from_venv():
    service = (UNITS / "cf-collect-metrika.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert "WorkingDirectory=/home/<user>/projects/CF" in service
    assert ("ExecStart=/home/<user>/projects/CF/.venv/bin/python -m cf collect metrika"
            in service)


def test_metrika_timer_fires_daily_0820_and_persists_missed_runs():
    timer = (UNITS / "cf-collect-metrika.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 08:20:00" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_metrika_timer_sits_between_raw_and_performance():
    # Решение спеки (тикет 06): 08:20 — между raw-сбором и дозамером метрик.
    assert (_oncalendar_time("cf-collect-tiktok.timer")
            < _oncalendar_time("cf-collect-metrika.timer")
            < _oncalendar_time("cf-collect-performance.timer"))


def test_install_sh_hints_enabling_the_metrika_timer():
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "cf-collect-metrika.timer" in script
