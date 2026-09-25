import json

import pytest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from cf.dashboard.app import create_app
from cf.dashboard.data import (DataCache, assignment_label,
                               creator_slots_from_config, overview_metrics,
                               weekly_target_from_config)
from tests.fakes import FakeSheets


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _client(root, tables):
    """Дашборд поверх FakeSheets с lab_root=root (для чтения index.json формул)."""
    return TestClient(create_app(sheets=FakeSheets(tables), lab_root=root))


class CountingSheets(FakeSheets):
    def __init__(self, tables=None):
        super().__init__(tables)
        self.reads = 0
        self.fail_next = False

    def read_rows(self, tab_key):
        if self.fail_next:
            raise ConnectionError("sheets down")
        self.reads += 1
        return super().read_rows(tab_key)


def test_cache_serves_within_ttl():
    sheets = CountingSheets({"briefs": [{"brief_id": "B1"}]})
    t = [100.0]
    cache = DataCache(sheets, ttl=60, clock=lambda: t[0])
    assert cache.rows("briefs") == [{"brief_id": "B1"}]
    t[0] += 30
    cache.rows("briefs")
    assert sheets.reads == 1          # второе чтение — из кеша
    t[0] += 31
    cache.rows("briefs")
    assert sheets.reads == 2          # TTL истёк — перечитали


def test_cache_degrades_to_stale_on_error():
    sheets = CountingSheets({"briefs": [{"brief_id": "B1"}]})
    t = [100.0]
    cache = DataCache(sheets, ttl=60, clock=lambda: t[0])
    cache.rows("briefs")
    t[0] += 120
    sheets.fail_next = True
    assert cache.rows("briefs") == [{"brief_id": "B1"}]  # старые данные, не исключение
    assert cache.stale is True
    sheets.fail_next = False
    cache.refresh()
    cache.rows("briefs")
    assert cache.stale is False


def test_cache_raises_when_no_fallback():
    sheets = CountingSheets({})
    sheets.fail_next = True
    cache = DataCache(sheets, ttl=60)
    with pytest.raises(ConnectionError):
        cache.rows("briefs")


# --- P3.5: точечная инвалидация одной вкладки + per-tab TTL ------------------


def test_invalidate_drops_only_target_tab():
    sheets = CountingSheets({"briefs": [{"brief_id": "B1"}],
                             "raw_tiktok": [{"source_url": "u"}]})
    t = [0.0]
    cache = DataCache(sheets, ttl=60, clock=lambda: t[0])
    cache.rows("briefs")
    cache.rows("raw_tiktok")
    assert sheets.reads == 2
    cache.invalidate("briefs")             # сбрасываем ТОЛЬКО briefs
    cache.rows("raw_tiktok")               # raw_tiktok всё ещё в кеше
    assert sheets.reads == 2
    cache.rows("briefs")                   # briefs перечитан
    assert sheets.reads == 3


def test_invalidate_clears_stale_flag_for_tab():
    sheets = CountingSheets({"briefs": [{"brief_id": "B1"}]})
    t = [0.0]
    cache = DataCache(sheets, ttl=60, clock=lambda: t[0])
    cache.rows("briefs")
    t[0] += 120
    sheets.fail_next = True
    cache.rows("briefs")                   # из кеша после сбоя -> stale
    assert cache.stale_for("briefs") is True
    cache.invalidate("briefs")
    assert cache.stale_for("briefs") is False


def test_per_tab_ttl_briefs_shorter_than_slow_tabs():
    cache = DataCache(FakeSheets({}), ttl=60, long_ttl=180)
    assert cache.ttl_for("briefs") < cache.ttl_for("raw_tiktok")
    assert cache.ttl_for("run_log") < cache.ttl_for("reels")
    assert cache.ttl_for("performance") == 180


def test_non_briefs_tab_stays_cached_longer_than_briefs():
    sheets = CountingSheets({"briefs": [{"brief_id": "B1"}],
                             "raw_tiktok": [{"source_url": "u"}]})
    t = [0.0]
    cache = DataCache(sheets, ttl=60, long_ttl=180, clock=lambda: t[0])
    cache.rows("briefs")
    cache.rows("raw_tiktok")
    assert sheets.reads == 2
    t[0] = 90                              # briefs протух (60), raw_tiktok — нет (180)
    cache.rows("raw_tiktok")               # ещё из кеша
    assert sheets.reads == 2
    cache.rows("briefs")                   # перечитан
    assert sheets.reads == 3


