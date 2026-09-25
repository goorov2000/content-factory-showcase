"""Сеялка демо-петли: состав строк, метки, идемпотентность, точность стирания.

Заголовки вкладок заданы ЯВНО по живому листу (2026-07-27) — без этого FakeSheets
выводит их из ключей самой записываемой строки, и потеря поля на алиасной колонке
(reel_id -> published_id, er -> engagement_rate) в тестах невидима.
"""
import argparse
import json

import pytest

from cf.cli import cmd_demo_seed, cmd_demo_wipe
from cf.demoseed import (DEMO_PREFIX, DEMO_STATUS, DemoSeedError, build_rows, seed,
                         wipe)

from tests.fakes import FakeSheets

REELS_HEADERS = ["published_id", "brief_id", "platform", "post_url", "published_at",
                 "creator", "content_owner", "prompt_version", "status", "notes"]
PERF_HEADERS = ["performance_id", "published_id", "brief_id", "prompt_version",
                "platform", "measured_at", "hours_since_publish", "views", "likes",
                "comments", "shares", "saves", "engagement_rate", "views_per_hour",
                "result_label", "result_reason", "eval_notes"]
RUN_LOG_HEADERS = ["run_id", "agent", "trigger_type", "started_at", "completed_at",
                   "status", "input_summary", "output_paths", "errors"]

CONFIG = {
    "tabs": {"briefs": "CF Creative Briefs", "reels": "CF Published Reels",
             "performance": "CF Performance", "run_log": "CF Run Log"},
    "column_aliases": {
        "reels": {"reel_id": "published_id", "production_notes": "notes"},
        "performance": {"reel_id": "published_id", "er": "engagement_rate"},
    },
}

BRIEFS = [
    {"brief_id": "b-1", "formula_id": "short-styling-idea-reel", "prompt_version": "v2",
     "platform": "tiktok", "review_status": "approved"},
    {"brief_id": "b-2", "formula_id": "store-native-skit", "prompt_version": "v1",
     "platform": "tiktok", "review_status": "approved"},
]


def _plan(**overrides):
    plan = {
        "created_at": "2026-07-28",
        "note": "DEMO eval-loop 2026-07-28",
        "reels": [
            {"slug": "ssir-01", "brief_id": "b-1", "formula_id": "short-styling-idea-reel",
             "prompt_version": "v2", "platform": "tiktok",
             "published_at": "2026-07-08T09:00:00+00:00",
             "measurements": [{"days_after": 7, "views": 4200, "likes": 340,
                               "comments": 21, "shares": 33, "saves": 52}]},
            {"slug": "sns-01", "brief_id": "b-2", "formula_id": "store-native-skit",
             "prompt_version": "v1", "platform": "tiktok",
             "published_at": "2026-07-10T09:00:00+00:00",
             "measurements": [{"days_after": 3, "views": 500, "likes": 20,
                               "comments": 2, "shares": 1, "saves": 3},
                              {"days_after": 7, "views": 640, "likes": 41,
                               "comments": 4, "shares": 3, "saves": 6}]},
        ],
    }
    plan.update(overrides)
    return plan


def _sheets(reels=(), performance=()):
    return FakeSheets(
        {"briefs": [dict(b) for b in BRIEFS], "reels": [dict(r) for r in reels],
         "performance": [dict(p) for p in performance], "run_log": []},
        headers={"reels": REELS_HEADERS, "performance": PERF_HEADERS,
                 "briefs": ["brief_id", "formula_id", "prompt_version", "platform",
                            "human_status"],
                 "run_log": RUN_LOG_HEADERS},
        config=CONFIG)


# --- состав строк -------------------------------------------------------------


def test_build_rows_reel_row_shape():
    reels, _ = build_rows(BRIEFS, _plan())
    row = reels[0]
    assert row["reel_id"] == f"{DEMO_PREFIX}ssir-01"
    assert row["brief_id"] == "b-1"
    assert row["prompt_version"] == "v2"
    assert row["published_at"] == "2026-07-08T09:00:00+00:00"
    # .invalid не резолвится никогда — второй предохранитель поверх status
    assert row["post_url"] == "https://demo.invalid/tiktok/DEMO-ssir-01"
    assert row["production_notes"] == "DEMO eval-loop 2026-07-28"


