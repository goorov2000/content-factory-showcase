"""Coalesce P1.16 — порт n8n Coalesce-Existing-Raw.js (копии идентичны).

Upsert пишет все переданные колонки: для уже существующих raw_id сохраняем
transcript_text (если новый пуст — прежний готовый транскрипт), source_query
(исходная атрибуция первого сбора) и collected_at (first-seen). Остальные
колонки (метрики, caption, processing_status, raw_json, ...) берут свежее.

Поверх порта — media/visual-поля визуального контура (тикет 03, спека
§Sheets-доставка): правило «непустое побеждает пустое». Свежедобытое медиа
и свежий разбор берут верх, а пересбор строки БЕЗ медиа/разбора (пустые
поля) не затирает добытое — visual_status/visual_facts пишет только
cf vision, и любой сбор затирал бы их пустотой.
"""

# Дорогие ПАРЫ визуального контура: статус и его полезная нагрузка живут
# вместе и побеждают ВМЕСТЕ — по непустому статусу входящей стороны.
# Попольный мерж давал противоречие (ревью 14.08): свежий failed со своим
# пустым манифестом поверх прежнего saved оставлял бы СТАРЫЙ манифест при
# НОВОМ статусе failed. Ключи не материализуются, когда статус пуст с обеих
# сторон (строки сборов без медиа-контура не обрастают фантомными колонками).
VISUAL_CONTOUR_PAIRS = (("media_status", "media_manifest"),
                        ("visual_status", "visual_facts"))


def first_non_empty(a, b):
    return a if a is not None and a != "" else b


def index_by_raw_id(rows):
    out = {}
    for row in rows or []:
        if row and row.get("raw_id") not in (None, ""):
            out[row["raw_id"]] = row  # последняя строка на raw_id побеждает (как appendOrUpdate)
    return out


def coalesce_row(incoming, existing=None):
    if not existing:
        return dict(incoming)  # новый raw_id — пишем как есть (копией)
    out = dict(incoming)
    inc_transcript = incoming.get("transcript_text")
    if isinstance(inc_transcript, str) and inc_transcript.strip():
        out["transcript_text"] = inc_transcript  # новый непустой — заменяет
    elif "transcript_text" in existing and existing["transcript_text"] is not None:
        out["transcript_text"] = existing["transcript_text"]
    else:
        out["transcript_text"] = inc_transcript
    out["source_query"] = first_non_empty(existing.get("source_query"),
                                          incoming.get("source_query"))
    out["collected_at"] = first_non_empty(existing.get("collected_at"),
                                          incoming.get("collected_at"))
    for status_field, payload_field in VISUAL_CONTOUR_PAIRS:
        if incoming.get(status_field) not in (None, ""):
            winner = incoming
        elif existing.get(status_field) not in (None, ""):
            winner = existing
        else:
            continue
        out[status_field] = winner.get(status_field)
        out[payload_field] = winner.get(payload_field, "")
    return out


def coalesce_rows(incoming_rows, existing_by_raw_id):
    index = existing_by_raw_id or {}
    return [coalesce_row(row, index.get(row.get("raw_id")) if row else None)
            for row in incoming_rows or []]
