import pytest

from cf.publish import MarkPublishedError, mark_published, parse_reel_id

from tests.fakes import FakeSheets


# --- parse_reel_id: TikTok + Instagram ---------------------------------------


@pytest.mark.parametrize("url, expected", [
    ("https://www.tiktok.com/@aypricot/video/7652315929052810516",
     "7652315929052810516"),
    ("https://www.tiktok.com/@user/video/12345?is_copy_url=1&lang=ru", "12345"),
    ("https://www.instagram.com/reel/CxYz-123/", "CxYz-123"),
    ("https://www.instagram.com/reels/AbCdEf/", "AbCdEf"),
    ("https://www.instagram.com/p/Code99/?hl=ru", "Code99"),
    # короткая ссылка без /video/ — не падаем, берём последний сегмент
    ("https://vm.tiktok.com/ZMhabc123/", "ZMhabc123"),
])
def test_parse_reel_id(url, expected):
    assert parse_reel_id(url) == expected


def test_parse_reel_id_unparseable_falls_back_not_crash():
    # URL без пути — фолбэк на сам URL, без исключения
    assert parse_reel_id("https://tiktok.com") == "https://tiktok.com"


# --- mark_published: happy path ----------------------------------------------


def _sheets():
    return FakeSheets({
        "briefs": [{"brief_id": "b-1", "prompt_version": "v3",
                    "review_status": "approved"}],
        "reels": [],
        "run_log": [],
    })


def test_mark_published_writes_reel_row_with_all_fields():
    sheets = _sheets()
    row = mark_published(sheets, "b-1",
                         "https://www.tiktok.com/@x/video/999",
                         notes="без плашки", now="2026-07-21T09:00:00+00:00")
    assert row["reel_id"] == "999"
    assert row["brief_id"] == "b-1"
    assert row["post_url"] == "https://www.tiktok.com/@x/video/999"
    assert row["published_at"] == "2026-07-21T09:00:00+00:00"
    assert row["prompt_version"] == "v3"                 # взято из брифа
    assert row["production_notes"] == "без плашки"
    assert row["platform"] == "tiktok"
    stored = sheets.tables["reels"]
    assert len(stored) == 1 and stored[0]["reel_id"] == "999"


def test_mark_published_logs_run_log():
    sheets = _sheets()
    mark_published(sheets, "b-1", "https://www.tiktok.com/@x/video/999")
    runs = sheets.tables["run_log"]
    assert len(runs) == 1
    assert runs[0]["agent"] == "mark-published"
    assert runs[0]["status"] == "success"
    assert "b-1" in runs[0]["input_summary"]


def test_mark_published_prompt_version_lookup_instagram():
    sheets = FakeSheets({
        "briefs": [{"brief_id": "b-2", "prompt_version": "v7"}],
        "reels": [], "run_log": [],
    })
    row = mark_published(sheets, "b-2", "https://www.instagram.com/reel/Zzz9/")
    assert row["reel_id"] == "Zzz9"
    assert row["prompt_version"] == "v7"
    assert row["platform"] == "instagram"


# --- account: привязка публикации к аккаунту завода (UTM-контур, тикет 01) ----


def test_mark_published_writes_account_column():
    sheets = _sheets()
    row = mark_published(sheets, "b-1", "https://www.tiktok.com/@x/video/999",
                         account="tiktok-1")
    assert row["account"] == "tiktok-1"
    assert sheets.tables["reels"][0]["account"] == "tiktok-1"
    # След в Run Log несёт аккаунт — по нему сверяется разметка публикаций.
    assert "account=tiktok-1" in sheets.tables["run_log"][0]["input_summary"]


def test_mark_published_without_account_writes_empty_column():
    # Обратная совместимость: старый вызов без account пишет пустую колонку —
    # как старые строки, где аккаунта ещё не было.
    sheets = _sheets()
    row = mark_published(sheets, "b-1", "https://www.tiktok.com/@x/video/999")
    assert row["account"] == ""
    assert "account=" not in sheets.tables["run_log"][0]["input_summary"]


# --- validation --------------------------------------------------------------


def test_mark_published_unknown_brief_raises():
    sheets = _sheets()
    with pytest.raises(MarkPublishedError):
        mark_published(sheets, "ghost", "https://tiktok.com/@x/video/1")
    assert sheets.tables["reels"] == []                  # ничего не записано
    assert sheets.tables["run_log"] == []


def test_mark_published_non_http_url_raises():
    sheets = _sheets()
    for bad in ("ftp://x/video/1", "tiktok.com/@x/video/1", "javascript:alert(1)"):
        with pytest.raises(MarkPublishedError):
            mark_published(sheets, "b-1", bad)
    assert sheets.tables["reels"] == []


