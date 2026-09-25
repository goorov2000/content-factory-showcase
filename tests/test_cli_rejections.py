import argparse

from cf.cli import cmd_rejection_history
from cf.io import read_json

from tests.fakes import FakeSheets


def ns(**kw):
    return argparse.Namespace(**kw)


BRIEFS = [
    {"brief_id": "b-1", "review_status": "rejected", "rejection_reason": "Слабый референс"},
    {"brief_id": "b-2", "review_status": "rejected", "rejection_reason": "слабый референс"},
    {"brief_id": "b-3", "review_status": "revised", "rejection_reason": "нет CTA"},
    {"brief_id": "b-4", "review_status": "approved", "rejection_reason": ""},
]


def test_groups_and_sorts_by_count(tmp_path):
    # Legacy-строки без кода: группируются по своему тексту (code=None), отчёт не ломается.
    out = tmp_path / "history.json"
    rc = cmd_rejection_history(FakeSheets({"briefs": BRIEFS}), ns(out=str(out)))
    assert rc == 0
    report = read_json(out)
    assert report["total_rejected_or_revised"] == 3
    assert report["reasons"][0] == {"code": None, "reason": "слабый референс",
                                    "count": 2, "brief_ids": ["b-1", "b-2"]}
    assert report["reasons"][1]["reason"] == "нет cta"


def test_groups_by_reason_code(tmp_path):
    # P5.12: записи с кодом [code] группируются по КОДУ (повторы копятся),
    # деталь после скобки на группировку не влияет.
    briefs = [
        {"brief_id": "b-1", "review_status": "rejected",
         "rejection_reason": "[reference_mismatch] URL a"},
        {"brief_id": "b-2", "review_status": "rejected",
         "rejection_reason": "[reference_mismatch] URL b"},
        {"brief_id": "b-3", "review_status": "revised",
         "rejection_reason": "[weak_hook]"},
        {"brief_id": "b-4", "review_status": "rejected",
         "rejection_reason": "старый свободный текст"},  # legacy — не ломает отчёт
    ]
    out = tmp_path / "history.json"
    cmd_rejection_history(FakeSheets({"briefs": briefs}), ns(out=str(out)))
    report = read_json(out)
    assert report["total_rejected_or_revised"] == 4
    top = report["reasons"][0]
    assert top["code"] == "reference_mismatch"
    assert top["count"] == 2
    assert top["brief_ids"] == ["b-1", "b-2"]
    assert top["reason"] == "Референс не соответствует скрипту"
    # legacy-строка присутствует отдельной группой с code=None.
    legacy = [r for r in report["reasons"] if r["code"] is None]
    assert legacy and legacy[0]["reason"] == "старый свободный текст"


def test_legacy_text_equal_to_code_name_does_not_merge_with_coded_group(tmp_path):
    # Legacy-текст, дословно равный имени кода ('other'), НЕ должен слиться с
    # кодированной группой '[other] …' и утопить code=None — ключи разведены.
    briefs = [
        {"brief_id": "b-1", "review_status": "rejected", "rejection_reason": "[other] мелочь"},
        {"brief_id": "b-2", "review_status": "rejected", "rejection_reason": "other"},
    ]
    out = tmp_path / "history.json"
    cmd_rejection_history(FakeSheets({"briefs": briefs}), ns(out=str(out)))
    report = read_json(out)
    assert report["total_rejected_or_revised"] == 2
    coded = [r for r in report["reasons"] if r["code"] == "other"]
    legacy = [r for r in report["reasons"] if r["code"] is None]
    assert len(coded) == 1 and coded[0]["count"] == 1 and coded[0]["brief_ids"] == ["b-1"]
    assert len(legacy) == 1 and legacy[0]["count"] == 1 and legacy[0]["brief_ids"] == ["b-2"]
    assert legacy[0]["reason"] == "other"


def test_normalizes_review_status(tmp_path):
    # P1.10: 'REJECTED'/'Revised ' (заглавные/пробел, ручная правка Sheets) должны
    # попадать в свод так же, как lowercase — иначе строки истории теряются.
    briefs = [
        {"brief_id": "b-1", "review_status": "REJECTED", "rejection_reason": "[weak_hook]"},
        {"brief_id": "b-2", "review_status": "Revised ", "rejection_reason": "[weak_hook]"},
        {"brief_id": "b-3", "review_status": "approved", "rejection_reason": ""},
    ]
    out = tmp_path / "history.json"
    rc = cmd_rejection_history(FakeSheets({"briefs": briefs}), ns(out=str(out)))
    assert rc == 0
    report = read_json(out)
    assert report["total_rejected_or_revised"] == 2
    assert report["reasons"][0]["code"] == "weak_hook"
    assert report["reasons"][0]["count"] == 2
    assert report["reasons"][0]["brief_ids"] == ["b-1", "b-2"]
