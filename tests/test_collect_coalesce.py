# C1.3 — порт coalesce.test.js (P1.16): повторный сбор не затирает
# transcript_text/source_query/collected_at; метрики и прочее берут свежее.
# Эталон: n8n/cf01-tiktok/code/Coalesce-Existing-Raw.js (копия в snowball идентична).
from pathlib import Path

import pytest

from cf.collect.coalesce import (
    coalesce_row,
    coalesce_rows,
    first_non_empty,
    index_by_raw_id,
)

REPO = Path(__file__).resolve().parents[1]


def fresh_row(**over):
    row = {
        "raw_id": "tiktok_v1",
        "collected_at": "2026-07-21T08:00:00.000Z",
        "platform": "tiktok",
        "source_actor": "clockworks/tiktok-scraper",
        "source_query": "snowball:tiktok_seedA",
        "video_id": "v1",
        "url": "https://tt/v1",
        "author": "acc",
        "caption": "подпись v2",
        "created_at": "2026-07-01T00:00:00.000Z",
        "duration_sec": 30,
        "views": 5000,
        "likes": 400,
        "comments": 20,
        "shares": 5,
        "saves": 3,
        "engagement_rate": 0.0856,
        "age_hours": 480,
        "thumbnail_url": "https://tt/thumb2.jpg",
        "video_url": "https://tt/dl2.mp4",
        "transcript_text": "",
        "raw_json": '{"fresh":true}',
        "processing_status": "raw_saved_subtitle_download_failed",
    }
    row.update(over)
    return row


def existing_row(**over):
    row = {
        "raw_id": "tiktok_v1",
        "collected_at": "2026-07-14T08:00:00.000Z",
        "source_query": "hashtag:#мужскаяодежда",
        "transcript_text": "готовый транскрипт видео",
        "views": 1000,
        "caption": "подпись v1",
    }
    row.update(over)
    return row


# JS: «пустой новый transcript + существующий готовый -> прежний сохранён»
def test_empty_new_transcript_keeps_existing():
    out = coalesce_row(fresh_row(transcript_text=""), existing_row())
    assert out["transcript_text"] == "готовый транскрипт видео"


# JS: «whitespace-only новый transcript -> прежний сохранён»
def test_whitespace_transcript_keeps_existing():
    out = coalesce_row(fresh_row(transcript_text="   \n\t "), existing_row())
    assert out["transcript_text"] == "готовый транскрипт видео"


# JS: «непустой новый transcript -> заменяет прежний»
def test_new_transcript_replaces():
    out = coalesce_row(fresh_row(transcript_text="свежий транскрипт"), existing_row())
    assert out["transcript_text"] == "свежий транскрипт"


# JS: «новый transcript заменяет даже когда у существующего он пуст»
def test_new_transcript_replaces_empty_existing():
    out = coalesce_row(fresh_row(transcript_text="новый"),
                       existing_row(transcript_text=""))
    assert out["transcript_text"] == "новый"


# JS: «source_query -> сохранена исходная атрибуция (hashtag поверх snowball)»
def test_source_query_keeps_original_attribution():
    out = coalesce_row(fresh_row(source_query="snowball:tiktok_seedA"), existing_row())
    assert out["source_query"] == "hashtag:#мужскаяодежда"


# JS: «collected_at -> сохранено время первого сбора»
# Этот инвариант несущий для гейта очереди фан-аута (runner._new_rows_since):
# если collected_at начнёт перетираться при пересборе, каждый повторный сбор
# старого ролика будет считаться новым притоком и ниша встанет в очередь на
# каждом цикле, сжигая платный прогон claude.
def test_collected_at_keeps_first_seen():
    out = coalesce_row(fresh_row(), existing_row())
    assert out["collected_at"] == "2026-07-14T08:00:00.000Z"


# JS: «метрики/caption/status берут свежее значение на существующем raw_id»
def test_metrics_take_fresh_values():
    out = coalesce_row(fresh_row(), existing_row())
    assert out["views"] == 5000
    assert out["caption"] == "подпись v2"
    assert out["processing_status"] == "raw_saved_subtitle_download_failed"
    assert out["raw_json"] == '{"fresh":true}'
    assert out["engagement_rate"] == 0.0856


# JS: «новый raw_id (нет в existing) -> incoming без изменений»
def test_new_raw_id_passthrough_copy():
    fresh = fresh_row(raw_id="tiktok_new", transcript_text="",
                      source_query="snowball:seedZ")
    out = coalesce_row(fresh, None)
    assert out == fresh
    assert out is not fresh  # копия, не тот же объект


# JS: «existing без transcript_text -> откат на incoming (пустой), не undefined»
def test_existing_without_transcript_column():
    out = coalesce_row(fresh_row(transcript_text=""),
                       {"raw_id": "tiktok_v1", "source_query": "hashtag:#x"})
    assert out["transcript_text"] == ""
    assert out["source_query"] == "hashtag:#x"