def make_cache(tables):
    return DataCache(FakeSheets(tables), ttl=60)


TODAY = datetime(2026, 7, 14)


def test_overview_metrics_counts():
    cache = make_cache({
        "raw_tiktok": [
            {"source_url": "u1", "posted_at": "2026-07-01"},
            {"source_url": "u2", "posted_at": "2026-05-01"},   # вне периода 30 дней
        ],
        "raw_instagram": [{"source_url": "u3", "posted_at": "2026-07-10"}],
        # Живой конвейер пишет generated_at (не created_at) и не ведёт колонку niche.
        "briefs": [
            {"brief_id": "B1", "review_status": "pending", "generated_at": "2026-07-12"},
            {"brief_id": "B2", "review_status": "approved", "generated_at": "2026-07-01"},
            {"brief_id": "B3", "review_status": "rejected", "generated_at": "2026-05-01"},
        ],
        # Ссылка обязательна: «вышел» — это ролик, получивший ссылку, а строка
        # без неё описывает намерение (тот же предикат, что у плитки).
        "reels": [
            {"reel_id": "R1", "published_at": "2026-07-05",
             "post_url": "https://tiktok.com/@x/1"},
            {"reel_id": "R2", "published_at": "2026-07-12",
             "post_url": "https://tiktok.com/@x/2"},
        ],
        "performance": [{"reel_id": "R1", "er": "0.083"}],   # ER — доля, не проценты
    })
    m = overview_metrics(cache, days=30, today=TODAY)
    assert m["raw_total"] == 2 and m["raw_tiktok"] == 1 and m["raw_instagram"] == 1
    assert m["briefs_pending"] == 1
    assert m["oldest_pending_days"] == 2     # B1 создан 2026-07-12
    assert m["briefs_created"] == 2          # B1 и B2 в периоде
    assert m["awaiting_stats"] == 1          # R2 без строки в performance
    assert m["reels_published"] == 2


def test_overview_metrics_tolerates_dirty_rows():
    cache = make_cache({
        "raw_tiktok": [{"source_url": "u1", "posted_at": "не дата"}],
        "raw_instagram": [],
        "briefs": [{"brief_id": "B1"}],                      # без статуса и даты
        "reels": [{"reel_id": "R1"}],                        # без даты
        "performance": [{"reel_id": "R1", "er": ""}],        # пустой ER
    })
    m = overview_metrics(cache, days=30, today=TODAY)
    assert m["raw_total"] == 0
    assert m["briefs_pending"] == 0
    assert m["awaiting_stats"] == 0
    assert m["links_week"] == {"raw": 0, "niches": 0, "formulas": 0, "briefs": 0}


# --- P2.3: брифы читают generated_at + нишу из формулы --------------------


def test_overview_metrics_uses_generated_at_for_briefs():
    # Фикстура с реальным полем generated_at (без created_at) -> метрики считаются.
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [],
        "briefs": [
            {"brief_id": "B1", "review_status": "pending", "generated_at": "2026-07-12"},
            {"brief_id": "B2", "review_status": "approved", "generated_at": "2026-07-01"},
        ],
        "reels": [], "performance": [],
    })
    m = overview_metrics(cache, days=30, today=TODAY)
    assert m["briefs_created"] == 2
    assert m["oldest_pending_days"] == 2


def test_overview_metrics_ignores_legacy_created_at():
    # created_at живой конвейер НЕ пишет: оно не должно давать ни счётчик, ни очередь.
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [],
        "briefs": [{"brief_id": "B1", "review_status": "pending",
                    "created_at": "2026-07-12"}],
        "reels": [], "performance": [],
    })
    m = overview_metrics(cache, days=30, today=TODAY)
    assert m["briefs_created"] == 0
    assert m["oldest_pending_days"] is None


