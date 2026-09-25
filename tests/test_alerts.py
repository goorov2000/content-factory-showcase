"""Приглушение повторов: одна поломка — одно сообщение, а не одно в сутки.

Образец из жизни: сбор лежал из-за Apify пять суток, и владелец пять утр
подряд получал одинаковую стену текста.
"""
from datetime import datetime, timedelta

from cf.alerts import track

DAY1 = datetime(2026, 8, 9, 7, 30)


def at(days):
    return DAY1 + timedelta(days=days)


def test_first_failure_speaks_immediately(tmp_path):
    p = tmp_path / "state.json"
    assert track("collect:tiktok", True, kind="failed", now=DAY1,
                 path=p) == (True, 1, False)


def test_second_run_same_day_stays_silent(tmp_path):
    p = tmp_path / "state.json"
    track("collect:tiktok", True, kind="failed", now=DAY1, path=p)
    # ручной перезапуск сбора через час — новостей нет, молчим
    send, day, _ = track("collect:tiktok", True, kind="failed",
                         now=DAY1 + timedelta(hours=1), path=p)
    assert send is False and day == 1


def test_reminds_on_schedule_and_is_quiet_between(tmp_path):
    p = tmp_path / "state.json"
    track("collect:tiktok", True, kind="failed", now=DAY1, path=p)
    spoke = [d for d in range(1, 32)
             if track("collect:tiktok", True, kind="failed", now=at(d - 1),
                      path=p)[0]]
    # день 1 уже сказан выше; напоминания — 2, 3, 7, 14, 30
    assert spoke == [2, 3, 7, 14, 30]


def test_message_knows_which_day_it_is(tmp_path):
    # сбор ходит каждое утро, поэтому и считаем по-дневно
    p = tmp_path / "state.json"
    for d in range(7):
        send, day, _ = track("collect:tiktok", True, kind="failed", now=at(d),
                             path=p)
    assert (send, day) == (True, 7)


def test_problem_unheard_of_for_days_counts_as_new(tmp_path):
    # systemd сообщает о падении юнита только в момент отказа. Если бы старая
    # запись жила вечно, повтор через сто дней попал бы в пустое расписание
    # напоминаний и ушёл бы в тишину.
    p = tmp_path / "state.json"
    track("unit:cf-backup.service", True, now=DAY1, path=p)
    assert track("unit:cf-backup.service", True, now=at(100),
                 path=p) == (True, 1, False)


def test_recovery_is_announced_once(tmp_path):
    p = tmp_path / "state.json"
    track("collect:tiktok", True, kind="failed", now=DAY1, path=p)
    assert track("collect:tiktok", False, kind="success", now=at(2),
                 path=p) == (False, 0, True)
    # второй успех подряд — молчим, тема уже закрыта
    assert track("collect:tiktok", False, kind="success", now=at(3),
                 path=p) == (False, 0, False)


def test_breaking_again_after_repair_starts_over(tmp_path):
    p = tmp_path / "state.json"
    track("collect:tiktok", True, kind="failed", now=DAY1, path=p)
    track("collect:tiktok", False, kind="success", now=at(1), path=p)
    assert track("collect:tiktok", True, kind="failed", now=at(2),
                 path=p) == (True, 1, False)


def test_different_breakage_is_different_news(tmp_path):
    # «сбор упал» и «сбор ничего не привёз» — разные новости, вторая не должна
    # утонуть в приглушении первой
    p = tmp_path / "state.json"
    track("collect:tiktok", True, kind="failed", now=DAY1, path=p)
    send, day, _ = track("collect:tiktok", True, kind="insufficient_data",
                         now=DAY1, path=p)
    assert (send, day) == (True, 1)


def test_collectors_are_tracked_separately(tmp_path):
    p = tmp_path / "state.json"
    track("collect:tiktok", True, kind="failed", now=DAY1, path=p)
    assert track("collect:instagram", True, kind="failed", now=DAY1,
                 path=p)[0] is True


def test_broken_state_file_never_swallows_an_alert(tmp_path):
    # направление отказа — в сторону болтливости: молча проглотить настоящую
    # аварию хуже, чем повториться
    p = tmp_path / "state.json"
    p.write_text("{это не json", encoding="utf-8")
    assert track("collect:tiktok", True, kind="failed", now=DAY1,
                 path=p)[0] is True


def test_unwritable_state_still_sends(tmp_path):
    # каталог, в который нельзя писать: сообщение всё равно уходит
    p = tmp_path / "нет-такого-каталога" / "state.json"
    p.parent.mkdir()
    p.parent.chmod(0o500)
    try:
        assert track("collect:tiktok", True, kind="failed", now=DAY1,
                     path=p)[0] is True
    finally:
        p.parent.chmod(0o700)
