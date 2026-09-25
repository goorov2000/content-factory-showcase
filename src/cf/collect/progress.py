"""Маркеры прогресса сбора: единственный контракт между `cf collect` и дашбордом.

Спека: docs/superpowers/specs/2026-07-25-cf-pipeline-timeline-design.md §7.

Сбор идёт подпроцессом (`python -m cf collect tiktok`), поэтому «что сейчас
происходит» умеет рассказать только он сам. Печатаем строки-маркеры в stdout, а
раннер дашборда читает stdout ПОТОКОВО и превращает их в прогресс ленты.

Формат нарочно плоский и однострочный (`CF_PROGRESS phase=fetch done=3 total=8`):
его видно в логах юнита глазами, он не ломает существующие сводки и парсится без
зависимостей. Писать и читать формат — только через этот модуль, чтобы отправитель
и получатель не разошлись.
"""

PREFIX = "CF_PROGRESS"

# Фазы одной единицы работы сбора (веса и подписи — на стороне дашборда,
# progress.COLLECT_PHASES): подготовка → выкачка батчей → гейт → запись.
PHASES = ("prepare", "fetch", "gate", "write")


def emit(log, phase, done=None, total=None, rows=None, new=None):
    """Напечатать маркер фазы. ``log`` None → тихо ничего (сбор не зависит от UI).

    ``rows``/``new`` — итог единицы работы: сколько роликов записано и сколько из
    них новых. Лента показывает оператору именно их («118 роликов · 24 новых»), а
    не «2/2 платформ»: платформы — это как считает машина, ролики — то, за чем
    он сюда пришёл.

    Неизвестная фаза — не исключение: сбор важнее телеметрии, поэтому просто
    печатаем как есть, а дашборд незнакомую фазу игнорирует (вес 0).

    Числа приводятся по одному и в try: ``rows``/``new`` приезжают из
    ``upsert_rows`` уже ПОСЛЕ записи строк, и падение на форматировании оборвало
    бы сбор между записью и сводкой прогона.
    """
    if log is None:
        return
    parts = [PREFIX, f"phase={phase}"]
    for key, value in (("done", done), ("total", total),
                       ("rows", rows), ("new", new)):
        if value is None:
            continue
        try:
            parts.append(f"{key}={int(value)}")
        except (TypeError, ValueError):
            continue      # битое число — фаза важнее счётчика (симметрично parse)
    try:
        log(" ".join(parts))
    except Exception:      # noqa: BLE001 — печать прогресса не ломает сбор
        pass


def parse(line):
    """Строка stdout → dict маркера или None, если это не маркер.

    Возвращает только присутствующие ключи: ``phase`` (str), ``done``/``total``/
    ``rows``/``new`` (int). Битые числа отбрасываются — лучше показать фазу без
    счётчика, чем уронить чтение потока.
    """
    text = str(line or "").strip()
    if not text.startswith(PREFIX):
        return None
    marker = {}
    for token in text[len(PREFIX):].split():
        key, sep, value = token.partition("=")
        if not sep:
            continue
        if key == "phase":
            marker["phase"] = value
        elif key in ("done", "total", "rows", "new"):
            try:
                marker[key] = int(value)
            except ValueError:
                continue
    return marker or None


def is_marker(line):
    """Маркер ли это — чтобы не тащить телеметрию в «Отчёты этапов» оператору."""
    return str(line or "").strip().startswith(PREFIX)