# ── M23/M32 (аудит 2026-07-24): идемпотентность по (brief_id, reel_id) ────────

def _pub_sheets():
    return FakeSheets({"briefs": [{"brief_id": "B1", "prompt_version": "v1"}],
                       "reels": [], "run_log": []})


def test_mark_published_repeat_same_url_no_duplicate():
    sheets = _pub_sheets()
    url = "https://www.tiktok.com/@acc/video/123"
    first = mark_published(sheets, "B1", url)
    again = mark_published(sheets, "B1", url)          # двойной клик/ретрай CLI
    assert len(sheets.tables["reels"]) == 1
    assert again["reel_id"] == first["reel_id"]


def test_mark_published_second_platform_appends():
    sheets = _pub_sheets()
    mark_published(sheets, "B1", "https://www.tiktok.com/@acc/video/123")
    mark_published(sheets, "B1", "https://www.instagram.com/reel/C8abc/")
    assert len(sheets.tables["reels"]) == 2            # две площадки — легально


# ── Дрейф схемы CF Published Reels: заголовки как в живом листе ───────────────
# Gap#3: mark_published пишет production_notes/reel_id, а колонки листа называются
# notes/published_id; Sheets.append_row поля вне заголовков выбрасывает МОЛЧА
# (src/cf/sheets.py:331-343). Раньше тесты этого не ловили: фейк без явных headers
# выводил их из ключей самой записываемой строки, поэтому «сохранялось» что угодно.

# Фактические заголовки боевой вкладки CF Published Reels (26.07.2026).
LIVE_REELS_HEADERS = ["published_id", "brief_id", "platform", "post_url",
                      "published_at", "creator", "content_owner", "prompt_version",
                      "status", "notes"]
# column_aliases.reels из cf.config.json: канонические имена -> колонки живого листа.
REELS_ALIASES = {"column_aliases": {"reels": {"reel_id": "published_id",
                                              "production_notes": "notes"}}}


def _live_sheets(config=None):
    return FakeSheets(
        {"briefs": [{"brief_id": "B1", "prompt_version": "v1"}],
         "reels": [], "run_log": []},
        headers={"reels": LIVE_REELS_HEADERS}, config=config)


def test_mark_published_without_aliases_loses_notes_and_reel_id():
    # Без алиасов оба канонических поля не совпадают с колонками листа и молча
    # теряются — тот самый дефект, который был невидим в тестах.
    sheets = _live_sheets()
    mark_published(sheets, "B1", "https://www.tiktok.com/@x/video/999",
                   notes="снято без плашки")
    stored = sheets.tables["reels"][0]
    assert stored["notes"] == ""              # production_notes не доехал
    assert stored["published_id"] == ""       # reel_id тоже
    assert "production_notes" not in stored
    assert sheets.missing_columns("reels", ("production_notes", "reel_id")) == [
        "production_notes", "reel_id"]


def test_mark_published_with_aliases_stores_notes_in_live_columns():
    # С column_aliases.reels строка ложится в фактические колонки, а чтение отдаёт
    # канонические имена — идемпотентность и trace по reel_id продолжают работать.
    sheets = _live_sheets(config=REELS_ALIASES)
    mark_published(sheets, "B1", "https://www.tiktok.com/@x/video/999",
                   notes="снято без плашки")
    stored = sheets.tables["reels"][0]
    assert stored["notes"] == "снято без плашки"
    assert stored["published_id"] == "999"
    assert sheets.missing_columns("reels", ("production_notes", "reel_id")) == []
    assert sheets.read_rows("reels")[0]["reel_id"] == "999"


def test_mark_published_idempotent_on_live_headers():
    # Повтор на живой схеме не должен плодить дубль: сверка идёт по reel_id из
    # read_rows, а он приезжает из колонки published_id только через алиасы.
    sheets = _live_sheets(config=REELS_ALIASES)
    url = "https://www.tiktok.com/@x/video/999"
    mark_published(sheets, "B1", url)
    mark_published(sheets, "B1", url)
    assert len(sheets.tables["reels"]) == 1


def test_account_column_is_registered_so_drift_is_seen_before_publication():
    # Как creator_slot/assigned_at у назначения: отсутствие колонки account в
    # живом листе должно быть видно health-чеку и cf status ДО того, как
    # публикация молча потеряет привязку к аккаунту (append_row пишет мягко).
    from cf.sheets import EXPECTED_COLUMNS, schema_drift
    assert "account" in EXPECTED_COLUMNS["reels"]
    sheets = _live_sheets()          # заголовки живой вкладки, колонки account нет
    drift = schema_drift(sheets, {"reels": EXPECTED_COLUMNS["reels"]})
    assert "account" in drift.get("reels", [])