def test_build_rows_every_reel_is_marked_demo():
    """Без status=demo ночной cf collect performance принял бы демо за published."""
    reels, _ = build_rows(BRIEFS, _plan())
    assert reels and all(r["status"] == DEMO_STATUS for r in reels)
    assert all(r["reel_id"].startswith(DEMO_PREFIX) for r in reels)


def test_build_rows_measurement_shape_and_arithmetic():
    _, perf = build_rows(BRIEFS, _plan())
    row = perf[0]
    assert row["performance_id"] == "DEMO-ssir-01-202607150900"
    assert row["reel_id"] == "DEMO-ssir-01"
    assert row["measured_at"] == "2026-07-15T09:00:00+00:00"   # publish + 7 дней
    assert row["hours_since_publish"] == 168
    assert row["views"] == 4200
    # er и views_per_hour считаются, а не берутся из плана: витрина показывает их
    # рядом с просмотрами, разъехавшиеся числа читаются как дефект системы
    assert row["er"] == round((340 + 21 + 33 + 52) / 4200, 4)
    assert row["views_per_hour"] == round(4200 / 168, 2)
    assert row["result_label"] == "unknown"
    assert "metrics_collected" in row["eval_notes"]


def test_build_rows_two_measurements_per_reel_kept_both():
    """Дедуп — работа eval (P5.8), а не сеялки: в лист уходят оба замера."""
    _, perf = build_rows(BRIEFS, _plan())
    sns = [p for p in perf if p["reel_id"] == "DEMO-sns-01"]
    assert len(sns) == 2
    assert {p["hours_since_publish"] for p in sns} == {72, 168}
    assert len({p["performance_id"] for p in sns}) == 2


# --- отказы до записи ---------------------------------------------------------


def test_build_rows_unknown_brief_refuses():
    plan = _plan()
    plan["reels"][0]["brief_id"] = "b-выдуманный"
    with pytest.raises(DemoSeedError, match="не найден"):
        build_rows(BRIEFS, plan)


def test_build_rows_formula_mismatch_refuses():
    plan = _plan()
    plan["reels"][0]["formula_id"] = "не-тот-рецепт"
    with pytest.raises(DemoSeedError, match="не-тот-рецепт"):
        build_rows(BRIEFS, plan)


def test_build_rows_duplicate_slug_refuses():
    plan = _plan()
    plan["reels"][1]["slug"] = "ssir-01"
    with pytest.raises(DemoSeedError, match="дважды"):
        build_rows(BRIEFS, plan)


def test_build_rows_broken_date_refuses():
    plan = _plan()
    plan["reels"][0]["published_at"] = "08.07.2026"
    with pytest.raises(DemoSeedError, match="ISO8601"):
        build_rows(BRIEFS, plan)


def test_seed_refuses_before_writing_anything():
    """Половина инъекции хуже отказа: при битом плане лист остаётся пустым."""
    sheets = _sheets()
    plan = _plan()
    plan["reels"][0]["brief_id"] = "b-нет"
    with pytest.raises(DemoSeedError):
        seed(sheets, plan)
    assert sheets.tables["reels"] == []
    assert sheets.tables["performance"] == []


# --- запись в лист ------------------------------------------------------------


def test_seed_writes_both_tabs_in_batches():
    sheets = _sheets()
    summary = seed(sheets, _plan())
    assert summary == {"reels": 2, "performance": 3,
                       "reels_skipped": 0, "performance_skipped": 0}
    assert sheets.append_rows_calls == 2      # один батч на вкладку, не строка за строкой
    assert len(sheets.tables["reels"]) == 2
    assert len(sheets.tables["performance"]) == 3


def test_seed_survives_live_column_names():
    """Алиасные колонки живого листа: ключ вне заголовков теряется МОЛЧА."""
    sheets = _sheets()
    seed(sheets, _plan())
    reel = sheets.tables["reels"][0]
    assert reel["published_id"] == "DEMO-ssir-01"     # reel_id -> published_id
    assert reel["notes"]                             # production_notes -> notes
    assert reel["status"] == DEMO_STATUS
    perf = sheets.tables["performance"][0]
    assert perf["published_id"] == "DEMO-ssir-01"
    assert perf["engagement_rate"]                   # er -> engagement_rate
    assert not any(str(v) == "" for k, v in perf.items()
                   if k in ("performance_id", "brief_id", "prompt_version", "views"))


def test_seed_is_idempotent():
    sheets = _sheets()
    seed(sheets, _plan())
    summary = seed(sheets, _plan())
    assert summary["reels"] == 0 and summary["performance"] == 0
    assert summary["reels_skipped"] == 2 and summary["performance_skipped"] == 3
    assert len(sheets.tables["reels"]) == 2
    assert len(sheets.tables["performance"]) == 3


