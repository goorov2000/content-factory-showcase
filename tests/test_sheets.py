import pytest

from cf.sheets import Sheets, UnknownFieldsError

from tests.fakes import FakeClient, FakeWorksheet

CFG = {
    "spreadsheet_id": "x",
    "service_account_file": "unused",
    "tabs": {"run_log": "CF Run Log", "briefs": "CF Creative Briefs"},
}


def make_sheets(worksheets):
    return Sheets(config=CFG, client=FakeClient(worksheets), retry_delay=0)


def test_read_rows_missing_worksheet_surfaces_typed_without_retries():
    # Вкладки нет — состояние, не сбой (баг приёмки 31.07): WorksheetNotFound
    # обязан дойти до вызывающего своим типом (страница «Деньги» рисует по нему
    # пустое состояние), а не переупаковаться ретраями в RetryError.
    from gspread.exceptions import WorksheetNotFound
    client = FakeClient({})
    s = Sheets(config=CFG, client=client, retry_delay=0)
    with pytest.raises(WorksheetNotFound):
        s.read_rows("run_log")
    assert client.worksheet_calls == 1   # ни одного повтора


def test_read_rows_returns_dicts():
    ws = FakeWorksheet(["run_id", "agent"], [["r1", "profiler"]])
    s = make_sheets({"CF Run Log": ws})
    assert s.read_rows("run_log") == [{"run_id": "r1", "agent": "profiler"}]


def test_read_rows_retries_transient_errors():
    ws = FakeWorksheet(["run_id"], [["r1"]], fail_reads=2)
    s = make_sheets({"CF Run Log": ws})
    assert s.read_rows("run_log") == [{"run_id": "r1"}]


def test_append_row_maps_by_headers():
    ws = FakeWorksheet(["run_id", "agent", "status"])
    s = make_sheets({"CF Run Log": ws})
    s.append_row("run_log", {"agent": "eval", "run_id": "r2"})
    assert ws.rows == [["r2", "eval", ""]]


def test_append_row_not_duplicated_when_error_after_write():
    ws = FakeWorksheet(["run_id", "agent"], fail_after_appends=1)
    s = make_sheets({"CF Run Log": ws})
    s.append_row("run_log", {"run_id": "r1", "agent": "eval"})
    assert ws.rows == [["r1", "eval"]]


def test_append_row_retries_when_error_before_write():
    ws = FakeWorksheet(["run_id", "agent"], fail_appends=1)
    s = make_sheets({"CF Run Log": ws})
    s.append_row("run_log", {"run_id": "r1", "agent": "eval"})
    assert ws.rows == [["r1", "eval"]]


def test_append_row_intentional_duplicate_rows_still_allowed():
    ws = FakeWorksheet(["run_id", "agent"], [["r1", "eval"]])
    s = make_sheets({"CF Run Log": ws})
    s.append_row("run_log", {"run_id": "r1", "agent": "eval"})
    assert ws.rows == [["r1", "eval"], ["r1", "eval"]]


def test_update_row_fields_updates_matching_row():
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"], ["b2", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    assert s.update_row_fields("briefs", "brief_id", "b2", {"review_status": "approved"}) is True
    assert ws.rows[1] == ["b2", "approved"]


def test_update_row_fields_returns_false_when_not_found():
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    assert s.update_row_fields("briefs", "brief_id", "nope", {"review_status": "x"}) is False