def test_briefs_row_renders_generated_at_date_and_derived_niche(tmp_path):
    _write_json(tmp_path / "formulas" / "_approved" / "index.json", {"approved": [
        {"name": "personal-style-system", "niche": "мужской-стиль",
         "path": "formulas/мужской-стиль/personal-style-system.json",
         "version": 1, "approved_at": "2026-07-17T09:46:25+00:00"}]})
    tables = {"briefs": [{"brief_id": "B1", "hook": "хук брифа", "script": "скрипт",
                          "review_status": "pending",
                          "generated_at": "2026-07-12T08:00:00+00:00",
                          "formula_id": "personal-style-system", "references": ""}],
              "run_log": []}
    resp = _client(tmp_path, tables).get("/briefs")
    assert resp.status_code == 200
    html = resp.text
    assert "12.07.2026" in html          # «Дата» из generated_at, не пусто
    assert "мужской стиль" in html        # «Тема» выведена из формулы через index.json


# --- P2.5: единая %-шкала ER (сырая доля в данных, {:.1%} в шаблонах) -------


def test_briefs_card_formats_pattern_er_as_percent(tmp_path):
    from tests.test_dashboard_sections import _origin_repo
    root = _origin_repo(tmp_path)         # паттерн tt-01 несёт evidence.avg_er = 0.16
    tables = {"briefs": [{"brief_id": "b-1", "hook": "х", "script": "с",
                          "review_status": "pending", "generated_at": "2026-07-12",
                          "formula_id": "short-styling-idea-reel",
                          "source_pattern_ids": "tt-01",
                          "references": "https://t.tt/a"}],
              "run_log": []}
    resp = _client(root, tables).get("/briefs?id=b-1")
    assert resp.status_code == 200
    assert "16,0%" in resp.text           # avg ER 0.16 -> fmt_pct, а не "0.16"


def test_lab_formula_card_formats_er_as_percent(tmp_path):
    _write_json(tmp_path / "formulas" / "_approved" / "index.json", {"approved": []})
    _write_json(tmp_path / "formulas" / "стиль" / "f1.json", {
        "name": "f1", "niche": "стиль", "version": 1, "status": "proposed",
        "hook_structure": "x",
        "evidence": {"source_urls": ["https://t/1"], "avg_views": 1000, "avg_er": 0.105}})
    resp = _client(tmp_path, {"run_log": [], "briefs": []}).get("/lab")
    assert resp.status_code == 200
    assert "10,5%" in resp.text           # та же fmt_pct-шкала, что у сценариев


# --- P2.10: per-tab stale + границы периода + метка недели -------------------


def test_cache_stale_is_per_tab():
    # Сбой одной вкладки не должен сбрасываться успехом другой.
    sheets = FakeSheets({"briefs": [{"brief_id": "B1"}], "reels": [{"reel_id": "R1"}]})
    t = [100.0]
    cache = DataCache(sheets, ttl=60, clock=lambda: t[0])
    cache.rows("briefs")
    cache.rows("reels")                   # прогрели обе
    t[0] += 120                           # TTL истёк
    orig = sheets.read_rows

    def selective(tab_key):
        if tab_key == "briefs":
            raise ConnectionError("briefs down")
        return orig(tab_key)

    sheets.read_rows = selective
    assert cache.rows("briefs") == [{"brief_id": "B1"}]   # из кеша -> stale для briefs
    cache.rows("reels")                                   # успех reels НЕ трогает briefs
    assert cache.stale is True
    assert cache.stale_for("briefs") is True
    assert cache.stale_for("reels") is False


def test_overview_metrics_includes_row_exactly_days_ago():
    # today со временем суток; ряд ровно `days` дней назад (полночь) должен ПОПАСТЬ в окно.
    today = datetime(2026, 7, 14, 15, 30)
    cache = make_cache({
        "raw_tiktok": [{"source_url": "u1", "posted_at": "2026-06-14"}],  # ровно 30 дней
        "raw_instagram": [], "briefs": [], "reels": [], "performance": [],
    })
    m = overview_metrics(cache, days=30, today=today)
    assert m["raw_total"] == 1


def _make_prompt(root, niche):
    """Кладёт prompts/briefs/{niche}/reel.md — ФАЙЛ промпта темы.

    Одного файла для «тема производит» недостаточно (queues.prompt_state): нужна
    ещё активная строка prompt_versions, см. _active_version."""
    p = root / "prompts" / "briefs" / niche / "reel.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("prompt", encoding="utf-8")