def test_seed_logs_run_with_demo_marker():
    sheets = _sheets()
    seed(sheets, _plan(), started_at="2026-07-27T19:00:00+00:00")
    row = sheets.tables["run_log"][-1]
    assert row["agent"] == "demo-seed"
    assert row["status"] == "success"
    assert row["input_summary"].startswith("DEMO:")
    assert row["started_at"] == "2026-07-27T19:00:00+00:00"


# --- стирание -----------------------------------------------------------------


def test_wipe_removes_only_demo_rows():
    sheets = _sheets()
    seed(sheets, _plan())
    # Чужая строка, появившаяся между инъекцией и стиранием (оператор отметил
    # настоящую публикацию), обязана пережить уборку.
    sheets.append_row("reels", {"reel_id": "7652315929052810516", "brief_id": "b-1",
                                "status": "published"})
    sheets.append_row("performance", {"performance_id": "p-live-1",
                                      "reel_id": "7652315929052810516",
                                      "brief_id": "b-1", "views": 9000})
    removed = wipe(sheets)
    assert removed == {"reels": 2, "performance": 3}
    assert [r["published_id"] for r in sheets.tables["reels"]] == ["7652315929052810516"]
    assert [r["performance_id"] for r in sheets.tables["performance"]] == ["p-live-1"]


def test_wipe_without_demo_rows_does_not_touch_sheet():
    """Нечего стирать — боевой лист не переписывается вовсе."""
    sheets = _sheets(reels=[{"published_id": "999", "brief_id": "b-1"}])
    calls = []
    original = sheets.replace_rows
    sheets.replace_rows = lambda tab, rows: (calls.append(tab), original(tab, rows))[1]
    removed = wipe(sheets)
    assert removed == {"reels": 0, "performance": 0}
    assert calls == []
    assert len(sheets.tables["reels"]) == 1


def test_wipe_logs_run_with_demo_marker():
    sheets = _sheets()
    seed(sheets, _plan())
    wipe(sheets, started_at="2026-07-27T19:00:00+00:00")
    row = sheets.tables["run_log"][-1]
    assert row["agent"] == "demo-wipe"
    assert row["input_summary"].startswith("DEMO:")
    assert row["started_at"] == "2026-07-27T19:00:00+00:00"


def test_seed_then_wipe_returns_sheet_to_empty():
    sheets = _sheets()
    seed(sheets, _plan())
    wipe(sheets)
    assert sheets.tables["reels"] == []
    assert sheets.tables["performance"] == []


# --- команды CLI --------------------------------------------------------------


def _args(**kwargs):
    return argparse.Namespace(**kwargs)


def test_cmd_demo_seed_happy_path(tmp_path):
    sheets = _sheets()
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    code = cmd_demo_seed(sheets, _args(plan=str(plan_file), started_at=None))
    assert code == 0
    assert len(sheets.tables["reels"]) == 2 and len(sheets.tables["performance"]) == 3


def test_cmd_demo_seed_missing_plan_logs_failure(tmp_path):
    """Правило №6: даже отказ оставляет след — иначе прогон выглядит несостоявшимся."""
    sheets = _sheets()
    code = cmd_demo_seed(sheets, _args(plan=str(tmp_path / "нет.json"), started_at=None))
    assert code == 1
    assert sheets.tables["reels"] == []
    row = sheets.tables["run_log"][-1]
    assert row["agent"] == "demo-seed" and row["status"] == "failed"


def test_cmd_demo_seed_bad_plan_logs_failure(tmp_path):
    sheets = _sheets()
    plan = _plan()
    plan["reels"][0]["brief_id"] = "b-нет"
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    code = cmd_demo_seed(sheets, _args(plan=str(plan_file), started_at=None))
    assert code == 1
    assert sheets.tables["reels"] == [] and sheets.tables["performance"] == []
    row = sheets.tables["run_log"][-1]
    assert row["status"] == "failed" and row["input_summary"].startswith("DEMO:")


def test_cmd_demo_wipe(tmp_path):
    sheets = _sheets()
    seed(sheets, _plan())
    code = cmd_demo_wipe(sheets, _args(started_at=None))
    assert code == 0
    assert sheets.tables["reels"] == [] and sheets.tables["performance"] == []