def test_read_rows_applies_column_aliases_from_config():
    cfg = {
        "spreadsheet_id": "x",
        "service_account_file": "unused",
        "tabs": {"raw_tiktok": "CF Raw TikTok"},
        "column_aliases": {
            "raw_tiktok": {"source_url": "url", "account": "author", "posted_at": "created_at"}
        },
    }
    ws = FakeWorksheet(["url", "author", "created_at", "views"],
                       [["https://a", "acc", "2026-07-01", "10"]])
    s = Sheets(config=cfg, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    row = s.read_rows("raw_tiktok")[0]
    assert row["source_url"] == "https://a"
    assert row["account"] == "acc"
    assert row["posted_at"] == "2026-07-01"
    assert row["url"] == "https://a"


def test_read_rows_without_aliases_for_tab_unchanged():
    ws = FakeWorksheet(["run_id"], [["r1"]])
    s = make_sheets({"CF Run Log": ws})  # CFG без column_aliases
    assert s.read_rows("run_log") == [{"run_id": "r1"}]


ALIASED_CFG = {
    "spreadsheet_id": "x",
    "service_account_file": "unused",
    "tabs": {"briefs": "CF Creative Briefs", "prompt_versions": "CF Prompt Versions"},
    "column_aliases": {
        "briefs": {"review_status": "human_status", "script": "script_text"},
        "prompt_versions": {"prompt_id": "prompt_key", "changelog": "change_summary"},
    },
}


def make_aliased(worksheets):
    return Sheets(config=ALIASED_CFG, client=FakeClient(worksheets), retry_delay=0)


def test_append_row_writes_canonical_fields_into_actual_columns():
    ws = FakeWorksheet(["brief_id", "human_status", "script_text"])
    s = make_aliased({"CF Creative Briefs": ws})
    s.append_row("briefs", {"brief_id": "b1", "review_status": "pending", "script": "0-3с: хук"})
    assert ws.rows == [["b1", "pending", "0-3с: хук"]]


def test_update_row_fields_translates_field_names_and_key_column():
    ws = FakeWorksheet(["brief_id", "human_status"], [["b1", "pending"]])
    s = make_aliased({"CF Creative Briefs": ws})
    assert s.update_row_fields("briefs", "brief_id", "b1", {"review_status": "approved"}) is True
    assert ws.rows == [["b1", "approved"]]


def test_update_rows_where_translates_match_and_fields():
    ws = FakeWorksheet(["prompt_key", "status", "change_summary"],
                       [["brief-pets", "TRUE", "v1"], ["brief-pets", "FALSE", "v0"]])
    cfg = dict(ALIASED_CFG)
    cfg["column_aliases"] = {"prompt_versions": {"prompt_id": "prompt_key", "active": "status"}}
    s = Sheets(config=cfg, client=FakeClient({"CF Prompt Versions": ws}), retry_delay=0)
    updated = s.update_rows_where("prompt_versions",
                                  {"prompt_id": "brief-pets", "active": "TRUE"},
                                  {"active": "FALSE"})
    assert updated == 1
    assert ws.rows[0] == ["brief-pets", "FALSE", "v1"]


RAW_CFG = {
    "spreadsheet_id": "x",
    "service_account_file": "unused",
    "tabs": {"raw_tiktok": "CF Raw TikTok"},
}


def test_set_column_by_key_creates_column_and_writes_mapped_values():
    ws = FakeWorksheet(["raw_id", "views"], [["t1", "10"], ["t2", "20"], ["t3", "30"]])
    s = Sheets(config=RAW_CFG, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    updated = s.set_column_by_key("raw_tiktok", "raw_id", "niche",
                                  {"t1": "мужская одежда", "t3": "питомцы"})
    assert updated == 2
    assert ws.headers == ["raw_id", "views", "niche"]
    assert ws.rows == [["t1", "10", "мужская одежда"],
                       ["t2", "20", ""],
                       ["t3", "30", "питомцы"]]
    assert ws.update_calls == 1  # вся колонка одним вызовом API


def test_read_rows_requests_unformatted_values():
    """ru_RU-локаль рендерит 19.833 как «19,833», а gspread-numericise съедает
    запятую как разделитель тысяч -> 19833. Чтение обязано брать UNFORMATTED_VALUE."""
    ws = FakeWorksheet(["raw_id", "duration_sec", "engagement_rate"],
                       [["t1", 19833, 4526]],
                       unformatted_rows=[["t1", 19.833, 0.4526]])
    s = Sheets(config=RAW_CFG, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    row = s.read_rows("raw_tiktok")[0]
    assert row["duration_sec"] == 19.833
    assert row["engagement_rate"] == 0.4526


def test_set_column_by_key_keeps_unmapped_values_in_existing_column():
    ws = FakeWorksheet(["raw_id", "niche"], [["t1", "old"], ["t2", "keep"]])
    s = Sheets(config=RAW_CFG, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    updated = s.set_column_by_key("raw_tiktok", "raw_id", "niche", {"t1": "new"})
    assert updated == 1
    assert ws.rows == [["t1", "new"], ["t2", "keep"]]


# ── P1.2: идемпотентный ensure_tab ────────────────────────────────────────────

SEEDS_CFG = {
    "spreadsheet_id": "x",
    "service_account_file": "unused",
    "tabs": {"seeds": "CF Seeds"},
}
SEED_HEADERS = ["seed_url", "seed_type", "niche", "added_at", "active"]


def test_ensure_tab_creates_missing_tab_with_headers():
    client = FakeClient({})
    s = Sheets(config=SEEDS_CFG, client=client, retry_delay=0)
    assert s.ensure_tab("seeds", SEED_HEADERS) is True
    assert client.worksheet("CF Seeds").row_values(1) == SEED_HEADERS


def test_ensure_tab_idempotent_when_headers_present():
    ws = FakeWorksheet(SEED_HEADERS, [["https://a", "video", "pets", "2026-07-01", "TRUE"]])
    client = FakeClient({"CF Seeds": ws})
    s = Sheets(config=SEEDS_CFG, client=client, retry_delay=0)
    assert s.ensure_tab("seeds", SEED_HEADERS) is False
    assert ws.headers == SEED_HEADERS
    assert ws.rows == [["https://a", "video", "pets", "2026-07-01", "TRUE"]]  # ничего не дописано


def test_ensure_tab_backfills_headers_on_existing_headerless_tab():
    # add_worksheet прошёл, а запись заголовков упала на прошлой попытке -> вкладка
    # существует, но пустая. Повторный ensure_tab обязан дописать заголовки.
    ws = FakeWorksheet([], [])
    client = FakeClient({"CF Seeds": ws})
    s = Sheets(config=SEEDS_CFG, client=client, retry_delay=0)
    assert s.ensure_tab("seeds", SEED_HEADERS) is True
    assert ws.row_values(1) == SEED_HEADERS


def test_ensure_tab_retry_after_header_append_fails_once():
    # add_worksheet ок, первая запись заголовков падает -> with_retry повторяет op,
    # видит существующую пустую вкладку и дописывает заголовки.
    client = FakeClient({}, new_ws_fail_appends=1)
    s = Sheets(config=SEEDS_CFG, client=client, retry_delay=0)
    assert s.ensure_tab("seeds", SEED_HEADERS) is True
    assert client.worksheet("CF Seeds").row_values(1) == SEED_HEADERS
    assert client.added == ["CF Seeds"]  # вкладка создана один раз


# ── P1.3: честный результат update_row_fields / update_rows_where ─────────────

def test_update_row_fields_raises_when_field_not_in_headers():
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    with pytest.raises(UnknownFieldsError):
        s.update_row_fields("briefs", "brief_id", "b1", {"ghost_col": "x"})
    assert ws.rows == [["b1", "pending"]]  # ничего не записано


def test_update_row_fields_raises_on_partially_unknown_fields():
    # известное поле есть, но одно поле вне заголовков -> вся операция сигналит скип
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    with pytest.raises(UnknownFieldsError):
        s.update_row_fields("briefs", "brief_id", "b1",
                            {"review_status": "approved", "ghost_col": "y"})
    assert ws.rows == [["b1", "pending"]]  # ни одно поле не записано (fail-fast)


def test_update_rows_where_raises_when_field_not_in_headers():
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    with pytest.raises(UnknownFieldsError):
        s.update_rows_where("briefs", {"brief_id": "b1"}, {"ghost_col": "z"})
    assert ws.rows == [["b1", "pending"]]


def test_update_row_fields_all_fields_present_still_returns_true():
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    assert s.update_row_fields("briefs", "brief_id", "b1", {"review_status": "approved"}) is True
    assert ws.rows == [["b1", "approved"]]


def test_update_row_fields_retries_transient_update_error():
    # fail_updates: update_cell падает один раз -> with_retry повторяет op целиком
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]], fail_updates=1)
    s = make_sheets({"CF Creative Briefs": ws})
    assert s.update_row_fields("briefs", "brief_id", "b1", {"review_status": "approved"}) is True
    assert ws.rows == [["b1", "approved"]]


# ── P1.4: алиасы в set_column_by_key ─────────────────────────────────────────

def test_set_column_by_key_applies_alias_to_key_column():
    # --key source_url, а реальный заголовок — url: без алиаса updated=0 и колонка
    # перезаписалась бы устаревшими значениями.
    ws = FakeWorksheet(["url", "niche"], [["https://a", ""], ["https://b", ""]])
    updated = _aliased_raw_sheets(ws).set_column_by_key(
        "raw_tiktok", "source_url", "niche", {"https://a": "pets", "https://b": "food"})
    assert updated == 2
    assert ws.rows == [["https://a", "pets"], ["https://b", "food"]]


def test_set_column_by_key_applies_alias_to_field_no_duplicate_column():
    # field=review_status, реальный заголовок — human_status: без алиаса создалась бы
    # дублирующая колонка.
    ws = FakeWorksheet(["brief_id", "human_status"], [["b1", "pending"]])
    cfg = {
        "spreadsheet_id": "x", "service_account_file": "unused",
        "tabs": {"briefs": "CF Creative Briefs"},
        "column_aliases": {"briefs": {"review_status": "human_status"}},
    }
    s = Sheets(config=cfg, client=FakeClient({"CF Creative Briefs": ws}), retry_delay=0)
    updated = s.set_column_by_key("briefs", "brief_id", "review_status", {"b1": "approved"})
    assert updated == 1
    assert ws.headers == ["brief_id", "human_status"]  # новая колонка НЕ создана
    assert ws.rows == [["b1", "approved"]]


def _aliased_raw_sheets(ws):
    cfg = {
        "spreadsheet_id": "x", "service_account_file": "unused",
        "tabs": {"raw_tiktok": "CF Raw TikTok"},
        "column_aliases": {"raw_tiktok": {"source_url": "url"}},
    }
    return Sheets(config=cfg, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)


# ── P1.6: UNFORMATTED-чтение в update/set_column ──────────────────────────────

def test_update_rows_where_matches_numeric_key_via_unformatted():
    # views=1000, локаль рендерит «1 000»: матч по FORMATTED провалился бы.
    ws = FakeWorksheet(["raw_id", "views", "niche"],
                       [["t1", "1 000", "old"]],
                       unformatted_rows=[["t1", 1000, "old"]])
    s = Sheets(config=RAW_CFG, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    updated = s.update_rows_where("raw_tiktok", {"views": 1000}, {"niche": "new"})
    assert updated == 1
    assert ws.rows[0] == ["t1", "1 000", "new"]


# ── P1.1: replace_rows (полная замена строк данных для cf restore) ────────────

def test_replace_rows_overwrites_and_clears_stale_rows():
    ws = FakeWorksheet(["raw_id", "views"], [["t1", "1"], ["t2", "2"], ["t3", "3"]])
    s = Sheets(config=RAW_CFG, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    written = s.replace_rows("raw_tiktok", [{"raw_id": "n1", "views": "10"}])
    assert written == 1
    # ниже заголовка осталась ровно одна строка — устаревшие t2/t3 вычищены
    assert s.read_rows("raw_tiktok") == [{"raw_id": "n1", "views": 10}]


def test_replace_rows_can_grow_beyond_existing():
    ws = FakeWorksheet(["raw_id"], [["t1"]])
    s = Sheets(config=RAW_CFG, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    written = s.replace_rows("raw_tiktok",
                             [{"raw_id": "a"}, {"raw_id": "b"}, {"raw_id": "c"}])
    assert written == 3
    assert s.read_rows("raw_tiktok") == [{"raw_id": "a"}, {"raw_id": "b"}, {"raw_id": "c"}]


def test_replace_rows_maps_by_headers_and_aliases():
    ws = FakeWorksheet(["url", "author"], [["https://old", "x"]])
    cfg = {
        "spreadsheet_id": "x", "service_account_file": "unused",
        "tabs": {"raw_tiktok": "CF Raw TikTok"},
        "column_aliases": {"raw_tiktok": {"source_url": "url", "account": "author"}},
    }
    s = Sheets(config=cfg, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    s.replace_rows("raw_tiktok", [{"source_url": "https://new", "account": "y"}])
    assert ws.rows == [["https://new", "y"]]


def test_set_column_by_key_preserves_untouched_numeric_via_unformatted():
    # нетронутую строку set_column переписывает str(rec[field]); при FORMATTED-чтении
    # 1000 вернулось бы как «1 000» и было бы записано текстом.
    ws = FakeWorksheet(["raw_id", "score"],
                       [["t1", "1 000"], ["t2", "500"]],
                       unformatted_rows=[["t1", 1000], ["t2", 500]])
    s = Sheets(config=RAW_CFG, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    updated = s.set_column_by_key("raw_tiktok", "raw_id", "score", {"t2": "999"})
    assert updated == 1
    assert ws.rows == [["t1", "1000"], ["t2", "999"]]


# ── P1.5: идемпотентный append под конкуренцией (скан хвоста) ─────────────────

def test_append_row_not_duplicated_when_foreign_row_interleaves_on_retry():
    # Ответ сервера потерян ПОСЛЕ применения записи, а параллельный аппендер вписал
    # свою строку между попыткой и ретраем -> наша строка уже НЕ последняя. Скан
    # хвоста обязан найти её и не дублировать (проверка только последней строки — нет).
    ws = FakeWorksheet(["run_id", "agent"], fail_after_appends=1,
                       interleave_row=["r-other", "profiler"])
    s = make_sheets({"CF Run Log": ws})
    s.append_row("run_log", {"run_id": "r1", "agent": "eval"})
    assert ws.rows.count(["r1", "eval"]) == 1  # ровно одна наша строка
    assert ["r-other", "profiler"] in ws.rows  # чужая строка не тронута


def test_append_row_still_single_on_after_write_retry_without_interleave():
    # Регрессия: обычный after-write ретрай без интерливинга -> по-прежнему одна строка.
    ws = FakeWorksheet(["run_id", "agent"], fail_after_appends=1)
    s = make_sheets({"CF Run Log": ws})
    s.append_row("run_log", {"run_id": "r1", "agent": "eval"})
    assert ws.rows == [["r1", "eval"]]


def test_append_row_before_write_retry_appends_when_our_row_absent():
    # Сбой ДО записи (наша строка НЕ приземлилась), а в хвосте лежат ЧУЖИЕ строки:
    # скан хвоста не находит нашу -> ретрай дописывает её (никакого ложного дедупа
    # различающихся строк).
    ws = FakeWorksheet(["run_id", "agent"], [["r-a", "x"], ["r-b", "y"]], fail_appends=1)
    s = make_sheets({"CF Run Log": ws})
    s.append_row("run_log", {"run_id": "r1", "agent": "eval"})
    assert ws.rows == [["r-a", "x"], ["r-b", "y"], ["r1", "eval"]]


# ── P3.1: кэш worksheet-хэндлов + инвалидация на APIError ──────────────────────

def test_worksheet_handle_cached_across_reads():
    ws = FakeWorksheet(["run_id"], [["r1"]])
    client = FakeClient({"CF Run Log": ws})
    s = Sheets(config=CFG, client=client, retry_delay=0)
    s.read_rows("run_log")
    s.read_rows("run_log")
    # второе чтение — из кэша хэндла: worksheet() (fetch_sheet_metadata) не дёргается снова
    assert client.worksheet_calls == 1
    assert client.open_by_key_calls == 1


def test_worksheet_handle_recreated_after_api_error():
    ws = FakeWorksheet(["run_id"], [["r1"]])
    client = FakeClient({"CF Run Log": ws})
    s = Sheets(config=CFG, client=client, retry_delay=0)
    s.read_rows("run_log")                 # хэндл закэширован (worksheet_calls == 1)
    ws.fail_reads_api = 1                   # следующее чтение падает APIError -> инвалидация
    assert s.read_rows("run_log") == [{"run_id": "r1"}]  # ретрай пересоздаёт хэндл
    assert client.worksheet_calls == 2     # протухший хэндл сброшен, worksheet() снова


def test_handle_cache_is_instance_level_not_shared():
    # Разные инстансы Sheets (напр. дашборд и отдельный health из P2.11) не делят кэш.
    ws1 = FakeWorksheet(["run_id"], [["r1"]])
    ws2 = FakeWorksheet(["run_id"], [["r2"]])
    s1 = Sheets(config=CFG, client=FakeClient({"CF Run Log": ws1}), retry_delay=0)
    s2 = Sheets(config=CFG, client=FakeClient({"CF Run Log": ws2}), retry_delay=0)
    assert s1.read_rows("run_log") == [{"run_id": "r1"}]
    assert s2.read_rows("run_log") == [{"run_id": "r2"}]


def test_write_paths_share_cached_handle():
    # read + update по одной вкладке одним инстансом -> worksheet() ровно один раз.
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]])
    client = FakeClient({"CF Creative Briefs": ws})
    s = Sheets(config=CFG, client=client, retry_delay=0)
    s.read_rows("briefs")
    s.update_row_fields("briefs", "brief_id", "b1", {"review_status": "approved"})
    assert client.worksheet_calls == 1


# ── P3.2: батч-запись вместо циклов update_cell ───────────────────────────────

def test_update_row_fields_writes_all_changes_in_one_batch():
    ws = FakeWorksheet(["brief_id", "a", "b", "c"], [["b1", "1", "2", "3"]])
    s = make_sheets({"CF Creative Briefs": ws})
    assert s.update_row_fields("briefs", "brief_id", "b1",
                               {"a": "x", "b": "y", "c": "z"}) is True
    assert ws.rows == [["b1", "x", "y", "z"]]
    assert ws.batch_update_calls == 1      # один батч на все поля, не три update_cell
    assert ws.update_cell_calls == 0


def test_update_row_fields_no_batch_when_row_not_found():
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    assert s.update_row_fields("briefs", "brief_id", "zzz", {"review_status": "x"}) is False
    assert ws.batch_update_calls == 0      # нет совпадения -> нет записи


def test_update_rows_where_batches_all_matched_cells_in_one_call():
    ws = FakeWorksheet(["brief_id", "status", "note"],
                       [["b1", "pending", ""], ["b2", "pending", ""], ["b3", "done", ""]])
    s = make_sheets({"CF Creative Briefs": ws})
    updated = s.update_rows_where("briefs", {"status": "pending"}, {"note": "seen"})
    assert updated == 2
    assert ws.rows == [["b1", "pending", "seen"], ["b2", "pending", "seen"], ["b3", "done", ""]]
    assert ws.batch_update_calls == 1      # все совпавшие строки — одним batch_update
    assert ws.update_cell_calls == 0


def test_update_row_fields_batch_raises_on_unknown_field_before_write():
    ws = FakeWorksheet(["brief_id", "review_status"], [["b1", "pending"]])
    s = make_sheets({"CF Creative Briefs": ws})
    with pytest.raises(UnknownFieldsError):
        s.update_row_fields("briefs", "brief_id", "b1",
                            {"review_status": "approved", "ghost": "y"})
    assert ws.rows == [["b1", "pending"]]  # fail-fast: ни одной ячейки не записано
    assert ws.batch_update_calls == 0


# ── P3.7: проекция чтения без тяжёлых колонок (raw_json) ───────────────────────

PROJ_CFG = {
    "spreadsheet_id": "x", "service_account_file": "unused",
    "tabs": {"raw_tiktok": "CF Raw TikTok"},
}


def _proj_sheets(ws):
    return Sheets(config=PROJ_CFG, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)


def test_read_rows_omits_heavy_raw_json_by_default():
    ws = FakeWorksheet(["raw_id", "views", "raw_json"],
                       [["t1", 1000, '{"big": "x"}']])
    row = _proj_sheets(ws).read_rows("raw_tiktok")[0]
    assert row == {"raw_id": "t1", "views": 1000}   # raw_json нет в результате
    assert "raw_json" not in ws.fetched_columns     # и не выкачивался по сети
    assert ws.batch_get_ranges is not None          # чтение шло проекцией


def test_read_rows_include_heavy_returns_raw_json():
    ws = FakeWorksheet(["raw_id", "views", "raw_json"],
                       [["t1", 1000, '{"big": "x"}']])
    row = _proj_sheets(ws).read_rows("raw_tiktok", include_heavy=True)[0]
    assert row == {"raw_id": "t1", "views": 1000, "raw_json": '{"big": "x"}'}
    assert ws.batch_get_ranges is None              # полное чтение, без проекции


def test_read_rows_no_projection_when_tab_has_no_heavy_columns():
    ws = FakeWorksheet(["raw_id", "views"], [["t1", 1000]])
    rows = _proj_sheets(ws).read_rows("raw_tiktok")
    assert rows == [{"raw_id": "t1", "views": 1000}]
    assert ws.batch_get_ranges is None              # нет тяжёлых колонок -> обычное чтение


def test_read_rows_projection_stitches_noncontiguous_light_columns():
    # raw_json В СЕРЕДИНЕ: светлые колонки несмежны -> проекция читает два диапазона
    ws = FakeWorksheet(["a", "raw_json", "b"],
                       [["a1", '{"x": 1}', "b1"], ["a2", '{"x": 2}', "b2"]])
    rows = _proj_sheets(ws).read_rows("raw_tiktok")
    assert rows == [{"a": "a1", "b": "b1"}, {"a": "a2", "b": "b2"}]
    assert "raw_json" not in ws.fetched_columns
    assert len(ws.batch_get_ranges) == 2            # два несмежных диапазона колонок


def test_read_rows_projection_preserves_unformatted_numbers():
    ws = FakeWorksheet(["raw_id", "duration", "raw_json"],
                       [["t1", "19,833", '{"x": 1}']],
                       unformatted_rows=[["t1", 19.833, '{"x": 1}']])
    row = _proj_sheets(ws).read_rows("raw_tiktok")[0]
    assert row["duration"] == 19.833                # проекция тоже читает UNFORMATTED
    assert "raw_json" not in row


def test_read_rows_projection_applies_aliases():
    cfg = {
        "spreadsheet_id": "x", "service_account_file": "unused",
        "tabs": {"raw_tiktok": "CF Raw TikTok"},
        "column_aliases": {"raw_tiktok": {"source_url": "url"}},
    }
    ws = FakeWorksheet(["url", "raw_json"], [["https://a", '{"x": 1}']])
    s = Sheets(config=cfg, client=FakeClient({"CF Raw TikTok": ws}), retry_delay=0)
    row = s.read_rows("raw_tiktok")[0]
    assert row["url"] == "https://a"
    assert row["source_url"] == "https://a"         # алиас применён к спроецированным колонкам
    assert "raw_json" not in row


# ── P3.3 (метод): батч-аппенд append_rows ─────────────────────────────────────

def test_append_rows_appends_all_in_one_call_by_headers():
    ws = FakeWorksheet(["run_id", "agent", "status"])
    s = make_sheets({"CF Run Log": ws})
    s.append_rows("run_log", [
        {"run_id": "r1", "agent": "eval"},
        {"agent": "profiler", "run_id": "r2", "extra": "drop"},  # лишний ключ выброшен
    ])
    assert ws.rows == [["r1", "eval", ""], ["r2", "profiler", ""]]
    assert ws.append_rows_calls == 1        # все строки — одним вызовом append_rows


def test_append_rows_empty_is_noop():
    ws = FakeWorksheet(["run_id", "agent"])
    s = make_sheets({"CF Run Log": ws})
    s.append_rows("run_log", [])
    assert ws.rows == []
    assert ws.append_rows_calls == 0


def test_append_rows_applies_column_aliases():
    ws = FakeWorksheet(["brief_id", "human_status"])
    s = make_aliased({"CF Creative Briefs": ws})
    s.append_rows("briefs", [{"brief_id": "b1", "review_status": "pending"}])
    assert ws.rows == [["b1", "pending"]]   # канонические ключи -> фактические колонки


# ── P3.4: дешёвый счётчик строк (одна колонка) для поллинга роста ─────────────

def test_count_rows_counts_data_rows_cheaply():
    # raw_json — тяжёлая колонка (~90% payload); count_rows читает ОДНУ колонку и
    # не выкачивает вкладку целиком, только число строк данных (минус заголовок).
    ws = FakeWorksheet(["raw_id", "views", "raw_json"],
                       [["t1", 1000, '{"big": "x"}'], ["t2", 2000, '{"big": "y"}']])
    s = _proj_sheets(ws)
    assert s.count_rows("raw_tiktok") == 2
    assert ws.col_values_reads == 1        # ровно одно чтение колонки
    assert ws.fetched_columns is None      # не проекция всей вкладки
    assert ws.get_all_records_calls == 0   # и не полное get_all_records


def test_count_rows_empty_tab_is_zero():
    ws = FakeWorksheet(["raw_id", "views"], [])
    assert _proj_sheets(ws).count_rows("raw_tiktok") == 0


def test_count_rows_retries_transient_error():
    ws = FakeWorksheet(["raw_id"], [["t1"], ["t2"]], fail_reads=1)
    assert _proj_sheets(ws).count_rows("raw_tiktok") == 2


def test_count_rows_recreates_handle_after_api_error():
    ws = FakeWorksheet(["raw_id"], [["t1"]])
    client = FakeClient({"CF Raw TikTok": ws})
    s = Sheets(config=PROJ_CFG, client=client, retry_delay=0)
    s.count_rows("raw_tiktok")
    ws.fail_reads_api = 1                   # протухший хэндл -> инвалидация + пересоздание
    assert s.count_rows("raw_tiktok") == 1
    assert client.worksheet_calls == 2


# ── P3.4: лёгкая проверка связности health — только строка заголовков ──────────

def test_header_reads_only_header_row():
    ws = FakeWorksheet(["run_id", "agent", "status"], [["r1", "eval", "ok"]])
    s = make_sheets({"CF Run Log": ws})
    assert s.header("run_log") == ["run_id", "agent", "status"]
    assert ws.get_all_records_calls == 0   # не выкачивает данные вкладки


# ── M35 (аудит 2026-07-24): TTL кэша заголовков ───────────────────────────────

def test_header_cache_expires_after_ttl(monkeypatch):
    from cf.sheets import Sheets

    class WS:
        calls = 0

        def row_values(self, n):
            WS.calls += 1
            return ["a"] if WS.calls == 1 else ["a", "niche"]

    s = Sheets(config={"tabs": {}}, client=object(), header_ttl=100.0)
    now = {"t": 1000.0}
    monkeypatch.setattr("cf.sheets.time.monotonic", lambda: now["t"])
    ws = WS()
    assert s._read_headers(ws, "raw") == ["a"]
    assert s._read_headers(ws, "raw") == ["a"]             # внутри TTL — из кэша
    now["t"] += 101.0
    assert s._read_headers(ws, "raw") == ["a", "niche"]    # новая колонка видна