def _active_version(niche):
    """Активная строка CF Prompt Versions — вторая половина готовности темы."""
    return {"prompt_id": f"brief-{niche}-reel", "active": "TRUE"}


def test_weekly_target_from_config_present_and_absent():
    # Ключ dashboard.weekly_target читается; при отсутствии — дефолт 70.
    assert weekly_target_from_config({"dashboard": {"weekly_target": 50}}) == 50
    assert weekly_target_from_config({"dashboard": {}}) == 70
    assert weekly_target_from_config({}) == 70
    assert weekly_target_from_config(None) == 70
    assert weekly_target_from_config({"dashboard": {"weekly_target": "мусор"}}) == 70


# ── звенья завода, назначение и публикации за скользящее окно (этап 3) ───────

def test_links_week_counts_each_stage_of_the_factory(tmp_path):
    # Четыре звена за одно окно: собрано → размечено тем → утверждено рецептов →
    # написано сценариев. Одно окно на весь экран (решение владельца 28.07).
    _write_json(tmp_path / "formulas" / "_approved" / "index.json", {"approved": [
        {"name": "f1", "niche": "мужской-стиль", "approved_at": "2026-07-13"},
        {"name": "f2", "niche": "аксессуары", "approved_at": "2026-07-14"},
        {"name": "OLD", "niche": "обувь", "approved_at": "2026-06-01"},
        {"name": "NODATE", "niche": "обувь"},          # даты нет — не «на неделе»
    ]})
    cache = make_cache({
        "raw_tiktok": [
            {"source_url": "u1", "collected_at": "2026-07-13", "niche": "мужской-стиль"},
            {"source_url": "u2", "collected_at": "2026-07-14", "niche": "мужской-стиль"},
            {"source_url": "OLD", "collected_at": "2026-06-01", "niche": "обувь"},
        ],
        "raw_instagram": [
            {"source_url": "i1", "collected_at": "2026-07-14", "niche": "аксессуары"},
            {"source_url": "i2", "collected_at": "2026-07-14", "niche": ""},
        ],
        "briefs": [
            {"brief_id": "A", "review_status": "approved", "generated_at": "2026-07-13"},
            {"brief_id": "B", "review_status": "pending", "generated_at": "2026-07-14"},
            {"brief_id": "OLD", "review_status": "approved", "generated_at": "2026-06-01"},
        ],
        "reels": [], "performance": [],
    })
    m = overview_metrics(cache, today=TODAY, root=tmp_path)
    # raw: u1, u2, i1, i2 (OLD вне окна); темы: мужской-стиль и аксессуары
    #      (у i2 темы нет — в счёт тем не идёт)
    assert m["links_week"] == {"raw": 4, "niches": 2, "formulas": 2, "briefs": 2}
    assert m["window_days"] == 7


def test_links_week_window_edge_is_stable_across_the_day(tmp_path):
    # Строка ровно 7 дней назад ведёт себя предсказуемо и не выпадает из-за
    # времени суток: нижняя граница нормализована к полуночи.
    rows = [{"source_url": "EDGE", "collected_at": "2026-07-07", "niche": "тема"},
            {"source_url": "OUT", "collected_at": "2026-07-06", "niche": "тема"}]
    for hour in (0, 12, 23):
        cache = make_cache({"raw_tiktok": rows, "raw_instagram": [], "briefs": [],
                            "reels": [], "performance": []})
        m = overview_metrics(cache, today=TODAY.replace(hour=hour, minute=59),
                             root=tmp_path)
        assert m["links_week"]["raw"] == 1, hour


