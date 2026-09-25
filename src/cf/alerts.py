"""Приглушение повторов: одна поломка — одно сообщение, а не одно в сутки.

Зачем (разбор 2026-08-09 по скриншоту чата): когда сбор лёг из-за Apify,
владелец получал одинаковый алерт каждое утро пятые сутки подряд. Человек в
такой ситуации не чинит быстрее — он перестаёт читать бота, а следом
пропускает и всё остальное, включая единственное сообщение, которое требует
его решения.

Политика: сказать сразу, напомнить на 2-й, 3-й, 7-й, 14-й и 30-й день (дальше
раз в месяц) и обязательно сказать, когда починилось. Считаются ДНИ, а не
прогоны: сбор запускается таймером раз в сутки, но руками — сколько угодно раз.

Смена характера поломки (`kind`) считается новой проблемой: «сбор упал» и
«сбор ничего не привёз» — разные новости, и вторая не должна утонуть в
приглушении первой.

Направление отказа — в сторону болтливости: любая беда с файлом состояния
означает «слать». Молча проглотить настоящую аварию хуже, чем повториться.
"""
import logging
from datetime import date, datetime
from pathlib import Path

from cf.io import read_json, write_json_atomic

logger = logging.getLogger(__name__)

# Runtime-артефакт (железное правило №4): не версионируется.
STATE_PATH = Path("agent-runtime/alerts-state.json")

# На какой день держащейся проблемы напоминать. Дальше — раз в REPEAT_EVERY дней.
REMIND_DAYS = (1, 2, 3, 7, 14, 30)
REPEAT_EVERY = 30

# Проблема, о которой не было слышно дольше этого срока, считается НОВОЙ.
# Без этого правила беда, о которой сообщают только в момент отказа (падение
# systemd-юнита: OnFailure= срабатывает и молчит, пока не упадёт снова),
# застревала бы в приглушении навсегда: на 100-й день после первого отказа
# расписание напоминаний уже пустое, и повтор ушёл бы в тишину.
STALE_DAYS = 3


def _state_path(path=None):
    return Path(path) if path else STATE_PATH


def _load(path):
    try:
        data = read_json(path)
    except Exception:  # noqa: BLE001 — нет файла / битый JSON: начинаем с чистого
        return {}
    return data if isinstance(data, dict) else {}


def _save(path, state):
    try:
        write_json_atomic(path, state)
    except Exception:  # noqa: BLE001 — не сохранили: в худшем случае повторимся
        logger.warning("состояние тревог не сохранено", exc_info=True)


def _day_number(since, today):
    """Какой день идёт проблема: первый — 1."""
    try:
        started = date.fromisoformat(str(since))
    except (TypeError, ValueError):
        return 1
    return max(1, (today - started).days + 1)


def is_remind_day(day):
    top = max(REMIND_DAYS)
    if day in REMIND_DAYS:
        return True
    return day > top and (day - top) % REPEAT_EVERY == 0


def track(key, broken, kind=None, now=None, path=None):
    """Обновить состояние проблемы `key` и решить, писать ли человеку.

    Возвращает (send, day, recovered):
      send      — слать сообщение сейчас;
      day       — какой день держится проблема (1 — первый);
      recovered — проблема была и ушла (повод сказать «снова работает»).
    """
    path = _state_path(path)
    state = _load(path)
    entry = state.get(key)
    if not isinstance(entry, dict):
        entry = None
    today = (now or datetime.now()).date()
    stamp = today.isoformat()

    if not broken:
        if entry is None:
            return False, 0, False
        state.pop(key, None)
        _save(path, state)
        return False, 0, True

    # новая проблема — сломалось иначе, чем вчера, или давно не было слышно
    gap = _day_number(entry.get("last_seen") or entry.get("since"), today) - 1 \
        if entry else 0
    if (entry is None or gap > STALE_DAYS
            or (kind is not None and entry.get("kind") != kind)):
        state[key] = {"since": stamp, "last_sent": stamp, "last_seen": stamp,
                      "kind": kind}
        _save(path, state)
        return True, 1, False

    day = _day_number(entry.get("since"), today)
    entry["last_seen"] = stamp
    if entry.get("last_sent") == stamp or not is_remind_day(day):
        state[key] = entry          # сегодня уже говорили / не день напоминания
        _save(path, state)
        return False, day, False
    entry["last_sent"] = stamp
    state[key] = entry
    _save(path, state)
    return True, day, False
