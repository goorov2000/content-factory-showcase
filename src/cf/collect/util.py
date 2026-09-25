"""Общие хелперы сбора — порт 1:1 хелперных секций n8n/*/code/*.js.

Семантика намеренно JS-овая (first пропускает только None/'', num коэрсит как
Number(), stable_hash считает по UTF-16 code units): raw_id и метрики должны
байт-в-байт совпадать с рядами, которые писал n8n, иначе upsert по raw_id
надублирует строки. Эталоны заморожены до этапа 6 миграции.
"""
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


def js_round(x):
    """JS Math.round: floor(x + 0.5) — не банковское округление Python."""
    return math.floor(x + 0.5)


def first(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def _canon_number(n):
    if not math.isfinite(n):
        return 0
    if isinstance(n, float) and n.is_integer() and abs(n) < 2 ** 53:
        return int(n)
    return n


def num(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return _canon_number(float(value)) if isinstance(value, float) else value
    if value is None:
        return 0
    s = str(value).strip()
    if not s:
        return 0
    try:
        return _canon_number(float(s))
    except ValueError:
        return 0


_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def stable_hash(value):
    text = str(value) if value else ""
    h = 0
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        code = units[i] | (units[i + 1] << 8)
        h = ((h << 5) - h + code) & 0xFFFFFFFF
    if h >= 2 ** 31:  # |0 в JS — знаковый 32-бит; затем Math.abs
        h = 2 ** 32 - h
    if h == 0:
        return "0"
    out = ""
    while h:
        h, r = divmod(h, 36)
        out = _BASE36[r] + out
    return out


def _iso_ms(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (dt.microsecond // 1000)


def _parse_date_string(s):
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_seconds(value):
    """ISO-штамп сбора -> формат cf.runlog.now_iso() (секунды, +00:00).

    started_at в CF Run Log обязан лежать в одном формате с completed_at:
    collected_at идёт с миллисекундами и 'Z' (_iso_ms), а now_iso() — секунды и
    '+00:00'. Смешение форматов ломает лексикографическое сравнение метки и
    мусорит колонку. Нечитаемое -> None: log_run тогда поставит момент записи.
    """
    dt = _parse_date_string(str(value))
    return dt.isoformat(timespec="seconds") if dt else None


def to_iso(value):
    if not value:
        return ""
    if isinstance(value, str):
        trimmed = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", trimmed):
            value = float(trimmed)
        else:
            dt = _parse_date_string(trimmed)
            return _iso_ms(dt) if dt else ""
    if isinstance(value, (int, float)):
        seconds = value if value < 10000000000 else value / 1000.0
        try:
            return _iso_ms(datetime.fromtimestamp(seconds, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def age_hours(created_at, now=None):
    # JS new Date(createdAt): числовые строки НЕ парсятся (в отличие от to_iso) —
    # сюда всегда приходит ISO-результат to_iso.
    if not created_at or not isinstance(created_at, str):
        return ""
    dt = _parse_date_string(created_at.strip())
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    hours = (now - dt).total_seconds() / 3600
    return max(0, js_round(hours * 10) / 10)


def engagement_rate(likes_raw, comments, shares, saves, views):
    if not views:
        return 0
    likes = likes_raw if isinstance(likes_raw, (int, float)) and likes_raw > 0 else 0
    return js_round((likes + comments + shares + saves) / views * 10000) / 10000


def normalize_url(url):
    raw = ("" if url is None else str(url)).strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return raw
    if not parts.scheme or not parts.netloc:
        return raw
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    default = {"https": 443, "http": 80}.get(scheme)
    origin = f"{scheme}://{host}" + (f":{port}" if port and port != default else "")
    path = parts.path or "/"
    if len(path) > 1:
        path = re.sub(r"/+$", "", path)
    return origin + (path or "/")


def is_apify_error(raw):
    """Строгий детект упавшего Apify-батча (P5.14): есть error И нет id/url.
    Item с error, но с id — данные с пометкой, не потеря."""
    if not isinstance(raw, dict):
        return False
    if raw.get("__apify_error"):
        return True
    err = raw.get("error")
    if err is None or err is False or err == "":
        return False
    return not any(raw.get(k) for k in
                   ("id", "videoId", "aweme_id", "webVideoUrl", "url", "shareUrl"))


def dedupe_by(rows, key, merge=None):
    """Дедуп рядов по ключу с сохранением порядка первого вхождения (H12/M33).

    Повтор ключа сливается в первое вхождение: merge(new, prev) — тот же хук,
    что merge в Sheets.upsert_rows (coalesce), — либо overlay {**prev, **new}.
    Ряды без ключа проходят как есть. Одно видео штатно приходит из двух батчей
    (пересечение хэштегов, related у снежка) — без дедупа upsert плодил строки.
    """
    out, pos = [], {}
    for row in rows:
        k = str(row.get(key, "") or "")
        if not k or k not in pos:
            if k:
                pos[k] = len(out)
            out.append(row)
            continue
        i = pos[k]
        out[i] = merge(row, out[i]) if merge else {**out[i], **row}
    return out