def test_awaiting_assignment_is_a_backlog_not_a_window():
    # Очередь работы, а не throughput: сценарий недельной давности из неё никуда
    # не девается. Назначенный уходит сразу, опубликованный не считается.
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [],
        "briefs": [
            {"brief_id": "A", "review_status": "approved", "generated_at": "2026-07-14"},
            {"brief_id": "OLD", "review_status": "approved", "generated_at": "2026-05-01"},
            {"brief_id": "MINE", "review_status": "approved", "generated_at": "2026-07-13",
             "creator_slot": "криэйтор-1", "assigned_at": "2026-07-13"},
            {"brief_id": "OUT", "review_status": "approved", "generated_at": "2026-07-13"},
            {"brief_id": "P", "review_status": "pending", "generated_at": "2026-07-14"},
        ],
        "reels": [{"reel_id": "R1", "brief_id": "OUT", "published_at": "2026-07-14",
                   "post_url": "https://tiktok.com/@x/1"}],
        "performance": [],
    })
    m = overview_metrics(cache, today=TODAY)
    assert m["awaiting_assignment"] == 2       # A и OLD; MINE назначен, OUT вышел, P не одобрен
    assert m["assigned_week"] == 1             # MINE — среднее полукольцо


def test_published_tile_does_not_count_demo_rows_or_rows_without_a_link():
    # 35 строк снимка 28.07 помечены как демо eval-петли: без фильтра плитка
    # показала бы съёмку, которой не было. Строка без ссылки — намерение, не факт.
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [], "briefs": [],
        "reels": [
            {"reel_id": "REAL", "published_at": "2026-07-13",
             "post_url": "https://tiktok.com/@x/1"},
            {"reel_id": "DEMO", "published_at": "2026-07-13", "status": "demo",
             "post_url": "https://demo.invalid/1"},
            {"reel_id": "NOLINK", "published_at": "2026-07-13", "post_url": ""},
        ],
        "performance": [],
    })
    assert overview_metrics(cache, today=TODAY)["reels_published_week"] == 1


# ── слоты исполнителей и метка назначения (2026-07-28, С4) ───────────────────

def test_creator_slots_from_config_present_and_absent():
    # Пустой или отсутствующий ключ — не авария: экран остаётся, кнопка прячется
    # с внятной причиной (проверка самой кнопки — в тестах маршрутов).
    cfg = {"dashboard": {"creator_slots": ["криэйтор-1", " криэйтор-2 "]}}
    assert creator_slots_from_config(cfg) == ["криэйтор-1", "криэйтор-2"]
    assert creator_slots_from_config({"dashboard": {}}) == []
    assert creator_slots_from_config({}) == []
    assert creator_slots_from_config(None) == []
    assert creator_slots_from_config({"dashboard": {"creator_slots": "мусор"}}) == []
    assert creator_slots_from_config({"dashboard": {"creator_slots": [""]}}) == []


def test_assignment_label_reads_slot_and_date():
    assert assignment_label({"creator_slot": "криэйтор-1",
                             "assigned_at": "2026-07-28T10:00:00+00:00"}) \
        == "криэйтор-1, с 28.07"
    # Битая/пустая дата назначение не отменяет: сам факт важнее подписи.
    assert assignment_label({"creator_slot": "криэйтор-1", "assigned_at": ""}) \
        == "криэйтор-1"
    assert assignment_label({"creator_slot": "криэйтор-1",
                             "assigned_at": "не дата"}) == "криэйтор-1"
    assert assignment_label({"creator_slot": "  "}) == ""
    assert assignment_label({}) == ""


def test_weekly_target_default_seventy_and_override():
    cache = make_cache({"raw_tiktok": [], "raw_instagram": [], "briefs": [],
                        "reels": [], "performance": []})
    assert overview_metrics(cache, today=TODAY)["weekly_target"] == 70
    assert overview_metrics(cache, today=TODAY, weekly_target=50)["weekly_target"] == 50


def test_briefs_approved_week_counts_a_sliding_seven_days():
    # Окно СКОЛЬЗЯЩЕЕ, не календарное (решение владельца 2026-07-28): календарная
    # неделя давала 0/70 во вторник, когда все сценарии одобрены в воскресенье.
    # TODAY = 2026-07-14, окно с 2026-07-07 включительно.
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [],
        "briefs": [
            {"brief_id": "A", "review_status": "approved", "generated_at": "2026-07-13"},
            {"brief_id": "B", "review_status": "approved", "generated_at": "2026-07-14"},
            # Воскресенье прошлой календарной недели: в ISO-неделю НЕ попадало,
            # в скользящее окно попадает — ровно тот сценарий, из-за которого
            # владелец и потребовал скользящее окно.
            {"brief_id": "C", "review_status": "approved", "generated_at": "2026-07-12"},
            {"brief_id": "OLD", "review_status": "approved", "generated_at": "2026-07-01"},
            {"brief_id": "D", "review_status": "pending", "generated_at": "2026-07-14"},
        ],
        "reels": [], "performance": [],
    })
    m = overview_metrics(cache, today=TODAY)
    assert m["briefs_approved_week"] == 3      # A, B, C; OLD вне окна, D не approved


