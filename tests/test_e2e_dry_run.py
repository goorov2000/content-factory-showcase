import argparse

from cf.cli import cmd_profile, cmd_rejection_history, cmd_set_review
from cf.evalprep import build_eval_dataset
from cf.io import read_json
from cf.trace import build_trace
from cf.validate import validate_json_file

from tests.fakes import FakeSheets, make_raw_row


def ns(**kw):
    return argparse.Namespace(**kw)


def seed_sheets():
    briefs = [
        {"brief_id": "b-001", "source_pattern_ids": "pets-hook-question-01",
         "formula_id": "pets-question-hook", "prompt_version": "v1",
         "review_status": "approved", "rejection_reason": "", "reviewer_notes": ""},
        {"brief_id": "b-002", "source_pattern_ids": "pets-hook-question-01",
         "formula_id": "pets-question-hook", "prompt_version": "v1",
         "review_status": "pending", "rejection_reason": "", "reviewer_notes": ""},
        {"brief_id": "b-003", "source_pattern_ids": "pets-hook-question-01",
         "formula_id": "pets-question-hook", "prompt_version": "v1",
         "review_status": "rejected", "rejection_reason": "слабый референс",
         "reviewer_notes": ""},
    ]
    reels = [{"reel_id": "reel-1", "brief_id": "b-001", "platform": "tiktok",
              "post_url": "https://tiktok.com/@x/video/9", "posted_at": "2026-07-05"}]
    performance = [{"reel_id": "reel-1", "brief_id": "b-001", "prompt_version": "v1",
                    "views": "50000", "er": "0.07", "measured_at": "2026-07-08"}]
    versions = [{"prompt_id": "brief-pets", "version": "v1",
                 "github_path": "prompts/briefs/pets/talking-head.md", "active": "TRUE"}]
    return FakeSheets({"raw_tiktok": [make_raw_row(i) for i in range(25)],
                       "briefs": briefs, "reels": reels,
                       "performance": performance, "prompt_versions": versions})


def test_full_cycle_dry_run(tmp_path):
    sheets = seed_sheets()

    # 1. Профилирование: батч готов к анализу
    assert cmd_profile(sheets, ns(tab="raw_tiktok", niche=None, since=None,
                                  min_rows=20, out_dir=str(tmp_path))) == 0
    report = read_json(next(tmp_path.glob("*-raw_tiktok-profile.json")))
    assert report["ready_for_analysis"] is True

    # 2. Артефакты цикла валидны по схемам (evidence-цепочка не рвётся)
    assert validate_json_file("patterns", "tests/fixtures/patterns.json") == []
    assert validate_json_file("formula", "tests/fixtures/formula.json") == []
    assert validate_json_file("brief", "tests/fixtures/brief.json") == []

    # 3. Ревью pending-брифа обновляет таблицу
    assert cmd_set_review(sheets, ns(brief_id="b-002", status="approved",
                                     reason="", notes="соответствует формуле")) == 0
    statuses = {b["brief_id"]: b["review_status"] for b in sheets.read_rows("briefs")}
    assert statuses["b-002"] == "approved"

    # 4. Rejection history собирает причины
    out = tmp_path / "rejection_history.json"
    assert cmd_rejection_history(sheets, ns(out=str(out))) == 0
    assert read_json(out)["reasons"][0]["reason"] == "слабый референс"

    # 5. Eval связывает performance с prompt_version и формулой
    dataset = build_eval_dataset(sheets.read_rows("performance"),
                                 sheets.read_rows("briefs"),
                                 sheets.read_rows("prompt_versions"))
    # ключ ведра — «промпт + версия» (namespace по formula_id, см. evalprep._prompt_key)
    assert dataset["by_prompt_version"]["pets-question-hook:v1"]["reels"] == 1
    assert dataset["rows"][0]["formula_id"] == "pets-question-hook"
    assert dataset["reviewer_pass_rate"] == 2 / 3

    # 6. Relationship chain целая
    trace = build_trace("b-001", sheets.read_rows("briefs"),
                        sheets.read_rows("reels"), sheets.read_rows("performance"))
    assert trace["missing_links"] == []

    # 7. Запуски логировались
    assert any(tab == "run_log" for tab, _ in sheets.appended)