# JS: «firstNonEmpty: непустое a -> a, пустое a -> b»
def test_first_non_empty():
    assert first_non_empty("hashtag:#x", "snowball:y") == "hashtag:#x"
    assert first_non_empty("", "snowball:y") == "snowball:y"
    assert first_non_empty(None, "b") == "b"


# JS: «indexByRawId: игнорирует пустой/отсутствующий raw_id, последняя строка побеждает»
def test_index_by_raw_id():
    idx = index_by_raw_id([
        {"raw_id": "a", "v": 1},
        {"raw_id": "", "v": 2},
        {"raw_id": "a", "v": 3},
        {"v": 4},
        None,
    ])
    assert idx["a"]["v"] == 3
    assert "" not in idx
    assert len(idx) == 1


# JS: «coalesceRows: смесь существующих и новых raw_id»
def test_coalesce_rows_mixed():
    existing = index_by_raw_id([existing_row()])
    rows = coalesce_rows([
        fresh_row(),
        fresh_row(raw_id="tiktok_v2", transcript_text="", source_query="snowball:s"),
    ], existing)
    assert rows[0]["transcript_text"] == "готовый транскрипт видео"
    assert rows[0]["source_query"] == "hashtag:#мужскаяодежда"
    assert rows[1]["transcript_text"] == ""
    assert rows[1]["source_query"] == "snowball:s"


# JS e2e «повторный сбор — сохранены, метрики свежие» (склейка нод -> coalesce_rows)
def test_e2e_repeat_collection():
    rows = coalesce_rows([fresh_row()], index_by_raw_id([existing_row()]))
    assert len(rows) == 1
    assert rows[0]["transcript_text"] == "готовый транскрипт видео"
    assert rows[0]["source_query"] == "hashtag:#мужскаяодежда"
    assert rows[0]["collected_at"] == "2026-07-14T08:00:00.000Z"
    assert rows[0]["views"] == 5000


# JS e2e «новое видео (пустой лист existing) -> passthrough»
def test_e2e_new_video_passthrough():
    fresh = fresh_row(raw_id="tiktok_brand_new", transcript_text="есть текст")
    rows = coalesce_rows([fresh], {})
    assert rows[0] == fresh


# JS: «обе Coalesce-копии байт-в-байт идентичны» — эталоны заморожены до этапа 6,
# инвариант держим и из pytest (JS-сьют может перестать гоняться раньше).
@pytest.mark.skipif(not (REPO / "n8n").is_dir(),
                    reason="n8n/ (JS-эталон) отсутствует в витринной копии")
def test_js_copies_identical():
    a = (REPO / "n8n/cf01-tiktok/code/Coalesce-Existing-Raw.js").read_bytes()
    b = (REPO / "n8n/cf01b-snowball/code/Coalesce-Existing-Raw.js").read_bytes()
    assert a == b


# --- Тикет 03 визуального контура: media/visual-поля — «непустое побеждает пустое» ---


def test_media_fields_incoming_nonempty_wins():
    # Свежедобытое медиа берёт верх над прежним статусом (в т.ч. failed поверх
    # saved: манифест с диска пропал — прежний saved уже враньё).
    out = coalesce_row(
        fresh_row(media_status="saved", media_manifest='{"frame_count":3}'),
        existing_row(media_status="failed", media_manifest=""))
    assert out["media_status"] == "saved"
    assert out["media_manifest"] == '{"frame_count":3}'


def test_media_fields_empty_incoming_keeps_existing():
    # Пересбор без медиа (пустые поля) не затирает добытое.
    out = coalesce_row(
        fresh_row(media_status="", media_manifest=""),
        existing_row(media_status="saved", media_manifest='{"frame_count":3}'))
    assert out["media_status"] == "saved"
    assert out["media_manifest"] == '{"frame_count":3}'


def test_visual_fields_survive_recollect():
    # visual_status/visual_facts пишет только cf vision — любой сбор несёт их
    # пустыми, и без правила «непустое побеждает пустое» затирал бы разбор.
    out = coalesce_row(
        fresh_row(),
        existing_row(visual_status="done", visual_facts='{"observed_media":"cover"}'))
    assert out["visual_status"] == "done"
    assert out["visual_facts"] == '{"observed_media":"cover"}'


def test_visual_fields_not_materialized_when_absent_both_sides():
    # Строки сборов без визуального контура не обрастают фантомными колонками.
    out = coalesce_row(fresh_row(), existing_row())
    assert "visual_status" not in out
    assert "media_status" not in out


def test_media_pair_wins_together_no_contradictory_mix():
    # Ревью 14.08: статус и манифест — одна пара. Свежий failed (манифест с
    # диска пропал) не должен оставить СТАРЫЙ манифест при НОВОМ статусе:
    # «failed + манифест saved-раза» — противоречие, а не coalesce.
    out = coalesce_row(
        fresh_row(media_status="failed", media_manifest=""),
        existing_row(media_status="saved", media_manifest='{"frame_count":3}'))
    assert out["media_status"] == "failed"
    assert out["media_manifest"] == ""