def test_sliding_window_boundary_does_not_depend_on_time_of_day():
    # Строка ровно 7 дней назад лежит в окне независимо от времени суток, с
    # которым открыли экран: нижняя граница нормализована к полуночи.
    rows = [{"brief_id": "EDGE", "review_status": "approved",
             "generated_at": "2026-07-07"},
            {"brief_id": "OUT", "review_status": "approved",
             "generated_at": "2026-07-06"}]
    for hour in (0, 9, 23):
        cache = make_cache({"raw_tiktok": [], "raw_instagram": [], "briefs": rows,
                            "reels": [], "performance": []})
        m = overview_metrics(cache, today=TODAY.replace(hour=hour, minute=59))
        assert m["briefs_approved_week"] == 1, hour       # EDGE внутри, OUT — нет


def test_funnel_niches_with_prompt_vs_with_formula(tmp_path):
    # N — тема, которая РЕАЛЬНО производит: рецепт + файл промпта + активная версия
    # (единый предикат queues.niches_ready_for_briefs, тот же, по которому решает
    # генератор сценариев).
    _make_prompt(tmp_path, "niche-a")          # промпт + версия + формула -> в N
    _make_prompt(tmp_path, "niche-b")          # есть промпт, но формулы нет -> не в N
    _write_json(tmp_path / "formulas" / "_approved" / "index.json", {"approved": [
        {"name": "f1", "niche": "niche-a"},
        {"name": "f2", "niche": "niche-c"}]})   # niche-c: формула есть, промпта нет -> не в N
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [],
        "briefs": [{"brief_id": "A", "review_status": "approved",
                    "generated_at": "2026-07-13"}],
        # Ссылка обязательна: строка рила без неё описывает намерение, не результат.
        "reels": [{"reel_id": "R1", "published_at": "2026-07-14",
                   "post_url": "https://tiktok.com/@x/1"}],
        "performance": [],
        "prompt_versions": [_active_version("niche-a"), _active_version("niche-b")],
    })
    m = overview_metrics(cache, today=TODAY, root=tmp_path)
    assert m["niches_with_prompt"] == 1        # N: только niche-a
    assert m["niches_with_formula"] == 2       # M: niche-a, niche-c (distinct)
    assert m["formulas_approved"] == 2         # K: две записи index
    assert m["briefs_approved_week"] == 1
    assert m["reels_published_week"] == 1


def test_funnel_niches_with_prompt_no_overlap_is_zero(tmp_path):
    # Живой кейс P5.6: промпт у одной ниши, approved-формула — у другой, пересечение
    # пусто. Раздельный подсчёт дал бы «1 из 1» (полное покрытие) и спрятал бы
    # единственный блокер к 70 брифам/нед; честный N здесь = 0.
    _make_prompt(tmp_path, "niche-b")          # промпт есть, формулы нет
    _write_json(tmp_path / "formulas" / "_approved" / "index.json", {"approved": [
        {"name": "f1", "niche": "niche-c"}]})  # формула есть, промпта нет
    cache = make_cache({"raw_tiktok": [], "raw_instagram": [],
                        "briefs": [], "reels": [], "performance": [],
                        "prompt_versions": [_active_version("niche-b")]})
    m = overview_metrics(cache, today=TODAY, root=tmp_path)
    assert m["niches_with_prompt"] == 0        # N: пересечение пусто
    assert m["niches_with_formula"] == 1       # M: только niche-c


def test_funnel_excludes_theme_whose_version_was_never_activated(tmp_path):
    # Та самая невидимая дыра: файл промпта сохранён, а cf log-prompt-version не
    # выполнен. Старый предикат считал тему готовой, и «Темы в работе 1 из 1»
    # показывало полное покрытие, пока сценарии по теме не производились вовсе.
    _make_prompt(tmp_path, "niche-a")
    _write_json(tmp_path / "formulas" / "_approved" / "index.json", {"approved": [
        {"name": "f1", "niche": "niche-a"}]})
    cache = make_cache({"raw_tiktok": [], "raw_instagram": [], "briefs": [],
                        "reels": [], "performance": [], "prompt_versions": []})
    m = overview_metrics(cache, today=TODAY, root=tmp_path)
    assert m["niches_with_prompt"] == 0
    assert m["niches_with_formula"] == 1


def test_aging_approved_no_reel_over_ndays(tmp_path):
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [],
        "briefs": [
            # approved, без рила, старый (>7 дн) -> считается
            {"brief_id": "OLD", "review_status": "approved", "generated_at": "2026-06-01"},
            # approved, НО есть опубликованный рил -> не считается
            {"brief_id": "SHOT", "review_status": "approved", "generated_at": "2026-06-01"},
            # approved, без рила, но свежий (<=7 дн) -> не считается
            {"brief_id": "NEW", "review_status": "approved", "generated_at": "2026-07-13"},
            # pending старый -> не approved, не считается
            {"brief_id": "PEND", "review_status": "pending", "generated_at": "2026-06-01"},
        ],
        # Ссылка обязательна: «вышел» — это ролик, получивший её (shot_brief_ids).
        # Строка рила без ссылки сценарий из счёта старения не убирает.
        "reels": [{"reel_id": "R1", "brief_id": "SHOT", "published_at": "2026-06-10",
                   "post_url": "https://tiktok.com/@x/1"}],
        "performance": [],
    })
    m = overview_metrics(cache, today=TODAY, aging_days=7)
    assert m["approved_no_reel_over_ndays"] == 1   # только OLD
    assert m["aging_days"] == 7


def test_overview_metrics_attention_counts_pending_plus_revised():
    # «Требуют внимания» = pending + revised: доработочные брифы (P2.13 сделал их
    # видимыми) не должны выпадать из счётчика внимания. approved туда не попадает.
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [],
        "briefs": [
            {"brief_id": "P", "review_status": "pending", "generated_at": "2026-07-13"},
            {"brief_id": "V", "review_status": "revised", "generated_at": "2026-07-13"},
            {"brief_id": "A", "review_status": "approved", "generated_at": "2026-07-13"},
        ],
        "reels": [], "performance": [],
    })
    m = overview_metrics(cache, today=TODAY)
    assert m["briefs_pending"] == 1
    assert m["briefs_revised"] == 1
    assert m["briefs_attention"] == 2      # pending + revised, approved не в счёте


def test_overview_metrics_attention_dirty_case_normalized():
    # Грязный регистр статуса нормализуется (brief_status.lower/strip), как в P2.13.
    cache = make_cache({
        "raw_tiktok": [], "raw_instagram": [],
        "briefs": [{"brief_id": "V", "review_status": "Revised ",
                    "generated_at": "2026-07-13"}],
        "reels": [], "performance": [],
    })
    m = overview_metrics(cache, today=TODAY)
    assert m["briefs_revised"] == 1
    assert m["briefs_attention"] == 1


def test_overview_renders_gate_row_after_cleanup(tmp_path):
    # Рендер-тест маршрута: overview_metrics зовётся с today=now, поэтому даты фикстур
    # берём относительно now (гарантированно текущая ISO-неделя).
    #
    # Инварианты на карточку темпа (X/70, воронка, старение) сняты НАМЕРЕННО:
    # карточка убрана с «Обзора» решением владельца 2026-07-28 (элементы 10–13).
    # Сами метрики никуда не делись и по-прежнему проверяются выше —
    # test_briefs_approved_week_counts_approved_in_current_iso_week,
    # test_funnel_niches_with_prompt_vs_with_formula, test_aging_approved_no_reel_over_ndays.
    # Здесь остаётся то, что на экране осталось: строка ворот.
    now = datetime.now()
    d_today = now.strftime("%Y-%m-%d")
    d_old = (now - timedelta(days=40)).strftime("%Y-%m-%d")
    _make_prompt(tmp_path, "niche-a")
    _make_prompt(tmp_path, "niche-b")
    _write_json(tmp_path / "formulas" / "_approved" / "index.json", {"approved": [
        {"name": "f1", "niche": "niche-a"}, {"name": "f2", "niche": "niche-c"}]})
    tables = {
        "raw_tiktok": [], "raw_instagram": [],
        "briefs": [
            {"brief_id": "B1", "review_status": "approved", "generated_at": d_today},
            {"brief_id": "OLD", "review_status": "approved", "generated_at": d_old},
        ],
        "reels": [{"reel_id": "R1", "brief_id": "B1", "published_at": d_today}],
        "performance": [], "run_log": [],
        # niche-a производит (файл + активная версия); niche-b — только файл.
        "prompt_versions": [_active_version("niche-a")],
    }
    resp = _client(tmp_path, tables).get("/overview")
    assert resp.status_code == 200
    html = resp.text
    # Ворота видны на главной: niche-c имеет рецепт, но не имеет промпта.
    assert "Включение правил сценариев темы" in html


# ── Кусочки сценариев для «Обзора» (2026-07-28) ──────────────────────────────


def test_brief_snippet_cuts_by_word_and_collapses_whitespace():
    from cf.dashboard.data import brief_snippet
    assert brief_snippet("  первая   строка\nвторая  ") == "первая строка вторая"
    long_text = "слово " * 100
    cut = brief_snippet(long_text)
    assert cut.endswith("…")
    assert len(cut) <= 171                       # 170 символов + многоточие
    assert not cut[:-1].endswith(" ")            # обрыв по границе слова, без хвоста
    assert brief_snippet("") == ""               # пусто остаётся пустым, а не «…»


def test_brief_peek_puts_waiting_first_and_skips_deferred(tmp_path):
    from cf.dashboard.data import brief_peek
    _write_json(tmp_path / "formulas" / "_approved" / "index.json",
                {"approved": [{"name": "f1", "niche": "мужской-стиль"}]})
    tables = {
        "briefs": [
            {"brief_id": "A", "review_status": "approved", "generated_at": "2026-07-20",
             "hook": "старый одобренный", "script": "текст", "formula_id": "f1"},
            {"brief_id": "B", "review_status": "pending", "generated_at": "2026-07-01",
             "hook": "ждёт решения", "script": "текст", "formula_id": "f1"},
            # отложенный капом ждёт квоты, а не человека — вперёд не выносится
            {"brief_id": "C", "review_status": "pending", "generated_at": "2026-07-25",
             "hook": "отложен", "script": "текст", "formula_id": "f1",
             "reviewer_notes": "отложено заводом: кап рецепта"},
        ],
        "reels": [],
    }
    cache = DataCache(FakeSheets(tables))
    cards = brief_peek(cache, root=tmp_path)
    assert [c["brief_id"] for c in cards] == ["B", "C", "A"]   # ждущий, потом по дате
    assert [c["waiting"] for c in cards] == [True, False, False]
    assert cards[0]["niche"] == "мужской-стиль"                # ниша — из index.json
    assert "order" not in cards[0]                             # ключ сортировки не течёт


def test_brief_peek_limits_the_number_of_cards_and_marks_published():
    from cf.dashboard.data import BRIEF_PEEK_LIMIT, brief_peek
    tables = {
        "briefs": [{"brief_id": f"B{i}", "review_status": "approved",
                    "generated_at": "2026-07-20", "hook": f"h{i}", "script": "текст"}
                   for i in range(BRIEF_PEEK_LIMIT + 3)],
        # демо-строка петли рилом не считается, настоящая ссылка — считается
        "reels": [{"reel_id": "R", "brief_id": "B0", "published_at": "2026-07-21",
                   "post_url": "https://tiktok.com/@x/1"},
                  {"reel_id": "D", "brief_id": "B1", "published_at": "2026-07-21",
                   "status": "demo", "post_url": "https://demo.invalid/1"}],
    }
    cards = brief_peek(DataCache(FakeSheets(tables)))
    assert len(cards) == BRIEF_PEEK_LIMIT
    by_id = {c["brief_id"]: c for c in cards}
    assert by_id["B0"]["published"] is True
    assert by_id["B1"]["published"] is False
